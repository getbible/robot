import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlencode

from tornado.httpclient import HTTPResponse
from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application, URLSpec

from modules.bookmark_backup import MAX_BOOKMARK_BACKUP_REQUEST_BYTES
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
        retired_sync_route = self.fetch(
            "/app/api/v1/contributions/sync",
            method="POST",
            body=b"x" * (MAX_MINI_APP_REQUEST_BYTES + 1),
        )
        allowed_backup = self.fetch(
            "/app/api/v1/bookmarks/backup",
            method="POST",
            body=b"x" * (MAX_MINI_APP_REQUEST_BYTES + 1),
        )
        oversized_backup = self.fetch(
            "/app/api/v1/bookmarks/backup",
            method="POST",
            body=b"x" * (MAX_BOOKMARK_BACKUP_REQUEST_BYTES + 1),
        )

        # Tornado rejects an over-limit Content-Length at the HTTP transport
        # boundary with 400; a chunked overrun raised by the handler is 413.
        self.assertIn(oversized_ordinary.code, (400, 413))
        self.assertIn(oversized_lookalike.code, (400, 413))
        self.assertIn(oversized_wrong_method.code, (400, 413))
        # Contribution PUSH moved to Telegram sendData, so the retired
        # POST contributions/sync route must not keep an escalated body
        # limit as an anonymous large-upload channel.
        self.assertIn(retired_sync_route.code, (400, 413))
        self.assertEqual(allowed_backup.code, 202)
        self.assertIn(oversized_backup.code, (400, 413))
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
    """Exercise the surviving HTTP contribution surface against real SQLite.

    Contribution PUSH now travels over Telegram ``sendData`` and the bot's
    ``web_app_data`` intake, never HTTPS.  What remains on HTTP is read-only
    and authenticated by the plain session bearer: the session bootstrap
    status payload, GET contributions/status, the bundled bookmark catalog,
    and GET contributions/receipt used to settle the browser outbox.
    """

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

    def _exchange_session(self) -> dict:
        response = self.fetch(
            "/getbible/api/v1/session",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": _INTEGRATION_ORIGIN,
            },
            body=json.dumps({"init_data": _signed_init_data(42)}).encode(),
        )
        self.assertEqual(response.code, 201)
        self._session_response = response
        return json.loads(response.body)

    def _authorized_get(
        self,
        path: str,
        token: str,
        headers: dict[str, str] | None = None,
    ) -> HTTPResponse:
        return self.fetch(
            path,
            headers={"Authorization": f"Bearer {token}", **(headers or {})},
        )

    def test_session_exchange_bootstraps_status_without_capability_header(self) -> None:
        bootstrap = self._exchange_session()

        header_names = {name.lower() for name in self._session_response.headers}
        self.assertNotIn("x-contribution-token", header_names)
        self.assertFalse(
            any("contribution" in name for name in header_names),
            header_names,
        )
        self.assertGreaterEqual(len(bootstrap["session_token"]), 16)
        contributions = bootstrap["contributions"]
        self.assertTrue(contributions["enabled"])
        self.assertEqual(contributions["state"], "approved")
        self.assertTrue(contributions["can_contribute"])
        self.assertTrue(contributions["disclosure_required"])
        self.assertEqual(contributions["topics"], [])

    def test_contribution_status_uses_the_plain_session_bearer_only(self) -> None:
        token = self._exchange_session()["session_token"]

        detailed = self._authorized_get(
            "/getbible/api/v1/contributions/status?details=1",
            token,
        )
        self.assertEqual(detailed.code, 200)
        status = json.loads(detailed.body)
        self.assertEqual(status["state"], "approved")
        self.assertTrue(status["can_contribute"])
        self.assertIn("topics", status)
        self.assertIn("summary", status)

        legacy = self._authorized_get(
            "/getbible/api/v1/contributions/status",
            token,
        )
        self.assertEqual(legacy.code, 200)
        self.assertEqual(
            set(json.loads(legacy.body)),
            {"enabled", "state", "can_contribute", "disclosure_required"},
        )

        unauthenticated = self.fetch("/getbible/api/v1/contributions/status?details=1")
        self.assertEqual(unauthenticated.code, 401)

        # The HTTPS write surface is retired end-to-end: PUSH travels only
        # through Telegram sendData and the web_app_data intake.
        write_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": _INTEGRATION_ORIGIN,
        }
        for target in (
            "/getbible/api/v1/contributions/sync",
            "/getbible/api/v1/contributions/events",
        ):
            with self.subTest(target=target):
                removed = self.fetch(
                    target,
                    method="POST",
                    headers=write_headers,
                    body=b"{}",
                )
                self.assertEqual(removed.code, 404)
        patched_status = self.fetch(
            "/getbible/api/v1/contributions/status",
            method="PATCH",
            headers=write_headers,
            body=b"{}",
        )
        self.assertEqual(patched_status.code, 405)
        self.assertEqual(patched_status.headers["Allow"], "GET, OPTIONS")

    def test_bookmark_catalog_serves_and_revalidates_with_etag(self) -> None:
        token = self._exchange_session()["session_token"]

        first = self._authorized_get("/getbible/api/v1/bookmarks/catalog", token)
        self.assertEqual(first.code, 200)
        etag = first.headers.get("ETag")
        self.assertIsNotNone(etag)
        assert etag is not None
        payload = json.loads(first.body)
        self.assertEqual(set(payload), {"revision", "checksum", "catalog"})
        self.assertIsInstance(payload["revision"], int)

        revalidated = self._authorized_get(
            "/getbible/api/v1/bookmarks/catalog",
            token,
            headers={"If-None-Match": etag},
        )
        self.assertEqual(revalidated.code, 304)
        self.assertEqual(revalidated.headers.get("ETag"), etag)
        self.assertEqual(revalidated.body, b"")

    def test_receipt_confirms_committed_snapshot_and_survives_restart(self) -> None:
        token = self._exchange_session()["session_token"]
        sync_id = "transport.integration.0001"

        def receipt_payload(requested_sync_id: str) -> dict:
            response = self._authorized_get(
                f"/getbible/api/v1/contributions/receipt?sync_id={requested_sync_id}",
                token,
            )
            self.assertEqual(response.code, 200)
            return json.loads(response.body)

        self.assertEqual(
            receipt_payload(sync_id),
            {"found": False, "receipt": None},
        )

        # Commit through synchronize_snapshot exactly as the Telegram
        # web_app_data intake does after reassembling a pushed bundle.
        result = self.contributions.synchronize_snapshot(
            42,
            sync_id=sync_id,
            client_id="transport.integration.browser",
            snapshot={
                "topics": [
                    {
                        "id": "local.grace",
                        "name": "Grace",
                        "color": "#bbf7d0",
                    }
                ],
                "assignments": [
                    {
                        "topic_id": "local.grace",
                        "book": 43,
                        "chapter": 3,
                        "verse": 16,
                    }
                ],
            },
            operations=(),
            disclosure_acknowledged=True,
        )
        self.assertEqual((result.accepted, result.replayed), (2, 0))

        committed = receipt_payload(sync_id)
        self.assertTrue(committed["found"])
        receipt = committed["receipt"]
        self.assertEqual(receipt["sync_id"], sync_id)
        self.assertEqual(receipt["accepted"], 2)
        self.assertEqual(receipt["replayed"], 0)
        self.assertEqual(receipt["snapshot_digest"], result.snapshot_digest)
        self.assertEqual(set(receipt["event_ids"]), set(result.event_ids))
        # The HTTP receipt must expose opaque contributor-scoped IDs, not
        # the store's global AUTOINCREMENT row IDs.
        self.assertNotEqual(
            sorted(receipt["event_ids"].values()),
            sorted(result.event_ids.values()),
        )
        for opaque_id in receipt["event_ids"].values():
            self.assertIsInstance(opaque_id, int)
            self.assertGreaterEqual(opaque_id, 1)
        self.assertEqual(
            receipt_payload("transport.integration.9999"),
            {"found": False, "receipt": None},
        )

        # Model a process restart: discard the live connection and make the
        # API use a fresh store. The exact receipt must survive byte for
        # byte because it is durable SQLite state, not process memory.
        self.contributions.close()
        self.contributions = ContributionStore(path=str(self.database_path))
        self.api._contributions = self.contributions

        self.assertEqual(receipt_payload(sync_id), committed)

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
        after_commit = self._authorized_get(
            "/getbible/api/v1/contributions/status?details=1",
            token,
        )
        self.assertEqual(after_commit.code, 200)
        self.assertFalse(json.loads(after_commit.body)["disclosure_required"])


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
