"""Telegram commands and bounded interactive Scripture workflows."""

from __future__ import annotations

import html
import logging
import re
import secrets
import unicodedata
from collections import OrderedDict
from collections.abc import Mapping, Sequence
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
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import Conflict, TelegramError
from telegram.ext import ContextTypes

from config import Settings
from modules.audit import audit_event, audit_identity
from modules.catalog import BookOption, ChapterOption, TranslationOption
from modules.ephemeral import (
    TELEGRAM_TEXT_LIMIT,
    delete_ephemeral_text,
    edit_ephemeral_text,
    send_ephemeral_text,
    telegram_text_length,
)
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
    ReferenceSelection,
    SearchOptions,
    SearchResult,
)
from modules.miniapp_sessions import MiniAppRoute
from modules.miniapp_tornado import MiniAppServer
from modules.posting import post_scripture
from modules.preferences import UserPreferenceStore
from modules.rate_limit import InboundRateLimiter
from modules.service import ScriptureQuery, ScriptureService
from modules.utils import safe_delete_command, safe_delete_messages, send_typing

LOGGER = logging.getLogger(__name__)

SETTINGS_SLOT = "settings"
SERVICE_SLOT = "scripture_service"
LIMITER_SLOT = "inbound_rate_limiter"
INTERACTIONS_SLOT = "interaction_store"
PREFERENCES_SLOT = "user_preference_store"
MINI_APP_SLOT = "mini_app_server"
DUPLICATE_POLLER_SLOT = "duplicate_poller_detected"

TRANSLATION_PAGE_SIZE = 6
BOOK_PAGE_SIZE = 10
CHAPTER_PAGE_SIZE = 20
VERSE_PAGE_SIZE = 25
SEARCH_PAGE_SIZE = 30
MAX_EXCLUSIONS = 32
BUTTON_LABEL_LIMIT = 60
_CALLBACK_RE = re.compile(r"gb:([A-Za-z0-9_-]{8,16}):([a-z]{1,8}):([A-Za-z0-9_-]{0,32})\Z")
_INCOMPLETE_REFERENCE_RE = re.compile(r"[\w\s-]{1,512}\Z")
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
        "radd",
        "rmore",
        "rrm",
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
        "srp",
        "spv",
        "sq",
        "sreset",
        "srs",
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
        "vone",
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


def _preference_store(
    context: ContextTypes.DEFAULT_TYPE,
) -> UserPreferenceStore | None:
    value = context.application.bot_data.get(PREFERENCES_SLOT)
    return value if isinstance(value, UserPreferenceStore) else None


def _mini_app(context: ContextTypes.DEFAULT_TYPE) -> MiniAppServer | None:
    value = context.application.bot_data.get(MINI_APP_SLOT)
    return value if isinstance(value, MiniAppServer) else None


async def _send_mini_app_launch(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mini_app: MiniAppServer,
    *,
    route: MiniAppRoute,
    query: str,
    text: str,
) -> None:
    """Open an owner-bound Mini App route without exposing content in its URL."""
    identity = _identity(update)
    if identity is None or update.effective_chat is None:
        return
    chat_id, user_id = identity
    source_ephemeral_message_id, source_ephemeral_receiver_user_id = (
        _ephemeral_source_target(update, context)
    )
    if source_ephemeral_receiver_user_id is None:
        source_ephemeral_message_id = None
    launch = mini_app.create_launch(
        user_id=user_id,
        target_chat_id=chat_id,
        message_thread_id=_update_message_thread_id(update),
        initial_route=route,
        initial_query=query,
        source_ephemeral_message_id=source_ephemeral_message_id,
        source_ephemeral_receiver_user_id=source_ephemeral_receiver_user_id,
    )
    if update.effective_chat.type == "private":
        url = mini_app.web_url(launch)
    else:
        raw_username = context.bot.username
        if not isinstance(raw_username, str) or not raw_username:
            remote_username = (await context.bot.get_me()).username
            raw_username = remote_username if isinstance(remote_username, str) else ""
        username = raw_username
        if not isinstance(username, str) or not username:
            raise TelegramError("Telegram did not provide the bot username.")
        url = mini_app.direct_url(username, launch)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open getBible.Life", web_app=WebAppInfo(url=url))]]
        if update.effective_chat.type == "private"
        else [[InlineKeyboardButton("Open getBible.Life", url=url)]]
    )
    if _uses_ephemeral_interaction(update):
        ephemeral_message_id = await send_ephemeral_text(
            context.bot,
            chat_id=chat_id,
            receiver_user_id=user_id,
            text=text,
            reply_markup=keyboard,
            reply_to_ephemeral_message_id=_update_ephemeral_message_id(update),
            message_thread_id=_update_message_thread_id(update),
        )
        mini_app.remember_prompt(
            launch,
            ephemeral_message_id=ephemeral_message_id,
        )
        return
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        message_thread_id=_update_message_thread_id(update),
    )
    message_id = getattr(message, "message_id", None)
    if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0:
        mini_app.remember_prompt(launch, message_id=message_id)


def _preferred_translation(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    application_default: str,
) -> str:
    store = _preference_store(context)
    return (
        store.translation_for(user_id)
        if store is not None
        else application_default
    )


def _save_preferred_translation(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    translation: str,
) -> None:
    store = _preference_store(context)
    if store is not None:
        store.set_translation(user_id, translation)


