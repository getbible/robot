import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, TelegramError

from modules.ephemeral import (
    EPHEMERAL_DELETE_RETRY_DELAY_SECONDS,
    delete_ephemeral_text,
    edit_ephemeral_text,
    send_ephemeral_text,
)


class EphemeralTransportTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_send_uses_receiver_and_returns_ephemeral_identifier(self) -> None:
        bot = SimpleNamespace(
            do_api_request=AsyncMock(
                return_value={
                    "message_id": 0,
                    "ephemeral_message_id": 701,
                }
            )
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Post", callback_data="post")]]
        )

        result = await send_ephemeral_text(
            bot,
            chat_id=-100,
            receiver_user_id=200,
            text="Private result",
            reply_markup=keyboard,
            parse_mode="HTML",
            callback_query_id="callback-1",
            reply_to_ephemeral_message_id=699,
            message_thread_id=12,
        )

        self.assertEqual(result, 701)
        call = bot.do_api_request.await_args
        self.assertEqual(call.args[0], "sendMessage")
        payload = call.kwargs["api_kwargs"]
        self.assertEqual(payload["chat_id"], -100)
        self.assertEqual(payload["receiver_user_id"], 200)
        self.assertEqual(payload["callback_query_id"], "callback-1")
        self.assertEqual(
            payload["reply_parameters"],
            {"ephemeral_message_id": 699},
        )
        self.assertEqual(payload["message_thread_id"], 12)
        self.assertIn("inline_keyboard", payload["reply_markup"])

    async def test_send_rejects_missing_ephemeral_identifier(self) -> None:
        bot = SimpleNamespace(
            do_api_request=AsyncMock(return_value={"message_id": 0})
        )

        with self.assertRaises(TelegramError):
            await send_ephemeral_text(
                bot,
                chat_id=-100,
                receiver_user_id=200,
                text="Private result",
            )

    async def test_edit_and_delete_use_ephemeral_endpoints(self) -> None:
        bot = SimpleNamespace(do_api_request=AsyncMock(return_value=True))

        await edit_ephemeral_text(
            bot,
            chat_id=-100,
            receiver_user_id=200,
            ephemeral_message_id=701,
            text="Page two",
        )
        await delete_ephemeral_text(
            bot,
            chat_id=-100,
            receiver_user_id=200,
            ephemeral_message_id=701,
        )

        self.assertEqual(
            [call.args[0] for call in bot.do_api_request.await_args_list],
            ["editEphemeralMessageText", "deleteEphemeralMessage"],
        )
        edit_payload = bot.do_api_request.await_args_list[0].kwargs["api_kwargs"]
        self.assertEqual(
            {
                "chat_id": edit_payload["chat_id"],
                "receiver_user_id": edit_payload["receiver_user_id"],
                "ephemeral_message_id": edit_payload["ephemeral_message_id"],
                "text": edit_payload["text"],
            },
            {
                "chat_id": -100,
                "receiver_user_id": 200,
                "ephemeral_message_id": 701,
                "text": "Page two",
            },
        )
        self.assertEqual(
            bot.do_api_request.await_args_list[1].kwargs["api_kwargs"],
            {
                "chat_id": -100,
                "receiver_user_id": 200,
                "ephemeral_message_id": 701,
            },
        )

    async def test_delete_retries_one_unconfirmed_transient_failure(self) -> None:
        bot = SimpleNamespace(
            do_api_request=AsyncMock(
                side_effect=[NetworkError("temporary failure"), True]
            )
        )

        with patch(
            "modules.ephemeral.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep:
            await delete_ephemeral_text(
                bot,
                chat_id=-100,
                receiver_user_id=200,
                ephemeral_message_id=701,
            )

        self.assertEqual(bot.do_api_request.await_count, 2)
        sleep.assert_awaited_once_with(EPHEMERAL_DELETE_RETRY_DELAY_SECONDS)

    async def test_delete_does_not_retry_a_definitive_failure(self) -> None:
        bot = SimpleNamespace(
            do_api_request=AsyncMock(
                side_effect=BadRequest("message is unavailable")
            )
        )

        with self.assertRaises(BadRequest):
            await delete_ephemeral_text(
                bot,
                chat_id=-100,
                receiver_user_id=200,
                ephemeral_message_id=701,
            )

        self.assertEqual(bot.do_api_request.await_count, 1)


if __name__ == "__main__":
    unittest.main()
