"""Bounded, validated access to the GetBible API navigation catalog."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import requests
from getbible import (
    CacheIntegrityError,
    RepositoryError,
    RepositoryResponseError,
    RepositoryResponseTooLarge,
    TranslationNotFoundError,
)

_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")
_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
_T = TypeVar("_T")
_K = TypeVar("_K")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranslationOption:
    """One validated translation shown in an interactive picker."""

    code: str
    name: str
    language: str


@dataclass(frozen=True, slots=True)
class BookOption:
    """One validated book entry from a translation's books index."""

    number: int
    name: str
    sha: str


@dataclass(frozen=True, slots=True)
class ChapterOption:
    """One chapter and its actual available verse numbers."""

    number: int
    verses: tuple[int, ...]


@dataclass(slots=True)
class _CacheEntry:
    value: object
    loaded_at: float


class _RetryableCatalogError(Exception):
    """Internal signal for a bounded retry of an idempotent catalog GET."""


class CatalogClient:
    """Fetch navigation metadata without allowing unbounded repository responses."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: tuple[float, float],
        request_retries: int,
        max_response_bytes: int,
        cache_ttl_seconds: float = 3600.0,
        books_cache_limit: int = 8,
        chapters_cache_limit: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = f"{base_url.rstrip('/')}/v2"
        self._timeout = timeout
        self._request_retries = request_retries
        self._max_response_bytes = max_response_bytes
        self._cache_ttl = cache_ttl_seconds
        self._books_cache_limit = books_cache_limit
        self._chapters_cache_limit = chapters_cache_limit
        self._clock = clock
        self._translations: _CacheEntry | None = None
        self._books: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._chapters: OrderedDict[tuple[str, int, str], _CacheEntry] = OrderedDict()
        self._guard = threading.RLock()

    def translations(self) -> tuple[TranslationOption, ...]:
        """Return the current validated translation catalog."""
        cached = self._cached_single(self._translations)
        if cached is not None:
            return cached

        payload = self._fetch_json("translations.json")
        result = self._validate_translations(payload)
        with self._guard:
            self._translations = _CacheEntry(result, self._clock())
        return result

    def books(self, translation: str) -> tuple[BookOption, ...]:
        """Return the actual books available in one translation."""
        code = self._translation_code(translation)
        cached = self._cached_mapping(self._books, code)
        if cached is not None:
            return cached

        payload = self._fetch_json(
            f"{code}/books.json",
            missing_translation=code,
        )
        result = self._validate_books(payload, code)
        self._remember(
            self._books,
            code,
            result,
            self._books_cache_limit,
        )
        return result

    def chapters(
        self,
        translation: str,
        book: BookOption,
    ) -> tuple[ChapterOption, ...]:
        """Return chapter and verse navigation derived from a checksum-verified book."""
        code = self._translation_code(translation)
        key = (code, book.number, book.sha)
        cached = self._cached_mapping(self._chapters, key)
        if cached is not None:
            return cached

        raw = self._fetch_bytes(
            f"{code}/{book.number}.json",
            missing_translation=code,
        )
        actual_sha = hashlib.sha1(raw, usedforsecurity=False).hexdigest()
        if actual_sha != book.sha:
            raise CacheIntegrityError(
                f"Checksum mismatch for navigation book {code}/{book.number}."
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RepositoryResponseError(
                f"Invalid navigation book JSON for {code}/{book.number}."
            ) from error
        result = self._validate_chapters(payload, code, book)
        self._remember(
            self._chapters,
            key,
            result,
            self._chapters_cache_limit,
        )
        return result

    def _fetch_json(
        self,
        relative_path: str,
        *,
        missing_translation: str | None = None,
    ) -> Any:
        raw = self._fetch_bytes(
            relative_path,
            missing_translation=missing_translation,
        )
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RepositoryResponseError(
                f"Invalid JSON returned for navigation resource {relative_path}."
            ) from error

    def _fetch_bytes(
        self,
        relative_path: str,
        *,
        missing_translation: str | None = None,
    ) -> bytes:
        url = f"{self._root}/{relative_path}"
        last_error: Exception | None = None
        for attempt in range(self._request_retries + 1):
            try:
                return self._request_bytes(url, missing_translation)
            except (requests.RequestException, _RetryableCatalogError) as error:
                last_error = error
                if attempt < self._request_retries:
                    time.sleep(min(0.25 * (2**attempt), 1.0))
                    continue
        raise RepositoryError("Navigation repository request failed.") from last_error

    def _request_bytes(
        self,
        url: str,
        missing_translation: str | None,
    ) -> bytes:
        with requests.get(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "getbible-robot/2.1",
            },
            timeout=self._timeout,
            stream=True,
            allow_redirects=False,
        ) as response:
            if response.status_code == 404 and missing_translation is not None:
                raise TranslationNotFoundError(
                    f"Translation ({missing_translation}) not found."
                )
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                raise _RetryableCatalogError(
                    f"Navigation repository returned HTTP {response.status_code}."
                )
            if response.status_code != 200:
                raise RepositoryError(
                    f"Navigation repository returned HTTP {response.status_code}."
                )
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    announced = int(length)
                except ValueError as error:
                    raise RepositoryResponseError(
                        "Navigation repository returned an invalid Content-Length."
                    ) from error
                if announced > self._max_response_bytes:
                    raise RepositoryResponseTooLarge(
                        "Navigation repository response exceeds the configured limit."
                    )

            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > self._max_response_bytes:
                    raise RepositoryResponseTooLarge(
                        "Navigation repository response exceeds the configured limit."
                    )
            return bytes(body)

    def _cached_single(self, entry: _CacheEntry | None) -> _T | None:
        with self._guard:
            if entry is None or not self._fresh(entry):
                return None
            return entry.value  # type: ignore[return-value]

    def _cached_mapping(
        self,
        cache: OrderedDict[_K, _CacheEntry],
        key: _K,
    ) -> _T | None:
        with self._guard:
            entry = cache.get(key)
            if entry is None:
                return None
            if not self._fresh(entry):
                cache.pop(key, None)
                return None
            cache.move_to_end(key)
            return entry.value  # type: ignore[return-value]

    def _fresh(self, entry: _CacheEntry) -> bool:
        return self._clock() - entry.loaded_at < self._cache_ttl

    def _remember(
        self,
        cache: OrderedDict[_K, _CacheEntry],
        key: _K,
        value: object,
        limit: int,
    ) -> None:
        with self._guard:
            cache[key] = _CacheEntry(value, self._clock())
            cache.move_to_end(key)
            while len(cache) > limit:
                cache.popitem(last=False)

    @staticmethod
    def _translation_code(value: str) -> str:
        code = value.casefold()
        if _TRANSLATION_RE.fullmatch(code) is None:
            raise TranslationNotFoundError("Invalid translation code.")
        return code

    @staticmethod
    def _validate_translations(payload: Any) -> tuple[TranslationOption, ...]:
        if not isinstance(payload, dict) or not 1 <= len(payload) <= 1000:
            raise RepositoryResponseError("Translation catalog is malformed.")
        result: list[TranslationOption] = []
        rejected = 0
        for raw_code, metadata in payload.items():
            if not isinstance(raw_code, str) or not isinstance(metadata, dict):
                rejected += 1
                continue
            code = raw_code.casefold()
            abbreviation = metadata.get("abbreviation")
            name = metadata.get("translation")
            if (
                _TRANSLATION_RE.fullmatch(code) is None
                or abbreviation != code
                or not isinstance(name, str)
                or not 1 <= len(name.strip()) <= 256
            ):
                rejected += 1
                continue

            language = metadata.get("language")
            if language is None or (isinstance(language, str) and not language.strip()):
                language = metadata.get("lang")
            if language is None:
                language = "Unspecified"
            if not isinstance(language, str) or not 1 <= len(language.strip()) <= 128:
                rejected += 1
                continue
            result.append(TranslationOption(code, name.strip(), language.strip()))

        if not result:
            raise RepositoryResponseError(
                "Translation catalog contains no usable translations."
            )
        if rejected:
            LOGGER.warning(
                "Ignored %d malformed translation catalog entr%s",
                rejected,
                "y" if rejected == 1 else "ies",
            )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.language.casefold(),
                    item.name.casefold(),
                    item.code,
                ),
            )
        )

    @staticmethod
    def _validate_books(payload: Any, code: str) -> tuple[BookOption, ...]:
        if not isinstance(payload, dict) or not 1 <= len(payload) <= 256:
            raise RepositoryResponseError("Translation books index is malformed.")
        result: list[BookOption] = []
        seen: set[int] = set()
        for raw_number, metadata in payload.items():
            if not isinstance(raw_number, str) or not isinstance(metadata, dict):
                raise RepositoryResponseError("Translation books index is malformed.")
            try:
                number = int(raw_number)
            except ValueError as error:
                raise RepositoryResponseError(
                    "Translation books index is malformed."
                ) from error
            name = metadata.get("name")
            sha = metadata.get("sha")
            if (
                not 1 <= number <= 1000
                or number in seen
                or metadata.get("nr") != number
                or metadata.get("abbreviation") != code
                or not isinstance(name, str)
                or not 1 <= len(name.strip()) <= 128
                or not isinstance(sha, str)
                or _SHA1_RE.fullmatch(sha) is None
            ):
                raise RepositoryResponseError("Translation books index is malformed.")
            seen.add(number)
            result.append(BookOption(number, name.strip(), sha))
        return tuple(sorted(result, key=lambda item: item.number))

    @staticmethod
    def _validate_chapters(
        payload: Any,
        code: str,
        book: BookOption,
    ) -> tuple[ChapterOption, ...]:
        if (
            not isinstance(payload, dict)
            or payload.get("abbreviation") != code
            or payload.get("nr") != book.number
            or payload.get("name") != book.name
        ):
            raise RepositoryResponseError("Navigation book payload is malformed.")
        chapters = payload.get("chapters")
        if not isinstance(chapters, list) or not 1 <= len(chapters) <= 500:
            raise RepositoryResponseError("Navigation book chapters are malformed.")

        result: list[ChapterOption] = []
        seen_chapters: set[int] = set()
        for chapter in chapters:
            if not isinstance(chapter, dict):
                raise RepositoryResponseError("Navigation book chapters are malformed.")
            number = chapter.get("chapter")
            verses = chapter.get("verses")
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or not 1 <= number <= 1000
                or number in seen_chapters
                or not isinstance(verses, list)
                or not 1 <= len(verses) <= 500
            ):
                raise RepositoryResponseError("Navigation book chapters are malformed.")
            seen_chapters.add(number)
            verse_numbers: list[int] = []
            seen_verses: set[int] = set()
            for verse in verses:
                verse_number = verse.get("verse") if isinstance(verse, dict) else None
                if (
                    not isinstance(verse_number, int)
                    or isinstance(verse_number, bool)
                    or not 1 <= verse_number <= 2000
                    or verse_number in seen_verses
                ):
                    raise RepositoryResponseError(
                        "Navigation book verse index is malformed."
                    )
                seen_verses.add(verse_number)
                verse_numbers.append(verse_number)
            result.append(ChapterOption(number, tuple(sorted(verse_numbers))))
        return tuple(sorted(result, key=lambda item: item.number))
