"""Telegram commands and bounded interactive Scripture workflows."""

from __future__ import annotations

import html
import logging
import re
import secrets
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import replace
from typing import TypeVar, cast

from getbible import (
    ReferenceValidationError,
    RequestLimitError,
    SearchValidationError,
    TranslationNotFoundError,
)
from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import Settings
from modules.audit import audit_event
from modules.catalog import BookOption, ChapterOption, TranslationOption
from modules.errors import (
    CircuitOpen,
    RobotBusy,
    RobotInputError,
    RobotRateLimited,
    ScriptureUnavailable,
)
from modules.interactions import (
    InteractionSession,
    InteractionStore,
    SearchOptions,
)
from modules.rate_limit import InboundRateLimiter
from modules.renderer import render_scripture
from modules.service import ScriptureQuery, ScriptureService
from modules.utils import safe_delete_command, send_typing

LOGGER = logging.getLogger(__name__)

SETTINGS_SLOT = "settings"
SERVICE_SLOT = "scripture_service"
LIMITER_SLOT = "inbound_rate_limiter"
INTERACTIONS_SLOT = "interaction_store"

TRANSLATION_PAGE_SIZE = 6
BOOK_PAGE_SIZE = 10
CHAPTER_PAGE_SIZE = 20
VERSE_PAGE_SIZE = 25
SEARCH_PAGE_SIZE = 5
MAX_EXCLUSIONS = 32
BUTTON_LABEL_LIMIT = 60
_CALLBACK_RE = re.compile(r"gb:([A-Za-z0-9_-]{8,16}):([a-z]{1,8}):([A-Za-z0-9_-]{0,32})\Z")
_T = TypeVar("_T")

CALLBACK_ACTIONS = frozenset(
    {
        "bg",
        "bp",
        "bs",
        "bt",
        "cancel",
        "cb",
        "chback",
        "cp",
        "cs",
        "rpost",
        "rreset",
        "sb",
        "sbclear",
        "sbdone",
        "sbg",
        "sbp",
        "sbt",
        "sc",
        "sdash",
        "sdi",
        "sm",
        "snew",
        "so",
        "spost",
        "spr",
        "spv",
        "sq",
        "sreset",
        "srp",
        "srt",
        "ss",
        "st",
        "sw",
        "sx",
        "tc",
        "tp",
        "tr",
        "vback",
        "ve",
        "vep",
        "vs",
        "vsp",
    }
)


def _components(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[Settings, ScriptureService, InboundRateLimiter, InteractionStore]:
    data = context.application.bot_data
    return (
        cast(Settings, data[SETTINGS_SLOT]),
        cast(ScriptureService, data[SERVICE_SLOT]),
        cast(InboundRateLimiter, data[LIMITER_SLOT]),
        cast(InteractionStore, data[INTERACTIONS_SLOT]),
    )


def _identity(update: Update) -> tuple[int, int] | None:
    if update.effective_chat is None:
        return None
    user_id = (
        update.effective_user.id
        if update.effective_user is not None
        else update.effective_chat.id
    )
    return update.effective_chat.id, user_id


async def _allow_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    limiter: InboundRateLimiter,
) -> bool:
    """Apply budgets and suppress repeated Telegram rejection notifications."""
    identity = _identity(update)
    if identity is None:
        return False
    chat_id, user_id = identity
    try:
        await limiter.acquire(user_id=user_id, chat_id=chat_id)
    except RobotRateLimited as error:
        if await limiter.should_notify_rejection(user_id=user_id, chat_id=chat_id):
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "Too many requests. Please try again in about "
                    f"{error.retry_after} seconds."
                ),
            )
        return False
    return True


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    settings, _, limiter, _ = _components(context)
    try:
        if not await _allow_command(update, context, limiter):
            return
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=settings.welcome_message,
        )
    finally:
        await safe_delete_command(
            update,
            context,
            enabled=settings.delete_command_messages,
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    settings, _, limiter, _ = _components(context)
    try:
        if not await _allow_command(update, context, limiter):
            return
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=settings.help_message,
            disable_web_page_preview=True,
        )
    finally:
        await safe_delete_command(
            update,
            context,
            enabled=settings.delete_command_messages,
        )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a default search or open the complete filter dashboard."""
    if update.effective_chat is None:
        return
    settings, service, limiter, interactions = _components(context)
    identity = _identity(update)
    if identity is None:
        return
    chat_id, user_id = identity
    request_id = secrets.token_hex(4)
    try:
        if not await _allow_command(update, context, limiter):
            return
        session = interactions.create(
            chat_id=chat_id,
            user_id=user_id,
            kind="search",
            stage="search_dashboard",
            translation=settings.default_translation,
        )
        query = " ".join(context.args or ()).strip()
        if query:
            await send_typing(update, context)
            await _run_search(session, query, service, settings)
            message = await context.bot.send_message(
                chat_id=chat_id,
                text=_search_results_text(session, 0),
                reply_markup=_search_results_keyboard(session, 0),
                disable_web_page_preview=True,
            )
        else:
            message = await context.bot.send_message(
                chat_id=chat_id,
                text=_search_dashboard_text(session),
                reply_markup=_search_dashboard_keyboard(session),
                disable_web_page_preview=True,
            )
        session.message_id = message.message_id
    except Exception as error:
        if "session" in locals():
            interactions.remove(session.token)
        await _report_command_error(error, request_id, chat_id, context)
    finally:
        await safe_delete_command(
            update,
            context,
            enabled=settings.delete_command_messages,
        )


async def bible_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post an explicit reference immediately or open the guided picker."""
    if update.effective_chat is None or update.effective_message is None:
        return
    settings, service, limiter, interactions = _components(context)
    identity = _identity(update)
    if identity is None:
        return
    chat_id, user_id = identity
    request_id = secrets.token_hex(4)
    session: InteractionSession | None = None

    try:
        if not await _allow_command(update, context, limiter):
            return
        if context.args:
            await send_typing(update, context)
            query = await service.resolve_query(context.args)
            await _post_scripture(
                chat_id,
                query,
                settings,
                service,
                context,
                source="bible_direct",
            )
            return

        await send_typing(update, context)
        translations = await service.translations()
        session = interactions.create(
            chat_id=chat_id,
            user_id=user_id,
            kind="reference",
            stage="reference_translation",
            translation=settings.default_translation,
        )
        session.translations = translations
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=_translation_text(session, 0),
            reply_markup=_translation_keyboard(session, 0),
            disable_web_page_preview=True,
        )
        session.message_id = message.message_id
    except Exception as error:
        if session is not None:
            interactions.remove(session.token)
        await _report_command_error(error, request_id, chat_id, context)
    finally:
        await safe_delete_command(
            update,
            context,
            enabled=settings.delete_command_messages,
        )


