"""Bounded, portable bookmark documents exchanged through Telegram."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MAX_BOOKMARK_BACKUP_BYTES = 4 * 1024 * 1024
# The authenticated JSON envelope adds only an idempotency key and field names.
MAX_BOOKMARK_BACKUP_REQUEST_BYTES = MAX_BOOKMARK_BACKUP_BYTES + 4096
MAX_BOOKMARK_TOPICS = 100
MAX_BOOKMARK_MARKINGS = 800
MAX_BOOKMARK_TOPICS_PER_MARKING = 100
BOOKMARK_RESTORE_CALLBACK_PREFIX = "gbr:"

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")
_COLOR_RE = re.compile(r"#[A-Fa-f0-9]{6}\Z")
_CALLBACK_RE = re.compile(r"gbr:([0-9]{1,16}):([A-Za-z0-9_-]{16})\Z")
_MAX_TELEGRAM_USER_ID = (2**52) - 1


class BookmarkBackupError(ValueError):
    """A bookmark document is malformed, unsupported, or outside its bounds."""


class BookmarkBackupUnavailable(RuntimeError):
    """Telegram could not currently store or retrieve a bookmark document."""


@dataclass(frozen=True, slots=True)
class BookmarkBackupDocument:
    """A validated canonical JSON document safe to hand to Telegram."""

    payload: bytes
    value: dict[str, Any]
    filename: str
    topic_count: int
    bookmark_count: int


@dataclass(frozen=True, slots=True)
class BookmarkRestoreFile:
    """A small Telegram-owned file reference retained by one Mini App launch."""

    file_id: str
    file_unique_id: str
    file_name: str
    file_size: int

    @classmethod
    def validated(
        cls,
        *,
        file_id: object,
        file_unique_id: object,
        file_name: object,
        file_size: object,
    ) -> BookmarkRestoreFile:
        identifier = _bounded_text(file_id, "file_id", 512)
        unique_identifier = _bounded_text(
            file_unique_id,
            "file_unique_id",
            256,
        )
        name = _bounded_text(file_name, "file_name", 255)
        if not name.casefold().endswith(".json"):
            raise BookmarkBackupError("The bookmark backup must be a JSON document.")
        size = _bounded_integer(file_size, "file_size", 1, MAX_BOOKMARK_BACKUP_BYTES)
        return cls(identifier, unique_identifier, name, size)


def parse_bookmark_backup_bytes(value: bytes | bytearray) -> BookmarkBackupDocument:
    """Decode, validate, and canonicalize one portable GetBible JSON backup."""
    payload = bytes(value)
    if not payload or len(payload) > MAX_BOOKMARK_BACKUP_BYTES:
        raise BookmarkBackupError("The bookmark backup is empty or too large.")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise BookmarkBackupError("The bookmark backup must be valid UTF-8 JSON.") from error
    return bookmark_backup_document(decoded)


def bookmark_backup_document(value: object) -> BookmarkBackupDocument:
    """Validate one decoded GetBible markings document and bound every field."""
    if not isinstance(value, dict):
        raise BookmarkBackupError("The bookmark backup must be a JSON object.")
    if (
        "format" in value
        and value.get("format") != "getbible-life-markings"
    ):
        raise BookmarkBackupError("The bookmark backup format is unsupported.")
    version = value.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in {1, 2, 3, 4}
    ):
        raise BookmarkBackupError("The bookmark backup version is unsupported.")
    exported_at = _iso_timestamp(value.get("exportedAt"))

    colors = value.get("colors")
    markings = value.get("markings")
    notes = value.get("notes", [])
    if (
        not isinstance(colors, list)
        or not 1 <= len(colors) <= MAX_BOOKMARK_TOPICS
        or not isinstance(markings, list)
        or len(markings) > MAX_BOOKMARK_MARKINGS
        or not isinstance(notes, list)
        or len(notes) > MAX_BOOKMARK_MARKINGS
    ):
        raise BookmarkBackupError("The bookmark backup collections are invalid.")

    topic_ids: set[str] = set()
    for color in colors:
        if not isinstance(color, dict):
            raise BookmarkBackupError("A bookmark topic is invalid.")
        topic_id = _bounded_text(color.get("id"), "topic id", 128)
        _bounded_text(color.get("name"), "topic name", 80)
        color_value = _bounded_text(color.get("value"), "topic color", 7)
        if (
            _ID_RE.fullmatch(topic_id) is None
            or _COLOR_RE.fullmatch(color_value) is None
            or topic_id in topic_ids
        ):
            raise BookmarkBackupError("A bookmark topic is invalid.")
        topic_ids.add(topic_id)

    bookmark_count = 0
    marking_ids: set[str] = set()
    for marking in markings:
        if not isinstance(marking, dict):
            raise BookmarkBackupError("A bookmark entry is invalid.")
        marking_id = _bounded_text(marking.get("id"), "bookmark id", 128)
        passage = marking.get("passage")
        if (
            _ID_RE.fullmatch(marking_id) is None
            or marking_id in marking_ids
            or not isinstance(passage, dict)
        ):
            raise BookmarkBackupError("A bookmark entry is invalid.")
        marking_ids.add(marking_id)
        translation = _bounded_text(
            passage.get("translation"),
            "translation",
            30,
        ).casefold()
        if _TRANSLATION_RE.fullmatch(translation) is None:
            raise BookmarkBackupError("A bookmark translation is invalid.")
        _bounded_integer(passage.get("book"), "book", 1, 200)
        _bounded_integer(passage.get("chapter"), "chapter", 1, 1000)
        _bounded_integer(marking.get("verse"), "verse", 1, 2000)
        _bounded_text(marking.get("quote"), "bookmark quote", 3000)
        if version == 4:
            if "reference" in marking or "bookName" not in marking:
                raise BookmarkBackupError("A bookmark reference is invalid.")
            _bounded_text(marking.get("bookName"), "bookmark book name", 128)
        else:
            if "bookName" in marking or "reference" not in marking:
                raise BookmarkBackupError("A bookmark reference is invalid.")
            _bounded_text(marking.get("reference"), "bookmark reference", 180)
        if version == 4:
            if "colorId" in marking or "colorIds" in marking:
                raise BookmarkBackupError("A bookmark entry has invalid topics.")
            color_indexes = marking.get("colorIndexes")
            if (
                not isinstance(color_indexes, list)
                or not 1 <= len(color_indexes) <= MAX_BOOKMARK_TOPICS_PER_MARKING
            ):
                raise BookmarkBackupError("A bookmark entry has invalid topics.")
            marking_topic_indexes: set[int] = set()
            for color_index_value in color_indexes:
                color_index = _bounded_integer(
                    color_index_value,
                    "bookmark topic index",
                    0,
                    len(colors) - 1,
                )
                if color_index in marking_topic_indexes:
                    raise BookmarkBackupError(
                        "A bookmark entry has duplicate topics."
                    )
                marking_topic_indexes.add(color_index)
        elif version == 3:
            if "colorId" in marking or "colorIndexes" in marking:
                raise BookmarkBackupError("A bookmark entry has invalid topics.")
            color_ids = marking.get("colorIds")
            if (
                not isinstance(color_ids, list)
                or not 1 <= len(color_ids) <= MAX_BOOKMARK_TOPICS_PER_MARKING
            ):
                raise BookmarkBackupError("A bookmark entry has invalid topics.")
            marking_topic_ids: set[str] = set()
            for color_id_value in color_ids:
                color_id = _bounded_text(
                    color_id_value,
                    "bookmark topic",
                    128,
                )
                if color_id not in topic_ids:
                    raise BookmarkBackupError(
                        "A bookmark entry uses an unknown topic."
                    )
                if color_id in marking_topic_ids:
                    raise BookmarkBackupError(
                        "A bookmark entry has duplicate topics."
                    )
                marking_topic_ids.add(color_id)
        else:
            if "colorIds" in marking or "colorIndexes" in marking:
                raise BookmarkBackupError("A bookmark entry has invalid topics.")
            color_id = _bounded_text(
                marking.get("colorId"),
                "bookmark topic",
                128,
            )
            if color_id not in topic_ids:
                raise BookmarkBackupError(
                    "A bookmark entry uses an unknown topic."
                )
        _bounded_integer(
            marking.get("createdAt"),
            "bookmark timestamp",
            0,
            (2**53) - 1,
        )
        start = marking.get("start")
        end = marking.get("end")
        if start is None and end is None:
            bookmark_count += 1
        elif (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end <= 100_000
        ):
            pass
        else:
            raise BookmarkBackupError("A bookmark text range is invalid.")

    # Notes are intentionally opaque to Robot, but each value must remain a
    # JSON object and the complete canonical file stays under the same cap.
    if any(not isinstance(note, dict) for note in notes):
        raise BookmarkBackupError("A bookmark note is invalid.")
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as error:
        raise BookmarkBackupError("The bookmark backup contains invalid JSON values.") from error
    if len(canonical) > MAX_BOOKMARK_BACKUP_BYTES:
        raise BookmarkBackupError("The bookmark backup is too large.")
    filename = f"getbible-bookmarks-{exported_at:%Y%m%d-%H%M%SZ}.json"
    return BookmarkBackupDocument(
        payload=canonical,
        value=value,
        filename=filename,
        topic_count=len(colors),
        bookmark_count=bookmark_count,
    )


def bookmark_restore_callback_data(user_id: int, secret: str) -> str:
    """Build a compact durable callback bound to one Telegram account."""
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or not 1 <= user_id <= _MAX_TELEGRAM_USER_ID
        or not isinstance(secret, str)
        or not secret
    ):
        raise ValueError("Bookmark restore callback identity is invalid.")
    owner = str(user_id)
    signature = base64.urlsafe_b64encode(
        hmac.new(
            secret.encode("utf-8"),
            f"getbible-bookmark-restore:v1:{owner}".encode(),
            hashlib.sha256,
        ).digest()[:12]
    ).decode("ascii").rstrip("=")
    return f"{BOOKMARK_RESTORE_CALLBACK_PREFIX}{owner}:{signature}"


def valid_bookmark_restore_callback(
    value: object,
    *,
    user_id: int,
    secret: str,
) -> bool:
    """Verify that a durable document button belongs to the current user."""
    if not isinstance(value, str) or _CALLBACK_RE.fullmatch(value) is None:
        return False
    try:
        expected = bookmark_restore_callback_data(user_id, secret)
    except ValueError:
        return False
    return hmac.compare_digest(value, expected)


def _iso_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise BookmarkBackupError("The bookmark backup date is invalid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BookmarkBackupError("The bookmark backup date is invalid.") from error
    if parsed.tzinfo is None:
        raise BookmarkBackupError("The bookmark backup date must include a timezone.")
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise BookmarkBackupError("The bookmark backup date is invalid.") from error


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise BookmarkBackupError(f"The bookmark backup {label} is invalid.")
    result = value.strip()
    if not result or len(result) > maximum or "\x00" in result:
        raise BookmarkBackupError(f"The bookmark backup {label} is invalid.")
    return result


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
        raise BookmarkBackupError(f"The bookmark backup {label} is invalid.")
    return value
