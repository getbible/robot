"""Strict client for the public GetBible Search API (``search.getbible.net``).

Full-text search is answered by the published search service, never by an
in-process index.  The robot only forwards a bounded query with the reader's
filters and validates the envelope that comes back.  Every response is treated
as untrusted input: the caller receives a decoded document only after the
transport, size, content type, and envelope shape have been checked, and every
failure is a typed error the service layer can classify for the reader.

The request form is the documented GET route ``/v2/{translation}?q=...`` with
the criteria as query parameters.  ``book`` and ``exclude`` repeat; every other
parameter appears once.  Filters that equal the service defaults are still sent
so the request is self-describing and does not depend on server configuration.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any, Protocol, TypeAlias
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

SEARCH_API_BASE_URL = "https://search.getbible.net/v2"

#: Published contract bounds.  A request outside them is refused locally so the
#: service never spends a round trip on a request the API documents as invalid.
MAX_QUERY_LENGTH = 500
MAX_LIMIT = 100
MAX_OFFSET = 10_000
MAX_BOOKS = 83
MAX_EXCLUSIONS = 32
MAX_EXCLUSION_LENGTH = 100
MAX_PROXIMITY = 100
WORDS = frozenset({"all", "any", "phrase"})
MATCHES = frozenset({"whole_word", "substring"})
SCOPES = frozenset({"bible", "old_testament", "new_testament", "deuterocanon"})
DIACRITICS = frozenset({"fold", "exact"})
SORTS = frozenset({"canonical", "relevance"})

_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_PROBLEM_BYTES = 16 * 1024
_MAX_PROBLEM_FIELD_LENGTH = 512
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


class GetBibleSearchError(RuntimeError):
    """Base error for a search that could not be answered."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class SearchInputError(GetBibleSearchError):
    """The query, criteria, or translation cannot be sent as a valid request."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class SearchTransportError(GetBibleSearchError):
    """The search service could not be reached before the deadline."""

    def __init__(self) -> None:
        super().__init__(
            "The GetBible Search API could not be reached.",
            retryable=True,
        )


class SearchHTTPError(GetBibleSearchError):
    """The search service answered with a problem document or another status."""

    def __init__(
        self,
        status_code: int,
        *,
        code: str = "",
        detail: str = "",
        retry_after: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.retry_after = retry_after
        retryable = status_code in {408, 425, 429} or 500 <= status_code <= 599
        super().__init__(
            f"The GetBible Search API returned HTTP {status_code}"
            f"{f' ({code})' if code else ''}.",
            retryable=retryable,
        )

    @property
    def translation_not_found(self) -> bool:
        return self.status_code == 404 and self.code == "translation_not_found"

    @property
    def invalid_request(self) -> bool:
        """True when the service rejected the request itself, not its own state."""
        if self.status_code == 400:
            return True
        return self.status_code == 404 and self.code not in {
            "translation_not_found",
            "unknown_version",
        }


class SearchResponseError(GetBibleSearchError):
    """The search service response was unsafe, malformed, or inconsistent."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    """The published search filters, validated before a request is built."""

    words: str = "all"
    match: str = "whole_word"
    case_sensitive: bool = False
    scope: str = "bible"
    books: tuple[int, ...] = ()
    diacritics: str = "fold"
    exclude: tuple[str, ...] = ()
    proximity: int | None = None
    sort: str = "canonical"
    limit: int = MAX_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        if self.words not in WORDS:
            raise SearchInputError("Search words mode is invalid.")
        if self.match not in MATCHES:
            raise SearchInputError("Search match mode is invalid.")
        if not isinstance(self.case_sensitive, bool):
            raise SearchInputError("Search case sensitivity must be boolean.")
        if self.scope not in SCOPES:
            raise SearchInputError("Search scope is invalid.")
        if self.diacritics not in DIACRITICS:
            raise SearchInputError("Search diacritics policy is invalid.")
        if self.sort not in SORTS:
            raise SearchInputError("Search sort order is invalid.")
        if (
            not isinstance(self.books, tuple)
            or len(self.books) > MAX_BOOKS
            or len(set(self.books)) != len(self.books)
            or any(
                isinstance(book, bool) or not isinstance(book, int) or not 1 <= book <= 1000
                for book in self.books
            )
        ):
            raise SearchInputError("Search book selection is invalid.")
        if (
            not isinstance(self.exclude, tuple)
            or len(self.exclude) > MAX_EXCLUSIONS
            or any(
                not isinstance(term, str)
                or not 1 <= len(term) <= MAX_EXCLUSION_LENGTH
                or term != term.strip()
                or _has_unsafe_characters(term)
                for term in self.exclude
            )
        ):
            raise SearchInputError("Search exclusions are invalid.")
        if self.proximity is not None:
            if (
                isinstance(self.proximity, bool)
                or not isinstance(self.proximity, int)
                or not 0 <= self.proximity <= MAX_PROXIMITY
            ):
                raise SearchInputError("Search proximity is invalid.")
            if self.words != "all":
                raise SearchInputError("Search proximity requires the all-words mode.")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_LIMIT
        ):
            raise SearchInputError("Search limit is invalid.")
        if (
            isinstance(self.offset, bool)
            or not isinstance(self.offset, int)
            or not 0 <= self.offset <= MAX_OFFSET
        ):
            raise SearchInputError("Search offset is invalid.")

    def parameters(self, query: str) -> list[tuple[str, str]]:
        """Return the ordered query parameters for one request."""
        parameters: list[tuple[str, str]] = [
            ("q", query),
            ("words", self.words),
            ("match", self.match),
            ("case_sensitive", "true" if self.case_sensitive else "false"),
            ("scope", self.scope),
            ("diacritics", self.diacritics),
            ("sort", self.sort),
            ("limit", str(self.limit)),
            ("offset", str(self.offset)),
        ]
        parameters.extend(("book", str(book)) for book in self.books)
        parameters.extend(("exclude", term) for term in self.exclude)
        if self.proximity is not None:
            parameters.append(("proximity", str(self.proximity)))
        return parameters


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


class GetBibleSearchClient:
    """Send one bounded search request and return its validated envelope."""

    def __init__(
        self,
        *,
        base_url: str = SEARCH_API_BASE_URL,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_query_length: int = MAX_QUERY_LENGTH,
        opener: _Opener | None = None,
    ) -> None:
        if (
            not isinstance(base_url, str)
            or not base_url.startswith("https://")
            or "?" in base_url
            or "#" in base_url
        ):
            raise SearchInputError("Search API base URL must be an https URL.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise SearchInputError("Search timeout must be a positive finite number.")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1024
        ):
            raise SearchInputError("Search response bound must be at least 1024 bytes.")
        if (
            isinstance(max_query_length, bool)
            or not isinstance(max_query_length, int)
            or not 1 <= max_query_length <= MAX_QUERY_LENGTH
        ):
            raise SearchInputError(
                f"Search query bound must be between 1 and {MAX_QUERY_LENGTH}."
            )
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes
        self.max_query_length = max_query_length
        self._opener: _Opener = opener if opener is not None else _STRICT_OPENER.open

    def build_url(
        self,
        query: str,
        translation: str,
        criteria: SearchCriteria,
    ) -> str:
        """Return the canonical GET URL for one search."""
        code = self._translation_code(translation)
        text = self._query_text(query)
        encoded = urlencode(criteria.parameters(text), doseq=False)
        return f"{self.base_url}/{quote(code, safe='')}?{encoded}"

    def search(
        self,
        query: str,
        translation: str,
        criteria: SearchCriteria,
    ) -> dict[str, Any]:
        """Run one search and return the decoded ``{query, results, matches}`` envelope."""
        request = Request(
            self.build_url(query, translation, criteria),
            headers={
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                status = response.getcode()
                if isinstance(status, bool) or not isinstance(status, int):
                    raise SearchResponseError(
                        "The GetBible Search API returned an invalid HTTP status."
                    )
                if status != 200:
                    raise SearchHTTPError(status)
                self._validate_content_type(response.headers)
                self._validate_content_length(response.headers)
                payload = response.read(self.max_response_bytes + 1)
        except HTTPError as error:
            problem = self._problem(error)
            error.close()
            raise SearchHTTPError(error.code, **problem) from None
        except (TimeoutError, URLError, OSError, HTTPException):
            raise SearchTransportError() from None

        if not isinstance(payload, bytes):
            raise SearchResponseError(
                "The GetBible Search API returned a non-binary HTTP response."
            )
        if len(payload) > self.max_response_bytes:
            raise SearchResponseError(
                "The GetBible Search API response exceeded the configured size limit."
            )
        try:
            document = json.loads(
                payload.decode("utf-8"),
                parse_constant=self._reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise SearchResponseError(
                "The GetBible Search API returned malformed JSON."
            ) from None
        return self._validate_envelope(document)

    def _query_text(self, query: str) -> str:
        if not isinstance(query, str):
            raise SearchInputError("Search words must be text.")
        text = " ".join(query.split())
        if not text:
            raise SearchInputError("Search words are required.")
        if len(text) > self.max_query_length:
            raise SearchInputError(
                f"Search words cannot exceed {self.max_query_length} characters."
            )
        if _has_unsafe_characters(text):
            raise SearchInputError("Search words contain unsupported characters.")
        return text

    @staticmethod
    def _translation_code(translation: str) -> str:
        if not isinstance(translation, str):
            raise SearchInputError("Translation must be a GetBible abbreviation.")
        code = translation.strip().casefold()
        if _TRANSLATION_RE.fullmatch(code) is None:
            raise SearchInputError("Translation must be a GetBible abbreviation.")
        return code

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"Non-standard JSON constant: {value}")

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        value = headers.get(name)
        if value is not None:
            return value
        wanted = name.casefold()
        for key, candidate in headers.items():
            if key.casefold() == wanted:
                return candidate
        return None

    def _validate_content_type(self, headers: Mapping[str, str]) -> None:
        raw_content_type = self._header(headers, "Content-Type")
        if raw_content_type is None:
            raise SearchResponseError(
                "The GetBible Search API response did not declare JSON content."
            )
        media_type = raw_content_type.partition(";")[0].strip().casefold()
        if media_type != "application/json" and not media_type.endswith("+json"):
            raise SearchResponseError(
                "The GetBible Search API response did not declare JSON content."
            )

    def _validate_content_length(self, headers: Mapping[str, str]) -> None:
        raw_content_length = self._header(headers, "Content-Length")
        if raw_content_length is None:
            return
        try:
            content_length = int(raw_content_length, 10)
        except (TypeError, ValueError):
            raise SearchResponseError(
                "The GetBible Search API returned an invalid Content-Length header."
            ) from None
        if content_length < 0:
            raise SearchResponseError(
                "The GetBible Search API returned an invalid Content-Length header."
            )
        if content_length > self.max_response_bytes:
            raise SearchResponseError(
                "The GetBible Search API response exceeded the configured size limit."
            )

    def _problem(self, error: HTTPError) -> dict[str, Any]:
        """Read a bounded RFC 9457 problem document without trusting it."""
        details: dict[str, Any] = {"code": "", "detail": "", "retry_after": None}
        headers: Mapping[str, str] = dict(error.headers.items()) if error.headers else {}
        retry_after = self._header(headers, "Retry-After")
        if isinstance(retry_after, str) and retry_after.strip().isdigit():
            details["retry_after"] = min(int(retry_after.strip()), 3600)
        try:
            raw = error.read(_MAX_PROBLEM_BYTES + 1)
        except (OSError, HTTPException, ValueError):
            return details
        if not isinstance(raw, bytes) or len(raw) > _MAX_PROBLEM_BYTES:
            return details
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return details
        if not isinstance(document, dict):
            return details
        for field in ("code", "detail"):
            value = document.get(field)
            if (
                isinstance(value, str)
                and 0 < len(value) <= _MAX_PROBLEM_FIELD_LENGTH
                and not _has_unsafe_characters(value)
            ):
                details[field] = " ".join(value.split())
        raw_retry = document.get("retry_after")
        if (
            details["retry_after"] is None
            and isinstance(raw_retry, int)
            and not isinstance(raw_retry, bool)
            and raw_retry >= 0
        ):
            details["retry_after"] = min(raw_retry, 3600)
        return details

    @staticmethod
    def _validate_envelope(document: Any) -> dict[str, Any]:
        if not isinstance(document, dict):
            raise SearchResponseError(
                "The GetBible Search API response was not an object."
            )
        metadata = document.get("query")
        results = document.get("results")
        matches = document.get("matches")
        if (
            not isinstance(metadata, dict)
            or not isinstance(results, dict)
            or not isinstance(matches, list)
        ):
            raise SearchResponseError(
                "The GetBible Search API returned an incomplete search envelope."
            )
        kind = metadata.get("kind")
        if kind not in {"search", "reference"}:
            raise SearchResponseError(
                "The GetBible Search API returned an unknown result kind."
            )
        for field in ("total", "returned"):
            value = metadata.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SearchResponseError(
                    f"The GetBible Search API returned invalid {field} metadata."
                )
        return {"query": metadata, "results": results, "matches": matches}


def _has_unsafe_characters(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cs"} and not character.isspace()
        for character in value
    )


__all__ = [
    "DIACRITICS",
    "MATCHES",
    "MAX_BOOKS",
    "MAX_EXCLUSIONS",
    "MAX_EXCLUSION_LENGTH",
    "MAX_LIMIT",
    "MAX_OFFSET",
    "MAX_PROXIMITY",
    "MAX_QUERY_LENGTH",
    "SCOPES",
    "SEARCH_API_BASE_URL",
    "SORTS",
    "WORDS",
    "GetBibleSearchClient",
    "GetBibleSearchError",
    "SearchCriteria",
    "SearchHTTPError",
    "SearchInputError",
    "SearchResponseError",
    "SearchTransportError",
]
