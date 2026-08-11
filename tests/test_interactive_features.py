import inspect
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.catalog import BookOption, ChapterOption, TranslationOption
from modules.commands import (
    CALLBACK_ACTIONS,
    INTERACTIONS_SLOT,
    LIMITER_SLOT,
    SERVICE_SLOT,
    SETTINGS_SLOT,
    _dispatch_callback,
    _interaction_callback_unlocked,
    interaction_callback,
    interaction_reply,
)
from modules.interactions import (
    InteractionStore,
    ReferenceSelection,
    SearchOptions,
    SearchResult,
)
from modules.service import SearchPage

EXPECTED_CALLBACK_ACTIONS = frozenset(
    {
        "bg",
        "bp",
        "bs",
        "bt",
        "cancel",
        "cb",
        "chback",
        "cp",
        "cs",
        "rpost",
        "radd",
        "rmore",
        "rrm",
        "rreset",
        "sb",
        "sbclear",
        "sbdone",
        "sbg",
        "sbp",
        "sbt",
        "sc",
        "sdash",
        "sdi",
        "sm",
        "snew",
        "so",
        "spost",
        "spr",
        "srp",
        "spv",
        "sq",
        "sreset",
        "srs",
        "srt",
        "ss",
        "st",
        "sw",
        "sx",
        "tc",
        "tp",
        "tr",
        "vback",
        "ve",
        "vep",
        "vone",
        "vs",
        "vsp",
    }
)


class _Limiter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def acquire(self, *, user_id: int, chat_id: int) -> None:
        self.calls.append((user_id, chat_id))

    async def should_notify_rejection(
        self,
        *,
        user_id: int,
        chat_id: int,
    ) -> bool:
        return True


class InteractiveFeatureTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.books = tuple(
            BookOption(number, f"Book {number}", f"{number:040x}")
            for number in range(1, 13)
        ) + (BookOption(43, "John", "f" * 40),)
        self.chapters = tuple(
            ChapterOption(number, tuple(range(1, 31)))
            for number in range(1, 31)
        )
        self.translations = (
            TranslationOption("kjv", "King James Version", "English"),
            TranslationOption("asv", "American Standard Version", "English"),
            TranslationOption("t1", "Translation 1", "Test"),
            TranslationOption("t2", "Translation 2", "Test"),
            TranslationOption("t3", "Translation 3", "Test"),
            TranslationOption("t4", "Translation 4", "Test"),
            TranslationOption("t5", "Translation 5", "Test"),
        )
        self.search_results = tuple(
            SearchResult(
                reference=f"John 1:{verse}",
                book_number=43,
                book_name="John",
                chapter=1,
                verse=verse,
                text=f"Result verse {verse}.",
            )
            for verse in range(1, 8)
        )
        self.service = SimpleNamespace(
            translations=AsyncMock(return_value=self.translations),
            books=AsyncMock(return_value=self.books),
            chapters=AsyncMock(return_value=self.chapters),
            search=AsyncMock(
                return_value=SearchPage(
                    query="grace",
                    translation="asv",
                    total=7,
                    items=self.search_results,
                )
            ),
            select=AsyncMock(
                return_value={
                    "asv_43_1": {
                        "book_name": "John",
                        "abbreviation": "asv",
                        "chapter": 1,
                        "verses": [
                            {
                                "verse": 1,
                                "text": "In the beginning.",
                            }
                        ],
                    }
                }
            ),
        )
        self.limiter = _Limiter()
        self.store = InteractionStore(max_sessions=20, ttl_seconds=60)
        settings = SimpleNamespace(
            default_translation="kjv",
            web_base_url="https://getbible.life",
            max_output_chunks=8,
            max_input_length=256,
            max_references=8,
            max_total_verses=100,
            audit_log_mode="metadata",
        )
        application = SimpleNamespace(
            bot_data={
                SETTINGS_SLOT: settings,
                SERVICE_SLOT: self.service,
                LIMITER_SLOT: self.limiter,
                INTERACTIONS_SLOT: self.store,
            }
        )
        self.context = SimpleNamespace(
            application=application,
            bot=SimpleNamespace(
                send_message=AsyncMock(
                    return_value=SimpleNamespace(message_id=901)
                ),
                send_chat_action=AsyncMock(),
                delete_message=AsyncMock(),
                edit_message_text=AsyncMock(),
            ),
            args=[],
        )

    @staticmethod
    def callback_update(
        token: str,
        action: str,
        value: str = "",
        *,
        user_id: int = 200,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=100),
            effective_user=SimpleNamespace(
                id=user_id,
                mention_html=lambda: f'<a href="tg://user?id={user_id}">Owner</a>',
            ),
            callback_query=SimpleNamespace(
                data=f"gb:{token}:{action}:{value}",
                answer=AsyncMock(),
            ),
        )

    @staticmethod
    def reply_update(
        prompt_message_id: int,
        text: str,
        *,
        user_id: int = 200,
        message_id: int = 902,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=100),
            effective_user=SimpleNamespace(id=user_id),
            effective_message=SimpleNamespace(
                chat_id=100,
                message_id=message_id,
                text=text,
                reply_to_message=SimpleNamespace(message_id=prompt_message_id),
            ),
        )

    async def dispatch(
        self,
        session,
        action: str,
        value: str = "",
    ) -> None:
        await _dispatch_callback(
            session,
            action,
            value,
            self.callback_update(session.token, action, value),
            self.context,
            self.service,
            self.store,
        )

    def test_callback_feature_inventory_is_explicit_and_complete(self) -> None:
        self.assertEqual(CALLBACK_ACTIONS, EXPECTED_CALLBACK_ACTIONS)
        source = "\n".join(
            (
                inspect.getsource(interaction_callback),
                inspect.getsource(_interaction_callback_unlocked),
                inspect.getsource(_dispatch_callback),
            )
        )
        implemented = set(re.findall(r'action == "([a-z]+)"', source))
        for group in re.findall(r"action in \{([^}]+)\}", source):
            implemented.update(re.findall(r'"([a-z]+)"', group))
        self.assertEqual(implemented, set(CALLBACK_ACTIONS))

    async def test_all_reference_picker_navigation_and_control_features(self) -> None:
        session = self.store.create(
            chat_id=100,
            user_id=200,
            kind="reference",
            stage="reference_translation",
            translation="kjv",
        )
        session.message_id = 300
        session.translations = self.translations

        await self.dispatch(session, "tp", "1")
        await self.dispatch(session, "tr", "asv")
        self.assertEqual(session.translation, "asv")
        self.assertEqual(session.stage, "reference_books")

        await self.dispatch(session, "bt")
        self.assertEqual(session.stage, "reference_translation")
        await self.dispatch(session, "tc")
        await self.dispatch(session, "bg", "ot")
        await self.dispatch(session, "bp", "1")
        await self.dispatch(session, "bs", "1")
        self.assertEqual(session.stage, "reference_chapter")

        await self.dispatch(session, "cb")
        self.assertEqual(session.stage, "reference_books")
        await self.dispatch(session, "bs", "1")
        await self.dispatch(session, "cp", "1")
        await self.dispatch(session, "cs", "1")
        self.assertEqual(session.stage, "reference_start")

        await self.dispatch(session, "chback")
        self.assertEqual(session.stage, "reference_chapter")
        await self.dispatch(session, "cs", "1")
        await self.dispatch(session, "vsp", "1")
        await self.dispatch(session, "vs", "5")
        self.assertEqual(session.stage, "reference_end")
        await self.dispatch(session, "vep", "0")
        await self.dispatch(session, "ve", "30")
        self.assertEqual(session.stage, "reference_review")

        await self.dispatch(session, "vback")
        await self.dispatch(session, "vs", "6")
        await self.dispatch(session, "vone")
        await self.dispatch(session, "rreset")
        self.assertEqual(session.stage, "reference_translation")
        self.assertIsNone(session.book)
        self.assertIsNone(session.chapter)

        session.book = BookOption(43, "John", "f" * 40)
        session.chapter = ChapterOption(1, tuple(range(1, 31)))
        session.start_verse = 1
        session.end_verse = 1
        session.reference_selections = [
            ReferenceSelection(43, "John", 1, 1, 1)
        ]
        session.translation = "asv"
        await self.dispatch(session, "rpost")
        self.service.select.assert_awaited()
        self.assertIsNone(
            self.store.get(session.token, chat_id=100, user_id=200)
        )

        cancelled = self.store.create(
            chat_id=100,
            user_id=200,
            kind="reference",
            stage="reference_translation",
            translation="kjv",
        )
        cancelled.message_id = 301
        await self.dispatch(cancelled, "cancel")
        self.assertIsNone(
            self.store.get(cancelled.token, chat_id=100, user_id=200)
        )

    async def test_all_search_filter_reply_navigation_and_post_features(self) -> None:
        session = self.store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_dashboard",
            translation="kjv",
        )
        session.message_id = 300

        await self.dispatch(session, "sdash")
        await self.dispatch(session, "st")
        self.assertEqual(session.stage, "search_translation")
        await self.dispatch(session, "tr", "asv")
        self.assertEqual(session.search_options.translation, "asv")

        await self.dispatch(session, "sw")
        await self.dispatch(session, "sm")
        await self.dispatch(session, "ss")
        await self.dispatch(session, "sc")
        await self.dispatch(session, "sdi")
        await self.dispatch(session, "so")
        self.assertEqual(session.search_options.words, "any")
        self.assertEqual(session.search_options.match, "substring")
        self.assertEqual(session.search_options.scope, "old_testament")
        self.assertTrue(session.search_options.case_sensitive)
        self.assertEqual(session.search_options.diacritics, "exact")
        self.assertEqual(session.search_options.sort, "relevance")

        await self.dispatch(session, "sb")
        await self.dispatch(session, "sbg", "ot")
        await self.dispatch(session, "sbp", "1")
        await self.dispatch(session, "sbt", "11")
        self.assertEqual(session.search_options.books, (11,))
        await self.dispatch(session, "sbclear")
        self.assertEqual(session.search_options.books, ())
        await self.dispatch(session, "sbt", "11")
        await self.dispatch(session, "sbdone")
        self.assertEqual(session.stage, "search_dashboard")

        await self.dispatch(session, "sx")
        self.assertEqual(session.stage, "search_exclude")
        await interaction_reply(
            self.reply_update(session.prompt_message_id or 0, "law death law"),
            self.context,
        )
        self.assertEqual(session.search_options.exclude, ("law", "death"))
        self.assertEqual(session.stage, "search_dashboard")

        await self.dispatch(session, "spr")
        self.assertEqual(session.stage, "search_proximity")
        await self.dispatch(session, "spv", "5")
        self.assertEqual(session.search_options.words, "all")
        self.assertEqual(session.search_options.proximity, 5)

        await self.dispatch(session, "sq")
        self.assertEqual(session.stage, "search_query")
        await interaction_reply(
            self.reply_update(session.prompt_message_id or 0, "grace"),
            self.context,
        )
        self.service.search.assert_awaited()
        self.assertEqual(session.stage, "search_results")
        self.assertEqual(len(session.search_results), 7)

        await self.dispatch(
            session,
            "srs",
            f"{session.search_generation}-nt",
        )
        self.assertEqual(session.search_options.scope, "new_testament")
        self.assertEqual(session.search_options.books, ())
        self.assertEqual(session.stage, "search_results")
        self.assertEqual(session.search_page, 0)

        await self.dispatch(session, "snew")
        self.assertEqual(session.stage, "search_query")
        await self.dispatch(session, "sdash")
        await self.dispatch(session, "sreset")
        self.assertEqual(
            session.search_options,
            SearchOptions(translation="kjv"),
        )

        session.search_options = SearchOptions(translation="asv")
        session.search_query = "grace"
        session.search_total = len(self.search_results)
        session.search_results = self.search_results
        session.stage = "search_results"
        callback = self.callback_update(
            session.token,
            "srt",
            f"{session.search_generation}-0",
        )
        await interaction_callback(callback, self.context)
        self.assertEqual(session.selected, {0})
        await interaction_callback(
            self.callback_update(
                session.token,
                "spost",
                str(session.search_generation),
            ),
            self.context,
        )
        self.service.select.assert_awaited()
        self.assertIsNone(
            self.store.get(session.token, chat_id=100, user_id=200)
        )
        self.assertTrue(
            {300, 901, 902}.issubset(
                {
                    call.kwargs["message_id"]
                    for call in self.context.bot.delete_message.await_args_list
                }
            )
        )

        cancelled = self.store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_dashboard",
            translation="kjv",
        )
        cancelled.message_id = 302
        await self.dispatch(cancelled, "cancel")
        self.assertIsNone(
            self.store.get(cancelled.token, chat_id=100, user_id=200)
        )

    async def test_reference_basket_adds_removes_and_continues(self) -> None:
        session = self.store.create(
            chat_id=100,
            user_id=200,
            kind="reference",
            stage="reference_review",
            translation="kjv",
        )
        session.message_id = 300
        session.books = self.books
        session.book = BookOption(43, "John", "f" * 40)
        session.chapters = self.chapters
        session.chapter = ChapterOption(3, tuple(range(1, 31)))
        session.reference_selections = [
            ReferenceSelection(43, "John", 3, 5, 7)
        ]

        await self.dispatch(session, "rmore")
        self.assertEqual(session.stage, "reference_start")
        await self.dispatch(session, "vs", "10")
        await self.dispatch(session, "vone")
        self.assertEqual(
            session.reference_selections,
            [
                ReferenceSelection(43, "John", 3, 5, 7),
                ReferenceSelection(43, "John", 3, 10, 10),
            ],
        )

        await self.dispatch(session, "rrm", "0")
        self.assertEqual(
            session.reference_selections,
            [ReferenceSelection(43, "John", 3, 10, 10)],
        )

        await self.dispatch(session, "radd")
        self.assertEqual(session.stage, "reference_books")
        self.assertIsNone(session.book)
        self.assertIsNone(session.chapter)
        self.assertEqual(
            session.reference_selections,
            [ReferenceSelection(43, "John", 3, 10, 10)],
        )

    async def test_invalid_expired_and_wrong_owner_controls_fail_closed(self) -> None:
        session = self.store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_dashboard",
            translation="kjv",
        )
        session.message_id = 300

        invalid = self.callback_update(session.token, "bogus")
        await interaction_callback(invalid, self.context)
        invalid.callback_query.answer.assert_awaited_once_with(
            "This control is invalid.",
            show_alert=True,
        )

        wrong_owner = self.callback_update(
            session.token,
            "sdash",
            user_id=201,
        )
        await interaction_callback(wrong_owner, self.context)
        wrong_owner.callback_query.answer.assert_awaited_once_with(
            "This selection expired. Run /bible or /search again.",
            show_alert=True,
        )

        malformed = self.callback_update(session.token, "sdash")
        malformed.callback_query.data = "not-a-getbible-control"
        await interaction_callback(malformed, self.context)
        malformed.callback_query.answer.assert_awaited_once_with(
            "This control is invalid.",
            show_alert=True,
        )


if __name__ == "__main__":
    unittest.main()
