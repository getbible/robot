import os
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from getbible import ReferenceValidationError, RequestLimitError

from config import Settings
from modules.errors import CircuitOpen, RobotBusy, ScriptureUnavailable
from modules.interactions import SearchOptions
from modules.service import CircuitBreaker, ScriptureQuery, ScriptureService


class _Client:
    def __init__(self, *, delay: float = 0.0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.translation_calls: list[str] = []
        self.search_calls: list[tuple[str, str, object]] = []
        self.closed = False

    def valid_translation(self, code: str) -> bool:
        self.translation_calls.append(code)
        return code in {"kjv", "aov", "codex"}

    def select(self, references: str, translation: str) -> dict:
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
                }
            ],
        }

    def cache_info(self) -> dict:
        return {"test": True}

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
        self.assertEqual(len(client.search_calls), 1)

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
        client = _Client(delay=0.1)
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

        with self.assertRaises(ScriptureUnavailable):
            await service.select(query)
        with self.assertRaises(RobotBusy):
            await service.select(query)


if __name__ == "__main__":
    unittest.main()
