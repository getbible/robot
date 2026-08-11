import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from getbible import RepositoryError
from telegram import Message
from telegram.error import NetworkError, TelegramError

from modules.catalog import BookOption, ChapterOption, TranslationOption
from modules.commands import (
    INTERACTIONS_SLOT,
    LIMITER_SLOT,
    MINI_APP_SLOT,
    PREFERENCES_SLOT,
    SERVICE_SLOT,
    SETTINGS_SLOT,
    _ephemeral_source_target,
    _highlight_search_terms,
    _highlight_search_terms_plain,
    _reference_basket_reference,
    _reference_selection,
    _report_command_error,
    _search_page_ranges,
    _search_results_keyboard,
    _search_results_text,
    _selected_search_reference,
    bible_command,
    help_command,
    interaction_callback,
    interaction_reply,
    search_command,
    start_command,
    unknown_command,
)
from modules.ephemeral import TELEGRAM_TEXT_LIMIT, telegram_text_length
from modules.errors import RobotInputError, RobotRateLimited, ScriptureUnavailable
from modules.interactions import (
    InteractionSession,
    InteractionStore,
    ReferenceSelection,
    SearchOptions,
    SearchResult,
)
from modules.miniapp_tornado import MiniAppServer
from modules.preferences import UserPreferenceStore
from modules.service import ScriptureQuery, SearchPage


class _Limiter:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls: list[tuple[int, int]] = []

    async def acquire(self, *, user_id: int, chat_id: int) -> None:
        self.calls.append((user_id, chat_id))
        if self.reject:
            raise RobotRateLimited(1.01)

    async def should_notify_rejection(self, *, user_id: int, chat_id: int) -> bool:
        return True


