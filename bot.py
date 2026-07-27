"""GetBible Telegram robot entry point."""

from __future__ import annotations

import json
import logging
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
from modules.ephemeral import delete_ephemeral_text
from modules.errors import ScriptureUnavailable
from modules.health import HealthServer
from modules.interactions import InteractionStore
from modules.miniapp_sessions import MiniAppLaunch
from modules.miniapp_tornado import MiniAppServer
from modules.posting import post_scripture_queries
from modules.preferences import UserPreferenceStore
from modules.rate_limit import InboundRateLimiter
from modules.service import ScriptureQuery, ScriptureService

HEALTH_SLOT = "health_server"
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


def configure_logging(
    level: int,
    *,
    instance_name: str,
    log_file: str | None,
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
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
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
    if getattr(settings, "mini_app_enabled", False):
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
            await _cleanup_mini_app_launch_prompt(application.bot, launch)
            return message_ids

        application.bot_data[MINI_APP_SLOT] = MiniAppServer(
            settings=settings,
            service=service,
            preferences=preferences,
            limiter=limiter,
            post_scripture=post_mini_app_scripture,
        )

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
    """Best-effort removal of the prompt that opened a completed Mini App."""
    try:
        if launch.prompt_ephemeral_message_id is not None:
            await delete_ephemeral_text(
                bot,
                chat_id=launch.target_chat_id,
                receiver_user_id=launch.user_id,
                ephemeral_message_id=launch.prompt_ephemeral_message_id,
            )
        elif launch.prompt_message_id is not None:
            delete_message = getattr(bot, "delete_message", None)
            if callable(delete_message):
                await delete_message(
                    chat_id=launch.target_chat_id,
                    message_id=launch.prompt_message_id,
                )
    except TelegramError:
        LOGGER.info(
            "Mini App launch prompt could not be deleted after a successful post"
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
    service: ScriptureService = application.bot_data[SERVICE_SLOT]
    settings: Settings = application.bot_data[SETTINGS_SLOT]
    mini_app: MiniAppServer | None = application.bot_data.get(MINI_APP_SLOT)
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
    # Readiness must not become true until Telegram initialization has succeeded.
    await health.start()
    LOGGER.info("GetBible Robot initialized")


async def _post_shutdown(application: Application) -> None:
    health: HealthServer = application.bot_data[HEALTH_SLOT]
    service: ScriptureService = application.bot_data[SERVICE_SLOT]
    preferences: UserPreferenceStore = application.bot_data[PREFERENCES_SLOT]
    mini_app: MiniAppServer | None = application.bot_data.get(MINI_APP_SLOT)
    if mini_app is not None:
        await mini_app.close()
    await health.close()
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
