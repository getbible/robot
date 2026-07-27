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
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from getbible import RequestLimitError

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
from .preferences import SearchDefaults, UserPreferenceStore
from .rate_limit import InboundRateLimiter
from .service import ScriptureQuery, ScriptureService

LOGGER = logging.getLogger(__name__)
_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")
_BEARER_RE = re.compile(r"Bearer ([A-Za-z0-9_-]{16,128})\Z")
MAX_MINIAPP_CHAPTER_VERSES = 250
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
                raise RobotRateLimited(retry_after)
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
        self._page_size = page_size
        self._max_body = max_body_bytes

    async def handle(self, request: MiniAppHttpRequest) -> MiniAppHttpResponse:
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
        except (OverflowError, ValueError) as error:
            return self._error_response(400, "invalid_request", str(error))
        except RobotRateLimited as error:
            return self._error_response(
                429,
                "rate_limited",
                "Too many requests. Please try again shortly.",
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
        return self._response(
            status,
            {
                "session_token": session.token,
                "expires_in": int(self._sessions.snapshot()["ttl_seconds"]),
                "user": {"id": session.user_id},
                "preferences": self._preferences.preferences_for(session.user_id).as_dict(),
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
        )
        if not self._sessions.touch(session):
            raise MiniAppAuthenticationError("Invalid Mini App session.")
        return session

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
        if set(payload) - {"translation", "book", "chapter"}:
            raise MiniAppApiInputError("Scripture request contains unsupported fields.")
        translation = _translation(payload.get("translation"), session.translation)
        book_number = _positive_integer(payload.get("book"), "book", maximum=1000)
        chapter_number = _positive_integer(
            payload.get("chapter"),
            "chapter",
            maximum=1000,
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
        chunk_size = min(
            self._service.settings.max_verses_per_reference,
            self._service.settings.max_total_verses,
        )
        loaded: dict[int, str] = {}
        for start in range(0, len(chapter.verses), chunk_size):
            numbers = chapter.verses[start : start + chunk_size]
            reference = f"{book.name} {chapter.number}:{_number_ranges(numbers)}"
            query = await self._service.resolve_query(
                [reference],
                default_translation=translation,
            )
            result = await self._service.select(query)
            for chapter_payload in _scripture_payload(result):
                if (
                    chapter_payload["book_name"] != book.name
                    or chapter_payload["chapter"] != chapter.number
                ):
                    raise ScriptureUnavailable(
                        "The Scripture repository returned the wrong chapter."
                    )
                for verse in chapter_payload["verses"]:
                    verse_number = int(verse["verse"])
                    if verse_number in loaded:
                        raise ScriptureUnavailable(
                            "The Scripture repository returned duplicate verses."
                        )
                    loaded[verse_number] = str(verse["text"])
        if set(loaded) != set(chapter.verses):
            raise ScriptureUnavailable("The Scripture repository returned an incomplete chapter.")
        self._remember_translation(session, translation)
        items: list[dict[str, object]] = []
        for verse_number in chapter.verses:
            selection = self._sessions.register_selection(
                session,
                reference=f"{book.name} {chapter.number}:{verse_number}",
                translation=translation,
                book_number=book.number,
                book_name=book.name,
                chapter=chapter.number,
                verse=verse_number,
                text=loaded[verse_number],
            )
            items.append(_selection_payload(selection))
        return self._response(
            200,
            {
                "translation": translation,
                "book": _book_payload(book),
                "chapter": chapter.number,
                "reference": f"{book.name} {chapter.number}",
                "items": items,
            },
        )

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
        self._remember_translation(session, page.translation)
        return self._response(200, self._search_payload(search, 0))

    def _search_page(
        self,
        session: MiniAppSession,
        search_token: str,
        query: str,
    ) -> MiniAppHttpResponse:
        values = _query(query)
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
        if set(payload) - {"translation", "search_defaults"}:
            raise MiniAppApiInputError("preferences contains unsupported fields.")
        if not payload:
            raise MiniAppApiInputError("preferences must not be empty.")
        if "translation" in payload:
            translation = _translation(payload["translation"], session.translation)
            if not await self._service.translation_exists(translation):
                raise MiniAppApiInputError("translation is not available.")
            self._preferences.set_translation(session.user_id, translation)
            session.translation = translation
        if "search_defaults" in payload:
            try:
                defaults = SearchDefaults.validated(payload["search_defaults"])
            except ValueError as error:
                raise MiniAppApiInputError(str(error)) from error
            self._preferences.set_search_defaults(session.user_id, defaults)
        return self._response(
            200,
            {"preferences": self._preferences.preferences_for(session.user_id).as_dict()},
        )

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

    def _remember_translation(
        self,
        session: MiniAppSession,
        translation: str,
    ) -> None:
        self._preferences.set_translation(session.user_id, translation)
        session.translation = translation

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


def _number_ranges(numbers: Sequence[int]) -> str:
    if not numbers:
        raise ScriptureUnavailable("The navigation catalog returned no verses.")
    result: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        result.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    result.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(result)


def _scripture_payload(result: object) -> list[dict[str, Any]]:
    if not isinstance(result, dict) or not result:
        raise ScriptureUnavailable("The Scripture repository returned no verses.")
    chapters: list[dict[str, Any]] = []
    for raw_chapter in result.values():
        if not isinstance(raw_chapter, dict):
            raise ScriptureUnavailable("The Scripture repository returned malformed data.")
        book_name = raw_chapter.get("book_name")
        abbreviation = raw_chapter.get("abbreviation")
        chapter_number = raw_chapter.get("chapter")
        raw_verses = raw_chapter.get("verses")
        if (
            not isinstance(book_name, str)
            or not book_name.strip()
            or not isinstance(abbreviation, str)
            or _TRANSLATION_RE.fullmatch(abbreviation.casefold()) is None
            or isinstance(chapter_number, bool)
            or not isinstance(chapter_number, int)
            or not 1 <= chapter_number <= 1000
            or not isinstance(raw_verses, list)
            or not raw_verses
        ):
            raise ScriptureUnavailable("The Scripture repository returned malformed data.")
        verses: list[dict[str, object]] = []
        for raw_verse in raw_verses:
            if not isinstance(raw_verse, dict):
                raise ScriptureUnavailable(
                    "The Scripture repository returned malformed verse data."
                )
            number = raw_verse.get("verse")
            text = raw_verse.get("text")
            if (
                isinstance(number, bool)
                or not isinstance(number, int)
                or not 1 <= number <= 2000
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise ScriptureUnavailable(
                    "The Scripture repository returned malformed verse data."
                )
            verses.append({"verse": number, "text": text.strip()})
        chapters.append(
            {
                "book_name": book_name.strip(),
                "translation": abbreviation.casefold(),
                "chapter": chapter_number,
                "verses": verses,
            }
        )
    return chapters