async def interaction_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Route one authenticated opaque callback to its bounded session."""
    callback = update.callback_query
    identity = _identity(update)
    if callback is None or not isinstance(callback.data, str) or identity is None:
        return
    match = _CALLBACK_RE.fullmatch(callback.data)
    if match is None:
        await callback.answer("This control is invalid.", show_alert=True)
        return

    _, service, limiter, interactions = _components(context)
    chat_id, user_id = identity
    token, action, value = match.groups()
    if action not in CALLBACK_ACTIONS:
        await callback.answer("This control is invalid.", show_alert=True)
        return
    session = interactions.get(token, chat_id=chat_id, user_id=user_id)
    if session is None:
        await callback.answer(
            "This selection expired. Run /bible or /search again.",
            show_alert=True,
        )
        return

    if not await _allow_command(update, context, limiter):
        return

    if action == "srt":
        if not value.isdigit() or int(value) >= len(session.search_results):
            await callback.answer("That result is no longer available.", show_alert=True)
            return
        index = int(value)
        if index not in session.selected and not _search_selection_fits(
            session,
            session.selected | {index},
            _components(context)[0],
        ):
            await callback.answer(
                "That would exceed the safe posting limit.",
                show_alert=True,
            )
            return
        if index in session.selected:
            session.selected.remove(index)
        else:
            session.selected.add(index)
        await callback.answer()
        page = _page_containing(index, SEARCH_PAGE_SIZE)
        await _edit(
            session,
            context,
            _search_results_text(session, page),
            _search_results_keyboard(session, page),
        )
        return

    if action == "spost" and not session.selected:
        await callback.answer("Select at least one verse first.", show_alert=True)
        return

    await callback.answer()
    request_id = secrets.token_hex(4)
    try:
        await _dispatch_callback(
            session,
            action,
            value,
            update,
            context,
            service,
            interactions,
        )
    except Exception as error:
        await _report_command_error(error, request_id, chat_id, context)


async def interaction_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Accept only replies to a bot-created prompt owned by this user/session."""
    if (
        update.effective_message is None
        or update.effective_message.reply_to_message is None
        or update.effective_message.text is None
    ):
        return
    identity = _identity(update)
    if identity is None:
        return
    settings, service, limiter, interactions = _components(context)
    chat_id, user_id = identity
    session = interactions.find_prompt(
        chat_id=chat_id,
        user_id=user_id,
        prompt_message_id=update.effective_message.reply_to_message.message_id,
    )
    if session is None:
        return

    text = update.effective_message.text.strip()
    request_id = secrets.token_hex(4)
    try:
        if not await _allow_command(update, context, limiter):
            return
        if session.stage == "search_exclude":
            exclusions = _parse_exclusions(text, settings.max_input_length)
            session.search_options = replace(
                session.search_options,
                exclude=exclusions,
            )
            session.stage = "search_dashboard"
            session.prompt_message_id = None
            await _edit(
                session,
                context,
                _search_dashboard_text(session),
                _search_dashboard_keyboard(session),
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text="Search exclusions updated.",
            )
            return

        if session.stage != "search_query":
            return
        await send_typing(update, context)
        await _run_search(session, text, service, settings)
        session.prompt_message_id = None
        await _edit(
            session,
            context,
            _search_results_text(session, 0),
            _search_results_keyboard(session, 0),
        )
    except Exception as error:
        await _report_command_error(error, request_id, chat_id, context)


