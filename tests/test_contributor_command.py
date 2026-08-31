import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import KeyboardButton, ReplyKeyboardMarkup

import bot
from config import Settings
from modules.commands import LIMITER_SLOT, SETTINGS_SLOT
from modules.contributions import ContributionStore
from modules.contributor_command import (
    CONTRIBUTION_STORE_SLOT,
    PUSH_KEYBOARD_BUTTON_TEXT,
    contributor_command,
)
from modules.errors import RobotRateLimited
from modules.rate_limit import InboundRateLimiter


def _settings(*, mini_app: bool) -> Settings:
    values = {"TELEGRAM_API_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"}
    if mini_app:
        values["MINI_APP_ENABLED"] = "true"
        values["MINI_APP_PUBLIC_URL"] = "https://bot.example.com/getbible/app"
    with patch.dict(os.environ, values, clear=True):
        return Settings.from_env(load_environment_file=False)


def _limiter() -> InboundRateLimiter:
    return InboundRateLimiter(
        user_capacity=10,
        user_refill_per_second=1.0,
        chat_capacity=10,
        chat_refill_per_second=1.0,
        max_entries=100,
    )


def _context(
    store: ContributionStore | None,
    *,
    limiter: InboundRateLimiter | None = None,
    settings: Settings | None = None,
) -> SimpleNamespace:
    data = {
        LIMITER_SLOT: limiter or _limiter(),
        SETTINGS_SLOT: settings
        or SimpleNamespace(
            audit_log_mode="metadata",
            audit_identity_mode="disabled",
            telegram_api_token="123456:abcdefghijklmnopqrstuvwxyzABCDEFGHIJ",
        ),
    }
    if store is not None:
        data[CONTRIBUTION_STORE_SLOT] = store
    return SimpleNamespace(
        application=SimpleNamespace(bot_data=data),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


def _update(*, private: bool = True) -> tuple[SimpleNamespace, AsyncMock]:
    reply = AsyncMock()
    update = SimpleNamespace(
        effective_message=SimpleNamespace(reply_text=reply),
        effective_chat=SimpleNamespace(
            id=42,
            type="private" if private else "group",
        ),
        effective_user=SimpleNamespace(
            id=42,
            first_name="Grace",
            last_name="Reader",
            username="grace_reader",
            language_code="en",
        ),
    )
    return update, reply


class ContributorCommandTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = ContributionStore(path=None)

    async def asyncTearDown(self) -> None:
        self.store.close()

    async def test_private_application_is_idempotent_and_approved_status_is_professional(
        self,
    ) -> None:
        update, reply = _update()
        await contributor_command(update, _context(self.store))
        self.assertIn("now in review", reply.await_args.args[0])

        await contributor_command(update, _context(self.store))
        self.assertIn("already in review", reply.await_args.args[0])
        self.store.decide_application(42, "approved", actor="admin")

        await contributor_command(update, _context(self.store))
        text = reply.await_args.args[0]
        self.assertIn("You are enrolled", text)
        self.assertIn("core catalogue", text)

    async def test_approved_reply_attaches_persistent_push_keyboard(self) -> None:
        self.store.submit_application(42, first_name="Grace")
        self.store.decide_application(42, "approved", actor="admin")
        update, reply = _update()

        await contributor_command(
            update,
            _context(self.store, settings=_settings(mini_app=True)),
        )

        text = reply.await_args.args[0]
        self.assertIn("You are enrolled", text)
        self.assertIn(PUSH_KEYBOARD_BUTTON_TEXT, text)
        markup = reply.await_args.kwargs["reply_markup"]
        self.assertIsInstance(markup, ReplyKeyboardMarkup)
        self.assertTrue(markup.is_persistent)
        self.assertTrue(markup.resize_keyboard)
        rows = markup.keyboard
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 1)
        button = rows[0][0]
        self.assertIsInstance(button, KeyboardButton)
        self.assertEqual(button.text, PUSH_KEYBOARD_BUTTON_TEXT)
        self.assertIsNotNone(button.web_app)
        self.assertTrue(button.web_app.url.endswith("?context=push"))

    async def test_approved_reply_has_no_keyboard_without_a_mini_app(self) -> None:
        self.store.submit_application(42, first_name="Grace")
        self.store.decide_application(42, "approved", actor="admin")

        disabled = _context(self.store, settings=_settings(mini_app=False))
        missing = _context(self.store)
        del missing.application.bot_data[SETTINGS_SLOT]
        for label, context in (("disabled", disabled), ("missing", missing)):
            with self.subTest(settings=label):
                update, reply = _update()

                await contributor_command(update, context)

                self.assertIn("You are enrolled", reply.await_args.args[0])
                self.assertNotIn("reply_markup", reply.await_args.kwargs)
                self.assertEqual(reply.await_args.args[1:], ())

    async def test_group_invocation_is_silently_ignored(self) -> None:
        update, reply = _update(private=False)
        await contributor_command(update, _context(self.store))
        reply.assert_not_awaited()
        self.assertIsNone(self.store.application_for(42))

    async def test_rate_limited_application_does_not_reach_storage(self) -> None:
        limiter = _limiter()
        limiter.acquire = AsyncMock(
            side_effect=RobotRateLimited(3),
        )
        limiter.should_notify_rejection = AsyncMock(return_value=True)
        update, reply = _update()
        context = _context(self.store, limiter=limiter)

        await contributor_command(update, context)

        limiter.acquire.assert_awaited_once_with(user_id=42, chat_id=42)
        self.assertIsNone(self.store.application_for(42))
        reply.assert_not_awaited()
        self.assertIn(
            "Too many requests",
            context.bot.send_message.await_args.kwargs["text"],
        )

    async def test_missing_limiter_fails_closed_without_writing(self) -> None:
        update, reply = _update()
        context = _context(self.store)
        del context.application.bot_data[LIMITER_SLOT]

        await contributor_command(update, context)

        self.assertIsNone(self.store.application_for(42))
        self.assertIn("temporarily unavailable", reply.await_args.args[0])

    async def test_unconfigured_instance_fails_safely_in_private(self) -> None:
        update, reply = _update()
        await contributor_command(update, _context(None))
        self.assertIn("temporarily unavailable", reply.await_args.args[0])

    async def test_moderated_application_states_have_clear_private_statuses(self) -> None:
        expected = {
            "deferred": "still under review",
            "rejected": "not approved",
            "revoked": "not currently active",
        }
        for state, phrase in expected.items():
            with self.subTest(state=state):
                store = ContributionStore(path=None)
                try:
                    store.submit_application(42, first_name="Grace")
                    store.decide_application(42, state, actor="admin")
                    update, reply = _update()
                    await contributor_command(update, _context(store))
                    self.assertIn(phrase, reply.await_args.args[0])
                finally:
                    store.close()

    async def test_missing_telegram_context_is_ignored_and_store_failure_is_safe(
        self,
    ) -> None:
        await contributor_command(
            SimpleNamespace(
                effective_message=None,
                effective_chat=None,
                effective_user=None,
            ),
            _context(self.store),
        )
        self.assertIsNone(self.store.application_for(42))

        update, reply = _update()
        self.store.close()
        await contributor_command(update, _context(self.store))
        self.assertIn("could not be recorded", reply.await_args.args[0])

    async def test_notification_worker_delivers_and_receipts_plain_text(self) -> None:
        self.store.submit_application(42, first_name="Grace")
        self.store.decide_application(42, "approved", actor="admin")
        application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        task = asyncio.create_task(
            bot._deliver_contribution_notifications(application, self.store)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        application.bot.send_message.assert_awaited_once()
        sent = application.bot.send_message.await_args
        self.assertEqual(sent.kwargs["chat_id"], 42)
        self.assertNotIn("parse_mode", sent.kwargs)
        self.assertEqual(self.store.list_notifications()[0].state, "sent")

    async def test_notification_failure_is_retried_without_reverting_approval(self) -> None:
        self.store.submit_application(42, first_name="Grace")
        self.store.decide_application(42, "approved", actor="admin")
        application = SimpleNamespace(
            bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("offline")))
        )
        task = asyncio.create_task(
            bot._deliver_contribution_notifications(application, self.store)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(self.store.application_for(42).state, "approved")
        notice = self.store.list_notifications()[0]
        self.assertEqual(notice.state, "failed")
        self.assertEqual(notice.attempts, 1)


if __name__ == "__main__":
    unittest.main()
