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
from typing import Any, TypeVar, cast

from getbible import (
    SEARCH_ENGINE_VERSION,
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
from getbible.search import shared_registry

from config import Settings

from .audit import audit_event
from .catalog import (
    BookOption,
    CatalogClient,
    ChapterContent,
    ChapterOption,
    TranslationOption,
)
from .errors import CircuitOpen, RobotBusy, RobotInputError, ScriptureUnavailable
from .interactions import SearchOptions, SearchResult

LOGGER = logging.getLogger(__name__)
_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")
MAX_SEARCH_TOTAL = 1_000_000
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
                # An index build serves every later request, so Librarian 2 bounds
                # it separately instead of charging it to whichever request arrived
                # first and leaving nothing cached behind the failure.
                index_build_seconds=settings.search_index_build_seconds,
            ),
            negative_translation_cache_limit=64,
            negative_translation_ttl=300.0,
            max_response_bytes=settings.max_response_bytes,
            reference_cache_limit=settings.reference_cache_limit,
            books_cache_limit=settings.books_cache_limit,
            chapter_cache_limit=settings.chapter_cache_limit,
            search_corpus_limit=settings.search_corpus_limit,
            translation_cache_limit=settings.translation_cache_limit,
        )
        # Two different caches, deliberately sized apart. `search_corpus_limit`
        # bounds this client's handle dictionary; the corpora themselves live in
        # Librarian's process-wide registry, and that registry is what lets a
        # second search of a translation skip the parse-and-analyse entirely.
        # Sizing it down to the per-client limit would defeat the point: every
        # switch between translations would re-read and re-index from scratch.
        # It is therefore configured on its own, for reuse first.
        shared_registry().resize(settings.search_shared_corpus_limit)
        self._catalog = CatalogClient(
            base_url=settings.api_base_url,
            timeout=(settings.connect_timeout, settings.read_timeout),
            request_retries=settings.request_retries,
            max_response_bytes=settings.max_response_bytes,
            cache_ttl_seconds=settings.catalog_cache_ttl_seconds,
        )
        self._parser = parser or GetBibleReference(
            cache_limit=settings.reference_cache_limit,
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
        """Resolve command arguments without probing the network for ordinary references."""
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

        try:
            self._validate_reference_set(raw, translation)
            return ScriptureQuery(raw, translation)
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
        """Load one complete chapter from the Main API, not Librarian."""
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
    ) -> SearchPage:
        """Run one bounded Librarian search and validate its public contract.

        Librarian 2 derives the matching strategy from the query text itself, so
        the application passes the user's criteria through unaltered. The 1.x
        habit of flipping ``whole_word`` to ``substring`` on seeing a continuous
        script is gone: it loosened the space-delimited terms of a mixed query,
        so ``all`` began matching inside ``shall``.

        The wait is the search budget, not the reference-lookup budget. The first
        search of a translation builds that translation's index, and the generic
        lookup timeout was shorter than the build it was waiting on, so searching
        anything other than the prewarmed default reliably failed on its first
        attempt while the build ran on in its worker.
        """
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
            timeout=self.settings.search_timeout,
        )
        return self._search_page(response, query, options.translation)

    async def warm_default_translation(self) -> dict[str, Any]:
        """Load the default corpus and index before Telegram accepts traffic.

        A cold build is bounded by the index budget rather than the interactive
        lookup timeout. Paying it once here is what keeps every later request off
        the build path, and Librarian shares the result process-wide.

        The case and diacritics policy must be passed explicitly and must match
        the policy a default search uses. An index is keyed by that pair, so
        warming under a different one builds an index no search will read: the
        prewarm becomes dead work and the first real search pays the whole build
        inside the request path. Librarian's hardened facade still defaults this
        argument to the 1.x spelling, which resolves to `exact`, so relying on
        its default would warm the wrong index.
        """
        policy = SearchOptions()
        return await self._search_call(
            "search_warmups",
            partial(
                self._client.warm_translation,
                case_sensitive=policy.case_sensitive,
                diacritics=policy.diacritics,
            ),
            self.settings.default_translation,
            timeout=self.settings.search_index_build_seconds,
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
            # Matching semantics can change without a translation SHA changing.
            # Publishing the engine version is what lets an operator tell an
            # upgrade apart from a regression when result counts move.
            "search_engine_version": SEARCH_ENGINE_VERSION,
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
            or total > MAX_SEARCH_TOTAL
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
            terms = match.get("terms")
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
                or not 1 <= len(terms) <= 64
                or any(
                    not isinstance(term, str)
                    or not 1 <= len(term.strip()) <= self.settings.max_input_length
                    for term in terms
                )
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
                    terms=tuple(term.strip() for term in terms),
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