async def _dispatch_callback(
    session: InteractionSession,
    action: str,
    value: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: ScriptureService,
    interactions: InteractionStore,
) -> None:
    settings = _components(context)[0]

    if action == "tp":
        page = _bounded_page(value, len(session.translations), TRANSLATION_PAGE_SIZE)
        await _edit(
            session,
            context,
            _translation_text(session, page),
            _translation_keyboard(session, page),
        )
        return
    if action in {"tr", "tc"}:
        if action == "tr":
            _select_translation(session, value)
        if session.kind == "reference":
            await _open_reference_books(session, service, context)
        else:
            session.search_options = replace(
                session.search_options,
                translation=session.translation,
                books=(),
            )
            session.books = ()
            session.stage = "search_dashboard"
            await _edit(
                session,
                context,
                _search_dashboard_text(session),
                _search_dashboard_keyboard(session),
            )
        return

    if action == "bt":
        _require_kind(session, "reference")
        session.stage = "reference_translation"
        await _edit(
            session,
            context,
            _translation_text(session, 0),
            _translation_keyboard(session, 0),
        )
        return
    if action == "bg":
        _require_kind(session, "reference")
        session.testament = _testament(value)
        await _edit(
            session,
            context,
            _books_text(session, 0),
            _books_keyboard(session, 0),
        )
        return
    if action == "bp":
        _require_kind(session, "reference")
        books = _filtered_books(session)
        page = _bounded_page(value, len(books), BOOK_PAGE_SIZE)
        await _edit(
            session,
            context,
            _books_text(session, page),
            _books_keyboard(session, page),
        )
        return
    if action == "bs":
        _require_kind(session, "reference")
        book = _book_by_number(session.books, value)
        session.chapters = await service.chapters(session.translation, book)
        session.book = book
        session.chapter = None
        session.start_verse = None
        session.end_verse = None
        session.stage = "reference_chapter"
        await _edit(
            session,
            context,
            _chapters_text(session, 0),
            _chapters_keyboard(session, 0),
        )
        return
    if action == "cb":
        _require_kind(session, "reference")
        session.stage = "reference_books"
        await _edit(
            session,
            context,
            _books_text(session, 0),
            _books_keyboard(session, 0),
        )
        return
    if action == "chback":
        _require_kind(session, "reference")
        session.stage = "reference_chapter"
        await _edit(
            session,
            context,
            _chapters_text(session, 0),
            _chapters_keyboard(session, 0),
        )
        return
    if action == "cp":
        _require_kind(session, "reference")
        page = _bounded_page(value, len(session.chapters), CHAPTER_PAGE_SIZE)
        await _edit(
            session,
            context,
            _chapters_text(session, page),
            _chapters_keyboard(session, page),
        )
        return
    if action == "cs":
        _require_kind(session, "reference")
        chapter = _chapter_by_number(session.chapters, value)
        session.chapter = chapter
        session.start_verse = None
        session.end_verse = None
        session.stage = "reference_start"
        await _edit(
            session,
            context,
            _verse_start_text(session, 0),
            _verse_start_keyboard(session, 0),
        )
        return
    if action == "vsp":
        _require_kind(session, "reference")
        verses = _session_verses(session)
        page = _bounded_page(value, len(verses), VERSE_PAGE_SIZE)
        await _edit(
            session,
            context,
            _verse_start_text(session, page),
            _verse_start_keyboard(session, page),
        )
        return
    if action == "vs":
        _require_kind(session, "reference")
        verse = _verse_by_number(_session_verses(session), value)
        session.start_verse = verse
        session.end_verse = None
        session.stage = "reference_end"
        await _edit(
            session,
            context,
            _verse_end_text(session, 0),
            _verse_end_keyboard(session, 0),
        )
        return
    if action == "vep":
        _require_kind(session, "reference")
        verses = _ending_verses(session)
        page = _bounded_page(value, len(verses), VERSE_PAGE_SIZE)
        await _edit(
            session,
            context,
            _verse_end_text(session, page),
            _verse_end_keyboard(session, page),
        )
        return
    if action == "ve":
        _require_kind(session, "reference")
        session.end_verse = _verse_by_number(_ending_verses(session), value)
        session.stage = "reference_review"
        await _edit(
            session,
            context,
            _reference_review_text(session),
            _reference_review_keyboard(session),
        )
        return
    if action == "vback":
        _require_kind(session, "reference")
        session.stage = "reference_start"
        await _edit(
            session,
            context,
            _verse_start_text(session, 0),
            _verse_start_keyboard(session, 0),
        )
        return
    if action == "rpost":
        _require_kind(session, "reference")
        reference = _reference_selection(session)
        query = ScriptureQuery(reference, session.translation)
        await _edit(
            session,
            context,
            f"Posting {reference} ({session.translation})…",
            None,
        )
        await _post_scripture(
            session.chat_id,
            query,
            settings,
            service,
            context,
            source="bible_guided",
        )
        interactions.remove(session.token)
        return
    if action == "rreset":
        _require_kind(session, "reference")
        session.stage = "reference_translation"
        session.book = None
        session.chapter = None
        session.start_verse = None
        session.end_verse = None
        await _edit(
            session,
            context,
            _translation_text(session, 0),
            _translation_keyboard(session, 0),
        )
        return

    if action == "sdash":
        _require_kind(session, "search")
        session.stage = "search_dashboard"
        await _edit(
            session,
            context,
            _search_dashboard_text(session),
            _search_dashboard_keyboard(session),
        )
        return
    if action == "st":
        _require_kind(session, "search")
        if not session.translations:
            session.translations = await service.translations()
        session.stage = "search_translation"
        await _edit(
            session,
            context,
            _translation_text(session, 0),
            _translation_keyboard(session, 0),
        )
        return
    if action == "sw":
        _cycle_search_option(session, "words")
        await _show_search_dashboard(session, context)
        return
    if action == "sm":
        _cycle_search_option(session, "match")
        await _show_search_dashboard(session, context)
        return
    if action == "ss":
        _cycle_search_option(session, "scope")
        await _show_search_dashboard(session, context)
        return
    if action == "sc":
        session.search_options = replace(
            session.search_options,
            case_sensitive=not session.search_options.case_sensitive,
        )
        await _show_search_dashboard(session, context)
        return
    if action == "sdi":
        session.search_options = replace(
            session.search_options,
            diacritics=(
                "insensitive"
                if session.search_options.diacritics == "sensitive"
                else "sensitive"
            ),
        )
        await _show_search_dashboard(session, context)
        return
    if action == "so":
        session.search_options = replace(
            session.search_options,
            sort=(
                "relevance"
                if session.search_options.sort == "canonical"
                else "canonical"
            ),
        )
        await _show_search_dashboard(session, context)
        return
    if action == "sb":
        _require_kind(session, "search")
        if not session.books:
            session.books = await service.books(session.search_options.translation)
        session.translation = session.search_options.translation
        session.testament = _default_testament(session.books)
        session.stage = "search_books"
        await _edit(
            session,
            context,
            _search_books_text(session, 0),
            _search_books_keyboard(session, 0),
        )
        return
    if action == "sbg":
        _require_kind(session, "search")
        session.testament = _testament(value)
        await _edit(
            session,
            context,
            _search_books_text(session, 0),
            _search_books_keyboard(session, 0),
        )
        return
    if action == "sbp":
        _require_kind(session, "search")
        books = _filtered_books(session)
        page = _bounded_page(value, len(books), BOOK_PAGE_SIZE)
        await _edit(
            session,
            context,
            _search_books_text(session, page),
            _search_books_keyboard(session, page),
        )
        return
    if action == "sbt":
        _require_kind(session, "search")
        book = _book_by_number(session.books, value)
        selected = set(session.search_options.books)
        if book.number in selected:
            selected.remove(book.number)
        else:
            selected.add(book.number)
        session.search_options = replace(
            session.search_options,
            books=tuple(sorted(selected)),
        )
        page = _page_for_book(_filtered_books(session), book.number)
        await _edit(
            session,
            context,
            _search_books_text(session, page),
            _search_books_keyboard(session, page),
        )
        return
    if action == "sbclear":
        session.search_options = replace(session.search_options, books=())
        await _edit(
            session,
            context,
            _search_books_text(session, 0),
            _search_books_keyboard(session, 0),
        )
        return
    if action == "sbdone":
        await _show_search_dashboard(session, context)
        return
    if action == "sx":
        await _prompt_for_reply(
            session,
            update,
            context,
            stage="search_exclude",
            text=(
                "Reply with words to exclude, separated by spaces. "
                "Reply with a single dash (-) to clear exclusions."
            ),
        )
        return
    if action == "spr":
        session.stage = "search_proximity"
        await _edit(
            session,
            context,
            "Choose the maximum number of intervening words.\n"
            "Proximity uses Librarian's “all words” mode.",
            _search_proximity_keyboard(session),
        )
        return
    if action == "spv":
        proximity = None if value == "none" else _bounded_integer(value, 0, 100)
        session.search_options = replace(
            session.search_options,
            words="all",
            proximity=proximity,
        )
        await _show_search_dashboard(session, context)
        return
    if action in {"sq", "snew"}:
        await _prompt_for_reply(
            session,
            update,
            context,
            stage="search_query",
            text=(
                "Reply with the words or phrase to search for. "
                f"The current translation is {session.search_options.translation}."
            ),
        )
        return
    if action == "sreset":
        session.search_options = SearchOptions(
            translation=settings.default_translation,
        )
        session.translation = settings.default_translation
        session.books = ()
        session.search_results = ()
        session.selected.clear()
        await _show_search_dashboard(session, context)
        return
    if action == "srp":
        page = _bounded_page(value, len(session.search_results), SEARCH_PAGE_SIZE)
        await _edit(
            session,
            context,
            _search_results_text(session, page),
            _search_results_keyboard(session, page),
        )
        return
    if action == "spost":
        reference = _selected_search_reference(session)
        query = ScriptureQuery(reference, session.search_options.translation)
        await _edit(
            session,
            context,
            (
                f"Posting {len(session.selected)} selected verse"
                f"{'' if len(session.selected) == 1 else 's'}…"
            ),
            None,
        )
        await _post_scripture(
            session.chat_id,
            query,
            settings,
            service,
            context,
            source="search_selection",
        )
        interactions.remove(session.token)
        return
    if action == "cancel":
        interactions.remove(session.token)
        await _edit(session, context, "Selection cancelled.", None)
        return

    raise RobotInputError("Unknown interactive action.")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    settings, _, limiter, _ = _components(context)
    try:
        if not await _allow_command(update, context, limiter):
            return
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Unknown command. Use /help to see the available commands.",
        )
    finally:
        await safe_delete_command(
            update,
            context,
            enabled=settings.delete_command_messages,
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


async def _open_reference_books(
    session: InteractionSession,
    service: ScriptureService,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    session.books = await service.books(session.translation)
    session.testament = _default_testament(session.books)
    session.stage = "reference_books"
    await _edit(
        session,
        context,
        _books_text(session, 0),
        _books_keyboard(session, 0),
    )


async def _run_search(
    session: InteractionSession,
    query: str,
    service: ScriptureService,
    settings: Settings,
) -> None:
    if not query:
        raise RobotInputError("Search words are required.")
    page = await service.search(query, session.search_options)
    session.search_query = page.query
    session.search_total = page.total
    session.search_results = page.items
    session.selected.clear()
    session.stage = "search_results"
    options = session.search_options
    audit_event(
        LOGGER,
        settings,
        "search_completed",
        metadata={
            "translation": options.translation,
            "total_results": page.total,
            "returned_results": len(page.items),
            "word_mode": options.words,
            "match_mode": options.match,
            "scope": options.scope,
            "case_sensitive": options.case_sensitive,
            "diacritics": options.diacritics,
            "sort": options.sort,
            "book_filter_count": len(options.books),
            "exclusion_count": len(options.exclude),
            "proximity": options.proximity,
        },
        content={"query": page.query},
    )


async def _post_scripture(
    chat_id: int,
    query: ScriptureQuery,
    settings: Settings,
    service: ScriptureService,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    source: str,
) -> None:
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
    for chunk in chunks:
        await context.bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
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


async def _report_command_error(
    error: Exception,
    request_id: str,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message, expected = _safe_error_message(error, request_id)
    log = LOGGER.info if expected else LOGGER.error
    log(
        "Request %s %s (%s)",
        request_id,
        "rejected safely" if expected else "failed unexpectedly",
        type(error).__name__,
    )
    await context.bot.send_message(chat_id=chat_id, text=message)


async def _edit(
    session: InteractionSession,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    if session.message_id is None:
        raise RobotInputError("Interactive message is unavailable.")
    await context.bot.edit_message_text(
        chat_id=session.chat_id,
        message_id=session.message_id,
        text=text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _show_search_dashboard(
    session: InteractionSession,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    session.stage = "search_dashboard"
    await _edit(
        session,
        context,
        _search_dashboard_text(session),
        _search_dashboard_keyboard(session),
    )


async def _prompt_for_reply(
    session: InteractionSession,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    stage: str,
    text: str,
) -> None:
    user = update.effective_user
    target = user.mention_html() if user is not None else "Please"
    message = await context.bot.send_message(
        chat_id=session.chat_id,
        text=f"{target}, {html.escape(text)}",
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(
            selective=user is not None,
            input_field_placeholder="Type your reply",
        ),
    )
    session.stage = stage
    session.prompt_message_id = message.message_id


def _translation_text(session: InteractionSession, page: int) -> str:
    current = _translation_name(session.translations, session.translation)
    purpose = "Bible" if session.kind == "reference" else "search"
    return (
        f"Choose a translation for this {purpose}.\n\n"
        f"Selected: {current} ({session.translation})\n"
        "Continue to use the selected translation, or choose another below.\n"
        f"Page {page + 1} of {_page_count(len(session.translations), TRANSLATION_PAGE_SIZE)}"
    )


def _translation_keyboard(
    session: InteractionSession,
    page: int,
) -> InlineKeyboardMarkup:
    items, page = _page(session.translations, page, TRANSLATION_PAGE_SIZE)
    rows = [
        [
            InlineKeyboardButton(
                f"Skip — use {session.translation.upper()}",
                callback_data=_callback(session, "tc"),
            )
        ]
    ]
    for item in items:
        marker = "✓ " if item.code == session.translation else ""
        rows.append(
            [
                InlineKeyboardButton(
                    _button_label(f"{marker}{item.name} ({item.code})"),
                    callback_data=_callback(session, "tr", item.code),
                )
            ]
        )
    rows.append(
        _navigation_row(
            session,
            "tp",
            page,
            len(session.translations),
            TRANSLATION_PAGE_SIZE,
        )
    )
    rows.append([InlineKeyboardButton("Cancel", callback_data=_callback(session, "cancel"))])
    return InlineKeyboardMarkup([row for row in rows if row])


def _books_text(session: InteractionSession, page: int) -> str:
    books = _filtered_books(session)
    label = _testament_label(session.testament)
    return (
        f"{session.translation.upper()} — choose a book\n\n"
        f"Section: {label}\n"
        f"Page {page + 1} of {_page_count(len(books), BOOK_PAGE_SIZE)}"
    )


def _books_keyboard(session: InteractionSession, page: int) -> InlineKeyboardMarkup:
    books = _filtered_books(session)
    items, page = _page(books, page, BOOK_PAGE_SIZE)
    rows: list[list[InlineKeyboardButton]] = [_testament_row(session, "bg")]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    _button_label(book.name),
                    callback_data=_callback(session, "bs", str(book.number)),
                )
                for book in items[index : index + 2]
            ]
            for index in range(0, len(items), 2)
        ]
    )
    rows.append(_navigation_row(session, "bp", page, len(books), BOOK_PAGE_SIZE))
    rows.append(
        [
            InlineKeyboardButton("Back to translations", callback_data=_callback(session, "bt")),
            InlineKeyboardButton("Cancel", callback_data=_callback(session, "cancel")),
        ]
    )
    return InlineKeyboardMarkup([row for row in rows if row])


def _chapters_text(session: InteractionSession, page: int) -> str:
    book = _required_book(session)
    return (
        f"{book.name} — choose a chapter\n\n"
        f"Page {page + 1} of {_page_count(len(session.chapters), CHAPTER_PAGE_SIZE)}"
    )


def _chapters_keyboard(session: InteractionSession, page: int) -> InlineKeyboardMarkup:
    items, page = _page(session.chapters, page, CHAPTER_PAGE_SIZE)
    rows = [
        [
            InlineKeyboardButton(
                str(chapter.number),
                callback_data=_callback(session, "cs", str(chapter.number)),
            )
            for chapter in items[index : index + 5]
        ]
        for index in range(0, len(items), 5)
    ]
    rows.append(_navigation_row(session, "cp", page, len(session.chapters), CHAPTER_PAGE_SIZE))
    rows.append(
        [
            InlineKeyboardButton("Back to books", callback_data=_callback(session, "cb")),
            InlineKeyboardButton("Cancel", callback_data=_callback(session, "cancel")),
        ]
    )
    return InlineKeyboardMarkup([row for row in rows if row])


def _verse_start_text(session: InteractionSession, page: int) -> str:
    book = _required_book(session)
    chapter = _required_chapter(session)
    return (
        f"{book.name} {chapter.number} — choose the first verse\n\n"
        f"Page {page + 1} of {_page_count(len(chapter.verses), VERSE_PAGE_SIZE)}"
    )


def _verse_start_keyboard(session: InteractionSession, page: int) -> InlineKeyboardMarkup:
    verses = _session_verses(session)
    items, page = _page(verses, page, VERSE_PAGE_SIZE)
    rows = [
        [
            InlineKeyboardButton(
                str(verse),
                callback_data=_callback(session, "vs", str(verse)),
            )
            for verse in items[index : index + 5]
        ]
        for index in range(0, len(items), 5)
    ]
    rows.append(_navigation_row(session, "vsp", page, len(verses), VERSE_PAGE_SIZE))
    rows.append(
        [
            InlineKeyboardButton(
                "Back to chapters",
                callback_data=_callback(session, "chback"),
            ),
            InlineKeyboardButton("Cancel", callback_data=_callback(session, "cancel")),
        ]
    )
    return InlineKeyboardMarkup([row for row in rows if row])


def _verse_end_text(session: InteractionSession, page: int) -> str:
    book = _required_book(session)
    chapter = _required_chapter(session)
    start = _required_start(session)
    verses = _ending_verses(session)
    return (
        f"{book.name} {chapter.number}:{start} — choose the last verse\n\n"
        "Choose the same verse for a single-verse selection.\n"
        f"Page {page + 1} of {_page_count(len(verses), VERSE_PAGE_SIZE)}"
    )


def _verse_end_keyboard(session: InteractionSession, page: int) -> InlineKeyboardMarkup:
    verses = _ending_verses(session)
    items, page = _page(verses, page, VERSE_PAGE_SIZE)
    rows = [
        [
            InlineKeyboardButton(
                str(verse),
                callback_data=_callback(session, "ve", str(verse)),
            )
            for verse in items[index : index + 5]
        ]
        for index in range(0, len(items), 5)
    ]
    rows.append(_navigation_row(session, "vep", page, len(verses), VERSE_PAGE_SIZE))
    rows.append(
        [
            InlineKeyboardButton(
                "Choose first verse again",
                callback_data=_callback(session, "vback"),
            ),
            InlineKeyboardButton("Cancel", callback_data=_callback(session, "cancel")),
        ]
    )
    return InlineKeyboardMarkup([row for row in rows if row])


def _reference_review_text(session: InteractionSession) -> str:
    return (
        "Ready to post this Scripture:\n\n"
        f"{_reference_selection(session)}\n"
        f"Translation: {session.translation.upper()}"
    )


def _reference_review_keyboard(session: InteractionSession) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Post Scripture", callback_data=_callback(session, "rpost"))],
            [
                InlineKeyboardButton("Change range", callback_data=_callback(session, "vback")),
                InlineKeyboardButton("Start over", callback_data=_callback(session, "rreset")),
            ],
            [InlineKeyboardButton("Cancel", callback_data=_callback(session, "cancel"))],
        ]
    )


