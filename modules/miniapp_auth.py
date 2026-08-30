"""Strict server-side validation for Telegram Mini App initialization data."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

MAX_INIT_DATA_BYTES = 8 * 1024
MAX_INIT_DATA_FIELDS = 32
MAX_TELEGRAM_ID = (1 << 52) - 1


class MiniAppAuthenticationError(ValueError):
    """Telegram initialization data was absent, stale, or unauthentic."""


class MiniAppReplayError(MiniAppAuthenticationError):
    """Authenticated Telegram initialization data was already exchanged."""


@dataclass(frozen=True, slots=True)
class TelegramMiniAppPrincipal:
    """Authenticated Telegram identity and launch context."""

    user_id: int
    auth_date: int
    query_id: str | None
    chat_id: int | None
    chat_instance: str | None
    chat_type: str | None
    start_param: str | None
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None

    @property
    def rate_limit_chat_id(self) -> int:
        """Use a real chat ID when present and isolate menu launches per user otherwise."""
        return self.chat_id if self.chat_id is not None else self.user_id


class TelegramInitDataValidator:
    """Authenticate ``Telegram.WebApp.initData`` using Telegram's HMAC contract."""

    def __init__(
        self,
        bot_token: str,
        *,
        max_age_seconds: int = 300,
        future_skew_seconds: int = 30,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(bot_token, str) or not bot_token:
            raise ValueError("bot_token must be a non-empty string.")
        if not 30 <= max_age_seconds <= 3600:
            raise ValueError("max_age_seconds must be between 30 and 3600.")
        if not 0 <= future_skew_seconds <= 300:
            raise ValueError("future_skew_seconds must be between 0 and 300.")
        # Telegram mandates this exact HMAC-SHA-256 key derivation for Mini App
        # initData. This is protocol authentication, not password storage.
        # codeql[py/weak-sensitive-data-hashing]
        self._secret_key = hmac.new(
            b"WebAppData",
            bot_token.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        self._max_age = max_age_seconds
        self._future_skew = future_skew_seconds
        self._wall_clock = wall_clock

    def validate(
        self,
        raw_init_data: str,
        *,
        check_freshness: bool = True,
    ) -> TelegramMiniAppPrincipal:
        """Return a trusted principal or fail closed without exposing validation detail."""
        if not isinstance(raw_init_data, str) or not raw_init_data:
            raise MiniAppAuthenticationError("Invalid Telegram authorization.")
        try:
            encoded = raw_init_data.encode("utf-8")
        except UnicodeEncodeError as error:
            raise MiniAppAuthenticationError("Invalid Telegram authorization.") from error
        if len(encoded) > MAX_INIT_DATA_BYTES:
            raise MiniAppAuthenticationError("Invalid Telegram authorization.")

        try:
            pairs = parse_qsl(
                raw_init_data,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=MAX_INIT_DATA_FIELDS,
                encoding="utf-8",
                errors="strict",
            )
        except (UnicodeError, ValueError) as error:
            raise MiniAppAuthenticationError("Invalid Telegram authorization.") from error
        if not pairs:
            raise MiniAppAuthenticationError("Invalid Telegram authorization.")

        fields: dict[str, str] = {}
        for key, value in pairs:
            if not key or key in fields or "\n" in key or "\r" in key:
                raise MiniAppAuthenticationError("Invalid Telegram authorization.")
            fields[key] = value

        supplied_hash = fields.get("hash", "")
        if len(supplied_hash) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in supplied_hash
        ):
            raise MiniAppAuthenticationError("Invalid Telegram authorization.")
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(fields.items()) if key != "hash"
        )
        expected_hash = hmac.new(
            self._secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_hash, supplied_hash.casefold()):
            raise MiniAppAuthenticationError("Invalid Telegram authorization.")

        auth_date = _integer(fields.get("auth_date"), "auth_date")
        if check_freshness:
            now = int(self._wall_clock())
            if auth_date > now + self._future_skew or now - auth_date > self._max_age:
                raise MiniAppAuthenticationError("Telegram authorization has expired.")

        user = _json_object(fields.get("user"), "user")
        user_id = _telegram_id(user.get("id"), "user.id")
        if user.get("is_bot") is True:
            raise MiniAppAuthenticationError("Invalid Telegram authorization.")

        chat_id: int | None = None
        raw_chat = fields.get("chat")
        if raw_chat is not None:
            chat = _json_object(raw_chat, "chat")
            chat_id = _signed_telegram_id(chat.get("id"), "chat.id")

        return TelegramMiniAppPrincipal(
            user_id=user_id,
            first_name=_optional_json_text(user.get("first_name"), 64),
            last_name=_optional_json_text(user.get("last_name"), 64),
            username=_optional_json_text(user.get("username"), 64),
            language_code=_optional_json_text(user.get("language_code"), 16),
            auth_date=auth_date,
            query_id=_optional_text(fields.get("query_id"), 256),
            chat_id=chat_id,
            chat_instance=_optional_text(fields.get("chat_instance"), 256),
            chat_type=_chat_type(fields.get("chat_type")),
            start_param=_optional_text(fields.get("start_param"), 512),
        )


