"""Strict GetBible Query API client for authoritative verse review text.

The contribution moderator must never make a decision from guessed, partial, or
client-supplied scripture text.  This module therefore treats every Query API
response as untrusted input and returns a batch only when every requested verse
has been validated.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any, Protocol, TypeAlias
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

QUERY_API_BASE_URL = "https://query.getbible.net/v2"

_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_MAX_REFERENCE_COMPONENT = 999
_MAX_DISPLAY_REFERENCE_LENGTH = 256
_MAX_VERSE_TEXT_LENGTH = 16_384
_DEFAULT_TIMEOUT_SECONDS = 8.0
_DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
_DEFAULT_BATCH_SIZE = 40
_DEFAULT_MAX_URL_LENGTH = 4096
_DEFAULT_MAX_REFERENCES = 5000


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


# Keep redirects visible to the status validator. Standard deployment proxy
# configuration remains available for hosts whose egress requires it.
_STRICT_OPENER = build_opener(_RejectRedirectHandler())


class GetBibleQueryError(RuntimeError):
    """Base error for failures that require contribution review to be deferred."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class QueryInputError(GetBibleQueryError):
    """The requested translation or verse coordinates are invalid."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class QueryTransportError(GetBibleQueryError):
    """The Query API could not be reached before the configured deadline."""

    def __init__(self) -> None:
        super().__init__(
            "The GetBible Query API could not be reached; defer this review.",
            retryable=True,
        )


class QueryHTTPError(GetBibleQueryError):
    """The Query API returned a non-successful HTTP status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        retryable = status_code in {408, 425, 429} or 500 <= status_code <= 599
        super().__init__(
            f"The GetBible Query API returned HTTP {status_code}; defer this review.",
            retryable=retryable,
        )


