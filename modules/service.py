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
    TranslationNotFoundError,
)

from config import Settings
from modules.errors import CircuitOpen, RobotBusy, RobotInputError, ScriptureUnavailable

LOGGER = logging.getLogger(__name__)
_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ScriptureQuery:
    references: str
    translation: str


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
            negative_translation_cache_limit=64,
            negative_translation_ttl=300.0,
            max_response_bytes=settings.max_response_bytes,
            reference_cache_limit=5000,
            books_cache_limit=64,
            chapter_cache_limit=1024,
            search_corpus_limit=1,
            translation_cache_limit=2,
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
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_lookups)
        self._circuit = CircuitBreaker(
            failure_threshold=settings.circuit_failure_threshold,
            recovery_seconds=settings.circuit_recovery_seconds,
        )
        self.metrics = Metrics()
        self._closed = False

    async def resolve_query(self, arguments: Sequence[str]) -> ScriptureQuery:
        """Resolve command arguments without probing the network for ordinary references."""
        raw = " ".join(arguments).strip() or self.settings.default_verse
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

    async def ready(self) -> bool:
        state = await self._circuit.snapshot()
        return not self._closed and state["state"] != "open"

    async def snapshot(self) -> dict[str, Any]:
        state = {
            "closed": self._closed,
            "metrics": self.metrics.snapshot(),
            "circuit": await self._circuit.snapshot(),
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
        await loop.run_in_executor(
            None,
            partial(self._executor.shutdown, wait=True, cancel_futures=True),
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
        if self._closed:
            raise ScriptureUnavailable("The Scripture service is closed.")

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self.settings.queue_timeout,
            )
        except TimeoutError as error:
            self.metrics.increment("queue_rejections")
            raise RobotBusy("The Scripture lookup queue is full.") from error

        submitted = False
        wrapped_future: asyncio.Future[Any] | None = None
        try:
            if self._closed:
                raise ScriptureUnavailable("The Scripture service is closed.")
            await self._circuit.before_call()
            loop = asyncio.get_running_loop()
            raw_future: ConcurrentFuture[Any] = self._executor.submit(function, *arguments)
            submitted = True
            raw_future.add_done_callback(
                lambda _future: loop.call_soon_threadsafe(self._semaphore.release)
            )
            wrapped_future = asyncio.wrap_future(raw_future)
            result = await asyncio.wait_for(
                asyncio.shield(wrapped_future),
                timeout=self.settings.lookup_timeout,
            )
        except CircuitOpen:
            self.metrics.increment("circuit_rejections")
            raise
        except (ReferenceValidationError, RequestLimitError, TranslationNotFoundError):
            await self._circuit.success()
            raise
        except TimeoutError as error:
            self.metrics.increment("lookup_timeouts")
            if wrapped_future is not None:
                wrapped_future.add_done_callback(_consume_background_result)
            await self._circuit.failure()
            raise ScriptureUnavailable("The Scripture lookup timed out.") from error
        except (RepositoryError, CacheIntegrityError, OSError) as error:
            self.metrics.increment("repository_failures")
            await self._circuit.failure()
            raise ScriptureUnavailable("The Scripture repository is unavailable.") from error
        except Exception as error:
            self.metrics.increment("unexpected_failures")
            await self._circuit.failure()
            LOGGER.error(
                "Unexpected Scripture service failure (%s)",
                type(error).__name__,
            )
            raise ScriptureUnavailable("The Scripture service failed safely.") from error
        else:
            self.metrics.increment(metric)
            await self._circuit.success()
            return result
        finally:
            # A timed-out thread keeps its permit until it actually exits. This prevents
            # ThreadPoolExecutor's internal unbounded queue from becoming an attack queue.
            if not submitted:
                self._semaphore.release()


def _consume_background_result(future: asyncio.Future[Any]) -> None:
    """Retrieve a timed-out worker's eventual exception without exposing it."""
    with suppress(asyncio.CancelledError, Exception):
        future.exception()
