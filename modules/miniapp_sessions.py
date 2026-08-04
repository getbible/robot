"""Bounded, owner-bound Telegram Mini App launch and interaction state."""

from __future__ import annotations

import asyncio
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal
from urllib.parse import quote, urlencode, urlsplit

from .interactions import SearchResult
from .miniapp_auth import TelegramMiniAppPrincipal

MAX_MINIAPP_SELECTION_TEXT_BYTES = 4096
# Covers the configured maximum 200-item basket plus one complete accepted
# 250-verse chapter at the maximum validated Unicode/metadata bounds.
MAX_MINIAPP_SESSION_RETAINED_BYTES = 8 * 1024 * 1024
# Leave enough headroom below the supported container's 210 MiB child-RSS
# guard for the warmed Librarian corpus, interpreter, and transient requests.
MAX_MINIAPP_PROCESS_RETAINED_BYTES = 32 * 1024 * 1024
MAX_MINIAPP_SESSION_RETAINED_SELECTIONS = 2500
MAX_MINIAPP_PROCESS_RETAINED_SELECTIONS = 250_000
_MINIAPP_SELECTION_OVERHEAD_BYTES = 2048
_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
MiniAppRoute = Literal["home", "bible", "search"]


class MiniAppSessionExpiredError(ValueError):
    """An authenticated request outlived its bounded server session."""


class MiniAppSessionInputError(ValueError):
    """A client supplied an invalid Mini App session-state mutation."""


class MiniAppSessionCapacityError(RuntimeError):
    """All bounded session slots are temporarily pinned by active posts."""


@dataclass(slots=True)
class MiniAppLaunch:
    """One command-created launch target bound to its Telegram user and chat."""

    token: str
    user_id: int
    target_chat_id: int
    message_thread_id: int | None
    initial_route: MiniAppRoute
    initial_query: str
    created_at: float
    prompt_message_id: int | None = None
    prompt_ephemeral_message_id: int | None = None
    source_ephemeral_message_id: int | None = None
    source_ephemeral_receiver_user_id: int | None = None