class QueryResponseError(GetBibleQueryError):
    """The Query API response was unsafe, malformed, or internally inconsistent."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class MissingVerseError(QueryResponseError):
    """One or more requested verses were absent from an otherwise valid response."""

    def __init__(self, missing: Iterable[VerseReference]) -> None:
        self.missing = tuple(missing)
        references = ", ".join(reference.canonical for reference in self.missing)
        super().__init__(
            f"The GetBible Query API omitted requested verse(s): {references}."
        )


@dataclass(frozen=True, order=True, slots=True)
class VerseReference:
    """A canonical numeric GetBible verse coordinate."""

    book: int
    chapter: int
    verse: int

    def __post_init__(self) -> None:
        for label, value in (
            ("book", self.book),
            ("chapter", self.chapter),
            ("verse", self.verse),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= _MAX_REFERENCE_COMPONENT
            ):
                raise QueryInputError(
                    f"Verse {label} must be an integer between 1 and "
                    f"{_MAX_REFERENCE_COMPONENT}."
                )

    @property
    def canonical(self) -> str:
        """Return the numeric reference syntax required by the Query API."""
        return f"{self.book} {self.chapter}:{self.verse}"


@dataclass(frozen=True, slots=True)
class AuthoritativeVerse:
    """Validated scripture text and display metadata returned by GetBible."""

    reference: VerseReference
    display_reference: str
    text: str
    translation: str
    translation_name: str
    book_name: str


ReferenceTuple: TypeAlias = tuple[int, int, int]
ReferenceInput: TypeAlias = VerseReference | ReferenceTuple


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


class GetBibleQueryClient:
    """Fetch complete, bounded batches of authoritative verse text.

    The client uses only numeric book references, preserves the caller's first
    occurrence order and deduplicates repeated coordinates.  It intentionally
    does not retain scripture between calls: persistent GetBible caches require
    scope hashes and revalidation, while this moderation client only needs
    short-lived review text.  It never returns partial results.
    """

    def __init__(
        self,
        *,
        translation: str = "kjv",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        max_url_length: int = _DEFAULT_MAX_URL_LENGTH,
        max_references: int = _DEFAULT_MAX_REFERENCES,
        opener: _Opener | None = None,
    ) -> None:
        if not isinstance(translation, str):
            raise QueryInputError("Translation must be a GetBible abbreviation.")
        normalized_translation = translation.strip().lower()
        if not _TRANSLATION_RE.fullmatch(normalized_translation):
            raise QueryInputError(
                "Translation must be a lowercase GetBible abbreviation containing "
                "only letters, numbers, periods, underscores, or hyphens."
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise QueryInputError("Query timeout must be a positive finite number.")
        for label, value, minimum in (
            ("maximum response size", max_response_bytes, 1),
            ("batch size", batch_size, 1),
            ("maximum URL length", max_url_length, 128),
            ("maximum reference count", max_references, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise QueryInputError(
                    f"Query {label} must be an integer of at least {minimum}."
                )

        self.translation = normalized_translation
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes
        self.batch_size = batch_size
        self.max_url_length = max_url_length
        self.max_references = max_references
        self._opener: _Opener = opener if opener is not None else _STRICT_OPENER.open

    def fetch_verses(
        self,
        references: Iterable[ReferenceInput],
    ) -> dict[VerseReference, AuthoritativeVerse]:
        """Return a complete mapping of coordinates to validated verse text.

        ``references`` may contain :class:`VerseReference` objects or numeric
        ``(book, chapter, verse)`` tuples.  Every operational failure raises a
        :class:`GetBibleQueryError`; moderators must defer rather than approve a
        contribution when that occurs.
        """
        requested = self._normalize_references(references)
        if not requested:
            return {}

        resolved: dict[VerseReference, AuthoritativeVerse] = {}
        for batch in self._batches(requested):
            fetched = self._fetch_batch(batch)
            resolved.update(fetched)

        # Construct a fresh insertion-ordered mapping in caller order.  The
        # indexing is safe because each batch is rejected if anything is absent.
        return {reference: resolved[reference] for reference in requested}

    def _normalize_references(
        self,
        references: Iterable[ReferenceInput],
    ) -> list[VerseReference]:
        if isinstance(references, (str, bytes)):
            raise QueryInputError("Verse references must be an iterable of coordinates.")

        normalized: list[VerseReference] = []
        seen: set[VerseReference] = set()
        try:
            iterator = iter(references)
        except TypeError as error:
            raise QueryInputError("Verse references must be iterable.") from error

        for item in iterator:
            reference = self._coerce_reference(item)
            if reference in seen:
                continue
            if len(normalized) >= self.max_references:
                raise QueryInputError(
                    f"A Query API call is limited to {self.max_references} unique verses."
                )
            seen.add(reference)
            normalized.append(reference)
        return normalized

    @staticmethod
    def _coerce_reference(item: object) -> VerseReference:
        if isinstance(item, VerseReference):
            return item
        if isinstance(item, tuple) and len(item) == 3:
            return VerseReference(item[0], item[1], item[2])
        raise QueryInputError(
            "Each verse reference must be VerseReference or a "
            "(book, chapter, verse) tuple."
        )

    def _batches(
        self,
        references: Iterable[VerseReference],
    ) -> Iterable[tuple[VerseReference, ...]]:
        batch: list[VerseReference] = []
        for reference in references:
            candidate = (*batch, reference)
            if batch and (
                len(candidate) > self.batch_size
                or len(self._build_url(candidate)) > self.max_url_length
            ):
                yield tuple(batch)
                batch = [reference]
            else:
                batch.append(reference)

            if len(self._build_url(batch)) > self.max_url_length:
                raise QueryInputError(
                    "A verse reference exceeds the configured Query API URL limit."
                )
        if batch:
            yield tuple(batch)

    def _build_url(self, references: Iterable[VerseReference]) -> str:
        query = "; ".join(reference.canonical for reference in references)
        # Preserve Query API reference punctuation exactly as the official
        # GetBible MCP client does while still encoding path separators,
        # whitespace, and every other unsafe character.
        encoded_query = quote(query, safe=":;,-")
        encoded_translation = quote(self.translation, safe="")
        return f"{QUERY_API_BASE_URL}/{encoded_translation}/{encoded_query}"

    def _fetch_batch(
        self,
        references: tuple[VerseReference, ...],
    ) -> dict[VerseReference, AuthoritativeVerse]:
        request = Request(
            self._build_url(references),
            headers={
                "Accept": "application/json",
                "User-Agent": "GetBible-Robot-Contribution-Review/2",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                status = response.getcode()
                if isinstance(status, bool) or not isinstance(status, int):
                    raise QueryResponseError(
                        "The GetBible Query API returned an invalid HTTP status."
                    )
                if status != 200:
                    raise QueryHTTPError(status)
                self._validate_content_type(response.headers)
                self._validate_content_length(response.headers)
                payload = response.read(self.max_response_bytes + 1)
        except HTTPError as error:
            error.close()
            raise QueryHTTPError(error.code) from None
        except (TimeoutError, URLError, OSError, HTTPException):
            raise QueryTransportError() from None

        if not isinstance(payload, bytes):
            raise QueryResponseError(
                "The GetBible Query API returned a non-binary HTTP response."
            )
        if len(payload) > self.max_response_bytes:
            raise QueryResponseError(
                "The GetBible Query API response exceeded the configured size limit."
            )
        try:
            decoded = payload.decode("utf-8")
            document = json.loads(decoded, parse_constant=self._reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise QueryResponseError(
                "The GetBible Query API returned malformed JSON."
            ) from None
        return self._parse_document(document, references)

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"Non-standard JSON constant: {value}")

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        value = headers.get(name)
        if value is not None:
            return value
        # Ordinary mappings used by tests are case-sensitive, while HTTPMessage
        # is not.  Keep validation reliable for either implementation.
        wanted = name.casefold()
        for key, candidate in headers.items():
            if key.casefold() == wanted:
                return candidate
        return None

    def _validate_content_type(self, headers: Mapping[str, str]) -> None:
        raw_content_type = self._header(headers, "Content-Type")
        if raw_content_type is None:
            raise QueryResponseError(
                "The GetBible Query API response did not declare JSON content."
            )
        media_type = raw_content_type.partition(";")[0].strip().casefold()
        if media_type != "application/json" and not media_type.endswith("+json"):
            raise QueryResponseError(
                "The GetBible Query API response did not declare JSON content."
            )

    def _validate_content_length(self, headers: Mapping[str, str]) -> None:
        raw_content_length = self._header(headers, "Content-Length")
        if raw_content_length is None:
            return
        try:
            content_length = int(raw_content_length, 10)
        except (TypeError, ValueError):
            raise QueryResponseError(
                "The GetBible Query API returned an invalid Content-Length header."
            ) from None
        if content_length < 0:
            raise QueryResponseError(
                "The GetBible Query API returned an invalid Content-Length header."
            )
        if content_length > self.max_response_bytes:
            raise QueryResponseError(
                "The GetBible Query API response exceeded the configured size limit."
            )

    def _parse_document(
        self,
        document: Any,
        requested: tuple[VerseReference, ...],
    ) -> dict[VerseReference, AuthoritativeVerse]:
        if not isinstance(document, dict) or not document:
            raise QueryResponseError(
                "The GetBible Query API response was not a non-empty object."
            )

        expected = set(requested)
        resolved: dict[VerseReference, AuthoritativeVerse] = {}
        for chapter_payload in document.values():
            if not isinstance(chapter_payload, dict):
                raise QueryResponseError(
                    "The GetBible Query API returned an invalid chapter object."
                )
            abbreviation = self._required_text(
                chapter_payload,
                "abbreviation",
                maximum=64,
            ).casefold()
            if abbreviation != self.translation:
                raise QueryResponseError(
                    "The GetBible Query API returned a different translation."
                )
            translation_name = self._required_text(
                chapter_payload,
                "translation",
                maximum=256,
            )
            book_name = self._required_text(
                chapter_payload,
                "book_name",
                maximum=256,
            )
            book = self._required_integer(chapter_payload, "book_nr")
            chapter = self._required_integer(chapter_payload, "chapter")
            verses = chapter_payload.get("verses")
            if not isinstance(verses, list) or not verses:
                raise QueryResponseError(
                    "The GetBible Query API returned an invalid verse list."
                )
            if len(verses) > len(requested):
                raise QueryResponseError(
                    "The GetBible Query API returned more verses than requested."
                )

            for verse_payload in verses:
                if not isinstance(verse_payload, dict):
                    raise QueryResponseError(
                        "The GetBible Query API returned an invalid verse object."
                    )
                verse_chapter = self._required_integer(verse_payload, "chapter")
                verse = self._required_integer(verse_payload, "verse")
                if verse_chapter != chapter:
                    raise QueryResponseError(
                        "The GetBible Query API returned inconsistent chapter metadata."
                    )
                try:
                    reference = VerseReference(book, chapter, verse)
                except QueryInputError:
                    raise QueryResponseError(
                        "The GetBible Query API returned invalid verse coordinates."
                    ) from None
                if reference not in expected:
                    raise QueryResponseError(
                        "The GetBible Query API returned an unrequested verse."
                    )
                if reference in resolved:
                    raise QueryResponseError(
                        "The GetBible Query API returned a duplicate verse."
                    )
                display_reference = self._required_text(
                    verse_payload,
                    "name",
                    maximum=_MAX_DISPLAY_REFERENCE_LENGTH,
                )
                if display_reference != f"{book_name} {chapter}:{verse}":
                    raise QueryResponseError(
                        "The GetBible Query API returned inconsistent verse naming."
                    )
                text = self._required_text(
                    verse_payload,
                    "text",
                    maximum=_MAX_VERSE_TEXT_LENGTH,
                )
                resolved[reference] = AuthoritativeVerse(
                    reference=reference,
                    display_reference=display_reference,
                    text=text,
                    translation=self.translation,
                    translation_name=translation_name,
                    book_name=book_name,
                )

        missing = [reference for reference in requested if reference not in resolved]
        if missing:
            raise MissingVerseError(missing)
        return resolved

    @staticmethod
    def _required_integer(payload: Mapping[str, Any], field: str) -> int:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise QueryResponseError(
                f"The GetBible Query API returned invalid {field} metadata."
            )
        return value

    @staticmethod
    def _required_text(
        payload: Mapping[str, Any],
        field: str,
        *,
        maximum: int,
    ) -> str:
        value = payload.get(field)
        if not isinstance(value, str):
            raise QueryResponseError(
                f"The GetBible Query API returned invalid {field} metadata."
            )
        if any(
            unicodedata.category(character) in {"Cc", "Cs"}
            and not character.isspace()
            for character in value
        ):
            raise QueryResponseError(
                f"The GetBible Query API returned unsafe {field} metadata."
            )
        normalized = " ".join(value.split())
        if not normalized or len(normalized) > maximum:
            raise QueryResponseError(
                f"The GetBible Query API returned invalid {field} metadata."
            )
        return normalized

__all__ = [
    "AuthoritativeVerse",
    "GetBibleQueryClient",
    "GetBibleQueryError",
    "MissingVerseError",
    "QueryHTTPError",
    "QueryInputError",
    "QueryResponseError",
    "QueryTransportError",
    "VerseReference",
]
