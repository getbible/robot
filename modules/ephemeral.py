"""Forward-compatible Telegram Bot API 10.2 ephemeral message helpers."""

from __future__ import annotations

import asyncio
from typing import Any

from telegram import Bot, ForceReply, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError

TELEGRAM_TEXT_LIMIT = 4096
EPHEMERAL_DELETE_MAX_ATTEMPTS = 2
EPHEMERAL_DELETE_RETRY_DELAY_SECONDS = 0.2


EphemeralReplyMarkup = InlineKeyboardMarkup | ForceReply


def telegram_text_length(value: str) -> int:
    """Measure conservatively in the UTF-16 code units used by Telegram."""
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as error:
        raise ValueError("Ephemeral message contains invalid Unicode.") from error


def _markup_payload(
    reply_markup: EphemeralReplyMarkup | None,
) -> dict[str, object] | None:
    if reply_markup is None:
        return None
    return reply_markup.to_dict()


async def send_ephemeral_text(
    bot: Bot,
    *,
    chat_id: int,
    receiver_user_id: int,
    text: str,
    reply_markup: EphemeralReplyMarkup | None = None,
    parse_mode: str | None = None,
    callback_query_id: str | None = None,
    reply_to_ephemeral_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> int:
    """Send one per-user group message and return its ephemeral identifier."""
    if not 1 <= telegram_text_length(text) <= TELEGRAM_TEXT_LIMIT:
        raise ValueError("Ephemeral message text is outside Telegram's limit.")

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "receiver_user_id": receiver_user_id,
        "text": text,
        "parse_mode": parse_mode,
        "link_preview_options": {"is_disabled": True},
        "reply_markup": _markup_payload(reply_markup),
        "callback_query_id": callback_query_id,
    }
    if reply_to_ephemeral_message_id is not None:
        payload["reply_parameters"] = {
            "ephemeral_message_id": reply_to_ephemeral_message_id,
        }
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id

    result = await bot.do_api_request("sendMessage", api_kwargs=payload)
    if not isinstance(result, dict):
        raise TelegramError("Telegram returned an invalid ephemeral message.")
    ephemeral_message_id = result.get("ephemeral_message_id")
    if (
        not isinstance(ephemeral_message_id, int)
        or isinstance(ephemeral_message_id, bool)
        or ephemeral_message_id <= 0
    ):
        raise TelegramError("Telegram omitted the ephemeral message identifier.")
    return ephemeral_message_id


async def edit_ephemeral_text(
    bot: Bot,
    *,
    chat_id: int,
    receiver_user_id: int,
    ephemeral_message_id: int,
    text: str,
    reply_markup: EphemeralReplyMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    """Edit an existing per-user group message."""
    if not 1 <= telegram_text_length(text) <= TELEGRAM_TEXT_LIMIT:
        raise ValueError("Ephemeral message text is outside Telegram's limit.")
    result = await bot.do_api_request(
        "editEphemeralMessageText",
        api_kwargs={
            "chat_id": chat_id,
            "receiver_user_id": receiver_user_id,
            "ephemeral_message_id": ephemeral_message_id,
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": {"is_disabled": True},
            "reply_markup": _markup_payload(reply_markup),
        },
    )
    if result is not True:
        raise TelegramError("Telegram did not confirm the ephemeral message edit.")


async def delete_ephemeral_text(
    bot: Bot,
    *,
    chat_id: int,
    receiver_user_id: int,
    ephemeral_message_id: int,
) -> None:
    """Delete an existing per-user group message with one bounded transient retry."""
    payload = {
        "chat_id": chat_id,
        "receiver_user_id": receiver_user_id,
        "ephemeral_message_id": ephemeral_message_id,
    }
    last_error: TelegramError | None = None
    for attempt in range(EPHEMERAL_DELETE_MAX_ATTEMPTS):
        try:
            result = await bot.do_api_request(
                "deleteEphemeralMessage",
                api_kwargs=payload,
            )
            if result is True:
                return
            last_error = TelegramError(
                "Telegram did not confirm ephemeral message deletion."
            )
        except (BadRequest, Forbidden):
            # Permission and unavailable-message failures are definitive.
            raise
        except NetworkError as error:
            last_error = error
        except TelegramError:
            raise

        if attempt + 1 < EPHEMERAL_DELETE_MAX_ATTEMPTS:
            await asyncio.sleep(EPHEMERAL_DELETE_RETRY_DELAY_SECONDS)

    if last_error is None:  # pragma: no cover - loop bounds are constant and positive
        last_error = TelegramError(
            "Telegram did not confirm ephemeral message deletion."
        )
    raise last_error
