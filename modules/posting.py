"""Authoritative Scripture posting shared by bot commands and the Mini App."""

from __future__ import annotations

import logging

from telegram import Bot
from telegram.constants import ParseMode

from config import Settings
from modules.audit import audit_event
from modules.renderer import render_scripture
from modules.service import ScriptureQuery, ScriptureService

LOGGER = logging.getLogger(__name__)


async def post_scripture(
    *,
    bot: Bot,
    chat_id: int,
    query: ScriptureQuery,
    settings: Settings,
    service: ScriptureService,
    source: str,
    message_thread_id: int | None = None,
) -> tuple[int, ...]:
    """Resolve, render, and post one validated query; return Telegram message IDs."""
    result = await service.select(query)
    verse_count = sum(
        len(chapter["verses"])
        for chapter in result.values()
        if isinstance(chapter, dict) and isinstance(chapter.get("verses"), list)
    )
    chunks = render_scripture(
        result,
        settings.web_base_url,
        max_chunks=settings.max_output_chunks,
    )
    message_ids: list[int] = []
    for chunk in chunks:
        message = await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            message_thread_id=message_thread_id,
        )
        message_id = getattr(message, "message_id", None)
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            message_ids.append(message_id)
    audit_event(
        LOGGER,
        settings,
        "scripture_posted",
        metadata={
            "source": source,
            "translation": query.translation,
            "reference_group_count": query.references.count(";") + 1,
            "verse_count": verse_count,
            "message_count": len(chunks),
        },
        content={"reference": query.references},
    )
    return tuple(message_ids)
