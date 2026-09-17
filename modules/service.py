"""Bounded, non-blocking boundary around the public GetBible APIs.

The robot holds no Scripture of its own.  Navigation data comes from the Main
API catalogue, an explicit reference is resolved by the Query API, and a
full-text search is answered by the Search API.  Each upstream is reached from
a fixed worker pool behind a semaphore, a timeout, and a circuit breaker, so a
slow or failing service can neither block the event loop nor take the other
paths down with it.
"""

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
from typing import Any, TypeVar, cast

from config import Settings

from .audit import audit_event
from .catalog import (
    BookOption,
    CatalogClient,
    ChapterContent,
    ChapterOption,
    TranslationOption,
)
from .errors import (
    CircuitOpen,
    ReferenceValidationError,
    RepositoryError,
    RequestLimitError,
    RobotBusy,
    RobotInputError,
    ScriptureUnavailable,
    SearchValidationError,
    TranslationNotFoundError,
)
from .getbible_query import (
    GetBibleQueryClient,
    GetBibleQueryError,
    QueryHTTPError,
    QueryInputError,
    QueryLimitError,
    QueryTransportError,
)
from .getbible_search import MAX_LIMIT as SEARCH_API_MAX_LIMIT
from .getbible_search import (
    MAX_QUERY_LENGTH,
    GetBibleSearchClient,
    GetBibleSearchError,
    SearchCriteria,
    SearchHTTPError,
    SearchInputError,
    SearchTransportError,
)
from .interactions import SearchOptions, SearchResult