def _search_dashboard_text(session: InteractionSession) -> str:
    options = session.search_options
    books = "all"
    if options.books:
        books = f"{len(options.books)} selected"
    exclusions = ", ".join(options.exclude) if options.exclude else "none"
    proximity = "none" if options.proximity is None else str(options.proximity)
    return (
        "Configure Scripture search\n\n"
        f"Translation: {options.translation.upper()}\n"
        f"Words: {_display(options.words)}\n"
        f"Match: {_display(options.match)}\n"
        f"Scope: {_display(options.scope)}\n"
        f"Case: {'sensitive' if options.case_sensitive else 'insensitive'}\n"
        f"Diacritics: {options.diacritics}\n"
        f"Sort: {options.sort}\n"
        f"Books: {books}\n"
        f"Exclude: {exclusions}\n"
        f"Proximity: {proximity}\n\n"
        "Choose Search words when the filters are ready."
    )


def _search_dashboard_keyboard(session: InteractionSession) -> InlineKeyboardMarkup:
    options = session.search_options
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Translation: {options.translation.upper()}",
                    callback_data=_callback(session, "st"),
                ),
                InlineKeyboardButton(
                    f"Words: {_display(options.words)}",
                    callback_data=_callback(session, "sw"),
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Match: {_display(options.match)}",
                    callback_data=_callback(session, "sm"),
                ),
                InlineKeyboardButton(
                    f"Scope: {_display(options.scope)}",
                    callback_data=_callback(session, "ss"),
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Case: {'On' if options.case_sensitive else 'Off'}",
                    callback_data=_callback(session, "sc"),
                ),
                InlineKeyboardButton(
                    f"Diacritics: {_display(options.diacritics)}",
                    callback_data=_callback(session, "sdi"),
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Sort: {_display(options.sort)}",
                    callback_data=_callback(session, "so"),
                ),
                InlineKeyboardButton(
                    f"Books: {len(options.books) if options.books else 'All'}",
                    callback_data=_callback(session, "sb"),
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Exclude: {len(options.exclude) if options.exclude else 'None'}",
                    callback_data=_callback(session, "sx"),
                ),
                InlineKeyboardButton(
                    f"Proximity: {options.proximity if options.proximity is not None else 'None'}",
                    callback_data=_callback(session, "spr"),
                ),
            ],
            [InlineKeyboardButton("Search words", callback_data=_callback(session, "sq"))],
            [
                InlineKeyboardButton("Reset defaults", callback_data=_callback(session, "sreset")),
                InlineKeyboardButton("Cancel", callback_data=_callback(session, "cancel")),
            ],
        ]
    )


