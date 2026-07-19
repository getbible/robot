"""GetBible Telegram robot entry point."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from telegram import BotCommand
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import ConfigurationError, Settings
from modules.commands import (
    LIMITER_SLOT,
    SERVICE_SLOT,
    SETTINGS_SLOT,
    bible_command,
    error_handler,
    help_command,
    search_command,
    start_command,
    unknown_command,
)
from modules.health import HealthServer
from modules.rate_limit import InboundRateLimiter
from modules.service import ScriptureService

HEALTH_SLOT = "health_server"
LOGGER = logging.getLogger(__name__)


class JsonFormatter(logging.Formatter):
    """One JSON object per line without recording Telegram message content."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: int) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
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
    )
    health = HealthServer(
        host=settings.health_host,
        port=settings.health_port,
        service=service,
        limiter=limiter,
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
    application.bot_data[HEALTH_SLOT] = health

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("get", bible_command))
    application.add_handler(CommandHandler("getbible", bible_command))
    application.add_handler(CommandHandler("bible", bible_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(error_handler)
    return application


async def _post_init(application: Application) -> None:
    health: HealthServer = application.bot_data[HEALTH_SLOT]
    await application.bot.set_my_commands(
        [
            BotCommand("bible", "Retrieve Scripture by reference"),
            BotCommand("search", "Open Scripture search"),
            BotCommand("help", "Show available commands"),
        ]
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


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        logging.basicConfig(level=logging.CRITICAL)
        logging.critical("Configuration error: %s", error)
        return 2

    configure_logging(settings.log_level)
    application = build_application(settings)
    application.run_polling(
        allowed_updates=["message"],
        drop_pending_updates=settings.drop_pending_updates,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
