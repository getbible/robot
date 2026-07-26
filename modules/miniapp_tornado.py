"""Tornado adapter for the framework-neutral Mini App JSON API."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from tornado.httpserver import HTTPServer
from tornado.web import Application as TornadoApplication
from tornado.web import RedirectHandler, RequestHandler, StaticFileHandler, URLSpec

from config import Settings

from .miniapp_api import MiniAppApi, MiniAppHttpRequest
from .miniapp_auth import TelegramInitDataReplayGuard, TelegramInitDataValidator
from .miniapp_sessions import (
    MiniAppLaunch,
    MiniAppLaunchStore,
    MiniAppRoute,
    MiniAppSessionStore,
    miniapp_direct_url,
    miniapp_public_web_url,
    miniapp_web_url,
)
from .preferences import UserPreferenceStore
from .rate_limit import InboundRateLimiter
from .service import ScriptureQuery, ScriptureService


class MiniAppApiHandler(RequestHandler):
    """Forward a bounded Tornado request to :class:`MiniAppApi`."""

    def initialize(self, api: MiniAppApi) -> None:
        self._api = api

    async def get(self, path: str = "") -> None:
        await self._dispatch()

    async def post(self, path: str = "") -> None:
        await self._dispatch()

    async def delete(self, path: str = "") -> None:
        await self._dispatch()

    async def options(self, path: str = "") -> None:
        await self._dispatch()

    async def put(self, path: str = "") -> None:
        await self._dispatch()

    async def patch(self, path: str = "") -> None:
        await self._dispatch()

    async def _dispatch(self) -> None:
        response = await self._api.handle(
            MiniAppHttpRequest(
                method=self.request.method or "",
                target=self.request.uri or "",
                headers=dict(self.request.headers),
                body=self.request.body,
                client_key=(
                    self.request.remote_ip if isinstance(self.request.remote_ip, str) else "unknown"
                ),
            )
        )
        self.set_status(response.status)
        for name, value in response.headers.items():
            self.set_header(name, value)
        if response.body:
            self.finish(response.body)
        else:
            self.finish()


def miniapp_api_handlers(api: MiniAppApi, *, public_path: str = "") -> Sequence[URLSpec]:
    """Return route specs for a dedicated, size-limited Tornado HTTP server."""
    prefix = re.escape(public_path.rstrip("/"))
    return (
        URLSpec(
            rf"{prefix}/api/v1/(.*)",
            MiniAppApiHandler,
            {"api": api},
        ),
    )


class MiniAppStaticHandler(StaticFileHandler):
    """Serve only packaged Mini App files with browser-hardening headers."""

    def set_extra_headers(self, path: str) -> None:
        self.set_header("X-Content-Type-Options", "nosniff")
        self.set_header("Referrer-Policy", "no-referrer")
        self.set_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        self.set_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self' https://telegram.org; "
            "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "font-src 'self'; base-uri 'none'; form-action 'self'; "
            "object-src 'none'; frame-ancestors https://web.telegram.org "
            "https://*.telegram.org; upgrade-insecure-requests",
        )
        self.set_header(
            "Strict-Transport-Security",
            "max-age=63072000; includeSubDomains",
        )
        self.set_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        if path in {"", "index.html"}:
            self.set_header("Cache-Control", "no-store, max-age=0")
        else:
            self.set_header("Cache-Control", "public, max-age=3600")


class MiniAppServer:
    """Own the loopback Mini App HTTP lifecycle and command launch handoffs."""

    def __init__(
        self,
        *,
        settings: Settings,
        service: ScriptureService,
        preferences: UserPreferenceStore,
        limiter: InboundRateLimiter,
        post_scripture: Callable[
            [MiniAppLaunch, tuple[ScriptureQuery, ...]],
            Awaitable[Sequence[int] | None],
        ],
        static_root: Path | None = None,
    ) -> None:
        if not settings.mini_app_enabled or settings.mini_app_public_url is None:
            raise ValueError("Mini App settings are not enabled.")
        self._settings = settings
        self._public_url = miniapp_public_web_url(settings.mini_app_public_url)
        self._public_path = urlsplit(self._public_url).path.rstrip("/")
        self.launches = MiniAppLaunchStore(
            max_launches=settings.mini_app_session_limit,
            ttl_seconds=settings.mini_app_launch_ttl_seconds,
        )
        self.sessions = MiniAppSessionStore(
            max_sessions=settings.mini_app_session_limit,
            ttl_seconds=settings.mini_app_session_ttl_seconds,
            max_basket_selections=settings.mini_app_max_selections,
        )
        validator = TelegramInitDataValidator(
            settings.telegram_api_token,
            max_age_seconds=settings.mini_app_init_data_max_age_seconds,
        )
        self.api = MiniAppApi(
            service=service,
            preferences=preferences,
            limiter=limiter,
            sessions=self.sessions,
            launches=self.launches,
            validator=validator,
            public_url=self._public_url,
            post_scripture=post_scripture,
            replay_guard=TelegramInitDataReplayGuard(
                ttl_seconds=settings.mini_app_init_data_max_age_seconds,
                max_entries=max(100, settings.mini_app_session_limit * 2),
            ),
        )
        root = static_root or Path(__file__).resolve().parent.parent / "miniapp"
        if not root.is_dir():
            raise ValueError("Mini App static root is unavailable.")
        self._static_root = root.resolve()
        self._server: HTTPServer | None = None

    async def start(self) -> None:
        """Bind the private HTTP listener; repeated calls are safe."""
        if self._server is not None:
            return
        prefix = re.escape(self._public_path)
        handlers: list[URLSpec] = list(
            miniapp_api_handlers(self.api, public_path=self._public_path)
        )
        if self._public_path:
            handlers.append(
                URLSpec(
                    rf"{prefix}",
                    RedirectHandler,
                    {"url": f"{self._public_path}/", "permanent": True},
                )
            )
        handlers.append(
            URLSpec(
                rf"{prefix}/(.*)" if self._public_path else r"/(.*)",
                MiniAppStaticHandler,
                {"path": str(self._static_root), "default_filename": "index.html"},
            )
        )
        application = TornadoApplication(
            handlers,
            compress_response=True,
            serve_traceback=False,
            static_hash_cache=True,
        )
        server = HTTPServer(
            application,
            xheaders=True,
            trusted_downstream=["127.0.0.1", "::1"],
            max_buffer_size=1024 * 1024,
            max_body_size=64 * 1024,
        )
        server.listen(
            self._settings.mini_app_port,
            address=self._settings.mini_app_listen,
        )
        self._server = server

    async def close(self) -> None:
        """Stop accepting requests and drain current connections."""
        server = self._server
        self._server = None
        if server is None:
            return
        server.stop()
        await server.close_all_connections()

    def create_launch(
        self,
        *,
        user_id: int,
        target_chat_id: int,
        message_thread_id: int | None = None,
        initial_route: MiniAppRoute = "home",
        initial_query: str = "",
    ) -> MiniAppLaunch:
        """Create one short-lived command handoff without putting content in a URL."""
        if initial_route not in {"home", "bible", "search"}:
            raise ValueError("initial_route is invalid.")
        return self.launches.create_launch(
            user_id=user_id,
            target_chat_id=target_chat_id,
            message_thread_id=message_thread_id,
            initial_route=initial_route,
            initial_query=initial_query,
        )

    def web_url(self, launch: MiniAppLaunch) -> str:
        """Return the inline/menu Web App URL for one launch."""
        return miniapp_web_url(self._public_url, launch.token)

    @property
    def public_web_url(self) -> str:
        """Return the trailing-slash URL used for Telegram's generic menu button."""
        return self._public_url

    @staticmethod
    def direct_url(bot_username: str, launch: MiniAppLaunch) -> str:
        """Return the configured Main Mini App direct link for one launch."""
        return miniapp_direct_url(bot_username, launch.token)
