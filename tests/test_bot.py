import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from telegram.error import BadRequest
from telegram.ext import CommandHandler

import bot
from modules.contributions import LIVE_NOTIFICATION_KIND, ContributionStore
from modules.getbible_bookmarks import (
    BookmarksCatalog,
    BookmarksIndex,
    BookmarksTransportError,
    BookmarkTopic,
)
from modules.service import ScriptureQuery


def _index(version: int, checksum: str) -> BookmarksIndex:
    return BookmarksIndex(
        catalog_version=version,
        checksum=checksum,
        topics=1,
        verses=1,
        locales=("en",),
    )


def _catalog(*verses: tuple[int, int, int]) -> BookmarksCatalog:
    topic = BookmarkTopic(
        id="grace",
        name="Grace",
        color="#bbf7d0",
        aliases=(),
        default=False,
        verses=tuple(verses),
    )
    return BookmarksCatalog(checksum="d" * 64, topics=(topic,), document=b"{}")


class _FakeBookmarksClient:
    """A scripted Bookmarks API: each index() call pops the next answer."""

    def __init__(self, *answers: BookmarksIndex | BaseException) -> None:
        self.answers = list(answers)
        self.catalog_calls = 0
        self.catalog_answer = _catalog((43, 3, 16))

    def index(self) -> BookmarksIndex:
        # The last scripted answer repeats so a loop can keep observing it.
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def catalog(self) -> BookmarksCatalog:
        self.catalog_calls += 1
        return self.catalog_answer


def _accepted_store(user_id: int = 42) -> ContributionStore:
    """An in-memory store holding one accepted, not yet live, contribution."""
    store = ContributionStore(path=None)
    store.submit_application(user_id, first_name="Grace")
    store.decide_application(user_id, "approved", actor="admin")
    store.acknowledge_disclosure(user_id)
    result = store.record_events(
        user_id,
        [
            {
                "client_event_id": "topic.grace.v1",
                "type": "topic_upsert",
                "topic": {"local_topic_id": "local.grace", "name": "Grace", "color": "#bbf7d0"},
            },
            {
                "client_event_id": "verse.grace.43.3.16",
                "type": "verse_add",
                "topic": {"local_topic_id": "local.grace"},
                "verse": {"book": 43, "chapter": 3, "verse": 16},
            },
        ],
    )
    store.set_topic_mapping(
        user_id,
        "local.grace",
        "grace",
        state="mapped",
        actor="admin",
        canonical_definition={"id": "grace", "name": "Grace", "color": "#bbf7d0", "aliases": []},
    )
    for event_id in result.event_ids.values():
        store.decide_event(event_id, "approved", actor="admin")
    store.publish_approved_events_atomically(
        {
            "schema_version": 1,
            "topics": [{"id": "grace", "name": "Grace", "color": "#bbf7d0", "aliases": []}],
            "associations": {
                "add": [{"topic_id": "grace", "book": 43, "chapter": 3, "verse": 16}],
                "remove": [],
            },
        },
        list(result.event_ids.values()),
        actor="admin",
    )
    return store


def _live_notifications(store: ContributionStore) -> list[int]:
    return [
        notification.contributor_id
        for notification in store.list_notifications()
        if notification.kind == LIVE_NOTIFICATION_KIND
    ]


