"""Bounded inbound token-bucket rate limiting."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from .errors import RobotRateLimited


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


@dataclass(slots=True)
class _AbuseState:
    window_started_at: float
    rejections: int = 0
    blocked_until: float = 0.0


_IdentityKey: TypeAlias = tuple[str, int | str]


class InboundRateLimiter:
    """Enforce bounded user/chat/IP budgets and temporary abuse blocks."""

    def __init__(
        self,
        *,
        user_capacity: int,
        user_refill_per_second: float,
        chat_capacity: int,
        chat_refill_per_second: float,
        max_entries: int,
        notification_cooldown: float = 10.0,
        client_capacity: int = 60,
        client_refill_per_second: float = 10.0,
        abuse_rejection_threshold: int = 6,
        abuse_window_seconds: float = 60.0,
        abuse_block_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            user_capacity < 1
            or user_refill_per_second <= 0
            or chat_capacity < 1
            or chat_refill_per_second <= 0
            or client_capacity < 1
            or client_refill_per_second <= 0
            or max_entries < 1
            or notification_cooldown <= 0
            or abuse_rejection_threshold < 2
            or abuse_window_seconds <= 0
            or abuse_block_seconds <= 0
        ):
            raise ValueError("Invalid inbound rate-limiter configuration.")
        self._user_capacity = float(user_capacity)
        self._user_refill = float(user_refill_per_second)
        self._chat_capacity = float(chat_capacity)
        self._chat_refill = float(chat_refill_per_second)
        self._client_capacity = float(client_capacity)
        self._client_refill = float(client_refill_per_second)
        self._max_entries = max_entries
        self._notification_cooldown = notification_cooldown
        self._abuse_rejection_threshold = abuse_rejection_threshold
        self._abuse_window_seconds = abuse_window_seconds
        self._abuse_block_seconds = abuse_block_seconds
        self._clock = clock
        self._buckets: OrderedDict[_IdentityKey, _Bucket] = OrderedDict()
        self._notifications: OrderedDict[tuple[int, int], float] = OrderedDict()
        self._abuse: OrderedDict[_IdentityKey, _AbuseState] = OrderedDict()
        self._lock = asyncio.Lock()
        self._allowed = 0
        self._rejected = 0
        self._evictions = 0
        self._abuse_blocks = 0
        self._blocked_rejections = 0

    async def acquire(
        self,
        *,
        user_id: int,
        chat_id: int,
        cost: float = 1.0,
        client_key: str | None = None,
    ) -> None:
        if cost <= 0:
            raise ValueError("cost must be positive.")
        normalized_client = self._normalized_client_key(client_key)
        now = self._clock()
        async with self._lock:
            user_key = ("user", user_id)
            chat_key = ("chat", chat_id)
            client_identity = (
                ("client", normalized_client)
                if normalized_client is not None
                else None
            )
            blocked = self._active_blocks(
                now,
                user_key,
                client_identity,
            )
            if blocked:
                retry_after = max(
                    self._abuse[key].blocked_until - now
                    for key in blocked
                )
                self._rejected += 1
                self._blocked_rejections += 1
                self._trim()
                raise RobotRateLimited(
                    retry_after,
                    blocked=True,
                    scopes=tuple(key[0] for key in blocked),
                    user_id=user_id,
                    chat_id=chat_id,
                    client_key=normalized_client,
                )

            user = self._refilled(
                user_key, now, self._user_capacity, self._user_refill
            )
            chat = self._refilled(
                chat_key, now, self._chat_capacity, self._chat_refill
            )
            client = (
                self._refilled(
                    client_identity,
                    now,
                    self._client_capacity,
                    self._client_refill,
                )
                if client_identity is not None
                else None
            )

            retry_after = 0.0
            exhausted: list[_IdentityKey] = []
            if user.tokens < cost:
                exhausted.append(user_key)
                retry_after = max(
                    retry_after, (cost - user.tokens) / self._user_refill
                )
            if chat.tokens < cost:
                exhausted.append(chat_key)
                retry_after = max(
                    retry_after, (cost - chat.tokens) / self._chat_refill
                )
            if client is not None and client.tokens < cost:
                if client_identity is None:  # pragma: no cover - narrowed above
                    raise AssertionError("client identity disappeared")
                exhausted.append(client_identity)
                retry_after = max(
                    retry_after,
                    (cost - client.tokens) / self._client_refill,
                )
            if retry_after > 0:
                self._rejected += 1
                self._touch(user_key)
                self._touch(chat_key)
                if client_identity is not None:
                    self._touch(client_identity)
                abuse_keys = tuple(
                    key for key in exhausted if key[0] in {"user", "client"}
                )
                newly_blocked, violation_count = self._record_rejection(
                    abuse_keys,
                    now,
                )
                active = self._active_blocks(now, *abuse_keys)
                if active:
                    retry_after = max(
                        retry_after,
                        max(self._abuse[key].blocked_until - now for key in active),
                    )
                self._trim()
                raise RobotRateLimited(
                    retry_after,
                    blocked=bool(active),
                    new_block=newly_blocked,
                    violation_count=violation_count,
                    scopes=tuple(key[0] for key in exhausted),
                    user_id=user_id,
                    chat_id=chat_id,
                    client_key=normalized_client,
                )

            user.tokens -= cost
            chat.tokens -= cost
            if client is not None:
                client.tokens -= cost
            self._allowed += 1
            self._touch(user_key)
            self._touch(chat_key)
            if client_identity is not None:
                self._touch(client_identity)
            self._trim()

    async def should_notify_rejection(self, *, user_id: int, chat_id: int) -> bool:
        """Allow at most one Telegram rejection message per identity/cooldown."""
        now = self._clock()
        key = (user_id, chat_id)
        async with self._lock:
            previous = self._notifications.get(key)
            self._notifications[key] = now
            self._notifications.move_to_end(key)
            while len(self._notifications) > self._max_entries:
                self._notifications.popitem(last=False)
            return previous is None or now - previous >= self._notification_cooldown

    def snapshot(self) -> dict[str, int]:
        return {
            "entries": len(self._buckets),
            "max_entries": self._max_entries,
            "notification_entries": len(self._notifications),
            "abuse_entries": len(self._abuse),
            "active_blocks": sum(
                1
                for state in self._abuse.values()
                if state.blocked_until > self._clock()
            ),
            "allowed": self._allowed,
            "rejected": self._rejected,
            "evictions": self._evictions,
            "abuse_blocks": self._abuse_blocks,
            "blocked_rejections": self._blocked_rejections,
        }

    def _refilled(
        self,
        key: _IdentityKey,
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

    def _touch(self, key: _IdentityKey) -> None:
        if key in self._buckets:
            self._buckets.move_to_end(key)

    def _trim(self) -> None:
        while len(self._buckets) > self._max_entries:
            self._buckets.popitem(last=False)
            self._evictions += 1
        while len(self._abuse) > self._max_entries:
            self._abuse.popitem(last=False)
            self._evictions += 1

    def _active_blocks(
        self,
        now: float,
        *keys: _IdentityKey | None,
    ) -> tuple[_IdentityKey, ...]:
        active: list[_IdentityKey] = []
        for key in keys:
            if key is None:
                continue
            state = self._abuse.get(key)
            if state is None:
                continue
            if state.blocked_until > now:
                self._abuse.move_to_end(key)
                active.append(key)
            elif state.blocked_until:
                state.blocked_until = 0.0
                state.rejections = 0
                state.window_started_at = now
        return tuple(active)

    def _record_rejection(
        self,
        keys: tuple[_IdentityKey, ...],
        now: float,
    ) -> tuple[bool, int]:
        newly_blocked = False
        highest_count = 0
        for key in keys:
            state = self._abuse.get(key)
            if state is None:
                state = _AbuseState(window_started_at=now)
                self._abuse[key] = state
            elif now - state.window_started_at >= self._abuse_window_seconds:
                state.window_started_at = now
                state.rejections = 0
            state.rejections += 1
            highest_count = max(highest_count, state.rejections)
            if (
                state.rejections >= self._abuse_rejection_threshold
                and state.blocked_until <= now
            ):
                state.blocked_until = now + self._abuse_block_seconds
                self._abuse_blocks += 1
                newly_blocked = True
            self._abuse.move_to_end(key)
        return newly_blocked, highest_count

    @staticmethod
    def _normalized_client_key(value: str | None) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized or normalized == "unknown" or len(normalized) > 64:
            return None
        return normalized
