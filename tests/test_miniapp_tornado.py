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
    mini_app_build_id,
    mini_app_menu_web_url,
    miniapp_api_handlers,
    render_mini_app_shell,
)
from modules.preferences import SearchDefaults, UserPreferences

_SHELL_SOURCE = (
    "<!doctype html>\n<html><head><title>getBible.Life</title>\n"
    '<link rel="stylesheet" href="./styles.css">\n'
    '<script src="https://telegram.org/js/telegram-web-app.js?63"></script>\n'
    '<script type="module" src="./app.js"></script>\n'
    '</head><body><img src="./assets/mark.png" alt=""></body></html>\n'
)


def _write_client_tree(root: Path) -> None:
    """Create the smallest packaged client the server accepts."""
    (root / "index.html").write_text(_SHELL_SOURCE, encoding="utf-8")
    (root / "app.js").write_text(
        'import { mark } from "./lib/model.js";\nexport { mark };\n',
        encoding="utf-8",
    )
    (root / "styles.css").write_text("body { margin: 0; }\n", encoding="utf-8")
    (root / "lib").mkdir()
    (root / "lib" / "model.js").write_text(
        'export const mark = "model";\n',
        encoding="utf-8",
    )
    (root / "assets").mkdir()
    (root / "assets" / "mark.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def _lifecycle_settings(**changes: object) -> SimpleNamespace:
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


def _build_lifecycle_server(root: Path) -> MiniAppServer:
    _write_client_tree(root)
    return MiniAppServer(
        settings=_lifecycle_settings(),
        service=Mock(),
        preferences=Mock(),
        limiter=Mock(),
        post_scripture=AsyncMock(return_value=(101,)),
        cleanup_launch=AsyncMock(),
        static_root=root,
    )


class MiniAppClientBuildTestCase(unittest.TestCase):
    def test_build_id_follows_the_packaged_client_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_client_tree(root)
            first = mini_app_build_id(root)
            self.assertRegex(first, r"^[a-f0-9]{16}$")
            self.assertEqual(mini_app_build_id(root), first)

            # Development-only files never change what phones download.
            (root / "node_modules").mkdir()
            (root / "node_modules" / "left-pad.js").write_text("x", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "x.test.mjs").write_text("x", encoding="utf-8")
            (root / "package.json").write_text("{}", encoding="utf-8")
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            (root / ".hidden").write_text("x", encoding="utf-8")
            self.assertEqual(mini_app_build_id(root), first)

            # Any served byte does, including one deep inside the module graph.
            (root / "lib" / "model.js").write_text(
                'export const mark = "changed";\n',
                encoding="utf-8",
            )
            second = mini_app_build_id(root)
            self.assertNotEqual(second, first)
            (root / "lib" / "extra.js").write_text("export {};\n", encoding="utf-8")
            self.assertNotEqual(mini_app_build_id(root), second)

            with self.assertRaisesRegex(ValueError, "static root"):
                mini_app_build_id(root / "missing")

    def test_shell_rendering_moves_every_entry_file_below_the_build(self) -> None:
        build_id = "0123456789abcdef"
        rendered = render_mini_app_shell(_SHELL_SOURCE, build_id)
        self.assertIn(f'src="./build/{build_id}/app.js"', rendered)
        self.assertIn(f'href="./build/{build_id}/styles.css"', rendered)
        self.assertNotIn('src="./app.js"', rendered)
        self.assertNotIn('href="./styles.css"', rendered)
        # Images and the Telegram SDK keep their addresses.
        self.assertIn('src="./assets/mark.png"', rendered)
        self.assertIn("https://telegram.org/js/telegram-web-app.js?63", rendered)
        with_watchdog = _SHELL_SOURCE.replace(
            '<script type="module"',
            '<script src="./boot.js"></script>\n<script type="module"',
        )
        self.assertIn(
            f'src="./build/{build_id}/boot.js"',
            render_mini_app_shell(with_watchdog, build_id),
        )

        with self.assertRaisesRegex(ValueError, "exactly once"):
            render_mini_app_shell(_SHELL_SOURCE.replace("./app.js", "./main.js"), build_id)
        with self.assertRaisesRegex(ValueError, "exactly once"):
            render_mini_app_shell(_SHELL_SOURCE + _SHELL_SOURCE, build_id)
        with self.assertRaisesRegex(ValueError, "build identifier"):
            render_mini_app_shell(_SHELL_SOURCE, "not-a-build")

    def test_menu_button_url_names_the_client_build(self) -> None:
        self.assertEqual(
            mini_app_menu_web_url("https://robot.example/getbible", "0123456789abcdef"),
            "https://robot.example/getbible/?build=0123456789abcdef",
        )
        self.assertEqual(
            mini_app_menu_web_url("https://robot.example/", "0123456789abcdef"),
            "https://robot.example/?build=0123456789abcdef",
        )
        with self.assertRaisesRegex(ValueError, "build identifier"):
            mini_app_menu_web_url("https://robot.example/getbible", "0123456789ABCDEF")


class MiniAppShellRoutingTestCase(AsyncHTTPTestCase):
    def setUp(self) -> None:
        self.static_directory = tempfile.TemporaryDirectory()
        self.server = _build_lifecycle_server(Path(self.static_directory.name))
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        self.static_directory.cleanup()

    def get_app(self) -> Application:
        return Application(list(self.server.handlers()), serve_traceback=False)

    def test_shell_names_build_specific_module_urls(self) -> None:
        build_id = self.server.build_id
        shell = self.fetch("/getbible/")
        self.assertEqual(shell.code, 200)
        self.assertEqual(shell.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(shell.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("default-src 'none'", shell.headers["Content-Security-Policy"])
        body = shell.body.decode("utf-8")
        self.assertIn(f'src="./build/{build_id}/app.js"', body)
        self.assertIn(f'href="./build/{build_id}/styles.css"', body)
        self.assertNotIn('src="./app.js"', body)

        # index.html is the same rendered shell, never the raw file.
        self.assertEqual(self.fetch("/getbible/index.html").body, shell.body)
        head = self.fetch("/getbible/", method="HEAD")
        self.assertEqual(head.code, 200)
        self.assertEqual(head.headers["Content-Length"], str(len(shell.body)))
        self.assertEqual(head.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(head.body, b"")

        redirect = self.fetch("/getbible", follow_redirects=False)
        self.assertEqual(redirect.code, 301)
        self.assertEqual(redirect.headers["Location"], "/getbible/")

        self.assertEqual(
            self.server.menu_web_url,
            f"https://robot.example/getbible/?build={build_id}",
        )

    def test_build_prefix_serves_the_complete_module_graph(self) -> None:
        build_id = self.server.build_id
        root = Path(self.static_directory.name)
        module = self.fetch(f"/getbible/build/{build_id}/app.js")
        self.assertEqual(module.code, 200)
        self.assertEqual(module.body, (root / "app.js").read_bytes())
        self.assertEqual(module.headers["Cache-Control"], "no-store, max-age=0")
        self.assertIn("javascript", module.headers["Content-Type"])
        self.assertEqual(
            self.fetch(f"/getbible/build/{build_id}/lib/model.js").body,
            (root / "lib" / "model.js").read_bytes(),
        )
        self.assertEqual(
            self.fetch(f"/getbible/build/{build_id}/styles.css").code,
            200,
        )
        self.assertEqual(
            self.fetch(f"/getbible/build/{build_id}/assets/mark.png").body,
            (root / "assets" / "mark.png").read_bytes(),
        )
        # The segment is a cache key, not a version selector: a shell cached
        # from an earlier deployment still receives the running tree instead
        # of an empty answer.
        self.assertEqual(
            self.fetch("/getbible/build/ffffffffffffffff/lib/model.js").code,
            200,
        )
        for path in (
            "/getbible/build/short/app.js",
            "/getbible/build/0123456789ABCDEF/app.js",
            f"/getbible/build/{build_id}/",
            f"/getbible/build/{build_id}/missing.js",
            "/getbible/lib/",
            "/getbible/missing.js",
        ):
            self.assertIn(self.fetch(path).code, (403, 404), path)
        # Plain module paths remain reachable for operators and probes.
        self.assertEqual(self.fetch("/getbible/app.js").code, 200)
        self.assertEqual(self.fetch("/getbible/assets/mark.png").code, 200)

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

    def test_static_shell_and_assets_are_never_cacheable(self) -> None:
        shell = self.fetch("/static/")
        self.assertEqual(shell.code, 200)
        self.assertEqual(shell.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(shell.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(shell.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'none'", shell.headers["Content-Security-Policy"])
        self.assertIn("noindex", shell.headers["X-Robots-Tag"])

        # Telegram WebViews do not reliably revalidate, so packaged modules
        # must carry the same no-store directive as the shell: an upgraded
        # server followed by a relaunch may never execute stale JavaScript.
        asset = self.fetch("/static/app.js")
        self.assertEqual(asset.code, 200)
        self.assertEqual(asset.headers["Cache-Control"], "no-store, max-age=0")


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
        return _lifecycle_settings(**changes)

    def build_server(self, root: Path) -> MiniAppServer:
        return _build_lifecycle_server(root)

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
