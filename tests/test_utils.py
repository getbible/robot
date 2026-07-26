import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest, NetworkError

from modules.utils import safe_delete_command, send_typing


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


if __name__ == "__main__":
    unittest.main()
