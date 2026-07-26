import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest, NetworkError

from modules.utils import safe_delete_command, safe_delete_messages, send_typing


class TelegramUtilityTestCase(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def update() -> SimpleNamespace:
        return SimpleNamespace(
            effective_message=SimpleNamespace(
                chat_id=100,
                message_id=200,
            )
        )

    async def test_typing_and_enabled_command_deletion_use_exact_message(self) -> None:
        context = SimpleNamespace(
            bot=SimpleNamespace(
                send_chat_action=AsyncMock(),
                delete_message=AsyncMock(),
            )
        )
        update = self.update()

        await send_typing(update, context)
        await safe_delete_command(update, context, enabled=True)

        context.bot.send_chat_action.assert_awaited_once()
        context.bot.delete_message.assert_awaited_once_with(
            chat_id=100,
            message_id=200,
        )

    async def test_optional_telegram_actions_never_break_successful_lookup(self) -> None:
        context = SimpleNamespace(
            bot=SimpleNamespace(
                send_chat_action=AsyncMock(
                    side_effect=NetworkError("offline")
                ),
                delete_message=AsyncMock(
                    side_effect=BadRequest("not permitted")
                ),
            )
        )
        update = self.update()

        await send_typing(update, context)
        await safe_delete_command(update, context, enabled=True)
        await safe_delete_command(update, context, enabled=False)

        context.bot.send_chat_action.assert_awaited_once()
        context.bot.delete_message.assert_awaited_once()

    async def test_workflow_cleanup_deduplicates_and_ignores_invalid_ids(self) -> None:
        context = SimpleNamespace(
            bot=SimpleNamespace(delete_message=AsyncMock())
        )

        await safe_delete_messages(
            context,
            chat_id=100,
            message_ids=(300, 200, 300, 0, -1, True),
        )

        self.assertEqual(
            [call.kwargs for call in context.bot.delete_message.await_args_list],
            [
                {"chat_id": 100, "message_id": 300},
                {"chat_id": 100, "message_id": 200},
            ],
        )

    async def test_workflow_cleanup_attempts_every_message_after_failures(self) -> None:
        context = SimpleNamespace(
            bot=SimpleNamespace(
                delete_message=AsyncMock(
                    side_effect=(
                        BadRequest("not permitted"),
                        NetworkError("offline"),
                        None,
                    )
                )
            )
        )

        await safe_delete_messages(
            context,
            chat_id=100,
            message_ids=(300, 200, 100),
        )

        self.assertEqual(context.bot.delete_message.await_count, 3)


if __name__ == "__main__":
    unittest.main()
