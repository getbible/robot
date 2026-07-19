import os
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from getbible import ReferenceValidationError, RequestLimitError

from config import Settings
from modules.errors import CircuitOpen, RobotBusy, ScriptureUnavailable
from modules.service import ScriptureQuery, ScriptureService


class _Client:
    def __init__(self, *, delay: float = 0.0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.translation_calls: list[str] = []
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

    def cache_info(self) -> dict:
        return {"test": True}

    def close(self) -> None:
        self.closed = True


def _settings(**environment: str) -> Settings:
    values = {"TELEGRAM_API_TOKEN": "test-token", "HEALTH_PORT": "0"}
    values.update(environment)
    with patch.dict(os.environ, values, clear=True):
        return Settings.from_env(load_environment_file=False)


class ScriptureServiceTestCase(unittest.IsolatedAsyncioTestCase):
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