def _search_books_text(session: InteractionSession, page: int) -> str:
    books = _filtered_books(session)
    return (
        "Optionally restrict search to specific books.\n\n"
        f"Section: {_testament_label(session.testament)}\n"
        f"Selected: {len(session.search_options.books)}\n"
        f"Page {page + 1} of {_page_count(len(books), BOOK_PAGE_SIZE)}"
    )


def _search_books_keyboard(session: InteractionSession, page: int) -> InlineKeyboardMarkup:
    books = _filtered_books(session)
    items, page = _page(books, page, BOOK_PAGE_SIZE)
    selected = set(session.search_options.books)
    rows: list[list[InlineKeyboardButton]] = [_testament_row(session, "sbg")]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    _button_label(
                        f"{'☑' if book.number in selected else '☐'} {book.name}"
                    ),
                    callback_data=_callback(session, "sbt", str(book.number)),
                )
                for book in items[index : index + 2]
            ]
            for index in range(0, len(items), 2)
        ]
    )
    rows.append(_navigation_row(session, "sbp", page, len(books), BOOK_PAGE_SIZE))
    rows.append(
        [
            InlineKeyboardButton("Done", callback_data=_callback(session, "sbdone")),
            InlineKeyboardButton("Clear", callback_data=_callback(session, "sbclear")),
        ]
    )
    return InlineKeyboardMarkup([row for row in rows if row])