class TelegramInitDataReplayGuard:
    """Bounded replay cache for already-exchanged, authenticated initData."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 30 <= ttl_seconds <= 3600:
            raise ValueError("ttl_seconds must be between 30 and 3600.")
        if not 100 <= max_entries <= 200_000:
            raise ValueError("max_entries must be between 100 and 200000.")
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._digests: OrderedDict[bytes, float] = OrderedDict()
        self._guard = threading.RLock()

    def claim(self, raw_init_data: str) -> None:
        """Remember exact validated data once or reject its replay."""
        digest = self._digest(raw_init_data)
        now = self._clock()
        with self._guard:
            self._purge_locked(now)
            if digest in self._digests:
                raise MiniAppReplayError("Telegram authorization was replayed.")
            self._digests[digest] = now
            while len(self._digests) > self._max_entries:
                self._digests.popitem(last=False)

    def contains(self, raw_init_data: str) -> bool:
        """Return whether exact validated data was already claimed."""
        digest = self._digest(raw_init_data)
        now = self._clock()
        with self._guard:
            self._purge_locked(now)
            return digest in self._digests

    def release(self, raw_init_data: str) -> None:
        """Release a claim when session creation fails before a response exists."""
        digest = self._digest(raw_init_data)
        with self._guard:
            self._digests.pop(digest, None)

    def _purge_locked(self, now: float) -> None:
        expired = [
            value for value, seen_at in self._digests.items() if now - seen_at >= self._ttl
        ]
        for value in expired:
            self._digests.pop(value, None)

    @staticmethod
    def _digest(raw_init_data: str) -> bytes:
        return hashlib.sha256(raw_init_data.encode("utf-8")).digest()


def _json_object(value: str | None, label: str) -> dict[str, Any]:
    if value is None or not value or len(value) > 8192:
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise MiniAppAuthenticationError("Invalid Telegram authorization.") from error
    if not isinstance(parsed, dict):
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    return parsed


def _integer(value: str | None, label: str) -> int:
    if value is None or not value.isascii() or not value.isdecimal():
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    try:
        parsed = int(value)
    except ValueError as error:
        raise MiniAppAuthenticationError("Invalid Telegram authorization.") from error
    if parsed < 0:
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    return parsed


def _telegram_id(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    if not 1 <= value <= MAX_TELEGRAM_ID:
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    return value


def _signed_telegram_id(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    if value == 0 or abs(value) > MAX_TELEGRAM_ID:
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    return value


def _optional_text(value: str | None, maximum: int) -> str | None:
    if value is None:
        return None
    if not value or len(value) > maximum or any(ord(character) < 32 for character in value):
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    return value


def _optional_json_text(value: object, maximum: int) -> str | None:
    """Keep signed Telegram profile text bounded and inert for private audit display."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    result = value.strip()
    if (
        not result
        or len(result) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
    ):
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    return result


def _chat_type(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in {"sender", "private", "group", "supergroup", "channel"}:
        raise MiniAppAuthenticationError("Invalid Telegram authorization.")
    return value
