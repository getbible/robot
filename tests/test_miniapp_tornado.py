import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application, URLSpec

from modules.miniapp_api import MiniAppHttpRequest, MiniAppHttpResponse
from modules.miniapp_tornado import (
    MAX_MINI_APP_REQUEST_BYTES,
    ClientAddressResolver,
    MiniAppServer,
    MiniAppStaticHandler,
    miniapp_api_handlers,
)


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

    def test_streaming_body_limit_is_large_only_for_bookmark_backup(self) -> None:
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

        # Tornado rejects an over-limit Content-Length at the HTTP transport
        # boundary with 400; a chunked overrun raised by the handler is 413.
        self.assertIn(oversized_ordinary.code, (400, 413))
        self.assertIn(oversized_lookalike.code, (400, 413))
        self.assertIn(oversized_wrong_method.code, (400, 413))
        self.assertEqual(allowed_backup.code, 202)
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
            "X-Telegram-Init-Data": init_data,
        }
        server.sessions.get.return_value = None
        self.assertEqual(await server._cleanup_session_request(request(headers)), 401)

        server.sessions.get.return_value = session
        server._validator.validate.side_effect = ValueError("invalid")
        self.assertEqual(await server._cleanup_session_request(request(headers)), 401)

        server._validator.validate.side_effect = None
        server._validator.validate.return_value = principal
        self.assertEqual(await server._cleanup_session_request(request(headers)), 204)
        server._validator.validate.assert_called_with(
            init_data,
            check_freshness=False,
        )
        server._cleanup.cleanup_now.assert_awaited_once_with(launch)


if __name__ == "__main__":
    unittest.main()
