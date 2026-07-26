"""GetBible Telegram robot entry point."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

from telegram import BotCommand
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
from modules.errors import ScriptureUnavailable
from modules.health import HealthServer
from modules.interactions import InteractionStore
from modules.rate_limit import InboundRateLimiter
from modules.service import ScriptureService

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
    application.bot_data[HEALTH_SLOT] = health

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("get", bible_command))
    application.add_handler(CommandHandler("getbible", bible_command))
    application.add_handler(CommandHandler("bible", bible_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(interaction_callback, pattern=r"^gb:"))
    application.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, interaction_reply)
    )
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(error_handler)
    return application


async def _post_init(application: Application) -> None:
    health: HealthServer = application.bot_data[HEALTH_SLOT]
    service: ScriptureService = application.bot_data[SERVICE_SLOT]
    settings: Settings = application.bot_data[SETTINGS_SLOT]
    await application.bot.set_my_commands(
        [
            BotCommand("bible", "Retrieve Scripture by reference"),
            BotCommand("search", "Search and select Scripture"),
            BotCommand("help", "Show detailed usage guidance"),
        ]
    )
    await application.bot.set_my_name(settings.bot_name)
    await application.bot.set_my_description(settings.bot_description)
    await application.bot.set_my_short_description(settings.bot_short_description)
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
    await health.close()
    await service.close()
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
    application = build_application(settings)
    run_application(application, settings)
    return 75 if application.bot_data.get(DUPLICATE_POLLER_SLOT) else 0


if __name__ == "__main__":
    sys.exit(main())
