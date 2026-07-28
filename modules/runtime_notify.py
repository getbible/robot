"""Minimal systemd readiness and event-loop watchdog notification."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import suppress

LOGGER = logging.getLogger(__name__)


class RuntimeNotifier:
    """Notify systemd when available; remain a no-op in Docker and local runs."""

    def __init__(self) -> None:
        self._socket_path = os.environ.get("NOTIFY_SOCKET", "")
        self._watchdog_interval = self._resolve_watchdog_interval()
        self._watchdog_task: asyncio.Task[None] | None = None

    def ready(self) -> None:
        self._send("READY=1\nSTATUS=GetBible Robot is ready")
        if self._watchdog_interval is not None and self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(
                self._watchdog(),
                name="systemd-watchdog",
            )

    async def stopping(self) -> None:
        task = self._watchdog_task
        self._watchdog_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._send("STOPPING=1\nSTATUS=GetBible Robot is stopping")

    async def _watchdog(self) -> None:
        if self._watchdog_interval is None:
            return
        while True:
            await asyncio.sleep(self._watchdog_interval)
            self._send("WATCHDOG=1")

    def _resolve_watchdog_interval(self) -> float | None:
        raw = os.environ.get("WATCHDOG_USEC", "")
        if not raw:
            return None
        try:
            watchdog_seconds = int(raw) / 1_000_000
        except ValueError:
            LOGGER.warning("Ignoring invalid WATCHDOG_USEC")
            return None
        raw_pid = os.environ.get("WATCHDOG_PID", "")
        if raw_pid:
            try:
                if int(raw_pid) != os.getpid():
                    return None
            except ValueError:
                return None
        if watchdog_seconds <= 0:
            return None
        return max(1.0, watchdog_seconds / 2)

    def _send(self, payload: str) -> None:
        if not self._socket_path:
            return
        address = self._socket_path
        if address.startswith("@"):
            address = f"\0{address[1:]}"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                notifier.connect(address)
                notifier.sendall(payload.encode("utf-8"))
        except OSError as error:
            LOGGER.warning(
                "Runtime manager notification failed safely (%s)",
                type(error).__name__,
            )
