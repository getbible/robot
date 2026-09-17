"""Strict client for the public GetBible Bookmarks API (``bookmarks.getbible.net``).

The Bookmarks API is a static, read-only tree of JSON documents published by
``getbible/v1_bookmark_builder``.  It is the single source of truth for the
shared topic catalogue: the robot never ships or overlays a copy of it, so the
only questions this client answers are "which catalogue version is published"
and "what does it contain".  Every document is treated as untrusted input and
returned only after its transport, size, checksum and shape have been checked.

``catalog.json`` is verified against the SHA-256 the API publishes for it in
``checksums.json``; ``index.json`` carries the catalogue version and the
checksum of ``all.json`` that consumers use to notice a change cheaply.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any, Protocol, TypeAlias
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .bible_canon import is_canonical_coordinate

BOOKMARKS_API_BASE_URL = "https://bookmarks.getbible.net/v1"

MAX_TOPICS = 1_000
MAX_LINKS = 100_000
MAX_ALIASES = 20
MAX_LOCALES = 500
TOPIC_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
ENGLISH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 &'():?-]*[A-Za-z0-9)]\Z")
COLOR_RE = re.compile(r"#[0-9a-f]{6}\Z")
LOCALE_RE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*\Z")
CHECKSUM_RE = re.compile(r"[0-9a-f]{64}\Z")
_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_INDEX_BYTES = 64 * 1024
_MAX_CHECKSUMS_BYTES = 256 * 1024
_USER_AGENT = "getbible-robot/2.2"


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Keep an upstream redirect visible as a non-200 response."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


_STRICT_OPENER = build_opener(_RejectRedirectHandler())


class GetBibleBookmarksError(RuntimeError):
    """Base error for a catalogue document that could not be obtained."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class BookmarksTransportError(GetBibleBookmarksError):
    """The Bookmarks API could not be reached before the deadline."""

    def __init__(self) -> None:
        super().__init__("The GetBible Bookmarks API could not be reached.", retryable=True)


class BookmarksHTTPError(GetBibleBookmarksError):
    """The Bookmarks API answered with a non-successful status."""

    def __init__(self, status_code: int, path: str) -> None:
        self.status_code = status_code
        self.path = path
        super().__init__(
            f"The GetBible Bookmarks API returned HTTP {status_code} for {path}.",
            retryable=status_code in {408, 425, 429} or 500 <= status_code <= 599,
        )


