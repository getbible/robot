import unittest

from modules.interactions import InteractionStore


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class InteractionStoreTestCase(unittest.TestCase):
    def test_sessions_are_owner_scoped_and_expire(self) -> None:
        clock = _Clock()
        store = InteractionStore(
            max_sessions=10,
            ttl_seconds=60,
            clock=clock,
        )
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="reference",
            stage="reference_translation",
            translation="kjv",
        )

        self.assertIs(
            store.get(session.token, chat_id=100, user_id=200),
            session,
        )
        self.assertIsNone(
            store.get(session.token, chat_id=100, user_id=201)
        )

        clock.value = 61
        self.assertIsNone(
            store.get(session.token, chat_id=100, user_id=200)
        )
        self.assertEqual(store.snapshot()["expired"], 1)

    def test_attacker_created_sessions_are_lru_bounded(self) -> None:
        store = InteractionStore(max_sessions=2, ttl_seconds=60)
        first = store.create(
            chat_id=1,
            user_id=1,
            kind="search",
            stage="search_dashboard",
            translation="kjv",
        )
        store.create(
            chat_id=2,
            user_id=2,
            kind="search",
            stage="search_dashboard",
            translation="kjv",
        )
        store.create(
            chat_id=3,
            user_id=3,
            kind="search",
            stage="search_dashboard",
            translation="kjv",
        )

        self.assertIsNone(store.get(first.token, chat_id=1, user_id=1))
        state = store.snapshot()
        self.assertEqual(state["sessions"], 2)
        self.assertEqual(state["evicted"], 1)

    def test_prompt_lookup_requires_chat_user_and_message(self) -> None:
        store = InteractionStore(max_sessions=10, ttl_seconds=60)
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_query",
            translation="kjv",
        )
        session.prompt_message_id = 300

        self.assertIs(
            store.find_prompt(
                chat_id=100,
                user_id=200,
                prompt_message_id=300,
            ),
            session,
        )
        self.assertIsNone(
            store.find_prompt(
                chat_id=100,
                user_id=201,
                prompt_message_id=300,
            )
        )


if __name__ == "__main__":
    unittest.main()