def _search_proximity_keyboard(session: InteractionSession) -> InlineKeyboardMarkup:
    values: list[int | None] = [None, 0, 1, 2, 5, 10, 25, 50, 100]
    buttons = [
        InlineKeyboardButton(
            f"{'✓ ' if session.search_options.proximity == value else ''}"
            f"{'None' if value is None else value}",
            callback_data=_callback(session, "spv", "none" if value is None else str(value)),
        )
        for value in values
    ]
    rows = [buttons[index : index + 3] for index in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("Back", callback_data=_callback(session, "sdash"))])
    return InlineKeyboardMarkup(rows)


def _search_results_text(session: InteractionSession, page: int) -> str:
    returned = len(session.search_results)
    page_total = _page_count(returned, SEARCH_PAGE_SIZE)
    coverage = (
        f"Showing {returned} of {session.search_total} matches"
        if session.search_total > returned
        else f"{session.search_total} matches"
    )
    empty = "\n\nNo verses matched. Change the filters or search words." if returned == 0 else ""
    return (
        f"Search: {session.search_query}\n"
        f"Translation: {session.search_options.translation.upper()}\n"
        f"{coverage}\n"
        f"Selected: {len(session.selected)}\n"
        f"Page {page + 1} of {page_total}\n\n"
        "Select one or more verses, then choose Post selected."
        f"{empty}"
    )


