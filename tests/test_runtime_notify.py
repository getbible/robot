import asyncio
import logging
import os
import socket
import unittest
from unittest.mock import AsyncMock, Mock, patch

from modules.runtime_notify import RuntimeNotifier


class RuntimeNotifierConfigurationTestCase(unittest.TestCase):
    def notifier(self, **environment: str) -> RuntimeNotifier:
        with patch.dict(os.environ, environment, clear=True):
            return RuntimeNotifier()

    def test_watchdog_is_disabled_without_a_valid_interval(self) -> None:
        self.assertIsNone(self.notifier()._watchdog_interval)
        self.assertIsNone(self.notifier(WATCHDOG_USEC="invalid")._watchdog_interval)
        self.assertIsNone(self.notifier(WATCHDOG_USEC="0")._watchdog_interval)
        self.assertIsNone(
            self.notifier(
                WATCHDOG_USEC="2000000",
                WATCHDOG_PID=str(os.getpid() + 1),
            )._watchdog_interval
        )
        self.assertIsNone(
            self.notifier(
                WATCHDOG_USEC="2000000",
                WATCHDOG_PID="invalid",
            )._watchdog_interval
        )

    def test_watchdog_uses_half_the_manager_interval_with_a_safe_floor(self) -> None:
        self.assertEqual(
            self.notifier(WATCHDOG_USEC="500000")._watchdog_interval,
            1.0,
        )
        self.assertEqual(
            self.notifier(
                WATCHDOG_USEC="6000000",
                WATCHDOG_PID=str(os.getpid()),
            )._watchdog_interval,
            3.0,
        )

    def test_send_is_a_noop_without_a_manager_socket(self) -> None:
        notifier = self.notifier()
        with patch("modules.runtime_notify.socket.socket") as socket_factory:
            notifier._send("READY=1")
        socket_factory.assert_not_called()

    def test_send_delivers_encoded_payload_to_the_manager_socket(self) -> None:
        notifier = self.notifier(NOTIFY_SOCKET="/run/systemd/notify")
        transport = Mock()
        transport.__enter__ = Mock(return_value=transport)
        transport.__exit__ = Mock(return_value=False)
        with patch(
            "modules.runtime_notify.socket.socket",
            return_value=transport,
        ) as socket_factory:
            notifier._send("READY=1\nSTATUS=ready")

        socket_factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_DGRAM)
        transport.connect.assert_called_once_with("/run/systemd/notify")
        transport.sendall.assert_called_once_with(b"READY=1\nSTATUS=ready")

    def test_abstract_socket_and_transport_errors_fail_safely(self) -> None:
        notifier = self.notifier(NOTIFY_SOCKET="@getbible-test")
        transport = Mock()
        transport.__enter__ = Mock(return_value=transport)
        transport.__exit__ = Mock(return_value=False)
        transport.connect.side_effect = OSError("unavailable")
        with (
            patch(
                "modules.runtime_notify.socket.socket",
                return_value=transport,
            ),
            self.assertLogs("modules.runtime_notify", logging.WARNING) as captured,
        ):
            notifier._send("READY=1")

        transport.connect.assert_called_once_with("\0getbible-test")
        self.assertIn("failed safely (OSError)", captured.output[0])


class RuntimeNotifierLifecycleTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_ready_starts_one_watchdog_and_stopping_cancels_it(self) -> None:
        with patch.dict(
            os.environ,
            {"WATCHDOG_USEC": "2000000"},
            clear=True,
        ):
            notifier = RuntimeNotifier()
        watchdog = AsyncMock(side_effect=asyncio.Event().wait)
        notifier._watchdog = watchdog
        notifier._send = Mock()

        notifier.ready()
        first_task = notifier._watchdog_task
        notifier.ready()
        await asyncio.sleep(0)

        self.assertIsNotNone(first_task)
        self.assertIs(notifier._watchdog_task, first_task)
        watchdog.assert_awaited_once()

        await notifier.stopping()

        self.assertIsNone(notifier._watchdog_task)
        self.assertTrue(first_task.cancelled())
        self.assertEqual(
            notifier._send.call_args_list,
            [
                unittest.mock.call("READY=1\nSTATUS=GetBible Robot is ready"),
                unittest.mock.call("READY=1\nSTATUS=GetBible Robot is ready"),
                unittest.mock.call(
                    "STOPPING=1\nSTATUS=GetBible Robot is stopping"
                ),
            ],
        )

    async def test_stopping_without_a_watchdog_still_notifies_manager(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            notifier = RuntimeNotifier()
        notifier._send = Mock()

        await notifier.stopping()

        notifier._send.assert_called_once_with(
            "STOPPING=1\nSTATUS=GetBible Robot is stopping"
        )

    async def test_watchdog_sends_after_each_interval(self) -> None:
        with patch.dict(
            os.environ,
            {"WATCHDOG_USEC": "2000000"},
            clear=True,
        ):
            notifier = RuntimeNotifier()
        notifier._send = Mock()
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])

        with (
            patch("modules.runtime_notify.asyncio.sleep", sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await notifier._watchdog()

        notifier._send.assert_called_once_with("WATCHDOG=1")


if __name__ == "__main__":
    unittest.main()