class CommandRateLimitTestCase(unittest.IsolatedAsyncioTestCase):
    def context(self, limiter: _Limiter) -> SimpleNamespace:
        settings = SimpleNamespace(
            welcome_message="welcome",
            help_message="help",
            web_base_url="https://getbible.life",
            delete_command_messages=False,
            default_translation="kjv",
            max_output_chunks=8,
            max_input_length=256,
            max_references=8,
            max_total_verses=100,
            audit_log_mode="metadata",
        )
        application = SimpleNamespace(
            bot_data={
                SETTINGS_SLOT: settings,
                SERVICE_SLOT: object(),
                LIMITER_SLOT: limiter,
                INTERACTIONS_SLOT: InteractionStore(
                    max_sessions=10,
                    ttl_seconds=60,
                ),
            }
        )
        return SimpleNamespace(
            application=application,
            bot=SimpleNamespace(
                send_message=AsyncMock(
                    return_value=SimpleNamespace(message_id=300)
                ),
                send_chat_action=AsyncMock(),
                delete_message=AsyncMock(),
                edit_message_text=AsyncMock(),
                do_api_request=AsyncMock(),
            ),
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

    def test_pinned_runtime_extracts_bot_api_10_ephemeral_source_fields(
        self,
    ) -> None:
        message = Message.de_json(
            {
                "message_id": 0,
                "date": 1_785_360_000,
                "chat": {
                    "id": -100_123,
                    "type": "supergroup",
                    "title": "Test",
                },
                "from": {
                    "id": 200,
                    "is_bot": False,
                    "first_name": "User",
                },
                "receiver_user": {
                    "id": 999,
                    "is_bot": True,
                    "first_name": "Robot",
                },
                "ephemeral_message_id": 250,
                "text": "/bible@getBibleRobot James 5:1-3",
            },
            None,
        )
        update = SimpleNamespace(effective_message=message)

        self.assertEqual(
            _ephemeral_source_target(update, self.context(_Limiter())),
            (250, 999),
        )

    async def test_rejected_command_sends_only_rate_limit_response(self) -> None:
        limiter = _Limiter(reject=True)
        context = self.context(limiter)

        await start_command(self.update(), context)

        context.bot.send_message.assert_awaited_once()
        message = context.bot.send_message.await_args.kwargs["text"]
        self.assertIn("2 seconds", message)
        self.assertNotIn("welcome", message)

    async def test_search_with_words_lists_results_without_posting_scripture(self) -> None:
        limiter = _Limiter()
        context = self.context(limiter)
        service = SimpleNamespace(
            search=AsyncMock(
                return_value=SearchPage(
                    query="grace",
                    translation="kjv",
                    total=1,
                    items=(
                        SearchResult(
                            reference="John 1:16",
                            book_number=43,
                            book_name="John",
                            chapter=1,
                            verse=16,
                            text=(
                                "And of his fulness have all we received, "
                                "and grace for grace."
                            ),
                            terms=("grace",),
                        ),
                    ),
                )
            ),
            select=AsyncMock(),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        context.args = ["grace"]

        await search_command(self.update(), context)

        service.search.assert_awaited_once()
        service.select.assert_not_awaited()
        result = context.bot.send_message.await_args
        self.assertNotIn("And of his fulness", result.kwargs["text"])
        labels = [
            button.text
            for row in result.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn(
            "☐ 1. John 1:16\n"
            "And of his fulness have all we received, and "
            "【grace】 for 【grace】.",
            labels,
        )
        self.assertFalse(any("…" in label for label in labels))
        self.assertEqual(result.kwargs["parse_mode"], "HTML")
        self.assertIn("Tap a verse block", result.kwargs["text"])
        self.assertIsNotNone(result.kwargs["reply_markup"])

    async def test_search_with_words_opens_owner_bound_mini_app_when_enabled(
        self,
    ) -> None:
        context = self.context(_Limiter())
        service = SimpleNamespace(search=AsyncMock())
        context.application.bot_data[SERVICE_SLOT] = service
        mini_app = Mock(spec=MiniAppServer)
        mini_app.create_launch.return_value = SimpleNamespace(token="opaque")
        mini_app.web_url.return_value = (
            "https://bot.example/getbible/?launch=opaque"
        )
        context.application.bot_data[MINI_APP_SLOT] = mini_app
        context.args = ["grace", "and", "truth"]
        update = self.update()
        update.effective_chat.type = "private"

        await search_command(update, context)

        service.search.assert_not_awaited()
        mini_app.create_launch.assert_called_once_with(
            user_id=200,
            target_chat_id=100,
            message_thread_id=None,
            initial_route="search",
            initial_query="grace and truth",
            source_ephemeral_message_id=None,
            source_ephemeral_receiver_user_id=None,
        )
        button = (
            context.bot.send_message.await_args.kwargs["reply_markup"]
            .inline_keyboard[0][0]
        )
        self.assertEqual(
            button.web_app.url,
            "https://bot.example/getbible/?launch=opaque",
        )
        self.assertNotIn("grace", button.web_app.url)
        mini_app.remember_prompt.assert_called_once_with(
            mini_app.create_launch.return_value,
            message_id=300,
        )

    async def test_group_mini_app_defers_source_to_lifecycle_cleanup(
        self,
    ) -> None:
        context = self.context(_Limiter())
        context.bot.username = "getBibleRobot"
        context.bot.do_api_request.side_effect = [
            {"ephemeral_message_id": 901},
            True,
        ]
        context.application.bot_data[SERVICE_SLOT] = SimpleNamespace(
            search=AsyncMock()
        )
        mini_app = Mock(spec=MiniAppServer)
        mini_app.create_launch.return_value = SimpleNamespace(token="opaque")
        mini_app.direct_url.return_value = (
            "https://t.me/getBibleRobot?startapp=opaque"
        )
        context.application.bot_data[MINI_APP_SLOT] = mini_app
        update = self.update()
        update.effective_chat.type = "supergroup"
        update.effective_message = SimpleNamespace(
            message_id=0,
            message_thread_id=12,
            api_kwargs={
                "ephemeral_message_id": 250,
                "receiver_user": {"id": 999},
            },
        )

        await search_command(update, context)

        mini_app.create_launch.assert_called_once_with(
            user_id=200,
            target_chat_id=100,
            message_thread_id=12,
            initial_route="search",
            initial_query="",
            source_ephemeral_message_id=250,
            source_ephemeral_receiver_user_id=999,
        )
        mini_app.remember_prompt.assert_called_once_with(
            mini_app.create_launch.return_value,
            ephemeral_message_id=901,
        )
        self.assertEqual(
            [call.args[0] for call in context.bot.do_api_request.await_args_list],
            ["sendMessage"],
        )

    async def test_group_search_results_are_ephemeral_until_post(self) -> None:
        limiter = _Limiter()
        context = self.context(limiter)
        context.bot.do_api_request.return_value = {
            "message_id": 0,
            "ephemeral_message_id": 901,
        }
        service = SimpleNamespace(
            search=AsyncMock(
                return_value=SearchPage(
                    query="grace",
                    translation="kjv",
                    total=1,
                    items=(
                        SearchResult(
                            reference="John 1:16",
                            book_number=43,
                            book_name="John",
                            chapter=1,
                            verse=16,
                            text="Grace for grace.",
                            terms=("grace",),
                        ),
                    ),
                )
            ),
            select=AsyncMock(),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        context.args = ["grace"]
        update = self.update()
        update.effective_chat.type = "supergroup"
        update.effective_message = SimpleNamespace(
            message_id=0,
            message_thread_id=12,
            api_kwargs={
                "ephemeral_message_id": 250,
                "receiver_user": {"id": 999},
            },
        )

        await search_command(update, context)

        context.bot.send_message.assert_not_awaited()
        service.select.assert_not_awaited()
        call = context.bot.do_api_request.await_args
        self.assertEqual(call.args[0], "sendMessage")
        payload = call.kwargs["api_kwargs"]
        self.assertEqual(payload["chat_id"], 100)
        self.assertEqual(payload["receiver_user_id"], 200)
        self.assertEqual(payload["message_thread_id"], 12)
        self.assertEqual(
            payload["reply_parameters"],
            {"ephemeral_message_id": 250},
        )
        self.assertIn("inline_keyboard", payload["reply_markup"])
        labels = [
            button["text"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(
            "☐ 1. John 1:16\n【Grace】 for 【grace】.",
            labels,
        )
        self.assertNotIn("Grace for grace.", payload["text"])

    async def test_pre_session_search_failure_remains_ephemeral(self) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.return_value = {
            "message_id": 0,
            "ephemeral_message_id": 901,
        }
        interactions = context.application.bot_data[INTERACTIONS_SLOT]
        interactions.create = Mock(
            side_effect=RuntimeError("creation failed")
        )
        update = self.update()
        update.effective_chat.type = "supergroup"
        update.effective_message = SimpleNamespace(
            message_id=0,
            message_thread_id=12,
            api_kwargs={"ephemeral_message_id": 250},
        )

        await search_command(update, context)

        context.bot.send_message.assert_not_awaited()
        call = context.bot.do_api_request.await_args
        self.assertEqual(call.args[0], "sendMessage")
        payload = call.kwargs["api_kwargs"]
        self.assertEqual(payload["receiver_user_id"], 200)
        self.assertEqual(payload["message_thread_id"], 12)
        self.assertEqual(
            payload["reply_parameters"],
            {"ephemeral_message_id": 250},
        )

    async def test_ephemeral_delivery_failure_never_leaks_public_results(
        self,
    ) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.side_effect = TelegramError(
            "ephemeral unavailable"
        )
        service = SimpleNamespace(
            search=AsyncMock(
                return_value=SearchPage(
                    query="grace",
                    translation="kjv",
                    total=1,
                    items=(
                        SearchResult(
                            "John 1:16",
                            43,
                            "John",
                            1,
                            16,
                            "Grace for grace.",
                            ("grace",),
                        ),
                    ),
                )
            ),
            select=AsyncMock(),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        context.args = ["grace"]
        update = self.update()
        update.effective_chat.type = "group"
        update.effective_message = SimpleNamespace(
            message_id=250,
            message_thread_id=None,
        )

        with self.assertLogs("modules.commands", level=logging.WARNING):
            await search_command(update, context)

        context.bot.send_message.assert_not_awaited()
        service.select.assert_not_awaited()

    async def test_bible_without_reference_opens_translation_picker(self) -> None:
        limiter = _Limiter()
        context = self.context(limiter)
        service = SimpleNamespace(
            translations=AsyncMock(
                return_value=(
                    TranslationOption("kjv", "King James Version", "English"),
                )
            ),
            resolve_query=AsyncMock(),
            select=AsyncMock(),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        update = self.update()
        update.effective_message = SimpleNamespace(chat_id=100)

        await bible_command(update, context)

        service.translations.assert_awaited_once()
        service.resolve_query.assert_not_awaited()
        self.assertIn(
            "Continue with KJV",
            str(context.bot.send_message.await_args.kwargs["reply_markup"]),
        )

    async def test_bible_without_reference_opens_full_text_mini_app_picker(
        self,
    ) -> None:
        context = self.context(_Limiter())
        service = SimpleNamespace(translations=AsyncMock())
        context.application.bot_data[SERVICE_SLOT] = service
        mini_app = Mock(spec=MiniAppServer)
        mini_app.create_launch.return_value = SimpleNamespace(token="opaque")
        mini_app.web_url.return_value = (
            "https://bot.example/getbible/?launch=opaque"
        )
        context.application.bot_data[MINI_APP_SLOT] = mini_app
        update = self.update()
        update.effective_chat.type = "private"
        update.effective_message = SimpleNamespace(chat_id=100)

        await bible_command(update, context)

        service.translations.assert_not_awaited()
        mini_app.create_launch.assert_called_once_with(
            user_id=200,
            target_chat_id=100,
            message_thread_id=None,
            initial_route="bible",
            initial_query="",
            source_ephemeral_message_id=None,
            source_ephemeral_receiver_user_id=None,
        )
        self.assertIn(
            "full-text verse",
            context.bot.send_message.await_args.kwargs["text"],
        )
        mini_app.remember_prompt.assert_called_once_with(
            mini_app.create_launch.return_value,
            message_id=300,
        )

    async def test_group_bible_picker_is_ephemeral_until_post(self) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.return_value = {
            "message_id": 0,
            "ephemeral_message_id": 901,
        }
        service = SimpleNamespace(
            translations=AsyncMock(
                return_value=(
                    TranslationOption("kjv", "King James Version", "English"),
                )
            ),
            resolve_query=AsyncMock(),
            select=AsyncMock(),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        update = self.update()
        update.effective_chat.type = "supergroup"
        update.effective_message = SimpleNamespace(
            message_id=0,
            message_thread_id=12,
            api_kwargs={
                "ephemeral_message_id": 250,
                "receiver_user": {"id": 999},
            },
        )

        await bible_command(update, context)

        context.bot.send_message.assert_not_awaited()
        context.bot.send_chat_action.assert_not_awaited()
        service.translations.assert_awaited_once()
        service.resolve_query.assert_not_awaited()
        call = context.bot.do_api_request.await_args
        self.assertEqual(call.args[0], "sendMessage")
        payload = call.kwargs["api_kwargs"]
        self.assertEqual(payload["chat_id"], 100)
        self.assertEqual(payload["receiver_user_id"], 200)
        self.assertEqual(payload["message_thread_id"], 12)
        self.assertEqual(
            payload["reply_parameters"],
            {"ephemeral_message_id": 250},
        )
        self.assertIn("Continue with KJV", str(payload["reply_markup"]))

    async def test_bible_catalog_failure_never_leaks_into_group(self) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.return_value = {
            "message_id": 0,
            "ephemeral_message_id": 901,
        }
        context.application.bot_data[SERVICE_SLOT] = SimpleNamespace(
            translations=AsyncMock(
                side_effect=ScriptureUnavailable("unavailable")
            ),
        )
        update = self.update()
        update.effective_chat.type = "group"
        update.effective_message = SimpleNamespace(
            message_id=0,
            message_thread_id=None,
            api_kwargs={"ephemeral_message_id": 250},
        )

        await bible_command(update, context)

        context.bot.send_message.assert_not_awaited()
        call = context.bot.do_api_request.await_args
        self.assertEqual(call.args[0], "sendMessage")
        payload = call.kwargs["api_kwargs"]
        self.assertEqual(payload["receiver_user_id"], 200)
        self.assertIn("temporarily unavailable", payload["text"])

    async def test_safe_error_log_correlates_reference_with_cause_types(self) -> None:
        context = self.context(_Limiter())
        repository_error = RepositoryError("controlled repository failure")
        error = ScriptureUnavailable("safe public failure")
        error.__cause__ = repository_error

        with self.assertLogs("modules.commands", level=logging.INFO) as captured:
            message_id = await _report_command_error(
                error,
                "891beaa0",
                100,
                context,
            )

        self.assertEqual(message_id, 300)
        self.assertIn("891beaa0", captured.output[0])
        self.assertIn("causes=RepositoryError", captured.output[0])
        self.assertNotIn("controlled repository failure", captured.output[0])
        self.assertIn(
            "Reference: 891beaa0",
            context.bot.send_message.await_args.kwargs["text"],
        )

    async def test_session_callbacks_do_not_consume_command_rate_tokens(
        self,
    ) -> None:
        context = self.context(_Limiter(reject=True))
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_dashboard",
            translation="kjv",
        )
        session.message_id = 300

        await interaction_callback(
            self.callback_update(session.token, "sdash"),
            context,
        )

        self.assertEqual(session.workflow_message_ids, set())
        context.bot.send_message.assert_not_awaited()

    async def test_recoverable_session_error_is_recorded_for_later_cleanup(
        self,
    ) -> None:
        context = self.context(_Limiter())
        context.bot.send_message.return_value = SimpleNamespace(message_id=901)
        context.application.bot_data[SERVICE_SLOT] = SimpleNamespace(
            books=AsyncMock(side_effect=ScriptureUnavailable("unavailable")),
        )
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="reference",
            stage="reference_translation",
            translation="kjv",
        )
        session.message_id = 300

        await interaction_callback(
            self.callback_update(session.token, "tc"),
            context,
        )

        self.assertIn(901, session.workflow_message_ids)
        self.assertIs(
            store.get(session.token, chat_id=100, user_id=200),
            session,
        )

    async def test_bible_with_reference_preserves_immediate_legacy_post(self) -> None:
        limiter = _Limiter()
        context = self.context(limiter)
        service = SimpleNamespace(
            resolve_query=AsyncMock(
                return_value=ScriptureQuery("John 3:16", "kjv")
            ),
            select=AsyncMock(
                return_value={
                    "kjv_43_3": {
                        "book_name": "John",
                        "abbreviation": "kjv",
                        "chapter": 3,
                        "verses": [
                            {
                                "verse": 16,
                                "text": "For God so loved the world.",
                            }
                        ],
                    }
                }
            ),
            translations=AsyncMock(),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        mini_app = Mock(spec=MiniAppServer)
        context.application.bot_data[MINI_APP_SLOT] = mini_app
        preferences = UserPreferenceStore(
            path=None,
            default_translation="kjv",
            max_users=100,
        )
        preferences.set_translation(200, "asv")
        context.application.bot_data[PREFERENCES_SLOT] = preferences
        context.args = ["John", "3:16"]
        update = self.update()
        update.effective_message = SimpleNamespace(chat_id=100, message_id=250)

        await bible_command(update, context)

        service.resolve_query.assert_awaited_once_with(
            ["John", "3:16"],
            default_translation="asv",
        )
        service.select.assert_awaited_once()
        service.translations.assert_not_awaited()
        mini_app.create_launch.assert_not_called()
        self.assertIn("For God so loved", context.bot.send_message.await_args.kwargs["text"])
        context.bot.delete_message.assert_awaited_once_with(
            chat_id=100,
            message_id=250,
        )

    async def test_incomplete_bible_reference_opens_bible_mini_app_entrypoint(
        self,
    ) -> None:
        context = self.context(_Limiter())
        service = SimpleNamespace(
            resolve_query=AsyncMock(
                side_effect=RobotInputError("reference is incomplete")
            ),
            select=AsyncMock(),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        mini_app = Mock(spec=MiniAppServer)
        mini_app.create_launch.return_value = SimpleNamespace(token="opaque")
        mini_app.web_url.return_value = (
            "https://bot.example/getbible/?launch=opaque"
        )
        context.application.bot_data[MINI_APP_SLOT] = mini_app
        context.args = ["John"]
        update = self.update()
        update.effective_chat.type = "private"
        update.effective_message = SimpleNamespace(
            chat_id=100,
            message_id=250,
        )

        await bible_command(update, context)

        service.resolve_query.assert_awaited_once_with(
            ["John"],
            default_translation="kjv",
        )
        service.select.assert_not_awaited()
        mini_app.create_launch.assert_called_once_with(
            user_id=200,
            target_chat_id=100,
            message_thread_id=None,
            initial_route="bible",
            initial_query="John",
            source_ephemeral_message_id=None,
            source_ephemeral_receiver_user_id=None,
        )
        self.assertIn(
            "Complete this Scripture reference",
            context.bot.send_message.await_args.kwargs["text"],
        )
        context.bot.delete_message.assert_awaited_once_with(
            chat_id=100,
            message_id=250,
        )

    async def test_malformed_bible_reference_does_not_become_a_mini_app_launch(
        self,
    ) -> None:
        context = self.context(_Limiter())
        service = SimpleNamespace(
            resolve_query=AsyncMock(
                side_effect=RobotInputError("reference is malformed")
            ),
            select=AsyncMock(),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        mini_app = Mock(spec=MiniAppServer)
        context.application.bot_data[MINI_APP_SLOT] = mini_app
        context.args = ["John", "3:16!"]
        update = self.update()
        update.effective_chat.type = "private"
        update.effective_message = SimpleNamespace(
            chat_id=100,
            message_id=250,
        )

        await bible_command(update, context)

        service.select.assert_not_awaited()
        mini_app.create_launch.assert_not_called()
        self.assertIn(
            "could not understand",
            context.bot.send_message.await_args.kwargs["text"],
        )

    async def test_selected_translation_becomes_the_users_next_default(
        self,
    ) -> None:
        context = self.context(_Limiter())
        preferences = UserPreferenceStore(
            path=None,
            default_translation="kjv",
            max_users=100,
        )
        context.application.bot_data[PREFERENCES_SLOT] = preferences
        service = SimpleNamespace(
            books=AsyncMock(
                return_value=(BookOption(43, "约翰福音", "a" * 40),)
            ),
            search=AsyncMock(
                return_value=SearchPage(
                    query="爱",
                    translation="chiuns",
                    total=0,
                    items=(),
                )
            ),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="reference",
            stage="reference_translation",
            translation="kjv",
        )
        session.message_id = 300
        session.translations = (
            TranslationOption("kjv", "King James Version", "English"),
            TranslationOption("chiuns", "和合本", "中文"),
        )

        await interaction_callback(
            self.callback_update(session.token, "tr", "chiuns"),
            context,
        )
        self.assertEqual(preferences.translation_for(200), "chiuns")

        context.args = ["爱"]
        context.bot.send_message.reset_mock()
        await search_command(self.update(), context)

        service.search.assert_awaited_once()
        options = service.search.await_args.args[1]
        self.assertEqual(options.translation, "chiuns")
        # Librarian 2 reads the Han query itself, so the default whole-word mode
        # survives instead of being rewritten to substring under the user.
        self.assertEqual(options.match, "whole_word")

    async def test_group_direct_bible_posts_only_scripture_publicly(self) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.side_effect = [
            NetworkError("temporary deletion failure"),
            True,
        ]
        service = SimpleNamespace(
            resolve_query=AsyncMock(
                return_value=ScriptureQuery("John 3:16", "kjv")
            ),
            select=AsyncMock(
                return_value={
                    "kjv_43_3": {
                        "book_name": "John",
                        "abbreviation": "kjv",
                        "chapter": 3,
                        "verses": [
                            {
                                "verse": 16,
                                "text": "For God so loved the world.",
                            }
                        ],
                    }
                }
            ),
            translations=AsyncMock(),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        context.args = ["John", "3:16"]
        update = self.update()
        update.effective_chat.type = "supergroup"
        update.effective_message = SimpleNamespace(
            message_id=0,
            message_thread_id=12,
            api_kwargs={
                "ephemeral_message_id": 250,
                "receiver_user": {"id": 999},
            },
        )

        await bible_command(update, context)

        context.bot.send_message.assert_awaited_once()
        sent = context.bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], 100)
        self.assertEqual(sent["message_thread_id"], 12)
        self.assertIn("For God so loved", sent["text"])
        context.bot.send_chat_action.assert_not_awaited()
        context.bot.delete_message.assert_not_awaited()
        self.assertEqual(
            [call.args[0] for call in context.bot.do_api_request.await_args_list],
            ["deleteEphemeralMessage", "deleteEphemeralMessage"],
        )
        self.assertTrue(
            all(
                call.kwargs["api_kwargs"]["receiver_user_id"] == 999
                for call in context.bot.do_api_request.await_args_list
            )
        )

    async def test_failed_direct_bible_post_preserves_command_for_retry(self) -> None:
        context = self.context(_Limiter())
        service = SimpleNamespace(
            resolve_query=AsyncMock(
                return_value=ScriptureQuery("John 3:16", "kjv")
            ),
            select=AsyncMock(side_effect=ScriptureUnavailable("unavailable")),
            translations=AsyncMock(),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        context.args = ["John", "3:16"]
        update = self.update()
        update.effective_message = SimpleNamespace(chat_id=100, message_id=250)

        await bible_command(update, context)

        context.bot.delete_message.assert_not_awaited()
        self.assertIn(
            "temporarily unavailable",
            context.bot.send_message.await_args.kwargs["text"],
        )

    async def test_guided_bible_picker_posts_only_after_confirmation(self) -> None:
        limiter = _Limiter()
        context = self.context(limiter)
        service = SimpleNamespace(
            books=AsyncMock(
                return_value=(BookOption(43, "John", "a" * 40),)
            ),
            chapters=AsyncMock(
                return_value=(ChapterOption(3, (16, 17, 18)),)
            ),
            select=AsyncMock(
                return_value={
                    "kjv_43_3": {
                        "book_name": "John",
                        "abbreviation": "kjv",
                        "chapter": 3,
                        "verses": [
                            {"verse": 16, "text": "Verse sixteen."},
                            {"verse": 17, "text": "Verse seventeen."},
                            {"verse": 18, "text": "Verse eighteen."},
                        ],
                    }
                }
            ),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="reference",
            stage="reference_translation",
            translation="kjv",
        )
        session.message_id = 300
        session.workflow_message_ids.update({250, 301, 302})
        session.translations = (
            TranslationOption("kjv", "King James Version", "English"),
        )

        for action, value in (
            ("tc", ""),
            ("bs", "43"),
            ("cs", "3"),
            ("vs", "16"),
            ("ve", "18"),
        ):
            await interaction_callback(
                self.callback_update(session.token, action, value),
                context,
            )
            service.select.assert_not_awaited()

        await interaction_callback(
            self.callback_update(session.token, "rpost"),
            context,
        )

        service.select.assert_awaited_once_with(
            ScriptureQuery("John 3:16-18", "kjv")
        )
        self.assertIsNone(store.get(session.token, chat_id=100, user_id=200))
        self.assertEqual(limiter.calls, [])
        self.assertEqual(
            {
                call.kwargs["message_id"]
                for call in context.bot.delete_message.await_args_list
            },
            {250, 300, 301, 302},
        )

    async def test_ephemeral_bible_picker_posts_only_final_scripture_publicly(
        self,
    ) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.return_value = True
        service = SimpleNamespace(
            books=AsyncMock(
                return_value=(BookOption(43, "John", "a" * 40),)
            ),
            chapters=AsyncMock(
                return_value=(ChapterOption(3, (16, 17, 18)),)
            ),
            select=AsyncMock(
                return_value={
                    "kjv_43_3": {
                        "book_name": "John",
                        "abbreviation": "kjv",
                        "chapter": 3,
                        "verses": [
                            {"verse": 16, "text": "Verse sixteen."},
                            {"verse": 17, "text": "Verse seventeen."},
                            {"verse": 18, "text": "Verse eighteen."},
                        ],
                    }
                }
            ),
        )
        context.application.bot_data[SERVICE_SLOT] = service
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="reference",
            stage="reference_translation",
            translation="kjv",
        )
        session.ephemeral = True
        session.ephemeral_message_id = 701
        session.source_ephemeral_message_id = 700
        session.source_ephemeral_receiver_user_id = 999
        session.message_thread_id = 12
        session.translations = (
            TranslationOption("kjv", "King James Version", "English"),
        )

        for action, value in (
            ("tc", ""),
            ("bs", "43"),
            ("cs", "3"),
            ("vs", "16"),
            ("ve", "18"),
        ):
            await interaction_callback(
                self.callback_update(session.token, action, value),
                context,
            )
            context.bot.send_message.assert_not_awaited()
            service.select.assert_not_awaited()

        await interaction_callback(
            self.callback_update(session.token, "rpost"),
            context,
        )

        context.bot.send_message.assert_awaited_once()
        sent = context.bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], 100)
        self.assertEqual(sent["message_thread_id"], 12)
        self.assertIn("Verse sixteen.", sent["text"])
        self.assertNotIn(
            "sendMessage",
            [
                call.args[0]
                for call in context.bot.do_api_request.await_args_list
            ],
        )
        self.assertEqual(
            [
                call.args[0]
                for call in context.bot.do_api_request.await_args_list[-2:]
            ],
            ["deleteEphemeralMessage", "deleteEphemeralMessage"],
        )
        self.assertIsNone(
            store.get(session.token, chat_id=100, user_id=200)
        )

    async def test_failed_ephemeral_bible_post_restores_private_controls(
        self,
    ) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.side_effect = [
            True,
            True,
            {"message_id": 0, "ephemeral_message_id": 702},
        ]
        context.application.bot_data[SERVICE_SLOT] = SimpleNamespace(
            select=AsyncMock(side_effect=ScriptureUnavailable("unavailable"))
        )
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="reference",
            stage="reference_review",
            translation="kjv",
        )
        session.ephemeral = True
        session.ephemeral_message_id = 701
        session.book = BookOption(43, "John", "a" * 40)
        session.chapter = ChapterOption(3, (16,))
        session.start_verse = 16
        session.end_verse = 16
        session.reference_selections = [
            ReferenceSelection(43, "John", 3, 16, 16)
        ]

        await interaction_callback(
            self.callback_update(session.token, "rpost"),
            context,
        )

        context.bot.send_message.assert_not_awaited()
        self.assertIs(
            store.get(session.token, chat_id=100, user_id=200),
            session,
        )
        self.assertEqual(
            [
                call.args[0]
                for call in context.bot.do_api_request.await_args_list
            ],
            [
                "editEphemeralMessageText",
                "editEphemeralMessageText",
                "sendMessage",
            ],
        )
        restored = context.bot.do_api_request.await_args_list[1]
        payload = restored.kwargs["api_kwargs"]
        self.assertIn("John 3:16", payload["text"])
        self.assertIn("inline_keyboard", payload["reply_markup"])

    async def test_search_results_toggle_then_post_selected(self) -> None:
        limiter = _Limiter()
        context = self.context(limiter)
        service = SimpleNamespace(
            select=AsyncMock(
                return_value={
                    "kjv_43_1": {
                        "book_name": "John",
                        "abbreviation": "kjv",
                        "chapter": 1,
                        "verses": [{"verse": 16, "text": "Grace for grace."}],
                    }
                }
            )
        )
        context.application.bot_data[SERVICE_SLOT] = service
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_results",
            translation="kjv",
        )
        session.message_id = 300
        session.workflow_message_ids.update({250, 301, 302})
        session.search_query = "grace"
        session.search_total = 1
        session.search_results = (
            SearchResult(
                "John 1:16",
                43,
                "John",
                1,
                16,
                "Grace for grace.",
            ),
        )

        await interaction_callback(
            self.callback_update(
                session.token,
                "srt",
                f"{session.search_generation}-0",
            ),
            context,
        )
        service.select.assert_not_awaited()
        self.assertEqual(session.selected, {0})

        await interaction_callback(
            self.callback_update(
                session.token,
                "spost",
                str(session.search_generation),
            ),
            context,
        )

        service.select.assert_awaited_once_with(ScriptureQuery("John 1:16", "kjv"))
        self.assertIsNone(store.get(session.token, chat_id=100, user_id=200))
        self.assertEqual(limiter.calls, [])
        self.assertEqual(
            {
                call.kwargs["message_id"]
                for call in context.bot.delete_message.await_args_list
            },
            {250, 300, 301, 302},
        )

    async def test_ephemeral_search_posts_only_final_scripture_publicly(
        self,
    ) -> None:
        limiter = _Limiter()
        context = self.context(limiter)
        context.bot.do_api_request.return_value = True
        service = SimpleNamespace(
            select=AsyncMock(
                return_value={
                    "kjv_43_1": {
                        "book_name": "John",
                        "abbreviation": "kjv",
                        "chapter": 1,
                        "verses": [{"verse": 16, "text": "Grace for grace."}],
                    }
                }
            )
        )
        context.application.bot_data[SERVICE_SLOT] = service
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_results",
            translation="kjv",
        )
        session.ephemeral = True
        session.ephemeral_message_id = 701
        session.source_ephemeral_message_id = 700
        session.source_ephemeral_receiver_user_id = 999
        session.message_thread_id = 12
        session.search_query = "grace"
        session.search_total = 1
        session.search_results = (
            SearchResult(
                "John 1:16",
                43,
                "John",
                1,
                16,
                "Grace for grace.",
            ),
        )
        session.search_page_ranges = _search_page_ranges(session)

        await interaction_callback(
            self.callback_update(
                session.token,
                "srt",
                f"{session.search_generation}-0",
            ),
            context,
        )

        context.bot.send_message.assert_not_awaited()
        self.assertEqual(session.selected, {0})

        await interaction_callback(
            self.callback_update(
                session.token,
                "spost",
                str(session.search_generation),
            ),
            context,
        )

        context.bot.send_message.assert_awaited_once()
        sent = context.bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], 100)
        self.assertEqual(sent["message_thread_id"], 12)
        self.assertNotIn("receiver_user_id", sent)
        self.assertIn("Grace for grace.", sent["text"])
        self.assertEqual(
            [
                call.args[0]
                for call in context.bot.do_api_request.await_args_list
            ],
            [
                "editEphemeralMessageText",
                "editEphemeralMessageText",
                "deleteEphemeralMessage",
                "deleteEphemeralMessage",
            ],
        )
        deleted_receivers = {
            call.kwargs["api_kwargs"]["receiver_user_id"]
            for call in context.bot.do_api_request.await_args_list[-2:]
        }
        self.assertEqual(deleted_receivers, {200, 999})
        self.assertIsNone(
            store.get(session.token, chat_id=100, user_id=200)
        )

    async def test_failed_ephemeral_post_restores_private_controls(self) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.side_effect = [
            True,
            True,
            {"message_id": 0, "ephemeral_message_id": 702},
        ]
        service = SimpleNamespace(
            select=AsyncMock(side_effect=ScriptureUnavailable("unavailable"))
        )
        context.application.bot_data[SERVICE_SLOT] = service
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_results",
            translation="kjv",
        )
        session.ephemeral = True
        session.ephemeral_message_id = 701
        session.search_query = "grace"
        session.search_total = 1
        session.search_generation = 4
        session.search_results = (
            SearchResult(
                "John 1:16",
                43,
                "John",
                1,
                16,
                "Grace for grace.",
                ("grace",),
            ),
        )
        session.search_page_ranges = _search_page_ranges(session)
        session.selected = {0}

        await interaction_callback(
            self.callback_update(session.token, "spost", "4"),
            context,
        )

        context.bot.send_message.assert_not_awaited()
        self.assertIs(
            store.get(session.token, chat_id=100, user_id=200),
            session,
        )
        self.assertEqual(session.selected, {0})
        calls = context.bot.do_api_request.await_args_list
        self.assertEqual(
            [call.args[0] for call in calls],
            [
                "editEphemeralMessageText",
                "editEphemeralMessageText",
                "sendMessage",
            ],
        )
        restored = calls[1].kwargs["api_kwargs"]
        self.assertIn("Showing 1 complete verse", restored["text"])
        self.assertIn("inline_keyboard", restored["reply_markup"])
        restored_labels = [
            button["text"]
            for row in restored["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(
            "☑ 1. John 1:16\n【Grace】 for 【grace】.",
            restored_labels,
        )

    async def test_stale_search_navigation_and_post_fail_closed(self) -> None:
        context = self.context(_Limiter())
        service = SimpleNamespace(select=AsyncMock())
        context.application.bot_data[SERVICE_SLOT] = service
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_results",
            translation="kjv",
        )
        session.ephemeral = True
        session.ephemeral_message_id = 701
        session.search_query = "new"
        session.search_total = 1
        session.search_generation = 5
        session.search_results = (
            SearchResult("John 1:1", 43, "John", 1, 1, "New result."),
        )
        session.search_page_ranges = _search_page_ranges(session)
        session.selected = {0}

        stale_post = self.callback_update(session.token, "spost", "4")
        await interaction_callback(stale_post, context)
        stale_post.callback_query.answer.assert_awaited_once_with(
            "Those search results are no longer active.",
            show_alert=True,
        )
        service.select.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()

        session.stage = "search_query"
        stale_page = self.callback_update(session.token, "srp", "5-0")
        await interaction_callback(stale_page, context)
        stale_page.callback_query.answer.assert_awaited_once_with(
            "Reply to the current private prompt first.",
            show_alert=True,
        )
        context.bot.do_api_request.assert_not_awaited()

        session.stage = "search_results"
        stale_scope = self.callback_update(session.token, "srs", "4-nt")
        await interaction_callback(stale_scope, context)
        stale_scope.callback_query.answer.assert_awaited_once_with(
            "Those search results are no longer active.",
            show_alert=True,
        )
        context.bot.do_api_request.assert_not_awaited()

    async def test_ephemeral_result_scope_reruns_inside_the_same_panel(
        self,
    ) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.return_value = True
        result = SearchResult(
            "Matthew 1:1",
            40,
            "Matthew",
            1,
            1,
            "The book of grace.",
            ("grace",),
        )
        service = SimpleNamespace(
            search=AsyncMock(
                return_value=SearchPage(
                    query="grace",
                    translation="kjv",
                    total=1,
                    items=(result,),
                )
            )
        )
        context.application.bot_data[SERVICE_SLOT] = service
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_results",
            translation="kjv",
        )
        session.ephemeral = True
        session.ephemeral_message_id = 701
        session.search_query = "grace"
        session.search_generation = 4
        session.search_total = 1
        session.search_options = SearchOptions(
            scope="bible",
            books=(1,),
        )
        session.search_results = (
            SearchResult(
                "Genesis 1:1",
                1,
                "Genesis",
                1,
                1,
                "Grace in the beginning.",
                ("grace",),
            ),
        )
        session.search_page_ranges = _search_page_ranges(session)
        session.selected = {0}

        await interaction_callback(
            self.callback_update(session.token, "srs", "4-nt"),
            context,
        )

        service.search.assert_awaited_once_with(
            "grace",
            SearchOptions(scope="new_testament"),
        )
        self.assertEqual(session.search_options.scope, "new_testament")
        self.assertEqual(session.search_options.books, ())
        self.assertEqual(session.search_generation, 5)
        self.assertEqual(session.selected, set())
        self.assertEqual(session.search_results, (result,))
        context.bot.send_message.assert_not_awaited()
        call = context.bot.do_api_request.await_args
        self.assertEqual(call.args[0], "editEphemeralMessageText")
        labels = [
            button["text"]
            for row in call.kwargs["api_kwargs"]["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(
            "☐ 1. Matthew 1:1\nThe book of 【grace】.",
            labels,
        )

    async def test_ephemeral_search_paging_edits_the_same_private_panel(
        self,
    ) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.return_value = True
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_results",
            translation="kjv",
        )
        session.ephemeral = True
        session.ephemeral_message_id = 701
        session.search_query = "grace"
        session.search_total = 31
        session.search_generation = 4
        session.search_results = tuple(
            SearchResult(
                f"John 1:{index}",
                43,
                "John",
                1,
                index,
                f"Grace result {index}.",
                ("grace",),
            )
            for index in range(1, 32)
        )
        session.search_page_ranges = _search_page_ranges(session)

        await interaction_callback(
            self.callback_update(session.token, "srp", "4-1"),
            context,
        )

        self.assertEqual(session.search_page, 1)
        context.bot.send_message.assert_not_awaited()
        call = context.bot.do_api_request.await_args
        self.assertEqual(call.args[0], "editEphemeralMessageText")
        payload = call.kwargs["api_kwargs"]
        self.assertEqual(payload["ephemeral_message_id"], 701)
        self.assertIn("<b>Page:</b> 2 of 2", payload["text"])
        self.assertNotIn("Grace result", payload["text"])
        labels = [
            button["text"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(
            "☐ 31. John 1:31\n【Grace】 result 31.",
            labels,
        )
        self.assertFalse(any("John 1:1\n" in label for label in labels))

    async def test_group_search_prompt_selectively_mentions_owner(self) -> None:
        limiter = _Limiter()
        context = self.context(limiter)
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_dashboard",
            translation="kjv",
        )
        session.message_id = 300
        update = self.callback_update(session.token, "sq")
        update.effective_user = SimpleNamespace(
            id=200,
            mention_html=lambda: '<a href="tg://user?id=200">Owner</a>',
        )

        await interaction_callback(update, context)

        prompt = context.bot.send_message.await_args.kwargs
        self.assertIn("tg://user?id=200", prompt["text"])
        self.assertTrue(prompt["reply_markup"].selective)
        self.assertEqual(session.stage, "search_query")
        self.assertEqual(session.prompt_message_id, 300)

    async def test_group_search_prompt_is_ephemeral_for_owner(self) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.return_value = {
            "message_id": 0,
            "ephemeral_message_id": 702,
        }
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_dashboard",
            translation="kjv",
        )
        session.ephemeral = True
        session.ephemeral_message_id = 701
        session.message_thread_id = 12

        await interaction_callback(
            self.callback_update(session.token, "sq"),
            context,
        )

        context.bot.send_message.assert_not_awaited()
        call = context.bot.do_api_request.await_args
        self.assertEqual(call.args[0], "sendMessage")
        payload = call.kwargs["api_kwargs"]
        self.assertEqual(payload["receiver_user_id"], 200)
        self.assertEqual(payload["callback_query_id"], "callback-1")
        self.assertEqual(payload["message_thread_id"], 12)
        self.assertIn("force_reply", payload["reply_markup"])
        self.assertEqual(session.prompt_ephemeral_message_id, 702)

    async def test_ephemeral_search_reply_does_not_require_public_reply_target(
        self,
    ) -> None:
        context = self.context(_Limiter())
        context.bot.do_api_request.return_value = True
        service = SimpleNamespace(
            search=AsyncMock(
                return_value=SearchPage(
                    query="grace",
                    translation="kjv",
                    total=1,
                    items=(
                        SearchResult(
                            "John 1:16",
                            43,
                            "John",
                            1,
                            16,
                            "Grace for grace.",
                            ("grace",),
                        ),
                    ),
                )
            )
        )
        context.application.bot_data[SERVICE_SLOT] = service
        store = context.application.bot_data[INTERACTIONS_SLOT]
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_query",
            translation="kjv",
        )
        session.ephemeral = True
        session.ephemeral_message_id = 701
        session.prompt_ephemeral_message_id = 702
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=100),
            effective_user=SimpleNamespace(id=200),
            effective_message=SimpleNamespace(
                text="grace",
                reply_to_message=None,
                message_id=0,
                message_thread_id=None,
                ephemeral_message_id=703,
                api_kwargs={
                    "receiver_user": {"id": 999},
                },
            ),
        )

        await interaction_reply(update, context)

        service.search.assert_awaited_once()
        context.bot.send_message.assert_not_awaited()
        self.assertEqual(session.stage, "search_results")
        self.assertIsNone(session.prompt_ephemeral_message_id)
        self.assertEqual(
            [
                call.args[0]
                for call in context.bot.do_api_request.await_args_list
            ],
            [
                "deleteEphemeralMessage",
                "deleteEphemeralMessage",
                "editEphemeralMessageText",
            ],
        )
        delete_calls = context.bot.do_api_request.await_args_list[:2]
        self.assertEqual(
            [
                call.kwargs["api_kwargs"]["receiver_user_id"]
                for call in delete_calls
            ],
            [200, 999],
        )

    @staticmethod
    def callback_update(
        token: str,
        action: str,
        value: str = "",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=100),
            effective_user=SimpleNamespace(id=200),
            callback_query=SimpleNamespace(
                id="callback-1",
                data=f"gb:{token}:{action}:{value}",
                answer=AsyncMock(),
            ),
        )