class BookmarksResponseError(GetBibleBookmarksError):
    """A document was malformed, oversized, or failed its checksum."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


@dataclass(frozen=True, slots=True)
class BookmarksIndex:
    """The discovery document: version, content checksum and counts."""

    catalog_version: int
    checksum: str
    topics: int
    verses: int
    locales: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BookmarkTopic:
    """One published topic with its translation-independent coordinates."""

    id: str
    name: str
    color: str
    aliases: tuple[str, ...]
    default: bool
    verses: tuple[tuple[int, int, int], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "aliases": list(self.aliases),
            "default": self.default,
            "verses": [list(triple) for triple in self.verses],
        }


@dataclass(frozen=True, slots=True)
class BookmarksCatalog:
    """The validated ``catalog.json`` document."""

    checksum: str
    topics: tuple[BookmarkTopic, ...]
    document: bytes

    @property
    def topic_ids(self) -> frozenset[str]:
        return frozenset(topic.id for topic in self.topics)

    @property
    def associations(self) -> frozenset[tuple[str, int, int, int]]:
        return frozenset(
            (topic.id, book, chapter, verse)
            for topic in self.topics
            for book, chapter, verse in topic.verses
        )

    @property
    def link_count(self) -> int:
        return sum(len(topic.verses) for topic in self.topics)


class _HTTPResponse(Protocol):
    headers: Mapping[str, str]

    def getcode(self) -> int | None: ...

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> _HTTPResponse: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None: ...


_Opener: TypeAlias = Callable[..., _HTTPResponse]


class GetBibleBookmarksClient:
    """Read and verify the published catalogue documents."""

    def __init__(
        self,
        *,
        base_url: str = BOOKMARKS_API_BASE_URL,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        opener: _Opener | None = None,
    ) -> None:
        if (
            not isinstance(base_url, str)
            or not base_url.startswith("https://")
            or "?" in base_url
            or "#" in base_url
        ):
            raise BookmarksResponseError("Bookmarks API base URL must be an https URL.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise BookmarksResponseError("Bookmarks timeout must be a positive finite number.")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 64 * 1024
        ):
            raise BookmarksResponseError("Bookmarks response bound must be at least 64 KiB.")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes
        self._opener: _Opener = opener if opener is not None else _STRICT_OPENER.open

    def index(self) -> BookmarksIndex:
        """Return the discovery document."""
        document = _decode_json(self._get("index.json", _MAX_INDEX_BYTES))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise BookmarksResponseError("The Bookmarks index has an unsupported schema.")
        version = document.get("catalog_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise BookmarksResponseError("The Bookmarks index catalogue version is invalid.")
        checksum = document.get("checksum")
        if not isinstance(checksum, str) or CHECKSUM_RE.fullmatch(checksum) is None:
            raise BookmarksResponseError("The Bookmarks index checksum is invalid.")
        counts = document.get("counts")
        if not isinstance(counts, dict):
            raise BookmarksResponseError("The Bookmarks index counts are invalid.")
        totals: dict[str, int] = {}
        bounds = (("topics", MAX_TOPICS), ("verses", MAX_LINKS), ("locales", MAX_LOCALES))
        for field, maximum in bounds:
            value = counts.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise BookmarksResponseError(f"The Bookmarks index {field} count is invalid.")
            totals[field] = value
        raw_locales = document.get("locales")
        if (
            not isinstance(raw_locales, list)
            or len(raw_locales) > MAX_LOCALES
            or any(
                not isinstance(code, str) or len(code) > 16 or LOCALE_RE.fullmatch(code) is None
                for code in raw_locales
            )
        ):
            raise BookmarksResponseError("The Bookmarks index locale list is invalid.")
        return BookmarksIndex(
            catalog_version=version,
            checksum=checksum,
            topics=totals["topics"],
            verses=totals["verses"],
            locales=tuple(raw_locales),
        )

    def checksums(self) -> dict[str, str]:
        """Return the published SHA-256 of every generated file."""
        document = _decode_json(self._get("checksums.json", _MAX_CHECKSUMS_BYTES))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise BookmarksResponseError("The Bookmarks checksum manifest is unsupported.")
        files = document.get("files")
        if not isinstance(files, dict) or not files:
            raise BookmarksResponseError("The Bookmarks checksum manifest is empty.")
        result: dict[str, str] = {}
        for path, digest in files.items():
            if (
                not isinstance(path, str)
                or not path
                or len(path) > 256
                or not isinstance(digest, str)
                or CHECKSUM_RE.fullmatch(digest) is None
            ):
                raise BookmarksResponseError("The Bookmarks checksum manifest is malformed.")
            result[path] = digest
        return result

    def fetch_catalog_json(self) -> bytes:
        """Return the exact ``catalog.json`` bytes after verifying their checksum."""
        expected = self.checksums().get("catalog.json")
        if expected is None:
            raise BookmarksResponseError("The Bookmarks checksum manifest omits catalog.json.")
        payload = self._get("catalog.json", self.max_response_bytes)
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise BookmarksResponseError("The Bookmarks catalogue did not match its checksum.")
        return payload

    def catalog(self) -> BookmarksCatalog:
        """Return the validated catalogue."""
        payload = self.fetch_catalog_json()
        return parse_catalog_document(payload)

    def _get(self, relative_path: str, maximum_bytes: int) -> bytes:
        request = Request(
            f"{self.base_url}/{relative_path}",
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            method="GET",
        )
        limit = min(maximum_bytes, self.max_response_bytes)
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                status = response.getcode()
                if isinstance(status, bool) or not isinstance(status, int):
                    raise BookmarksResponseError(
                        "The GetBible Bookmarks API returned an invalid HTTP status."
                    )
                if status != 200:
                    raise BookmarksHTTPError(status, relative_path)
                content_length = _header(response.headers, "Content-Length")
                if content_length is not None:
                    try:
                        announced = int(content_length, 10)
                    except ValueError:
                        raise BookmarksResponseError(
                            "The GetBible Bookmarks API returned an invalid Content-Length."
                        ) from None
                    if announced < 0 or announced > limit:
                        raise BookmarksResponseError(
                            f"The GetBible Bookmarks API document {relative_path} is too large."
                        )
                payload = response.read(limit + 1)
        except HTTPError as error:
            status_code = error.code
            error.close()
            raise BookmarksHTTPError(status_code, relative_path) from None
        except (TimeoutError, URLError, OSError, HTTPException):
            raise BookmarksTransportError() from None
        if not isinstance(payload, bytes):
            raise BookmarksResponseError(
                "The GetBible Bookmarks API returned a non-binary HTTP response."
            )
        if len(payload) > limit:
            raise BookmarksResponseError(
                f"The GetBible Bookmarks API document {relative_path} is too large."
            )
        return payload


def parse_catalog_document(payload: bytes) -> BookmarksCatalog:
    """Validate ``catalog.json`` bytes into a :class:`BookmarksCatalog`."""
    if not isinstance(payload, bytes):
        raise BookmarksResponseError("The Bookmarks catalogue must be raw bytes.")
    document = _decode_json(payload)
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise BookmarksResponseError("The Bookmarks catalogue has an unsupported schema.")
    if set(document) != {"schema_version", "topics"}:
        raise BookmarksResponseError("The Bookmarks catalogue has unexpected members.")
    raw_topics = document.get("topics")
    if not isinstance(raw_topics, list) or len(raw_topics) > MAX_TOPICS:
        raise BookmarksResponseError("The Bookmarks catalogue topic list is invalid.")
    topics: list[BookmarkTopic] = []
    seen_ids: set[str] = set()
    seen_names: dict[str, str] = {}
    links = 0
    for index, raw in enumerate(raw_topics):
        topic = _parse_topic(raw, f"topics[{index}]")
        if topic.id in seen_ids:
            raise BookmarksResponseError(f"The Bookmarks catalogue repeats topic {topic.id!r}.")
        seen_ids.add(topic.id)
        for label in (topic.name, *topic.aliases):
            folded = label.casefold()
            owner = seen_names.get(folded)
            if owner is not None and owner != topic.id:
                raise BookmarksResponseError(
                    f"The Bookmarks catalogue reuses the name {label!r} across topics."
                )
            seen_names[folded] = topic.id
        links += len(topic.verses)
        if links > MAX_LINKS:
            raise BookmarksResponseError("The Bookmarks catalogue exceeds the link limit.")
        topics.append(topic)
    return BookmarksCatalog(
        checksum=hashlib.sha256(payload).hexdigest(),
        topics=tuple(topics),
        document=payload,
    )


def _parse_topic(raw: object, label: str) -> BookmarkTopic:
    if not isinstance(raw, dict):
        raise BookmarksResponseError(f"The Bookmarks catalogue {label} is not an object.")
    if set(raw) != {"id", "name", "color", "aliases", "default", "verses"}:
        raise BookmarksResponseError(f"The Bookmarks catalogue {label} has unexpected members.")
    topic_id = raw["id"]
    if (
        not isinstance(topic_id, str)
        or not 1 <= len(topic_id) <= 80
        or TOPIC_ID_RE.fullmatch(topic_id) is None
    ):
        raise BookmarksResponseError(f"The Bookmarks catalogue {label} id is invalid.")
    name = raw["name"]
    if not isinstance(name, str) or not 2 <= len(name) <= 80 or not _english_name(name):
        raise BookmarksResponseError(f"The Bookmarks catalogue {label} name is invalid.")
    color = raw["color"]
    if not isinstance(color, str) or COLOR_RE.fullmatch(color) is None:
        raise BookmarksResponseError(f"The Bookmarks catalogue {label} colour is invalid.")
    raw_aliases = raw["aliases"]
    if (
        not isinstance(raw_aliases, list)
        or len(raw_aliases) > MAX_ALIASES
        or any(
            not isinstance(alias, str) or not 2 <= len(alias) <= 80 or not _english_name(alias)
            for alias in raw_aliases
        )
        or len({alias.casefold() for alias in raw_aliases}) != len(raw_aliases)
    ):
        raise BookmarksResponseError(f"The Bookmarks catalogue {label} aliases are invalid.")
    default = raw["default"]
    if not isinstance(default, bool):
        raise BookmarksResponseError(f"The Bookmarks catalogue {label} default flag is invalid.")
    raw_verses = raw["verses"]
    if not isinstance(raw_verses, list) or len(raw_verses) > MAX_LINKS:
        raise BookmarksResponseError(f"The Bookmarks catalogue {label} verse list is invalid.")
    verses: list[tuple[int, int, int]] = []
    previous: tuple[int, int, int] | None = None
    for triple in raw_verses:
        if (
            not isinstance(triple, list)
            or len(triple) != 3
            or not is_canonical_coordinate(*triple)
        ):
            raise BookmarksResponseError(
                f"The Bookmarks catalogue {label} contains an invalid coordinate."
            )
        coordinate = (int(triple[0]), int(triple[1]), int(triple[2]))
        if previous is not None and coordinate <= previous:
            raise BookmarksResponseError(
                f"The Bookmarks catalogue {label} verses are not strictly ascending."
            )
        previous = coordinate
        verses.append(coordinate)
    return BookmarkTopic(
        id=topic_id,
        name=name,
        color=color,
        aliases=tuple(raw_aliases),
        default=default,
        verses=tuple(verses),
    )


def _english_name(value: str) -> bool:
    return ENGLISH_NAME_RE.fullmatch(value) is not None and "  " not in value


def _decode_json(payload: bytes) -> Any:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise BookmarksResponseError("The Bookmarks API returned invalid UTF-8.") from None
    if any(unicodedata.category(character) == "Cs" for character in text):
        raise BookmarksResponseError("The Bookmarks API returned unsafe text.")
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        raise BookmarksResponseError("The Bookmarks API returned malformed JSON.") from None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return value
    wanted = name.casefold()
    for key, candidate in headers.items():
        if key.casefold() == wanted:
            return candidate
    return None


__all__ = [
    "BOOKMARKS_API_BASE_URL",
    "BookmarkTopic",
    "BookmarksCatalog",
    "BookmarksHTTPError",
    "BookmarksIndex",
    "BookmarksResponseError",
    "BookmarksTransportError",
    "GetBibleBookmarksClient",
    "GetBibleBookmarksError",
    "parse_catalog_document",
]
