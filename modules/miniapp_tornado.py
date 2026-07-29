"""Tornado adapter for the framework-neutral Mini App JSON API."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from pathlib import Path
from typing import TypeAlias
from urllib.parse import urlsplit

from tornado.httpserver import HTTPServer
from tornado.web import Application as TornadoApplication
from tornado.web import RedirectHandler, RequestHandler, StaticFileHandler, URLSpec

from config import Settings

from .miniapp_api import MiniAppApi, MiniAppHttpRequest, MiniAppIngressLimiter
from .miniapp_auth import TelegramInitDataReplayGuard, TelegramInitDataValidator
from .miniapp_cleanup import MiniAppLaunchCleanup
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

_IpAddress: TypeAlias = IPv4Address | IPv6Address
CleanupSessionCallback = Callable[[MiniAppHttpRequest], Awaitable[int]]


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Read one case-insensitive header from a request mapping."""
    requested = name.casefold()
    return next(
        (value for raw_name, value in headers.items() if raw_name.casefold() == requested),
        None,
    )


class ClientAddressResolver:
    """Trust forwarded client addresses only from configured proxy networks."""

    def __init__(self, trusted_proxy_cidrs: Sequence[str]) -> None:
        self._trusted = tuple(
            ip_network(network, strict=False)
            for network in trusted_proxy_cidrs
        )

    def resolve(self, peer: str, forwarded_for: str | None) -> str:
        try:
            peer_address = ip_address(peer)
        except ValueError:
            return "unknown"
        if not self._is_trusted(peer_address) or not forwarded_for:
            return str(peer_address)
        chain: list[_IpAddress] = []
        for raw_address in forwarded_for.split(","):
            candidate = raw_address.strip()
            try:
                chain.append(ip_address(candidate))
            except ValueError:
                return str(peer_address)
        if not chain or len(chain) > 32:
            return str(peer_address)
        for address in reversed((*chain, peer_address)):
            if not self._is_trusted(address):
                return str(address)
        return str(chain[0])

    def _is_trusted(self, address: _IpAddress) -> bool:
        return any(
            address.version == network.version and address in network
            for network in self._trusted
        )


class MiniAppApiHandler(RequestHandler):
    """Forward a bounded Tornado request to :class:`MiniAppApi`."""

    def initialize(
        self,
        api: MiniAppApi,
        address_resolver: ClientAddressResolver,
        cleanup_session: CleanupSessionCallback | None = None,
    ) -> None:
        self._api = api
        self._address_resolver = address_resolver
        self._cleanup_session = cleanup_session

    async def get(self, path: str = "") -> None:
        await self._dispatch(path)

    async def post(self, path: str = "") -> None:
        await self._dispatch(path)

    async def delete(self, path: str = "") -> None:
        await self._dispatch(path)

    async def options(self, path: str = "") -> None:
        await self._dispatch(path)

    async def put(self, path: str = "") -> None:
        await self._dispatch(path)

    async def patch(self, path: str = "") -> None:
        await self._dispatch(path)

    async def _dispatch(self, path: str) -> None:
        peer = (
            self.request.remote_ip
            if isinstance(self.request.remote_ip, str)
            else "unknown"
        )
        client_ip = self._address_resolver.resolve(
            peer,
            self.request.headers.get("X-Forwarded-For"),
        )
        request = MiniAppHttpRequest(
            method=self.request.method or "",
            target=self.request.uri or "",
            headers=dict(self.request.headers),
            body=self.request.body,
            client_key=client_ip,
        )
        if path == "cleanup":
            await self._dispatch_cleanup(request)
            return

        response = await self._api.handle(request)
        self.set_status(response.status)
        for name, value in response.headers.items():
            self.set_header(name, value)
        if response.body:
            self.finish(response.body)
        else:
            self.finish()

    async def _dispatch_cleanup(self, request: MiniAppHttpRequest) -> None:
        self.set_header("Cache-Control", "no-store, max-age=0")
        self.set_header("X-Content-Type-Options", "nosniff")
        method = request.method.upper()
        if method == "OPTIONS":
            self.set_header("Allow", "POST, OPTIONS")
            self.set_status(204)
            self.finish()
            return
        if method != "POST":
            self.set_header("Allow", "POST, OPTIONS")
            self.set_status(405)
            self.finish()
            return
        if self._cleanup_session is None:
            self.set_status(404)
            self.finish()
            return
        self.set_status(await self._cleanup_session(request))
        self.finish()