class BotWiringTestCase(unittest.IsolatedAsyncioTestCase):
    def settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            user_rate_capacity=4,
            user_rate_refill_per_second=1.0,
            chat_rate_capacity=10,
            chat_rate_refill_per_second=2.0,
            rate_limit_cache_size=100,
            rate_limit_notice_cooldown=5.0,
            mini_app_ip_rate_capacity=60,
            mini_app_ip_rate_refill_per_second=10.0,
            abuse_rejection_threshold=6,
            abuse_window_seconds=60.0,
            abuse_block_seconds=300.0,
            interaction_session_limit=50,
            interaction_ttl_seconds=300.0,
            health_host="127.0.0.1",
            health_port=8081,
            telegram_api_token=(
                "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
            ),
            max_concurrent_updates=4,
            max_output_chunks=8,
            default_translation="kjv",
            user_preferences_file=None,
            user_preference_limit=100,
            bot_name="GetBible Robot",
            bot_description="Read and search Scripture in Telegram with GetBible.",
            bot_short_description="Read and search Scripture with GetBible.",
            bookmarks_base_url="https://bookmarks.getbible.net",
            bookmark_catalog_check_interval_seconds=21_600,
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
            {
                "start",
                "get",
                "getbible",
                "bible",
                "search",
                "help",
                "contributor",
            },
        )
        self.assertEqual(application.add_handler.call_count, 11)
        application.add_error_handler.assert_called_once_with(bot.error_handler)
        services = result.bot_data[bot.APPLICATION_SERVICES_SLOT]
        self.assertIs(services.settings, result.bot_data[bot.SETTINGS_SLOT])
        self.assertIs(services.scripture, result.bot_data[bot.SERVICE_SLOT])
        self.assertIs(services.limiter, result.bot_data[bot.LIMITER_SLOT])
        self.assertIs(
            services.interactions,
            result.bot_data[bot.INTERACTIONS_SLOT],
        )
        self.assertIs(
            services.preferences,
            result.bot_data[bot.PREFERENCES_SLOT],
        )
        self.assertIsNone(services.mini_app)
        self.assertIsNone(services.contributions)

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

    def test_configured_contribution_database_failure_stops_startup(self) -> None:
        settings = self.settings()
        settings.contribution_store_file = (
            "/var/lib/getbible-robot/production/contributions.sqlite3"
        )

        with patch.object(
            bot,
            "ContributionStore",
            side_effect=OSError("permission denied"),
        ), self.assertRaisesRegex(
            RuntimeError,
            "Configured contributor storage is unavailable",
        ):
            bot._build_contribution_store(settings)

    async def test_startup_and_shutdown_cover_telegram_health_and_service(self) -> None:
        events: list[str] = []

        def record(name: str) -> Mock:
            return Mock(side_effect=lambda *args, **kwargs: events.append(name))

        def record_async(name: str) -> AsyncMock:
            return AsyncMock(side_effect=lambda *args, **kwargs: events.append(name))

        health = SimpleNamespace(
            start=record_async("health.start"),
            close=record_async("health.close"),
            mark_ready=record("health.mark_ready"),
            mark_not_ready=record("health.mark_not_ready"),
        )
        notifier = SimpleNamespace(
            ready=record("notifier.ready"),
            stopping=record_async("notifier.stopping"),
        )
        # The service offers nothing but close(): the robot holds no corpus,
        # so startup must not reach for a warm-up hook that no longer exists.
        service = SimpleNamespace(close=record_async("service.close"))
        preferences = SimpleNamespace(close=record("preferences.close"))
        settings = self.settings()
        application = SimpleNamespace(
            bot=SimpleNamespace(
                set_my_commands=record_async("telegram.set_my_commands"),
                set_my_name=record_async("telegram.set_my_name"),
                set_my_description=record_async("telegram.set_my_description"),
                set_my_short_description=record_async(
                    "telegram.set_my_short_description"
                ),
            ),
            bot_data={
                bot.HEALTH_SLOT: health,
                bot.SERVICE_SLOT: service,
                bot.SETTINGS_SLOT: settings,
                bot.PREFERENCES_SLOT: preferences,
                bot.NOTIFIER_SLOT: notifier,
            },
        )
        slots_before_startup = set(application.bot_data)

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
        self.assertNotIn(
            "contributor",
            [command.command for command in private_commands],
        )
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
        # Liveness comes first, readiness only once the Telegram profile is
        # synchronised, and the systemd notice only once readiness is true.
        # Nothing is warmed in between: every lookup goes to the public APIs.
        self.assertEqual(
            events,
            [
                "health.start",
                "telegram.set_my_commands",
                "telegram.set_my_commands",
                "telegram.set_my_name",
                "telegram.set_my_description",
                "telegram.set_my_short_description",
                "health.mark_ready",
                "notifier.ready",
            ],
        )
        # No prewarm task and no cache janitor run behind readiness, so no
        # slot appears for either.
        self.assertEqual(set(application.bot_data), slots_before_startup)
        self.assertFalse(hasattr(bot, "PREWARM_SLOT"))
        self.assertFalse(hasattr(bot, "CACHE_JANITOR_SLOT"))
        self.assertFalse(hasattr(bot, "CacheJanitor"))

        events.clear()
        await bot._post_shutdown(application)
        # Readiness drops before the stop notice, and the service closes only
        # after the listeners that hand it work are gone.
        self.assertEqual(
            events,
            [
                "health.mark_not_ready",
                "notifier.stopping",
                "health.close",
                "service.close",
                "preferences.close",
            ],
        )

    async def test_catalog_watcher_runs_with_the_store_and_stops_before_notifications(
        self,
    ) -> None:
        health = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(),
            mark_ready=Mock(),
            mark_not_ready=Mock(),
        )
        notifier = SimpleNamespace(ready=Mock(), stopping=AsyncMock())
        service = SimpleNamespace(close=AsyncMock())
        preferences = SimpleNamespace(close=Mock())
        store = ContributionStore(path=None)
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
                bot.SETTINGS_SLOT: self.settings(),
                bot.PREFERENCES_SLOT: preferences,
                bot.NOTIFIER_SLOT: notifier,
                bot.CONTRIBUTION_STORE_SLOT: store,
            },
        )

        await bot._post_init(application)
        watcher = application.bot_data[bot.BOOKMARK_CATALOG_WATCH_TASK_SLOT]
        deliverer = application.bot_data[bot.CONTRIBUTION_NOTIFICATION_TASK_SLOT]
        self.assertEqual(watcher.get_name(), "watch-bookmark-catalog")
        self.assertEqual(deliverer.get_name(), "deliver-contribution-notifications")
        self.assertFalse(watcher.done())
        # The first check waits for startup to settle, so nothing reached the
        # network or the store yet.
        await asyncio.sleep(0)
        self.assertFalse(watcher.done())
        self.assertFalse(store.live_catalog_state()["observed"])

        await bot._post_shutdown(application)
        self.assertTrue(watcher.cancelled())
        self.assertTrue(deliverer.cancelled())

        # Shutdown cancels the watcher before the deliverer, so a notice queued
        # by a final observation still has a deliverer to drain it.
        order: list[str] = []

        async def cancellable(name: str) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                order.append(name)
                raise

        watcher = asyncio.create_task(cancellable("watcher"))
        deliverer = asyncio.create_task(cancellable("deliverer"))
        await asyncio.sleep(0)
        application.bot_data[bot.BOOKMARK_CATALOG_WATCH_TASK_SLOT] = watcher
        application.bot_data[bot.CONTRIBUTION_NOTIFICATION_TASK_SLOT] = deliverer
        application.bot_data[bot.CONTRIBUTION_STORE_SLOT] = ContributionStore(path=None)
        await bot._post_shutdown(application)
        self.assertEqual(order, ["watcher", "deliverer"])

    async def test_catalog_watcher_is_not_started_without_a_client(self) -> None:
        settings = self.settings()
        settings.bookmarks_base_url = "http://bookmarks.example"  # rejected: not https
        with self.assertLogs(bot.LOGGER, level="WARNING") as captured:
            self.assertIsNone(bot._build_bookmarks_client(settings))
        self.assertIn("BookmarksResponseError", captured.output[0])
        client = bot._build_bookmarks_client(self.settings())
        assert client is not None
        self.assertEqual(client.base_url, "https://bookmarks.getbible.net/v1")

    async def test_catalog_observation_downloads_only_a_changed_catalogue(self) -> None:
        store = _accepted_store()
        try:
            self.assertEqual(_live_notifications(store), [])
            client = _FakeBookmarksClient(_index(3, "c" * 64))

            with self.assertLogs(bot.LOGGER, level="INFO") as captured:
                update = await bot._observe_bookmark_catalog(store, client)  # type: ignore[arg-type]
            self.assertTrue(update.changed)
            self.assertEqual(update.catalog_version, 3)
            self.assertEqual(len(update.newly_live_event_ids), 2)
            self.assertEqual(update.notified_contributor_ids, (42,))
            self.assertEqual(client.catalog_calls, 1)
            state = store.live_catalog_state()
            self.assertEqual((state["catalog_version"], state["checksum"]), (3, "c" * 64))
            self.assertEqual(_live_notifications(store), [42])
            self.assertTrue(any("version 3 observed" in line for line in captured.output))
            self.assertTrue(
                any("made 2 accepted contribution events live" in line for line in captured.output)
            )

            # An unchanged index is a cheap check: no catalogue download, the
            # check time moves, nothing new is live and nobody is told twice.
            checked_before = state["checked_at"]
            again = await bot._observe_bookmark_catalog(store, client)  # type: ignore[arg-type]
            self.assertFalse(again.changed)
            self.assertEqual(again.newly_live_event_ids, ())
            self.assertEqual(client.catalog_calls, 1)
            self.assertGreater(store.live_catalog_state()["checked_at"], checked_before)
            self.assertEqual(_live_notifications(store), [42])

            # A new version with the same content changes the version only.
            client.answers = [_index(4, "c" * 64)]
            bumped = await bot._observe_bookmark_catalog(store, client)  # type: ignore[arg-type]
            self.assertFalse(bumped.changed)
            self.assertEqual(client.catalog_calls, 2)
            self.assertEqual(store.live_catalog_state()["catalog_version"], 4)
        finally:
            store.close()

    async def test_catalog_watcher_logs_failures_and_keeps_checking(self) -> None:
        store = _accepted_store()
        try:
            client = _FakeBookmarksClient(
                BookmarksTransportError(),
                RuntimeError("store exploded"),
                _index(9, "e" * 64),
            )
            with self.assertLogs(bot.LOGGER, level="WARNING") as captured:
                task = asyncio.create_task(
                    bot._watch_bookmark_catalog(
                        store,
                        client,  # type: ignore[arg-type]
                        interval_seconds=0.01,
                        initial_delay_seconds=0.0,
                    ),
                    name="watch-bookmark-catalog",
                )
                for _ in range(500):
                    if store.live_catalog_state()["observed"]:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(store.live_catalog_state()["observed"])
                self.assertFalse(task.done())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(store.live_catalog_state()["catalog_version"], 9)
            self.assertEqual(_live_notifications(store), [42])
            warnings = [line for line in captured.output if "catalogue check failed" in line]
            self.assertEqual(len(warnings), 2)
            self.assertIn("BookmarksTransportError", warnings[0])
            self.assertIn("RuntimeError", warnings[1])
            self.assertNotIn("store exploded", "".join(captured.output))
        finally:
            store.close()

    async def test_ephemeral_registration_failure_uses_ordinary_group_commands(
        self,
    ) -> None:
        health = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(),
            mark_ready=Mock(),
        )
        notifier = SimpleNamespace(ready=Mock())
        service = SimpleNamespace(close=AsyncMock())
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
                bot.NOTIFIER_SLOT: notifier,
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

    async def test_successful_group_post_cleans_prompt_and_source_command(
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
            source_ephemeral_message_id=250,
            source_ephemeral_receiver_user_id=999,
        )

        await bot._cleanup_mini_app_launch_prompt(telegram_bot, launch)

        self.assertEqual(
            telegram_bot.do_api_request.await_args_list,
            [
                call(
                    "deleteEphemeralMessage",
                    api_kwargs={
                        "chat_id": -100,
                        "receiver_user_id": 42,
                        "ephemeral_message_id": 654,
                    },
                ),
                call(
                    "deleteEphemeralMessage",
                    api_kwargs={
                        "chat_id": -100,
                        "receiver_user_id": 999,
                        "ephemeral_message_id": 250,
                    },
                ),
            ],
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
            log_max_bytes=10 * 1024 * 1024,
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


class MenuButtonTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_menu_button_names_the_packaged_client_build(self) -> None:
        settings = SimpleNamespace(
            bot_name="getBible.Life",
            bot_description="Scripture",
            bot_short_description="Scripture",
            mini_app_enabled=True,
            mini_app_public_url="https://bot.example.com/getbible",
        )
        telegram_bot = SimpleNamespace(
            set_my_commands=AsyncMock(),
            set_my_name=AsyncMock(),
            set_my_description=AsyncMock(),
            set_my_short_description=AsyncMock(),
            set_chat_menu_button=AsyncMock(),
        )
        versioned = "https://bot.example.com/getbible/?build=0123456789abcdef"
        application = SimpleNamespace(
            bot=telegram_bot,
            bot_data={
                bot.SETTINGS_SLOT: settings,
                bot.MINI_APP_SLOT: SimpleNamespace(menu_web_url=versioned),
            },
        )

        await bot._synchronize_telegram_profile(application, settings)
        menu_button = telegram_bot.set_chat_menu_button.await_args.kwargs["menu_button"]
        self.assertEqual(menu_button.web_app.url, versioned)

        # A menu launch carries no one-time token, so without the build query
        # a WebView could keep replaying a cached shell. The fixed URL remains
        # the fallback only when no Mini App listener exists to name a build.
        application.bot_data.pop(bot.MINI_APP_SLOT)
        telegram_bot.set_chat_menu_button.reset_mock()
        await bot._synchronize_telegram_profile(application, settings)
        fallback = telegram_bot.set_chat_menu_button.await_args.kwargs["menu_button"]
        self.assertEqual(fallback.web_app.url, "https://bot.example.com/getbible/")

        application.bot_data[bot.MINI_APP_SLOT] = Mock()
        telegram_bot.set_chat_menu_button.reset_mock()
        await bot._synchronize_telegram_profile(application, settings)
        mocked = telegram_bot.set_chat_menu_button.await_args.kwargs["menu_button"]
        self.assertEqual(mocked.web_app.url, "https://bot.example.com/getbible/")


if __name__ == "__main__":
    unittest.main()
