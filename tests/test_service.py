import asyncio
import os
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from getbible import (
    SEARCH_ENGINE_VERSION,
    ReferenceValidationError,
    RequestLimitError,
)
from getbible.search import shared_registry

from config import Settings
from modules.catalog import BookOption
from modules.errors import CircuitOpen, RobotBusy, ScriptureUnavailable
from modules.interactions import SearchOptions
from modules.service import CircuitBreaker, ScriptureQuery, ScriptureService


class _Client:
    def __init__(
        self,
        *,
        delay: float = 0.0,
        fail: bool = False,
        search_delay: float = 0.0,
        search_fail: bool = False,
        select_started: threading.Event | None = None,
        select_release: threading.Event | None = None,
        search_started: threading.Event | None = None,
        search_release: threading.Event | None = None,
    ) -> None:
        self.delay = delay
        self.fail = fail
        self.search_delay = search_delay
        self.search_fail = search_fail
        self.select_started = select_started
        self.select_release = select_release
        self.search_started = search_started
        self.search_release = search_release
        self.translation_calls: list[str] = []
        self.search_calls: list[tuple[str, str, object]] = []
        self.warm_calls: list[str] = []
        self.warm_policies: list[tuple[bool, str]] = []
        self.closed = False

    def valid_translation(self, code: str) -> bool:
        self.translation_calls.append(code)
        return code in {"kjv", "aov", "codex"}

    def select(self, references: str, translation: str) -> dict:
        if self.select_started is not None:
            self.select_started.set()
        if self.select_release is not None:
            self.select_release.wait(timeout=1)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise OSError("upstream failed")
        return {
            f"{translation}_43_3": {
                "book_name": "John",
                "abbreviation": translation,
                "chapter": 3,
                "verses": [{"verse": 16, "text": "For God so loved the world."}],
            }
        }

    def search(self, query: str, translation: str, criteria: object) -> dict:
        if self.search_started is not None:
            self.search_started.set()
        if self.search_release is not None:
            self.search_release.wait(timeout=1)
        if self.search_delay:
            time.sleep(self.search_delay)
        if self.search_fail:
            raise OSError("search corpus unavailable")
        self.search_calls.append((query, translation, criteria))
        return {
            "query": {"total": 1},
            "results": {
                f"{translation}_43_3": {
                    "book_nr": 43,
                    "book_name": "John",
                    "chapter": 3,
                    "verses": [
                        {
                            "verse": 16,
                            "text": "For God so loved the world.",
                        }
                    ],
                }
            },
            "matches": [
                {
                    "reference": "John 3:16",
                    "book_nr": 43,
                    "chapter": 3,
                    "verse": 16,
                    "terms": ["loved"],
                }
            ],
        }

    def warm_translation(
        self,
        translation: str,
        *,
        case_sensitive: bool = False,
        diacritics: str = "sensitive",
    ) -> dict:
        # The hardened Librarian facade still defaults this to the 1.x spelling,
        # which resolves to `exact`. The fake keeps that default so a caller that
        # stops passing the policy explicitly is caught here.
        self.warm_calls.append(translation)
        self.warm_policies.append((case_sensitive, diacritics))
        fold = diacritics in {"fold", "insensitive"}
        # Shaped after the real return value: corpus.cache_info() merged with
        # index.analysis_report(). Inventing a shape here would let the suite
        # pass against a response Librarian does not produce.
        return {
            "abbreviation": translation,
            "sha": "a" * 40,
            "checked_at": 0.0,
            "stale": False,
            "verses": 31_102,
            "indexes": [
                {"case_sensitive": case_sensitive, "fold_diacritics": fold}
            ],
            "analysis": {
                "dominant_script": "alphabetic",
                "scripts": {"alphabetic": 1.0},
                "case_sensitive": case_sensitive,
                "fold_diacritics": fold,
                "terms": 12_345,
            },
        }

    def cache_info(self) -> dict:
        return {
            "search_corpora": {
                "kjv": {
                    "sha": "a" * 40,
                    "checked_at": 0.0,
                    "stale": False,
                    "verses": 31_102,
                    "indexes": [
                        {"case_sensitive": False, "fold_diacritics": True}
                    ],
                }
            },
        }

    def close(self) -> None:
        self.closed = True


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


class ScriptureServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_identical_catalog_reads_share_one_in_flight_request(self) -> None:
        service = ScriptureService(_settings(), client=_Client())
        self.addAsyncCleanup(service.close)
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

    async def test_librarian_receives_independent_corpus_and_search_limits(
        self,
    ) -> None:
        settings = _settings(
            GETBIBLE_MAX_RESPONSE_BYTES=str(64 * 1024 * 1024),
            SEARCH_MAX_RESPONSE_BYTES=str(4 * 1024 * 1024),
        )
        client = MagicMock()
        with patch("modules.service.GetBible", return_value=client) as constructor:
            service = ScriptureService(settings)
        self.addAsyncCleanup(service.close)

        arguments = constructor.call_args.kwargs
        self.assertEqual(arguments["max_response_bytes"], 64 * 1024 * 1024)
        self.assertEqual(
            arguments["search_limits"].max_response_bytes,
            4 * 1024 * 1024,
        )

    async def test_empty_reference_is_never_replaced_with_a_default(self) -> None:
        service = ScriptureService(_settings(), client=_Client())
        self.addAsyncCleanup(service.close)

        with self.assertRaises(Exception) as raised:
            await service.resolve_query([])
        self.assertEqual(type(raised.exception).__name__, "RobotInputError")

    async def test_ordinary_reference_does_not_probe_translation_repository(self) -> None:
        client = _Client()
        service = ScriptureService(_settings(), client=client)
        self.addAsyncCleanup(service.close)

        query = await service.resolve_query(["John", "3:16"])
        self.assertEqual(query, ScriptureQuery("John 3:16", "kjv"))
        self.assertEqual(client.translation_calls, [])

    async def test_ordinary_reference_accepts_a_saved_user_default(self) -> None:
        client = _Client()
        service = ScriptureService(_settings(), client=client)
        self.addAsyncCleanup(service.close)

        query = await service.resolve_query(
            ["John", "3:16"],
            default_translation="asv",
        )
        self.assertEqual(query, ScriptureQuery("John 3:16", "asv"))
        self.assertEqual(client.translation_calls, [])

    async def test_explicit_translation_is_validated_once(self) -> None:
        client = _Client()
        service = ScriptureService(_settings(), client=client)
        self.addAsyncCleanup(service.close)

        query = await service.resolve_query(["Gen", "1:1", "aov"])
        self.assertEqual(query, ScriptureQuery("Gen 1:1", "aov"))
        self.assertEqual(client.translation_calls, ["aov"])

    async def test_invalid_explicit_reference_never_probes_translation(self) -> None:
        client = _Client()
        service = ScriptureService(_settings(), client=client)
        self.addAsyncCleanup(service.close)

        with self.assertRaises(ReferenceValidationError):
            await service.resolve_query(["John", "1:16!", "aov"])
        self.assertEqual(client.translation_calls, [])

    async def test_unbounded_range_is_rejected_before_repository_access(self) -> None:
        client = _Client()
        service = ScriptureService(_settings(), client=client)
        self.addAsyncCleanup(service.close)

        with self.assertRaises(RequestLimitError):
            await service.resolve_query(["John", "1:1-999999999"])
        self.assertEqual(client.translation_calls, [])

    async def test_search_uses_librarian_and_validates_result_contract(self) -> None:
        client = _Client()
        service = ScriptureService(_settings(), client=client)
        self.addAsyncCleanup(service.close)

        page = await service.search("loved", SearchOptions(translation="kjv"))

        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].reference, "John 3:16")
        self.assertEqual(page.items[0].text, "For God so loved the world.")
        self.assertEqual(page.items[0].terms, ("loved",))
        self.assertEqual(len(client.search_calls), 1)

    async def test_search_never_rewrites_the_requested_match_mode(self) -> None:
        """Librarian 2 reads the script itself; the application must not guess.

        Under 1.x a single Han character flipped the whole query to substring,
        which also loosened its space-delimited terms, so ``all`` matched inside
        ``shall``. Every script must now reach Librarian exactly as asked.
        """
        client = _Client()
        service = ScriptureService(_settings(), client=client)
        self.addAsyncCleanup(service.close)
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
                    criteria = client.search_calls[-1][2]
                    self.assertEqual(criteria.match, requested)

    async def test_search_passes_the_requested_diacritics_policy(self) -> None:
        client = _Client()
        service = ScriptureService(_settings(), client=client)
        self.addAsyncCleanup(service.close)

        for policy in ("fold", "exact"):
            with self.subTest(diacritics=policy):
                await service.search(
                    "λογος",
                    SearchOptions(translation="moderngreek", diacritics=policy),
                )
                criteria = client.search_calls[-1][2]
                self.assertEqual(criteria.diacritics, policy)

    async def test_search_total_matches_the_public_contract_bound(self) -> None:
        client = _Client()
        service = ScriptureService(_settings(), client=client)
        self.addAsyncCleanup(service.close)
        response = client.search("loved", "kjv", object())
        response["query"]["total"] = 1_000_000
        accepted = service._search_page(response, "loved", "kjv")
        self.assertEqual(accepted.total, 1_000_000)

        response["query"]["total"] = 1_000_001
        with self.assertRaises(ScriptureUnavailable):
            service._search_page(response, "loved", "kjv")

    async def test_default_translation_prewarm_uses_search_capacity(self) -> None:
        client = _Client()
        service = ScriptureService(_settings(), client=client)
        self.addAsyncCleanup(service.close)

        metadata = await service.warm_default_translation()

        self.assertEqual(metadata["abbreviation"], "kjv")
        self.assertEqual(client.warm_calls, ["kjv"])
        self.assertEqual(service.metrics.snapshot()["search_warmups"], 1)

    async def test_shared_corpus_registry_is_sized_for_reuse(self) -> None:
        """The registry is the cache that stops a repeat search re-reading a corpus.

        It is configured on its own rather than from `search_corpus_limit`, which
        bounds only this client's handle dictionary. Sizing the registry down to
        that number would make every switch between translations re-parse and
        re-index from scratch — the opposite of what sharing them is for.
        """
        registry = shared_registry()
        self.addCleanup(registry.resize, 8)

        service = ScriptureService(
            replace(
                _settings(),
                search_corpus_limit=1,
                search_shared_corpus_limit=6,
            )
        )
        self.addAsyncCleanup(service.close)

        self.assertEqual(registry._limit, 6)

    async def test_shared_corpus_registry_defaults_to_reuse_not_frugality(
        self,
    ) -> None:
        registry = shared_registry()
        self.addCleanup(registry.resize, 8)
        registry.resize(1)

        settings = _settings()
        self.assertEqual(settings.search_shared_corpus_limit, 8)
        service = ScriptureService(settings)
        self.addAsyncCleanup(service.close)

        # Comfortably above the per-client handle limit, so a reader moving
        # between translations keeps hitting parsed, analysed corpora.
        self.assertEqual(registry._limit, 8)
        self.assertGreater(
            settings.search_shared_corpus_limit,
            settings.search_corpus_limit,
        )

    async def test_index_build_budget_reaches_librarian(self) -> None:
        """The budget governing the new cold-start failure mode must be wired.

        An index build is bounded separately from a request, so a build that
        serves every later search is not abandoned because one caller's clock
        ran out. Unasserted, the setting could stop reaching Librarian silently.
        """
        settings = replace(_settings(), search_index_build_seconds=42.0)
        service = ScriptureService(settings)
        self.addAsyncCleanup(service.close)

        limits = service._client.search_limits
        self.assertEqual(limits.index_build_seconds, 42.0)
        self.assertEqual(limits.deadline_seconds, settings.search_deadline_seconds)
        self.assertNotEqual(limits.index_build_seconds, limits.deadline_seconds)

    async def test_prewarm_builds_the_index_a_default_search_will_use(self) -> None:
        """An index is keyed by its case and diacritics policy.

        Warming under a different policy builds an index no search reads: the
        prewarm becomes dead work and the first real search pays the whole build
        inside the request path. Librarian's hardened facade defaults this
        argument to the 1.x spelling, which resolves to `exact`, so the policy
        has to be passed explicitly and has to track the search default.
        """
        client = _Client()
        service = ScriptureService(_settings(), client=client)
        self.addAsyncCleanup(service.close)

        await service.warm_default_translation()
        await service.search("loved", SearchOptions(translation="kjv"))

        searched = client.search_calls[-1][2]
        self.assertEqual(
            client.warm_policies,
            [(searched.case_sensitive, searched.diacritics)],
        )
        self.assertEqual(client.warm_policies, [(False, "fold")])

    async def test_slow_search_does_not_consume_lightweight_lookup_capacity(self) -> None:
        search_started = threading.Event()
        search_release = threading.Event()
        client = _Client(
            search_started=search_started,
            search_release=search_release,
        )
        settings = replace(
            _settings(),
            max_concurrent_lookups=1,
            max_concurrent_searches=1,
        )
        service = ScriptureService(settings, client=client)
        self.addAsyncCleanup(service.close)

        search = asyncio.create_task(
            service.search("loved", SearchOptions(translation="kjv"))
        )
        started = await asyncio.to_thread(search_started.wait, 1)
        self.assertTrue(started)
        selected = await service.select(ScriptureQuery("John 3:16", "kjv"))
        self.assertFalse(search.done())
        search_release.set()
        await search

        self.assertIn("kjv_43_3", selected)

    async def test_search_circuit_failure_does_not_disable_direct_scripture(self) -> None:
        client = _Client(search_fail=True)
        settings = replace(
            _settings(),
            circuit_failure_threshold=1,
            circuit_recovery_seconds=60.0,
        )
        service = ScriptureService(settings, client=client)
        self.addAsyncCleanup(service.close)

        with self.assertRaises(ScriptureUnavailable):
            await service.search("loved", SearchOptions(translation="kjv"))
        selected = await service.select(ScriptureQuery("John 3:16", "kjv"))
        snapshot = await service.snapshot()

        self.assertIn("kjv_43_3", selected)
        self.assertEqual(snapshot["circuit"]["state"], "closed")
        self.assertEqual(snapshot["search_circuit"]["state"], "open")
        # Published so an operator can tell a matching-semantics upgrade apart
        # from a regression when result counts move under a stable corpus.
        self.assertEqual(snapshot["search_engine_version"], SEARCH_ENGINE_VERSION)
        self.assertGreaterEqual(SEARCH_ENGINE_VERSION, 4)

    async def test_timeout_opens_circuit_and_followup_fails_fast(self) -> None:
        client = _Client(delay=0.05)
        settings = replace(
            _settings(),
            lookup_timeout=0.01,
            circuit_failure_threshold=1,
            circuit_recovery_seconds=60.0,
        )
        service = ScriptureService(settings, client=client)
        self.addAsyncCleanup(service.close)
        query = ScriptureQuery("John 3:16", "kjv")

        with self.assertRaises(ScriptureUnavailable):
            await service.select(query)
        with self.assertRaises(CircuitOpen):
            await service.select(query)

    async def test_timed_out_worker_keeps_capacity_until_thread_finishes(self) -> None:
        select_started = threading.Event()
        select_release = threading.Event()
        client = _Client(
            select_started=select_started,
            select_release=select_release,
        )
        settings = replace(
            _settings(),
            lookup_timeout=0.01,
            queue_timeout=0.01,
            max_concurrent_lookups=1,
            circuit_failure_threshold=5,
        )
        service = ScriptureService(settings, client=client)
        self.addAsyncCleanup(service.close)
        query = ScriptureQuery("John 3:16", "kjv")

        try:
            with self.assertRaises(ScriptureUnavailable):
                await service.select(query)
            self.assertTrue(select_started.is_set())
            with self.assertRaises(RobotBusy):
                await service.select(query)
        finally:
            select_release.set()


if __name__ == "__main__":
    unittest.main()
