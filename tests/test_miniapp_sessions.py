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


def _principal(user_id: int) -> TelegramMiniAppPrincipal:
    return TelegramMiniAppPrincipal(
        user_id=user_id,
        auth_date=1,
        query_id="query",
        chat_id=None,
        chat_instance=None,
        chat_type=None,
        start_param=None,
    )


class MiniAppSessionStoreTestCase(unittest.TestCase):
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
            items=(item,),
        )
        second = store.remember_search(
            session,
            query="world",
            translation="kjv",
            total=1,
            items=(item,),
        )
        self.assertIsNone(store.search(session, first.token))
        self.assertIs(store.search(session, second.token), second)

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
        clock.value = 61
        self.assertIsNone(store.get(second.token))

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
