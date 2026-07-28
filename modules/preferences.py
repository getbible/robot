"""Durable, bounded per-user preferences for one robot instance."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")
_WORDS = frozenset({"all", "any", "phrase"})
_MATCHES = frozenset({"whole_word", "substring"})
_SCOPES = frozenset({"bible", "old_testament", "new_testament", "deuterocanon"})
_DIACRITICS = frozenset({"sensitive", "insensitive"})
_SORTS = frozenset({"canonical", "relevance"})


@dataclass(frozen=True, slots=True)
class SearchDefaults:
    """Non-content search choices that may safely persist between launches."""

    words: str = "all"
    match: str = "whole_word"
    scope: str = "bible"
    case_sensitive: bool = False
    diacritics: str = "sensitive"
    sort: str = "canonical"

    @classmethod
    def validated(cls, value: object | None = None, **overrides: object) -> SearchDefaults:
        payload: dict[str, object] = {}
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError("Search defaults must be an object.")
            payload.update(value)
        payload.update(overrides)
        allowed = {
            "words",
            "match",
            "scope",
            "case_sensitive",
            "diacritics",
            "sort",
        }
        if unknown := set(payload) - allowed:
            raise ValueError(f"Unknown search default: {sorted(unknown)[0]}.")
        result = cls(
            words=_choice(payload.get("words", "all"), _WORDS, "words"),
            match=_choice(payload.get("match", "whole_word"), _MATCHES, "match"),
            scope=_choice(payload.get("scope", "bible"), _SCOPES, "scope"),
            case_sensitive=_boolean(
                payload.get("case_sensitive", False),
                "case_sensitive",
            ),
            diacritics=_choice(
                payload.get("diacritics", "sensitive"),
                _DIACRITICS,
                "diacritics",
            ),
            sort=_choice(payload.get("sort", "canonical"), _SORTS, "sort"),
        )
        return result

    def as_dict(self) -> dict[str, str | bool]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReaderLocation:
    """A content-free reading position safe to retain between launches."""

    translation: str
    book: int
    chapter: int
    verse: int = 1

    @classmethod
    def validated(cls, value: object) -> ReaderLocation:
        if not isinstance(value, dict):
            raise ValueError("Reader location must be an object.")
        if set(value) - {"translation", "book", "chapter", "verse"}:
            raise ValueError("Reader location contains unsupported fields.")
        translation = value.get("translation")
        if not isinstance(translation, str):
            raise ValueError("Reader location translation is invalid.")
        code = translation.casefold()
        if _TRANSLATION_RE.fullmatch(code) is None:
            raise ValueError("Reader location translation is invalid.")
        return cls(
            translation=code,
            book=_bounded_integer(value.get("book"), "book", 1, 200),
            chapter=_bounded_integer(value.get("chapter"), "chapter", 1, 250),
            verse=_bounded_integer(value.get("verse", 1), "verse", 1, 500),
        )

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class UserPreferences:
    """The bounded profile retained for one Telegram user."""

    translation: str
    search_defaults: SearchDefaults
    reader_location: ReaderLocation | None

    def as_dict(self) -> dict[str, object]:
        return {
            "translation": self.translation,
            "search_defaults": self.search_defaults.as_dict(),
            "reader_location": (
                self.reader_location.as_dict()
                if self.reader_location is not None
                else None
            ),
        }


class UserPreferenceStore:
    """Persist Telegram-user defaults without storing profile or message data."""

    def __init__(
        self,
        *,
        path: str | None,
        default_translation: str,
        max_users: int,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._default_translation = self._translation(default_translation)
        self._max_users = max_users
        self._guard = threading.RLock()
        self._memory: dict[int, UserPreferences] = {}
        self._connection: sqlite3.Connection | None = None
        if self._path is not None:
            self._open()

    def translation_for(self, user_id: int) -> str:
        """Return one user's saved translation or the application default."""
        return self.preferences_for(user_id).translation

    def preferences_for(self, user_id: int) -> UserPreferences:
        """Return one user's validated durable choices without profile data."""
        identity = self._user_id(user_id)
        with self._guard:
            if self._connection is None:
                return self._memory.get(identity, self._defaults())
            row = self._connection.execute(
                """
                SELECT translation, search_defaults, reader_location
                FROM user_preferences
                WHERE user_id = ?
                """,
                (identity,),
            ).fetchone()
            if row is None:
                return self._defaults()
            try:
                translation = self._translation(row[0])
                search_defaults = self._decode_search_defaults(row[1])
                reader_location = self._decode_reader_location(row[2])
            except (ValueError, TypeError):
                return self._defaults()
            return UserPreferences(translation, search_defaults, reader_location)

    def set_translation(self, user_id: int, translation: str) -> None:
        """Save one validated translation and enforce the configured bound."""
        identity = self._user_id(user_id)
        code = self._translation(translation)
        with self._guard:
            if self._connection is None:
                current = self._memory.get(identity, self._defaults())
                self._memory[identity] = UserPreferences(
                    code,
                    current.search_defaults,
                    current.reader_location,
                )
                self._trim_memory()
                return
            now = time.time_ns()
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO user_preferences (user_id, translation, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        translation = excluded.translation,
                        updated_at = excluded.updated_at
                    """,
                    (identity, code, now),
                )
                self._trim_database()

    def set_search_defaults(
        self,
        user_id: int,
        value: SearchDefaults | dict[str, object],
    ) -> None:
        """Persist only allow-listed filter modes, never query or exclusion text."""
        identity = self._user_id(user_id)
        defaults = (
            value
            if isinstance(value, SearchDefaults)
            else SearchDefaults.validated(value)
        )
        encoded = json.dumps(
            defaults.as_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._guard:
            if self._connection is None:
                current = self._memory.get(identity, self._defaults())
                self._memory[identity] = UserPreferences(
                    current.translation,
                    defaults,
                    current.reader_location,
                )
                self._trim_memory()
                return
            now = time.time_ns()
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO user_preferences (
                        user_id,
                        translation,
                        search_defaults,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        search_defaults = excluded.search_defaults,
                        updated_at = excluded.updated_at
                    """,
                    (identity, self._default_translation, encoded, now),
                )
                self._trim_database()

    def set_reader_location(
        self,
        user_id: int,
        value: ReaderLocation | dict[str, object],
    ) -> None:
        """Persist only translation/book/chapter/verse identifiers."""
        identity = self._user_id(user_id)
        location = (
            value
            if isinstance(value, ReaderLocation)
            else ReaderLocation.validated(value)
        )
        encoded = json.dumps(
            location.as_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._guard:
            if self._connection is None:
                current = self._memory.get(identity, self._defaults())
                self._memory[identity] = UserPreferences(
                    current.translation,
                    current.search_defaults,
                    location,
                )
                self._trim_memory()
                return
            now = time.time_ns()
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO user_preferences (
                        user_id,
                        translation,
                        reader_location,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        reader_location = excluded.reader_location,
                        updated_at = excluded.updated_at
                    """,
                    (
                        identity,
                        self._default_translation,
                        encoded,
                        now,
                    ),
                )
                self._trim_database()

    def close(self) -> None:
        """Close the durable store; repeated calls are safe."""
        with self._guard:
            if self._connection is None:
                return
            self._connection.close()
            self._connection = None

    def _open(self) -> None:
        assert self._path is not None
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._path,
            timeout=5.0,
            isolation_level="DEFERRED",
            check_same_thread=False,
        )
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    translation TEXT NOT NULL,
                    search_defaults TEXT NOT NULL DEFAULT '{}',
                    reader_location TEXT NOT NULL DEFAULT '{}',
                    updated_at INTEGER NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(user_preferences)"
                ).fetchall()
            }
            if "search_defaults" not in columns:
                connection.execute(
                    """
                    ALTER TABLE user_preferences
                    ADD COLUMN search_defaults TEXT NOT NULL DEFAULT '{}'
                    """
                )
            if "reader_location" not in columns:
                connection.execute(
                    """
                    ALTER TABLE user_preferences
                    ADD COLUMN reader_location TEXT NOT NULL DEFAULT '{}'
                    """
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    user_preferences_updated_at
                ON user_preferences (updated_at, user_id)
                """
            )
            connection.commit()
        except Exception:
            connection.close()
            raise
        self._connection = connection

    def _count_locked(self) -> int:
        assert self._connection is not None
        row = self._connection.execute(
            "SELECT COUNT(*) FROM user_preferences"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def _trim_memory(self) -> None:
        while len(self._memory) > self._max_users:
            self._memory.pop(next(iter(self._memory)))

    def _trim_database(self) -> None:
        assert self._connection is not None
        overflow = self._count_locked() - self._max_users
        if overflow <= 0:
            return
        self._connection.execute(
            """
            DELETE FROM user_preferences
            WHERE user_id IN (
                SELECT user_id
                FROM user_preferences
                ORDER BY updated_at ASC, user_id ASC
                LIMIT ?
            )
            """,
            (overflow,),
        )

    def _defaults(self) -> UserPreferences:
        return UserPreferences(self._default_translation, SearchDefaults(), None)

    @staticmethod
    def _decode_search_defaults(value: Any) -> SearchDefaults:
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError("Stored search defaults are invalid.")
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ValueError("Stored search defaults are invalid.") from error
        return SearchDefaults.validated(decoded)

    @staticmethod
    def _decode_reader_location(value: Any) -> ReaderLocation | None:
        if not isinstance(value, str) or len(value) > 512:
            raise ValueError("Stored reader location is invalid.")
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ValueError("Stored reader location is invalid.") from error
        if decoded == {}:
            return None
        return ReaderLocation.validated(decoded)

    @staticmethod
    def _user_id(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("Telegram user ID must be a positive integer.")
        return value

    @staticmethod
    def _translation(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("Translation code is invalid.")
        code = value.casefold()
        if _TRANSLATION_RE.fullmatch(code) is None:
            raise ValueError("Translation code is invalid.")
        return code


def _choice(value: object, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"Search default {label} is invalid.")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Search default {label} must be a boolean.")
    return value


def _bounded_integer(
    value: object,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"Reader location {label} is invalid.")
    return value
