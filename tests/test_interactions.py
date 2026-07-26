import unittest

from modules.interactions import (
    MAX_WORKFLOW_MESSAGE_IDS,
    InteractionStore,
)


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

    def test_workflow_message_ledger_is_deduplicated_and_bounded(self) -> None:
        store = InteractionStore(max_sessions=10, ttl_seconds=60)
        session = store.create(
            chat_id=100,
            user_id=200,
            kind="search",
            stage="search_dashboard",
            translation="kjv",
        )
        session.message_id = 20
        session.prompt_message_id = 30
        session.remember_message(10)
        session.remember_message(10)
        session.remember_message(0)
        session.remember_message(None)
        for message_id in range(1000, 1000 + MAX_WORKFLOW_MESSAGE_IDS + 10):
            session.remember_message(message_id)

        self.assertEqual(
            len(session.workflow_message_ids),
            MAX_WORKFLOW_MESSAGE_IDS,
        )
        self.assertEqual(
            session.cleanup_message_ids()[-3:],
            (30, 20, 10),
        )

    def test_ephemeral_pending_input_is_owner_scoped(self) -> None:
        store = InteractionStore(max_sessions=10, ttl_seconds=60)
        session = store.create(
            chat_id=-100,
            user_id=200,
            kind="search",
            stage="search_query",
            translation="kjv",
        )
        session.ephemeral = True
        session.source_ephemeral_message_id = 700
        session.source_ephemeral_receiver_user_id = 999

        self.assertIs(
            store.find_pending_input(chat_id=-100, user_id=200),
            session,
        )
        self.assertIsNone(
            store.find_pending_input(chat_id=-100, user_id=201)
        )
        self.assertEqual(session.source_ephemeral_message_id, 700)
        self.assertEqual(session.source_ephemeral_receiver_user_id, 999)

        session.stage = "search_results"
        self.assertIsNone(
            store.find_pending_input(chat_id=-100, user_id=200)
        )


if __name__ == "__main__":
    unittest.main()
