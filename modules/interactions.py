"""Bounded in-memory state for Telegram-native interactive workflows."""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from .catalog import BookOption, ChapterOption, TranslationOption

InteractionKind = Literal["reference", "search"]


@dataclass(frozen=True, slots=True)
class SearchOptions:
    """User-configurable Librarian search criteria."""

    translation: str = "kjv"
    words: str = "all"
    match: str = "whole_word"
    scope: str = "bible"
    case_sensitive: bool = False
    diacritics: str = "sensitive"
    sort: str = "canonical"
    books: tuple[int, ...] = ()
    exclude: tuple[str, ...] = ()
    proximity: int | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One selectable verse returned by Librarian search."""

    reference: str
    book_number: int
    book_name: str
    chapter: int
    verse: int
    text: str


@dataclass(slots=True)
class InteractionSession:
    """One user's state machine, keyed by an opaque callback token."""

    token: str
    chat_id: int
    user_id: int
    kind: InteractionKind
    stage: str
    touched_at: float
    message_id: int | None = None
    prompt_message_id: int | None = None
    translation: str = "kjv"
    translations: tuple[TranslationOption, ...] = ()
    books: tuple[BookOption, ...] = ()
    book: BookOption | None = None
    chapters: tuple[ChapterOption, ...] = ()
    chapter: ChapterOption | None = None
    start_verse: int | None = None
    end_verse: int | None = None
    testament: str = "all"
    search_options: SearchOptions = field(default_factory=SearchOptions)
    search_query: str = ""
    search_total: int = 0
    search_results: tuple[SearchResult, ...] = ()
    selected: set[int] = field(default_factory=set)


class InteractionStore:
    """A TTL/LRU store that prevents arbitrary callback-state growth."""

    def __init__(
        self,
        *,
        max_sessions: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_sessions = max_sessions
        self._ttl = ttl_seconds
        self._clock = clock
        self._sessions: OrderedDict[str, InteractionSession] = OrderedDict()
        self._guard = threading.RLock()
        self._created = 0
        self._expired = 0
        self._evicted = 0

    def create(
        self,
        *,
        chat_id: int,
        user_id: int,
        kind: InteractionKind,
        stage: str,
        translation: str,
    ) -> InteractionSession:
        with self._guard:
            self._purge_expired()
            token = self._new_token()
            now = self._clock()
            session = InteractionSession(
                token=token,
                chat_id=chat_id,
                user_id=user_id,
                kind=kind,
                stage=stage,
                touched_at=now,
                translation=translation,
                search_options=SearchOptions(translation=translation),
            )
            self._sessions[token] = session
            self._created += 1
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
                self._evicted += 1
            return session

    def get(
        self,
        token: str,
        *,
        chat_id: int,
        user_id: int,
    ) -> InteractionSession | None:
        with self._guard:
            session = self._sessions.get(token)
            if session is None:
                return None
            now = self._clock()
            if now - session.touched_at >= self._ttl:
                self._sessions.pop(token, None)
                self._expired += 1
                return None
            if session.chat_id != chat_id or session.user_id != user_id:
                return None
            session.touched_at = now
            self._sessions.move_to_end(token)
            return session

    def remove(self, token: str) -> None:
        with self._guard:
            self._sessions.pop(token, None)

    def find_prompt(
        self,
        *,
        chat_id: int,
        user_id: int,
        prompt_message_id: int,
    ) -> InteractionSession | None:
        with self._guard:
            self._purge_expired()
            for token in reversed(self._sessions):
                session = self._sessions[token]
                if (
                    session.chat_id == chat_id
                    and session.user_id == user_id
                    and session.prompt_message_id == prompt_message_id
                ):
                    session.touched_at = self._clock()
                    self._sessions.move_to_end(token)
                    return session
            return None

    def snapshot(self) -> dict[str, int | float]:
        with self._guard:
            self._purge_expired()
            return {
                "sessions": len(self._sessions),
                "max_sessions": self._max_sessions,
                "ttl_seconds": self._ttl,
                "created": self._created,
                "expired": self._expired,
                "evicted": self._evicted,
            }

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [
            token
            for token, session in self._sessions.items()
            if now - session.touched_at >= self._ttl
        ]
        for token in expired:
            self._sessions.pop(token, None)
            self._expired += 1

    def _new_token(self) -> str:
        while True:
            token = secrets.token_urlsafe(8)
            if token not in self._sessions:
                return token