def miniapp_api_handlers(
    api: MiniAppApi,
    *,
    public_path: str = "",
    address_resolver: ClientAddressResolver | None = None,
    cleanup_session: CleanupSessionCallback | None = None,
) -> Sequence[URLSpec]:
    """Return route specs for a dedicated, size-limited Tornado HTTP server."""
    prefix = re.escape(public_path.rstrip("/"))
    return (
        URLSpec(
            rf"{prefix}/api/v1/(.*)",
            MiniAppApiHandler,
            {
                "api": api,
                "address_resolver": (
                    address_resolver
                    or ClientAddressResolver(("127.0.0.1/32", "::1/128"))
                ),
                "cleanup_session": cleanup_session,
            },
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
            # Telegram's embedded browser may keep the WebView cache between
            # launches. Revalidate every packaged asset so an atomic instance
            # upgrade cannot combine a new index with stale JavaScript, locale
            # catalogs, styles, or branding. Tornado's content ETag still lets
            # unchanged files return 304 without downloading the body again.
            self.set_header(
                "Cache-Control",
                "no-cache, max-age=0, must-revalidate",
            )


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
        cleanup_launch: Callable[[MiniAppLaunch], Awaitable[None]] | None = None,
        abuse_warning: Callable[[int, int, str], Awaitable[None]] | None = None,
        static_root: Path | None = None,
    ) -> None:
        if not settings.mini_app_enabled or settings.mini_app_public_url is None:
            raise ValueError("Mini App settings are not enabled.")
        self._settings = settings
        self._public_url = miniapp_public_web_url(settings.mini_app_public_url)
        public_parts = urlsplit(self._public_url)
        self._public_path = public_parts.path.rstrip("/")
        self._public_origin = f"{public_parts.scheme}://{public_parts.netloc}"
        self._address_resolver = ClientAddressResolver(
            settings.mini_app_trusted_proxy_cidrs
        )
        self._cleanup = MiniAppLaunchCleanup(
            cleanup_launch,
            ttl_seconds=settings.mini_app_launch_ttl_seconds,
            max_pending=settings.mini_app_session_limit,
        )
        self.launches = MiniAppLaunchStore(
            max_launches=settings.mini_app_session_limit,
            ttl_seconds=settings.mini_app_launch_ttl_seconds,
        )
        self.sessions = MiniAppSessionStore(
            max_sessions=settings.mini_app_session_limit,
            ttl_seconds=settings.mini_app_session_ttl_seconds,
            max_sessions_per_user=settings.mini_app_sessions_per_user,
            max_searches_per_session=settings.mini_app_max_searches_per_session,
            max_available_selections=settings.mini_app_max_available_selections,
            max_basket_selections=settings.mini_app_max_selections,
        )
        validator = TelegramInitDataValidator(
            settings.telegram_api_token,
            max_age_seconds=settings.mini_app_init_data_max_age_seconds,
        )
        self._validator = validator

        async def post_with_cleanup(
            launch: MiniAppLaunch,
            queries: tuple[ScriptureQuery, ...],
        ) -> Sequence[int] | None:
            return await self._cleanup.post(launch, queries, post_scripture)

        self.api = MiniAppApi(
            service=service,
            preferences=preferences,
            limiter=limiter,
            sessions=self.sessions,
            launches=self.launches,
            validator=validator,
            public_url=self._public_url,
            post_scripture=post_with_cleanup,
            cleanup_launch=self._cleanup.cleanup_now,
            ingress_limiter=MiniAppIngressLimiter(
                capacity=settings.mini_app_session_exchange_rate_capacity,
                refill_per_second=(
                    settings.mini_app_session_exchange_rate_refill_per_second
                ),
                max_entries=settings.rate_limit_cache_size,
            ),
            replay_guard=TelegramInitDataReplayGuard(
                ttl_seconds=settings.mini_app_init_data_max_age_seconds,
                max_entries=max(100, settings.mini_app_session_limit * 2),
            ),
            audit_settings=settings,
            abuse_warning=abuse_warning,
            navigation_rate_cost=settings.mini_app_navigation_rate_cost,
            access_log=settings.mini_app_access_log,
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
            miniapp_api_handlers(
                self.api,
                public_path=self._public_path,
                address_resolver=self._address_resolver,
                cleanup_session=self._cleanup_session_request,
            )
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
            xheaders=False,
            max_buffer_size=128 * 1024,
            max_body_size=64 * 1024,
            max_header_size=self._settings.mini_app_max_header_bytes,
            idle_connection_timeout=self._settings.mini_app_idle_timeout_seconds,
            body_timeout=self._settings.mini_app_body_timeout_seconds,
        )
        server.listen(
            self._settings.mini_app_port,
            address=self._settings.mini_app_listen,
        )
        self._server = server

    async def close(self) -> None:
        """Stop requests and make one final attempt for every pending launch row."""
        server = self._server
        self._server = None
        if server is not None:
            server.stop()
            await server.close_all_connections()
        await self._cleanup.close()

    def snapshot(self) -> dict[str, int | float]:
        """Return bounded session and API traffic counters for health metrics."""
        snapshot = dict(self.sessions.snapshot())
        snapshot.update(self.api.snapshot())
        snapshot.update(self._cleanup.snapshot())
        return snapshot

    def create_launch(
        self,
        *,
        user_id: int,
        target_chat_id: int,
        message_thread_id: int | None = None,
        initial_route: MiniAppRoute = "home",
        initial_query: str = "",
        source_ephemeral_message_id: int | None = None,
        source_ephemeral_receiver_user_id: int | None = None,
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
            source_ephemeral_message_id=source_ephemeral_message_id,
            source_ephemeral_receiver_user_id=(
                source_ephemeral_receiver_user_id
            ),
        )

    def web_url(self, launch: MiniAppLaunch) -> str:
        """Return the inline/menu Web App URL for one launch."""
        return miniapp_web_url(self._public_url, launch.token)

    def remember_prompt(
        self,
        launch: MiniAppLaunch,
        *,
        message_id: int | None = None,
        ephemeral_message_id: int | None = None,
    ) -> MiniAppLaunch:
        """Retain the source command and launch response as one cleanup unit."""
        remembered = self.launches.remember_prompt(
            launch,
            message_id=message_id,
            ephemeral_message_id=ephemeral_message_id,
        )
        self._cleanup.remember_prompt(remembered)
        return remembered

    async def _cleanup_session_request(self, request: MiniAppHttpRequest) -> int:
        """Authenticate one browser-ready signal without blocking cleanup on rate limits."""
        if _header_value(request.headers, "origin") != self._public_origin:
            return 403
        authorization = _header_value(request.headers, "authorization") or ""
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{16,128})\Z", authorization)
        if match is None:
            return 401
        session = self.sessions.get(match.group(1), touch=False)
        if session is None:
            return 401
        raw_init_data = _header_value(request.headers, "x-telegram-init-data")
        if raw_init_data is None:
            return 401
        try:
            principal = self._validator.validate(
                raw_init_data,
                check_freshness=False,
            )
        except Exception:
            return 401
        digest = hashlib.sha256(raw_init_data.encode("utf-8")).digest()
        if (
            not hmac.compare_digest(digest, session.init_data_digest)
            or principal.user_id != session.user_id
            or (
                session.query_id is not None
                and principal.query_id != session.query_id
            )
            or not self.sessions.touch(session)
        ):
            return 401
        await self._cleanup.cleanup_now(session.launch)
        return 204

    @property
    def public_web_url(self) -> str:
        """Return the trailing-slash URL used for Telegram's generic menu button."""
        return self._public_url

    @staticmethod
    def direct_url(bot_username: str, launch: MiniAppLaunch) -> str:
        """Return the configured Main Mini App direct link for one launch."""
        return miniapp_direct_url(bot_username, launch.token)