class MiniAppLaunchStore:
    """One-time TTL/LRU handoff from a bot command to an authenticated Mini App."""

    def __init__(
        self,
        *,
        max_launches: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= max_launches <= 100_000:
            raise ValueError("max_launches must be between 1 and 100000.")
        if not 30 <= ttl_seconds <= 3600:
            raise ValueError("ttl_seconds must be between 30 and 3600.")
        self._max_launches = max_launches
        self._ttl = ttl_seconds
        self._clock = clock
        self._launches: OrderedDict[str, MiniAppLaunch] = OrderedDict()
        self._guard = threading.RLock()

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
        if (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id <= 0
            or isinstance(target_chat_id, bool)
            or not isinstance(target_chat_id, int)
            or target_chat_id == 0
        ):
            raise ValueError("Telegram user and target chat IDs are invalid.")
        if message_thread_id is not None and (
            isinstance(message_thread_id, bool)
            or not isinstance(message_thread_id, int)
            or message_thread_id <= 0
        ):
            raise ValueError("message_thread_id must be a positive integer or None.")
        if initial_route not in {"home", "bible", "search"}:
            raise ValueError("initial_route is invalid.")
        source_values = (
            source_ephemeral_message_id,
            source_ephemeral_receiver_user_id,
        )
        if (
            sum(value is not None for value in source_values) not in {0, 2}
            or any(
                value is not None
                and (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value <= 0
                )
                for value in source_values
            )
        ):
            raise ValueError(
                "Ephemeral source message and receiver IDs must be valid together."
            )
        query = initial_query.strip()
        if len(query) > 240 or "\x00" in query:
            raise ValueError("initial_query is invalid.")
        with self._guard:
            self._purge_locked()
            token = MiniAppSessionStore._new_token(self._launches, size=24)
            launch = MiniAppLaunch(
                token=token,
                user_id=user_id,
                target_chat_id=target_chat_id,
                message_thread_id=message_thread_id,
                initial_route=initial_route,
                initial_query=query,
                created_at=self._clock(),
                source_ephemeral_message_id=source_ephemeral_message_id,
                source_ephemeral_receiver_user_id=(
                    source_ephemeral_receiver_user_id
                ),
            )
            self._launches[token] = launch
            while len(self._launches) > self._max_launches:
                self._launches.popitem(last=False)
            return launch

    def consume(self, token: str, *, user_id: int) -> MiniAppLaunch | None:
        """Consume a launch exactly once and only for its bound Telegram user."""
        if _TOKEN_RE.fullmatch(token) is None:
            return None
        with self._guard:
            self._purge_locked()
            launch = self._launches.get(token)
            if launch is None or launch.user_id != user_id:
                return None
            self._launches.pop(token, None)
            return launch

    def peek(self, token: str, *, user_id: int) -> MiniAppLaunch | None:
        """Validate a launch without consuming it during exchange preflight."""
        if _TOKEN_RE.fullmatch(token) is None:
            return None
        with self._guard:
            self._purge_locked()
            launch = self._launches.get(token)
            if launch is None or launch.user_id != user_id:
                return None
            return launch

    def take_expired(
        self,
        token: str,
        *,
        user_id: int,
    ) -> MiniAppLaunch | None:
        """Remove and return one expired owner launch for message cleanup."""
        if _TOKEN_RE.fullmatch(token) is None:
            return None
        with self._guard:
            launch = self._launches.get(token)
            if (
                launch is not None
                and launch.user_id == user_id
                and self._clock() - launch.created_at >= self._ttl
            ):
                self._launches.pop(token, None)
                self._purge_locked()
                return launch
            self._purge_locked()
            return None

    def restore(self, launch: MiniAppLaunch) -> None:
        """Restore a consumed launch after a local session-creation failure."""
        with self._guard:
            self._purge_locked()
            if (
                self._clock() - launch.created_at < self._ttl
                and launch.token not in self._launches
            ):
                self._launches[launch.token] = launch
                while len(self._launches) > self._max_launches:
                    self._launches.popitem(last=False)

    def remember_prompt(
        self,
        launch: MiniAppLaunch,
        *,
        message_id: int | None = None,
        ephemeral_message_id: int | None = None,
    ) -> MiniAppLaunch:
        """Attach exactly one bot-created launch prompt for later cleanup."""
        values = (message_id, ephemeral_message_id)
        if sum(value is not None for value in values) != 1 or any(
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            )
            for value in values
        ):
            raise ValueError("Exactly one valid launch prompt identifier is required.")
        with self._guard:
            self._purge_locked()
            current = self._launches.get(launch.token)
            if current is not None and current is not launch:
                raise ValueError("Mini App launch identity does not match.")
            if (
                launch.prompt_message_id is not None
                or launch.prompt_ephemeral_message_id is not None
            ):
                raise ValueError("Mini App launch already has a prompt.")
            # Mutate the issued object in place. If the user exchanged the
            # launch after Telegram returned the prompt but before this method
            # acquired the lock, the newly created session holds this same
            # object and observes the prompt identifiers for later cleanup.
            launch.prompt_message_id = message_id
            launch.prompt_ephemeral_message_id = ephemeral_message_id
            return launch

    def _purge_locked(self) -> None:
        now = self._clock()
        expired = [
            token
            for token, launch in self._launches.items()
            if now - launch.created_at >= self._ttl
        ]
        for token in expired:
            self._launches.pop(token, None)


