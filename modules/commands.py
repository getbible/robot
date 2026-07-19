"""Telegram command handlers with bounded work and user-safe failures."""

from __future__ import annotations

import logging
import secrets
from typing import cast

from getbible import (
    ReferenceValidationError,
    RequestLimitError,
    TranslationNotFoundError,
)
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import Settings
from .errors import (
    CircuitOpen,
    RobotBusy,
    RobotInputError,
    RobotRateLimited,
    ScriptureUnavailable,
)
from .rate_limit import InboundRateLimiter
from .renderer import render_scripture
from .service import ScriptureService
from .utils import safe_delete_command, send_typing

LOGGER = logging.getLogger(__name__)

SETTINGS_KEY = "settings"
SERVICE_KEY = "scripture_service"
LIMITER_KEY = "inbound_rate_limiter"


def _components(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[Settings, ScriptureService, InboundRateLimiter]:
    data = context.application.bot_data
    return (
        cast(Settings, data[SETTINGS_KEY]),
        cast(ScriptureService, data[SERVICE_KEY]),
        cast(InboundRateLimiter, data[LIMITER_KEY]),
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, _ = _components(context)
    if update.effective_chat is None:
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=settings.welcome_message,
    )
    await safe_delete_command(
        update,
        context,
        enabled=settings.delete_command_messages,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, _ = _components(context)
    if update.effective_chat is None:
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=settings.help_message,
        disable_web_page_preview=True,
    )
    await safe_delete_command(
        update,
        context,
        enabled=settings.delete_command_messages,
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _, _ = _components(context)
    if update.effective_chat is None:
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Search the Scriptures at {settings.web_base_url}/search",
        disable_web_page_preview=True,
    )
    await safe_delete_command(
        update,
        context,
        enabled=settings.delete_command_messages,
    )


async def bible_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_message is None:
        return
    settings, service, limiter = _components(context)
    user_id = (
        update.effective_user.id
        if update.effective_user is not None
        else update.effective_chat.id
    )
    request_id = secrets.token_hex(4)

    try:
        await limiter.acquire(
            user_id=user_id,
            chat_id=update.effective_chat.id,
        )
        await send_typing(update, context)
        query = await service.resolve_query(context.args)
        result = await service.select(query)
        chunks = render_scripture(result, settings.web_base_url)
    except Exception as error:
        message, expected = _safe_error_message(error, request_id)
        if expected:
            LOGGER.info(
                "Request %s rejected safely (%s)",
                request_id,
                type(error).__name__,
            )
        else:
            LOGGER.error(
                "Request %s failed unexpectedly (%s)",
                request_id,
                type(error).__name__,
            )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=message,
        )
    else:
        for chunk in chunks:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    finally:
        await safe_delete_command(
            update,
            context,
            enabled=settings.delete_command_messages,
        )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Unknown command. Use /help to see the available commands.",
    )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Last-resort boundary; raw exceptions are never reflected to Telegram."""
    incident_id = secrets.token_hex(4)
    error = context.error
    LOGGER.error(
        "Unhandled update failure %s (%s)",
        incident_id,
        type(error).__name__ if error is not None else "unknown",
    )
    if isinstance(error, TelegramError):
        return
    effective_chat = getattr(update, "effective_chat", None)
    if effective_chat is None:
        return
    try:
        await context.bot.send_message(
            chat_id=effective_chat.id,
            text=(
                "Something went wrong safely. Please try again later. "
                f"Reference: {incident_id}"
            ),
        )
    except TelegramError:
        LOGGER.warning("Unable to report incident %s to Telegram", incident_id)


def _safe_error_message(error: Exception, request_id: str) -> tuple[str, bool]:
    if isinstance(error, RobotRateLimited):
        return (
            f"Too many requests. Please try again in about {error.retry_after} seconds.",
            True,
        )
    if isinstance(error, RequestLimitError):
        return (
            "That request exceeds the bot's safety limits. "
            "Please request fewer verses or references.",
            True,
        )
    if isinstance(error, (ReferenceValidationError, RobotInputError)):
        return (
            "I could not understand that Scripture reference. "
            "Try /bible John 3:16.",
            True,
        )
    if isinstance(error, TranslationNotFoundError):
        return (
            "That Bible translation is not available. "
            "Try the default translation or another abbreviation.",
            True,
        )
    if isinstance(error, RobotBusy):
        return (
            "The bot is handling its safe workload limit. Please try again shortly.",
            True,
        )
    if isinstance(error, (CircuitOpen, ScriptureUnavailable)):
        return (
            "The Scripture service is temporarily unavailable. "
            f"Please try again later. Reference: {request_id}",
            True,
        )
    return (
        "Something went wrong safely. "
        f"Please try again later. Reference: {request_id}",
        False,
    )
