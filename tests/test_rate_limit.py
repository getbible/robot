import unittest

from modules.errors import RobotRateLimited
from modules.rate_limit import InboundRateLimiter


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class InboundRateLimiterTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_user_and_chat_budgets_are_enforced_atomically(self) -> None:
        clock = _Clock()
        limiter = InboundRateLimiter(
            user_capacity=1,
            user_refill_per_second=1.0,
            chat_capacity=2,
            chat_refill_per_second=1.0,
            max_entries=10,
            clock=clock,
        )
        await limiter.acquire(user_id=1, chat_id=10)
        with self.assertRaises(RobotRateLimited) as raised:
            await limiter.acquire(user_id=1, chat_id=10)
        self.assertEqual(raised.exception.retry_after, 1)

        clock.value = 1.0
        await limiter.acquire(user_id=1, chat_id=10)
        self.assertEqual(limiter.snapshot()["allowed"], 2)
        self.assertEqual(limiter.snapshot()["rejected"], 1)

    async def test_attacker_controlled_identifiers_cannot_grow_state(self) -> None:
        limiter = InboundRateLimiter(
            user_capacity=10,
            user_refill_per_second=1.0,
            chat_capacity=10,
            chat_refill_per_second=1.0,
            max_entries=4,
        )
        for identifier in range(20):
            await limiter.acquire(user_id=identifier, chat_id=identifier + 100)
        state = limiter.snapshot()
        self.assertLessEqual(state["entries"], 4)
        self.assertGreater(state["evictions"], 0)

    async def test_rejection_notifications_have_a_bounded_cooldown(self) -> None:
        clock = _Clock()
        limiter = InboundRateLimiter(
            user_capacity=1,
            user_refill_per_second=1.0,
            chat_capacity=1,
            chat_refill_per_second=1.0,
            max_entries=2,
            notification_cooldown=10.0,
            clock=clock,
        )

        self.assertTrue(
            await limiter.should_notify_rejection(user_id=1, chat_id=10)
        )
        self.assertFalse(
            await limiter.should_notify_rejection(user_id=1, chat_id=10)
        )
        clock.value = 10.0
        self.assertTrue(
            await limiter.should_notify_rejection(user_id=1, chat_id=10)
        )

        for identifier in range(20):
            await limiter.should_notify_rejection(
                user_id=identifier,
                chat_id=identifier + 100,
            )
        self.assertLessEqual(limiter.snapshot()["notification_entries"], 2)

    async def test_repeated_user_rejections_open_and_clear_temporary_block(
        self,
    ) -> None:
        clock = _Clock()
        limiter = InboundRateLimiter(
            user_capacity=1,
            user_refill_per_second=0.01,
            chat_capacity=100,
            chat_refill_per_second=100,
            client_capacity=100,
            client_refill_per_second=100,
            max_entries=20,
            abuse_rejection_threshold=2,
            abuse_window_seconds=60,
            abuse_block_seconds=30,
            clock=clock,
        )
        await limiter.acquire(user_id=1, chat_id=10)
        with self.assertRaises(RobotRateLimited) as first:
            await limiter.acquire(user_id=1, chat_id=10)
        self.assertFalse(first.exception.blocked)
        self.assertEqual(first.exception.violation_count, 1)

        with self.assertRaises(RobotRateLimited) as escalated:
            await limiter.acquire(user_id=1, chat_id=10)
        self.assertTrue(escalated.exception.blocked)
        self.assertTrue(escalated.exception.new_block)
        self.assertIn("user", escalated.exception.scopes)

        with self.assertRaises(RobotRateLimited) as blocked:
            await limiter.acquire(user_id=1, chat_id=10)
        self.assertTrue(blocked.exception.blocked)
        self.assertFalse(blocked.exception.new_block)
        self.assertEqual(limiter.snapshot()["active_blocks"], 1)

        clock.value = 31
        with self.assertRaises(RobotRateLimited) as after_block:
            await limiter.acquire(user_id=1, chat_id=10)
        self.assertFalse(after_block.exception.blocked)
        self.assertEqual(limiter.snapshot()["active_blocks"], 0)

    async def test_client_budget_is_independent_and_identity_state_is_bounded(
        self,
    ) -> None:
        limiter = InboundRateLimiter(
            user_capacity=100,
            user_refill_per_second=100,
            chat_capacity=100,
            chat_refill_per_second=100,
            client_capacity=1,
            client_refill_per_second=0.01,
            max_entries=8,
        )
        await limiter.acquire(
            user_id=1,
            chat_id=10,
            client_key="192.0.2.10",
        )
        with self.assertRaises(RobotRateLimited) as rejected:
            await limiter.acquire(
                user_id=2,
                chat_id=20,
                client_key="192.0.2.10",
            )
        self.assertIn("client", rejected.exception.scopes)
        self.assertEqual(rejected.exception.client_key, "192.0.2.10")

        for identifier in range(20):
            await limiter.acquire(
                user_id=identifier + 100,
                chat_id=identifier + 200,
                client_key=f"192.0.2.{identifier + 20}",
            )
        state = limiter.snapshot()
        self.assertLessEqual(state["entries"], 8)
        self.assertLessEqual(state["abuse_entries"], 8)


if __name__ == "__main__":
    unittest.main()
