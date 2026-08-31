"""GetBible Telegram robot entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
from collections.abc import Awaitable
from contextlib import suppress
from datetime import datetime, timezone
from urllib.parse import urlsplit

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    MenuButtonCommands,
    MenuButtonWebApp,
    WebAppInfo,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import ConfigurationError, Settings
from modules.bookmark_backup import (
    BOOKMARK_RESTORE_CALLBACK_PREFIX,
    MAX_BOOKMARK_BACKUP_BYTES,
    BookmarkBackupDocument,
    BookmarkBackupUnavailable,
    BookmarkRestoreFile,
    bookmark_restore_callback_data,
)
from modules.cache_maintenance import CacheJanitor
from modules.commands import (
    APPLICATION_SERVICES_SLOT,
    DUPLICATE_POLLER_SLOT,
    INTERACTIONS_SLOT,
    LIMITER_SLOT,
    MINI_APP_SLOT,
    PREFERENCES_SLOT,
    SERVICE_SLOT,
    SETTINGS_SLOT,
    bible_command,
    bookmark_restore_callback,
    error_handler,
    help_command,
    interaction_callback,
    interaction_reply,
    search_command,
    start_command,
    unknown_command,
)
from modules.contributions import ContributionStore
from modules.contributor_command import (
    CONTRIBUTION_STORE_SLOT,
    contributor_command,
)
from modules.dependencies import ApplicationServices
from modules.ephemeral import delete_ephemeral_text, send_ephemeral_text
from modules.errors import ScriptureUnavailable
from modules.health import HealthServer
from modules.interactions import InteractionStore
from modules.miniapp_sessions import MiniAppLaunch
from modules.miniapp_tornado import MiniAppServer
from modules.posting import post_scripture_queries
from modules.preferences import UserPreferenceStore
from modules.rate_limit import InboundRateLimiter
from modules.runtime_notify import RuntimeNotifier
from modules.service import ScriptureQuery, ScriptureService

HEALTH_SLOT = "health_server"
CACHE_JANITOR_SLOT = "cache_janitor"
NOTIFIER_SLOT = "runtime_notifier"
PREWARM_SLOT = "prewarm_task"
CONTRIBUTION_NOTIFICATION_TASK_SLOT = "contribution_notification_task"
LOGGER = logging.getLogger(__name__)
ALLOWED_UPDATES = ("message", "callback_query")


class JsonFormatter(logging.Formatter):
    """One instance-tagged JSON object per line with controlled audit fields."""

    def __init__(self, instance_name: str) -> None:
        super().__init__()
        self.instance_name = instance_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "instance": self.instance_name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        audit = getattr(record, "audit", None)
        if isinstance(event, str):
            payload["event"] = event
        if isinstance(audit, dict):
            payload["audit"] = audit
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class BoundedFileHandler(logging.FileHandler):
    """Keep an optional JSONL file under a strict byte ceiling.

    Journald or the container log remains the durable history. The local file
    is truncated in place so a restricted service account never needs write
    access to the parent log directory for renames.
    """

    def __init__(self, filename: str, *, max_bytes: int) -> None:
        super().__init__(filename, encoding="utf-8")
        self._max_bytes = max_bytes

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.stream is None:
                self.stream = self._open()
            message = self.format(record)
            encoded_size = len((message + self.terminator).encode("utf-8"))
            if encoded_size > self._max_bytes:
                message = (
                    '{"level":"ERROR","message":'
                    '"one log record exceeded LOG_MAX_BYTES and was discarded"}'
                )
                encoded_size = len((message + self.terminator).encode("utf-8"))
            descriptor = self.stream.fileno()
            if os.fstat(descriptor).st_size + encoded_size > self._max_bytes:
                self.stream.seek(0)
                self.stream.truncate(0)
            self.stream.write(message + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


def configure_logging(
    level: int,
    *,
    instance_name: str,
    log_file: str | None,
    log_max_bytes: int = 10 * 1024 * 1024,
) -> None:
    formatter = JsonFormatter(instance_name)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    previous_handlers = tuple(root.handlers)
    root.handlers.clear()
    for handler in previous_handlers:
        handler.close()
    root.addHandler(stream_handler)

    if log_file is not None:
        file_handler = BoundedFileHandler(
            log_file,
            max_bytes=log_max_bytes,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def _build_preference_store(settings: Settings) -> UserPreferenceStore:
    """Keep the core bot available when optional durable preferences cannot open."""
    try:
        return UserPreferenceStore(
            path=settings.user_preferences_file,
            default_translation=settings.default_translation,
            max_users=settings.user_preference_limit,
        )
    except (OSError, sqlite3.Error) as error:
        LOGGER.error(
            "Persistent user preferences are unavailable; using memory-only "
            "preferences for this process (%s)",
            type(error).__name__,
            exc_info=True,
        )
        return UserPreferenceStore(
            path=None,
            default_translation=settings.default_translation,
            max_users=settings.user_preference_limit,
        )


def _build_contribution_store(settings: Settings) -> ContributionStore | None:
    """Open the private moderation database only when durable storage is configured."""
    path = getattr(settings, "contribution_store_file", None)
    if not isinstance(path, str) or not path:
        return None
    try:
        return ContributionStore(
            path=path,
            max_contributors=getattr(
                settings,
                "contribution_contributor_limit",
                10_000,
            ),
            max_events=getattr(settings, "contribution_event_limit", 250_000),
        )
    except (OSError, sqlite3.Error) as error:
        LOGGER.critical(
            "Configured contributor storage is unavailable; refusing to start "
            "with contribution authority detached from its database (%s)",
            type(error).__name__,
            exc_info=True,
        )
        raise RuntimeError("Configured contributor storage is unavailable.") from error


def build_application(settings: Settings) -> Application:
    service = ScriptureService(settings)
    limiter = InboundRateLimiter(
        user_capacity=settings.user_rate_capacity,
        user_refill_per_second=settings.user_rate_refill_per_second,
        chat_capacity=settings.chat_rate_capacity,
        chat_refill_per_second=settings.chat_rate_refill_per_second,
        max_entries=settings.rate_limit_cache_size,
        notification_cooldown=settings.rate_limit_notice_cooldown,
        client_capacity=settings.mini_app_ip_rate_capacity,
        client_refill_per_second=settings.mini_app_ip_rate_refill_per_second,
        abuse_rejection_threshold=settings.abuse_rejection_threshold,
        abuse_window_seconds=settings.abuse_window_seconds,
        abuse_block_seconds=settings.abuse_block_seconds,
    )
    interactions = InteractionStore(
        max_sessions=settings.interaction_session_limit,
        ttl_seconds=settings.interaction_ttl_seconds,
    )
    preferences = _build_preference_store(settings)
    contributions = _build_contribution_store(settings)
    health = HealthServer(
        host=settings.health_host,
        port=settings.health_port,
        service=service,
        limiter=limiter,
        interactions=interactions,
    )

    application = (
        ApplicationBuilder()
        .token(settings.telegram_api_token)
        .concurrent_updates(settings.max_concurrent_updates)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data[SETTINGS_SLOT] = settings
    application.bot_data[SERVICE_SLOT] = service
    application.bot_data[LIMITER_SLOT] = limiter
    application.bot_data[INTERACTIONS_SLOT] = interactions
    application.bot_data[PREFERENCES_SLOT] = preferences
    if contributions is not None:
        application.bot_data[CONTRIBUTION_STORE_SLOT] = contributions
    application.bot_data[HEALTH_SLOT] = health
    application.bot_data[CACHE_JANITOR_SLOT] = CacheJanitor(
        max_bytes=settings.cache_max_bytes,
        interval_seconds=settings.cache_maintenance_interval_seconds,
    )
    application.bot_data[NOTIFIER_SLOT] = RuntimeNotifier()
    mini_app: MiniAppServer | None = None
    if getattr(settings, "mini_app_enabled", False):
        async def cleanup_mini_app_launch(launch: MiniAppLaunch) -> None:
            await _cleanup_mini_app_launch_prompt(application.bot, launch)

        async def post_mini_app_scripture(
            launch: MiniAppLaunch,
            queries: tuple[ScriptureQuery, ...],
        ) -> tuple[int, ...]:
            message_ids = await post_scripture_queries(
                bot=application.bot,
                chat_id=launch.target_chat_id,
                queries=queries,
                settings=settings,
                service=service,
                source="mini_app",
                message_thread_id=launch.message_thread_id,
                max_messages=settings.max_output_chunks,
            )
            await cleanup_mini_app_launch(launch)
            return message_ids

        async def send_abuse_warning(
            user_id: int,
            chat_id: int,
            text: str,
        ) -> None:
            if chat_id != user_id:
                try:
                    await send_ephemeral_text(
                        application.bot,
                        chat_id=chat_id,
                        receiver_user_id=user_id,
                        text=text,
                    )
                    return
                except TelegramError:
                    LOGGER.warning(
                        "Unable to deliver an ephemeral Mini App abuse warning; "
                        "trying the user's private chat"
                    )
            await application.bot.send_message(chat_id=user_id, text=text)

        async def send_bookmark_backup(
            user_id: int,
            document: BookmarkBackupDocument,
        ) -> int:
            try:
                message = await application.bot.send_document(
                    chat_id=user_id,
                    document=InputFile(
                        document.payload,
                        filename=document.filename,
                    ),
                    caption=(
                        "GetBible bookmark backup\n"
                        f"{document.bookmark_count} bookmarks · "
                        f"{document.topic_count} topics\n"
                        "Keep this message as your portable backup. Tap Restore "
                        "bookmarks to review and merge it on any Telegram client."
                    ),
                    disable_content_type_detection=True,
                    reply_markup=InlineKeyboardMarkup(
                        [[
                            InlineKeyboardButton(
                                "Restore bookmarks",
                                callback_data=bookmark_restore_callback_data(
                                    user_id,
                                    settings.telegram_api_token,
                                ),
                            )
                        ]]
                    ),
                )
            except TelegramError as error:
                raise BookmarkBackupUnavailable(
                    "Telegram could not store the bookmark backup."
                ) from error
            message_id = getattr(message, "message_id", None)
            if (
                isinstance(message_id, bool)
                or not isinstance(message_id, int)
                or message_id <= 0
            ):
                raise BookmarkBackupUnavailable(
                    "Telegram returned an invalid bookmark backup message."
                )
            return message_id

        async def load_bookmark_backup(restore: BookmarkRestoreFile) -> bytes:
            try:
                telegram_file = await application.bot.get_file(restore.file_id)
                remote_size = getattr(telegram_file, "file_size", None)
                remote_unique_id = getattr(telegram_file, "file_unique_id", None)
                if (
                    remote_unique_id != restore.file_unique_id
                    or
                    remote_size is not None
                    and (
                        isinstance(remote_size, bool)
                        or not isinstance(remote_size, int)
                        or remote_size != restore.file_size
                        or remote_size > MAX_BOOKMARK_BACKUP_BYTES
                    )
                ):
                    raise BookmarkBackupUnavailable(
                        "Telegram bookmark backup metadata changed."
                    )
                payload = bytes(await telegram_file.download_as_bytearray())
            except BookmarkBackupUnavailable:
                raise
            except TelegramError as error:
                raise BookmarkBackupUnavailable(
                    "Telegram could not retrieve the bookmark backup."
                ) from error
            if (
                len(payload) != restore.file_size
                or len(payload) > MAX_BOOKMARK_BACKUP_BYTES
            ):
                raise BookmarkBackupUnavailable(
                    "Telegram returned an invalid bookmark backup document."
                )
            return payload

        mini_app = MiniAppServer(
            settings=settings,
            service=service,
            preferences=preferences,
            limiter=limiter,
            post_scripture=post_mini_app_scripture,
            send_bookmark_backup=send_bookmark_backup,
            load_bookmark_backup=load_bookmark_backup,
            cleanup_launch=cleanup_mini_app_launch,
            abuse_warning=send_abuse_warning,
            contributions=contributions,
        )
        application.bot_data[MINI_APP_SLOT] = mini_app
        health.set_mini_app_snapshot(mini_app.snapshot)

    application.bot_data[APPLICATION_SERVICES_SLOT] = ApplicationServices(
        settings=settings,
        scripture=service,
        limiter=limiter,
        interactions=interactions,
        preferences=preferences,
        mini_app=mini_app,
        contributions=contributions,
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("get", bible_command))
    application.add_handler(CommandHandler("getbible", bible_command))
    application.add_handler(CommandHandler("bible", bible_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("help", help_command))
    # Intentionally absent from BotCommand metadata: enrolment is a private,
    # operator-directed workflow, not a public menu action.
    application.add_handler(CommandHandler("contributor", contributor_command))
    application.add_handler(
        CallbackQueryHandler(
            bookmark_restore_callback,
            pattern=rf"^{re.escape(BOOKMARK_RESTORE_CALLBACK_PREFIX)}",
        )
    )
    application.add_handler(CallbackQueryHandler(interaction_callback, pattern=r"^gb:"))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, interaction_reply)
    )
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(error_handler)
    return application


async def _cleanup_mini_app_launch_prompt(
    bot: Bot,
    launch: MiniAppLaunch,
) -> None:
    """Best-effort removal of both ephemeral rows that opened the Mini App."""
    if launch.prompt_ephemeral_message_id is not None:
        try:
            await delete_ephemeral_text(
                bot,
                chat_id=launch.target_chat_id,
                receiver_user_id=launch.user_id,
                ephemeral_message_id=launch.prompt_ephemeral_message_id,
            )
        except TelegramError:
            LOGGER.info(
                "Mini App launch response could not be deleted after a successful post"
            )
    elif launch.prompt_message_id is not None:
        delete_message = getattr(bot, "delete_message", None)
        if callable(delete_message):
            try:
                await delete_message(
                    chat_id=launch.target_chat_id,
                    message_id=launch.prompt_message_id,
                )
            except TelegramError:
                LOGGER.info(
                    "Mini App launch response could not be deleted after a successful post"
                )

    if (
        launch.source_ephemeral_message_id is not None
        and launch.source_ephemeral_receiver_user_id is not None
    ):
        try:
            await delete_ephemeral_text(
                bot,
                chat_id=launch.target_chat_id,
                receiver_user_id=launch.source_ephemeral_receiver_user_id,
                ephemeral_message_id=launch.source_ephemeral_message_id,
            )
        except TelegramError:
            LOGGER.info(
                "Mini App source command could not be deleted after a successful post"
            )


async def _optional_telegram_call(
    label: str,
    operation: Awaitable[object],
) -> bool:
    """Run non-essential Telegram metadata synchronization without killing polling."""
    try:
        await operation
    except Exception as error:
        LOGGER.warning(
            "Telegram %s synchronization failed; the robot will continue (%s)",
            label,
            type(error).__name__,
            exc_info=True,
        )
        return False
    return True


async def _synchronize_telegram_profile(
    application: Application,
    settings: Settings,
) -> None:
    """Synchronize optional command/profile metadata with a functional fallback."""
    ordinary_commands = [
        BotCommand("bible", "Retrieve Scripture by reference"),
        BotCommand("search", "Search and select Scripture"),
        BotCommand("help", "Show detailed usage guidance"),
    ]
    await _optional_telegram_call(
        "private command",
        application.bot.set_my_commands(
            ordinary_commands,
            scope=BotCommandScopeAllPrivateChats(),
        ),
    )

    ephemeral_registered = await _optional_telegram_call(
        "ephemeral group command",
        application.bot.set_my_commands(
            [
                BotCommand(
                    "bible",
                    "Retrieve Scripture by reference",
                    api_kwargs={"is_ephemeral": True},
                ),
                BotCommand(
                    "search",
                    "Search and select Scripture",
                    api_kwargs={"is_ephemeral": True},
                ),
                ordinary_commands[2],
            ],
            scope=BotCommandScopeAllGroupChats(),
        ),
    )
    if not ephemeral_registered:
        LOGGER.warning(
            "Ephemeral group commands are unavailable; registering ordinary "
            "group commands so the robot remains usable"
        )
        await _optional_telegram_call(
            "ordinary group command fallback",
            application.bot.set_my_commands(
                ordinary_commands,
                scope=BotCommandScopeAllGroupChats(),
            ),
        )

    await _optional_telegram_call(
        "display name",
        application.bot.set_my_name(settings.bot_name),
    )
    await _optional_telegram_call(
        "description",
        application.bot.set_my_description(settings.bot_description),
    )
    await _optional_telegram_call(
        "short description",
        application.bot.set_my_short_description(settings.bot_short_description),
    )
    mini_app_enabled = getattr(settings, "mini_app_enabled", False)
    mini_app_public_url = getattr(settings, "mini_app_public_url", None)
    if mini_app_enabled and isinstance(mini_app_public_url, str):
        menu_button: MenuButtonWebApp | MenuButtonCommands = MenuButtonWebApp(
            text="Open getBible.Life",
            web_app=WebAppInfo(url=f"{mini_app_public_url.rstrip('/')}/"),
        )
    else:
        menu_button = MenuButtonCommands()
    set_chat_menu_button = getattr(application.bot, "set_chat_menu_button", None)
    if callable(set_chat_menu_button):
        await _optional_telegram_call(
            "menu button",
            set_chat_menu_button(menu_button=menu_button),
        )


async def _post_init(application: Application) -> None:
    health: HealthServer = application.bot_data[HEALTH_SLOT]
    janitor: CacheJanitor = application.bot_data[CACHE_JANITOR_SLOT]
    notifier: RuntimeNotifier = application.bot_data[NOTIFIER_SLOT]
    service: ScriptureService = application.bot_data[SERVICE_SLOT]
    settings: Settings = application.bot_data[SETTINGS_SLOT]
    mini_app: MiniAppServer | None = application.bot_data.get(MINI_APP_SLOT)
    contributions: ContributionStore | None = application.bot_data.get(
        CONTRIBUTION_STORE_SLOT
    )
    # Liveness starts before network synchronization and corpus warming. The
    # readiness bit remains false until every required startup stage completes.
    await health.start()
    janitor.start()
    if mini_app is not None:
        await mini_app.start()
    if contributions is not None:
        application.bot_data[CONTRIBUTION_NOTIFICATION_TASK_SLOT] = (
            asyncio.create_task(
                _deliver_contribution_notifications(application, contributions),
                name="deliver-contribution-notifications",
            )
        )
    await _synchronize_telegram_profile(application, settings)
    # Readiness is not gated on the corpus. An index build is bounded by
    # SEARCH_INDEX_BUILD_SECONDS, which can exceed the unit's TimeoutStartSec,
    # and blocking READY=1 behind it would let a cold corpus on a slow host
    # register as a failed start on a service that is fine. Reference delivery,
    # navigation and the Mini App do not need the index at all, so the robot
    # reports ready and warms behind it. Search stays correct throughout: a
    # query arriving first simply waits on the same build.
    health.mark_ready()
    notifier.ready()
    if settings.prewarm_default_translation:
        application.bot_data[PREWARM_SLOT] = asyncio.create_task(
            _prewarm_default_translation(service, settings),
            name="prewarm-default-translation",
        )
    LOGGER.info("GetBible Robot initialized")


async def _deliver_contribution_notifications(
    application: Application,
    store: ContributionStore,
) -> None:
    """Drain decision notices with leases so CLI transactions never depend on Telegram."""
    while True:
        try:
            notifications = store.claim_notifications(limit=10, lease_seconds=60)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.warning(
                "Contributor notification outbox could not be read (%s)",
                type(error).__name__,
            )
            await asyncio.sleep(10)
            continue
        if not notifications:
            await asyncio.sleep(10)
            continue
        for notification in notifications:
            try:
                # Plain text only: moderator notes are deliberately absent and
                # no parse mode can turn stored text into Telegram markup.
                await application.bot.send_message(
                    chat_id=notification.contributor_id,
                    text=notification.message,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                try:
                    store.mark_notification_failed(
                        notification.id,
                        notification.claim_token,
                        type(error).__name__,
                    )
                except Exception as persistence_error:
                    LOGGER.warning(
                        "Contributor notification failure state could not be saved (%s)",
                        type(persistence_error).__name__,
                    )
                LOGGER.warning(
                    "Contributor decision notification delivery failed (%s)",
                    type(error).__name__,
                )
            else:
                try:
                    store.mark_notification_sent(
                        notification.id,
                        notification.claim_token,
                    )
                except Exception as error:
                    # The sending lease expires and safely retries. Duplicate
                    # decision notices are preferable to silently losing one.
                    LOGGER.warning(
                        "Contributor notification receipt could not be saved (%s)",
                        type(error).__name__,
                    )
        await asyncio.sleep(1)


async def _prewarm_default_translation(
    service: ScriptureService,
    settings: Settings,
) -> None:
    """Build the default corpus and index without holding up readiness."""
    try:
        metadata = await service.warm_default_translation()
    except ScriptureUnavailable as error:
        LOGGER.warning(
            "Default search corpus prewarm failed safely (%s)",
            type(error).__name__,
        )
    except asyncio.CancelledError:
        LOGGER.info("Default search corpus prewarm cancelled during shutdown")
        raise
    except Exception as error:  # never let a background task kill the process
        LOGGER.warning(
            "Default search corpus prewarm failed safely (%s)",
            type(error).__name__,
        )
    else:
        LOGGER.info(
            "Default search corpus ready (%s, %s verses)",
            metadata.get("abbreviation", settings.default_translation),
            metadata.get("verses", "unknown"),
        )


async def _post_shutdown(application: Application) -> None:
    health: HealthServer = application.bot_data[HEALTH_SLOT]
    janitor: CacheJanitor = application.bot_data[CACHE_JANITOR_SLOT]
    notifier: RuntimeNotifier = application.bot_data[NOTIFIER_SLOT]
    service: ScriptureService = application.bot_data[SERVICE_SLOT]
    preferences: UserPreferenceStore = application.bot_data[PREFERENCES_SLOT]
    mini_app: MiniAppServer | None = application.bot_data.get(MINI_APP_SLOT)
    contributions: ContributionStore | None = application.bot_data.get(
        CONTRIBUTION_STORE_SLOT
    )
    contribution_notifications: asyncio.Task[None] | None = application.bot_data.get(
        CONTRIBUTION_NOTIFICATION_TASK_SLOT
    )
    prewarm: asyncio.Task[None] | None = application.bot_data.get(PREWARM_SLOT)
    health.mark_not_ready()
    await notifier.stopping()
    if prewarm is not None and not prewarm.done():
        # The executor below refuses new work once closed, so a build still in
        # flight has to be released here rather than left to fail on shutdown.
        prewarm.cancel()
        with suppress(asyncio.CancelledError):
            await prewarm
    if contribution_notifications is not None and not contribution_notifications.done():
        contribution_notifications.cancel()
        with suppress(asyncio.CancelledError):
            await contribution_notifications
    if mini_app is not None:
        await mini_app.close()
    await health.close()
    await janitor.close()
    await service.close()
    preferences.close()
    if contributions is not None:
        contributions.close()
    LOGGER.info("GetBible Robot shut down cleanly")


def run_application(application: Application, settings: Settings) -> None:
    """Run exactly one configured Telegram delivery transport."""
    if settings.telegram_delivery_mode == "polling":
        application.run_polling(
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=settings.drop_pending_updates,
        )
        return

    if settings.webhook_public_url is None or settings.webhook_secret_token is None:
        raise ConfigurationError("Webhook delivery is missing validated configuration.")
    url_path = urlsplit(settings.webhook_public_url).path.lstrip("/")
    application.run_webhook(
        listen=settings.webhook_listen,
        port=settings.webhook_port,
        url_path=url_path,
        webhook_url=settings.webhook_public_url,
        ip_address=settings.webhook_ip_address,
        max_connections=settings.webhook_max_connections,
        secret_token=settings.webhook_secret_token,
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=settings.drop_pending_updates,
    )


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        logging.basicConfig(level=logging.CRITICAL)
        logging.critical("Configuration error: %s", error)
        return 2

    try:
        configure_logging(
            settings.log_level,
            instance_name=settings.instance_name,
            log_file=settings.log_file,
            log_max_bytes=settings.log_max_bytes,
        )
    except OSError as error:
        logging.critical(
            "Unable to initialize the configured log safely (%s).",
            type(error).__name__,
        )
        return 2

    try:
        application = build_application(settings)
        run_application(application, settings)
    except Exception:
        LOGGER.critical(
            "GetBible Robot terminated because of an unhandled startup or runtime failure",
            exc_info=True,
        )
        return 1
    return 75 if application.bot_data.get(DUPLICATE_POLLER_SLOT) else 0


if __name__ == "__main__":
    sys.exit(main())
