import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from getbible import ReferenceValidationError, RequestLimitError
from telegram.error import Conflict

from modules.commands import (
    DUPLICATE_POLLER_SLOT,
    _safe_error_message,
    error_handler,
)
from modules.errors import RobotRateLimited, ScriptureUnavailable


class SafeErrorMessageTestCase(unittest.TestCase):
    def test_unexpected_exception_text_is_never_reflected(self) -> None:
        message, expected = _safe_error_message(
            RuntimeError("secret filesystem and token details"),
            "deadbeef",
        )
        self.assertFalse(expected)
        self.assertNotIn("secret", message)
        self.assertIn("deadbeef", message)

    def test_validation_and_limit_failures_are_actionable_but_generic(self) -> None:
        validation, _ = _safe_error_message(
            ReferenceValidationError("attacker-controlled reference"),
            "id",
        )
        limited, _ = _safe_error_message(
            RequestLimitError("999999999"),
            "id",
        )
        self.assertNotIn("attacker", validation)
        self.assertNotIn("999999999", limited)

    def test_rate_limit_rounds_retry_time(self) -> None:
        message, expected = _safe_error_message(RobotRateLimited(1.01), "id")
        self.assertTrue(expected)
        self.assertIn("2 seconds", message)

    def test_repository_failure_has_correlation_id(self) -> None:
        message, expected = _safe_error_message(
            ScriptureUnavailable("raw upstream URL"),
            "cafebabe",
        )
        self.assertTrue(expected)
        self.assertIn("cafebabe", message)
        self.assertNotIn("upstream", message)


class PollingConflictTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_poller_stops_once_without_fighting_telegram(self) -> None:
        application = SimpleNamespace(bot_data={}, stop_running=Mock())
        context = SimpleNamespace(
            error=Conflict("terminated by other getUpdates request"),
            application=application,
            bot=SimpleNamespace(send_message=AsyncMock()),
        )

        await error_handler(object(), context)
        await error_handler(object(), context)

        self.assertTrue(application.bot_data[DUPLICATE_POLLER_SLOT])
        application.stop_running.assert_called_once_with()
        context.bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