LOGGER = logging.getLogger(__name__)
_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")
#: A reference is book words, numbers, and the punctuation the Query API reads.
#: Anything else is refused before a request is spent on it.
_REFERENCE_RE = re.compile(r"[\w\s:,.\-–'’]{1,100}\Z")
MAX_SEARCH_TOTAL = 1_000_000
MAX_SEARCH_TERMS = 64
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
    kind: str = "search"
    offset: int = 0
    has_more: bool = False


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
    """Fail fast after repeated upstream failures and permit one recovery probe."""

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
    """Coordinates strict validation, bounded work, upstream access, and shutdown."""

    def __init__(
        self,
        settings: Settings,
        *,
        query_client: GetBibleQueryClient | None = None,
        search_client: GetBibleSearchClient | None = None,
        catalog: CatalogClient | None = None,
    ) -> None:
        self.settings = settings
        self._catalog = catalog or CatalogClient(
            base_url=settings.api_base_url,
            timeout=(settings.connect_timeout, settings.read_timeout),
            request_retries=settings.request_retries,
            max_response_bytes=settings.max_response_bytes,
            cache_ttl_seconds=settings.catalog_cache_ttl_seconds,
        )
        # One socket deadline per attempt; the lookup budget bounds the whole
        # wait, so a single stalled read can never consume it all by itself.
        self._query = query_client or GetBibleQueryClient(
            translation=settings.default_translation,
            base_url=f"{settings.query_base_url}/v2",
            timeout_seconds=settings.connect_timeout + settings.read_timeout,
            max_response_bytes=settings.max_response_bytes,
            max_references=max(settings.max_total_verses, 1),
        )
        self._search = search_client or GetBibleSearchClient(
            base_url=f"{settings.search_base_url}/v2",
            timeout_seconds=settings.search_timeout,
            max_response_bytes=settings.search_max_response_bytes,
            max_query_length=min(settings.max_input_length, MAX_QUERY_LENGTH),
        )
        self._executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_lookups,
            thread_name_prefix="getbible",
        )
        # Search keeps its own workers, permits, and circuit: the search service
        # is a different upstream from the catalogue and the Query API, and its
        # trouble must never cost a reader a chapter or a reference.
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
        self._catalog_flights: dict[tuple[object, ...], asyncio.Task[Any]] = {}
        self._catalog_flights_lock = asyncio.Lock()
        self.metrics = Metrics()
        self._closed = False

    async def resolve_query(
        self,
        arguments: Sequence[str],
        *,
        default_translation: str | None = None,
    ) -> ScriptureQuery:
        """Split command arguments into a reference and a translation.

        The Query API is the authority on what a reference means, so nothing is
        parsed here beyond the bounds a request must satisfy.  A trailing word
        names a translation only when the catalogue publishes it — ``John 3:16
        aov`` — and is otherwise part of the reference the API will judge.
        """
        raw = " ".join(arguments).strip()
        if not raw:
            raise RobotInputError("A Scripture reference is required.")
        if len(raw) > self.settings.max_input_length:
            raise RequestLimitError(
                f"Reference input cannot exceed {self.settings.max_input_length} characters."
            )

        translation = (
            default_translation.casefold()
            if default_translation is not None
            else self.settings.default_translation
        )
        if _TRANSLATION_RE.fullmatch(translation) is None:
            raise RobotInputError("The preferred Scripture translation is invalid.")

        reference = raw
        prefix, separator, candidate = raw.rpartition(" ")
        candidate = candidate.casefold()
        if (
            separator
            and prefix.strip()
            and _looks_like_translation(candidate)
            and await self.translation_exists(candidate)
        ):
            reference = prefix.strip()
            translation = candidate
        self._validate_reference_set(reference)
        return ScriptureQuery(reference, translation)

    async def translation_exists(self, abbreviation: str) -> bool:
        code = abbreviation.casefold()
        if _TRANSLATION_RE.fullmatch(code) is None:
            return False
        options = await self.translations()
        self.metrics.increment("translation_checks")
        return any(option.code == code for option in options)

    async def select(self, query: ScriptureQuery) -> dict[str, Any]:
        """Resolve one validated query through the Query API."""
        return await self._repository_call(
            "scripture_lookups",
            self._fetch_scripture,
            query.references,
            query.translation,
        )

    async def translations(self) -> tuple[TranslationOption, ...]:
        return await self._catalog_call(
            ("translations",),
            "catalog_translation_lookups",
            self._catalog.translations,
        )

    async def books(self, translation: str) -> tuple[BookOption, ...]:
        return await self._catalog_call(
            ("books", translation.casefold()),
            "catalog_book_lookups",
            self._catalog.books,
            translation,
        )

    async def chapters(
        self,
        translation: str,
        book: BookOption,
    ) -> tuple[ChapterOption, ...]:
        return await self._catalog_call(
            ("chapters", translation.casefold(), book.number, book.sha),
            "catalog_chapter_lookups",
            self._catalog.chapters,
            translation,
            book,
        )

    async def chapter(
        self,
        translation: str,
        book: BookOption,
        chapter: ChapterOption,
    ) -> ChapterContent:
        """Load one complete chapter from the Main API."""
        return await self._catalog_call(
            (
                "chapter",
                translation.casefold(),
                book.number,
                book.sha,
                chapter.number,
            ),
            "catalog_scripture_chapter_lookups",
            self._catalog.chapter,
            translation,
            book,
            chapter,
        )

    async def _catalog_call(
        self,
        key: tuple[object, ...],
        metric: str,
        function: Callable[..., _T],
        *args: Any,
    ) -> _T:
        """Share identical in-flight catalog reads without sharing mutations."""
        async with self._catalog_flights_lock:
            task = self._catalog_flights.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._repository_call(metric, function, *args)
                )
                self._catalog_flights[key] = task
                task.add_done_callback(
                    partial(self._finish_catalog_flight, key)
                )
        return cast(_T, await asyncio.shield(task))

    def _finish_catalog_flight(
        self,
        key: tuple[object, ...],
        task: asyncio.Task[Any],
    ) -> None:
        if self._catalog_flights.get(key) is task:
            self._catalog_flights.pop(key, None)
        if task.cancelled():
            return
        # Retrieve the exception so an abandoned shielded task never produces
        # an unhandled-task warning. Awaiting callers still receive it.
        task.exception()

    async def search(
        self,
        query: str,
        options: SearchOptions,
        *,
        offset: int = 0,
    ) -> SearchPage:
        """Run one bounded search through the Search API and validate its contract.

        The reader's criteria are passed through unaltered: the search service
        reads the writing system of the query itself.  One page is at most the
        configured result limit, clamped to the service's own maximum.
        """
        limit = min(self.settings.search_result_limit, SEARCH_API_MAX_LIMIT)
        try:
            criteria = SearchCriteria(
                words=options.words,
                match=options.match,
                case_sensitive=options.case_sensitive,
                scope=options.scope,
                books=tuple(options.books),
                diacritics=options.diacritics,
                exclude=tuple(options.exclude),
                proximity=options.proximity,
                sort=options.sort,
                limit=limit,
                offset=offset,
            )
        except SearchInputError as error:
            raise SearchValidationError(str(error)) from error
        response = await self._search_call(
            "scripture_searches",
            self._search_request,
            query,
            options.translation,
            criteria,
            # The socket deadline inside the worker fires first; this bound only
            # keeps a worker that ignores it from holding the caller forever.
            timeout=self.settings.search_timeout + 1.0,
        )
        return self._search_page(response, query, options.translation, limit)

    async def ready(self) -> bool:
        state = await self._circuit.snapshot()
        return not self._closed and state["state"] != "open"

    async def snapshot(self) -> dict[str, Any]:
        repository_circuit, search_circuit = await asyncio.gather(
            self._circuit.snapshot(),
            self._search_circuit.snapshot(),
        )
        return {
            "closed": self._closed,
            "metrics": self.metrics.snapshot(),
            "circuit": repository_circuit,
            "search_circuit": search_circuit,
        }

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

    def _validate_reference_set(self, value: str) -> None:
        references = value.split(";")
        if len(references) > self.settings.max_references:
            raise RequestLimitError(
                f"A request cannot contain more than {self.settings.max_references} references."
            )
        maximum_length = min(self.settings.max_input_length, 100)
        for raw_reference in references:
            reference = raw_reference.strip()
            if not reference:
                raise ReferenceValidationError("Invalid empty reference.")
            if len(reference) > maximum_length:
                raise ReferenceValidationError(
                    f"A reference cannot exceed {maximum_length} characters."
                )
            if _REFERENCE_RE.fullmatch(reference) is None or not any(
                character.isalnum() for character in reference
            ):
                raise ReferenceValidationError("The Scripture reference is invalid.")

    def _fetch_scripture(self, references: str, translation: str) -> dict[str, Any]:
        """Resolve a reference set in a worker thread, retrying only transport faults."""
        attempts = self.settings.request_retries + 1
        for attempt in range(attempts):
            try:
                return self._query.fetch_scripture(
                    references,
                    translation=translation,
                    max_verses=self.settings.max_total_verses,
                )
            except QueryLimitError as error:
                raise RequestLimitError(
                    "A request cannot select more than "
                    f"{self.settings.max_total_verses} verses."
                ) from error
            except QueryInputError as error:
                raise ReferenceValidationError("The Scripture reference is invalid.") from error
            except QueryHTTPError as error:
                if error.translation_not_found:
                    raise TranslationNotFoundError(
                        f"Translation ({translation}) not found."
                    ) from error
                if error.request_limit:
                    raise RequestLimitError(
                        "That request exceeds the Scripture service limits."
                    ) from error
                if error.invalid_reference:
                    raise ReferenceValidationError(
                        "The Scripture reference is invalid."
                    ) from error
                if error.retryable and attempt + 1 < attempts:
                    time.sleep(_backoff(attempt))
                    continue
                raise RepositoryError(
                    f"The Query API returned HTTP {error.status_code}."
                ) from error
            except QueryTransportError as error:
                if attempt + 1 < attempts:
                    time.sleep(_backoff(attempt))
                    continue
                raise RepositoryError("The Query API could not be reached.") from error
            except GetBibleQueryError as error:
                raise RepositoryError("The Query API returned an unusable answer.") from error
        raise RepositoryError("The Query API could not be reached.")

    def _search_request(
        self,
        query: str,
        translation: str,
        criteria: SearchCriteria,
    ) -> dict[str, Any]:
        """Run one search in a worker thread and classify what came back."""
        try:
            return self._search.search(query, translation, criteria)
        except SearchInputError as error:
            raise SearchValidationError(str(error)) from error
        except SearchHTTPError as error:
            if error.translation_not_found:
                raise TranslationNotFoundError(
                    f"Translation ({translation}) not found."
                ) from error
            if error.invalid_request:
                raise SearchValidationError(
                    error.detail or "The search words or filters were rejected."
                ) from error
            raise RepositoryError(
                f"The Search API returned HTTP {error.status_code}."
            ) from error
        except SearchTransportError as error:
            raise RepositoryError("The Search API could not be reached.") from error
        except GetBibleSearchError as error:
            raise RepositoryError("The Search API returned an unusable answer.") from error

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
        timeout: float | None = None,
    ) -> _T:
        return await self._bounded_call(
            metric,
            function,
            *arguments,
            executor=self._search_executor,
            semaphore=self._search_semaphore,
            circuit=self._search_circuit,
            queue_metric="search_queue_rejections",
            timeout=timeout,
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
        timeout: float | None = None,
    ) -> _T:
        if self._closed:
            raise ScriptureUnavailable("The Scripture service is closed.")
        effective_timeout = (
            self.settings.lookup_timeout if timeout is None else timeout
        )

        try:
            await asyncio.wait_for(
                semaphore.acquire(),
                timeout=self.settings.queue_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError) as error:
            self.metrics.increment(queue_metric)
            audit_event(
                LOGGER,
                self.settings,
                "capacity_queue_rejected",
                metadata={
                    "operation": metric,
                    "queue_metric": queue_metric,
                    "queue_timeout_seconds": self.settings.queue_timeout,
                },
                level=logging.WARNING,
            )
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
                timeout=effective_timeout,
            )
        except CircuitOpen:
            self.metrics.increment("circuit_rejections")
            audit_event(
                LOGGER,
                self.settings,
                "upstream_circuit_rejected",
                metadata={"operation": metric},
                level=logging.WARNING,
            )
            raise
        except (RobotInputError, TranslationNotFoundError):
            # The caller's input was refused; the upstream itself is healthy.
            await circuit.success()
            raise
        except (TimeoutError, asyncio.TimeoutError) as error:
            self.metrics.increment("lookup_timeouts")
            audit_event(
                LOGGER,
                self.settings,
                "lookup_timed_out",
                metadata={
                    "operation": metric,
                    "timeout_seconds": effective_timeout,
                },
                level=logging.WARNING,
            )
            if wrapped_future is not None:
                wrapped_future.add_done_callback(_consume_background_result)
            await circuit.failure()
            raise ScriptureUnavailable("The Scripture lookup timed out.") from error
        except asyncio.CancelledError:
            if wrapped_future is not None:
                wrapped_future.add_done_callback(_consume_background_result)
            await circuit.abandoned()
            raise
        except (RepositoryError, OSError) as error:
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
        limit: int,
    ) -> SearchPage:
        if not isinstance(response, dict):
            raise ScriptureUnavailable("The search service returned a malformed response.")
        metadata = response.get("query")
        grouped = response.get("results")
        matches = response.get("matches")
        if (
            not isinstance(metadata, dict)
            or not isinstance(grouped, dict)
            or not isinstance(matches, list)
        ):
            raise ScriptureUnavailable("The search service returned a malformed response.")
        kind = metadata.get("kind")
        total = metadata.get("total")
        if (
            kind not in {"search", "reference"}
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or total > MAX_SEARCH_TOTAL
            or len(matches) > limit
            or total < len(matches)
            or len(grouped) > limit
        ):
            raise ScriptureUnavailable("The search service returned invalid pagination.")
        offset = metadata.get("offset", 0) if kind == "search" else 0
        has_more = metadata.get("has_more", False) if kind == "search" else False
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or not isinstance(has_more, bool)
        ):
            raise ScriptureUnavailable("The search service returned invalid pagination.")

        verses: dict[tuple[int, int, int], tuple[str, str]] = {}
        verse_count = 0
        for chapter in grouped.values():
            if not isinstance(chapter, dict):
                raise ScriptureUnavailable("The search service returned malformed results.")
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
                raise ScriptureUnavailable("The search service returned malformed results.")
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
                        "The search service returned malformed verse data."
                    )
                key = (book_number, chapter_number, verse_number)
                if key in verses:
                    raise ScriptureUnavailable(
                        "The search service returned duplicate verse data."
                    )
                verse_count += 1
                if verse_count > limit:
                    raise ScriptureUnavailable(
                        "The search service returned too many verse records."
                    )
                verses[key] = (
                    book_name.strip(),
                    text.strip(),
                )

        items: list[SearchResult] = []
        seen_matches: set[tuple[int, int, int]] = set()
        for match in matches:
            if not isinstance(match, dict):
                raise ScriptureUnavailable("The search service returned malformed match metadata.")
            reference = match.get("reference")
            book_number = match.get("book_nr")
            chapter_number = match.get("chapter")
            verse_number = match.get("verse")
            # A reference-kind answer carries no matched terms at all.
            terms = match.get("terms", [])
            if terms is None:
                terms = []
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
                or not isinstance(terms, list)
                or len(terms) > MAX_SEARCH_TERMS
                or any(
                    not isinstance(term, str)
                    or not 1 <= len(term.strip()) <= self.settings.max_input_length
                    for term in terms
                )
            ):
                raise ScriptureUnavailable("The search service returned malformed match metadata.")
            key = (book_number, chapter_number, verse_number)
            if key in seen_matches:
                raise ScriptureUnavailable(
                    "The search service returned duplicate match metadata."
                )
            seen_matches.add(key)
            verse_data = verses.get(key)
            if verse_data is None:
                raise ScriptureUnavailable(
                    "The search service metadata did not match its result set."
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
                    terms=tuple(term.strip() for term in terms),
                )
            )

        return SearchPage(
            query=query,
            translation=translation,
            total=total,
            items=tuple(items),
            kind=kind,
            offset=offset,
            has_more=has_more,
        )


def _looks_like_translation(candidate: str) -> bool:
    """A trailing word can only name a translation if it reads like one."""
    return (
        _TRANSLATION_RE.fullmatch(candidate) is not None
        and any(character.isalpha() for character in candidate)
    )


def _backoff(attempt: int) -> float:
    return min(0.25 * (2**attempt), 1.0)


def _consume_background_result(future: asyncio.Future[Any]) -> None:
    """Retrieve a timed-out worker's eventual exception without exposing it."""
    with suppress(asyncio.CancelledError, Exception):
        future.exception()
