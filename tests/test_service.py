"""ScriptureService: the bounded boundary around the public GetBible APIs.

Every upstream is faked at the seam the service exposes — a Query API client,
a Search API client, and the Main API catalogue — so these tests pin the
service's own contract: what is refused before a request is spent, how each
API answer is classified for the reader, and that the search path can fail
without costing anyone a reference or a chapter.
"""

import asyncio
import os
import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import replace
from unittest.mock import patch

from config import Settings
from modules.catalog import (
    BookOption,
    ChapterContent,
    ChapterOption,
    ChapterVerse,
    TranslationOption,
)
from modules.errors import (
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
from modules.getbible_query import (
    QueryHTTPError,
    QueryInputError,
    QueryLimitError,
    QueryResponseError,
    QueryTransportError,
)
from modules.getbible_search import (
    SearchHTTPError,
    SearchInputError,
    SearchResponseError,
    SearchTransportError,
)
from modules.interactions import SearchOptions
from modules.service import CircuitBreaker, ScriptureQuery, ScriptureService, _backoff

VERSE_TEXT = "For God so loved the world."
JOHN = BookOption(43, "John", "a" * 40)
JOHN_3 = ChapterOption(3, (16, 17))


def _chapter(
    translation: str,
    book_nr: int,
    book_name: str,
    chapter: int,
    verses: list[tuple[int, str]],
) -> dict:
    return {
        "translation": "King James Version",
        "abbreviation": translation,
        "lang": "en",
        "language": "English",
        "direction": "LTR",
        "encoding": "UTF-8",
        "book_nr": book_nr,
        "book_name": book_name,
        "chapter": chapter,
        "name": f"{book_name} {chapter}",
        "ref": [f"{book_nr} {chapter}:{verse}" for verse, _ in verses],
        "verses": [
            {
                "chapter": chapter,
                "verse": verse,
                "name": f"{book_name} {chapter}:{verse}",
                "text": text,
            }
            for verse, text in verses
        ],
    }


def _scripture_document(translation: str = "kjv") -> dict:
    return {f"{translation}_43_3": _chapter(translation, 43, "John", 3, [(16, VERSE_TEXT)])}


def _search_envelope(translation: str = "kjv") -> dict:
    return {
        "query": {
            "text": "loved",
            "kind": "search",
            "translation": translation,
            "engine_version": 4,
            "total": 1,
            "returned": 1,
            "offset": 0,
            "limit": 50,
            "has_more": False,
        },
        "results": {
            f"{translation}_43_3": _chapter(translation, 43, "John", 3, [(16, VERSE_TEXT)]),
        },
        "matches": [
            {
                "reference": "John 3:16",
                "book_nr": 43,
                "chapter": 3,
                "verse": 16,
                "score": 1.0,
                "occurrences": 1,
                "terms": ["loved"],
            }
        ],
    }


class _QueryClient:
    """Fake Query API client: scripted answers, otherwise John 3:16."""

    def __init__(
        self,
        *actions: dict | BaseException,
        delay: float = 0.0,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.actions = list(actions)
        self.delay = delay
        self.started = started
        self.release = release
        self.calls: list[tuple[str, str, int | None]] = []

    def fetch_scripture(
        self,
        reference: str,
        *,
        translation: str,
        max_verses: int | None,
    ) -> dict:
        self.calls.append((reference, translation, max_verses))
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        if self.delay:
            time.sleep(self.delay)
        if self.actions:
            action = self.actions.pop(0)
            if isinstance(action, BaseException):
                raise action
            return action
        return _scripture_document(translation)


class _SearchClient:
    """Fake Search API client: scripted answers, otherwise one John 3:16 hit."""

    def __init__(
        self,
        *actions: dict | BaseException,
        delay: float = 0.0,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.actions = list(actions)
        self.delay = delay
        self.started = started
        self.release = release
        self.calls: list[tuple[str, str, object]] = []

    def search(self, query: str, translation: str, criteria: object) -> dict:
        self.calls.append((query, translation, criteria))
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        if self.delay:
            time.sleep(self.delay)
        if self.actions:
            action = self.actions.pop(0)
            if isinstance(action, BaseException):
                raise action
            return action
        return _search_envelope(translation)


class _Catalog:
    """Fake Main API catalogue publishing a fixed set of translations."""

    def __init__(
        self,
        codes: tuple[str, ...] = ("kjv", "aov", "codex"),
        *,
        fail: BaseException | None = None,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.options = tuple(
            TranslationOption(code, code.upper(), "English", "en") for code in codes
        )
        self.fail = fail
        self.started = started
        self.release = release
        self.calls: list[tuple[object, ...]] = []

    def _record(self, *call: object) -> None:
        self.calls.append(call)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        if self.fail is not None:
            raise self.fail

    def translations(self) -> tuple[TranslationOption, ...]:
        self._record("translations")
        return self.options

    def books(self, translation: str) -> tuple[BookOption, ...]:
        self._record("books", translation)
        return (JOHN,)

    def chapters(self, translation: str, book: BookOption) -> tuple[ChapterOption, ...]:
        self._record("chapters", translation, book.number)
        return (JOHN_3,)

    def chapter(
        self,
        translation: str,
        book: BookOption,
        chapter: ChapterOption,
    ) -> ChapterContent:
        self._record("chapter", translation, book.number, chapter.number)
        return ChapterContent(
            translation=translation,
            translation_name=translation.upper(),
            book_number=book.number,
            book_name=book.name,
            chapter=chapter.number,
            reference=f"{book.name} {chapter.number}",
            verses=(ChapterVerse(16, VERSE_TEXT),),
            sha=book.sha,
        )


def _settings(**environment: str) -> Settings:
    values = {
        "TELEGRAM_API_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        "HEALTH_PORT": "0",
    }
    values.update(environment)
    with patch.dict(os.environ, values, clear=True):
        return Settings.from_env(load_environment_file=False)


class CircuitBreakerTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_abandoned_half_open_probe_can_be_retried_after_recovery(self) -> None:
        now = [0.0]
        circuit = CircuitBreaker(
            failure_threshold=1,
            recovery_seconds=5.0,
            clock=lambda: now[0],
        )

        await circuit.failure()
        now[0] = 6.0
        await circuit.before_call()
        self.assertEqual((await circuit.snapshot())["state"], "half_open")

        await circuit.abandoned()
        abandoned = await circuit.snapshot()
        self.assertEqual(abandoned["state"], "open")
        self.assertEqual(abandoned["failures"], 1)

        now[0] = 12.0
        await circuit.before_call()
        await circuit.success()
        self.assertEqual((await circuit.snapshot())["state"], "closed")

    async def test_half_open_circuit_admits_one_probe_at_a_time(self) -> None:
        now = [0.0]
        circuit = CircuitBreaker(
            failure_threshold=1,
            recovery_seconds=5.0,
            clock=lambda: now[0],
        )

        await circuit.failure()
        with self.assertRaises(CircuitOpen):
            await circuit.before_call()
        opened = await circuit.snapshot()
        self.assertEqual(opened["state"], "open")
        self.assertEqual(opened["retry_after_seconds"], 5.0)

        now[0] = 5.0
        await circuit.before_call()
        with self.assertRaises(CircuitOpen):
            await circuit.before_call()
        await circuit.failure()
        self.assertEqual((await circuit.snapshot())["state"], "open")

        now[0] = 10.0
        await circuit.before_call()
        await circuit.success()
        self.assertEqual(
            await circuit.snapshot(),
            {"state": "closed", "failures": 0, "retry_after_seconds": 0.0},
        )


class ScriptureServiceTestCase(unittest.IsolatedAsyncioTestCase):
    def build(
        self,
        settings: Settings | None = None,
        *,
        query: _QueryClient | None = None,
        search: _SearchClient | None = None,
        catalog: _Catalog | None = None,
    ) -> ScriptureService:
        service = ScriptureService(
            settings or _settings(),
            query_client=query or _QueryClient(),
            search_client=search or _SearchClient(),
            catalog=catalog or _Catalog(),
        )
        self.addAsyncCleanup(service.close)
        return service

    # ------------------------------------------------------------------ setup

    async def test_default_clients_are_built_from_settings(self) -> None:
        settings = _settings(
            GETBIBLE_QUERY_BASE_URL="https://query.example.test",
            GETBIBLE_SEARCH_BASE_URL="https://search.example.test",
            GETBIBLE_CONNECT_TIMEOUT="2",
            GETBIBLE_READ_TIMEOUT="5",
            SEARCH_TIMEOUT="12",
            SEARCH_MAX_RESPONSE_BYTES=str(128 * 1024),
            MAX_INPUT_LENGTH="64",
            MAX_TOTAL_VERSES="150",
            MAX_VERSES_PER_REFERENCE="100",
            TRANSLATION="aov",
        )
        service = ScriptureService(settings)
        self.addAsyncCleanup(service.close)

        self.assertEqual(service._query.base_url, "https://query.example.test/v2")
        self.assertEqual(service._query.translation, "aov")
        self.assertEqual(service._query.timeout_seconds, 7.0)
        self.assertEqual(service._query.max_references, 150)
        self.assertEqual(service._search.base_url, "https://search.example.test/v2")
        self.assertEqual(service._search.timeout_seconds, 12.0)
        self.assertEqual(service._search.max_response_bytes, 128 * 1024)
        self.assertEqual(service._search.max_query_length, 64)

        wide = ScriptureService(_settings(MAX_INPUT_LENGTH="1024"))
        self.addAsyncCleanup(wide.close)
        # The Search API accepts 500 characters; a wider robot limit is clamped.
        self.assertEqual(wide._search.max_query_length, 500)

    # ---------------------------------------------------------- resolve_query

    async def test_empty_reference_is_never_replaced_with_a_default(self) -> None:
        catalog = _Catalog()
        service = self.build(catalog=catalog)

        for arguments in ([], [""], ["   "]):
            with self.subTest(arguments=arguments), self.assertRaises(RobotInputError):
                await service.resolve_query(arguments)
        self.assertEqual(catalog.calls, [])

    async def test_ordinary_reference_does_not_probe_the_catalogue(self) -> None:
        catalog = _Catalog()
        service = self.build(catalog=catalog)

        query = await service.resolve_query(["John", "3:16"])

        self.assertEqual(query, ScriptureQuery("John 3:16", "kjv"))
        self.assertEqual(catalog.calls, [])

    async def test_ordinary_reference_accepts_a_saved_user_default(self) -> None:
        catalog = _Catalog()
        service = self.build(catalog=catalog)

        query = await service.resolve_query(["John", "3:16"], default_translation="ASV")

        self.assertEqual(query, ScriptureQuery("John 3:16", "asv"))
        self.assertEqual(catalog.calls, [])

    async def test_invalid_saved_default_is_refused(self) -> None:
        service = self.build()

        for default in ("../kjv", "", "A" * 31, "k j v"):
            with self.subTest(default=default), self.assertRaises(RobotInputError):
                await service.resolve_query(["John", "3:16"], default_translation=default)

    async def test_trailing_word_names_a_translation_only_when_published(self) -> None:
        catalog = _Catalog()
        service = self.build(catalog=catalog)

        self.assertEqual(
            await service.resolve_query(["John", "3:16", "aov"]),
            ScriptureQuery("John 3:16", "aov"),
        )
        self.assertEqual(
            await service.resolve_query(["Gen", "1:1", "CODEX"]),
            ScriptureQuery("Gen 1:1", "codex"),
        )
        # An unpublished word stays part of the reference: the Query API judges it.
        self.assertEqual(
            await service.resolve_query(["John", "3:16", "nope"]),
            ScriptureQuery("John 3:16 nope", "kjv"),
        )
        self.assertEqual(len(catalog.calls), 3)
        self.assertEqual(service.metrics.snapshot()["translation_checks"], 3)

    async def test_book_names_and_numbers_are_never_mistaken_for_translations(
        self,
    ) -> None:
        catalog = _Catalog()
        service = self.build(catalog=catalog)

        # A trailing chapter number cannot be a translation, so nothing is asked.
        self.assertEqual(
            await service.resolve_query(["1", "John", "3"]),
            ScriptureQuery("1 John 3", "kjv"),
        )
        self.assertEqual(catalog.calls, [])
        # A trailing book word looks like a code, is checked, and stays a reference.
        self.assertEqual(
            await service.resolve_query(["1", "John"]),
            ScriptureQuery("1 John", "kjv"),
        )
        self.assertEqual(catalog.calls, [("translations",)])
        # A lone word has nothing before it to be the reference of.
        self.assertEqual(
            await service.resolve_query(["aov"]),
            ScriptureQuery("aov", "kjv"),
        )
        self.assertEqual(len(catalog.calls), 1)

    async def test_static_bounds_are_enforced_before_any_request(self) -> None:
        query = _QueryClient()
        catalog = _Catalog()
        service = self.build(_settings(MAX_INPUT_LENGTH="256"), query=query, catalog=catalog)
        cases: list[tuple[str, type[RobotInputError]]] = [
            ("x" * 257, RequestLimitError),
            (";".join(["John 3:16"] * 9), RequestLimitError),
            ("John 3:16;;John 3:17", ReferenceValidationError),
            (";John 3:16", ReferenceValidationError),
            ("John 3:16;", ReferenceValidationError),
            ("John 3:16!", ReferenceValidationError),
            ("<b>John</b> 3:16", ReferenceValidationError),
            ("John 3:16\x00", ReferenceValidationError),
            ("---", ReferenceValidationError),
            ("J" * 101, ReferenceValidationError),
            ("John 1:16!;aov", ReferenceValidationError),
        ]

        for raw, expected in cases:
            with self.subTest(raw=raw[:24]), self.assertRaises(expected):
                await service.resolve_query([raw])

        self.assertEqual(query.calls, [])
        # Well-formed but absurd coordinates are the Query API's business.
        self.assertEqual(
            await service.resolve_query(["John", "1:1-999999999"]),
            ScriptureQuery("John 1:1-999999999", "kjv"),
        )
        self.assertEqual(
            await service.resolve_query([";".join(["John 3:16"] * 8)]),
            ScriptureQuery(";".join(["John 3:16"] * 8), "kjv"),
        )

    async def test_translation_exists_ignores_impossible_codes(self) -> None:
        catalog = _Catalog()
        service = self.build(catalog=catalog)

        self.assertFalse(await service.translation_exists("../kjv"))
        self.assertFalse(await service.translation_exists(""))
        self.assertEqual(catalog.calls, [])
        self.assertTrue(await service.translation_exists("KJV"))
        self.assertFalse(await service.translation_exists("asv"))
        self.assertEqual(len(catalog.calls), 2)
        self.assertEqual(service.metrics.snapshot()["translation_checks"], 2)

    # ----------------------------------------------------------------- select

    async def test_select_returns_the_query_document(self) -> None:
        query = _QueryClient()
        settings = _settings(MAX_TOTAL_VERSES="120", MAX_VERSES_PER_REFERENCE="100")
        service = self.build(settings, query=query)

        document = await service.select(ScriptureQuery("John 3:16", "aov"))

        self.assertEqual(document, _scripture_document("aov"))
        self.assertEqual(query.calls, [("John 3:16", "aov", 120)])
        snapshot = await service.snapshot()
        self.assertEqual(snapshot["metrics"], {"scripture_lookups": 1})
        self.assertEqual(snapshot["circuit"]["state"], "closed")
        self.assertEqual(snapshot["circuit"]["failures"], 0)

    async def test_select_classifies_query_api_refusals_as_input_errors(self) -> None:
        cases: list[tuple[BaseException, type[RobotInputError | TranslationNotFoundError]]] = [
            (QueryHTTPError(404, code="invalid_reference"), ReferenceValidationError),
            (QueryHTTPError(404, code="not_found"), ReferenceValidationError),
            (QueryHTTPError(404, code="missing_reference"), ReferenceValidationError),
            (QueryHTTPError(400, code="parameters_not_accepted"), ReferenceValidationError),
            (QueryHTTPError(404, code="translation_not_found"), TranslationNotFoundError),
            (QueryHTTPError(400, code="request_limit"), RequestLimitError),
            (QueryLimitError("The selection exceeds the 100-verse limit."), RequestLimitError),
            (QueryInputError("The Scripture reference is invalid."), ReferenceValidationError),
        ]
        settings = replace(_settings(), request_retries=3, circuit_failure_threshold=1)

        for error, expected in cases:
            with self.subTest(error=str(error)):
                query = _QueryClient(error)
                service = self.build(settings, query=query)
                with self.assertRaises(expected):
                    await service.select(ScriptureQuery("John 3:16", "kjv"))
                # A refused request is never retried and never counts against
                # the upstream: the API answered, the input was wrong.
                self.assertEqual(len(query.calls), 1)
                snapshot = await service.snapshot()
                self.assertEqual(snapshot["circuit"], {
                    "state": "closed",
                    "failures": 0,
                    "retry_after_seconds": 0.0,
                })
                self.assertNotIn("repository_failures", snapshot["metrics"])
                self.assertTrue(await service.ready())

    async def test_request_limit_message_names_the_configured_bound(self) -> None:
        query = _QueryClient(QueryLimitError("too many"))
        service = self.build(
            _settings(MAX_TOTAL_VERSES="150", MAX_VERSES_PER_REFERENCE="100"),
            query=query,
        )

        with self.assertRaisesRegex(RequestLimitError, "150 verses"):
            await service.select(ScriptureQuery("Psalm 119", "kjv"))

    async def test_transport_faults_are_retried_then_fail_the_circuit(self) -> None:
        query = _QueryClient(QueryTransportError(), QueryTransportError(), QueryTransportError())
        settings = replace(_settings(), request_retries=2)
        service = self.build(settings, query=query)

        with (
            patch("modules.service._backoff", return_value=0.0),
            self.assertRaises(ScriptureUnavailable),
        ):
            await service.select(ScriptureQuery("John 3:16", "kjv"))

        self.assertEqual(len(query.calls), 3)
        snapshot = await service.snapshot()
        self.assertEqual(snapshot["circuit"]["failures"], 1)
        self.assertEqual(snapshot["metrics"], {"repository_failures": 1})

    def test_retry_backoff_doubles_and_is_capped_at_one_second(self) -> None:
        # The tests above patch the schedule away to stay fast; pin it here so a
        # retry never sleeps longer than a reader would wait for a reference.
        self.assertEqual([_backoff(attempt) for attempt in range(5)], [0.25, 0.5, 1.0, 1.0, 1.0])

    async def test_transient_transport_fault_recovers_within_the_retry_budget(self) -> None:
        query = _QueryClient(QueryTransportError())
        service = self.build(replace(_settings(), request_retries=1), query=query)

        with patch("modules.service._backoff", return_value=0.0):
            document = await service.select(ScriptureQuery("John 3:16", "kjv"))

        self.assertEqual(document, _scripture_document("kjv"))
        self.assertEqual(len(query.calls), 2)
        self.assertEqual((await service.snapshot())["circuit"]["failures"], 0)

    async def test_retryable_status_is_retried_then_reported_unavailable(self) -> None:
        query = _QueryClient(QueryHTTPError(503, code="busy"), QueryHTTPError(429))
        service = self.build(replace(_settings(), request_retries=1), query=query)

        with (
            patch("modules.service._backoff", return_value=0.0),
            self.assertRaises(ScriptureUnavailable),
        ):
            await service.select(ScriptureQuery("John 3:16", "kjv"))

        self.assertEqual(len(query.calls), 2)
        self.assertEqual((await service.snapshot())["circuit"]["failures"], 1)

    async def test_status_then_transport_fault_shares_one_retry_budget(self) -> None:
        query = _QueryClient(QueryHTTPError(502), QueryTransportError())
        service = self.build(replace(_settings(), request_retries=1), query=query)

        with (
            patch("modules.service._backoff", return_value=0.0),
            self.assertRaises(ScriptureUnavailable),
        ):
            await service.select(ScriptureQuery("John 3:16", "kjv"))

        self.assertEqual(len(query.calls), 2)

    async def test_unusable_upstream_answers_are_not_retried(self) -> None:
        cases: list[BaseException] = [
            QueryHTTPError(404, code="unknown_version"),
            QueryHTTPError(405),
            QueryResponseError("The GetBible Query API returned malformed JSON."),
        ]
        settings = replace(_settings(), request_retries=3)

        for error in cases:
            with self.subTest(error=str(error)):
                query = _QueryClient(error)
                service = self.build(settings, query=query)
                with self.assertRaises(ScriptureUnavailable):
                    await service.select(ScriptureQuery("John 3:16", "kjv"))
                self.assertEqual(len(query.calls), 1)
                snapshot = await service.snapshot()
                self.assertEqual(snapshot["circuit"]["failures"], 1)
                self.assertEqual(snapshot["metrics"], {"repository_failures": 1})

    async def test_unexpected_worker_failures_are_contained(self) -> None:
        for error, metric in (
            (RuntimeError("bug"), "unexpected_failures"),
            (OSError("socket"), "repository_failures"),
        ):
            with self.subTest(metric=metric):
                query = _QueryClient(error)
                service = self.build(query=query)
                with self.assertRaises(ScriptureUnavailable):
                    await service.select(ScriptureQuery("John 3:16", "kjv"))
                snapshot = await service.snapshot()
                self.assertEqual(snapshot["metrics"], {metric: 1})
                self.assertEqual(snapshot["circuit"]["failures"], 1)

    async def test_timeout_opens_circuit_and_followup_fails_fast(self) -> None:
        query = _QueryClient(delay=0.05)
        settings = replace(
            _settings(),
            lookup_timeout=0.01,
            circuit_failure_threshold=1,
            circuit_recovery_seconds=60.0,
        )
        service = self.build(settings, query=query)
        query_reference = ScriptureQuery("John 3:16", "kjv")

        with self.assertRaises(ScriptureUnavailable):
            await service.select(query_reference)
        with self.assertRaises(CircuitOpen):
            await service.select(query_reference)

        snapshot = await service.snapshot()
        self.assertEqual(snapshot["metrics"], {"lookup_timeouts": 1, "circuit_rejections": 1})
        self.assertEqual(snapshot["circuit"]["state"], "open")
        self.assertFalse(await service.ready())

    async def test_timed_out_worker_keeps_capacity_until_thread_finishes(self) -> None:
        started = threading.Event()
        release = threading.Event()
        query = _QueryClient(started=started, release=release)
        settings = replace(
            _settings(),
            lookup_timeout=0.01,
            queue_timeout=0.01,
            max_concurrent_lookups=1,
            circuit_failure_threshold=5,
        )
        service = self.build(settings, query=query)
        query_reference = ScriptureQuery("John 3:16", "kjv")

        try:
            with self.assertRaises(ScriptureUnavailable):
                await service.select(query_reference)
            self.assertTrue(started.is_set())
            with self.assertRaises(RobotBusy):
                await service.select(query_reference)
        finally:
            release.set()
        self.assertEqual(
            (await service.snapshot())["metrics"],
            {"lookup_timeouts": 1, "queue_rejections": 1},
        )

    async def test_cancelled_lookup_releases_the_probe_but_not_the_running_permit(
        self,
    ) -> None:
        started = threading.Event()
        release = threading.Event()
        query = _QueryClient(started=started, release=release)
        settings = replace(_settings(), max_concurrent_lookups=1, queue_timeout=0.2)
        service = self.build(settings, query=query)
        query_reference = ScriptureQuery("John 3:16", "kjv")

        task = asyncio.create_task(service.select(query_reference))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # The abandoned worker still holds its permit until it really exits.
        with self.assertRaises(RobotBusy):
            await service.select(query_reference)
        release.set()

        document = await service.select(query_reference)
        self.assertEqual(document, _scripture_document("kjv"))
        snapshot = await service.snapshot()
        self.assertEqual(snapshot["circuit"]["state"], "closed")
        self.assertEqual(snapshot["metrics"]["scripture_lookups"], 1)

    # -------------------------------------------------------------- catalogue

    async def test_identical_catalog_reads_share_one_in_flight_request(self) -> None:
        service = self.build()
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def books(translation: str) -> tuple[BookOption, ...]:
            nonlocal calls
            calls += 1
            started.set()
            release.wait(timeout=1)
            return (BookOption(43, "John", "a" * 40),)

        service._catalog.books = books
        first = asyncio.create_task(service.books("kjv"))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        second = asyncio.create_task(service.books("kjv"))
        await asyncio.sleep(0)
        release.set()

        left, right = await asyncio.gather(first, second)
        self.assertEqual(calls, 1)
        self.assertIs(left, right)
        self.assertEqual(service._catalog_flights, {})

    async def test_navigation_reaches_only_the_catalogue(self) -> None:
        query = _QueryClient()
        search = _SearchClient()
        catalog = _Catalog()
        service = self.build(query=query, search=search, catalog=catalog)

        translations = await service.translations()
        books = await service.books("KJV")
        chapters = await service.chapters("kjv", JOHN)
        chapter = await service.chapter("kjv", JOHN, JOHN_3)

        self.assertEqual(translations, catalog.options)
        self.assertEqual(books, (JOHN,))
        self.assertEqual(chapters, (JOHN_3,))
        self.assertEqual(chapter.reference, "John 3")
        self.assertEqual(
            catalog.calls,
            [
                ("translations",),
                ("books", "KJV"),
                ("chapters", "kjv", 43),
                ("chapter", "kjv", 43, 3),
            ],
        )
        self.assertEqual(query.calls, [])
        self.assertEqual(search.calls, [])
        self.assertEqual(
            service.metrics.snapshot(),
            {
                "catalog_translation_lookups": 1,
                "catalog_book_lookups": 1,
                "catalog_chapter_lookups": 1,
                "catalog_scripture_chapter_lookups": 1,
            },
        )
        self.assertEqual(service._catalog_flights, {})

    async def test_catalog_failures_count_against_the_repository_circuit(self) -> None:
        catalog = _Catalog(fail=RepositoryError("Main API unreachable"))
        service = self.build(catalog=catalog)

        with self.assertRaises(ScriptureUnavailable):
            await service.translations()
        # A failed flight is forgotten, not cached: the next reader retries.
        with self.assertRaises(ScriptureUnavailable):
            await service.translations()

        self.assertEqual(len(catalog.calls), 2)
        self.assertEqual(service._catalog_flights, {})
        snapshot = await service.snapshot()
        self.assertEqual(snapshot["circuit"]["failures"], 2)
        self.assertEqual(snapshot["metrics"], {"repository_failures": 2})

    async def test_cancelled_catalog_flight_is_forgotten(self) -> None:
        started = threading.Event()
        release = threading.Event()
        catalog = _Catalog(started=started, release=release)
        service = self.build(catalog=catalog)

        first = asyncio.create_task(service.books("kjv"))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        flight = service._catalog_flights[("books", "kjv")]
        flight.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first

        self.assertEqual(service._catalog_flights, {})
        self.assertEqual((await service.snapshot())["circuit"]["state"], "closed")

    # ----------------------------------------------------------------- search

    async def test_search_forwards_the_reader_criteria_unchanged(self) -> None:
        search = _SearchClient()
        service = self.build(search=search)
        options = SearchOptions(
            translation="aov",
            words="all",
            match="substring",
            scope="new_testament",
            case_sensitive=True,
            diacritics="exact",
            sort="relevance",
            books=(43, 62),
            exclude=("hate", "world"),
            proximity=5,
        )

        page = await service.search("liefde", options)

        self.assertEqual(len(search.calls), 1)
        query, translation, criteria = search.calls[0]
        self.assertEqual(query, "liefde")
        self.assertEqual(translation, "aov")
        self.assertEqual(criteria.words, "all")
        self.assertEqual(criteria.match, "substring")
        self.assertEqual(criteria.scope, "new_testament")
        self.assertTrue(criteria.case_sensitive)
        self.assertEqual(criteria.diacritics, "exact")
        self.assertEqual(criteria.sort, "relevance")
        self.assertEqual(criteria.books, (43, 62))
        self.assertEqual(criteria.exclude, ("hate", "world"))
        self.assertEqual(criteria.proximity, 5)
        self.assertEqual(criteria.limit, 50)
        self.assertEqual(criteria.offset, 0)
        self.assertEqual(page.query, "liefde")
        self.assertEqual(page.translation, "aov")
        self.assertEqual(page.kind, "search")
        self.assertEqual(page.total, 1)
        self.assertEqual(page.offset, 0)
        self.assertFalse(page.has_more)
        self.assertEqual(page.items[0].reference, "John 3:16")
        self.assertEqual(page.items[0].book_number, 43)
        self.assertEqual(page.items[0].book_name, "John")
        self.assertEqual(page.items[0].chapter, 3)
        self.assertEqual(page.items[0].verse, 16)
        self.assertEqual(page.items[0].text, VERSE_TEXT)
        self.assertEqual(page.items[0].terms, ("loved",))
        self.assertEqual(service.metrics.snapshot(), {"scripture_searches": 1})

    async def test_search_never_rewrites_the_requested_match_mode(self) -> None:
        """The search service reads the writing system itself; the robot must not guess.

        Under the old in-process engine a single Han character flipped the whole
        query to substring, which also loosened its space-delimited terms, so
        ``all`` matched inside ``shall``. Every script must reach the Search API
        exactly as asked.
        """
        search = _SearchClient()
        service = self.build(search=search)
        queries = (
            "神",  # Han
            "イエス",  # Katakana
            "예수",  # Hangul
            "พระ",  # Thai
            "ພຣະ",  # Lao
            "ព្រះ",  # Khmer
            "ယေရှု",  # Myanmar
            "المسيح",  # Arabic
            "משיח",  # Hebrew
            "यीशु",  # Devanagari
            "Jesus",  # Latin
            "Jesus 耶稣",  # mixed Latin and Han
        )

        for requested in ("whole_word", "substring"):
            for query in queries:
                with self.subTest(query=query, match=requested):
                    await service.search(
                        query,
                        SearchOptions(translation="kjv", match=requested),
                    )
                    sent_query, _, criteria = search.calls[-1]
                    self.assertEqual(sent_query, query)
                    self.assertEqual(criteria.match, requested)

    async def test_search_passes_the_requested_diacritics_policy(self) -> None:
        search = _SearchClient()
        service = self.build(search=search)

        for policy in ("fold", "exact"):
            with self.subTest(diacritics=policy):
                await service.search(
                    "λογος",
                    SearchOptions(translation="moderngreek", diacritics=policy),
                )
                criteria = search.calls[-1][2]
                self.assertEqual(criteria.diacritics, policy)

    async def test_search_limit_is_clamped_to_the_search_api_maximum(self) -> None:
        for configured, expected in (("200", 100), ("100", 100), ("25", 25), ("1", 1)):
            with self.subTest(configured=configured):
                search = _SearchClient()
                service = self.build(_settings(SEARCH_RESULT_LIMIT=configured), search=search)
                await service.search("loved", SearchOptions(translation="kjv"))
                self.assertEqual(search.calls[-1][2].limit, expected)

    async def test_search_offset_is_forwarded_and_echoed(self) -> None:
        envelope = _search_envelope()
        envelope["query"]["offset"] = 50
        envelope["query"]["has_more"] = True
        search = _SearchClient(envelope)
        service = self.build(search=search)

        page = await service.search("loved", SearchOptions(translation="kjv"), offset=50)

        self.assertEqual(search.calls[-1][2].offset, 50)
        self.assertEqual(page.offset, 50)
        self.assertTrue(page.has_more)

    async def test_invalid_criteria_are_refused_before_the_request(self) -> None:
        search = _SearchClient()
        service = self.build(search=search)

        with self.assertRaises(SearchValidationError):
            await service.search(
                "loved",
                SearchOptions(translation="kjv", words="any", proximity=3),
            )
        with self.assertRaises(SearchValidationError):
            await service.search("loved", SearchOptions(translation="kjv"), offset=10_001)
        with self.assertRaises(SearchValidationError):
            await service.search("loved", SearchOptions(translation="kjv", books=(0,)))

        self.assertEqual(search.calls, [])
        snapshot = await service.snapshot()
        self.assertEqual(snapshot["search_circuit"]["failures"], 0)
        self.assertEqual(snapshot["metrics"], {})

    async def test_search_client_refusals_are_validation_errors(self) -> None:
        search = _SearchClient(SearchInputError("Search words are required."))
        service = self.build(search=search)

        with self.assertRaisesRegex(SearchValidationError, "Search words are required"):
            await service.search("   ", SearchOptions(translation="kjv"))

        snapshot = await service.snapshot()
        self.assertEqual(snapshot["search_circuit"]["failures"], 0)
        self.assertEqual(snapshot["metrics"], {})

    async def test_search_classifies_search_api_problems(self) -> None:
        cases: list[tuple[SearchHTTPError, type[Exception], str]] = [
            (
                SearchHTTPError(400, code="invalid_search", detail="Query too vague."),
                SearchValidationError,
                "Query too vague",
            ),
            (
                SearchHTTPError(400, code="missing_search"),
                SearchValidationError,
                "rejected",
            ),
            (
                SearchHTTPError(404, code="not_found"),
                SearchValidationError,
                "rejected",
            ),
            (
                SearchHTTPError(404, code="translation_not_found"),
                TranslationNotFoundError,
                r"Translation \(kjv\) not found",
            ),
        ]

        for error, expected, message in cases:
            with self.subTest(error=str(error)):
                search = _SearchClient(error)
                service = self.build(search=search)
                with self.assertRaisesRegex(expected, message):
                    await service.search("loved", SearchOptions(translation="kjv"))
                self.assertEqual(len(search.calls), 1)
                snapshot = await service.snapshot()
                self.assertEqual(snapshot["search_circuit"]["failures"], 0)
                self.assertEqual(snapshot["circuit"]["failures"], 0)

    async def test_search_outages_open_only_the_search_circuit(self) -> None:
        settings = replace(
            _settings(),
            circuit_failure_threshold=1,
            circuit_recovery_seconds=60.0,
        )
        cases: list[tuple[BaseException, str]] = [
            (SearchHTTPError(429, code="rate_limited", retry_after=5), "repository_failures"),
            (SearchHTTPError(503, code="busy"), "repository_failures"),
            (SearchHTTPError(404, code="unknown_version"), "repository_failures"),
            (SearchTransportError(), "repository_failures"),
            (SearchResponseError("malformed"), "repository_failures"),
            (OSError("socket"), "repository_failures"),
            (RuntimeError("bug"), "unexpected_failures"),
        ]

        for error, metric in cases:
            with self.subTest(error=str(error)):
                query = _QueryClient()
                search = _SearchClient(error)
                service = self.build(settings, query=query, search=search)

                with self.assertRaises(ScriptureUnavailable):
                    await service.search("loved", SearchOptions(translation="kjv"))
                # Direct Scripture keeps working: the Query API is another upstream.
                selected = await service.select(ScriptureQuery("John 3:16", "kjv"))
                self.assertIn("kjv_43_3", selected)
                self.assertTrue(await service.ready())
                with self.assertRaises(CircuitOpen):
                    await service.search("loved", SearchOptions(translation="kjv"))

                snapshot = await service.snapshot()
                self.assertEqual(snapshot["circuit"]["state"], "closed")
                self.assertEqual(snapshot["circuit"]["failures"], 0)
                self.assertEqual(snapshot["search_circuit"]["state"], "open")
                self.assertEqual(snapshot["search_circuit"]["failures"], 1)
                self.assertEqual(
                    snapshot["metrics"],
                    {metric: 1, "scripture_lookups": 1, "circuit_rejections": 1},
                )
                self.assertEqual(len(search.calls), 1)

    async def test_search_waits_on_the_search_budget_not_the_lookup_budget(self) -> None:
        # The Search API's own deadline is longer than the reference-delivery
        # budget. Charging a search the shorter budget failed the searcher while
        # the request they triggered ran on to completion without them.
        search = _SearchClient(delay=0.2)
        settings = replace(_settings(), lookup_timeout=0.01, search_timeout=0.5)
        service = self.build(settings, search=search)

        page = await service.search("loved", SearchOptions(translation="kjv"))

        self.assertEqual(page.translation, "kjv")
        self.assertEqual(len(search.calls), 1)
        self.assertNotIn("lookup_timeouts", service.metrics.snapshot())

    async def test_search_still_gives_up_once_its_own_budget_is_spent(self) -> None:
        started = threading.Event()
        release = threading.Event()
        search = _SearchClient(started=started, release=release)
        settings = replace(
            _settings(),
            lookup_timeout=30.0,
            search_timeout=0.01,
            circuit_failure_threshold=5,
        )
        service = self.build(settings, search=search)

        begun = time.monotonic()
        try:
            with self.assertRaises(ScriptureUnavailable):
                await service.search("loved", SearchOptions(translation="kjv"))
        finally:
            release.set()
        elapsed = time.monotonic() - begun

        # The bound is the search deadline plus one second of grace, never the
        # thirty-second lookup budget.
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertLess(elapsed, 10.0)
        snapshot = await service.snapshot()
        self.assertEqual(snapshot["metrics"], {"lookup_timeouts": 1})
        self.assertEqual(snapshot["search_circuit"]["failures"], 1)
        self.assertEqual(snapshot["circuit"]["failures"], 0)

    async def test_slow_search_does_not_consume_lookup_capacity(self) -> None:
        started = threading.Event()
        release = threading.Event()
        search = _SearchClient(started=started, release=release)
        settings = replace(_settings(), max_concurrent_lookups=1, max_concurrent_searches=1)
        service = self.build(settings, search=search)

        pending = asyncio.create_task(service.search("loved", SearchOptions(translation="kjv")))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        selected = await service.select(ScriptureQuery("John 3:16", "kjv"))
        self.assertFalse(pending.done())
        release.set()
        page = await pending

        self.assertIn("kjv_43_3", selected)
        self.assertEqual(page.total, 1)

    async def test_slow_lookup_does_not_consume_search_capacity(self) -> None:
        started = threading.Event()
        release = threading.Event()
        query = _QueryClient(started=started, release=release)
        settings = replace(_settings(), max_concurrent_lookups=1, max_concurrent_searches=1)
        service = self.build(settings, query=query)

        pending = asyncio.create_task(service.select(ScriptureQuery("John 3:16", "kjv")))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        page = await service.search("loved", SearchOptions(translation="kjv"))
        self.assertFalse(pending.done())
        release.set()
        selected = await pending

        self.assertEqual(page.total, 1)
        self.assertIn("kjv_43_3", selected)

    async def test_search_queue_rejections_are_counted_on_their_own(self) -> None:
        started = threading.Event()
        release = threading.Event()
        search = _SearchClient(started=started, release=release)
        settings = replace(_settings(), max_concurrent_searches=1, queue_timeout=0.01)
        service = self.build(settings, search=search)

        pending = asyncio.create_task(service.search("loved", SearchOptions(translation="kjv")))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        try:
            with self.assertRaises(RobotBusy):
                await service.search("grace", SearchOptions(translation="kjv"))
        finally:
            release.set()
        await pending

        metrics = service.metrics.snapshot()
        self.assertEqual(metrics["search_queue_rejections"], 1)
        self.assertNotIn("queue_rejections", metrics)

    async def test_search_page_keeps_the_match_order_and_reference_answers(self) -> None:
        service = self.build()

        def two_chapter_envelope() -> dict:
            envelope = _search_envelope()
            envelope["results"]["kjv_1_1"] = _chapter(
                "kjv", 1, "Genesis", 1, [(1, "  In the beginning.  ")]
            )
            envelope["matches"].append(
                {
                    "reference": " Genesis 1:1 ",
                    "book_nr": 1,
                    "chapter": 1,
                    "verse": 1,
                    "terms": [" beginning "],
                }
            )
            envelope["query"]["total"] = 2
            envelope["query"]["returned"] = 2
            return envelope

        ordered = two_chapter_envelope()
        page = service._search_page(ordered, "loved", "kjv", 50)
        self.assertEqual(
            [item.reference for item in page.items],
            ["John 3:16", "Genesis 1:1"],
        )
        self.assertEqual(page.items[1].text, "In the beginning.")
        self.assertEqual(page.items[1].terms, ("beginning",))

        reversed_matches = two_chapter_envelope()
        reversed_matches["matches"].reverse()
        page = service._search_page(reversed_matches, "loved", "kjv", 50)
        self.assertEqual(
            [item.reference for item in page.items],
            ["Genesis 1:1", "John 3:16"],
        )

        # A reference-kind answer has no terms and no pagination of its own.
        reference = _search_envelope()
        reference["query"]["kind"] = "reference"
        reference["query"]["offset"] = 7
        reference["query"]["has_more"] = True
        del reference["matches"][0]["terms"]
        page = service._search_page(reference, "John 3:16", "kjv", 50)
        self.assertEqual(page.kind, "reference")
        self.assertEqual(page.offset, 0)
        self.assertFalse(page.has_more)
        self.assertEqual(page.items[0].terms, ())

        explicit_null = _search_envelope()
        explicit_null["matches"][0]["terms"] = None
        page = service._search_page(explicit_null, "loved", "kjv", 50)
        self.assertEqual(page.items[0].terms, ())

        bounded_total = _search_envelope()
        bounded_total["query"]["total"] = 1_000_000
        page = service._search_page(bounded_total, "loved", "kjv", 50)
        self.assertEqual(page.total, 1_000_000)

        # Metadata the search kind may omit falls back to a first page.
        minimal = _search_envelope()
        del minimal["query"]["offset"]
        del minimal["query"]["has_more"]
        page = service._search_page(minimal, "loved", "kjv", 50)
        self.assertEqual((page.offset, page.has_more), (0, False))

    async def test_search_page_rejects_malformed_envelopes(self) -> None:
        service = self.build()

        def set_query(field: str, value: object) -> Callable[[dict], object]:
            def mutate(envelope: dict) -> dict:
                envelope["query"][field] = value
                return envelope

            return mutate

        def set_chapter(field: str, value: object) -> Callable[[dict], object]:
            def mutate(envelope: dict) -> dict:
                envelope["results"]["kjv_43_3"][field] = value
                return envelope

            return mutate

        def set_verse(field: str, value: object) -> Callable[[dict], object]:
            def mutate(envelope: dict) -> dict:
                envelope["results"]["kjv_43_3"]["verses"][0][field] = value
                return envelope

            return mutate

        def set_match(field: str, value: object) -> Callable[[dict], object]:
            def mutate(envelope: dict) -> dict:
                envelope["matches"][0][field] = value
                return envelope

            return mutate

        def duplicate_chapter(envelope: dict) -> dict:
            envelope["results"]["kjv_43_3_again"] = dict(envelope["results"]["kjv_43_3"])
            return envelope

        def duplicate_match(envelope: dict) -> dict:
            envelope["matches"].append(dict(envelope["matches"][0]))
            envelope["query"]["total"] = 2
            return envelope

        def extra_verse(envelope: dict) -> dict:
            envelope["results"]["kjv_43_3"]["verses"].append(
                {"chapter": 3, "verse": 17, "name": "John 3:17", "text": "Sent."}
            )
            return envelope

        cases: list[tuple[str, Callable[[dict], object], int]] = [
            ("not an object", lambda envelope: ["not", "an", "object"], 50),
            ("query missing", lambda envelope: {"results": {}, "matches": []}, 50),
            ("query not object", lambda envelope: {**envelope, "query": "search"}, 50),
            ("results not object", lambda envelope: {**envelope, "results": []}, 50),
            ("matches not list", lambda envelope: {**envelope, "matches": {}}, 50),
            ("unknown kind", set_query("kind", "fuzzy"), 50),
            ("total too large", set_query("total", 1_000_001), 50),
            ("total below returned matches", set_query("total", 0), 50),
            ("total boolean", set_query("total", True), 50),
            ("total negative", set_query("total", -1), 50),
            ("total text", set_query("total", "1"), 50),
            ("offset negative", set_query("offset", -1), 50),
            ("offset boolean", set_query("offset", True), 50),
            ("offset text", set_query("offset", "0"), 50),
            ("has_more not boolean", set_query("has_more", "yes"), 50),
            ("more matches than the page", duplicate_match, 1),
            ("more chapters than the page", duplicate_chapter, 1),
            ("more verses than the page", extra_verse, 1),
            ("chapter not object", lambda envelope: {**envelope, "results": {"x": []}}, 50),
            ("chapter book number zero", set_chapter("book_nr", 0), 50),
            ("chapter book number boolean", set_chapter("book_nr", True), 50),
            ("chapter book name blank", set_chapter("book_name", "  "), 50),
            ("chapter book name too long", set_chapter("book_name", "J" * 129), 50),
            ("chapter number zero", set_chapter("chapter", 0), 50),
            ("chapter number boolean", set_chapter("chapter", True), 50),
            ("chapter verses not list", set_chapter("verses", {}), 50),
            ("verse not object", set_chapter("verses", ["16"]), 50),
            ("verse number zero", set_verse("verse", 0), 50),
            ("verse number boolean", set_verse("verse", True), 50),
            ("verse number too large", set_verse("verse", 2001), 50),
            ("verse text blank", set_verse("text", "  "), 50),
            ("verse text not text", set_verse("text", 16), 50),
            ("duplicate verse data", duplicate_chapter, 50),
            ("match not object", lambda envelope: {**envelope, "matches": ["x"]}, 50),
            ("match reference blank", set_match("reference", " "), 50),
            ("match reference too long", set_match("reference", "x" * 257), 50),
            ("match reference not text", set_match("reference", 43), 50),
            ("match book number boolean", set_match("book_nr", True), 50),
            ("match book number too large", set_match("book_nr", 1001), 50),
            ("match chapter zero", set_match("chapter", 0), 50),
            ("match chapter boolean", set_match("chapter", True), 50),
            ("match verse too large", set_match("verse", 2001), 50),
            ("match verse boolean", set_match("verse", True), 50),
            ("match terms not list", set_match("terms", "loved"), 50),
            ("match too many terms", set_match("terms", ["t"] * 65), 50),
            ("match term not text", set_match("terms", [1]), 50),
            ("match term blank", set_match("terms", [" "]), 50),
            ("match term too long", set_match("terms", ["t" * 257]), 50),
            ("duplicate match", duplicate_match, 50),
            ("match without verse data", set_match("verse", 17), 50),
        ]

        for label, mutate, limit in cases:
            with self.subTest(case=label), self.assertRaises(ScriptureUnavailable):
                service._search_page(mutate(_search_envelope()), "loved", "kjv", limit)

    async def test_malformed_search_pages_are_refused_after_the_bounded_call(self) -> None:
        envelope = _search_envelope()
        envelope["query"]["total"] = 1_000_001
        search = _SearchClient(envelope)
        service = self.build(replace(_settings(), circuit_failure_threshold=1), search=search)

        with self.assertRaises(ScriptureUnavailable):
            await service.search("loved", SearchOptions(translation="kjv"))

        # The client already checked the envelope shape inside the worker; the
        # deeper page validation runs once the bounded call has returned, so an
        # answer that reached this far counts as a completed search and the
        # reader is told the service failed safely rather than being locked out.
        snapshot = await service.snapshot()
        self.assertEqual(snapshot["search_circuit"]["state"], "closed")
        self.assertEqual(snapshot["metrics"], {"scripture_searches": 1})
        self.assertEqual(len(search.calls), 1)

    # ------------------------------------------------------- snapshot / close

    async def test_snapshot_exposes_only_the_two_circuits(self) -> None:
        service = self.build()

        snapshot = await service.snapshot()

        self.assertEqual(set(snapshot), {"closed", "metrics", "circuit", "search_circuit"})
        self.assertFalse(snapshot["closed"])
        self.assertEqual(snapshot["metrics"], {})
        for circuit in (snapshot["circuit"], snapshot["search_circuit"]):
            self.assertEqual(
                circuit,
                {"state": "closed", "failures": 0, "retry_after_seconds": 0.0},
            )
        # The robot runs no search engine and keeps no corpus, so neither a
        # Librarian block nor an engine version can honestly be reported.
        self.assertNotIn("librarian", snapshot)
        self.assertNotIn("search_engine_version", snapshot)
        self.assertFalse(hasattr(service, "warm_default_translation"))

    async def test_ready_tracks_the_repository_circuit_and_closure(self) -> None:
        settings = replace(_settings(), circuit_failure_threshold=1)
        query = _QueryClient(OSError("socket"))
        service = self.build(settings, query=query)

        self.assertTrue(await service.ready())
        with self.assertRaises(ScriptureUnavailable):
            await service.select(ScriptureQuery("John 3:16", "kjv"))
        self.assertFalse(await service.ready())

        await service.close()
        self.assertFalse(await service.ready())
        self.assertTrue((await service.snapshot())["closed"])

    async def test_close_refuses_new_work_and_shuts_both_executors(self) -> None:
        query = _QueryClient()
        search = _SearchClient()
        service = self.build(query=query, search=search)

        await service.close()
        await service.close()

        with self.assertRaises(ScriptureUnavailable):
            await service.select(ScriptureQuery("John 3:16", "kjv"))
        with self.assertRaises(ScriptureUnavailable):
            await service.search("loved", SearchOptions(translation="kjv"))
        with self.assertRaises(ScriptureUnavailable):
            await service.translations()
        for executor in (service._executor, service._search_executor):
            with self.assertRaisesRegex(RuntimeError, "after shutdown"):
                executor.submit(time.sleep, 0)
        self.assertEqual(query.calls, [])
        self.assertEqual(search.calls, [])
        self.assertEqual(service.metrics.snapshot(), {})


if __name__ == "__main__":
    unittest.main()
