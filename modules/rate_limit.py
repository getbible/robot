"""Bounded inbound token-bucket rate limiting."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from .errors import RobotRateLimited


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class InboundRateLimiter:
    """Atomically enforces per-user and per-chat token buckets.

    State is least-recently-used and bounded so arbitrary identifiers cannot grow
    process memory indefinitely.
    """

    def __init__(
        self,
        *,
        user_capacity: int,
        user_refill_per_second: float,
        chat_capacity: int,
        chat_refill_per_second: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._user_capacity = float(user_capacity)
        self._user_refill = float(user_refill_per_second)
        self._chat_capacity = float(chat_capacity)
        self._chat_refill = float(chat_refill_per_second)
        self._max_entries = max_entries
        self._clock = clock
        self._buckets: OrderedDict[tuple[str, int], _Bucket] = OrderedDict()
        self._lock = asyncio.Lock()
        self._allowed = 0
        self._rejected = 0
        self._evictions = 0

    async def acquire(self, *, user_id: int, chat_id: int, cost: float = 1.0) -> None:
        if cost <= 0:
            raise ValueError("cost must be positive.")
        now = self._clock()
        async with self._lock:
            user_key = ("user", user_id)
            chat_key = ("chat", chat_id)
            user = self._refilled(
                user_key, now, self._user_capacity, self._user_refill
            )
            chat = self._refilled(
                chat_key, now, self._chat_capacity, self._chat_refill
            )

            retry_after = 0.0
            if user.tokens < cost:
                retry_after = max(
                    retry_after, (cost - user.tokens) / self._user_refill
                )
            if chat.tokens < cost:
                retry_after = max(
                    retry_after, (cost - chat.tokens) / self._chat_refill
                )
            if retry_after > 0:
                self._rejected += 1
                self._touch(user_key)
                self._touch(chat_key)
                self._trim()
                raise RobotRateLimited(retry_after)

            user.tokens -= cost
            chat.tokens -= cost
            self._allowed += 1
            self._touch(user_key)
            self._touch(chat_key)
            self._trim()

    def snapshot(self) -> dict[str, int]:
        return {
            "entries": len(self._buckets),
            "max_entries": self._max_entries,
            "allowed": self._allowed,
            "rejected": self._rejected,
            "evictions": self._evictions,
        }

    def _refilled(
        self,
        key: tuple[str, int],
        now: float,
        capacity: float,
        refill_per_second: float,
    ) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=capacity, updated_at=now)
            self._buckets[key] = bucket
            return bucket
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_per_second)
        bucket.updated_at = now
        return bucket

    def _touch(self, key: tuple[str, int]) -> None:
        if key in self._buckets:
            self._buckets.move_to_end(key)

    def _trim(self) -> None:
        while len(self._buckets) > self._max_entries:
            self._buckets.popitem(last=False)
            self._evictions += 1
