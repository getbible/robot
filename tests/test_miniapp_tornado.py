import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlencode

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application, URLSpec

from modules.catalog import TranslationOption
from modules.contributions import ContributionStore
from modules.miniapp_api import (
    MiniAppApi,
    MiniAppHttpRequest,
    MiniAppHttpResponse,
)
from modules.miniapp_auth import TelegramInitDataValidator
from modules.miniapp_sessions import MiniAppLaunchStore, MiniAppSessionStore
from modules.miniapp_tornado import (
    MAX_MINI_APP_REQUEST_BYTES,
    ClientAddressResolver,
    MiniAppServer,
    MiniAppStaticHandler,
    miniapp_api_handlers,
)
from modules.preferences import SearchDefaults, UserPreferences

_INTEGRATION_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
_INTEGRATION_ORIGIN = "https://robot.example"


def _signed_init_data(user_id: int) -> str:
    fields = {
        "auth_date": "1700000000",
        "query_id": "transport-integration-query",
        "user": json.dumps(
            {"id": user_id, "first_name": "Grace"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(
        b"WebAppData",
        _INTEGRATION_TOKEN.encode(),
        hashlib.sha256,
    ).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class _TransportService:
    settings = SimpleNamespace(
        mini_app_max_selections=50,
        search_timeout=150.0,
    )

    async def translations(self) -> tuple[TranslationOption, ...]:
        return (TranslationOption("kjv", "King James Version", "English"),)


class _TransportPreferences:
    def translation_for(self, user_id: int) -> str:
        return "kjv"

    def preferences_for(self, user_id: int) -> UserPreferences:
        return UserPreferences("kjv", SearchDefaults(), None)

    def update_preferences(
        self,
        user_id: int,
        *,
        translation: str,
    ) -> UserPreferences:
        return UserPreferences(translation, SearchDefaults(), None)


class _TransportLimiter:
    async def acquire(self, **kwargs: object) -> None:
        return None


class _RecordingApi:
    def __init__(self) -> None:
        self.requests: list[MiniAppHttpRequest] = []

    async def handle(self, request: MiniAppHttpRequest) -> MiniAppHttpResponse:
        self.requests.append(request)
        return MiniAppHttpResponse(
            status=202,
            headers={
                "Content-Type": "application/json",
                "X-Adapter-Test": "forwarded",
            },
            body=b'{"accepted":true}',
        )


class MiniAppTornadoAdapterTestCase(AsyncHTTPTestCase):
    def setUp(self) -> None:
        self.api = _RecordingApi()
        self.cleanup_requests: list[MiniAppHttpRequest] = []
        self.static_directory = tempfile.TemporaryDirectory()
        root = Path(self.static_directory.name)
        (root / "index.html").write_text("<p>index</p>", encoding="utf-8")
        (root / "app.js").write_text("export {};", encoding="utf-8")
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        self.static_directory.cleanup()

    async def cleanup(self, request: MiniAppHttpRequest) -> int:
        self.cleanup_requests.append(request)
        return 204

    def get_app(self) -> Application:
        resolver = ClientAddressResolver(("127.0.0.1/32", "::1/128"))
        handlers = [
            *miniapp_api_handlers(
                self.api,
                public_path="/app",
                address_resolver=resolver,
                cleanup_session=self.cleanup,
            ),
            *miniapp_api_handlers(
                self.api,
                public_path="/without-cleanup",
                address_resolver=resolver,
            ),
            URLSpec(
                r"/static/(.*)",
                MiniAppStaticHandler,
                {
                    "path": self.static_directory.name,
                    "default_filename": "index.html",
                },
            ),
        ]
        return Application(handlers, serve_traceback=False)

    def test_adapter_forwards_supported_http_methods_and_response_metadata(self) -> None:
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                response = self.fetch(
                    "/app/api/v1/resource?test=1",
                    method=method,
                    headers={"X-Forwarded-For": "198.51.100.25"},
                    body=b"{}" if method in {"POST", "PUT", "PATCH"} else None,
                    allow_nonstandard_methods=True,
                )
                self.assertEqual(response.code, 202)
                self.assertEqual(response.headers["X-Adapter-Test"], "forwarded")
                self.assertEqual(response.body, b'{"accepted":true}')

        self.assertEqual(
            [request.method for request in self.api.requests],
            ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        )
        for request in self.api.requests:
            self.assertEqual(request.target, "/app/api/v1/resource?test=1")
            self.assertEqual(request.client_key, "198.51.100.25")

    def test_cleanup_endpoint_enforces_its_independent_method_contract(self) -> None:
        options = self.fetch(
            "/app/api/v1/cleanup",
            method="OPTIONS",
        )
        self.assertEqual(options.code, 204)
        self.assertEqual(options.headers["Allow"], "POST, OPTIONS")
        self.assertEqual(options.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(options.headers["X-Content-Type-Options"], "nosniff")

        rejected = self.fetch("/app/api/v1/cleanup")
        self.assertEqual(rejected.code, 405)
        self.assertEqual(rejected.headers["Allow"], "POST, OPTIONS")

        unavailable = self.fetch(
            "/without-cleanup/api/v1/cleanup",
            method="POST",
            body=b"",
        )
        self.assertEqual(unavailable.code, 404)

        accepted = self.fetch(
            "/app/api/v1/cleanup",
            method="POST",
            headers={"Origin": "https://robot.example"},
            body=b"",
        )
        self.assertEqual(accepted.code, 204)
        self.assertEqual(len(self.cleanup_requests), 1)
        self.assertEqual(self.cleanup_requests[0].method, "POST")
        self.assertEqual(self.api.requests, [])

    def test_streaming_body_limits_are_route_specific(self) -> None:
        oversized_ordinary = self.fetch(
            "/app/api/v1/preferences",
            method="POST",
            body=b"x" * (MAX_MINI_APP_REQUEST_BYTES + 1),
        )
        oversized_lookalike = self.fetch(
            "/app/api/v1/x/api/v1/bookmarks/backup",
            method="POST",
            body=b"x" * (MAX_MINI_APP_REQUEST_BYTES + 1),
        )
        oversized_wrong_method = self.fetch(
            "/app/api/v1/bookmarks/backup",
            method="PUT",
            body=b"x" * (MAX_MINI_APP_REQUEST_BYTES + 1),
        )
        allowed_backup = self.fetch(
            "/app/api/v1/bookmarks/backup",
            method="POST",
            body=b"x" * (MAX_MINI_APP_REQUEST_BYTES + 1),
        )
        # Contribution batches deliberately share the ordinary bound: the drip
        # transport never needs a special body budget.
        oversized_events = self.fetch(
            "/app/api/v1/contributions/events",
            method="POST",
            body=b"x" * (MAX_MINI_APP_REQUEST_BYTES + 1),
        )

        # Tornado rejects an over-limit Content-Length at the HTTP transport
        # boundary with 400; a chunked overrun raised by the handler is 413.
        self.assertIn(oversized_ordinary.code, (400, 413))
        self.assertIn(oversized_lookalike.code, (400, 413))
        self.assertIn(oversized_wrong_method.code, (400, 413))
        self.assertEqual(allowed_backup.code, 202)
        self.assertIn(oversized_events.code, (400, 413))
        self.assertEqual(len(self.api.requests), 1)
        self.assertEqual(
            self.api.requests[-1].body,
            b"x" * (MAX_MINI_APP_REQUEST_BYTES + 1),
        )

    def test_static_shell_and_assets_receive_distinct_hardened_cache_headers(self) -> None:
        shell = self.fetch("/static/")
        self.assertEqual(shell.code, 200)
        self.assertEqual(shell.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(shell.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(shell.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'none'", shell.headers["Content-Security-Policy"])
        self.assertIn("noindex", shell.headers["X-Robots-Tag"])

        asset = self.fetch("/static/app.js")
        self.assertEqual(asset.code, 200)
        self.assertEqual(
            asset.headers["Cache-Control"],
            "no-cache, max-age=0, must-revalidate",
        )


class ContributionTransportIntegrationTestCase(AsyncHTTPTestCase):
    """Exercise the real HTTP, API, session, and SQLite drip-sync boundary."""

    def setUp(self) -> None:
        self.database_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.database_directory.name) / "contributions.sqlite3"
        self.contributions = ContributionStore(path=str(self.database_path))
        self.contributions.submit_application(42, first_name="Grace")
        self.contributions.decide_application(42, "approved", actor="admin")
        self.api = MiniAppApi(
            service=_TransportService(),  # type: ignore[arg-type]
            preferences=_TransportPreferences(),  # type: ignore[arg-type]
            limiter=_TransportLimiter(),  # type: ignore[arg-type]
            sessions=MiniAppSessionStore(max_sessions=10, ttl_seconds=600),
            launches=MiniAppLaunchStore(max_launches=10, ttl_seconds=300),
            validator=TelegramInitDataValidator(
                _INTEGRATION_TOKEN,
                wall_clock=lambda: 1_700_000_000,
            ),
            public_url=f"{_INTEGRATION_ORIGIN}/getbible",
            post_scripture=AsyncMock(return_value=(101,)),
            contributions=self.contributions,
        )
        super().setUp()

    def tearDown(self) -> None:
        try:
            super().tearDown()
        finally:
            self.contributions.close()
            self.database_directory.cleanup()

    def get_app(self) -> Application:
        return Application(
            miniapp_api_handlers(
                self.api,
                public_path="/getbible",
                address_resolver=ClientAddressResolver(("127.0.0.1/32", "::1/128")),
            ),
            serve_traceback=False,
        )

    def test_signed_exchange_batched_events_and_restart_safe_replay(self) -> None:
        session_response = self.fetch(
            "/getbible/api/v1/session",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": _INTEGRATION_ORIGIN,
            },
            body=json.dumps({"init_data": _signed_init_data(42)}).encode(),
        )

        self.assertEqual(session_response.code, 201)
        self.assertIsNone(session_response.headers.get("X-Contribution-Token"))
        session_payload = json.loads(session_response.body)
        self.assertTrue(session_payload["contributions"]["can_contribute"])
        session_token = session_payload["session_token"]
        # The contributor token rides only inside the JSON payload, and only
        # approved contributors ever receive one.
        contribution_token = session_payload["contributions"]["contribution_token"]
        self.assertRegex(contribution_token, r"\Agbc_[A-Za-z0-9_-]{43}\Z")

        # The drip transport sends the same plain session bearer search uses.
        batch_document = {
            "events": [
                {
                    "client_event_id": "baseline:topic_upsert:0011223344556677",
                    "type": "topic_upsert",
                    "topic": {
                        "local_topic_id": "local.grace",
                        "name": "Grace",
                        "color": "#bbf7d0",
                    },
                },
                {
                    "client_event_id": "baseline:verse_add:8899aabbccddeeff",
                    "type": "verse_add",
                    "topic": {
                        "local_topic_id": "local.grace",
                        "name": "Grace",
                        "color": "#bbf7d0",
                    },
                    "verse": {"book": 43, "chapter": 3, "verse": 16},
                },
            ],
            "disclosure_acknowledged": True,
            "contribution_token": contribution_token,
        }
        batch_body = json.dumps(batch_document, separators=(",", ":")).encode()
        batch_headers = {
            "Authorization": f"Bearer {session_token}",
            "Content-Type": "application/json",
            "Origin": _INTEGRATION_ORIGIN,
        }

        first_response = self.fetch(
            "/getbible/api/v1/contributions/events",
            method="POST",
            headers=batch_headers,
            body=batch_body,
        )
        self.assertEqual(first_response.code, 200)
        first_payload = json.loads(first_response.body)
        self.assertEqual(first_payload["accepted"], 2)
        self.assertEqual(first_payload["replayed"], 0)
        self.assertEqual(first_payload["status"]["state"], "approved")
        self.assertFalse(first_payload["status"]["disclosure_required"])
        self.assertEqual(
            first_payload["status"]["summary"]["events"]["pending"],
            2,
        )
        self.assertEqual(first_payload["catalog"]["revision"], 0)

        # Model a process restart: discard the live connection and make the
        # API use a fresh store. The recorded events and their per-event
        # idempotency must survive because they are durable SQLite state.
        self.contributions.close()
        self.contributions = ContributionStore(path=str(self.database_path))
        self.api._contributions = self.contributions

        replay_response = self.fetch(
            "/getbible/api/v1/contributions/events",
            method="POST",
            headers=batch_headers,
            body=batch_body,
        )
        self.assertEqual(replay_response.code, 200)
        replay_payload = json.loads(replay_response.body)
        self.assertEqual(replay_payload["accepted"], 0)
        self.assertEqual(replay_payload["replayed"], 2)
        self.assertEqual(replay_payload["event_ids"], first_payload["event_ids"])

        events = self.contributions.list_events()
        self.assertEqual(
            [event.event_type for event in events],
            ["topic_upsert", "verse_add"],
        )
        self.assertEqual(len({event.id for event in events}), 2)
        self.assertEqual(events[0].local_topic_id, "local.grace")
        self.assertEqual(
            (events[1].book, events[1].chapter, events[1].verse),
            (43, 3, 16),
        )
        self.assertFalse(
            self.contributions.contribution_status(42)["disclosure_required"]
        )

        retired_sync = self.fetch(
            "/getbible/api/v1/contributions/sync",
            method="POST",
            headers=batch_headers,
            body=batch_body,
        )
        self.assertEqual(retired_sync.code, 404)


class MiniAppServerLifecycleTestCase(unittest.IsolatedAsyncioTestCase):
    def settings(self, **changes: object) -> SimpleNamespace:
        values: dict[str, object] = {
            "mini_app_enabled": True,
            "mini_app_public_url": "https://robot.example/getbible",
            "mini_app_trusted_proxy_cidrs": ("127.0.0.1/32", "::1/128"),
            "mini_app_launch_ttl_seconds": 300,
            "mini_app_session_limit": 20,
            "mini_app_session_ttl_seconds": 10_800,
            "mini_app_sessions_per_user": 2,
            "mini_app_max_searches_per_session": 2,
            "mini_app_max_available_selections": 256,
            "mini_app_max_selections": 100,
            "telegram_api_token": (
                "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
            ),
            "mini_app_init_data_max_age_seconds": 300,
            "mini_app_session_exchange_rate_capacity": 10,
            "mini_app_session_exchange_rate_refill_per_second": 0.2,
            "rate_limit_cache_size": 100,
            "contribution_rate_capacity": 60,
            "contribution_rate_refill_per_second": 5.0,
            "mini_app_navigation_rate_cost": 0.25,
            "mini_app_access_log": True,
            "mini_app_max_header_bytes": 16 * 1024,
            "mini_app_idle_timeout_seconds": 30.0,
            "mini_app_body_timeout_seconds": 10.0,
            "mini_app_port": 9201,
            "mini_app_listen": "127.0.0.1",
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def build_server(self, root: Path) -> MiniAppServer:
        return MiniAppServer(
            settings=self.settings(),
            service=Mock(),
            preferences=Mock(),
            limiter=Mock(),
            post_scripture=AsyncMock(return_value=(101,)),
            cleanup_launch=AsyncMock(),
            static_root=root,
        )

    async def test_constructor_validates_enablement_and_packaged_static_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "not enabled"):
            MiniAppServer(
                settings=self.settings(
                    mini_app_enabled=False,
                    mini_app_public_url=None,
                ),
                service=Mock(),
                preferences=Mock(),
                limiter=Mock(),
                post_scripture=AsyncMock(),
            )

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaisesRegex(ValueError, "static root"):
                MiniAppServer(
                    settings=self.settings(),
                    service=Mock(),
                    preferences=Mock(),
                    limiter=Mock(),
                    post_scripture=AsyncMock(),
                    static_root=missing,
                )

    async def test_listener_start_is_idempotent_and_close_is_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = self.build_server(Path(directory))
            transport = Mock()
            transport.close_all_connections = AsyncMock()
            application = Mock()
            with (
                patch(
                    "modules.miniapp_tornado.TornadoApplication",
                    return_value=application,
                ) as application_factory,
                patch(
                    "modules.miniapp_tornado.HTTPServer",
                    return_value=transport,
                ) as server_factory,
            ):
                await server.start()
                await server.start()

            application_factory.assert_called_once()
            server_factory.assert_called_once_with(
                application,
                xheaders=False,
                max_buffer_size=MAX_MINI_APP_REQUEST_BYTES,
                max_body_size=MAX_MINI_APP_REQUEST_BYTES,
                max_header_size=16 * 1024,
                idle_connection_timeout=30.0,
                body_timeout=10.0,
            )
            transport.listen.assert_called_once_with(
                9201,
                address="127.0.0.1",
            )

            await server.close()
            await server.close()

            transport.stop.assert_called_once_with()
            transport.close_all_connections.assert_awaited_once_with()

    async def test_launch_helpers_and_snapshot_delegate_to_owned_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = self.build_server(Path(directory))
            self.addAsyncCleanup(server.close)

            launch = server.create_launch(
                user_id=41,
                target_chat_id=-1001,
                initial_route="search",
                initial_query="grace",
            )
            self.assertEqual(launch.initial_route, "search")
            self.assertIn("?launch=", server.web_url(launch))
            self.assertIn("startapp=", server.direct_url("GetBibleRobot", launch))
            self.assertEqual(
                server.public_web_url,
                "https://robot.example/getbible/",
            )

            with self.assertRaisesRegex(ValueError, "initial_route"):
                server.create_launch(
                    user_id=41,
                    target_chat_id=-1001,
                    initial_route="invalid",  # type: ignore[arg-type]
                )

            server.sessions.snapshot = Mock(return_value={"sessions": 1})
            server.api.snapshot = Mock(return_value={"requests": 2})
            server._cleanup.snapshot = Mock(return_value={"cleanup_pending": 3})
            self.assertEqual(
                server.snapshot(),
                {
                    "sessions": 1,
                    "requests": 2,
                    "cleanup_pending": 3,
                },
            )

    async def test_cleanup_signal_authentication_fails_closed_and_accepts_owner(
        self,
    ) -> None:
        server = object.__new__(MiniAppServer)
        server._public_origin = "https://robot.example"
        server.sessions = Mock()
        server._validator = Mock()
        server._cleanup = Mock()
        server._cleanup.cleanup_now = AsyncMock()
        token = "abcdefghijklmnop"
        init_data = "query_id=owner"
        launch = SimpleNamespace(token="launch")
        session = SimpleNamespace(
            token=token,
            init_data_digest=hashlib.sha256(init_data.encode()).digest(),
            user_id=41,
            query_id="owner-query",
            launch=launch,
        )
        principal = SimpleNamespace(user_id=41, query_id="owner-query")
        server.sessions.get.return_value = session
        server.sessions.touch.return_value = True
        server._validator.validate.return_value = principal

        def request(headers: dict[str, str]) -> MiniAppHttpRequest:
            return MiniAppHttpRequest(
                method="POST",
                target="/getbible/api/v1/cleanup",
                headers=headers,
                body=b"",
                client_key="127.0.0.1",
            )

        self.assertEqual(await server._cleanup_session_request(request({})), 403)
        self.assertEqual(
            await server._cleanup_session_request(
                request({"Origin": "https://robot.example"})
            ),
            401,
        )
        self.assertEqual(
            await server._cleanup_session_request(
                request(
                    {
                        "Origin": "https://robot.example",
                        "Authorization": "Bearer too-short",
                    }
                )
            ),
            401,
        )

        headers = {
            "Origin": "https://robot.example",
            "Authorization": f"Bearer {token}",
        }
        server.sessions.get.return_value = None
        self.assertEqual(await server._cleanup_session_request(request(headers)), 401)

        server.sessions.get.return_value = session
        self.assertEqual(await server._cleanup_session_request(request(headers)), 204)
        server._validator.validate.assert_not_called()
        server._cleanup.cleanup_now.assert_awaited_once_with(launch)


if __name__ == "__main__":
    unittest.main()
