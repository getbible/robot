"""Telegram helpers that fail safely without affecting successful commands."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

LOGGER = logging.getLogger(__name__)


async def send_typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.TYPING,
        )
    except TelegramError:
        LOGGER.debug("Unable to send typing action", exc_info=True)


async def safe_delete_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    enabled: bool,
) -> None:
    """Delete only when explicitly configured; permission failures are non-fatal."""
    if not enabled or update.effective_message is None:
        return
    await safe_delete_messages(
        context,
        chat_id=update.effective_message.chat_id,
        message_ids=(update.effective_message.message_id,),
    )


async def safe_delete_messages(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    message_ids: Iterable[int],
) -> None:
    """Best-effort deletion that can never invalidate a successful response."""
    unique_ids = tuple(
        dict.fromkeys(
            message_id
            for message_id in message_ids
            if isinstance(message_id, int)
            and not isinstance(message_id, bool)
            and message_id > 0
        )
    )
    permission_failures = 0
    telegram_failures = 0
    for message_id in unique_ids:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )
        except (BadRequest, Forbidden):
            permission_failures += 1
        except TelegramError:
            telegram_failures += 1

    if permission_failures:
        LOGGER.info(
            "Workflow cleanup could not delete %d message(s): "
            "permission denied or message unavailable",
            permission_failures,
        )
    if telegram_failures:
        LOGGER.warning(
            "Workflow cleanup failed for %d message(s)",
            telegram_failures,
        )
