"""Framework-neutral, authenticated JSON API for the GetBible Telegram Mini App."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from getbible import RequestLimitError, TranslationNotFoundError

from config import Settings

from .audit import audit_event, audit_identity
from .catalog import BookOption, ChapterOption, TranslationOption
from .errors import (
    CircuitOpen,
    RobotBusy,
    RobotError,
    RobotInputError,
    RobotRateLimited,
    ScriptureUnavailable,
)
from .interactions import SearchOptions
from .miniapp_auth import (
    MiniAppAuthenticationError,
    MiniAppReplayError,
    TelegramInitDataReplayGuard,
    TelegramInitDataValidator,
)
from .miniapp_sessions import (
    MiniAppLaunch,
    MiniAppLaunchStore,
    MiniAppPostAttempt,
    MiniAppSearch,
    MiniAppSelection,
    MiniAppSession,
    MiniAppSessionStore,
)
from .preferences import ReaderLocation, SearchDefaults, UserPreferenceStore
from .rate_limit import InboundRateLimiter
from .service import ScriptureQuery, ScriptureService

LOGGER = logging.getLogger(__name__)
_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")
_BEARER_RE = re.compile(r"Bearer ([A-Za-z0-9_-]{16,128})\Z")
MAX_MINIAPP_CHAPTER_VERSES = 250
_PREFERENCE_UNCHANGED = object()
_SEARCH_ENUMS: dict[str, frozenset[str]] = {
    "words": frozenset({"all", "any", "phrase"}),
    "match": frozenset({"whole_word", "substring"}),
    "scope": frozenset({"bible", "old_testament", "new_testament", "deuterocanon"}),
    "diacritics": frozenset({"sensitive", "insensitive"}),
    "sort": frozenset({"canonical", "relevance"}),
}


class MiniAppApiInputError(ValueError):
    """The Mini App submitted malformed or unsupported API input."""


@dataclass(frozen=True, slots=True)
class MiniAppHttpRequest:
    """Minimal HTTP request contract used by adapters and deterministic tests."""

    method: str
    target: str
    headers: Mapping[str, str]
    body: bytes = b""
    client_key: str = "unknown"


@dataclass(frozen=True, slots=True)
class MiniAppHttpResponse:
    """Complete JSON response returned to a concrete HTTP adapter."""

    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(slots=True)
class _IngressBucket:
    tokens: float
    updated_at: float


class MiniAppIngressLimiter:
    """Bound unauthenticated session-exchange traffic by trusted remote address."""

    def __init__(
        self,
        *,
        capacity: int = 10,
        refill_per_second: float = 0.2,
        max_entries: int = 20_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0 or max_entries < 100:
            raise ValueError("Invalid Mini App ingress limiter configuration.")
        self._capacity = float(capacity)
        self._refill = refill_per_second
        self._max_entries = max_entries
        self._clock = clock
        self._buckets: OrderedDict[str, _IngressBucket] = OrderedDict()
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> None:
        if not isinstance(key, str) or not key or len(key) > 256:
            key = "unknown"
        now = self._clock()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _IngressBucket(self._capacity, now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(
                    self._capacity,
                    bucket.tokens + elapsed * self._refill,
                )
                bucket.updated_at = now
            self._buckets.move_to_end(key)
            while len(self._buckets) > self._max_entries:
                self._buckets.popitem(last=False)
            if bucket.tokens < 1.0:
                retry_after = (1.0 - bucket.tokens) / self._refill
                raise RobotRateLimited(
                    retry_after,
                    scopes=("session_exchange",),
                    client_key=key,
                )
            bucket.tokens -= 1.0


class MiniAppApi:
    """Route secure Mini App requests to existing bounded GetBible services."""

    def __init__(
        self,
        *,
        service: ScriptureService,
        preferences: UserPreferenceStore,
        limiter: InboundRateLimiter,
        sessions: MiniAppSessionStore,
        launches: MiniAppLaunchStore,
        validator: TelegramInitDataValidator,
        public_url: str,
        post_scripture: Callable[
            [MiniAppLaunch, tuple[ScriptureQuery, ...]],
            Awaitable[Sequence[int] | None],
        ],
        cleanup_launch: Callable[[MiniAppLaunch], Awaitable[None]] | None = None,
        ingress_limiter: MiniAppIngressLimiter | None = None,
        replay_guard: TelegramInitDataReplayGuard | None = None,
        audit_settings: Settings | None = None,
        abuse_warning: Callable[[int, int, str], Awaitable[None]] | None = None,
        navigation_rate_cost: float = 0.25,
        access_log: bool = True,
        page_size: int = 10,
        max_body_bytes: int = 64 * 1024,
    ) -> None:
        origin = urlsplit(public_url)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
        ):
            raise ValueError("public_url must be an HTTPS URL.")
        if not 1 <= page_size <= 25:
            raise ValueError("page_size must be between 1 and 25.")
        if not 1024 <= max_body_bytes <= 1024 * 1024:
            raise ValueError("max_body_bytes must be between 1024 and 1048576.")
        if not 0.05 <= navigation_rate_cost <= 1.0:
            raise ValueError("navigation_rate_cost must be between 0.05 and 1.")
        self._service = service
        self._preferences = preferences
        self._limiter = limiter
        self._sessions = sessions
        self._launches = launches
        self._validator = validator
        self._post_scripture = post_scripture
        self._cleanup_launch = cleanup_launch
        self._replay_guard = replay_guard or TelegramInitDataReplayGuard(
            ttl_seconds=300,
            max_entries=20_000,
        )
        self._origin = f"{origin.scheme}://{origin.netloc}"
        public_path = origin.path.rstrip("/")
        self._api_prefix = f"{public_path}/api/v1" if public_path else "/api/v1"
        self._search_path_re = re.compile(
            rf"{re.escape(self._api_prefix)}/search/([A-Za-z0-9_-]{{16,128}})\Z"
        )
        self._basket_item_path_re = re.compile(
            rf"{re.escape(self._api_prefix)}/basket/items/"
            r"([A-Za-z0-9_-]{16,128})\Z"
        )
        self._ingress = ingress_limiter or MiniAppIngressLimiter()
        self._audit_settings = audit_settings
        self._abuse_warning = abuse_warning
        self._navigation_rate_cost = navigation_rate_cost
        self._access_log = access_log
        self._traffic: Counter[str] = Counter()
        self._page_size = page_size
        self._max_body = max_body_bytes

    async def handle(self, request: MiniAppHttpRequest) -> MiniAppHttpResponse:
        """Handle and account for one request without logging secrets or content."""
        started_at = time.monotonic()
        session = self._session_from_request(request)
        response = await self._handle(request)
        self._record_access(
            request,
            response,
            session=session,
            duration_seconds=max(0.0, time.monotonic() - started_at),
        )
        return response

    async def _handle(self, request: MiniAppHttpRequest) -> MiniAppHttpResponse:
        """Handle one request and convert all expected failures to safe JSON."""
        method = request.method.upper()
        parts = urlsplit(request.target)
        if (
            parts.scheme
            or parts.netloc
            or parts.fragment
            or not parts.path.startswith(f"{self._api_prefix}/")
        ):
            return self._error_response(404, "not_found", "Resource not found.")

        origin = _header(request.headers, "origin")
        if origin != self._origin and not (method in {"GET", "HEAD"} and origin is None):
            return self._error_response(403, "forbidden", "Request origin is not allowed.")
        if method == "OPTIONS":
            return self._response(
                204,
                None,
                extra_headers={
                    "Access-Control-Allow-Methods": ("GET, POST, PUT, PATCH, DELETE, OPTIONS"),
                    "Access-Control-Allow-Headers": (
                        "Authorization, Content-Type, X-Telegram-Init-Data"
                    ),
                    "Access-Control-Max-Age": "600",
                },
            )
        if len(request.body) > self._max_body:
            return self._error_response(
                413,
                "request_too_large",
                "Request body is too large.",
            )

        try:
            if parts.path == f"{self._api_prefix}/session":
                if method == "POST":
                    return await self._exchange_session(request)
                if method == "GET":
                    session = await self._authenticated(request)
                    return await self._session_bootstrap(session)
                if method == "DELETE":
                    session = await self._authenticated(request)
                    self._sessions.revoke(session.token)
                    return self._response(204, None)
                return self._method_not_allowed("GET, POST, DELETE, OPTIONS")

            session = await self._authenticated(request)
            if parts.path == f"{self._api_prefix}/translations":
                if method != "GET":
                    return self._method_not_allowed("GET, OPTIONS")
                return await self._translations(session)
            if parts.path == f"{self._api_prefix}/books":
                if method != "GET":
                    return self._method_not_allowed("GET, OPTIONS")
                return await self._books(session, parts.query)
            if parts.path == f"{self._api_prefix}/chapters":
                if method != "GET":
                    return self._method_not_allowed("GET, OPTIONS")
                return await self._chapters(session, parts.query)
            if parts.path == f"{self._api_prefix}/scripture":
                if method != "POST":
                    return self._method_not_allowed("POST, OPTIONS")
                return await self._scripture(session, request)
            if parts.path == f"{self._api_prefix}/search":
                if method != "POST":
                    return self._method_not_allowed("POST, OPTIONS")
                return await self._search(session, request)
            search_match = self._search_path_re.fullmatch(parts.path)
            if search_match is not None:
                if method != "GET":
                    return self._method_not_allowed("GET, OPTIONS")
                return self._search_page(
                    session,
                    search_match.group(1),
                    parts.query,
                )
            if parts.path == f"{self._api_prefix}/basket":
                if method == "GET":
                    return self._basket(session)
                if method == "DELETE":
                    self._sessions.clear_basket(session)
                    return self._response(204, None)
                return self._method_not_allowed("GET, DELETE, OPTIONS")
            if parts.path == f"{self._api_prefix}/basket/items":
                if method != "POST":
                    return self._method_not_allowed("POST, OPTIONS")
                return self._add_basket_item(session, request)
            basket_item_match = self._basket_item_path_re.fullmatch(parts.path)
            if basket_item_match is not None:
                if method != "DELETE":
                    return self._method_not_allowed("DELETE, OPTIONS")
                return self._remove_basket_item(
                    session,
                    basket_item_match.group(1),
                )
            if parts.path == f"{self._api_prefix}/basket/order":
                if method != "PATCH":
                    return self._method_not_allowed("PATCH, OPTIONS")
                return self._reorder_basket(session, request)
            if parts.path == f"{self._api_prefix}/preferences":
                if method != "PUT":
                    return self._method_not_allowed("PUT, OPTIONS")
                return await self._update_preferences(session, request)
            if parts.path == f"{self._api_prefix}/post":
                if method != "POST":
                    return self._method_not_allowed("POST, OPTIONS")
                return await self._post(session, request)
            return self._error_response(404, "not_found", "Resource not found.")
        except MiniAppReplayError:
            return self._error_response(
                409,
                "authorization_replayed",
                "This Telegram launch was already exchanged.",
            )
        except MiniAppAuthenticationError:
            return self._error_response(
                401,
                "unauthorized",
                "Telegram authorization is invalid or expired.",
            )
        except MiniAppApiInputError as error:
            return self._error_response(400, "invalid_request", str(error))
        except TranslationNotFoundError:
            return self._error_response(
                400,
                "invalid_request",
                "translation is not available.",
            )
        except (OverflowError, ValueError) as error:
            return self._error_response(400, "invalid_request", str(error))
        except RobotRateLimited as error:
            await self._handle_rate_limit(error)
            return self._error_response(
                429,
                "rate_limited",
                (
                    "Repeated requests have been paused. Please stop repeated or "
                    "automated requests and try again later."
                    if error.blocked
                    else "Too many requests. Please try again shortly."
                ),
                details={"retry_after": error.retry_after},
                extra_headers={"Retry-After": str(error.retry_after)},
            )
        except (RobotInputError, RequestLimitError) as error:
            return self._error_response(422, "invalid_selection", str(error))
        except (RobotBusy, CircuitOpen, ScriptureUnavailable):
            return self._error_response(
                503,
                "scripture_temporarily_unavailable",
                "Scripture is temporarily unavailable.",
            )
        except RobotError:
            return self._error_response(
                422,
                "scripture_request_failed",
                "The Scripture request could not be completed.",
            )
        except Exception as error:
            LOGGER.error(
                "Unexpected Mini App API failure (%s)",
                type(error).__name__,
                exc_info=True,
            )
            return self._error_response(
                500,
                "internal_error",
                "The request could not be completed.",
            )

    async def _exchange_session(
        self,
        request: MiniAppHttpRequest,
    ) -> MiniAppHttpResponse:
        await self._ingress.acquire(request.client_key)
        payload = self._json_body(request)
        if set(payload) - {"init_data", "launch_token"}:
            raise MiniAppApiInputError("Session request contains unsupported fields.")
        init_data = _required_text(payload, "init_data", 8 * 1024)
        principal = self._validator.validate(init_data)
        supplied_launch = payload.get("launch_token")
        if supplied_launch is not None and not isinstance(supplied_launch, str):
            raise MiniAppApiInputError("launch_token must be text.")
        if (
            supplied_launch is not None
            and principal.start_param is not None
            and supplied_launch != principal.start_param
        ):
            raise MiniAppAuthenticationError("Invalid Mini App launch.")
        launch_token = supplied_launch or principal.start_param

        if self._replay_guard.contains(init_data):
            raise MiniAppReplayError("Telegram authorization was replayed.")
        recovery_session: MiniAppSession | None = None
        if launch_token is not None:
            expired_launch = self._launches.take_expired(
                launch_token,
                user_id=principal.user_id,
            )
            if expired_launch is None:
                expired_launch = self._sessions.take_expired_launch(
                    launch_token,
                    user_id=principal.user_id,
                )
            if expired_launch is not None:
                await self._cleanup_expired_launch(expired_launch)
                raise MiniAppAuthenticationError("Invalid Mini App launch.")
            pending_launch = self._launches.peek(
                launch_token,
                user_id=principal.user_id,
            )
            if pending_launch is None:
                recovery_session = self._sessions.find_by_launch(
                    launch_token,
                    user_id=principal.user_id,
                )
                if recovery_session is None:
                    raise MiniAppAuthenticationError("Invalid Mini App launch.")

        await self._limiter.acquire(
            user_id=principal.user_id,
            chat_id=principal.rate_limit_chat_id,
            client_key=request.client_key,
        )
        translation = self._preferences.translation_for(principal.user_id)
        translations = await self._service.translations()
        init_data_digest = hashlib.sha256(init_data.encode("utf-8")).digest()

        if recovery_session is not None:
            self._replay_guard.claim(init_data)
            try:
                if not self._sessions.rebind(
                    recovery_session,
                    principal,
                    init_data_digest=init_data_digest,
                ):
                    raise MiniAppAuthenticationError("Invalid Mini App launch.")
                return await self._session_bootstrap(
                    recovery_session,
                    status=200,
                    translations=translations,
                )
            except BaseException:
                self._replay_guard.release(init_data)
                raise

        consumed_launch: MiniAppLaunch | None = None
        if launch_token is None:
            launch = MiniAppLaunch(
                token="generic-private",
                user_id=principal.user_id,
                target_chat_id=principal.user_id,
                message_thread_id=None,
                initial_route="home",
                initial_query="",
                created_at=time.monotonic(),
            )
        else:
            if self._replay_guard.contains(init_data):
                raise MiniAppReplayError("Telegram authorization was replayed.")
            self._replay_guard.claim(init_data)
            consumed_launch = self._launches.consume(
                launch_token,
                user_id=principal.user_id,
            )
            if consumed_launch is None:
                self._replay_guard.release(init_data)
                raise MiniAppAuthenticationError("Invalid Mini App launch.")
            launch = consumed_launch
        if launch_token is None:
            self._replay_guard.claim(init_data)
        session: MiniAppSession | None = None
        try:
            session = self._sessions.create(
                principal,
                translation=translation,
                launch=launch,
                init_data_digest=init_data_digest,
            )
            return await self._session_bootstrap(
                session,
                status=201,
                translations=translations,
            )
        except BaseException:
            if session is not None:
                self._sessions.revoke(session.token)
            self._replay_guard.release(init_data)
            if consumed_launch is not None:
                self._launches.restore(consumed_launch)
            raise

    async def _cleanup_expired_launch(self, launch: MiniAppLaunch) -> None:
        """Remove stale Telegram launch rows without affecting auth failure."""
        if self._cleanup_launch is None:
            return
        try:
            await self._cleanup_launch(launch)
        except Exception:
            LOGGER.warning(
                "Expired Mini App launch messages could not be cleaned up",
                exc_info=True,
            )

    async def _session_bootstrap(
        self,
        session: MiniAppSession,
        *,
        status: int = 200,
        translations: Sequence[TranslationOption] | None = None,
    ) -> MiniAppHttpResponse:
        options = (
            tuple(translations)
            if translations is not None
            else await self._service.translations()
        )
        preferences = self._preferences.preferences_for(session.user_id)
        available = {option.code for option in options}
        if preferences.translation not in available and options:
            preferences = self._preferences.update_preferences(
                session.user_id,
                translation=options[0].code,
            )
        self._sessions.set_user_translation(
            session.user_id,
            preferences.translation,
        )
        return self._response(
            status,
            {
                "session_token": session.token,
                "expires_in": int(self._sessions.snapshot()["ttl_seconds"]),
                "user": {"id": session.user_id},
                "preferences": preferences.as_dict(),
                "entrypoint": {
                    "route": session.launch.initial_route,
                    "query": session.launch.initial_query,
                },
                "translations": [_translation_payload(option) for option in options],
                "basket": self._basket_payload(session),
            },
        )

    async def _authenticated(self, request: MiniAppHttpRequest) -> MiniAppSession:
        authorization = _header(request.headers, "authorization") or ""
        match = _BEARER_RE.fullmatch(authorization)
        if match is None:
            raise MiniAppAuthenticationError("Invalid Mini App session.")
        session = self._sessions.get(match.group(1), touch=False)
        if session is None:
            raise MiniAppAuthenticationError("Invalid Mini App session.")
        raw_init_data = _header(request.headers, "x-telegram-init-data")
        if raw_init_data is None:
            raise MiniAppAuthenticationError("Invalid Telegram authorization.")
        principal = self._validator.validate(
            raw_init_data,
            check_freshness=False,
        )
        digest = hashlib.sha256(raw_init_data.encode("utf-8")).digest()
        if (
            not hmac.compare_digest(digest, session.init_data_digest)
            or principal.user_id != session.user_id
            or (session.query_id is not None and principal.query_id != session.query_id)
        ):
            raise MiniAppAuthenticationError("Invalid Telegram authorization.")
        await self._limiter.acquire(
            user_id=session.user_id,
            chat_id=session.chat_id,
            cost=self._rate_cost(request),
            client_key=request.client_key,
        )
        if not self._sessions.touch(session):
            raise MiniAppAuthenticationError("Invalid Mini App session.")
        return session

    def snapshot(self) -> dict[str, int]:
        """Return aggregate API outcomes without retaining request identities."""
        return {
            "api_requests": self._traffic["requests"],
            "api_successes": self._traffic["successes"],
            "api_client_errors": self._traffic["client_errors"],
            "api_server_errors": self._traffic["server_errors"],
            "api_rate_limited": self._traffic["rate_limited"],
            "api_abuse_blocks": self._traffic["abuse_blocks"],
        }

    async def _handle_rate_limit(self, error: RobotRateLimited) -> None:
        self._traffic["rate_limited"] += 1
        if error.new_block:
            self._traffic["abuse_blocks"] += 1
        settings = self._audit_settings
        if settings is not None:
            audit_event(
                LOGGER,
                settings,
                "inbound_rate_limited",
                metadata={
                    "source": "mini_app",
                    "retry_after_seconds": error.retry_after,
                    "temporarily_blocked": error.blocked,
                    "new_block": error.new_block,
                    "violation_count": error.violation_count,
                    "limited_scopes": ",".join(error.scopes),
                },
                identity=audit_identity(
                    settings,
                    user_id=error.user_id,
                    chat_id=error.chat_id,
                    client_ip=error.client_key,
                ),
                level=logging.WARNING,
            )
        if (
            not error.new_block
            or self._abuse_warning is None
            or error.user_id is None
            or error.chat_id is None
        ):
            return
        warning = (
            settings.abuse_warning_message
            if settings is not None
            else (
                "Your requests have been paused because the bot received repeated "
                "requests too quickly. Please stop repeated or automated requests "
                "and try again later."
            )
        )
        try:
            await self._abuse_warning(
                error.user_id,
                error.chat_id,
                f"{warning}\n\nPlease try again in about {error.retry_after} seconds.",
            )
        except Exception as warning_error:
            LOGGER.warning(
                "Unable to send a Mini App abuse warning (%s)",
                type(warning_error).__name__,
            )

    def _record_access(
        self,
        request: MiniAppHttpRequest,
        response: MiniAppHttpResponse,
        *,
        session: MiniAppSession | None,
        duration_seconds: float,
    ) -> None:
        self._traffic["requests"] += 1
        if response.status < 400:
            self._traffic["successes"] += 1
        elif response.status < 500:
            self._traffic["client_errors"] += 1
        else:
            self._traffic["server_errors"] += 1
        settings = self._audit_settings
        if settings is None or (not self._access_log and response.status < 400):
            return
        audit_event(
            LOGGER,
            settings,
            "mini_app_request",
            metadata={
                "method": request.method.upper()[:8],
                "route": self._route_name(request.target),
                "status": response.status,
                "duration_ms": round(duration_seconds * 1000, 3),
            },
            identity=audit_identity(
                settings,
                user_id=session.user_id if session is not None else None,
                chat_id=session.chat_id if session is not None else None,
                client_ip=request.client_key,
            ),
            level=logging.WARNING if response.status >= 400 else logging.INFO,
        )

    def _session_from_request(
        self,
        request: MiniAppHttpRequest,
    ) -> MiniAppSession | None:
        authorization = _header(request.headers, "authorization") or ""
        match = _BEARER_RE.fullmatch(authorization)
        if match is None:
            return None
        return self._sessions.get(match.group(1), touch=False)

    def _rate_cost(self, request: MiniAppHttpRequest) -> float:
        path = urlsplit(request.target).path
        expensive = {
            f"{self._api_prefix}/scripture",
            f"{self._api_prefix}/search",
            f"{self._api_prefix}/post",
        }
        if request.method.upper() == "POST" and path in expensive:
            return 1.0
        return self._navigation_rate_cost

    def _route_name(self, target: str) -> str:
        path = urlsplit(target).path
        prefix = f"{self._api_prefix}/"
        if not path.startswith(prefix):
            return "not_found"
        relative = path[len(prefix) :]
        if self._search_path_re.fullmatch(path) is not None:
            return "search_page"
        if self._basket_item_path_re.fullmatch(path) is not None:
            return "basket_item"
        known = {
            "session",
            "translations",
            "books",
            "chapters",
            "scripture",
            "search",
            "basket",
            "basket/items",
            "basket/order",
            "preferences",
            "post",
        }
        return relative if relative in known else "not_found"

    async def _translations(self, session: MiniAppSession) -> MiniAppHttpResponse:
        options = await self._service.translations()
        return self._response(
            200,
            {
                "selected": session.translation,
                "items": [_translation_payload(option) for option in options],
            },
        )

    async def _books(
        self,
        session: MiniAppSession,
        query: str,
    ) -> MiniAppHttpResponse:
        values = _query(query)
        if set(values) - {"translation"}:
            raise MiniAppApiInputError("Books request contains unsupported parameters.")
        translation = _translation(values.get("translation"), session.translation)
        books = await self._service.books(translation)
        return self._response(
            200,
            {
                "translation": translation,
                "items": [_book_payload(book) for book in books],
            },
        )

    async def _chapters(
        self,
        session: MiniAppSession,
        query: str,
    ) -> MiniAppHttpResponse:
        values = _query(query)
        if set(values) - {"translation", "book"}:
            raise MiniAppApiInputError("Chapters request contains unsupported parameters.")
        translation = _translation(values.get("translation"), session.translation)
        book_number = _positive_integer(values.get("book"), "book", maximum=1000)
        books = await self._service.books(translation)
        book = next((item for item in books if item.number == book_number), None)
        if book is None:
            raise MiniAppApiInputError("book is not available in this translation.")
        chapters = await self._service.chapters(translation, book)
        return self._response(
            200,
            {
                "translation": translation,
                "book": _book_payload(book),
                "items": [_chapter_payload(chapter) for chapter in chapters],
            },
        )

    async def _scripture(
        self,
        session: MiniAppSession,
        request: MiniAppHttpRequest,
    ) -> MiniAppHttpResponse:
        payload = self._json_body(request)
        if set(payload) - {"translation", "book", "chapter", "verse"}:
            raise MiniAppApiInputError("Scripture request contains unsupported fields.")
        translation = _translation(payload.get("translation"), session.translation)
        book_number = _positive_integer(payload.get("book"), "book", maximum=200)
        chapter_number = _positive_integer(
            payload.get("chapter"),
            "chapter",
            maximum=250,
        )
        books = await self._service.books(translation)
        book = next((item for item in books if item.number == book_number), None)
        if book is None:
            raise MiniAppApiInputError("book is not available in this translation.")
        chapters = await self._service.chapters(translation, book)
        chapter = next(
            (item for item in chapters if item.number == chapter_number),
            None,
        )
        if chapter is None:
            raise MiniAppApiInputError("chapter is not available in this book.")
        if len(chapter.verses) > MAX_MINIAPP_CHAPTER_VERSES:
            raise ScriptureUnavailable("The chapter exceeds the Mini App display bound.")
        target_verse = _positive_integer(
            payload.get("verse", chapter.verses[0]),
            "verse",
            maximum=500,
        )
        if target_verse not in chapter.verses:
            raise MiniAppApiInputError("verse is not available in this chapter.")
        content = await self._service.chapter(translation, book, chapter)
        previous, following = await self._chapter_navigation(
            translation,
            books,
            book,
            chapters,
            chapter,
        )
        items: list[dict[str, object]] = []
        for verse in content.verses:
            selection = self._sessions.register_selection(
                session,
                reference=f"{book.name} {chapter.number}:{verse.number}",
                translation=translation,
                book_number=book.number,
                book_name=book.name,
                chapter=chapter.number,
                verse=verse.number,
                text=verse.text,
            )
            items.append(_selection_payload(selection))
        return self._response(
            200,
            {
                "translation": translation,
                "book": _book_payload(book),
                "chapter": chapter.number,
                "reference": content.reference,
                "target_verse": target_verse,
                "sha": content.sha,
                "navigation": {
                    "previous": previous,
                    "next": following,
                },
                "items": items,
            },
        )

    async def _chapter_navigation(
        self,
        translation: str,
        books: Sequence[BookOption],
        book: BookOption,
        chapters: Sequence[ChapterOption],
        chapter: ChapterOption,
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        book_index = next(
            (index for index, item in enumerate(books) if item.number == book.number),
            -1,
        )
        chapter_index = next(
            (
                index
                for index, item in enumerate(chapters)
                if item.number == chapter.number
            ),
            -1,
        )
        if book_index < 0 or chapter_index < 0:
            raise ScriptureUnavailable("The chapter navigation state is invalid.")

        previous: dict[str, object] | None = None
        following: dict[str, object] | None = None
        if chapter_index > 0:
            previous = _reader_location_payload(
                book,
                chapters[chapter_index - 1].number,
            )
        elif book_index > 0:
            previous_book = books[book_index - 1]
            previous_chapters = await self._service.chapters(
                translation,
                previous_book,
            )
            if previous_chapters:
                previous = _reader_location_payload(
                    previous_book,
                    previous_chapters[-1].number,
                )

        if chapter_index + 1 < len(chapters):
            following = _reader_location_payload(
                book,
                chapters[chapter_index + 1].number,
            )
        elif book_index + 1 < len(books):
            following_book = books[book_index + 1]
            following_chapters = await self._service.chapters(
                translation,
                following_book,
            )
            if following_chapters:
                following = _reader_location_payload(
                    following_book,
                    following_chapters[0].number,
                )
        return previous, following

    async def _search(
        self,
        session: MiniAppSession,
        request: MiniAppHttpRequest,
    ) -> MiniAppHttpResponse:
        payload = self._json_body(request)
        if set(payload) - {"query", "options"}:
            raise MiniAppApiInputError("Search request contains unsupported fields.")
        query = _required_text(
            payload,
            "query",
            self._service.settings.max_input_length,
        )
        preferences = self._preferences.preferences_for(session.user_id)
        options = _search_options(
            payload.get("options"),
            session.translation,
            defaults=preferences.search_defaults,
        )
        page = await self._service.search(query, options)
        search = self._sessions.remember_search(
            session,
            query=page.query,
            translation=page.translation,
            total=page.total,
            items=page.items,
        )
        return self._response(200, self._search_payload(search, 0))

    def _search_page(
        self,
        session: MiniAppSession,
        search_token: str,
        query: str,
    ) -> MiniAppHttpResponse:
        values = _query(query)
        if set(values) - {"page"}:
            raise MiniAppApiInputError("Search page contains unsupported parameters.")
        page = _nonnegative_integer(values.get("page", "0"), "page", maximum=10_000)
        search = self._sessions.search(session, search_token)
        if search is None:
            return self._error_response(
                404,
                "search_not_found",
                "Search results are unavailable or expired.",
            )
        return self._response(200, self._search_payload(search, page))

    def _basket(self, session: MiniAppSession) -> MiniAppHttpResponse:
        return self._response(200, self._basket_payload(session))

    def _add_basket_item(
        self,
        session: MiniAppSession,
        request: MiniAppHttpRequest,
    ) -> MiniAppHttpResponse:
        payload = self._json_body(request)
        if set(payload) != {"selection_id"}:
            raise MiniAppApiInputError("Basket item request is invalid.")
        selection_token = _required_text(payload, "selection_id", 128)
        self._sessions.add_to_basket(session, selection_token)
        return self._response(200, self._basket_payload(session))

    def _remove_basket_item(
        self,
        session: MiniAppSession,
        selection_token: str,
    ) -> MiniAppHttpResponse:
        self._sessions.remove_from_basket(session, selection_token)
        return self._response(200, self._basket_payload(session))

    def _reorder_basket(
        self,
        session: MiniAppSession,
        request: MiniAppHttpRequest,
    ) -> MiniAppHttpResponse:
        payload = self._json_body(request)
        if set(payload) != {"selection_ids"}:
            raise MiniAppApiInputError("Basket order request is invalid.")
        selection_tokens = payload.get("selection_ids")
        if not isinstance(selection_tokens, list) or any(
            not isinstance(token, str) for token in selection_tokens
        ):
            raise MiniAppApiInputError("selection_ids must be an array of IDs.")
        self._sessions.reorder_basket(session, selection_tokens)
        return self._response(200, self._basket_payload(session))

    async def _update_preferences(
        self,
        session: MiniAppSession,
        request: MiniAppHttpRequest,
    ) -> MiniAppHttpResponse:
        payload = self._json_body(request)
        if set(payload) - {"translation", "search_defaults", "reader_location"}:
            raise MiniAppApiInputError("preferences contains unsupported fields.")
        if not payload:
            raise MiniAppApiInputError("preferences must not be empty.")
        async with session.preference_lock:
            current = self._preferences.preferences_for(session.user_id)
            translation = current.translation
            if "translation" in payload:
                translation = _translation(payload["translation"], session.translation)
                available = {
                    option.code for option in await self._service.translations()
                }
                if translation not in available:
                    raise MiniAppApiInputError("translation is not available.")

            defaults: SearchDefaults | None = None
            if "search_defaults" in payload:
                try:
                    defaults = SearchDefaults.validated(payload["search_defaults"])
                except ValueError as error:
                    raise MiniAppApiInputError(str(error)) from error

            location: ReaderLocation | None | object = _PREFERENCE_UNCHANGED
            if "reader_location" in payload:
                raw_location = payload["reader_location"]
                if raw_location is None:
                    location = None
                else:
                    try:
                        location = ReaderLocation.validated(raw_location)
                    except ValueError as error:
                        raise MiniAppApiInputError(str(error)) from error
                    if location.translation != translation:
                        raise MiniAppApiInputError(
                            "Reader location translation must match the preference."
                        )
                    await self._validate_reader_location(location)

            try:
                if location is _PREFERENCE_UNCHANGED:
                    preferences = self._preferences.update_preferences(
                        session.user_id,
                        translation=(
                            translation if "translation" in payload else None
                        ),
                        search_defaults=defaults,
                    )
                else:
                    preferences = self._preferences.update_preferences(
                        session.user_id,
                        translation=(
                            translation if "translation" in payload else None
                        ),
                        search_defaults=defaults,
                        reader_location=cast(ReaderLocation | None, location),
                    )
            except ValueError as error:
                raise MiniAppApiInputError(str(error)) from error
            self._sessions.set_user_translation(
                session.user_id,
                preferences.translation,
            )
            return self._response(
                200,
                {"preferences": preferences.as_dict()},
            )

    async def _validate_reader_location(self, location: ReaderLocation) -> None:
        books = await self._service.books(location.translation)
        book = next((item for item in books if item.number == location.book), None)
        if book is None:
            raise MiniAppApiInputError(
                "Reader book is not available in this translation."
            )
        chapters = await self._service.chapters(location.translation, book)
        chapter = next(
            (item for item in chapters if item.number == location.chapter),
            None,
        )
        if chapter is None:
            raise MiniAppApiInputError("Reader chapter is not available in this book.")
        if location.verse not in chapter.verses:
            raise MiniAppApiInputError("Reader verse is not available in this chapter.")

    async def _post(
        self,
        session: MiniAppSession,
        request: MiniAppHttpRequest,
    ) -> MiniAppHttpResponse:
        payload = self._json_body(request)
        if set(payload) != {"idempotency_key"}:
            raise MiniAppApiInputError("Post request is invalid.")
        idempotency_key = _required_text(payload, "idempotency_key", 128)
        if re.fullmatch(r"[A-Fa-f0-9-]{16,64}", idempotency_key) is None:
            raise MiniAppApiInputError("idempotency_key is invalid.")
        async with session.post_lock:
            selections = self._sessions.basket(session)
            previous = self._sessions.post_attempt(session, idempotency_key)
            if previous is not None and not selections:
                return self._post_attempt_response(previous)
            if not selections:
                raise MiniAppApiInputError("The Scripture basket is empty.")
            basket_digest = _basket_digest(selections)
            attempt, created = self._sessions.begin_post(
                session,
                idempotency_key,
                basket_digest,
            )
            if not created:
                return self._post_attempt_response(attempt)
            resolved = [(selection.reference, selection.translation) for selection in selections]
            try:
                queries = await self._resolve_grouped_queries(resolved)
                raw_message_ids = await self._post_scripture(session.launch, queries)
                message_ids = tuple(raw_message_ids or ())
                if not message_ids or any(
                    isinstance(message_id, bool)
                    or not isinstance(message_id, int)
                    or message_id <= 0
                    for message_id in message_ids
                ):
                    raise ScriptureUnavailable("The Telegram post response was invalid.")
            except BaseException:
                self._sessions.fail_post(
                    session,
                    idempotency_key,
                    basket_digest,
                )
                raise
            self._sessions.complete_post(
                session,
                idempotency_key,
                basket_digest,
                message_ids,
            )
            self._sessions.clear_basket(session)
            return self._response(
                200,
                {
                    "status": "posted",
                    "message_ids": list(message_ids),
                    "idempotent_replay": False,
                },
            )

    def _post_attempt_response(
        self,
        attempt: MiniAppPostAttempt,
    ) -> MiniAppHttpResponse:
        if attempt.state == "completed":
            return self._response(
                200,
                {
                    "status": "posted",
                    "message_ids": list(attempt.message_ids),
                    "idempotent_replay": True,
                },
            )
        return self._error_response(
            409,
            "post_outcome_locked",
            (
                "This basket already has an incomplete posting attempt. "
                "Review the target chat before creating a new selection."
            ),
        )

    async def _resolve_grouped_queries(
        self,
        items: Sequence[tuple[str, str]],
    ) -> tuple[ScriptureQuery, ...]:
        grouped: list[tuple[str, list[str]]] = []
        group_limit = min(
            self._service.settings.max_references,
            self._service.settings.max_total_verses,
        )
        for reference, translation in items:
            can_append = (
                bool(grouped)
                and grouped[-1][0] == translation
                and len(grouped[-1][1]) < group_limit
                and len(";".join((*grouped[-1][1], reference)))
                <= self._service.settings.max_input_length
            )
            if not can_append:
                grouped.append((translation, [reference]))
            else:
                grouped[-1][1].append(reference)
        queries: list[ScriptureQuery] = []
        for translation, references in grouped:
            queries.append(
                await self._service.resolve_query(
                    [";".join(references)],
                    default_translation=translation,
                )
            )
        return tuple(queries)

    def _search_payload(self, search: MiniAppSearch, page: int) -> dict[str, Any]:
        available = len(search.items)
        pages = max(1, math.ceil(available / self._page_size))
        if page >= pages:
            raise MiniAppApiInputError("page is outside the available result set.")
        start = page * self._page_size
        stop = min(start + self._page_size, available)
        return {
            "search_id": search.token,
            "query": search.query,
            "translation": search.translation,
            "total": search.total,
            "available": available,
            "truncated": search.total > available,
            "page": page,
            "page_count": pages,
            "items": [_selection_payload(search.items[index]) for index in range(start, stop)],
        }

    def _basket_payload(self, session: MiniAppSession) -> dict[str, Any]:
        items = self._sessions.basket(session)
        return {
            "count": len(items),
            "maximum": self._service.settings.mini_app_max_selections,
            "items": [_selection_payload(item) for item in items],
        }

    def _json_body(self, request: MiniAppHttpRequest) -> dict[str, Any]:
        content_type = (_header(request.headers, "content-type") or "").split(";", 1)[0]
        if content_type.strip().casefold() != "application/json":
            raise MiniAppApiInputError("Content-Type must be application/json.")
        try:
            payload = json.loads(request.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MiniAppApiInputError("Request body must be valid JSON.") from error
        if not isinstance(payload, dict):
            raise MiniAppApiInputError("Request body must be a JSON object.")
        return payload

    def _method_not_allowed(self, allow: str) -> MiniAppHttpResponse:
        return self._error_response(
            405,
            "method_not_allowed",
            "HTTP method is not allowed for this resource.",
            extra_headers={"Allow": allow},
        )

    def _error_response(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> MiniAppHttpResponse:
        payload: dict[str, object] = {"error": code, "message": message}
        if details is not None:
            payload.update(details)
        return self._response(
            status,
            payload,
            extra_headers=extra_headers,
        )

    def _response(
        self,
        status: int,
        payload: dict[str, Any] | None,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> MiniAppHttpResponse:
        headers = {
            "Access-Control-Allow-Origin": self._origin,
            "Cache-Control": "no-store, max-age=0",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Permissions-Policy": ("camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
            "Referrer-Policy": "no-referrer",
            "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
            "Vary": "Origin",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }
        if payload is None:
            body = b""
        else:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if extra_headers is not None:
            headers.update(extra_headers)
        return MiniAppHttpResponse(status=status, headers=headers, body=body)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


def _query(value: str) -> dict[str, str]:
    try:
        parsed = parse_qs(
            value,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=16,
        )
    except ValueError as error:
        raise MiniAppApiInputError("Invalid query string.") from error
    if any(len(values) != 1 for values in parsed.values()):
        raise MiniAppApiInputError("Duplicate query parameters are not allowed.")
    return {key: values[0] for key, values in parsed.items()}


def _required_text(
    payload: Mapping[str, Any],
    key: str,
    maximum: int,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise MiniAppApiInputError(f"{key} must be text.")
    result = value.strip()
    if not result or len(result) > maximum or "\x00" in result:
        raise MiniAppApiInputError(f"{key} must contain between 1 and {maximum} characters.")
    return result


def _translation(value: object, default: str) -> str:
    if value is None:
        result = default.casefold()
    elif isinstance(value, str):
        result = value.casefold()
    else:
        raise MiniAppApiInputError("translation must be text.")
    if _TRANSLATION_RE.fullmatch(result) is None:
        raise MiniAppApiInputError("translation is invalid.")
    return result


def _positive_integer(value: object, label: str, *, maximum: int) -> int:
    parsed = _nonnegative_integer(value, label, maximum=maximum)
    if parsed == 0:
        raise MiniAppApiInputError(f"{label} must be positive.")
    return parsed


def _nonnegative_integer(value: object, label: str, *, maximum: int) -> int:
    if isinstance(value, str):
        if not value.isascii() or not value.isdecimal():
            raise MiniAppApiInputError(f"{label} must be an integer.")
        parsed = int(value)
    elif isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    else:
        raise MiniAppApiInputError(f"{label} must be an integer.")
    if not 0 <= parsed <= maximum:
        raise MiniAppApiInputError(f"{label} is outside the supported range.")
    return parsed


def _search_options(
    value: object,
    default_translation: str,
    *,
    defaults: SearchDefaults,
) -> SearchOptions:
    if value is None:
        payload: Mapping[str, Any] = {}
    elif isinstance(value, dict):
        payload = value
    else:
        raise MiniAppApiInputError("options must be an object.")

    unknown = set(payload) - {
        "translation",
        "words",
        "match",
        "scope",
        "case_sensitive",
        "diacritics",
        "sort",
        "books",
        "exclude",
        "proximity",
    }
    if unknown:
        raise MiniAppApiInputError("options contains unsupported fields.")

    enum_values: dict[str, str] = {}
    for key, allowed in _SEARCH_ENUMS.items():
        candidate = payload.get(key, getattr(defaults, key))
        if not isinstance(candidate, str) or candidate not in allowed:
            raise MiniAppApiInputError(f"options.{key} is invalid.")
        enum_values[key] = candidate

    case_sensitive = payload.get("case_sensitive", defaults.case_sensitive)
    if not isinstance(case_sensitive, bool):
        raise MiniAppApiInputError("options.case_sensitive must be boolean.")

    raw_books = payload.get("books", [])
    if (
        not isinstance(raw_books, list)
        or len(raw_books) > 200
        or any(
            isinstance(book, bool) or not isinstance(book, int) or not 1 <= book <= 1000
            for book in raw_books
        )
        or len(set(raw_books)) != len(raw_books)
    ):
        raise MiniAppApiInputError("options.books is invalid.")

    raw_exclude = payload.get("exclude", [])
    if (
        not isinstance(raw_exclude, list)
        or len(raw_exclude) > 20
        or any(
            not isinstance(term, str) or not 1 <= len(term.strip()) <= 80
            for term in raw_exclude
        )
    ):
        raise MiniAppApiInputError("options.exclude is invalid.")

    raw_proximity = payload.get("proximity")
    proximity: int | None
    if raw_proximity is None:
        proximity = None
    else:
        proximity = _nonnegative_integer(
            raw_proximity,
            "options.proximity",
            maximum=100,
        )

    return SearchOptions(
        translation=_translation(payload.get("translation"), default_translation),
        words=enum_values["words"],
        match=enum_values["match"],
        scope=enum_values["scope"],
        case_sensitive=case_sensitive,
        diacritics=enum_values["diacritics"],
        sort=enum_values["sort"],
        books=tuple(raw_books),
        exclude=tuple(term.strip() for term in raw_exclude),
        proximity=proximity,
    )


def _translation_payload(option: TranslationOption) -> dict[str, object]:
    return {
        "code": option.code,
        "name": option.name,
        "language": option.language,
        "lang": option.lang,
        "direction": option.direction,
    }


def _book_payload(book: BookOption) -> dict[str, object]:
    return {"number": book.number, "name": book.name}


def _chapter_payload(chapter: ChapterOption) -> dict[str, object]:
    return {"number": chapter.number, "verses": list(chapter.verses)}


def _reader_location_payload(
    book: BookOption,
    chapter: int,
) -> dict[str, object]:
    return {
        "book": book.number,
        "book_name": book.name,
        "chapter": chapter,
    }


def _selection_payload(item: MiniAppSelection) -> dict[str, object]:
    return {
        "selection_id": item.token,
        "reference": item.reference,
        "translation": item.translation,
        "book_number": item.book_number,
        "book_name": item.book_name,
        "chapter": item.chapter,
        "verse": item.verse,
        "text": item.text,
        "terms": list(item.terms),
    }


def _basket_digest(items: Sequence[MiniAppSelection]) -> bytes:
    """Bind an idempotency attempt to the exact authoritative posting order."""
    payload = [
        {
            "reference": item.reference,
            "translation": item.translation,
            "book": item.book_number,
            "chapter": item.chapter,
            "verse": item.verse,
        }
        for item in items
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.blake2s(encoded, digest_size=32).digest()
