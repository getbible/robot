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
_LANG_RE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,7}\Z")
_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
_T = TypeVar("_T")
_K = TypeVar("_K")
LOGGER = logging.getLogger(__name__)


def _normalize_lang(value: object) -> str:
    """Return safe canonical casing for a bounded BCP-47 language tag."""
    if not isinstance(value, str):
        return "und"
    candidate = value.strip().replace("_", "-")
    if _LANG_RE.fullmatch(candidate) is None:
        return "und"
    parts = candidate.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif (len(part) == 2 and part.isalpha()) or (
            len(part) == 3 and part.isdecimal()
        ):
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


@dataclass(frozen=True, slots=True)
class TranslationOption:
    """One validated translation shown in an interactive picker."""

    code: str
    name: str
    language: str
    lang: str = "und"
    direction: str = "ltr"


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


@dataclass(frozen=True, slots=True)
class ChapterVerse:
    """One validated verse from a complete Main API chapter."""

    number: int
    text: str


@dataclass(frozen=True, slots=True)
class ChapterContent:
    """One checksum-consistent, presentation-ready Main API chapter."""

    translation: str
    translation_name: str
    book_number: int
    book_name: str
    chapter: int
    reference: str
    verses: tuple[ChapterVerse, ...]
    sha: str


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
        scripture_cache_ttl_seconds: float = 900.0,
        scripture_cache_limit: int = 64,
        scripture_max_response_bytes: int = 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if scripture_cache_ttl_seconds <= 0:
            raise ValueError("scripture_cache_ttl_seconds must be positive.")
        if not 1 <= scripture_cache_limit <= 1024:
            raise ValueError("scripture_cache_limit must be between 1 and 1024.")
        if not 4096 <= scripture_max_response_bytes <= 16 * 1024 * 1024:
            raise ValueError(
                "scripture_max_response_bytes must be between 4096 and 16777216."
            )
        self._root = f"{base_url.rstrip('/')}/v2"
        self._timeout = timeout
        self._request_retries = request_retries
        self._max_response_bytes = max_response_bytes
        self._cache_ttl = cache_ttl_seconds
        self._books_cache_limit = books_cache_limit
        self._chapters_cache_limit = chapters_cache_limit
        self._scripture_cache_ttl = scripture_cache_ttl_seconds
        self._scripture_cache_limit = scripture_cache_limit
        self._scripture_max_response_bytes = min(
            max_response_bytes,
            scripture_max_response_bytes,
        )
        self._clock = clock
        self._translations: _CacheEntry | None = None
        self._books: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._chapters: OrderedDict[tuple[str, int, str], _CacheEntry] = OrderedDict()
        self._scripture: OrderedDict[
            tuple[str, int, str, int],
            _CacheEntry,
        ] = OrderedDict()
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

    def chapter(
        self,
        translation: str,
        book: BookOption,
        chapter: ChapterOption,
    ) -> ChapterContent:
        """Return one whole chapter after a bounded before/after hash check."""
        code = self._translation_code(translation)
        key = (code, book.number, book.sha, chapter.number)
        cached = self._cached_mapping(
            self._scripture,
            key,
            ttl_seconds=self._scripture_cache_ttl,
        )
        if cached is not None:
            return cached

        base = f"{code}/{book.number}/{chapter.number}"
        last_before = ""
        last_after = ""
        for _attempt in range(2):
            last_before = self._fetch_sha(f"{base}.sha", missing_translation=code)
            raw = self._fetch_bytes(
                f"{base}.json",
                missing_translation=code,
                accept="application/json",
                maximum_bytes=self._scripture_max_response_bytes,
            )
            last_after = self._fetch_sha(f"{base}.sha", missing_translation=code)
            if last_before != last_after:
                continue
            actual_sha = hashlib.sha1(raw, usedforsecurity=False).hexdigest()
            if actual_sha != last_after:
                raise CacheIntegrityError(
                    "Checksum mismatch for chapter content "
                    f"{code}/{book.number}/{chapter.number}."
                )
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RepositoryResponseError(
                    f"Invalid chapter JSON for {code}/{book.number}/{chapter.number}."
                ) from error
            result = self._validate_chapter_content(
                payload,
                code,
                book,
                chapter,
                last_after,
            )
            self._remember(
                self._scripture,
                key,
                result,
                self._scripture_cache_limit,
            )
            return result

        raise CacheIntegrityError(
            "Chapter content changed repeatedly during retrieval "
            f"({last_before} -> {last_after})."
        )

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
        accept: str = "application/json",
        maximum_bytes: int | None = None,
    ) -> bytes:
        url = f"{self._root}/{relative_path}"
        response_limit = (
            self._max_response_bytes
            if maximum_bytes is None
            else min(self._max_response_bytes, maximum_bytes)
        )
        last_error: Exception | None = None
        for attempt in range(self._request_retries + 1):
            try:
                return self._request_bytes(
                    url,
                    missing_translation,
                    accept,
                    response_limit,
                )
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
        accept: str,
        response_limit: int,
    ) -> bytes:
        with requests.get(
            url,
            headers={
                "Accept": accept,
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
                if announced > response_limit:
                    raise RepositoryResponseTooLarge(
                        "Navigation repository response exceeds the configured limit."
                    )

            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > response_limit:
                    raise RepositoryResponseTooLarge(
                        "Navigation repository response exceeds the configured limit."
                    )
            return bytes(body)

    def _fetch_sha(
        self,
        relative_path: str,
        *,
        missing_translation: str | None = None,
    ) -> str:
        raw = self._fetch_bytes(
            relative_path,
            missing_translation=missing_translation,
            accept="text/plain, application/octet-stream;q=0.9",
            maximum_bytes=256,
        )
        try:
            value = raw.decode("ascii").strip().casefold()
        except UnicodeDecodeError as error:
            raise RepositoryResponseError(
                f"Invalid checksum returned for {relative_path}."
            ) from error
        if _SHA1_RE.fullmatch(value) is None:
            raise RepositoryResponseError(
                f"Invalid checksum returned for {relative_path}."
            )
        return value

    def _cached_single(self, entry: _CacheEntry | None) -> _T | None:
        with self._guard:
            if entry is None or not self._fresh(entry):
                return None
            return entry.value  # type: ignore[return-value]

    def _cached_mapping(
        self,
        cache: OrderedDict[_K, _CacheEntry],
        key: _K,
        *,
        ttl_seconds: float | None = None,
    ) -> _T | None:
        with self._guard:
            entry = cache.get(key)
            if entry is None:
                return None
            if not self._fresh(entry, ttl_seconds=ttl_seconds):
                cache.pop(key, None)
                return None
            cache.move_to_end(key)
            return entry.value  # type: ignore[return-value]

    def _fresh(
        self,
        entry: _CacheEntry,
        *,
        ttl_seconds: float | None = None,
    ) -> bool:
        ttl = self._cache_ttl if ttl_seconds is None else ttl_seconds
        return self._clock() - entry.loaded_at < ttl

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
            lang = _normalize_lang(metadata.get("lang"))
            direction_value = metadata.get("direction")
            direction = (
                direction_value.strip().lower()
                if isinstance(direction_value, str)
                else "ltr"
            )
            if direction not in {"ltr", "rtl"}:
                direction = "ltr"
            result.append(
                TranslationOption(
                    code,
                    name.strip(),
                    language.strip(),
                    lang,
                    direction,
                )
            )

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
                not 1 <= number <= 200
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

    @staticmethod
    def _validate_chapter_content(
        payload: Any,
        code: str,
        book: BookOption,
        chapter: ChapterOption,
        sha: str,
    ) -> ChapterContent:
        if not isinstance(payload, dict):
            raise RepositoryResponseError("Scripture chapter payload is malformed.")
        translation_name = payload.get("translation")
        book_name = payload.get("book_name")
        reference = payload.get("name")
        raw_verses = payload.get("verses")
        if (
            payload.get("abbreviation") != code
            or payload.get("book_nr") != book.number
            or book_name != book.name
            or payload.get("chapter") != chapter.number
            or not isinstance(translation_name, str)
            or not 1 <= len(translation_name.strip()) <= 256
            or not isinstance(reference, str)
            or not 1 <= len(reference.strip()) <= 180
            or not isinstance(raw_verses, list)
            or not 1 <= len(raw_verses) <= 500
        ):
            raise RepositoryResponseError("Scripture chapter payload is malformed.")

        verses: list[ChapterVerse] = []
        seen: set[int] = set()
        for raw_verse in raw_verses:
            number = raw_verse.get("verse") if isinstance(raw_verse, dict) else None
            text = raw_verse.get("text") if isinstance(raw_verse, dict) else None
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or number not in chapter.verses
                or number in seen
                or not isinstance(text, str)
                or not text.strip()
                or len(text) > 20_000
            ):
                raise RepositoryResponseError(
                    "Scripture chapter verse data is malformed."
                )
            seen.add(number)
            verses.append(ChapterVerse(number, text.strip()))
        if seen != set(chapter.verses):
            raise RepositoryResponseError(
                "Scripture chapter does not match its navigation index."
            )
        return ChapterContent(
            translation=code,
            translation_name=translation_name.strip(),
            book_number=book.number,
            book_name=book.name,
            chapter=chapter.number,
            reference=reference.strip(),
            verses=tuple(sorted(verses, key=lambda item: item.number)),
            sha=sha,
        )
