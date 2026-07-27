"""Authoritative Scripture posting shared by bot commands and the Mini App."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from getbible import RequestLimitError
from telegram import Bot
from telegram.constants import ParseMode

from config import Settings
from modules.audit import audit_event
from modules.errors import ScriptureUnavailable
from modules.renderer import render_scripture
from modules.service import ScriptureQuery, ScriptureService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PreparedScripture:
    query: ScriptureQuery
    chunks: tuple[str, ...]
    verse_count: int


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
    return await post_scripture_queries(
        bot=bot,
        chat_id=chat_id,
        queries=(query,),
        settings=settings,
        service=service,
        source=source,
        message_thread_id=message_thread_id,
        max_messages=settings.max_output_chunks,
    )


async def post_scripture_queries(
    *,
    bot: Bot,
    chat_id: int,
    queries: Sequence[ScriptureQuery],
    settings: Settings,
    service: ScriptureService,
    source: str,
    max_messages: int,
    message_thread_id: int | None = None,
) -> tuple[int, ...]:
    """Pre-render and globally bound a complete post before its first send."""
    if (
        not isinstance(max_messages, int)
        or isinstance(max_messages, bool)
        or not 1 <= max_messages <= 32
    ):
        raise ValueError("max_messages must be an integer between 1 and 32.")
    if not queries:
        raise ScriptureUnavailable("The Scripture post contains no queries.")

    prepared: list[_PreparedScripture] = []
    total_messages = 0
    for query in queries:
        result = await service.select(query)
        verse_count = sum(
            len(chapter["verses"])
            for chapter in result.values()
            if isinstance(chapter, dict) and isinstance(chapter.get("verses"), list)
        )
        chunks = tuple(
            render_scripture(
                result,
                settings.web_base_url,
                max_chunks=max_messages,
            )
        )
        total_messages += len(chunks)
        if total_messages > max_messages:
            raise RequestLimitError(
                f"The selected Scripture would exceed the {max_messages}-message posting limit."
            )
        prepared.append(
            _PreparedScripture(
                query=query,
                chunks=chunks,
                verse_count=verse_count,
            )
        )

    message_ids: list[int] = []
    try:
        for item in prepared:
            for chunk in item.chunks:
                message = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    message_thread_id=message_thread_id,
                )
                message_id = getattr(message, "message_id", None)
                if (
                    isinstance(message_id, bool)
                    or not isinstance(message_id, int)
                    or message_id <= 0
                ):
                    raise ScriptureUnavailable("Telegram returned an invalid message ID.")
                message_ids.append(message_id)

        for item in prepared:
            audit_event(
                LOGGER,
                settings,
                "scripture_posted",
                metadata={
                    "source": source,
                    "translation": item.query.translation,
                    "reference_group_count": item.query.references.count(";") + 1,
                    "verse_count": item.verse_count,
                    "message_count": len(item.chunks),
                },
                content={"reference": item.query.references},
            )
    except BaseException:
        await _rollback_messages(bot, chat_id, message_ids)
        raise

    return tuple(message_ids)


async def _rollback_messages(
    bot: Bot,
    chat_id: int,
    message_ids: Sequence[int],
) -> None:
    """Best-effort rollback of known messages from an incomplete final post."""
    for message_id in reversed(message_ids):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            LOGGER.warning(
                "Could not roll back incomplete Scripture message %s",
                message_id,
            )
