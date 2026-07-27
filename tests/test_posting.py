import unittest
from types import SimpleNamespace
from typing import cast

from getbible import RequestLimitError
from telegram import Bot
from telegram.error import TelegramError

from config import Settings
from modules.posting import post_scripture_queries
from modules.service import ScriptureQuery, ScriptureService


class _Bot:
    def __init__(self, *, fail_on_send: int | None = None) -> None:
        self.fail_on_send = fail_on_send
        self.sent: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, int]] = []

    async def send_message(self, *, chat_id: int, text: str, **_: object) -> object:
        call = len(self.sent) + 1
        if self.fail_on_send == call:
            raise TelegramError("simulated Telegram failure")
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=100 + call)

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


class _Service:
    async def select(self, query: ScriptureQuery) -> dict[str, object]:
        number = int(query.references.rsplit(":", 1)[1])
        return {
            f"{query.translation}_43_3": {
                "book_name": "John",
                "abbreviation": query.translation,
                "chapter": 3,
                "verses": [
                    {
                        "verse": number,
                        "text": f"Authoritative verse {number}.",
                    }
                ],
            }
        }


def _settings(max_output_chunks: int = 8) -> Settings:
    return cast(
        Settings,
        SimpleNamespace(
            web_base_url="https://getbible.net",
            max_output_chunks=max_output_chunks,
            audit_log_mode="metadata",
        ),
    )


class ScripturePostingTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_global_cap_fails_before_the_first_telegram_send(self) -> None:
        bot = _Bot()

        with self.assertRaisesRegex(RequestLimitError, "1-message posting limit"):
            await post_scripture_queries(
                bot=cast(Bot, bot),
                chat_id=-100,
                queries=(
                    ScriptureQuery("John 3:16", "kjv"),
                    ScriptureQuery("John 3:17", "asv"),
                ),
                settings=_settings(1),
                service=cast(ScriptureService, _Service()),
                source="mini_app",
                max_messages=1,
            )

        self.assertEqual(bot.sent, [])
        self.assertEqual(bot.deleted, [])

    async def test_incomplete_send_rolls_back_every_known_message(self) -> None:
        bot = _Bot(fail_on_send=2)

        with self.assertRaises(TelegramError):
            await post_scripture_queries(
                bot=cast(Bot, bot),
                chat_id=-100,
                queries=(
                    ScriptureQuery("John 3:16", "kjv"),
                    ScriptureQuery("John 3:17", "asv"),
                ),
                settings=_settings(2),
                service=cast(ScriptureService, _Service()),
                source="mini_app",
                max_messages=2,
                message_thread_id=9,
            )

        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.deleted, [(-100, 101)])

    async def test_successful_batch_never_exceeds_the_global_cap(self) -> None:
        bot = _Bot()

        message_ids = await post_scripture_queries(
            bot=cast(Bot, bot),
            chat_id=42,
            queries=(
                ScriptureQuery("John 3:16", "kjv"),
                ScriptureQuery("John 3:17", "asv"),
            ),
            settings=_settings(2),
            service=cast(ScriptureService, _Service()),
            source="mini_app",
            max_messages=2,
        )

        self.assertEqual(message_ids, (101, 102))
        self.assertEqual(len(bot.sent), 2)
        self.assertEqual(bot.deleted, [])


if __name__ == "__main__":
    unittest.main()
