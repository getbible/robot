import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.ext import CommandHandler

import bot


class BotWiringTestCase(unittest.IsolatedAsyncioTestCase):
    def settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            user_rate_capacity=4,
            user_rate_refill_per_second=1.0,
            chat_rate_capacity=10,
            chat_rate_refill_per_second=2.0,
            rate_limit_cache_size=100,
            rate_limit_notice_cooldown=5.0,
            interaction_session_limit=50,
            interaction_ttl_seconds=300.0,
            health_host="127.0.0.1",
            health_port=8081,
            telegram_api_token=(
                "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
            ),
            max_concurrent_updates=4,
        )

    def test_every_public_command_alias_and_interaction_handler_is_registered(
        self,
    ) -> None:
        application = SimpleNamespace(
            bot_data={},
            add_handler=Mock(),
            add_error_handler=Mock(),
        )
        builder = Mock()
        builder.token.return_value = builder
        builder.concurrent_updates.return_value = builder
        builder.post_init.return_value = builder
        builder.post_shutdown.return_value = builder
        builder.build.return_value = application

        with (
            patch.object(bot, "ApplicationBuilder", return_value=builder),
            patch.object(bot, "ScriptureService", return_value=Mock()),
            patch.object(bot, "InboundRateLimiter", return_value=Mock()),
            patch.object(bot, "InteractionStore", return_value=Mock()),
            patch.object(bot, "HealthServer", return_value=Mock()),
        ):
            result = bot.build_application(self.settings())

        self.assertIs(result, application)
        handlers = [
            call.args[0] for call in application.add_handler.call_args_list
        ]
        commands = {
            command
            for handler in handlers
            if isinstance(handler, CommandHandler)
            for command in handler.commands
        }
        self.assertEqual(
            commands,
            {"start", "get", "getbible", "bible", "search", "help"},
        )
        self.assertEqual(application.add_handler.call_count, 9)
        application.add_error_handler.assert_called_once_with(bot.error_handler)

    async def test_startup_and_shutdown_cover_telegram_health_and_service(self) -> None:
        health = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        service = SimpleNamespace(close=AsyncMock())
        application = SimpleNamespace(
            bot=SimpleNamespace(set_my_commands=AsyncMock()),
            bot_data={
                bot.HEALTH_SLOT: health,
                bot.SERVICE_SLOT: service,
            },
        )

        await bot._post_init(application)
        application.bot.set_my_commands.assert_awaited_once()
        commands = application.bot.set_my_commands.await_args.args[0]
        self.assertEqual(
            [command.command for command in commands],
            ["bible", "search", "help"],
        )
        health.start.assert_awaited_once()

        await bot._post_shutdown(application)
        health.close.assert_awaited_once()
        service.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
