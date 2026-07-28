"""GetBible Telegram robot entry point."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from collections.abc import Awaitable
from datetime import datetime, timezone
from urllib.parse import urlsplit

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
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
from modules.cache_maintenance import CacheJanitor
from modules.commands import (
    DUPLICATE_POLLER_SLOT,
    INTERACTIONS_SLOT,
    LIMITER_SLOT,
    MINI_APP_SLOT,
    PREFERENCES_SLOT,
    SERVICE_SLOT,
    SETTINGS_SLOT,
    bible_command,
    error_handler,
    help_command,
    interaction_callback,
    interaction_reply,
    search_command,
    start_command,
    unknown_command,
)
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
    application.bot_data[HEALTH_SLOT] = health
    application.bot_data[CACHE_JANITOR_SLOT] = CacheJanitor(
        max_bytes=settings.cache_max_bytes,
        interval_seconds=settings.cache_maintenance_interval_seconds,
    )
    application.bot_data[NOTIFIER_SLOT] = RuntimeNotifier()
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

        mini_app = MiniAppServer(
            settings=settings,
            service=service,
            preferences=preferences,
            limiter=limiter,
            post_scripture=post_mini_app_scripture,
            cleanup_launch=cleanup_mini_app_launch,
            abuse_warning=send_abuse_warning,
        )
        application.bot_data[MINI_APP_SLOT] = mini_app
        health.set_mini_app_snapshot(mini_app.snapshot)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("get", bible_command))
    application.add_handler(CommandHandler("getbible", bible_command))
    application.add_handler(CommandHandler("bible", bible_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("help", help_command))
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
    # Liveness starts before network synchronization and corpus warming. The
    # readiness bit remains false until every required startup stage completes.
    await health.start()
    janitor.start()
    if mini_app is not None:
        await mini_app.start()
    await _synchronize_telegram_profile(application, settings)
    if settings.prewarm_default_translation:
        try:
            metadata = await service.warm_default_translation()
        except ScriptureUnavailable as error:
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
    health.mark_ready()
    notifier.ready()
    LOGGER.info("GetBible Robot initialized")


async def _post_shutdown(application: Application) -> None:
    health: HealthServer = application.bot_data[HEALTH_SLOT]
    janitor: CacheJanitor = application.bot_data[CACHE_JANITOR_SLOT]
    notifier: RuntimeNotifier = application.bot_data[NOTIFIER_SLOT]
    service: ScriptureService = application.bot_data[SERVICE_SLOT]
    preferences: UserPreferenceStore = application.bot_data[PREFERENCES_SLOT]
    mini_app: MiniAppServer | None = application.bot_data.get(MINI_APP_SLOT)
    health.mark_not_ready()
    await notifier.stopping()
    if mini_app is not None:
        await mini_app.close()
    await health.close()
    await janitor.close()
    await service.close()
    preferences.close()
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
