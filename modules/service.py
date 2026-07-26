"""Bounded, non-blocking boundary around the synchronous GetBible client."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import Any, TypeVar

from getbible import (
    CacheIntegrityError,
    GetBible,
    GetBibleReference,
    ReferenceValidationError,
    RepositoryError,
    RequestLimitError,
    RequestLimits,
    SearchBible,
    SearchLimits,
    SearchValidationError,
    TranslationNotFoundError,
)

from config import Settings
from modules.catalog import BookOption, CatalogClient, ChapterOption, TranslationOption
from modules.errors import CircuitOpen, RobotBusy, RobotInputError, ScriptureUnavailable
from modules.interactions import SearchOptions, SearchResult

LOGGER = logging.getLogger(__name__)
_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ScriptureQuery:
    references: str
    translation: str


@dataclass(frozen=True, slots=True)
class SearchPage:
    """Validated, presentation-ready search results."""

    query: str
    translation: str
    total: int
    items: tuple[SearchResult, ...]


class Metrics:
    """Small thread-safe aggregate counter set; never stores message content."""

    def __init__(self) -> None:
        self._values: Counter[str] = Counter()
        self._guard = threading.Lock()

    def increment(self, name: str, amount: int = 1) -> None:
        with self._guard:
            self._values[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._guard:
            return dict(self._values)


class CircuitBreaker:
    """Fail fast after repeated repository failures and permit one recovery probe."""

    def __init__(
        self,
        *,
        failure_threshold: int,
        recovery_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._clock = clock
        self._state = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False
        self._lock = asyncio.Lock()

    async def before_call(self) -> None:
        async with self._lock:
            if self._state == "closed":
                return
            now = self._clock()
            if self._state == "open":
                if now - self._opened_at < self._recovery_seconds:
                    raise CircuitOpen("The Scripture repository circuit is open.")
                self._state = "half_open"
                self._probe_in_flight = True
                return
            if self._probe_in_flight:
                raise CircuitOpen("The Scripture repository recovery probe is running.")
            self._probe_in_flight = True

    async def success(self) -> None:
        async with self._lock:
            self._state = "closed"
            self._failures = 0
            self._probe_in_flight = False

    async def failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == "half_open" or self._failures >= self._threshold:
                self._state = "open"
                self._opened_at = self._clock()
            self._probe_in_flight = False

    async def abandoned(self) -> None:
        """Release half-open probe state when its caller is cancelled."""
        async with self._lock:
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = self._clock()
            self._probe_in_flight = False

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            retry_after = 0.0
            if self._state == "open":
                retry_after = max(
                    0.0,
                    self._recovery_seconds - (self._clock() - self._opened_at),
                )
            return {
                "state": self._state,
                "failures": self._failures,
                "retry_after_seconds": round(retry_after, 3),
            }


class ScriptureService:
    """Coordinates strict parsing, bounded work, repository access, and shutdown."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: GetBible | None = None,
        parser: GetBibleReference | None = None,
    ) -> None:
        self.settings = settings
        limits = RequestLimits(
            max_input_length=settings.max_input_length,
            max_references=settings.max_references,
            max_verses_per_reference=settings.max_verses_per_reference,
            max_total_verses=settings.max_total_verses,
        )
        self._client = client or GetBible(
            repo_path=settings.api_base_url,
            request_timeout=(settings.connect_timeout, settings.read_timeout),
            request_retries=settings.request_retries,
            request_limits=limits,
            search_limits=SearchLimits(
                max_response_bytes=settings.search_max_response_bytes,
                max_query_length=settings.max_input_length,
                max_limit=settings.search_result_limit,
                deadline_seconds=settings.search_deadline_seconds,
            ),
            negative_translation_cache_limit=64,
            negative_translation_ttl=300.0,
            max_response_bytes=settings.max_response_bytes,
            reference_cache_limit=5000,
            books_cache_limit=64,
            chapter_cache_limit=1024,
            search_corpus_limit=1,
            translation_cache_limit=2,
        )
        self._catalog = CatalogClient(
            base_url=settings.api_base_url,
            timeout=(settings.connect_timeout, settings.read_timeout),
            request_retries=settings.request_retries,
            max_response_bytes=settings.max_response_bytes,
            cache_ttl_seconds=settings.catalog_cache_ttl_seconds,
        )
        self._parser = parser or GetBibleReference(
            cache_limit=5000,
            max_reference_length=min(settings.max_input_length, 100),
            max_verses=settings.max_verses_per_reference,
            max_verse_number=1000,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_lookups,
            thread_name_prefix="getbible",
        )
        self._search_executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_searches,
            thread_name_prefix="getbible-search",
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_lookups)
        self._search_semaphore = asyncio.Semaphore(settings.max_concurrent_searches)
        self._circuit = CircuitBreaker(
            failure_threshold=settings.circuit_failure_threshold,
            recovery_seconds=settings.circuit_recovery_seconds,
        )
        self._search_circuit = CircuitBreaker(
            failure_threshold=settings.circuit_failure_threshold,
            recovery_seconds=settings.circuit_recovery_seconds,
        )
        self.metrics = Metrics()
        self._closed = False

    async def resolve_query(self, arguments: Sequence[str]) -> ScriptureQuery:
        """Resolve command arguments without probing the network for ordinary references."""
        raw = " ".join(arguments).strip()
        if not raw:
            raise RobotInputError("A Scripture reference is required.")
        if len(raw) > self.settings.max_input_length:
            raise RequestLimitError(
                f"Reference input cannot exceed {self.settings.max_input_length} characters."
            )

        try:
            self._validate_reference_set(raw, self.settings.default_translation)
            return ScriptureQuery(raw, self.settings.default_translation)
        except RequestLimitError:
            raise
        except ReferenceValidationError:
            pass

        prefix, separator, candidate = raw.rpartition(" ")
        candidate = candidate.casefold()
        if (
            not separator
            or not prefix.strip()
            or _TRANSLATION_RE.fullmatch(candidate) is None
        ):
            raise RobotInputError("The Scripture reference is invalid.")

        reference = prefix.strip()
        self._validate_reference_set(reference, candidate)
        if not await self.translation_exists(candidate):
            raise TranslationNotFoundError(f"Translation ({candidate}) not found.")
        return ScriptureQuery(reference, candidate)

    async def translation_exists(self, abbreviation: str) -> bool:
        code = abbreviation.casefold()
        if _TRANSLATION_RE.fullmatch(code) is None:
            return False
        return await self._repository_call(
            "translation_checks",
            self._client.valid_translation,
            code,
        )

    async def select(self, query: ScriptureQuery) -> dict[str, Any]:
        return await self._repository_call(
            "scripture_lookups",
            self._client.select,
            query.references,
            query.translation,
        )

    async def translations(self) -> tuple[TranslationOption, ...]:
        return await self._repository_call(
            "catalog_translation_lookups",
            self._catalog.translations,
        )

    async def books(self, translation: str) -> tuple[BookOption, ...]:
        return await self._repository_call(
            "catalog_book_lookups",
            self._catalog.books,
            translation,
        )

    async def chapters(
        self,
        translation: str,
        book: BookOption,
    ) -> tuple[ChapterOption, ...]:
        return await self._repository_call(
            "catalog_chapter_lookups",
            self._catalog.chapters,
            translation,
            book,
        )

    async def search(
        self,
        query: str,
        options: SearchOptions,
    ) -> SearchPage:
        """Run one bounded Librarian 1.2 search and validate its public contract."""
        criteria = SearchBible(
            words=options.words,
            match=options.match,
            case_sensitive=options.case_sensitive,
            scope=options.scope,
            books=options.books,
            diacritics=options.diacritics,
            exclude=options.exclude,
            proximity=options.proximity,
            sort=options.sort,
            limit=self.settings.search_result_limit,
            offset=0,
        )
        response = await self._search_call(
            "scripture_searches",
            self._client.search,
            query,
            options.translation,
            criteria,
        )
        return self._search_page(response, query, options.translation)

    async def warm_default_translation(self) -> dict[str, Any]:
        """Load the default corpus and index before Telegram accepts traffic."""
        return await self._search_call(
            "search_warmups",
            self._client.warm_translation,
            self.settings.default_translation,
        )

    async def ready(self) -> bool:
        state = await self._circuit.snapshot()
        return not self._closed and state["state"] != "open"

    async def snapshot(self) -> dict[str, Any]:
        repository_circuit, search_circuit = await asyncio.gather(
            self._circuit.snapshot(),
            self._search_circuit.snapshot(),
        )
        state = {
            "closed": self._closed,
            "metrics": self.metrics.snapshot(),
            "circuit": repository_circuit,
            "search_circuit": search_circuit,
        }
        try:
            state["librarian"] = self._client.cache_info()
        except Exception:  # telemetry must never affect serving
            state["librarian"] = {"available": False}
        return state

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = asyncio.get_running_loop()
        await asyncio.gather(
            loop.run_in_executor(
                None,
                partial(self._executor.shutdown, wait=True, cancel_futures=True),
            ),
            loop.run_in_executor(
                None,
                partial(self._search_executor.shutdown, wait=True, cancel_futures=True),
            ),
        )
        self._client.close()

    def _validate_reference_set(self, value: str, translation: str) -> None:
        references = value.split(";")
        if len(references) > self.settings.max_references:
            raise RequestLimitError(
                f"A request cannot contain more than {self.settings.max_references} references."
            )
        total = 0
        for raw_reference in references:
            reference = raw_reference.strip()
            if not reference:
                raise ReferenceValidationError("Invalid empty reference.")
            parsed = self._parser.ref(reference, translation)
            total += len(parsed.verses)
            if total > self.settings.max_total_verses:
                raise RequestLimitError(
                    f"A request cannot select more than {self.settings.max_total_verses} verses."
                )

    async def _repository_call(
        self,
        metric: str,
        function: Callable[..., _T],
        *arguments: object,
    ) -> _T:
        return await self._bounded_call(
            metric,
            function,
            *arguments,
            executor=self._executor,
            semaphore=self._semaphore,
            circuit=self._circuit,
            queue_metric="queue_rejections",
        )

    async def _search_call(
        self,
        metric: str,
        function: Callable[..., _T],
        *arguments: object,
    ) -> _T:
        return await self._bounded_call(
            metric,
            function,
            *arguments,
            executor=self._search_executor,
            semaphore=self._search_semaphore,
            circuit=self._search_circuit,
            queue_metric="search_queue_rejections",
        )

    async def _bounded_call(
        self,
        metric: str,
        function: Callable[..., _T],
        *arguments: object,
        executor: ThreadPoolExecutor,
        semaphore: asyncio.Semaphore,
        circuit: CircuitBreaker,
        queue_metric: str,
    ) -> _T:
        if self._closed:
            raise ScriptureUnavailable("The Scripture service is closed.")

        try:
            await asyncio.wait_for(
                semaphore.acquire(),
                timeout=self.settings.queue_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError) as error:
            self.metrics.increment(queue_metric)
            raise RobotBusy("The Scripture lookup queue is full.") from error

        submitted = False
        wrapped_future: asyncio.Future[Any] | None = None
        try:
            if self._closed:
                raise ScriptureUnavailable("The Scripture service is closed.")
            await circuit.before_call()
            loop = asyncio.get_running_loop()
            raw_future: ConcurrentFuture[Any] = executor.submit(function, *arguments)
            submitted = True
            raw_future.add_done_callback(
                lambda _future: loop.call_soon_threadsafe(semaphore.release)
            )
            wrapped_future = asyncio.wrap_future(raw_future)
            result = await asyncio.wait_for(
                asyncio.shield(wrapped_future),
                timeout=self.settings.lookup_timeout,
            )
        except CircuitOpen:
            self.metrics.increment("circuit_rejections")
            raise
        except (
            ReferenceValidationError,
            RequestLimitError,
            SearchValidationError,
            TranslationNotFoundError,
        ):
            await circuit.success()
            raise
        except (TimeoutError, asyncio.TimeoutError) as error:
            self.metrics.increment("lookup_timeouts")
            if wrapped_future is not None:
                wrapped_future.add_done_callback(_consume_background_result)
            await circuit.failure()
            raise ScriptureUnavailable("The Scripture lookup timed out.") from error
        except asyncio.CancelledError:
            if wrapped_future is not None:
                wrapped_future.add_done_callback(_consume_background_result)
            await circuit.abandoned()
            raise
        except (RepositoryError, CacheIntegrityError, OSError) as error:
            self.metrics.increment("repository_failures")
            await circuit.failure()
            raise ScriptureUnavailable("The Scripture repository is unavailable.") from error
        except Exception as error:
            self.metrics.increment("unexpected_failures")
            await circuit.failure()
            LOGGER.error(
                "Unexpected Scripture service failure (%s)",
                type(error).__name__,
            )
            raise ScriptureUnavailable("The Scripture service failed safely.") from error
        else:
            self.metrics.increment(metric)
            await circuit.success()
            return result
        finally:
            # A timed-out thread keeps its permit until it actually exits. This prevents
            # ThreadPoolExecutor's internal unbounded queue from becoming an attack queue.
            if not submitted:
                semaphore.release()

    def _search_page(
        self,
        response: object,
        query: str,
        translation: str,
    ) -> SearchPage:
        if not isinstance(response, dict):
            raise ScriptureUnavailable("Librarian returned a malformed search response.")
        metadata = response.get("query")
        grouped = response.get("results")
        matches = response.get("matches")
        if (
            not isinstance(metadata, dict)
            or not isinstance(grouped, dict)
            or not isinstance(matches, list)
        ):
            raise ScriptureUnavailable("Librarian returned a malformed search response.")
        total = metadata.get("total")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or len(matches) > self.settings.search_result_limit
            or total < len(matches)
            or len(grouped) > self.settings.search_result_limit
        ):
            raise ScriptureUnavailable("Librarian returned invalid search pagination.")

        verses: dict[tuple[int, int, int], tuple[str, str]] = {}
        verse_count = 0
        for chapter in grouped.values():
            if not isinstance(chapter, dict):
                raise ScriptureUnavailable("Librarian returned malformed search results.")
            book_number = chapter.get("book_nr")
            book_name = chapter.get("book_name")
            chapter_number = chapter.get("chapter")
            chapter_verses = chapter.get("verses")
            if (
                not isinstance(book_number, int)
                or isinstance(book_number, bool)
                or not 1 <= book_number <= 1000
                or not isinstance(book_name, str)
                or not 1 <= len(book_name.strip()) <= 128
                or not isinstance(chapter_number, int)
                or isinstance(chapter_number, bool)
                or not 1 <= chapter_number <= 1000
                or not isinstance(chapter_verses, list)
            ):
                raise ScriptureUnavailable("Librarian returned malformed search results.")
            for verse in chapter_verses:
                verse_number = verse.get("verse") if isinstance(verse, dict) else None
                text = verse.get("text") if isinstance(verse, dict) else None
                if (
                    not isinstance(verse_number, int)
                    or isinstance(verse_number, bool)
                    or not 1 <= verse_number <= 2000
                    or not isinstance(text, str)
                    or not text.strip()
                ):
                    raise ScriptureUnavailable(
                        "Librarian returned malformed search verse data."
                    )
                key = (book_number, chapter_number, verse_number)
                if key in verses:
                    raise ScriptureUnavailable(
                        "Librarian returned duplicate search verse data."
                    )
                verse_count += 1
                if verse_count > self.settings.search_result_limit:
                    raise ScriptureUnavailable(
                        "Librarian returned too many search verse records."
                    )
                verses[key] = (
                    book_name.strip(),
                    text.strip(),
                )

        items: list[SearchResult] = []
        seen_matches: set[tuple[int, int, int]] = set()
        for match in matches:
            if not isinstance(match, dict):
                raise ScriptureUnavailable("Librarian returned malformed match metadata.")
            reference = match.get("reference")
            book_number = match.get("book_nr")
            chapter_number = match.get("chapter")
            verse_number = match.get("verse")
            if (
                not isinstance(reference, str)
                or not 1 <= len(reference.strip()) <= self.settings.max_input_length
                or not isinstance(book_number, int)
                or isinstance(book_number, bool)
                or not 1 <= book_number <= 1000
                or not isinstance(chapter_number, int)
                or isinstance(chapter_number, bool)
                or not 1 <= chapter_number <= 1000
                or not isinstance(verse_number, int)
                or isinstance(verse_number, bool)
                or not 1 <= verse_number <= 2000
            ):
                raise ScriptureUnavailable("Librarian returned malformed match metadata.")
            key = (book_number, chapter_number, verse_number)
            if key in seen_matches:
                raise ScriptureUnavailable(
                    "Librarian returned duplicate search match metadata."
                )
            seen_matches.add(key)
            verse_data = verses.get(key)
            if verse_data is None:
                raise ScriptureUnavailable(
                    "Librarian search metadata did not match its result set."
                )
            book_name, text = verse_data
            items.append(
                SearchResult(
                    reference=reference.strip(),
                    book_number=book_number,
                    book_name=book_name,
                    chapter=chapter_number,
                    verse=verse_number,
                    text=text,
                )
            )

        return SearchPage(
            query=query,
            translation=translation,
            total=total,
            items=tuple(items),
        )


def _consume_background_result(future: asyncio.Future[Any]) -> None:
    """Retrieve a timed-out worker's eventual exception without exposing it."""
    with suppress(asyncio.CancelledError, Exception):
        future.exception()