async def _allow_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    limiter: InboundRateLimiter,
    *,
    session: InteractionSession | None = None,
    ephemeral_receiver_user_id: int | None = None,
    reply_to_ephemeral_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> bool:
    """Apply budgets and suppress repeated Telegram rejection notifications."""
    identity = _identity(update)
    if identity is None:
        return False
    chat_id, user_id = identity
    try:
        await limiter.acquire(user_id=user_id, chat_id=chat_id)
    except RobotRateLimited as error:
        settings = cast(Settings, context.application.bot_data[SETTINGS_SLOT])
        audit_event(
            LOGGER,
            settings,
            "inbound_rate_limited",
            metadata={
                "source": "telegram",
                "retry_after_seconds": error.retry_after,
                "temporarily_blocked": error.blocked,
                "new_block": error.new_block,
                "violation_count": error.violation_count,
                "limited_scopes": ",".join(error.scopes),
            },
            identity=audit_identity(
                settings,
                user_id=user_id,
                chat_id=chat_id,
            ),
            level=logging.WARNING,
        )
        if error.new_block or await limiter.should_notify_rejection(
            user_id=user_id,
            chat_id=chat_id,
        ):
            if error.blocked:
                warning = getattr(
                    settings,
                    "abuse_warning_message",
                    (
                        "Your requests have been paused because the bot received "
                        "repeated requests too quickly. Please stop repeated or "
                        "automated requests and try again later."
                    ),
                )
                text = (
                    f"{warning}\n\n"
                    f"Please try again in about {error.retry_after} seconds."
                )
            else:
                text = (
                    "Too many requests. Please try again in about "
                    f"{error.retry_after} seconds."
                )
            if session is not None and session.ephemeral:
                callback = getattr(update, "callback_query", None)
                if callback is not None:
                    await callback.answer(text, show_alert=True)
                else:
                    await _send_ephemeral_notice(session, context, text)
            elif ephemeral_receiver_user_id is not None:
                try:
                    await send_ephemeral_text(
                        context.bot,
                        chat_id=chat_id,
                        receiver_user_id=ephemeral_receiver_user_id,
                        text=text,
                        reply_to_ephemeral_message_id=(
                            reply_to_ephemeral_message_id
                        ),
                        message_thread_id=message_thread_id,
                    )
                except TelegramError:
                    LOGGER.warning(
                        "Unable to deliver an ephemeral rate-limit notice"
                    )
            else:
                message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                )
                if session is not None:
                    session.remember_message(message.message_id)
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
    ephemeral = _uses_ephemeral_interaction(update)
    source_ephemeral_message_id = _update_ephemeral_message_id(update)
    source_ephemeral_receiver_user_id = _update_ephemeral_receiver_user_id(update)
    message_thread_id = _update_message_thread_id(update)
    preferred_translation = _preferred_translation(
        context,
        user_id=user_id,
        application_default=settings.default_translation,
    )
    try:
        if not await _allow_command(
            update,
            context,
            limiter,
            ephemeral_receiver_user_id=user_id if ephemeral else None,
            reply_to_ephemeral_message_id=source_ephemeral_message_id,
            message_thread_id=message_thread_id,
        ):
            return
        query = " ".join(context.args or ()).strip()
        mini_app = _mini_app(context)
        if mini_app is not None:
            await _send_mini_app_launch(
                update,
                context,
                mini_app,
                route="search",
                query=query,
                text=(
                    f'Explore contained search results for “{query}”.'
                    if query
                    else "Search and filter Scripture in getBible.Life."
                ),
            )
            await _cleanup_command_source(update, context, chat_id=chat_id)
            return
        session = interactions.create(
            chat_id=chat_id,
            user_id=user_id,
            kind="search",
            stage="search_dashboard",
            translation=preferred_translation,
        )
        session.ephemeral = ephemeral
        session.message_thread_id = message_thread_id
        session.source_ephemeral_message_id = source_ephemeral_message_id
        session.source_ephemeral_receiver_user_id = (
            source_ephemeral_receiver_user_id
        )
        session.remember_message(_update_message_id(update))
        if query:
            if not session.ephemeral:
                await send_typing(update, context)
            await _run_search(session, query, service, settings)
            await _send_search_results(session, context, settings)
        else:
            await _send_interaction_message(
                session,
                context,
                text=_search_dashboard_text(session),
                reply_markup=_search_dashboard_keyboard(session),
            )
    except Exception as error:
        if "session" in locals():
            interactions.remove(session.token)
        await _report_command_error(
            error,
            request_id,
            chat_id,
            context,
            session=session if "session" in locals() else None,
            ephemeral_receiver_user_id=user_id if ephemeral else None,
            reply_to_ephemeral_message_id=source_ephemeral_message_id,
            message_thread_id=message_thread_id,
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
    ephemeral = _uses_ephemeral_interaction(update)
    source_ephemeral_message_id = _update_ephemeral_message_id(update)
    source_ephemeral_receiver_user_id = _update_ephemeral_receiver_user_id(update)
    message_thread_id = _update_message_thread_id(update)
    preferred_translation = _preferred_translation(
        context,
        user_id=user_id,
        application_default=settings.default_translation,
    )

    try:
        if not await _allow_command(
            update,
            context,
            limiter,
            ephemeral_receiver_user_id=user_id if ephemeral else None,
            reply_to_ephemeral_message_id=source_ephemeral_message_id,
            message_thread_id=message_thread_id,
        ):
            return
        mini_app = _mini_app(context)
        if context.args:
            raw_reference = " ".join(context.args).strip()
            try:
                query = await service.resolve_query(
                    context.args,
                    default_translation=preferred_translation,
                )
            except (ReferenceValidationError, RobotInputError):
                if mini_app is None or not _looks_like_incomplete_reference(
                    raw_reference
                ):
                    raise
                await _send_mini_app_launch(
                    update,
                    context,
                    mini_app,
                    route="bible",
                    query=raw_reference,
                    text="Complete this Scripture reference in getBible.Life.",
                )
                await _cleanup_command_source(update, context, chat_id=chat_id)
                return
            if not ephemeral:
                await send_typing(update, context)
            await _post_scripture(
                chat_id,
                query,
                settings,
                service,
                context,
                source="bible_direct",
                message_thread_id=message_thread_id,
            )
            await _cleanup_command_source(update, context, chat_id=chat_id)
            return

        if mini_app is not None:
            await _send_mini_app_launch(
                update,
                context,
                mini_app,
                route="bible",
                query="",
                text="Choose a translation, chapter, and full-text verse.",
            )
            await _cleanup_command_source(update, context, chat_id=chat_id)
            return

        if not ephemeral:
            await send_typing(update, context)
        translations = await service.translations()
        selected_translation = _available_translation(
            translations,
            preferred_translation,
            settings.default_translation,
        )
        session = interactions.create(
            chat_id=chat_id,
            user_id=user_id,
            kind="reference",
            stage="reference_translation",
            translation=selected_translation,
        )
        session.ephemeral = ephemeral
        session.message_thread_id = message_thread_id
        session.source_ephemeral_message_id = source_ephemeral_message_id
        session.source_ephemeral_receiver_user_id = (
            source_ephemeral_receiver_user_id
        )
        session.remember_message(_update_message_id(update))
        session.translations = translations
        await _send_interaction_message(
            session,
            context,
            text=_translation_text(session, 0),
            reply_markup=_translation_keyboard(session, 0),
        )
    except Exception as error:
        if session is not None:
            interactions.remove(session.token)
        await _report_command_error(
            error,
            request_id,
            chat_id,
            context,
            session=session,
            ephemeral_receiver_user_id=user_id if ephemeral else None,
            reply_to_ephemeral_message_id=source_ephemeral_message_id,
            message_thread_id=message_thread_id,
        )


def _looks_like_incomplete_reference(value: str) -> bool:
    """Recognize a safe book/chapter fragment without accepting malformed syntax."""
    return bool(
        value
        and _INCOMPLETE_REFERENCE_RE.fullmatch(value)
        and any(character.isalpha() for character in value)
    )


async def interaction_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Serialize controls belonging to one owner-scoped interaction."""
    callback = update.callback_query
    identity = _identity(update)
    if callback is None or not isinstance(callback.data, str) or identity is None:
        return
    match = _CALLBACK_RE.fullmatch(callback.data)
    if match is not None and match.group(2) in CALLBACK_ACTIONS:
        chat_id, user_id = identity
        interactions = _components(context)[3]
        session = interactions.get(
            match.group(1),
            chat_id=chat_id,
            user_id=user_id,
        )
        if session is not None:
            async with session.callback_lock:
                await _interaction_callback_unlocked(update, context)
            return
    await _interaction_callback_unlocked(update, context)


async def _interaction_callback_unlocked(
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

    _, service, _, interactions = _components(context)
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

    if (
        session.ephemeral
        and session.stage in {"search_exclude", "search_query"}
        and action != "cancel"
    ):
        await callback.answer(
            "Reply to the current private prompt first.",
            show_alert=True,
        )
        return

    if action == "srt":
        generation, separator, raw_index = value.partition("-")
        if (
            session.stage != "search_results"
            or not separator
            or not generation.isdigit()
            or int(generation) != session.search_generation
            or not raw_index.isdigit()
            or int(raw_index) >= len(session.search_results)
        ):
            await callback.answer("That result is no longer available.", show_alert=True)
            return
        index = int(raw_index)
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
        await _edit_search_selection(
            session,
            index,
            update,
            context,
        )
        return

    if action == "spost":
        if (
            session.stage != "search_results"
            or not value.isdigit()
            or int(value) != session.search_generation
        ):
            await callback.answer(
                "Those search results are no longer active.",
                show_alert=True,
            )
            return
        if not session.selected:
            await callback.answer("Select at least one verse first.", show_alert=True)
            return

    if action == "srs":
        generation, separator, raw_scope = value.partition("-")
        if (
            session.stage != "search_results"
            or not separator
            or not generation.isdigit()
            or int(generation) != session.search_generation
        ):
            await callback.answer(
                "Those search results are no longer active.",
                show_alert=True,
            )
            return
        try:
            _search_result_scope(raw_scope)
        except RobotInputError:
            await callback.answer(
                "That Scripture section is invalid.",
                show_alert=True,
            )
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
        message_id = await _report_command_error(
            error,
            request_id,
            chat_id,
            context,
            session=session,
        )
        session.remember_message(message_id)


async def interaction_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Accept only replies to a bot-created prompt owned by this user/session."""
    if update.effective_message is None or update.effective_message.text is None:
        return
    identity = _identity(update)
    if identity is None:
        return
    settings, service, limiter, interactions = _components(context)
    chat_id, user_id = identity
    reply_to = getattr(update.effective_message, "reply_to_message", None)
    session = None
    if reply_to is not None:
        session = interactions.find_prompt(
            chat_id=chat_id,
            user_id=user_id,
            prompt_message_id=reply_to.message_id,
        )
    if session is None and _update_ephemeral_message_id(update) is not None:
        session = interactions.find_pending_input(
            chat_id=chat_id,
            user_id=user_id,
        )
    if session is None:
        return

    session.remember_message(_update_message_id(update))
    text = update.effective_message.text.strip()
    request_id = secrets.token_hex(4)
    try:
        if not await _allow_command(
            update,
            context,
            limiter,
            session=session,
        ):
            return
        if session.stage == "search_exclude":
            exclusions = _parse_exclusions(text, settings.max_input_length)
            session.search_options = replace(
                session.search_options,
                exclude=exclusions,
            )
            session.stage = "search_dashboard"
            session.prompt_message_id = None
            await _clear_ephemeral_prompt(
                session,
                context,
                reply_ephemeral_message_id=_update_ephemeral_message_id(update),
                reply_ephemeral_receiver_user_id=(
                    _update_ephemeral_receiver_user_id(update)
                ),
            )
            await _edit(
                session,
                context,
                _search_dashboard_text(session),
                _search_dashboard_keyboard(session),
            )
            if not session.ephemeral:
                message = await context.bot.send_message(
                    chat_id=chat_id,
                    text="Search exclusions updated.",
                )
                session.remember_message(message.message_id)
            return

        if session.stage != "search_query":
            return
        if not session.ephemeral:
            await send_typing(update, context)
        await _run_search(session, text, service, settings)
        session.prompt_message_id = None
        await _clear_ephemeral_prompt(
            session,
            context,
            reply_ephemeral_message_id=_update_ephemeral_message_id(update),
            reply_ephemeral_receiver_user_id=(
                _update_ephemeral_receiver_user_id(update)
            ),
        )
        await _send_search_results(session, context, settings)
    except Exception as error:
        message_id = await _report_command_error(
            error,
            request_id,
            chat_id,
            context,
            session=session,
        )
        session.remember_message(message_id)


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
            _save_preferred_translation(
                context,
                user_id=session.user_id,
                translation=session.translation,
            )
        if session.kind == "reference":
            session.reference_selections.clear()
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
    if action == "vone":
        _require_kind(session, "reference")
        start = _required_start(session)
        _add_reference_selection(session, start, start, settings)
        session.stage = "reference_review"
        await _edit(
            session,
            context,
            _reference_review_text(session),
            _reference_review_keyboard(session),
        )
        return
    if action == "vep":
        _require_kind(session, "reference")
        verses = _range_ending_verses(session)
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
        end = _verse_by_number(_range_ending_verses(session), value)
        _add_reference_selection(
            session,
            _required_start(session),
            end,
            settings,
        )
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
        reference = _reference_basket_reference(session)
        query = ScriptureQuery(reference, session.translation)
        await _edit(
            session,
            context,
            f"Posting {reference} ({session.translation})…",
            None,
        )
        try:
            await _post_scripture(
                session.chat_id,
                query,
                settings,
                service,
                context,
                source="bible_guided",
                message_thread_id=session.message_thread_id,
            )
        except Exception:
            try:
                await _edit(
                    session,
                    context,
                    _reference_review_text(session),
                    _reference_review_keyboard(session),
                )
            except TelegramError:
                LOGGER.warning(
                    "Unable to restore private Bible controls after posting failure"
                )
            raise
        interactions.remove(session.token)
        await _cleanup_interaction(session, context)
        return
    if action == "rmore":
        _require_kind(session, "reference")
        _required_chapter(session)
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
    if action == "radd":
        _require_kind(session, "reference")
        session.testament = _default_testament(session.books)
        session.book = None
        session.chapter = None
        session.start_verse = None
        session.end_verse = None
        session.stage = "reference_books"
        await _edit(
            session,
            context,
            _books_text(session, 0),
            _books_keyboard(session, 0),
        )
        return
    if action == "rrm":
        _require_kind(session, "reference")
        index = _bounded_integer(
            value,
            0,
            max(0, len(session.reference_selections) - 1),
        )
        if not session.reference_selections:
            raise RobotInputError("There is no Scripture selection to remove.")
        session.reference_selections.pop(index)
        if session.reference_selections:
            await _edit(
                session,
                context,
                _reference_review_text(session),
                _reference_review_keyboard(session),
            )
            return
        session.start_verse = None
        session.end_verse = None
        if session.chapter is not None:
            session.stage = "reference_start"
            await _edit(
                session,
                context,
                _verse_start_text(session, 0),
                _verse_start_keyboard(session, 0),
            )
        else:
            session.stage = "reference_books"
            await _edit(
                session,
                context,
                _books_text(session, 0),
                _books_keyboard(session, 0),
            )
        return
    if action == "rreset":
        _require_kind(session, "reference")
        session.stage = "reference_translation"
        session.book = None
        session.chapter = None
        session.start_verse = None
        session.end_verse = None
        session.reference_selections.clear()
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
        preferred_translation = _preferred_translation(
            context,
            user_id=session.user_id,
            application_default=settings.default_translation,
        )
        session.search_options = SearchOptions(
            translation=preferred_translation,
        )
        session.translation = preferred_translation
        session.books = ()
        session.search_results = ()
        session.search_page = 0
        session.search_page_ranges = ()
        session.selected.clear()
        await _show_search_dashboard(session, context)
        return
    if action == "srs":
        _require_kind(session, "search")
        generation, separator, raw_scope = value.partition("-")
        if (
            session.stage != "search_results"
            or not separator
            or not generation.isdigit()
            or int(generation) != session.search_generation
        ):
            raise RobotInputError("Search scope selection is no longer active.")
        scope = _search_result_scope(raw_scope)
        if scope != session.search_options.scope or session.search_options.books:
            previous_options = session.search_options
            session.search_options = replace(
                previous_options,
                scope=scope,
                books=(),
            )
            try:
                await _run_search(
                    session,
                    session.search_query,
                    service,
                    settings,
                )
            except Exception:
                session.search_options = previous_options
                raise
        await _edit(
            session,
            context,
            _search_results_text(session),
            _search_results_keyboard(session),
            parse_mode=ParseMode.HTML,
        )
        return
    if action == "srp":
        _require_kind(session, "search")
        generation, separator, raw_page = value.partition("-")
        if (
            session.stage != "search_results"
            or not separator
            or not generation.isdigit()
            or int(generation) != session.search_generation
            or not raw_page.isdigit()
        ):
            raise RobotInputError("Search page selection is invalid.")
        session.search_page = _bounded_integer(
            raw_page,
            0,
            max(0, len(session.search_page_ranges) - 1),
        )
        await _edit(
            session,
            context,
            _search_results_text(session),
            _search_results_keyboard(session),
            parse_mode=ParseMode.HTML,
        )
        return
    if action == "spost":
        if (
            session.stage != "search_results"
            or not value.isdigit()
            or int(value) != session.search_generation
        ):
            raise RobotInputError("Search selection is no longer active.")
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
        try:
            await _post_scripture(
                session.chat_id,
                query,
                settings,
                service,
                context,
                source="search_selection",
                message_thread_id=session.message_thread_id,
            )
        except Exception:
            try:
                await _edit(
                    session,
                    context,
                    _search_results_text(session),
                    _search_results_keyboard(session),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                LOGGER.warning(
                    "Unable to restore private search controls after posting failure"
                )
            raise
        interactions.remove(session.token)
        await _cleanup_interaction(session, context)
        return
    if action == "cancel":
        interactions.remove(session.token)
        await _cleanup_interaction(session, context)
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
    if isinstance(error, Conflict):
        application = context.application
        if not application.bot_data.get(DUPLICATE_POLLER_SLOT):
            application.bot_data[DUPLICATE_POLLER_SLOT] = True
            LOGGER.critical(
                "Telegram polling conflict: another process is using this bot token; "
                "stopping this instance"
            )
            application.stop_running()
        return
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
    if (
        session.search_options.match == "whole_word"
        and _contains_unsegmented_script(query)
    ):
        session.search_options = replace(
            session.search_options,
            match="substring",
        )
    page = await service.search(query, session.search_options)
    session.search_query = page.query
    session.search_total = page.total
    session.search_results = page.items
    session.search_generation = (session.search_generation % 999_999) + 1
    session.search_page = 0
    session.search_page_ranges = ()
    session.selected.clear()
    session.stage = "search_results"
    session.search_page_ranges = _search_page_ranges(session)
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


async def _send_search_results(
    session: InteractionSession,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
) -> None:
    """Render only the current complete-result page in one private panel."""
    del settings
    text = _search_results_text(session)
    keyboard = _search_results_keyboard(session)
    if session.ephemeral_message_id is not None or session.message_id is not None:
        await _edit(
            session,
            context,
            text,
            keyboard,
            parse_mode=ParseMode.HTML,
        )
        return
    await _send_interaction_message(
        session,
        context,
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def _post_scripture(
    chat_id: int,
    query: ScriptureQuery,
    settings: Settings,
    service: ScriptureService,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    source: str,
    message_thread_id: int | None = None,
) -> None:
    await post_scripture(
        bot=context.bot,
        chat_id=chat_id,
        query=query,
        settings=settings,
        service=service,
        source=source,
        message_thread_id=message_thread_id,
    )


async def _report_command_error(
    error: Exception,
    request_id: str,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    session: InteractionSession | None = None,
    ephemeral_receiver_user_id: int | None = None,
    reply_to_ephemeral_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> int | None:
    message, expected = _safe_error_message(error, request_id)
    log = LOGGER.info if expected else LOGGER.error
    causes = _error_cause_types(error)
    log(
        "Request %s %s (%s%s)",
        request_id,
        "rejected safely" if expected else "failed unexpectedly",
        type(error).__name__,
        f"; causes={','.join(causes)}" if causes else "",
    )
    if session is not None and session.ephemeral:
        try:
            await _send_ephemeral_notice(session, context, message)
        except TelegramError:
            LOGGER.warning(
                "Unable to deliver ephemeral error for request %s",
                request_id,
            )
        return None
    if ephemeral_receiver_user_id is not None:
        try:
            await send_ephemeral_text(
                context.bot,
                chat_id=chat_id,
                receiver_user_id=ephemeral_receiver_user_id,
                text=message,
                reply_to_ephemeral_message_id=reply_to_ephemeral_message_id,
                message_thread_id=message_thread_id,
            )
        except TelegramError:
            LOGGER.warning(
                "Unable to deliver ephemeral error for request %s",
                request_id,
            )
        return None
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=message,
        message_thread_id=(
            session.message_thread_id if session is not None else message_thread_id
        ),
    )
    message_id = getattr(sent, "message_id", None)
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id <= 0
    ):
        return None
    return message_id


async def _cleanup_interaction(
    session: InteractionSession,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Remove only the messages recorded for a completed bot workflow."""
    if session.ephemeral:
        targets = {
            (session.user_id, session.ephemeral_message_id),
            (session.user_id, session.prompt_ephemeral_message_id),
            (
                session.source_ephemeral_receiver_user_id,
                session.source_ephemeral_message_id,
            ),
        }
        for receiver_user_id, ephemeral_message_id in targets:
            if receiver_user_id is None or ephemeral_message_id is None:
                continue
            try:
                await delete_ephemeral_text(
                    context.bot,
                    chat_id=session.chat_id,
                    receiver_user_id=receiver_user_id,
                    ephemeral_message_id=ephemeral_message_id,
                )
            except TelegramError:
                LOGGER.warning(
                    "Unable to delete ephemeral workflow message safely"
                )
    await safe_delete_messages(
        context,
        chat_id=session.chat_id,
        message_ids=session.cleanup_message_ids(),
    )


async def _cleanup_command_source(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
) -> None:
    """Remove one successfully completed direct command without risking delivery."""
    ephemeral_message_id, receiver_user_id = _ephemeral_source_target(
        update,
        context,
    )
    if receiver_user_id is not None and ephemeral_message_id is not None:
        try:
            await delete_ephemeral_text(
                context.bot,
                chat_id=chat_id,
                receiver_user_id=receiver_user_id,
                ephemeral_message_id=ephemeral_message_id,
            )
        except TelegramError:
            LOGGER.warning("Unable to delete ephemeral command safely")
    message_id = _update_message_id(update)
    if message_id is not None:
        await safe_delete_messages(
            context,
            chat_id=chat_id,
            message_ids=(message_id,),
        )


def _update_message_id(update: Update) -> int | None:
    message = getattr(update, "effective_message", None)
    message_id = getattr(message, "message_id", None)
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id <= 0
    ):
        return None
    return message_id


def _update_ephemeral_message_id(update: Update) -> int | None:
    message = getattr(update, "effective_message", None)
    value = getattr(message, "ephemeral_message_id", None)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    api_kwargs = getattr(message, "api_kwargs", None)
    value = (
        api_kwargs.get("ephemeral_message_id")
        if isinstance(api_kwargs, Mapping)
        else None
    )
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value


def _update_ephemeral_receiver_user_id(update: Update) -> int | None:
    message = getattr(update, "effective_message", None)
    receiver_user = getattr(message, "receiver_user", None)
    value = getattr(receiver_user, "id", None)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    api_kwargs = getattr(message, "api_kwargs", None)
    raw_receiver = (
        api_kwargs.get("receiver_user")
        if isinstance(api_kwargs, Mapping)
        else None
    )
    value = (
        raw_receiver.get("id")
        if isinstance(raw_receiver, Mapping)
        else None
    )
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value


def _ephemeral_source_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[int | None, int | None]:
    """Resolve a command's ephemeral ID and its bot receiver for later cleanup."""
    ephemeral_message_id = _update_ephemeral_message_id(update)
    if ephemeral_message_id is None:
        return None, None
    receiver_user_id = _update_ephemeral_receiver_user_id(update)
    if receiver_user_id is None:
        bot_id = getattr(context.bot, "id", None)
        if isinstance(bot_id, int) and not isinstance(bot_id, bool) and bot_id > 0:
            receiver_user_id = bot_id
    return ephemeral_message_id, receiver_user_id


def _update_message_thread_id(update: Update) -> int | None:
    value = getattr(getattr(update, "effective_message", None), "message_thread_id", None)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value


def _uses_ephemeral_interaction(update: Update) -> bool:
    chat_type = getattr(getattr(update, "effective_chat", None), "type", None)
    return chat_type in {"group", "supergroup"} and update.effective_user is not None


def _error_cause_types(error: Exception) -> tuple[str, ...]:
    """Return a bounded, message-free exception chain for operator diagnostics."""
    result: list[str] = []
    seen = {id(error)}
    current: BaseException = error
    for _ in range(4):
        cause = current.__cause__ or current.__context__
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        result.append(type(cause).__name__)
        current = cause
    return tuple(result)


async def _edit(
    session: InteractionSession,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
    *,
    parse_mode: str | None = None,
) -> None:
    if session.ephemeral:
        if session.ephemeral_message_id is None:
            raise RobotInputError("Ephemeral interactive message is unavailable.")
        await edit_ephemeral_text(
            context.bot,
            chat_id=session.chat_id,
            receiver_user_id=session.user_id,
            ephemeral_message_id=session.ephemeral_message_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=parse_mode,
        )
        return
    if session.message_id is None:
        raise RobotInputError("Interactive message is unavailable.")
    await context.bot.edit_message_text(
        chat_id=session.chat_id,
        message_id=session.message_id,
        text=text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
        parse_mode=parse_mode,
    )


async def _edit_search_selection(
    session: InteractionSession,
    index: int,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Refresh the current private result page after one selection changes."""
    del index, update
    await _edit(
        session,
        context,
        _search_results_text(session),
        _search_results_keyboard(session),
        parse_mode=ParseMode.HTML,
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
    if session.ephemeral:
        if session.stage in {"search_exclude", "search_query"}:
            raise RobotInputError("Reply to the current search prompt first.")
        pending = _components(context)[3].find_pending_input(
            chat_id=session.chat_id,
            user_id=session.user_id,
        )
        if pending is not None and pending.token != session.token:
            raise RobotInputError(
                "Another private search prompt is already waiting for your reply."
            )
        callback = getattr(update, "callback_query", None)
        callback_query_id = getattr(callback, "id", None)
        ephemeral_message_id = await send_ephemeral_text(
            context.bot,
            chat_id=session.chat_id,
            receiver_user_id=session.user_id,
            text=text,
            reply_markup=ForceReply(input_field_placeholder="Type your reply"),
            callback_query_id=(
                callback_query_id if isinstance(callback_query_id, str) else None
            ),
            message_thread_id=session.message_thread_id,
        )
        session.stage = stage
        session.prompt_ephemeral_message_id = ephemeral_message_id
        return
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
        message_thread_id=session.message_thread_id,
    )
    session.stage = stage
    session.prompt_message_id = message.message_id
    session.remember_message(message.message_id)


async def _send_interaction_message(
    session: InteractionSession,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | ForceReply | None = None,
    parse_mode: str | None = None,
) -> None:
    """Send an interactive panel without exposing group workflow content."""
    if session.ephemeral:
        ephemeral_message_id = await send_ephemeral_text(
            context.bot,
            chat_id=session.chat_id,
            receiver_user_id=session.user_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            reply_to_ephemeral_message_id=session.source_ephemeral_message_id,
            message_thread_id=session.message_thread_id,
        )
        session.ephemeral_message_id = ephemeral_message_id
        return
    message = await context.bot.send_message(
        chat_id=session.chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        disable_web_page_preview=True,
        message_thread_id=session.message_thread_id,
    )
    session.message_id = message.message_id
    session.remember_message(message.message_id)


async def _send_ephemeral_notice(
    session: InteractionSession,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    await send_ephemeral_text(
        context.bot,
        chat_id=session.chat_id,
        receiver_user_id=session.user_id,
        text=text,
        reply_to_ephemeral_message_id=session.source_ephemeral_message_id,
        message_thread_id=session.message_thread_id,
    )


async def _clear_ephemeral_prompt(
    session: InteractionSession,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reply_ephemeral_message_id: int | None,
    reply_ephemeral_receiver_user_id: int | None,
) -> None:
    if not session.ephemeral:
        return
    for receiver_user_id, ephemeral_message_id in (
        (session.user_id, session.prompt_ephemeral_message_id),
        (reply_ephemeral_receiver_user_id, reply_ephemeral_message_id),
    ):
        if receiver_user_id is None or ephemeral_message_id is None:
            continue
        try:
            await delete_ephemeral_text(
                context.bot,
                chat_id=session.chat_id,
                receiver_user_id=receiver_user_id,
                ephemeral_message_id=ephemeral_message_id,
            )
        except TelegramError:
            LOGGER.warning("Unable to clear ephemeral search prompt safely")
    session.prompt_ephemeral_message_id = None


def _translation_text(session: InteractionSession, page: int) -> str:
    current = _translation_name(session.translations, session.translation)
    purpose = "Bible" if session.kind == "reference" else "search"
    return (
        f"Choose a translation for this {purpose}.\n\n"
        f"Your default: {current} ({session.translation})\n"
        "Continue with it, or choose another translation and save that as "
        "your personal default.\n"
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
                f"Continue with {session.translation.upper()}",
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
    verses = _range_ending_verses(session)
    return (
        f"{book.name} {chapter.number}:{start} — single verse or range?\n\n"
        "Add this verse by itself, or choose the final verse of a range.\n"
        f"Page {page + 1} of {_page_count(len(verses), VERSE_PAGE_SIZE)}"
    )


def _verse_end_keyboard(session: InteractionSession, page: int) -> InlineKeyboardMarkup:
    start = _required_start(session)
    verses = _range_ending_verses(session)
    items, page = _page(verses, page, VERSE_PAGE_SIZE)
    rows = [
        [
            InlineKeyboardButton(
                f"Add verse {start} only",
                callback_data=_callback(session, "vone"),
            )
        ]
    ]
    rows.extend(
        [
            InlineKeyboardButton(
                str(verse),
                callback_data=_callback(session, "ve", str(verse)),
            )
            for verse in items[index : index + 5]
        ]
        for index in range(0, len(items), 5)
    )
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
    selections = "\n".join(
        f"{index + 1}. {_reference_selection_label(selection)}"
        for index, selection in enumerate(session.reference_selections)
    )
    return (
        "Scripture selection\n\n"
        f"{selections}\n\n"
        f"Combined: {_reference_basket_reference(session)}\n"
        f"Translation: {session.translation.upper()}"
    )


def _reference_review_keyboard(session: InteractionSession) -> InlineKeyboardMarkup:
    remove_rows = [
        [
            InlineKeyboardButton(
                _button_label(
                    f"Remove {index + 1}: "
                    f"{_reference_selection_label(selection)}"
                ),
                callback_data=_callback(session, "rrm", str(index)),
            )
        ]
        for index, selection in enumerate(session.reference_selections)
    ]
    return InlineKeyboardMarkup(
        [
            *remove_rows,
            [
                InlineKeyboardButton(
                    "Add another from this chapter",
                    callback_data=_callback(session, "rmore"),
                )
            ],
            [
                InlineKeyboardButton(
                    "Add another book/chapter",
                    callback_data=_callback(session, "radd"),
                )
            ],
            [InlineKeyboardButton("Post selection", callback_data=_callback(session, "rpost"))],
            [InlineKeyboardButton("Start over", callback_data=_callback(session, "rreset"))],
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


def _search_page_ranges(
    session: InteractionSession,
) -> tuple[tuple[int, int], ...]:
    """Build stable pages containing at most thirty result buttons."""
    returned = len(session.search_results)
    if returned == 0:
        return ((0, 0),)
    return tuple(
        (start, min(start + SEARCH_PAGE_SIZE, returned))
        for start in range(0, returned, SEARCH_PAGE_SIZE)
    )


def _search_results_text(session: InteractionSession) -> str:
    ranges = session.search_page_ranges or _search_page_ranges(session)
    page = min(max(0, session.search_page), len(ranges) - 1)
    start, end = ranges[page]
    text = _search_results_page_text(
        session,
        start=start,
        end=end,
        page=page,
        page_count=len(ranges),
    )
    if telegram_text_length(text) > TELEGRAM_TEXT_LIMIT:
        raise RequestLimitError("The complete search page exceeds Telegram's limit.")
    return text


def _search_results_page_text(
    session: InteractionSession,
    *,
    start: int,
    end: int,
    page: int,
    page_count: int,
) -> str:
    returned = len(session.search_results)
    coverage = (
        f"{returned} returned of {session.search_total} matches"
        if session.search_total > returned
        else f"{session.search_total} matches"
    )
    header = (
        f"<b>Search:</b> {html.escape(session.search_query)}\n"
        f"<b>Translation:</b> "
        f"{html.escape(session.search_options.translation.upper())}\n"
        f"<b>Section:</b> {_search_scope_label(session.search_options.scope)}\n"
        f"<b>Results:</b> {coverage}\n"
        f"<b>Page:</b> {page + 1} of {max(1, page_count)}\n"
        f"<b>Selected:</b> {len(session.selected)}"
    )
    if returned == 0:
        return (
            f"{header}\n\n"
            "No verses matched. Change the filters or search words."
        )
    visible = end - start
    return (
        f"{header}\n\n"
        f"Showing {visible} complete verse"
        f"{'' if visible == 1 else 's'} in the menu below.\n"
        "Tap a verse block to select it, then choose "
        "<b>Post selected</b>."
    )


def _search_results_keyboard(
    session: InteractionSession,
) -> InlineKeyboardMarkup:
    ranges = session.search_page_ranges or _search_page_ranges(session)
    page = min(max(0, session.search_page), len(ranges) - 1)
    start, end = ranges[page]
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{_search_scope_marker(session, scope)}{label}",
                callback_data=_callback(
                    session,
                    "srs",
                    f"{session.search_generation}-{value}",
                ),
            )
            for value, scope, label in (
                ("all", "bible", "All"),
                ("ot", "old_testament", "Old"),
                ("nt", "new_testament", "New"),
                ("dc", "deuterocanon", "Other"),
            )
        ]
    ]
    for index in range(start, end):
        item = session.search_results[index]
        rows.append(
            [
                InlineKeyboardButton(
                    _search_result_button_label(session, index, item),
                    callback_data=_callback(
                        session,
                        "srt",
                        f"{session.search_generation}-{index}",
                    ),
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "‹ Previous",
                callback_data=_callback(
                    session,
                    "srp",
                    f"{session.search_generation}-{page - 1}",
                ),
            )
        )
    if page + 1 < len(ranges):
        navigation.append(
            InlineKeyboardButton(
                "Next ›",
                callback_data=_callback(
                    session,
                    "srp",
                    f"{session.search_generation}-{page + 1}",
                ),
            )
        )
    if navigation:
        rows.append(navigation)
    if session.search_results:
        rows.append(
            [
                InlineKeyboardButton(
                    f"Post selected ({len(session.selected)})",
                    callback_data=_callback(
                        session,
                        "spost",
                        str(session.search_generation),
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "New search",
                callback_data=_callback(session, "snew"),
            ),
            InlineKeyboardButton(
                "Filters",
                callback_data=_callback(session, "sdash"),
            ),
        ]
    )
    rows.append(
        [InlineKeyboardButton("Cancel", callback_data=_callback(session, "cancel"))]
    )
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


def _available_translation(
    translations: tuple[TranslationOption, ...],
    preferred: str,
    application_default: str,
) -> str:
    available = {item.code for item in translations}
    for candidate in (preferred, application_default):
        if candidate in available:
            return candidate
    if not translations:
        raise RobotInputError("No safe Bible translations are available.")
    return translations[0].code


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


def _search_result_scope(value: str) -> str:
    scopes = {
        "all": "bible",
        "ot": "old_testament",
        "nt": "new_testament",
        "dc": "deuterocanon",
    }
    try:
        return scopes[value]
    except KeyError as error:
        raise RobotInputError("Invalid search scope selection.") from error


def _search_scope_label(value: str) -> str:
    labels = {
        "bible": "All Scripture",
        "old_testament": "Old Testament",
        "new_testament": "New Testament",
        "deuterocanon": "Other books",
    }
    return labels.get(value, _display(value))


def _search_scope_marker(session: InteractionSession, scope: str) -> str:
    if session.search_options.books:
        return ""
    return "✓ " if session.search_options.scope == scope else ""


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


def _range_ending_verses(session: InteractionSession) -> tuple[int, ...]:
    start = _required_start(session)
    return tuple(verse for verse in _session_verses(session) if verse > start)


def _reference_selection(session: InteractionSession) -> str:
    book = _required_book(session)
    chapter = _required_chapter(session)
    start = _required_start(session)
    end = session.end_verse
    if end is None or end < start:
        raise RobotInputError("Choose an ending verse.")
    verses = str(start) if start == end else f"{start}-{end}"
    return f"{book.name} {chapter.number}:{verses}"


def _add_reference_selection(
    session: InteractionSession,
    start: int,
    end: int,
    settings: Settings,
) -> None:
    book = _required_book(session)
    chapter = _required_chapter(session)
    if end < start:
        raise RobotInputError("The end of a range cannot precede its first verse.")
    candidate = ReferenceSelection(
        book_number=book.number,
        book_name=book.name,
        chapter=chapter.number,
        start_verse=start,
        end_verse=end,
    )
    session.start_verse = start
    session.end_verse = end
    appended = candidate not in session.reference_selections
    if appended:
        session.reference_selections.append(candidate)
    try:
        groups = _normalized_reference_groups(session.reference_selections)
        if len(groups) > settings.max_references:
            raise RequestLimitError(
                f"A selection cannot contain more than "
                f"{settings.max_references} reference groups."
            )
        verse_limit = getattr(
            settings,
            "max_verses_per_reference",
            settings.max_total_verses,
        )
        for intervals in groups.values():
            group_total = sum(
                end_value - start_value + 1
                for start_value, end_value in intervals
            )
            if group_total > verse_limit:
                raise RequestLimitError(
                    "One reference group exceeds the safe verse limit."
                )
        total = sum(
            end_value - start_value + 1
            for intervals in groups.values()
            for start_value, end_value in intervals
        )
        if total > settings.max_total_verses:
            raise RequestLimitError(
                f"A selection cannot contain more than "
                f"{settings.max_total_verses} verses."
            )
    except Exception:
        if appended:
            session.reference_selections.pop()
        raise


def _normalized_reference_groups(
    selections: Sequence[ReferenceSelection],
) -> OrderedDict[tuple[int, str, int], tuple[tuple[int, int], ...]]:
    grouped: OrderedDict[tuple[int, str, int], list[tuple[int, int]]] = OrderedDict()
    for selection in selections:
        key = (
            selection.book_number,
            selection.book_name,
            selection.chapter,
        )
        grouped.setdefault(key, []).append(
            (selection.start_verse, selection.end_verse)
        )

    normalized: OrderedDict[
        tuple[int, str, int],
        tuple[tuple[int, int], ...],
    ] = OrderedDict()
    for key, intervals in grouped.items():
        merged: list[tuple[int, int]] = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        normalized[key] = tuple(merged)
    return normalized


def _reference_selection_label(selection: ReferenceSelection) -> str:
    verses = (
        str(selection.start_verse)
        if selection.start_verse == selection.end_verse
        else f"{selection.start_verse}-{selection.end_verse}"
    )
    return f"{selection.book_name} {selection.chapter}:{verses}"


def _reference_basket_reference(session: InteractionSession) -> str:
    if not session.reference_selections:
        raise RobotInputError("Select at least one verse before posting.")
    references: list[str] = []
    for (_, book_name, chapter), intervals in _normalized_reference_groups(
        session.reference_selections
    ).items():
        verses = ",".join(
            str(start) if start == end else f"{start}-{end}"
            for start, end in intervals
        )
        references.append(f"{book_name} {chapter}:{verses}")
    return ";".join(references)


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


def _search_result_button_label(
    session: InteractionSession,
    index: int,
    result: SearchResult,
) -> str:
    terms = result.terms or _search_query_terms(session.search_query)
    verse = _highlight_search_terms_plain(
        result.text,
        terms,
        session.search_options,
    )
    marker = "☑" if index in session.selected else "☐"
    return f"{marker} {index + 1}. {result.reference}\n{verse}"


def _search_query_terms(query: str) -> tuple[str, ...]:
    """Extract bounded word-like terms when an older search result lacks metadata."""
    return tuple(
        dict.fromkeys(
            re.findall(r"[^\W_]+(?:['’][^\W_]+)*", query, flags=re.UNICODE)
        )
    )[:64]


def _highlight_search_terms(
    text: str,
    terms: Sequence[str],
    options: SearchOptions,
) -> str:
    """Escape one verse and bold every matching word without changing its text."""
    spans = _search_match_spans(text, terms, options)
    if not spans:
        return html.escape(text)

    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(html.escape(text[cursor:start]))
        parts.append(f"<b>{html.escape(text[start:end])}</b>")
        cursor = end
    parts.append(html.escape(text[cursor:]))
    return "".join(parts)


def _highlight_search_terms_plain(
    text: str,
    terms: Sequence[str],
    options: SearchOptions,
) -> str:
    """Mark search matches visibly inside an unformatted Telegram button label."""
    spans = _search_match_spans(text, terms, options)
    if not spans:
        return text

    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        parts.append(f"【{text[start:end]}】")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _search_match_spans(
    text: str,
    terms: Sequence[str],
    options: SearchOptions,
) -> tuple[tuple[int, int], ...]:
    """Locate merged match spans while retaining original Unicode positions."""
    normalized, positions = _normalized_search_value(text, options)
    if not normalized or not positions:
        return ()

    spans: list[tuple[int, int]] = []
    for term in terms[:64]:
        needle, _ = _normalized_search_value(term.strip(), options)
        if not needle:
            continue
        offset = 0
        while True:
            match_at = normalized.find(needle, offset)
            if match_at < 0:
                break
            match_end = match_at + len(needle)
            offset = max(match_end, match_at + 1)
            if options.match == "whole_word" and (
                (match_at > 0 and _is_search_word_char(normalized[match_at - 1]))
                or (
                    match_end < len(normalized)
                    and _is_search_word_char(normalized[match_end])
                )
            ):
                continue

            original_start = positions[match_at]
            original_end = positions[match_end - 1] + 1
            if options.match == "substring":
                while original_start > 0 and _is_search_word_char(text[original_start - 1]):
                    original_start -= 1
                while original_end < len(text) and _is_search_word_char(text[original_end]):
                    original_end += 1
            spans.append((original_start, original_end))

    if not spans:
        return ()
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _normalized_search_value(
    value: str,
    options: SearchOptions,
) -> tuple[str, tuple[int, ...]]:
    """Normalize search text while retaining positions in the original value."""
    characters: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(value):
        normalized = (
            unicodedata.normalize("NFKD", character)
            if options.diacritics == "insensitive"
            else character
        )
        if options.diacritics == "insensitive":
            normalized = "".join(
                item for item in normalized if not unicodedata.category(item).startswith("M")
            )
        if not options.case_sensitive:
            normalized = normalized.casefold()
        for item in normalized:
            characters.append(item)
            positions.append(index)
    return "".join(characters), tuple(positions)


def _contains_unsegmented_script(value: str) -> bool:
    """Detect scripts where whitespace-delimited whole-word matching is unsafe."""
    return any(
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0x20000 <= codepoint <= 0x323AF
        for codepoint in map(ord, value)
    )


def _is_search_word_char(value: str) -> bool:
    return bool(value) and (
        unicodedata.category(value).startswith(("L", "M", "N"))
        or value in {"'", "’"}
    )


def _display(value: str) -> str:
    return value.replace("_", " ").title()


def _require_kind(session: InteractionSession, kind: str) -> None:
    if session.kind != kind:
        raise RobotInputError("This control belongs to another workflow.")
