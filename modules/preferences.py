"""Durable, bounded per-user preferences for one robot instance."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")


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
        self._memory: dict[int, str] = {}
        self._connection: sqlite3.Connection | None = None
        if self._path is not None:
            self._open()

    def translation_for(self, user_id: int) -> str:
        """Return one user's saved translation or the application default."""
        identity = self._user_id(user_id)
        with self._guard:
            if self._connection is None:
                return self._memory.get(identity, self._default_translation)
            row = self._connection.execute(
                "SELECT translation FROM user_preferences WHERE user_id = ?",
                (identity,),
            ).fetchone()
            if row is None or not isinstance(row[0], str):
                return self._default_translation
            try:
                return self._translation(row[0])
            except ValueError:
                return self._default_translation

    def set_translation(self, user_id: int, translation: str) -> None:
        """Save one validated translation and enforce the configured bound."""
        identity = self._user_id(user_id)
        code = self._translation(translation)
        with self._guard:
            if self._connection is None:
                self._memory[identity] = code
                while len(self._memory) > self._max_users:
                    self._memory.pop(next(iter(self._memory)))
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
                overflow = self._count_locked() - self._max_users
                if overflow > 0:
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
                    updated_at INTEGER NOT NULL
                )
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

    @staticmethod
    def _user_id(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("Telegram user ID must be a positive integer.")
        return value

    @staticmethod
    def _translation(value: str) -> str:
        code = value.casefold()
        if _TRANSLATION_RE.fullmatch(code) is None:
            raise ValueError("Translation code is invalid.")
        return code
