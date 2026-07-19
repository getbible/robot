import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.commands import (
    LIMITER_KEY,
    SERVICE_KEY,
    SETTINGS_KEY,
    help_command,
    search_command,
    start_command,
    unknown_command,
)
from modules.errors import RobotRateLimited


class _Limiter:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls: list[tuple[int, int]] = []

    async def acquire(self, *, user_id: int, chat_id: int) -> None:
        self.calls.append((user_id, chat_id))
        if self.reject:
            raise RobotRateLimited(1.01)


class CommandRateLimitTestCase(unittest.IsolatedAsyncioTestCase):
    def context(self, limiter: _Limiter) -> SimpleNamespace:
        settings = SimpleNamespace(
            welcome_message="welcome",
            help_message="help",
            web_base_url="https://getbible.life",
            delete_command_messages=False,
        )
        application = SimpleNamespace(
            bot_data={
                SETTINGS_KEY: settings,
                SERVICE_KEY: object(),
                LIMITER_KEY: limiter,
            }
        )
        return SimpleNamespace(
            application=application,
            bot=SimpleNamespace(send_message=AsyncMock(), delete_message=AsyncMock()),
            args=[],
        )

    @staticmethod
    def update() -> SimpleNamespace:
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=100),
            effective_user=SimpleNamespace(id=200),
            effective_message=None,
        )

    async def test_non_lookup_commands_all_consume_user_and_chat_tokens(self) -> None:
        limiter = _Limiter()
        context = self.context(limiter)
        update = self.update()

        await start_command(update, context)
        await help_command(update, context)
        await search_command(update, context)
        await unknown_command(update, context)

        self.assertEqual(limiter.calls, [(200, 100)] * 4)
        self.assertEqual(context.bot.send_message.await_count, 4)

    async def test_rejected_command_sends_only_rate_limit_response(self) -> None:
        limiter = _Limiter(reject=True)
        context = self.context(limiter)

        await start_command(self.update(), context)

        context.bot.send_message.assert_awaited_once()
        message = context.bot.send_message.await_args.kwargs["text"]
        self.assertIn("2 seconds", message)
        self.assertNotIn("welcome", message)


if __name__ == "__main__":
    unittest.main()
