"""Telegram helpers that fail safely without affecting successful commands."""

from __future__ import annotations

import logging

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
    try:
        await context.bot.delete_message(
            chat_id=update.effective_message.chat_id,
            message_id=update.effective_message.message_id,
        )
    except (BadRequest, Forbidden):
        LOGGER.info("Command deletion was not permitted")
    except TelegramError:
        LOGGER.warning("Command deletion failed", exc_info=True)
