import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from getbible import RepositoryError

from modules.catalog import BookOption, ChapterOption, TranslationOption
from modules.commands import (
    INTERACTIONS_SLOT,
    LIMITER_SLOT,
    SERVICE_SLOT,
    SETTINGS_SLOT,
    _highlight_search_terms,
    _reference_selection,
    _report_command_error,
    _search_results_keyboard,
    _selected_search_reference,
    bible_command,
    help_command,
    interaction_callback,
    search_command,
    start_command,
    unknown_command,
)
from modules.errors import RobotRateLimited, ScriptureUnavailable
from modules.interactions import (
    InteractionSession,
    InteractionStore,
    SearchOptions,
    SearchResult,
)
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
        result, controls = context.bot.send_message.await_args_list
        self.assertIn(
            "And of his fulness have all we received, and "
            "<b>grace</b> for <b>grace</b>.",
            result.kwargs["text"],
        )
        self.assertNotIn("…", result.kwargs["text"])
        self.assertEqual(result.kwargs["parse_mode"], "HTML")
        self.assertIn("Select one or more references", controls.kwargs["text"])
        self.assertIsNotNone(controls.kwargs["reply_markup"])

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
            "Skip — use KJV",
            str(context.bot.send_message.await_args.kwargs["reply_markup"]),
        )

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

    async def test_session_rate_limit_notice_is_recorded_for_later_cleanup(
        self,
    ) -> None:
        context = self.context(_Limiter(reject=True))
        context.bot.send_message.return_value = SimpleNamespace(message_id=901)
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

        self.assertIn(901, session.workflow_message_ids)

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
        context.args = ["John", "3:16"]
        update = self.update()
        update.effective_message = SimpleNamespace(chat_id=100, message_id=250)

        await bible_command(update, context)

        service.resolve_query.assert_awaited_once_with(["John", "3:16"])
        service.select.assert_awaited_once()
        service.translations.assert_not_awaited()
        self.assertIn("For God so loved", context.bot.send_message.await_args.kwargs["text"])
        context.bot.delete_message.assert_awaited_once_with(
            chat_id=100,
            message_id=250,
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
        self.assertEqual(limiter.calls, [(200, 100)] * 6)
        self.assertEqual(
            {
                call.kwargs["message_id"]
                for call in context.bot.delete_message.await_args_list
            },
            {250, 300, 301, 302},
        )

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
            self.callback_update(session.token, "spost"),
            context,
        )

        service.select.assert_awaited_once_with(ScriptureQuery("John 1:16", "kjv"))
        self.assertIsNone(store.get(session.token, chat_id=100, user_id=200))
        self.assertEqual(limiter.calls, [(200, 100)] * 2)
        self.assertEqual(
            {
                call.kwargs["message_id"]
                for call in context.bot.delete_message.await_args_list
            },
            {250, 300, 301, 302},
        )

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

    def test_search_result_keyboard_lists_every_reference_without_paging(self) -> None:
        session = InteractionSession(
            token="abcdefgh",
            chat_id=1,
            user_id=2,
            kind="search",
            stage="search_results",
            touched_at=0,
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

        keyboard = _search_results_keyboard(session)
        buttons = [
            button
            for row in keyboard.inline_keyboard
            for button in row
        ]
        labels = [button.text for button in buttons]

        self.assertTrue(
            all(f"John 1:{index}" in labels[index - 1] for index in range(1, 8))
        )
        self.assertFalse(any("Next" in label or "Previous" in label for label in labels))

    def test_large_configured_result_set_uses_bounded_scroll_selectors(self) -> None:
        session = InteractionSession(
            token="abcdefgh",
            chat_id=1,
            user_id=2,
            kind="search",
            stage="search_results",
            touched_at=0,
            search_generation=3,
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
        callbacks: set[str] = set()
        ranges = ((0, 96), (96, 192), (192, 200))
        for start, end in ranges:
            keyboard = _search_results_keyboard(
                session,
                start=start,
                end=end,
                include_controls=end == 200,
            )
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
