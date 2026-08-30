"""Hidden private Telegram command for contributor enrolment."""

from __future__ import annotations

import logging
import sqlite3

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from .commands import LIMITER_SLOT, allow_command
from .contributions import ContributionError, ContributionStore
from .rate_limit import InboundRateLimiter

LOGGER = logging.getLogger(__name__)
CONTRIBUTION_STORE_SLOT = "contribution_store"


async def contributor_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Submit or report one ID-bound application without exposing a menu item."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    if chat.type != ChatType.PRIVATE:
        # Keep the private, operator-directed workflow undiscoverable in groups.
        return
    data = context.application.bot_data
    limiter = data.get(LIMITER_SLOT)
    if not isinstance(limiter, InboundRateLimiter):
        LOGGER.error("Contributor command has no inbound rate limiter")
        await message.reply_text(
            "Contributor applications are temporarily unavailable on this instance. "
            "Please try again later."
        )
        return
    if not await allow_command(update, context, limiter):
        return

    value = data.get(CONTRIBUTION_STORE_SLOT)
    if not isinstance(value, ContributionStore):
        await message.reply_text(
            "Contributor applications are temporarily unavailable on this instance. "
            "Please try again later."
        )
        return
    store = value
    try:
        application, created = store.submit_application(
            user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
            language_code=user.language_code,
        )
    except (ContributionError, OSError, sqlite3.Error, RuntimeError):
        LOGGER.warning("A contributor application could not be recorded", exc_info=True)
        await message.reply_text(
            "Your contributor application could not be recorded right now. "
            "Please try again later."
        )
        return

    if application.state == "approved":
        text = (
            "You are enrolled as a GetBible topic contributor. Your topic and "
            "verse-tag changes are being reviewed, and approved changes become "
            "part of the core catalogue for the rest of the world using the app."
        )
    elif application.state == "pending":
        text = (
            "Thank you. Your GetBible contributor application is now in review. "
            "We will notify you here when a decision is available."
            if created
            else (
                "Your GetBible contributor application is already in review. "
                "We will notify you here when a decision is available."
            )
        )
    elif application.state == "deferred":
        text = (
            "Your GetBible contributor application is still under review. "
            "We will notify you here when a final decision is available."
        )
    elif application.state == "rejected":
        text = (
            "Your GetBible contributor application was not approved at this time. "
            "Your personal topics and verse markings remain private on your devices."
        )
    else:
        text = (
            "Your GetBible contributor enrolment is not currently active. Your "
            "personal topics and verse markings remain available on your devices."
        )
    await message.reply_text(text)