def _search_results_keyboard(
    session: InteractionSession,
    page: int,
) -> InlineKeyboardMarkup:
    items, page = _page(session.search_results, page, SEARCH_PAGE_SIZE)
    start = page * SEARCH_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    for offset, item in enumerate(items):
        index = start + offset
        marker = "☑" if index in session.selected else "☐"
        label = _button_label(
            f"{marker} {item.reference} — {_plain_preview(item.text, 32)}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=_callback(session, "srt", str(index)),
                )
            ]
        )
    rows.append(
        _navigation_row(
            session,
            "srp",
            page,
            len(session.search_results),
            SEARCH_PAGE_SIZE,
        )
    )
    if session.search_results:
        rows.append(
            [
                InlineKeyboardButton(
                    f"Post selected ({len(session.selected)})",
                    callback_data=_callback(session, "spost"),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("New search", callback_data=_callback(session, "snew")),
            InlineKeyboardButton("Filters", callback_data=_callback(session, "sdash")),
        ]
    )
    rows.append([InlineKeyboardButton("Cancel", callback_data=_callback(session, "cancel"))])
    return InlineKeyboardMarkup([row for row in rows if row])


def _safe_error_message(error: Exception, request_id: str) -> tuple[str, bool]:
    if isinstance(error, RobotRateLimited):
        return (
            f"Too many requests. Please try again in about {error.retry_after} seconds.",
            True,
        )
    if isinstance(error, RequestLimitError):
        return (
            "That request exceeds the bot's safety limits. "
            "Please request fewer verses, references, or search results.",
            True,
        )
    if isinstance(error, SearchValidationError):
        return (
            "I could not use those search words or filters. "
            "Please shorten the query or adjust the filters.",
            True,
        )
    if isinstance(error, ReferenceValidationError | RobotInputError):
        return (
            "I could not understand that Scripture selection. "
            "Try /bible John 3:16 or start again with /bible.",
            True,
        )
    if isinstance(error, TranslationNotFoundError):
        return (
            "That Bible translation is not available. "
            "Choose another translation and try again.",
            True,
        )
    if isinstance(error, RobotBusy):
        return (
            "The bot is handling its safe workload limit. Please try again shortly.",
            True,
        )
    if isinstance(error, CircuitOpen | ScriptureUnavailable):
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


def _callback(session: InteractionSession, action: str, value: str = "") -> str:
    data = f"gb:{session.token}:{action}:{value}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Callback data exceeds Telegram's limit.")
    return data


def _navigation_row(
    session: InteractionSession,
    action: str,
    page: int,
    total: int,
    page_size: int,
) -> list[InlineKeyboardButton]:
    pages = _page_count(total, page_size)
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(
            InlineKeyboardButton(
                "‹ Previous",
                callback_data=_callback(session, action, str(page - 1)),
            )
        )
    if page + 1 < pages:
        row.append(
            InlineKeyboardButton(
                "Next ›",
                callback_data=_callback(session, action, str(page + 1)),
            )
        )
    return row


def _testament_row(
    session: InteractionSession,
    action: str,
) -> list[InlineKeyboardButton]:
    available = {_book_testament(book.number) for book in session.books}
    return [
        InlineKeyboardButton(
            f"{'✓ ' if session.testament == key else ''}{label}",
            callback_data=_callback(session, action, key),
        )
        for key, label in (("ot", "Old"), ("nt", "New"), ("dc", "Other"))
        if key in available
    ]


def _select_translation(session: InteractionSession, code: str) -> None:
    if not any(item.code == code for item in session.translations):
        raise RobotInputError("Translation selection is invalid.")
    session.translation = code


def _translation_name(
    translations: tuple[TranslationOption, ...],
    code: str,
) -> str:
    return next((item.name for item in translations if item.code == code), code.upper())


def _filtered_books(session: InteractionSession) -> tuple[BookOption, ...]:
    return tuple(
        book
        for book in session.books
        if _book_testament(book.number) == session.testament
    )


def _book_testament(number: int) -> str:
    if 1 <= number <= 39:
        return "ot"
    if 40 <= number <= 66:
        return "nt"
    return "dc"


def _testament(value: str) -> str:
    if value not in {"ot", "nt", "dc"}:
        raise RobotInputError("Invalid testament selection.")
    return value


def _testament_label(value: str) -> str:
    return {"ot": "Old Testament", "nt": "New Testament", "dc": "Other books"}[value]


def _default_testament(books: tuple[BookOption, ...]) -> str:
    available = {_book_testament(book.number) for book in books}
    for candidate in ("ot", "nt", "dc"):
        if candidate in available:
            return candidate
    raise RobotInputError("This translation has no selectable books.")


def _book_by_number(books: tuple[BookOption, ...], value: str) -> BookOption:
    number = _bounded_integer(value, 1, 1000)
    for book in books:
        if book.number == number:
            return book
    raise RobotInputError("Book selection is invalid.")


