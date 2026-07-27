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

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
MiniAppRoute = Literal["home", "bible", "search"]


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
        query = initial_query.strip()
        if len(query) > 512 or "\x00" in query:
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


class MiniAppSessionStore:
    """Thread-safe TTL/LRU state with independent bounds for every cache."""

    def __init__(
        self,
        *,
        max_sessions: int,
        ttl_seconds: float,
        max_searches_per_session: int = 4,
        max_available_selections: int = 512,
        max_basket_selections: int = 50,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= max_sessions <= 100_000:
            raise ValueError("max_sessions must be between 1 and 100000.")
        if not 30 <= ttl_seconds <= 86_400:
            raise ValueError("ttl_seconds must be between 30 and 86400.")
        if not 1 <= max_searches_per_session <= 16:
            raise ValueError("max_searches_per_session must be between 1 and 16.")
        if not 25 <= max_available_selections <= 5000:
            raise ValueError("max_available_selections must be between 25 and 5000.")
        if not 1 <= max_basket_selections <= 200:
            raise ValueError("max_basket_selections must be between 1 and 200.")
        self._max_sessions = max_sessions
        self._ttl = ttl_seconds
        self._max_searches = max_searches_per_session
        self._max_available = max_available_selections
        self._max_basket = max_basket_selections
        self._clock = clock
        self._sessions: OrderedDict[str, MiniAppSession] = OrderedDict()
        self._guard = threading.RLock()
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
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
                self._evicted += 1
            return session

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
            self._sessions.pop(token, None)

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
                raise ValueError("Mini App session is no longer active.")
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
                raise ValueError("Mini App session is no longer active.")
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
                raise ValueError("Mini App session is no longer active.")
            return tuple(session.basket)

    def add_to_basket(
        self,
        session: MiniAppSession,
        selection_token: str,
    ) -> tuple[MiniAppSelection, ...]:
        if _TOKEN_RE.fullmatch(selection_token) is None:
            raise ValueError("Selection is invalid or expired.")
        with self._guard:
            if self._sessions.get(session.token) is not session:
                raise ValueError("Mini App session is no longer active.")
            selection = session.available_selections.get(selection_token)
            if selection is None:
                raise ValueError("Selection is invalid or expired.")
            if all(item.token != selection.token for item in session.basket):
                if len(session.basket) >= self._max_basket:
                    raise OverflowError("The Scripture basket is full.")
                session.basket.append(selection)
            return tuple(session.basket)

    def remove_from_basket(
        self,
        session: MiniAppSession,
        selection_token: str,
    ) -> tuple[MiniAppSelection, ...]:
        with self._guard:
            if self._sessions.get(session.token) is not session:
                raise ValueError("Mini App session is no longer active.")
            session.basket[:] = [item for item in session.basket if item.token != selection_token]
            return tuple(session.basket)

    def reorder_basket(
        self,
        session: MiniAppSession,
        selection_tokens: Sequence[str],
    ) -> tuple[MiniAppSelection, ...]:
        with self._guard:
            if self._sessions.get(session.token) is not session:
                raise ValueError("Mini App session is no longer active.")
            existing = {item.token: item for item in session.basket}
            if (
                len(selection_tokens) != len(existing)
                or len(set(selection_tokens)) != len(selection_tokens)
                or set(selection_tokens) != set(existing)
            ):
                raise ValueError("Basket order must contain every current selection once.")
            session.basket[:] = [existing[token] for token in selection_tokens]
            return tuple(session.basket)

    def clear_basket(self, session: MiniAppSession) -> None:
        with self._guard:
            if self._sessions.get(session.token) is not session:
                raise ValueError("Mini App session is no longer active.")
            session.basket.clear()

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
                raise ValueError("Mini App session is no longer active.")
            existing = session.post_attempts.get(idempotency_key)
            if existing is not None:
                session.post_attempts.move_to_end(idempotency_key)
                if existing.basket_digest != basket_digest:
                    raise ValueError("Idempotency key belongs to a different basket.")
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
                raise ValueError("Mini App session is no longer active.")
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
            return {
                "sessions": len(self._sessions),
                "max_sessions": self._max_sessions,
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
            if now - session.created_at >= self._ttl
        ]
        for token in expired_sessions:
            self._sessions.pop(token, None)
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
        for existing in session.available_selections.values():
            if existing.reference == reference and existing.translation == translation:
                current = replace(
                    existing,
                    book_number=book_number,
                    book_name=book_name,
                    chapter=chapter,
                    verse=verse,
                    text=text,
                    terms=terms,
                )
                session.available_selections[existing.token] = current
                session.basket[:] = [
                    current if item.token == existing.token else item
                    for item in session.basket
                ]
                session.available_selections.move_to_end(existing.token)
                return current
        token = self._new_token(session.available_selections, size=18)
        selection = MiniAppSelection(
            token=token,
            reference=reference,
            translation=translation,
            book_number=book_number,
            book_name=book_name,
            chapter=chapter,
            verse=verse,
            text=text,
            terms=terms,
        )
        session.available_selections[token] = selection
        while len(session.available_selections) > self._max_available:
            session.available_selections.popitem(last=False)
        return selection

    @staticmethod
    def _new_token(mapping: object, *, size: int = 32) -> str:
        while True:
            token = secrets.token_urlsafe(size)
            if token not in mapping:  # type: ignore[operator]
                return token
