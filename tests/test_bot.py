import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import BadRequest
from telegram.ext import CommandHandler

import bot
from modules.service import ScriptureQuery


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
            max_output_chunks=8,
            prewarm_default_translation=True,
            default_translation="kjv",
            user_preferences_file=None,
            user_preference_limit=100,
            bot_name="GetBible Robot",
            bot_description="Read and search Scripture in Telegram with GetBible.",
            bot_short_description="Read and search Scripture with GetBible.",
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
            patch.object(bot, "UserPreferenceStore", return_value=Mock()),
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

    def test_preference_database_failure_falls_back_to_memory(self) -> None:
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
        settings = self.settings()
        settings.user_preferences_file = (
            "/var/lib/getbible-robot/production/preferences.sqlite3"
        )
        memory_store = Mock()

        with (
            patch.object(bot, "ApplicationBuilder", return_value=builder),
            patch.object(bot, "ScriptureService", return_value=Mock()),
            patch.object(bot, "InboundRateLimiter", return_value=Mock()),
            patch.object(bot, "InteractionStore", return_value=Mock()),
            patch.object(
                bot,
                "UserPreferenceStore",
                side_effect=[OSError("permission denied"), memory_store],
            ) as preference_store,
            patch.object(bot, "HealthServer", return_value=Mock()),
        ):
            result = bot.build_application(settings)

        self.assertIs(result.bot_data[bot.PREFERENCES_SLOT], memory_store)
        self.assertEqual(preference_store.call_count, 2)
        self.assertEqual(
            preference_store.call_args_list[0].kwargs["path"],
            settings.user_preferences_file,
        )
        self.assertIsNone(preference_store.call_args_list[1].kwargs["path"])

    async def test_startup_and_shutdown_cover_telegram_health_and_service(self) -> None:
        health = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        service = SimpleNamespace(
            close=AsyncMock(),
            warm_default_translation=AsyncMock(
                return_value={"abbreviation": "kjv", "verses": 31_102}
            ),
        )
        preferences = SimpleNamespace(close=Mock())
        settings = self.settings()
        application = SimpleNamespace(
            bot=SimpleNamespace(
                set_my_commands=AsyncMock(),
                set_my_name=AsyncMock(),
                set_my_description=AsyncMock(),
                set_my_short_description=AsyncMock(),
            ),
            bot_data={
                bot.HEALTH_SLOT: health,
                bot.SERVICE_SLOT: service,
                bot.SETTINGS_SLOT: settings,
                bot.PREFERENCES_SLOT: preferences,
            },
        )

        await bot._post_init(application)
        self.assertEqual(application.bot.set_my_commands.await_count, 2)
        private_call, group_call = (
            application.bot.set_my_commands.await_args_list
        )
        private_commands = private_call.args[0]
        self.assertEqual(
            private_call.kwargs["scope"].to_dict()["type"],
            "all_private_chats",
        )
        self.assertNotIn("is_ephemeral", private_commands[1].api_kwargs)
        commands = group_call.args[0]
        self.assertEqual(
            group_call.kwargs["scope"].to_dict()["type"],
            "all_group_chats",
        )
        self.assertEqual(
            [command.command for command in commands],
            ["bible", "search", "help"],
        )
        self.assertEqual(commands[1].api_kwargs["is_ephemeral"], True)
        self.assertEqual(commands[1].to_dict()["is_ephemeral"], True)
        self.assertEqual(commands[0].api_kwargs["is_ephemeral"], True)
        self.assertEqual(commands[0].to_dict()["is_ephemeral"], True)
        application.bot.set_my_name.assert_awaited_once_with(settings.bot_name)
        application.bot.set_my_description.assert_awaited_once_with(
            settings.bot_description
        )
        application.bot.set_my_short_description.assert_awaited_once_with(
            settings.bot_short_description
        )
        service.warm_default_translation.assert_awaited_once()
        health.start.assert_awaited_once()

        await bot._post_shutdown(application)
        health.close.assert_awaited_once()
        service.close.assert_awaited_once()
        preferences.close.assert_called_once_with()

    async def test_ephemeral_registration_failure_uses_ordinary_group_commands(
        self,
    ) -> None:
        health = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        service = SimpleNamespace(
            close=AsyncMock(),
            warm_default_translation=AsyncMock(
                return_value={"abbreviation": "kjv", "verses": 31_102}
            ),
        )
        preferences = SimpleNamespace(close=Mock())
        telegram_bot = SimpleNamespace(
            set_my_commands=AsyncMock(
                side_effect=[
                    True,
                    BadRequest("ephemeral commands are unavailable"),
                    True,
                ]
            ),
            set_my_name=AsyncMock(),
            set_my_description=AsyncMock(),
            set_my_short_description=AsyncMock(),
        )
        settings = self.settings()
        application = SimpleNamespace(
            bot=telegram_bot,
            bot_data={
                bot.HEALTH_SLOT: health,
                bot.SERVICE_SLOT: service,
                bot.SETTINGS_SLOT: settings,
                bot.PREFERENCES_SLOT: preferences,
            },
        )

        await bot._post_init(application)

        self.assertEqual(telegram_bot.set_my_commands.await_count, 3)
        failed_ephemeral_call = telegram_bot.set_my_commands.await_args_list[1]
        fallback_call = telegram_bot.set_my_commands.await_args_list[2]
        self.assertTrue(
            failed_ephemeral_call.args[0][0].api_kwargs["is_ephemeral"]
        )
        self.assertEqual(
            fallback_call.kwargs["scope"].to_dict()["type"],
            "all_group_chats",
        )
        self.assertTrue(
            all(
                "is_ephemeral" not in command.api_kwargs
                for command in fallback_call.args[0]
            )
        )
        health.start.assert_awaited_once()

    async def test_successful_mini_app_post_cleans_private_launch_prompt(
        self,
    ) -> None:
        telegram_bot = SimpleNamespace(delete_message=AsyncMock())
        launch = bot.MiniAppLaunch(
            token="abcdefghijklmnop",
            user_id=42,
            target_chat_id=42,
            message_thread_id=None,
            initial_route="search",
            initial_query="grace",
            created_at=0,
            prompt_message_id=321,
        )

        await bot._cleanup_mini_app_launch_prompt(telegram_bot, launch)

        telegram_bot.delete_message.assert_awaited_once_with(
            chat_id=42,
            message_id=321,
        )

    async def test_successful_group_post_cleans_ephemeral_launch_prompt(
        self,
    ) -> None:
        telegram_bot = SimpleNamespace(
            do_api_request=AsyncMock(return_value=True),
        )
        launch = bot.MiniAppLaunch(
            token="abcdefghijklmnop",
            user_id=42,
            target_chat_id=-100,
            message_thread_id=9,
            initial_route="bible",
            initial_query="John",
            created_at=0,
            prompt_ephemeral_message_id=654,
        )

        await bot._cleanup_mini_app_launch_prompt(telegram_bot, launch)

        telegram_bot.do_api_request.assert_awaited_once_with(
            "deleteEphemeralMessage",
            api_kwargs={
                "chat_id": -100,
                "receiver_user_id": 42,
                "ephemeral_message_id": 654,
            },
        )

    async def test_mini_app_post_callback_cleans_prompt_after_scripture(
        self,
    ) -> None:
        telegram_bot = SimpleNamespace(delete_message=AsyncMock())
        application = SimpleNamespace(
            bot=telegram_bot,
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
        settings = self.settings()
        settings.mini_app_enabled = True
        service = Mock()
        mini_app_server = Mock()

        with (
            patch.object(bot, "ApplicationBuilder", return_value=builder),
            patch.object(bot, "ScriptureService", return_value=service),
            patch.object(bot, "InboundRateLimiter", return_value=Mock()),
            patch.object(bot, "InteractionStore", return_value=Mock()),
            patch.object(bot, "UserPreferenceStore", return_value=Mock()),
            patch.object(bot, "HealthServer", return_value=Mock()),
            patch.object(
                bot,
                "MiniAppServer",
                return_value=mini_app_server,
            ) as mini_app_constructor,
            patch.object(
                bot,
                "post_scripture_queries",
                new=AsyncMock(return_value=(101,)),
            ) as post_scripture,
        ):
            bot.build_application(settings)
            callback = mini_app_constructor.call_args.kwargs["post_scripture"]
            launch = bot.MiniAppLaunch(
                token="abcdefghijklmnop",
                user_id=42,
                target_chat_id=42,
                message_thread_id=None,
                initial_route="search",
                initial_query="grace",
                created_at=0,
                prompt_message_id=321,
            )
            result = await callback(
                launch,
                (ScriptureQuery("John 3:16", "kjv"),),
            )

        self.assertEqual(result, (101,))
        post_scripture.assert_awaited_once()
        telegram_bot.delete_message.assert_awaited_once_with(
            chat_id=42,
            message_id=321,
        )
        self.assertIs(
            application.bot_data[bot.MINI_APP_SLOT],
            mini_app_server,
        )

    def test_main_logs_unhandled_startup_failure(self) -> None:
        settings = SimpleNamespace(
            log_level=logging.INFO,
            instance_name="production",
            log_file=None,
        )
        with (
            patch.object(bot.Settings, "from_env", return_value=settings),
            patch.object(bot, "configure_logging"),
            patch.object(
                bot,
                "build_application",
                side_effect=RuntimeError("startup failed"),
            ),
            self.assertLogs(bot.LOGGER, level="CRITICAL") as captured,
        ):
            result = bot.main()

        self.assertEqual(result, 1)
        self.assertIn("unhandled startup or runtime failure", captured.output[0])

    def test_delivery_runner_selects_polling_or_webhook_exclusively(self) -> None:
        application = SimpleNamespace(
            run_polling=Mock(),
            run_webhook=Mock(),
        )
        polling = SimpleNamespace(
            telegram_delivery_mode="polling",
            drop_pending_updates=True,
        )
        bot.run_application(application, polling)
        application.run_polling.assert_called_once_with(
            allowed_updates=bot.ALLOWED_UPDATES,
            drop_pending_updates=True,
        )
        application.run_webhook.assert_not_called()

        application.run_polling.reset_mock()
        webhook = SimpleNamespace(
            telegram_delivery_mode="webhook",
            drop_pending_updates=False,
            webhook_public_url="https://bot.example.com/telegram/production",
            webhook_secret_token="A" * 32,
            webhook_listen="127.0.0.1",
            webhook_port=9001,
            webhook_ip_address="1.1.1.1",
            webhook_max_connections=16,
        )
        bot.run_application(application, webhook)
        application.run_polling.assert_not_called()
        application.run_webhook.assert_called_once_with(
            listen="127.0.0.1",
            port=9001,
            url_path="telegram/production",
            webhook_url="https://bot.example.com/telegram/production",
            ip_address="1.1.1.1",
            max_connections=16,
            secret_token="A" * 32,
            allowed_updates=bot.ALLOWED_UPDATES,
            drop_pending_updates=False,
        )


if __name__ == "__main__":
    unittest.main()
