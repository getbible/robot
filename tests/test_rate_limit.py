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


if __name__ == "__main__":
    unittest.main()