def miniapp_public_web_url(public_url: str) -> str:
    """Normalize the configured public URL for relative static and API paths."""
    parts = urlsplit(public_url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("public_url must be an HTTPS URL without credentials or query.")
    return f"{public_url.rstrip('/')}/"


def miniapp_web_url(public_url: str, launch_token: str) -> str:
    """Build the fixed HTTPS Web App URL for an inline/menu launch."""
    if _TOKEN_RE.fullmatch(launch_token) is None:
        raise ValueError("launch_token is invalid.")
    normalized = miniapp_public_web_url(public_url)
    return f"{normalized}?{urlencode({'launch': launch_token})}"


def miniapp_direct_url(
    bot_username: str,
    launch_token: str,
) -> str:
    """Build Telegram's Main Mini App link for group-aware launches."""
    username = bot_username.removeprefix("@")
    component_re = re.compile(r"[A-Za-z0-9_]{3,64}\Z")
    if (
        component_re.fullmatch(username) is None
        or _TOKEN_RE.fullmatch(launch_token) is None
        or len(launch_token) > 64
    ):
        raise ValueError("Mini App direct-link parameters are invalid.")
    return (
        f"https://t.me/{quote(username, safe='')}?"
        f"startapp={quote(launch_token, safe='')}&mode=compact"
    )


@dataclass(frozen=True, slots=True)
class MiniAppSearch:
    """One authoritative, bounded Librarian result cached for client-side paging."""

    token: str
    query: str
    translation: str
    total: int
    items: tuple[MiniAppSelection, ...]


@dataclass(frozen=True, slots=True)
class MiniAppSelection:
    """One authoritative verse represented by an opaque, session-local ID."""

    token: str
    reference: str
    translation: str
    book_number: int
    book_name: str
    chapter: int
    verse: int
    text: str
    terms: tuple[str, ...] = ()


def _selection_identity(
    selection: MiniAppSelection,
) -> tuple[str, int, int, int]:
    return (
        selection.translation,
        selection.book_number,
        selection.chapter,
        selection.verse,
    )


def _selection_retained_bytes(selection: MiniAppSelection) -> int:
    """Conservatively account for one retained selection and its strings."""
    strings = (
        selection.token,
        selection.reference,
        selection.translation,
        selection.book_name,
        selection.text,
        *selection.terms,
    )
    return _MINIAPP_SELECTION_OVERHEAD_BYTES + sum(
        len(value.encode("utf-8")) for value in strings
    )


MiniAppPostState = Literal["pending", "completed", "failed"]


@dataclass(slots=True)
class MiniAppPostAttempt:
    """Basket-bound final-post state used to prevent duplicate retries."""

    idempotency_key: str
    basket_digest: bytes
    state: MiniAppPostState = "pending"
    message_ids: tuple[int, ...] = ()


@dataclass(slots=True)
class MiniAppSession:
    """One opaque browser session bound to one authenticated Telegram user."""

    token: str
    user_id: int
    chat_id: int
    query_id: str | None
    chat_instance: str | None
    created_at: float
    touched_at: float
    translation: str
    launch: MiniAppLaunch
    init_data_digest: bytes
    searches: OrderedDict[str, MiniAppSearch] = field(default_factory=OrderedDict)
    available_selections: OrderedDict[str, MiniAppSelection] = field(default_factory=OrderedDict)
    basket: list[MiniAppSelection] = field(default_factory=list)
    post_attempts: OrderedDict[str, MiniAppPostAttempt] = field(default_factory=OrderedDict)
    post_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    retained_selection_bytes: int = 0
    retained_selection_count: int = 0


class MiniAppSessionStore:
    """Thread-safe TTL/LRU state with independent bounds for every cache."""

    def __init__(
        self,
        *,
        max_sessions: int,
        ttl_seconds: float,
        max_sessions_per_user: int = 2,
        max_searches_per_session: int = 4,
        max_available_selections: int = 512,
        max_basket_selections: int = 50,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= max_sessions <= 100_000:
            raise ValueError("max_sessions must be between 1 and 100000.")
        if not 30 <= ttl_seconds <= 86_400:
            raise ValueError("ttl_seconds must be between 30 and 86400.")
        if not 1 <= max_sessions_per_user <= 10:
            raise ValueError("max_sessions_per_user must be between 1 and 10.")
        if not 1 <= max_searches_per_session <= 16:
            raise ValueError("max_searches_per_session must be between 1 and 16.")
        if not 250 <= max_available_selections <= 5000:
            raise ValueError(
                "max_available_selections must be between 250 and 5000."
            )
        if not 1 <= max_basket_selections <= 200:
            raise ValueError("max_basket_selections must be between 1 and 200.")
        self._max_sessions = max_sessions
        self._max_sessions_per_user = max_sessions_per_user
        self._ttl = ttl_seconds
        self._max_searches = max_searches_per_session
        self._max_available = max_available_selections
        self._max_basket = max_basket_selections
        self._clock = clock
        self._sessions: OrderedDict[str, MiniAppSession] = OrderedDict()
        self._guard = threading.RLock()
        self._retained_selection_bytes = 0
        self._retained_selection_count = 0
        self._created = 0
        self._expired = 0
        self._evicted = 0

    def create(
        self,
        principal: TelegramMiniAppPrincipal,
        *,
        translation: str,
        launch: MiniAppLaunch,
        init_data_digest: bytes,
    ) -> MiniAppSession:
        with self._guard:
            self._purge_locked()
            user_sessions = [
                token
                for token, current in self._sessions.items()
                if current.user_id == principal.user_id
            ]
            while len(user_sessions) >= self._max_sessions_per_user:
                oldest = next(
                    (
                        token
                        for token in user_sessions
                        if not self._sessions[token].post_lock.locked()
                    ),
                    None,
                )
                if oldest is None:
                    raise MiniAppSessionCapacityError(
                        "Active posts temporarily occupy this user's sessions."
                    )
                user_sessions.remove(oldest)
                self._drop_session_locked(oldest)
                self._evicted += 1
            while len(self._sessions) >= self._max_sessions:
                oldest = self._oldest_evictable_session_locked()
                if oldest is None:
                    raise MiniAppSessionCapacityError(
                        "Active posts temporarily occupy every Mini App session."
                    )
                self._drop_session_locked(oldest)
                self._evicted += 1
            token = self._new_token(self._sessions)
            now = self._clock()
            session = MiniAppSession(
                token=token,
                user_id=principal.user_id,
                chat_id=principal.rate_limit_chat_id,
                query_id=principal.query_id,
                chat_instance=principal.chat_instance,
                created_at=now,
                touched_at=now,
                translation=translation,
                launch=launch,
                init_data_digest=init_data_digest,
            )
            self._sessions[token] = session
            self._created += 1
            return session

    @property
    def max_basket_selections(self) -> int:
        """Return the configured atomic-post selection bound."""
        return self._max_basket

    def get(self, token: str, *, touch: bool = True) -> MiniAppSession | None:
        if _TOKEN_RE.fullmatch(token) is None:
            return None
        with self._guard:
            self._purge_locked()
            session = self._sessions.get(token)
            if session is None:
                return None
            if touch:
                session.touched_at = self._clock()
                self._sessions.move_to_end(token)
            return session

    def find_by_launch(
        self,
        launch_token: str,
        *,
        user_id: int,
    ) -> MiniAppSession | None:
        """Find an active session for a consumed owner-bound launch token."""
        if _TOKEN_RE.fullmatch(launch_token) is None:
            return None
        with self._guard:
            self._purge_locked()
            for session in reversed(tuple(self._sessions.values())):
                if (
                    session.user_id == user_id
                    and session.launch.token == launch_token
                ):
                    return session
            return None

    def take_expired_launch(
        self,
        launch_token: str,
        *,
        user_id: int,
    ) -> MiniAppLaunch | None:
        """Remove one expired owner session and return its launch for cleanup."""
        if _TOKEN_RE.fullmatch(launch_token) is None:
            return None
        with self._guard:
            now = self._clock()
            for token, session in tuple(self._sessions.items()):
                if (
                    session.user_id == user_id
                    and session.launch.token == launch_token
                    and now - session.created_at >= self._ttl
                    and not session.post_lock.locked()
                ):
                    self._drop_session_locked(token)
                    self._expired += 1
                    self._purge_locked()
                    return session.launch
            self._purge_locked()
            return None

    def rebind(
        self,
        session: MiniAppSession,
        principal: TelegramMiniAppPrincipal,
        *,
        init_data_digest: bytes,
    ) -> bool:
        """Bind a reopened WebView to the same active user/chat session."""
        if not isinstance(init_data_digest, bytes) or len(init_data_digest) != 32:
            raise ValueError("init_data_digest must be a SHA-256 digest.")
        with self._guard:
            self._purge_locked()
            if (
                self._sessions.get(session.token) is not session
                or session.user_id != principal.user_id
                or session.chat_id != principal.rate_limit_chat_id
                or session.chat_instance != principal.chat_instance
            ):
                return False
            session.query_id = principal.query_id
            session.init_data_digest = init_data_digest
            session.touched_at = self._clock()
            self._sessions.move_to_end(session.token)
            return True

    def touch(self, session: MiniAppSession) -> bool:
        """Mark an authenticated absolute-lifetime session as recently used."""
        with self._guard:
            self._purge_locked()
            if self._sessions.get(session.token) is not session:
                return False
            session.touched_at = self._clock()
            self._sessions.move_to_end(session.token)
            return True

    def revoke(self, token: str) -> None:
        with self._guard:
            self._drop_session_locked(token)

    def set_user_translation(self, user_id: int, translation: str) -> None:
        """Keep every active session for one user on the explicit preference."""
        with self._guard:
            self._purge_locked()
            for session in self._sessions.values():
                if session.user_id == user_id:
                    session.translation = translation

    def remember_search(
        self,
        session: MiniAppSession,
        *,
        query: str,
        translation: str,
        total: int,
        items: Sequence[SearchResult],
    ) -> MiniAppSearch:
        with self._guard:
            current = self._sessions.get(session.token)
            if current is not session:
                raise MiniAppSessionExpiredError(
                    "Mini App session is no longer active."
                )
            token = self._new_token(current.searches, size=18)
            selections = tuple(
                self._register_selection_locked(
                    current,
                    reference=item.reference,
                    translation=translation,
                    book_number=item.book_number,
                    book_name=item.book_name,
                    chapter=item.chapter,
                    verse=item.verse,
                    text=item.text,
                    terms=item.terms,
                )
                for item in items
            )
            search = MiniAppSearch(
                token=token,
                query=query,
                translation=translation,
                total=total,
                items=selections,
            )
            current.searches[token] = search
            while len(current.searches) > self._max_searches:
                current.searches.popitem(last=False)
            self._enforce_retained_selection_budget_locked(current)
            return search

    def register_selection(
        self,
        session: MiniAppSession,
        *,
        reference: str,
        translation: str,
        book_number: int,
        book_name: str,
        chapter: int,
        verse: int,
        text: str,
        terms: Sequence[str] = (),
    ) -> MiniAppSelection:
        with self._guard:
            current = self._sessions.get(session.token)
            if current is not session:
                raise MiniAppSessionExpiredError(
                    "Mini App session is no longer active."
                )
            return self._register_selection_locked(
                current,
                reference=reference,
                translation=translation,
                book_number=book_number,
                book_name=book_name,
                chapter=chapter,
                verse=verse,
                text=text,
                terms=tuple(terms),
            )

    def basket(self, session: MiniAppSession) -> tuple[MiniAppSelection, ...]:
        with self._guard:
            if self._sessions.get(session.token) is not session:
                raise MiniAppSessionExpiredError(
                    "Mini App session is no longer active."
                )
            return tuple(session.basket)

    def add_to_basket(
        self,
        session: MiniAppSession,
        selection_token: str,
    ) -> tuple[MiniAppSelection, ...]:
        if _TOKEN_RE.fullmatch(selection_token) is None:
            raise MiniAppSessionInputError("Selection is invalid or expired.")
        with self._guard:
            if self._sessions.get(session.token) is not session:
                raise MiniAppSessionExpiredError(
                    "Mini App session is no longer active."
                )
            selection = session.available_selections.get(selection_token)
            if selection is None:
                selection = next(
                    (
                        item
                        for search in reversed(tuple(session.searches.values()))
                        for item in search.items
                        if item.token == selection_token
                    ),
                    None,
                )
            if selection is None:
                raise MiniAppSessionInputError("Selection is invalid or expired.")
            identity = _selection_identity(selection)
            if all(_selection_identity(item) != identity for item in session.basket):
                if len(session.basket) >= self._max_basket:
                    raise MiniAppSessionInputError("The Scripture basket is full.")
                session.basket.append(selection)
                session.available_selections[selection.token] = selection
                session.available_selections.move_to_end(selection.token)
                self._evict_available_locked(session)
                self._enforce_retained_selection_budget_locked(session)
            return tuple(session.basket)

    def remove_from_basket(
        self,
        session: MiniAppSession,
        selection_token: str,
    ) -> tuple[MiniAppSelection, ...]:
        with self._guard:
            if self._sessions.get(session.token) is not session:
                raise MiniAppSessionExpiredError(
                    "Mini App session is no longer active."
                )
            session.basket[:] = [item for item in session.basket if item.token != selection_token]
            self._evict_available_locked(session)
            self._enforce_retained_selection_budget_locked(session)
            return tuple(session.basket)

    def reorder_basket(
        self,
        session: MiniAppSession,
        selection_tokens: Sequence[str],
    ) -> tuple[MiniAppSelection, ...]:
        with self._guard:
            if self._sessions.get(session.token) is not session:
                raise MiniAppSessionExpiredError(
                    "Mini App session is no longer active."
                )
            existing = {item.token: item for item in session.basket}
            if (
                len(selection_tokens) != len(existing)
                or len(set(selection_tokens)) != len(selection_tokens)
                or set(selection_tokens) != set(existing)
            ):
                raise MiniAppSessionInputError(
                    "Basket order must contain every current selection once."
                )
            session.basket[:] = [existing[token] for token in selection_tokens]
            return tuple(session.basket)

    def clear_basket(self, session: MiniAppSession) -> None:
        with self._guard:
            if self._sessions.get(session.token) is not session:
                raise MiniAppSessionExpiredError(
                    "Mini App session is no longer active."
                )
            session.basket.clear()
            self._evict_available_locked(session)
            self._enforce_retained_selection_budget_locked(session)

    def post_attempt(
        self,
        session: MiniAppSession,
        idempotency_key: str,
    ) -> MiniAppPostAttempt | None:
        with self._guard:
            if self._sessions.get(session.token) is not session:
                return None
            attempt = session.post_attempts.get(idempotency_key)
            if attempt is not None:
                session.post_attempts.move_to_end(idempotency_key)
            return attempt

    def begin_post(
        self,
        session: MiniAppSession,
        idempotency_key: str,
        basket_digest: bytes,
    ) -> tuple[MiniAppPostAttempt, bool]:
        """Reserve one authoritative basket before any Telegram send starts."""
        with self._guard:
            if self._sessions.get(session.token) is not session:
                raise MiniAppSessionExpiredError(
                    "Mini App session is no longer active."
                )
            existing = session.post_attempts.get(idempotency_key)
            if existing is not None:
                session.post_attempts.move_to_end(idempotency_key)
                if existing.basket_digest != basket_digest:
                    raise MiniAppSessionInputError(
                        "Idempotency key belongs to a different basket."
                    )
                return existing, False
            for attempt in session.post_attempts.values():
                if attempt.basket_digest == basket_digest:
                    return attempt, False
            attempt = MiniAppPostAttempt(
                idempotency_key=idempotency_key,
                basket_digest=basket_digest,
            )
            session.post_attempts[idempotency_key] = attempt
            while len(session.post_attempts) > 16:
                session.post_attempts.popitem(last=False)
            return attempt, True

    def complete_post(
        self,
        session: MiniAppSession,
        idempotency_key: str,
        basket_digest: bytes,
        message_ids: Sequence[int],
    ) -> None:
        with self._guard:
            if self._sessions.get(session.token) is not session:
                raise MiniAppSessionExpiredError(
                    "Mini App session is no longer active."
                )
            attempt = session.post_attempts.get(idempotency_key)
            if (
                attempt is None
                or attempt.basket_digest != basket_digest
                or attempt.state != "pending"
            ):
                raise ValueError("Mini App post reservation is invalid.")
            attempt.state = "completed"
            attempt.message_ids = tuple(message_ids)

    def fail_post(
        self,
        session: MiniAppSession,
        idempotency_key: str,
        basket_digest: bytes,
    ) -> None:
        """Keep an indeterminate attempt closed against duplicate retries."""
        with self._guard:
            if self._sessions.get(session.token) is not session:
                return
            attempt = session.post_attempts.get(idempotency_key)
            if (
                attempt is not None
                and attempt.basket_digest == basket_digest
                and attempt.state == "pending"
            ):
                attempt.state = "failed"

    def search(self, session: MiniAppSession, token: str) -> MiniAppSearch | None:
        if _TOKEN_RE.fullmatch(token) is None:
            return None
        with self._guard:
            current = self._sessions.get(session.token)
            if current is not session:
                return None
            search = current.searches.get(token)
            if search is not None:
                current.searches.move_to_end(token)
            return search

    def snapshot(self) -> dict[str, int | float]:
        with self._guard:
            self._purge_locked()
            selections = sum(
                len(session.available_selections)
                for session in self._sessions.values()
            )
            searches = sum(
                len(session.searches)
                for session in self._sessions.values()
            )
            return {
                "sessions": len(self._sessions),
                "max_sessions": self._max_sessions,
                "max_sessions_per_user": self._max_sessions_per_user,
                "available_selections": selections,
                "searches": searches,
                "retained_selection_bytes": self._retained_selection_bytes,
                "retained_selection_count": self._retained_selection_count,
                "ttl_seconds": self._ttl,
                "created": self._created,
                "expired": self._expired,
                "evicted": self._evicted,
            }

    def _purge_locked(self) -> None:
        now = self._clock()
        expired_sessions = [
            token
            for token, session in self._sessions.items()
            if (
                now - session.created_at >= self._ttl
                and not session.post_lock.locked()
            )
        ]
        for token in expired_sessions:
            self._drop_session_locked(token)
            self._expired += 1

    def _register_selection_locked(
        self,
        session: MiniAppSession,
        *,
        reference: str,
        translation: str,
        book_number: int,
        book_name: str,
        chapter: int,
        verse: int,
        text: str,
        terms: tuple[str, ...],
    ) -> MiniAppSelection:
        if (
            not isinstance(reference, str)
            or not isinstance(translation, str)
            or not isinstance(book_number, int)
            or isinstance(book_number, bool)
            or not 1 <= book_number <= 200
            or not isinstance(book_name, str)
            or not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not 1 <= chapter <= 1000
            or not isinstance(verse, int)
            or isinstance(verse, bool)
            or not 1 <= verse <= 2000
            or not isinstance(text, str)
            or _TRANSLATION_RE.fullmatch(translation) is None
        ):
            raise ValueError("Selection metadata exceeds the Mini App bounds.")
        normalized_book_name = book_name.strip()
        if not normalized_book_name or len(normalized_book_name) > 128:
            raise ValueError("Selection metadata exceeds the Mini App bounds.")
        if not text.strip() or len(text.encode("utf-8")) > MAX_MINIAPP_SELECTION_TEXT_BYTES:
            raise ValueError("Selection text exceeds the retained display bound.")
        normalized_reference = reference.strip()
        if not normalized_reference or len(normalized_reference) > 180:
            normalized_reference = (
                f"{normalized_book_name} {chapter}:{verse}"
            )
        normalized_terms = tuple(
            term.strip()
            for term in terms
            if isinstance(term, str) and 1 <= len(term.strip()) <= 80
        )[:20]
        identity = (translation, book_number, chapter, verse)
        existing = next(
            (
                item
                for item in session.basket
                if _selection_identity(item) == identity
            ),
            None,
        )
        if existing is None:
            existing = next(
                (
                    item
                    for item in session.available_selections.values()
                    if _selection_identity(item) == identity
                ),
                None,
            )
        if existing is None:
            existing = next(
                (
                    item
                    for search in reversed(tuple(session.searches.values()))
                    for item in search.items
                    if _selection_identity(item) == identity
                ),
                None,
            )
        if existing is not None:
            current = replace(
                existing,
                reference=normalized_reference,
                book_number=book_number,
                book_name=normalized_book_name,
                chapter=chapter,
                verse=verse,
                text=text,
                terms=normalized_terms,
            )
            session.available_selections[existing.token] = current
            retained: list[MiniAppSelection] = []
            replaced = False
            for item in session.basket:
                if _selection_identity(item) == identity:
                    if not replaced:
                        retained.append(current)
                        replaced = True
                else:
                    retained.append(item)
            session.basket[:] = retained
            session.available_selections.move_to_end(existing.token)
            self._evict_available_locked(session)
            self._enforce_retained_selection_budget_locked(session)
            return current
        token = self._new_token(session.available_selections, size=18)
        selection = MiniAppSelection(
            token=token,
            reference=normalized_reference,
            translation=translation,
            book_number=book_number,
            book_name=normalized_book_name,
            chapter=chapter,
            verse=verse,
            text=text,
            terms=normalized_terms,
        )
        session.available_selections[token] = selection
        self._evict_available_locked(session)
        self._enforce_retained_selection_budget_locked(session)
        return selection

    def _evict_available_locked(self, session: MiniAppSession) -> None:
        protected = {item.token for item in session.basket}
        # Basket entries have their own independently bounded budget. Keeping
        # that budget separate ensures a full 250-verse response remains
        # selectable even when the basket is already populated.
        maximum = self._max_available + len(protected)
        while len(session.available_selections) > maximum:
            removable = next(
                (
                    token
                    for token in session.available_selections
                    if token not in protected
                ),
                None,
            )
            if removable is None:
                break
            session.available_selections.pop(removable, None)

    def _enforce_retained_selection_budget_locked(
        self,
        session: MiniAppSession,
    ) -> None:
        """Bound retained selection payloads per session and process."""
        self._refresh_retained_selection_budget_locked(session)
        while (
            (
                session.retained_selection_bytes
                > MAX_MINIAPP_SESSION_RETAINED_BYTES
                or session.retained_selection_count
                > MAX_MINIAPP_SESSION_RETAINED_SELECTIONS
            )
            and session.available_selections
        ):
            protected = {item.token for item in session.basket}
            # Retained searches are historical views. Discard the oldest
            # snapshot before invalidating an opaque ID that was just issued
            # for the chapter currently visible in the reader.
            if session.searches:
                _, expired = session.searches.popitem(last=False)
                remaining_search_tokens = {
                    item.token
                    for search in session.searches.values()
                    for item in search.items
                }
                for item in expired.items:
                    if (
                        item.token not in protected
                        and item.token not in remaining_search_tokens
                        and session.available_selections.get(item.token) is item
                    ):
                        session.available_selections.pop(item.token, None)
                self._refresh_retained_selection_budget_locked(session)
                continue
            removable = next(
                (
                    token
                    for token in session.available_selections
                    if token not in protected
                ),
                None,
            )
            if removable is None:
                break
            session.available_selections.pop(removable, None)
            self._refresh_retained_selection_budget_locked(session)

        while (
            self._retained_selection_bytes > MAX_MINIAPP_PROCESS_RETAINED_BYTES
            or self._retained_selection_count
            > MAX_MINIAPP_PROCESS_RETAINED_SELECTIONS
        ):
            oldest = next(
                (
                    token
                    for token, current in self._sessions.items()
                    if (
                        current is not session
                        and not current.post_lock.locked()
                    )
                ),
                None,
            )
            if oldest is None:
                self._drop_session_locked(session.token)
                raise MiniAppSessionExpiredError(
                    "Mini App session exceeded the bounded selection capacity."
                )
            self._drop_session_locked(oldest)
            self._evicted += 1

    def _oldest_evictable_session_locked(self) -> str | None:
        return next(
            (
                token
                for token, session in self._sessions.items()
                if not session.post_lock.locked()
            ),
            None,
        )

    def _refresh_retained_selection_budget_locked(
        self,
        session: MiniAppSession,
    ) -> None:
        previous_bytes = session.retained_selection_bytes
        previous_count = session.retained_selection_count
        unique: dict[int, MiniAppSelection] = {}
        for item in session.available_selections.values():
            unique[id(item)] = item
        for item in session.basket:
            unique[id(item)] = item
        for search in session.searches.values():
            for item in search.items:
                unique[id(item)] = item
        current_bytes = sum(
            _selection_retained_bytes(item) for item in unique.values()
        )
        current_count = len(unique)
        session.retained_selection_bytes = current_bytes
        session.retained_selection_count = current_count
        self._retained_selection_bytes += current_bytes - previous_bytes
        self._retained_selection_count += current_count - previous_count

    def _drop_session_locked(self, token: str) -> MiniAppSession | None:
        session = self._sessions.pop(token, None)
        if session is not None:
            self._retained_selection_bytes = max(
                0,
                self._retained_selection_bytes
                - session.retained_selection_bytes,
            )
            self._retained_selection_count = max(
                0,
                self._retained_selection_count
                - session.retained_selection_count,
            )
            session.retained_selection_bytes = 0
            session.retained_selection_count = 0
        return session

    @staticmethod
    def _new_token(mapping: object, *, size: int = 32) -> str:
        while True:
            token = secrets.token_urlsafe(size)
            if token not in mapping:  # type: ignore[operator]
                return token