def _chapter_by_number(
    chapters: tuple[ChapterOption, ...],
    value: str,
) -> ChapterOption:
    number = _bounded_integer(value, 1, 1000)
    for chapter in chapters:
        if chapter.number == number:
            return chapter
    raise RobotInputError("Chapter selection is invalid.")


def _verse_by_number(verses: tuple[int, ...], value: str) -> int:
    number = _bounded_integer(value, 1, 2000)
    if number not in verses:
        raise RobotInputError("Verse selection is invalid.")
    return number


def _required_book(session: InteractionSession) -> BookOption:
    if session.book is None:
        raise RobotInputError("Choose a book first.")
    return session.book


def _required_chapter(session: InteractionSession) -> ChapterOption:
    if session.chapter is None:
        raise RobotInputError("Choose a chapter first.")
    return session.chapter


def _required_start(session: InteractionSession) -> int:
    if session.start_verse is None:
        raise RobotInputError("Choose a starting verse first.")
    return session.start_verse


def _session_verses(session: InteractionSession) -> tuple[int, ...]:
    return _required_chapter(session).verses


def _ending_verses(session: InteractionSession) -> tuple[int, ...]:
    start = _required_start(session)
    return tuple(verse for verse in _session_verses(session) if verse >= start)


def _reference_selection(session: InteractionSession) -> str:
    book = _required_book(session)
    chapter = _required_chapter(session)
    start = _required_start(session)
    end = session.end_verse
    if end is None or end < start:
        raise RobotInputError("Choose an ending verse.")
    verses = str(start) if start == end else f"{start}-{end}"
    return f"{book.name} {chapter.number}:{verses}"


def _cycle_search_option(session: InteractionSession, name: str) -> None:
    options = session.search_options
    if name == "words":
        word_choices = ("all", "any", "phrase")
        value = word_choices[
            (word_choices.index(options.words) + 1) % len(word_choices)
        ]
        session.search_options = replace(
            options,
            words=value,
            proximity=options.proximity if value == "all" else None,
        )
        return
    if name == "match":
        match_choices = ("whole_word", "substring")
        value = match_choices[
            (match_choices.index(options.match) + 1) % len(match_choices)
        ]
        session.search_options = replace(options, match=value)
        return
    if name == "scope":
        scope_choices = ("bible", "old_testament", "new_testament", "deuterocanon")
        value = scope_choices[
            (scope_choices.index(options.scope) + 1) % len(scope_choices)
        ]
        session.search_options = replace(options, scope=value)
        return
    raise RobotInputError("Unknown search option.")


def _parse_exclusions(value: str, maximum_length: int) -> tuple[str, ...]:
    if value == "-":
        return ()
    if not value or len(value) > maximum_length:
        raise RobotInputError("Search exclusions are invalid.")
    words = tuple(dict.fromkeys(re.findall(r"\S+", value)))
    if not words or len(words) > MAX_EXCLUSIONS:
        raise RobotInputError("Too many search exclusions.")
    return words


def _selected_search_reference(session: InteractionSession) -> str:
    selected = [
        session.search_results[index]
        for index in sorted(session.selected)
        if 0 <= index < len(session.search_results)
    ]
    if not selected:
        raise RobotInputError("Select at least one search result.")

    groups: OrderedDict[tuple[int, str, int], list[int]] = OrderedDict()
    for item in selected:
        key = (item.book_number, item.book_name, item.chapter)
        groups.setdefault(key, []).append(item.verse)
    references = [
        f"{book_name} {chapter}:{_number_ranges(verses)}"
        for (_, book_name, chapter), verses in groups.items()
    ]
    return ";".join(references)


def _search_selection_fits(
    session: InteractionSession,
    selected: set[int],
    settings: Settings,
) -> bool:
    if len(selected) > settings.max_total_verses:
        return False
    original = session.selected
    try:
        session.selected = selected
        reference = _selected_search_reference(session)
    finally:
        session.selected = original
    return (
        len(reference) <= settings.max_input_length
        and len(reference.split(";")) <= settings.max_references
    )


def _number_ranges(values: list[int]) -> str:
    numbers = sorted(set(values))
    if not numbers:
        raise RobotInputError("No verses were selected.")
    result: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        result.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    result.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(result)


def _page(
    values: Sequence[_T],
    page: int,
    page_size: int,
) -> tuple[Sequence[_T], int]:
    pages = _page_count(len(values), page_size)
    page = min(max(0, page), pages - 1)
    start = page * page_size
    return values[start : start + page_size], page


def _page_count(total: int, page_size: int) -> int:
    return max(1, (total + page_size - 1) // page_size)


def _bounded_page(value: str, total: int, page_size: int) -> int:
    page = _bounded_integer(value, 0, _page_count(total, page_size) - 1)
    return page


def _bounded_integer(value: str, minimum: int, maximum: int) -> int:
    if not value.isdigit():
        raise RobotInputError("Invalid numeric selection.")
    number = int(value)
    if not minimum <= number <= maximum:
        raise RobotInputError("Numeric selection is outside its allowed range.")
    return number


def _page_containing(index: int, page_size: int) -> int:
    return max(0, index // page_size)


def _page_for_book(books: tuple[BookOption, ...], number: int) -> int:
    for index, book in enumerate(books):
        if book.number == number:
            return _page_containing(index, BOOK_PAGE_SIZE)
    return 0


def _button_label(value: str) -> str:
    return value if len(value) <= BUTTON_LABEL_LIMIT else value[: BUTTON_LABEL_LIMIT - 1] + "…"


def _plain_preview(value: str, maximum: int) -> str:
    clean = html.unescape(" ".join(value.split()))
    return clean if len(clean) <= maximum else clean[: maximum - 1] + "…"


def _display(value: str) -> str:
    return value.replace("_", " ").title()


def _require_kind(session: InteractionSession, kind: str) -> None:
    if session.kind != kind:
        raise RobotInputError("This control belongs to another workflow.")