class SelectionFormattingTestCase(unittest.TestCase):
    def test_search_highlight_preserves_full_escaped_verse_text(self) -> None:
        rendered = _highlight_search_terms(
            "Grâce & truth; beloved.",
            ("grace", "love"),
            SearchOptions(
                match="substring",
                diacritics="insensitive",
            ),
        )

        self.assertEqual(
            rendered,
            "<b>Grâce</b> &amp; truth; <b>beloved</b>.",
        )

    def test_search_button_highlight_preserves_full_unescaped_verse_text(self) -> None:
        rendered = _highlight_search_terms_plain(
            "Grâce & truth; beloved.",
            ("grace", "love"),
            SearchOptions(
                match="substring",
                diacritics="insensitive",
            ),
        )

        self.assertEqual(
            rendered,
            "【Grâce】 & truth; 【beloved】.",
        )

    def test_search_result_keyboard_uses_one_complete_verse_block_per_row(
        self,
    ) -> None:
        session = InteractionSession(
            token="abcdefgh",
            chat_id=1,
            user_id=2,
            kind="search",
            stage="search_results",
            touched_at=0,
            search_query="grace",
            search_total=7,
            search_results=tuple(
                SearchResult(
                    f"John 1:{index}",
                    43,
                    "John",
                    1,
                    index,
                    f"Complete verse {index}",
                )
                for index in range(1, 8)
            ),
        )
        session.search_page_ranges = _search_page_ranges(session)

        keyboard = _search_results_keyboard(session)
        result_rows = [
            row
            for row in keyboard.inline_keyboard
            if row
            and row[0].callback_data is not None
            and ":srt:" in row[0].callback_data
        ]
        labels = [row[0].text for row in result_rows]

        self.assertEqual(len(result_rows), 7)
        self.assertTrue(all(len(row) == 1 for row in result_rows))
        self.assertTrue(
            all(
                f"John 1:{index}\nComplete verse {index}" in labels[index - 1]
                for index in range(1, 8)
            )
        )
        self.assertFalse(any("Next" in label or "Previous" in label for label in labels))
        self.assertNotIn("Complete verse", _search_results_text(session))

    def test_large_result_set_uses_complete_thirty_result_pages(self) -> None:
        session = InteractionSession(
            token="abcdefgh",
            chat_id=1,
            user_id=2,
            kind="search",
            stage="search_results",
            touched_at=0,
            search_generation=3,
            search_query="complete",
            search_total=200,
            search_results=tuple(
                SearchResult(
                    f"John 1:{index}",
                    43,
                    "John",
                    1,
                    index,
                    f"Complete verse {index}",
                )
                for index in range(1, 201)
            ),
        )
        session.search_page_ranges = _search_page_ranges(session)
        callbacks: set[str] = set()
        self.assertEqual(len(session.search_page_ranges), 7)
        self.assertTrue(
            all(
                1 <= end - start <= 30
                for start, end in session.search_page_ranges
            )
        )
        for page in range(len(session.search_page_ranges)):
            session.search_page = page
            keyboard = _search_results_keyboard(session)
            buttons = [
                button
                for row in keyboard.inline_keyboard
                for button in row
            ]
            self.assertLessEqual(len(buttons), 100)
            callbacks.update(
                button.callback_data
                for button in buttons
                if button.callback_data is not None
                and ":srt:" in button.callback_data
            )

        self.assertEqual(len(callbacks), 200)
        self.assertIn("gb:abcdefgh:srt:3-199", callbacks)

    def test_long_results_remain_complete_inside_button_blocks(self) -> None:
        results = tuple(
            SearchResult(
                f"Psalm 119:{index}",
                19,
                "Psalm",
                119,
                index,
                f"Complete verse {index}: " + ("grace " * 65).strip(),
                ("grace",),
            )
            for index in range(1, 31)
        )
        session = InteractionSession(
            token="abcdefgh",
            chat_id=1,
            user_id=2,
            kind="search",
            stage="search_results",
            touched_at=0,
            search_query="grace",
            search_total=30,
            search_results=results,
        )
        session.search_page_ranges = _search_page_ranges(session)

        self.assertEqual(session.search_page_ranges, ((0, 30),))
        result_buttons = [
            row[0]
            for row in _search_results_keyboard(session).inline_keyboard
            if row
            and row[0].callback_data is not None
            and ":srt:" in row[0].callback_data
        ]
        self.assertEqual(len(result_buttons), 30)
        for index in range(1, 31):
            expected = (
                f"Complete verse {index}: "
                + ("【grace】 " * 64)
                + "【grace】"
            )
            self.assertIn(expected, result_buttons[index - 1].text)
            self.assertNotIn("…", result_buttons[index - 1].text)
        self.assertNotIn("Complete verse", _search_results_text(session))

    def test_selected_count_growth_stays_within_page_budget(self) -> None:
        session = InteractionSession(
            token="abcdefgh",
            chat_id=1,
            user_id=2,
            kind="search",
            stage="search_results",
            touched_at=0,
            search_generation=9,
            search_query="grace",
            search_total=100,
            search_results=tuple(
                SearchResult(
                    f"Psalm 119:{index}",
                    19,
                    "Psalm",
                    119,
                    index,
                    f"Complete verse {index}: " + ("grace " * 55).strip(),
                    ("grace",),
                )
                for index in range(1, 101)
            ),
        )
        session.search_page_ranges = _search_page_ranges(session)
        session.selected = set(range(100))

        for page in range(len(session.search_page_ranges)):
            session.search_page = page
            self.assertLessEqual(
                telegram_text_length(_search_results_text(session)),
                TELEGRAM_TEXT_LIMIT,
            )

        buttons = [
            button
            for row in _search_results_keyboard(session).inline_keyboard
            for button in row
        ]
        post = next(button for button in buttons if button.text.startswith("Post"))
        self.assertEqual(post.callback_data, "gb:abcdefgh:spost:9")

    def test_guided_reference_uses_start_and_end_range(self) -> None:
        session = InteractionSession(
            token="abcdefgh",
            chat_id=1,
            user_id=2,
            kind="reference",
            stage="reference_review",
            touched_at=0,
            book=BookOption(43, "John", "a" * 40),
            chapter=ChapterOption(3, tuple(range(1, 37))),
            start_verse=16,
            end_verse=18,
        )
        self.assertEqual(_reference_selection(session), "John 3:16-18")

    def test_guided_reference_basket_compacts_ranges_and_separate_verses(
        self,
    ) -> None:
        session = InteractionSession(
            token="abcdefgh",
            chat_id=1,
            user_id=2,
            kind="reference",
            stage="reference_review",
            touched_at=0,
            reference_selections=[
                ReferenceSelection(43, "John", 3, 16, 18),
                ReferenceSelection(43, "John", 3, 20, 20),
                ReferenceSelection(43, "John", 3, 19, 19),
                ReferenceSelection(45, "Romans", 8, 1, 2),
            ],
        )
        self.assertEqual(
            _reference_basket_reference(session),
            "John 3:16-20;Romans 8:1-2",
        )

    def test_search_selection_compresses_verses_by_chapter(self) -> None:
        session = InteractionSession(
            token="abcdefgh",
            chat_id=1,
            user_id=2,
            kind="search",
            stage="search_results",
            touched_at=0,
            search_options=SearchOptions(translation="kjv"),
            search_results=(
                SearchResult("John 3:16", 43, "John", 3, 16, "one"),
                SearchResult("John 3:17", 43, "John", 3, 17, "two"),
                SearchResult("Romans 8:1", 45, "Romans", 8, 1, "three"),
            ),
            selected={0, 1, 2},
        )
        self.assertEqual(
            _selected_search_reference(session),
            "John 3:16-17;Romans 8:1",
        )


if __name__ == "__main__":
    unittest.main()
