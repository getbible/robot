"""At-most-once cleanup for Telegram rows created by Mini App launches."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import replace
from functools import partial
from typing import Any

from .miniapp_sessions import MiniAppLaunch
from .service import ScriptureQuery

CleanupCallback = Callable[[MiniAppLaunch], Awaitable[None]]
PostCallback = Callable[
    [MiniAppLaunch, tuple[ScriptureQuery, ...]],
    Awaitable[Sequence[int] | None],
]
SleepCallback = Callable[[float], Awaitable[None]]


class MiniAppLaunchCleanup:
    """Own cleanup for every Telegram row involved in a Mini App launch.

    The source command and launcher stay together until authenticated readiness,
    post, expiry, capacity eviction, or shutdown claims them for cleanup. This
    avoids deleting the source before the user can open the launcher and keeps
    the source identifier available to the same lifecycle path that removes the
    launcher.

    All identifiers are moved into an immutable snapshot before any await.
    Concurrent lifecycle paths therefore observe an already-cleared launch and
    cannot issue a second Telegram deletion request.
    """

    def __init__(
        self,
        cleanup_launch: CleanupCallback | None,
        *,
        ttl_seconds: float,
        max_pending: int,
        sleep: SleepCallback = asyncio.sleep,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive.")
        if max_pending < 1:
            raise ValueError("max_pending must be positive.")
        self._cleanup_launch = cleanup_launch
        self._ttl_seconds = ttl_seconds
        self._max_pending = max_pending
        self._sleep = sleep
        self._timers: OrderedDict[str, tuple[MiniAppLaunch, asyncio.Task[None]]] = (
            OrderedDict()
        )
        self._workers: set[asyncio.Task[None]] = set()
        self._closed = False

    def remember_prompt(self, launch: MiniAppLaunch) -> None:
        """Schedule expiry for the source command and its recorded launcher."""
        if (
            self._closed
            or self._cleanup_launch is None
            or (
                launch.prompt_message_id is None
                and launch.prompt_ephemeral_message_id is None
                and launch.source_ephemeral_message_id is None
            )
        ):
            return

        task = asyncio.create_task(self._expire(launch))
        self._timers[launch.token] = (launch, task)
        task.add_done_callback(partial(self._timer_done, launch.token))
        while len(self._timers) > self._max_pending:
            _, (old_launch, old_timer) = self._timers.popitem(last=False)
            old_timer.cancel()
            self._start_worker(self.cleanup_now(old_launch, cancel_timer=False))

    async def cleanup_now(
        self,
        launch: MiniAppLaunch,
        *,
        cancel_timer: bool = True,
    ) -> None:
        """Attempt deletion once and make every concurrent/later path a no-op."""
        if cancel_timer:
            timer_entry = self._timers.pop(launch.token, None)
            if timer_entry is not None:
                timer = timer_entry[1]
                if timer is not asyncio.current_task():
                    timer.cancel()

        await self._execute(self._take_launch(launch))

    async def post(
        self,
        launch: MiniAppLaunch,
        queries: tuple[ScriptureQuery, ...],
        callback: PostCallback,
    ) -> Sequence[int] | None:
        """Prevent the legacy post callback from retrying lifecycle cleanup."""
        timer_entry = self._timers.pop(launch.token, None)
        if timer_entry is not None:
            timer_entry[1].cancel()
        snapshot = self._take_launch(launch)
        try:
            return await callback(launch, queries)
        finally:
            await self._execute(snapshot)

    async def close(self) -> None:
        """Clean every still-pending prompt once during graceful shutdown."""
        if self._closed:
            return
        self._closed = True
        pending = [launch for launch, _ in self._timers.values()]
        timers = [task for _, task in self._timers.values()]
        self._timers.clear()
        for task in timers:
            task.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        if pending:
            await asyncio.gather(
                *(
                    self.cleanup_now(launch, cancel_timer=False)
                    for launch in pending
                ),
                return_exceptions=True,
            )
        workers = tuple(self._workers)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    def snapshot(self) -> dict[str, int]:
        """Expose only aggregate cleanup state."""
        return {
            "cleanup_pending": len(self._timers),
            "cleanup_workers": len(self._workers),
        }

    async def _expire(self, launch: MiniAppLaunch) -> None:
        try:
            await self._sleep(self._ttl_seconds)
        except asyncio.CancelledError:
            return
        self._timers.pop(launch.token, None)
        await self.cleanup_now(launch, cancel_timer=False)

    async def _execute(self, launch: MiniAppLaunch | None) -> None:
        if launch is None or self._cleanup_launch is None:
            return
        try:
            await self._cleanup_launch(launch)
        except Exception:
            # Cleanup is deliberately invisible and never changes the user's
            # successful open, copy, close, or Scripture-posting outcome.
            return

    def _timer_done(self, token: str, task: asyncio.Future[None]) -> None:
        current = self._timers.get(token)
        if current is not None and current[1] is task:
            self._timers.pop(token, None)

    def _start_worker(self, operation: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(operation)
        self._workers.add(task)
        task.add_done_callback(self._worker_done)

    def _worker_done(self, future: asyncio.Future[None]) -> None:
        if isinstance(future, asyncio.Task):
            self._workers.discard(future)

    @staticmethod
    def _take_launch(launch: MiniAppLaunch) -> MiniAppLaunch | None:
        if (
            launch.prompt_message_id is None
            and launch.prompt_ephemeral_message_id is None
            and launch.source_ephemeral_message_id is None
        ):
            return None
        snapshot = replace(launch)
        launch.prompt_message_id = None
        launch.prompt_ephemeral_message_id = None
        launch.source_ephemeral_message_id = None
        launch.source_ephemeral_receiver_user_id = None
        return snapshot
