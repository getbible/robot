import re
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import miniapp_sessions
from modules.bookmark_backup import BookmarkRestoreFile
from modules.miniapp_auth import TelegramMiniAppPrincipal
from modules.miniapp_sessions import (
    MiniAppLaunchStore,
    MiniAppSessionInputError,
    MiniAppSessionStore,
    miniapp_direct_url,
    miniapp_public_web_url,
    miniapp_web_url,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _principal(
    user_id: int,
    *,
    chat_id: int | None = None,
    chat_instance: str | None = None,
) -> TelegramMiniAppPrincipal:
    return TelegramMiniAppPrincipal(
        user_id=user_id,
        auth_date=1,
        query_id="query",
        chat_id=chat_id,
        chat_instance=chat_instance,
        chat_type=None,
        start_param=None,
    )


class MiniAppSessionStoreTestCase(unittest.TestCase):
    def test_process_budget_fits_supported_container_rss_headroom(self) -> None:
        root = Path(__file__).resolve().parents[1]
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        guard_match = re.search(
            r"CONTAINER_INSTANCE_MEMORY_LIMIT_MB:-([0-9]+)",
            compose,
        )
        self.assertIsNotNone(guard_match)
        guard_mib = int(guard_match.group(1))

        # Retained selections are the only content the robot keeps per reader.
        # They must stay a small slice of the child-RSS guard so the
        # interpreter, catalogue caches, and in-flight requests never compete
        # with them for the remaining headroom.
        self.assertLessEqual(
            miniapp_sessions.MAX_MINIAPP_PROCESS_RETAINED_BYTES,
            guard_mib * 1024 * 1024 // 8,
        )

    def test_each_user_has_a_small_independent_session_budget(self) -> None:
        launches = MiniAppLaunchStore(max_launches=10, ttl_seconds=60)
        store = MiniAppSessionStore(
            max_sessions=10,
            max_sessions_per_user=2,
            ttl_seconds=60,
        )
        sessions = []
        for _ in range(3):
            launch = launches.create_launch(user_id=7, target_chat_id=7)
            sessions.append(
                store.create(
                    _principal(7),
                    translation="kjv",
                    launch=launch,
                    init_data_digest=b"x" * 32,
                )
            )

        self.assertIsNone(store.get(sessions[0].token))
        self.assertIs(store.get(sessions[1].token), sessions[1])
        self.assertIs(store.get(sessions[2].token), sessions[2])
        self.assertEqual(store.snapshot()["sessions"], 2)
        self.assertEqual(store.snapshot()["evicted"], 1)

    def test_launch_is_owner_bound_one_time_and_expires(self) -> None:
        clock = _Clock()
        launches = MiniAppLaunchStore(
            max_launches=2,
            ttl_seconds=60,
            clock=clock,
        )
        launch = launches.create_launch(
            user_id=7,
            target_chat_id=-100,
            message_thread_id=5,
            initial_route="search",
            initial_query="grace",
        )

        self.assertIsNone(launches.consume(launch.token, user_id=8))
        self.assertIs(launches.consume(launch.token, user_id=7), launch)
        self.assertIsNone(launches.consume(launch.token, user_id=7))

        expired = launches.create_launch(user_id=7, target_chat_id=7)
        clock.value = 61
        self.assertIsNone(launches.consume(expired.token, user_id=7))

    def test_launch_prompt_is_retained_for_post_success_cleanup(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        private_launch = launches.create_launch(user_id=7, target_chat_id=7)
        updated = launches.remember_prompt(private_launch, message_id=101)

        self.assertEqual(updated.prompt_message_id, 101)
        self.assertIsNone(updated.prompt_ephemeral_message_id)
        self.assertIs(
            launches.consume(private_launch.token, user_id=7),
            updated,
        )

        ephemeral_launch = launches.create_launch(
            user_id=7,
            target_chat_id=-100,
            source_ephemeral_message_id=50,
            source_ephemeral_receiver_user_id=99,
        )
        updated_ephemeral = launches.remember_prompt(
            ephemeral_launch,
            ephemeral_message_id=202,
        )
        self.assertEqual(updated_ephemeral.prompt_ephemeral_message_id, 202)
        self.assertEqual(updated_ephemeral.source_ephemeral_message_id, 50)
        self.assertEqual(
            updated_ephemeral.source_ephemeral_receiver_user_id,
            99,
        )
        with self.assertRaises(ValueError):
            launches.remember_prompt(
                updated_ephemeral,
                message_id=1,
                ephemeral_message_id=2,
            )
        with self.assertRaises(ValueError):
            launches.create_launch(
                user_id=7,
                target_chat_id=-100,
                source_ephemeral_message_id=51,
            )

    def test_bookmark_restore_launch_is_private_owner_bound_and_acknowledged(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        restore = BookmarkRestoreFile.validated(
            file_id="telegram-file-id",
            file_unique_id="telegram-unique-id",
            file_name="getbible-bookmarks.json",
            file_size=1024,
        )
        launch = launches.create_launch(
            user_id=7,
            target_chat_id=7,
            initial_route="bookmarks",
            bookmark_restore=restore,
        )
        store = MiniAppSessionStore(max_sessions=2, ttl_seconds=60)
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launch,
            init_data_digest=b"x" * 32,
        )

        self.assertIs(store.bookmark_restore(session), restore)
        self.assertTrue(store.acknowledge_bookmark_restore(session))
        self.assertIsNone(store.bookmark_restore(session))
        self.assertFalse(store.acknowledge_bookmark_restore(session))

        with self.assertRaisesRegex(ValueError, "private chat"):
            launches.create_launch(
                user_id=7,
                target_chat_id=-100,
                initial_route="bookmarks",
                bookmark_restore=restore,
            )

    def test_prompt_attachment_survives_launch_exchange_race(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        launch = launches.create_launch(user_id=7, target_chat_id=-100)
        consumed = launches.consume(launch.token, user_id=7)
        self.assertIs(consumed, launch)

        store = MiniAppSessionStore(max_sessions=2, ttl_seconds=60)
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=consumed,
            init_data_digest=b"x" * 32,
        )
        updated = launches.remember_prompt(launch, ephemeral_message_id=303)

        self.assertIs(updated, launch)
        self.assertEqual(session.launch.prompt_ephemeral_message_id, 303)

    def test_active_session_can_rebind_to_a_reopened_owner_launch(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        launch = launches.create_launch(user_id=7, target_chat_id=7)
        store = MiniAppSessionStore(max_sessions=2, ttl_seconds=60)
        principal = _principal(7)
        session = store.create(
            principal,
            translation="kjv",
            launch=launch,
            init_data_digest=b"x" * 32,
        )

        self.assertIs(
            store.find_by_launch(launch.token, user_id=7),
            session,
        )
        self.assertIsNone(store.find_by_launch(launch.token, user_id=8))
        self.assertTrue(
            store.rebind(
                session,
                principal,
                init_data_digest=b"y" * 32,
            )
        )
        self.assertEqual(session.init_data_digest, b"y" * 32)

    def test_active_session_is_recoverable_only_by_owner_and_exact_init_data(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        store = MiniAppSessionStore(max_sessions=2, ttl_seconds=60)
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launches.create_launch(user_id=7, target_chat_id=7),
            init_data_digest=b"x" * 32,
        )
        self.assertIs(
            store.find_by_init_data(b"x" * 32, user_id=7),
            session,
        )
        self.assertIsNone(store.find_by_init_data(b"x" * 32, user_id=8))
        self.assertIsNone(store.find_by_init_data(b"y" * 32, user_id=7))

    def test_rebind_rejects_a_different_signed_chat_context(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        launch = launches.create_launch(user_id=7, target_chat_id=-100)
        store = MiniAppSessionStore(max_sessions=2, ttl_seconds=60)
        session = store.create(
            _principal(7, chat_id=-100, chat_instance="chat-a"),
            translation="kjv",
            launch=launch,
            init_data_digest=b"x" * 32,
        )

        self.assertFalse(
            store.rebind(
                session,
                _principal(7, chat_id=-200, chat_instance="chat-a"),
                init_data_digest=b"y" * 32,
            )
        )
        self.assertFalse(
            store.rebind(
                session,
                _principal(7, chat_id=-100, chat_instance="chat-b"),
                init_data_digest=b"y" * 32,
            )
        )
        self.assertEqual(session.init_data_digest, b"x" * 32)

    def test_session_selections_and_basket_are_bounded_and_owner_scoped(
        self,
    ) -> None:
        clock = _Clock()
        launches = MiniAppLaunchStore(
            max_launches=10,
            ttl_seconds=60,
            clock=clock,
        )
        launch = launches.create_launch(user_id=7, target_chat_id=-100)
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=60,
            clock=clock,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launch,
            init_data_digest=b"x" * 32,
        )
        first = store.register_selection(
            session,
            reference="John 3:16",
            translation="kjv",
            book_number=43,
            book_name="John",
            chapter=3,
            verse=16,
            text="For God so loved the world.",
        )
        # The same verse read again (a reload, another translation view of the
        # chapter) keeps the opaque ID the page already holds and simply
        # refreshes the authoritative text behind it.
        second = store.register_selection(
            session,
            reference="John 3:16",
            translation="kjv",
            book_number=43,
            book_name="John",
            chapter=3,
            verse=16,
            text="For God so loved the world, that he gave his only begotten Son.",
        )
        self.assertEqual(first.token, second.token)
        self.assertIs(session.available_selections[first.token], second)
        self.assertEqual(len(session.available_selections), 1)

        store.add_to_basket(session, second.token)
        self.assertEqual(store.basket(session), (second,))
        refreshed = store.register_selection(
            session,
            reference="John 3:16",
            translation="kjv",
            book_number=43,
            book_name="John",
            chapter=3,
            verse=16,
            text="For God so loved the world.",
        )
        self.assertEqual(refreshed.token, first.token)
        self.assertEqual(store.basket(session), (refreshed,))
        self.assertEqual(store.snapshot()["available_selections"], 1)
        self.assertNotIn("searches", store.snapshot())

        with self.assertRaisesRegex(MiniAppSessionInputError, "invalid or expired"):
            store.add_to_basket(session, "b" * 24)
        store.clear_basket(session)
        self.assertEqual(store.basket(session), ())

        other = store.create(
            _principal(8),
            translation="kjv",
            launch=launches.create_launch(user_id=8, target_chat_id=8),
            init_data_digest=b"y" * 32,
        )
        with self.assertRaisesRegex(MiniAppSessionInputError, "invalid or expired"):
            store.add_to_basket(other, first.token)
        store.revoke(session.token)
        with self.assertRaises(miniapp_sessions.MiniAppSessionExpiredError):
            store.register_selection(
                session,
                reference="John 3:16",
                translation="kjv",
                book_number=43,
                book_name="John",
                chapter=3,
                verse=16,
                text="For God so loved the world.",
            )

    def test_session_store_rejects_out_of_range_bounds(self) -> None:
        for changes, message in (
            ({"max_sessions": 0}, "max_sessions must be between 1 and 100000."),
            ({"ttl_seconds": 29}, "ttl_seconds must be between 30 and 15552000."),
            (
                {"max_sessions_per_user": 11},
                "max_sessions_per_user must be between 1 and 10.",
            ),
            (
                {"max_basket_selections": 201},
                "max_basket_selections must be between 1 and 200.",
            ),
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, re.escape(message)),
            ):
                MiniAppSessionStore(
                    **{"max_sessions": 2, "ttl_seconds": 60, **changes}
                )

    def test_basket_reorder_requires_every_current_selection_exactly_once(
        self,
    ) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        store = MiniAppSessionStore(max_sessions=2, ttl_seconds=60)
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launches.create_launch(user_id=7, target_chat_id=7),
            init_data_digest=b"x" * 32,
        )
        verses = tuple(
            store.register_selection(
                session,
                reference=f"John 3:{verse}",
                translation="kjv",
                book_number=43,
                book_name="John",
                chapter=3,
                verse=verse,
                text=f"Verse {verse}.",
            )
            for verse in (1, 2, 16)
        )
        for selection in verses:
            store.add_to_basket(session, selection.token)
        tokens = [selection.token for selection in verses]

        reordered = store.reorder_basket(session, list(reversed(tokens)))
        self.assertEqual(
            [item.token for item in reordered],
            list(reversed(tokens)),
        )
        self.assertEqual(store.basket(session), reordered)

        for invalid in (
            tokens[:2],
            [*tokens, tokens[0]],
            [tokens[0], tokens[1], "c" * 24],
        ):
            with (
                self.subTest(order=invalid),
                self.assertRaisesRegex(
                    MiniAppSessionInputError,
                    "every current selection once",
                ),
            ):
                store.reorder_basket(session, invalid)
        self.assertEqual(store.basket(session), reordered)

        remaining = store.remove_from_basket(session, tokens[1])
        self.assertEqual(
            [item.token for item in remaining],
            [tokens[2], tokens[0]],
        )
        self.assertEqual(store.remove_from_basket(session, "d" * 24), remaining)

        store.revoke(session.token)
        expired = miniapp_sessions.MiniAppSessionExpiredError
        with self.assertRaises(expired):
            store.reorder_basket(session, [tokens[2], tokens[0]])
        with self.assertRaises(expired):
            store.remove_from_basket(session, tokens[0])
        with self.assertRaises(expired):
            store.clear_basket(session)
        with self.assertRaises(expired):
            store.basket(session)

    def test_basket_capacity_uses_a_typed_client_safe_error(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=60,
            max_basket_selections=1,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launches.create_launch(user_id=7, target_chat_id=7),
            init_data_digest=b"x" * 32,
        )
        first = store.register_selection(
            session,
            reference="John 3:1",
            translation="kjv",
            book_number=43,
            book_name="John",
            chapter=3,
            verse=1,
            text="First verse.",
        )
        second = store.register_selection(
            session,
            reference="John 3:2",
            translation="kjv",
            book_number=43,
            book_name="John",
            chapter=3,
            verse=2,
            text="Second verse.",
        )
        store.add_to_basket(session, first.token)

        with self.assertRaisesRegex(
            MiniAppSessionInputError,
            "Scripture basket is full",
        ):
            store.add_to_basket(session, second.token)

    def test_basket_identity_survives_available_selection_eviction(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        launch = launches.create_launch(user_id=7, target_chat_id=7)
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=60,
            max_available_selections=250,
            max_basket_selections=50,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launch,
            init_data_digest=b"x" * 32,
        )
        selected = store.register_selection(
            session,
            reference="John 3:1",
            translation="kjv",
            book_number=43,
            book_name="John",
            chapter=3,
            verse=1,
            text="Selected verse.",
        )
        store.add_to_basket(session, selected.token)

        for verse in range(2, 253):
            store.register_selection(
                session,
                reference=f"John 3:{verse}",
                translation="kjv",
                book_number=43,
                book_name="John",
                chapter=3,
                verse=verse,
                text=f"Verse {verse}.",
            )

        revisited = store.register_selection(
            session,
            reference="John 3:1",
            translation="kjv",
            book_number=43,
            book_name="John",
            chapter=3,
            verse=1,
            text="Updated selected verse.",
        )
        store.add_to_basket(session, revisited.token)

        self.assertEqual(revisited.token, selected.token)
        self.assertIn(selected.token, session.available_selections)
        self.assertLessEqual(len(session.available_selections), 251)
        self.assertEqual(len(store.basket(session)), 1)
        self.assertEqual(
            store.basket(session)[0].text,
            "Updated selected verse.",
        )

    def test_full_chapter_remains_selectable_with_existing_basket(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        launch = launches.create_launch(user_id=7, target_chat_id=7)
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=60,
            max_available_selections=250,
            max_basket_selections=100,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launch,
            init_data_digest=b"x" * 32,
        )
        for verse in range(1, 100):
            selected = store.register_selection(
                session,
                reference=f"Genesis 1:{verse}",
                translation="kjv",
                book_number=1,
                book_name="Genesis",
                chapter=1,
                verse=verse,
                text=f"Basket verse {verse}.",
            )
            store.add_to_basket(session, selected.token)

        chapter = [
            store.register_selection(
                session,
                reference=f"Psalms 119:{verse}",
                translation="kjv",
                book_number=19,
                book_name="Psalms",
                chapter=119,
                verse=verse,
                text=f"Chapter verse {verse}.",
            )
            for verse in range(1, 251)
        ]

        basket = store.add_to_basket(session, chapter[0].token)
        self.assertEqual(len(basket), 100)
        self.assertEqual(basket[-1].reference, "Psalms 119:1")
        self.assertIn(chapter[0].token, session.available_selections)
        self.assertLessEqual(len(session.available_selections), 350)

    def test_large_maximum_basket_preserves_every_chapter_selection(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=60,
            max_available_selections=250,
            max_basket_selections=200,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launches.create_launch(user_id=7, target_chat_id=7),
            init_data_digest=b"x" * 32,
        )
        for verse in range(1, 200):
            selection = store.register_selection(
                session,
                reference="📖" * 180,
                translation="kjv",
                book_number=43,
                book_name="📚" * 128,
                chapter=1,
                verse=verse,
                text="😀" * 1024,
            )
            store.add_to_basket(session, selection.token)

        # Two more maximal chapters browsed before the one under test fill the
        # available-selection budget on top of the protected basket.
        for book_number, chapter_number in ((44, 1), (43, 4)):
            for verse in range(1, 201):
                store.register_selection(
                    session,
                    reference="🔖" * 180,
                    translation="kjv",
                    book_number=book_number,
                    book_name="📚" * 128,
                    chapter=chapter_number,
                    verse=verse,
                    text="😀" * 1024,
                )

        chapter = tuple(
            store.register_selection(
                session,
                reference=f"John 2:{verse}",
                translation="kjv",
                book_number=43,
                book_name="John",
                chapter=2,
                verse=verse,
                text="😀" * 1024,
            )
            for verse in range(1, 251)
        )

        self.assertTrue(
            all(
                selection.token in session.available_selections
                for selection in chapter
            )
        )
        store.add_to_basket(session, chapter[0].token)
        store.remove_from_basket(session, chapter[0].token)
        store.add_to_basket(session, chapter[-1].token)
        self.assertIn(
            chapter[-1].token,
            {item.token for item in store.basket(session)},
        )
        self.assertLessEqual(
            session.retained_selection_bytes,
            miniapp_sessions.MAX_MINIAPP_SESSION_RETAINED_BYTES,
        )

    def test_evicted_chapter_selection_is_reissued_on_the_next_read(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=60,
            max_available_selections=250,
            max_basket_selections=50,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launches.create_launch(user_id=7, target_chat_id=7),
            init_data_digest=b"x" * 32,
        )
        genesis = tuple(
            store.register_selection(
                session,
                reference=f"Genesis 1:{verse}",
                translation="kjv",
                book_number=1,
                book_name="Genesis",
                chapter=1,
                verse=verse,
                text=f"Genesis verse {verse}.",
            )
            for verse in range(1, 201)
        )
        for verse in range(1, 251):
            store.register_selection(
                session,
                reference=f"Psalms 119:{verse}",
                translation="kjv",
                book_number=19,
                book_name="Psalms",
                chapter=119,
                verse=verse,
                text=f"Chapter verse {verse}.",
            )

        # A complete later chapter displaces the unselected earlier one, so the
        # stale opaque ID is refused rather than resolved to a guess.
        self.assertNotIn(genesis[0].token, session.available_selections)
        with self.assertRaisesRegex(MiniAppSessionInputError, "invalid or expired"):
            store.add_to_basket(session, genesis[0].token)

        reader_selection = store.register_selection(
            session,
            reference="Genesis 1:1",
            translation="kjv",
            book_number=1,
            book_name="Genesis",
            chapter=1,
            verse=1,
            text="Reader verse 1.",
        )
        self.assertNotEqual(reader_selection.token, genesis[0].token)

        basket = store.add_to_basket(session, reader_selection.token)
        repeated = store.add_to_basket(session, reader_selection.token)

        self.assertEqual(basket[-1].reference, "Genesis 1:1")
        self.assertEqual(basket[-1].text, "Reader verse 1.")
        self.assertEqual(repeated, basket)
        self.assertIn(reader_selection.token, session.available_selections)

    def test_available_selection_budget_covers_a_complete_chapter(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 250 and 5000"):
            MiniAppSessionStore(
                max_sessions=2,
                ttl_seconds=60,
                max_available_selections=249,
            )

    def test_selection_text_has_an_independent_byte_bound(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        launch = launches.create_launch(user_id=7, target_chat_id=7)
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=60,
            max_available_selections=250,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launch,
            init_data_digest=b"x" * 32,
        )
        with self.assertRaisesRegex(ValueError, "retained display bound"):
            store.register_selection(
                session,
                reference="John 3:16",
                translation="kjv",
                book_number=43,
                book_name="John",
                chapter=3,
                verse=16,
                text="x" * 4097,
            )

    def test_retained_selection_is_bounded_using_utf8_bytes(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        launch = launches.create_launch(user_id=7, target_chat_id=7)
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=60,
            max_available_selections=250,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launch,
            init_data_digest=b"x" * 32,
        )
        text = "界" * 1000
        with patch.object(
            miniapp_sessions,
            "MAX_MINIAPP_SESSION_RETAINED_BYTES",
            12_000,
        ):
            for verse in range(1, 10):
                store.register_selection(
                    session,
                    reference=f"John 3:{verse}",
                    translation="kjv",
                    book_number=43,
                    book_name="John",
                    chapter=3,
                    verse=verse,
                    text=text,
                )

        self.assertLessEqual(session.retained_selection_bytes, 12_000)
        self.assertLessEqual(len(session.available_selections), 4)
        self.assertEqual(
            store.snapshot()["retained_selection_bytes"],
            session.retained_selection_bytes,
        )

    def test_process_text_budget_evicts_the_oldest_other_session(self) -> None:
        launches = MiniAppLaunchStore(max_launches=4, ttl_seconds=60)
        store = MiniAppSessionStore(
            max_sessions=4,
            ttl_seconds=60,
            max_available_selections=250,
        )
        first = store.create(
            _principal(7),
            translation="kjv",
            launch=launches.create_launch(user_id=7, target_chat_id=7),
            init_data_digest=b"x" * 32,
        )
        second = store.create(
            _principal(8),
            translation="kjv",
            launch=launches.create_launch(user_id=8, target_chat_id=8),
            init_data_digest=b"y" * 32,
        )
        text = "x" * 4000
        with patch.object(
            miniapp_sessions,
            "MAX_MINIAPP_PROCESS_RETAINED_BYTES",
            24_000,
        ):
            for verse in range(1, 4):
                store.register_selection(
                    first,
                    reference=f"John 3:{verse}",
                    translation="kjv",
                    book_number=43,
                    book_name="John",
                    chapter=3,
                    verse=verse,
                    text=text,
                )
            for verse in range(1, 3):
                store.register_selection(
                    second,
                    reference=f"John 4:{verse}",
                    translation="kjv",
                    book_number=43,
                    book_name="John",
                    chapter=4,
                    verse=verse,
                    text=text,
                )

        self.assertIsNone(store.get(first.token, touch=False))
        self.assertIs(store.get(second.token, touch=False), second)
        self.assertLessEqual(
            store.snapshot()["retained_selection_bytes"],
            24_000,
        )

    def test_retained_selection_budget_counts_metadata_and_objects(self) -> None:
        launches = MiniAppLaunchStore(max_launches=2, ttl_seconds=60)
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=60,
            max_available_selections=250,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launches.create_launch(user_id=7, target_chat_id=7),
            init_data_digest=b"x" * 32,
        )
        with (
            patch.object(
                miniapp_sessions,
                "MAX_MINIAPP_SESSION_RETAINED_BYTES",
                20_000,
            ),
            patch.object(
                miniapp_sessions,
                "MAX_MINIAPP_SESSION_RETAINED_SELECTIONS",
                10,
            ),
        ):
            for verse in range(1, 30):
                store.register_selection(
                    session,
                    reference="r" * 300,
                    translation="kjv",
                    book_number=43,
                    book_name="  John  ",
                    chapter=3,
                    verse=verse,
                    text="x",
                )

        self.assertLessEqual(session.retained_selection_bytes, 20_000)
        self.assertLessEqual(session.retained_selection_count, 10)
        retained = tuple(session.available_selections.values())
        self.assertTrue(retained)
        self.assertTrue(all(len(item.reference) <= 180 for item in retained))
        self.assertTrue(all(item.book_name == "John" for item in retained))
        self.assertEqual(
            {item.reference for item in retained},
            {f"John 3:{item.verse}" for item in retained},
        )
        self.assertEqual(
            store.snapshot()["retained_selection_count"],
            session.retained_selection_count,
        )

    def test_process_selection_count_budget_evicts_oldest_session(self) -> None:
        launches = MiniAppLaunchStore(max_launches=4, ttl_seconds=60)
        store = MiniAppSessionStore(
            max_sessions=4,
            ttl_seconds=60,
            max_available_selections=250,
        )
        first = store.create(
            _principal(7),
            translation="kjv",
            launch=launches.create_launch(user_id=7, target_chat_id=7),
            init_data_digest=b"x" * 32,
        )
        second = store.create(
            _principal(8),
            translation="kjv",
            launch=launches.create_launch(user_id=8, target_chat_id=8),
            init_data_digest=b"y" * 32,
        )
        with patch.object(
            miniapp_sessions,
            "MAX_MINIAPP_PROCESS_RETAINED_SELECTIONS",
            5,
        ):
            for verse in range(1, 5):
                store.register_selection(
                    first,
                    reference=f"John 3:{verse}",
                    translation="kjv",
                    book_number=43,
                    book_name="John",
                    chapter=3,
                    verse=verse,
                    text="x",
                )
            for verse in range(1, 3):
                store.register_selection(
                    second,
                    reference=f"John 4:{verse}",
                    translation="kjv",
                    book_number=43,
                    book_name="John",
                    chapter=4,
                    verse=verse,
                    text="x",
                )

        self.assertIsNone(store.get(first.token, touch=False))
        self.assertIs(store.get(second.token, touch=False), second)
        self.assertLessEqual(
            store.snapshot()["retained_selection_count"],
            5,
        )

    def test_session_lru_and_ttl_are_enforced(self) -> None:
        clock = _Clock()
        launches = MiniAppLaunchStore(
            max_launches=10,
            ttl_seconds=60,
            clock=clock,
        )
        store = MiniAppSessionStore(
            max_sessions=1,
            ttl_seconds=60,
            clock=clock,
        )
        first = store.create(
            _principal(1),
            translation="kjv",
            launch=launches.create_launch(user_id=1, target_chat_id=1),
            init_data_digest=b"x" * 32,
        )
        store.create(
            _principal(2),
            translation="kjv",
            launch=launches.create_launch(user_id=2, target_chat_id=2),
            init_data_digest=b"y" * 32,
        )
        self.assertIsNone(store.get(first.token))
        self.assertEqual(store.snapshot()["evicted"], 1)

        second = next(iter(store._sessions.values()))
        clock.value = 59
        self.assertIs(store.get(second.token), second)
        clock.value = 61
        self.assertIsNone(store.get(second.token))

    def test_three_hour_reader_session_uses_an_absolute_boundary(self) -> None:
        clock = _Clock()
        launches = MiniAppLaunchStore(
            max_launches=2,
            ttl_seconds=60,
            clock=clock,
        )
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=10_800,
            clock=clock,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launches.create_launch(user_id=7, target_chat_id=7),
            init_data_digest=b"x" * 32,
        )

        clock.value = 10_799
        self.assertIs(store.get(session.token), session)
        clock.value = 10_800
        self.assertIsNone(store.get(session.token))

    def test_post_attempts_are_basket_bound_and_failed_retries_remain_closed(
        self,
    ) -> None:
        clock = _Clock()
        launches = MiniAppLaunchStore(
            max_launches=10,
            ttl_seconds=60,
            clock=clock,
        )
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=60,
            clock=clock,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launches.create_launch(user_id=7, target_chat_id=7),
            init_data_digest=b"x" * 32,
        )
        digest = b"a" * 32
        attempt, created = store.begin_post(session, "abcdef0123456789", digest)
        self.assertTrue(created)
        self.assertEqual(attempt.state, "pending")

        same, created = store.begin_post(session, "fedcba9876543210", digest)
        self.assertFalse(created)
        self.assertIs(same, attempt)

        store.fail_post(session, "abcdef0123456789", digest)
        self.assertEqual(attempt.state, "failed")
        with self.assertRaisesRegex(ValueError, "different basket"):
            store.begin_post(
                session,
                "abcdef0123456789",
                b"b" * 32,
            )

    def test_launch_url_helpers_never_embed_chat_or_query_content(self) -> None:
        token = "abcdefghijklmnop"
        self.assertEqual(
            miniapp_web_url("https://robot.example/app", token),
            "https://robot.example/app/?launch=abcdefghijklmnop",
        )
        self.assertEqual(
            miniapp_web_url("https://robot.example", token),
            "https://robot.example/?launch=abcdefghijklmnop",
        )
        self.assertEqual(
            miniapp_public_web_url("https://robot.example/getbible"),
            "https://robot.example/getbible/",
        )
        self.assertEqual(
            miniapp_direct_url("GetBibleBot", token),
            "https://t.me/GetBibleBot?startapp=abcdefghijklmnop&mode=compact",
        )


class MiniAppSessionConcurrencyTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_bookmark_io_pins_an_expired_session_until_release(self) -> None:
        clock = _Clock()
        launches = MiniAppLaunchStore(
            max_launches=2,
            ttl_seconds=60,
            clock=clock,
        )
        store = MiniAppSessionStore(
            max_sessions=2,
            ttl_seconds=60,
            clock=clock,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launches.create_launch(user_id=7, target_chat_id=7),
            init_data_digest=b"x" * 32,
        )

        async with session.bookmark_io_lock:
            clock.value = 61
            self.assertIs(store.get(session.token, touch=False), session)
            self.assertEqual(store.snapshot()["sessions"], 1)

        self.assertIsNone(store.get(session.token, touch=False))


if __name__ == "__main__":
    unittest.main()
