import unittest

from modules.interactions import SearchResult
from modules.miniapp_auth import TelegramMiniAppPrincipal
from modules.miniapp_sessions import (
    MiniAppLaunchStore,
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

    def test_session_searches_and_basket_are_bounded_and_owner_scoped(
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
            max_searches_per_session=1,
            clock=clock,
        )
        session = store.create(
            _principal(7),
            translation="kjv",
            launch=launch,
            init_data_digest=b"x" * 32,
        )
        item = SearchResult(
            reference="John 3:16",
            book_number=43,
            book_name="John",
            chapter=3,
            verse=16,
            text="For God so loved the world.",
        )
        first = store.remember_search(
            session,
            query="loved",
            translation="kjv",
            total=1,
            items=(
                SearchResult(
                    reference=item.reference,
                    book_number=item.book_number,
                    book_name=item.book_name,
                    chapter=item.chapter,
                    verse=item.verse,
                    text=item.text,
                    terms=("loved",),
                ),
            ),
        )
        second = store.remember_search(
            session,
            query="world",
            translation="kjv",
            total=1,
            items=(
                SearchResult(
                    reference=item.reference,
                    book_number=item.book_number,
                    book_name=item.book_name,
                    chapter=item.chapter,
                    verse=item.verse,
                    text=item.text,
                    terms=("world",),
                ),
            ),
        )
        self.assertIsNone(store.search(session, first.token))
        self.assertIs(store.search(session, second.token), second)
        self.assertEqual(first.items[0].terms, ("loved",))
        self.assertEqual(second.items[0].terms, ("world",))
        self.assertEqual(first.items[0].token, second.items[0].token)

        store.add_to_basket(session, second.items[0].token)
        self.assertEqual(store.basket(session), (second.items[0],))
        store.clear_basket(session)
        self.assertEqual(store.basket(session), ())

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


if __name__ == "__main__":
    unittest.main()
