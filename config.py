"""Validated runtime configuration for the GetBible Telegram robot."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Raised when required or security-sensitive configuration is invalid."""


_TRANSLATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}\Z")
_INSTANCE_RE = re.compile(r"[a-z][a-z0-9-]{0,22}[a-z0-9]\Z")
_TELEGRAM_TOKEN_RE = re.compile(r"[0-9]{6,12}:[A-Za-z0-9_-]{30,64}\Z")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_AUDIT_LOG_MODES = frozenset({"metadata", "content"})
_AUDIT_IDENTITY_MODES = frozenset({"disabled", "pseudonymous", "raw"})
_DELIVERY_MODES = frozenset({"polling", "webhook"})
_WEBHOOK_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
_WEBHOOK_PATH_RE = re.compile(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")
_MINI_APP_PATH_RE = re.compile(r"(?:/[A-Za-z0-9_-]+)*\Z")
_TELEGRAM_WEBHOOK_PORTS = frozenset({80, 88, 443, 8443})
_WILDCARD_LISTENERS = frozenset(
    {str(IPv4Address(0)), str(IPv6Address(0))}
)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw or "")
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer.") from error
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw or "")
    except ValueError as error:
        raise ConfigurationError(f"{name} must be numeric.") from error
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = (_env(name, "true" if default else "false") or "").casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def _listener(name: str, default: str, *, containerized: bool) -> str:
    value = _env(name, default) or ""
    if value in _LOCAL_HOSTS:
        return value
    if containerized and value in _WILDCARD_LISTENERS:
        return value
    suffix = " or a wildcard container address" if containerized else ""
    raise ConfigurationError(
        f"{name} must be localhost or a loopback address{suffix}."
    )


def _secret(name: str) -> str | None:
    direct = _env(name)
    secret_file = _env(f"{name}_FILE", "") or ""
    if direct and secret_file:
        raise ConfigurationError(f"{name} and {name}_FILE cannot both be set.")
    if not secret_file:
        return direct
    path = Path(secret_file)
    if not path.is_absolute():
        raise ConfigurationError(f"{name}_FILE must be an absolute path.")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(f"{name}_FILE could not be read safely.") from error
    if not value:
        raise ConfigurationError(f"{name}_FILE is empty.")
    return value


def _base_url(name: str, default: str) -> str:
    raw = _env(name, default) or ""
    parts = urlsplit(raw)
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigurationError(
            f"{name} must be an absolute base URL without credentials, query, or fragment."
        )
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname.casefold() in _LOCAL_HOSTS
    ):
        raise ConfigurationError(f"{name} must use HTTPS (HTTP is allowed only for localhost).")
    if parts.path not in {"", "/"}:
        raise ConfigurationError(f"{name} must not contain a path.")
    return raw.rstrip("/")


def _message(name: str, default: str) -> str:
    file_value = _env(f"{name}_FILE", "") or ""
    if file_value:
        path = Path(file_value)
        if not path.is_absolute():
            raise ConfigurationError(f"{name}_FILE must be an absolute path.")
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise ConfigurationError(f"{name}_FILE could not be read safely.") from error
    else:
        value = _env(name, default) or ""
    value = value.replace(r"\n", "\n")
    if not value or len(value) > 4096:
        raise ConfigurationError(f"{name} must contain between 1 and 4096 characters.")
    return value


def _network_list(name: str, default: str) -> tuple[str, ...]:
    raw = _env(name, default) or ""
    if not raw:
        return ()
    values: list[str] = []
    for candidate in raw.split(","):
        candidate = candidate.strip()
        if not candidate:
            raise ConfigurationError(f"{name} contains an empty network.")
        try:
            network = ip_network(candidate, strict=False)
        except ValueError as error:
            raise ConfigurationError(
                f"{name} must contain comma-separated IPv4 or IPv6 CIDR networks."
            ) from error
        normalized = str(network)
        if normalized not in values:
            values.append(normalized)
    if len(values) > 32:
        raise ConfigurationError(f"{name} cannot contain more than 32 networks.")
    return tuple(values)


def _profile_text(name: str, default: str, maximum: int) -> str:
    value = _env(name, default) or ""
    value = value.replace(r"\n", "\n").strip()
    if not 1 <= len(value) <= maximum:
        raise ConfigurationError(f"{name} must contain between 1 and {maximum} characters.")
    return value


def _delivery_mode() -> str:
    value = (_env("TELEGRAM_DELIVERY_MODE", "polling") or "").casefold()
    if value not in _DELIVERY_MODES:
        raise ConfigurationError("TELEGRAM_DELIVERY_MODE must be polling or webhook.")
    return value


def _webhook_public_url(delivery_mode: str) -> str | None:
    raw = _env("TELEGRAM_WEBHOOK_PUBLIC_URL", "") or ""
    if not raw:
        if delivery_mode == "webhook":
            raise ConfigurationError(
                "TELEGRAM_WEBHOOK_PUBLIC_URL is required in webhook mode."
            )
        return None
    parts = urlsplit(raw)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or _WEBHOOK_PATH_RE.fullmatch(parts.path) is None
    ):
        raise ConfigurationError(
            "TELEGRAM_WEBHOOK_PUBLIC_URL must be an HTTPS URL with an alphanumeric "
            "private path and without credentials, query, or fragment."
        )
    try:
        port = parts.port
    except ValueError as error:
        raise ConfigurationError("TELEGRAM_WEBHOOK_PUBLIC_URL has an invalid port.") from error
    hostname = parts.hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ConfigurationError(
            "TELEGRAM_WEBHOOK_PUBLIC_URL must use a publicly reachable host."
        )
    try:
        address = ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ConfigurationError(
                "TELEGRAM_WEBHOOK_PUBLIC_URL must use a publicly routable address."
            )
    if port is not None and port not in _TELEGRAM_WEBHOOK_PORTS:
        raise ConfigurationError(
            "Telegram webhook URLs may explicitly use only ports 80, 88, 443, or 8443."
        )
    return raw.rstrip("/")


def _webhook_ip_address() -> str | None:
    raw = _env("TELEGRAM_WEBHOOK_IP_ADDRESS", "") or ""
    if not raw:
        return None
    try:
        address = ip_address(raw)
    except ValueError as error:
        raise ConfigurationError(
            "TELEGRAM_WEBHOOK_IP_ADDRESS must be a valid public IPv4 or IPv6 address."
        ) from error
    if not address.is_global:
        raise ConfigurationError("TELEGRAM_WEBHOOK_IP_ADDRESS must be publicly routable.")
    return str(address)


def _webhook_secret(delivery_mode: str) -> str | None:
    value = _secret("TELEGRAM_WEBHOOK_SECRET_TOKEN") or ""
    if not value:
        if delivery_mode == "webhook":
            raise ConfigurationError(
                "TELEGRAM_WEBHOOK_SECRET_TOKEN is required in webhook mode."
            )
        return None
    if _WEBHOOK_SECRET_RE.fullmatch(value) is None:
        raise ConfigurationError(
            "TELEGRAM_WEBHOOK_SECRET_TOKEN must contain 32-256 letters, numbers, "
            "underscores, or hyphens."
        )
    return value


def _mini_app_public_url(enabled: bool) -> str | None:
    raw = _env("MINI_APP_PUBLIC_URL", "") or ""
    if not raw:
        if enabled:
            raise ConfigurationError(
                "MINI_APP_PUBLIC_URL is required when MINI_APP_ENABLED is true."
            )
        return None
    parts = urlsplit(raw)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or _MINI_APP_PATH_RE.fullmatch(parts.path.rstrip("/")) is None
        or "//" in parts.path
    ):
        raise ConfigurationError(
            "MINI_APP_PUBLIC_URL must be an HTTPS URL with an optional "
            "alphanumeric path and without credentials, query, or fragment."
        )
    try:
        _ = parts.port
    except ValueError as error:
        raise ConfigurationError("MINI_APP_PUBLIC_URL has an invalid port.") from error
    hostname = parts.hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ConfigurationError("MINI_APP_PUBLIC_URL must use a publicly reachable host.")
    try:
        address = ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ConfigurationError(
                "MINI_APP_PUBLIC_URL must use a publicly routable address."
            )
    return raw.rstrip("/")


def _instance_name() -> str:
    value = _env("INSTANCE_NAME", "local") or ""
    if _INSTANCE_RE.fullmatch(value) is None or "--" in value:
        raise ConfigurationError(
            "INSTANCE_NAME must be 2-24 lowercase letters, numbers, or single hyphens."
        )
    return value


def _log_file() -> str | None:
    value = _env("LOG_FILE", "") or ""
    if not value:
        return None
    if len(value) > 4096 or "\x00" in value:
        raise ConfigurationError("LOG_FILE contains an invalid path.")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigurationError("LOG_FILE must be an absolute path or empty.")
    return str(path)


def _preferences_file() -> str | None:
    value = _env("USER_PREFERENCES_FILE", "") or ""
    if not value:
        return None
    if len(value) > 4096 or "\x00" in value:
        raise ConfigurationError("USER_PREFERENCES_FILE contains an invalid path.")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigurationError(
            "USER_PREFERENCES_FILE must be an absolute path or empty."
        )
    return str(path)


@dataclass(frozen=True, slots=True)
class Settings:
    """All settings are validated once before Telegram polling starts."""

    telegram_api_token: str
    telegram_delivery_mode: str
    webhook_public_url: str | None
    webhook_listen: str
    webhook_port: int
    webhook_secret_token: str | None
    webhook_ip_address: str | None
    webhook_max_connections: int
    bot_name: str
    bot_description: str
    bot_short_description: str
    default_translation: str
    user_preferences_file: str | None
    user_preference_limit: int
    api_base_url: str
    web_base_url: str
    welcome_message: str
    help_message: str
    connect_timeout: float
    read_timeout: float
    lookup_timeout: float
    queue_timeout: float
    request_retries: int
    max_response_bytes: int
    search_max_response_bytes: int
    reference_cache_limit: int
    books_cache_limit: int
    chapter_cache_limit: int
    search_corpus_limit: int
    translation_cache_limit: int
    cache_max_bytes: int
    cache_maintenance_interval_seconds: int
    max_input_length: int
    max_references: int
    max_verses_per_reference: int
    max_total_verses: int
    max_output_chunks: int
    search_result_limit: int
    search_deadline_seconds: float
    max_concurrent_lookups: int
    max_concurrent_searches: int
    max_concurrent_updates: int
    user_rate_capacity: int
    user_rate_refill_per_second: float
    chat_rate_capacity: int
    chat_rate_refill_per_second: float
    rate_limit_cache_size: int
    rate_limit_notice_cooldown: float
    abuse_rejection_threshold: int
    abuse_window_seconds: float
    abuse_block_seconds: float
    abuse_warning_message: str
    interaction_session_limit: int
    interaction_ttl_seconds: float
    catalog_cache_ttl_seconds: float
    circuit_failure_threshold: int
    circuit_recovery_seconds: float
    delete_command_messages: bool
    drop_pending_updates: bool
    prewarm_default_translation: bool
    health_host: str
    health_port: int
    instance_name: str
    log_file: str | None
    log_max_bytes: int
    audit_log_mode: str
    audit_identity_mode: str
    log_level: int
    containerized: bool = False
    mini_app_enabled: bool = False
    mini_app_public_url: str | None = None
    mini_app_listen: str = "127.0.0.1"
    mini_app_port: int = 9201
    mini_app_init_data_max_age_seconds: int = 300
    mini_app_launch_ttl_seconds: int = 300
    mini_app_session_ttl_seconds: int = 900
    mini_app_session_limit: int = 200
    mini_app_sessions_per_user: int = 2
    mini_app_max_searches_per_session: int = 2
    mini_app_max_available_selections: int = 256
    mini_app_max_selections: int = 100
    mini_app_body_timeout_seconds: float = 10.0
    mini_app_idle_timeout_seconds: float = 30.0
    mini_app_max_header_bytes: int = 16 * 1024
    mini_app_trusted_proxy_cidrs: tuple[str, ...] = (
        "127.0.0.1/32",
        "::1/128",
    )
    mini_app_ip_rate_capacity: int = 60
    mini_app_ip_rate_refill_per_second: float = 10.0
    mini_app_session_exchange_rate_capacity: int = 10
    mini_app_session_exchange_rate_refill_per_second: float = 0.2
    mini_app_navigation_rate_cost: float = 0.25
    mini_app_access_log: bool = True

    @classmethod
    def from_env(cls, *, load_environment_file: bool = True) -> Settings:
        if load_environment_file:
            load_dotenv(override=False)

        primary_token = _secret("TELEGRAM_API_TOKEN")
        legacy_token = _env("TELEGRAM_TOKEN")
        if primary_token and legacy_token and primary_token != legacy_token:
            raise ConfigurationError(
                "TELEGRAM_API_TOKEN and deprecated TELEGRAM_TOKEN disagree; keep only one value."
            )
        token = primary_token or legacy_token
        if not token or token == "replace-with-a-real-bot-token":
            raise ConfigurationError("TELEGRAM_API_TOKEN is required.")
        if _TELEGRAM_TOKEN_RE.fullmatch(token) is None:
            raise ConfigurationError("TELEGRAM_API_TOKEN has an invalid format.")

        translation = (_env("TRANSLATION", "kjv") or "").casefold()
        if _TRANSLATION_RE.fullmatch(translation) is None:
            raise ConfigurationError("TRANSLATION must be a valid GetBible abbreviation.")

        audit_log_mode = (_env("AUDIT_LOG_MODE", "metadata") or "").casefold()
        if audit_log_mode not in _AUDIT_LOG_MODES:
            raise ConfigurationError("AUDIT_LOG_MODE must be metadata or content.")
        audit_identity_mode = (
            _env("AUDIT_IDENTITY_MODE", "pseudonymous") or ""
        ).casefold()
        if audit_identity_mode not in _AUDIT_IDENTITY_MODES:
            raise ConfigurationError(
                "AUDIT_IDENTITY_MODE must be disabled, pseudonymous, or raw."
            )

        containerized = _boolean("CONTAINERIZED", False)
        delivery_mode = _delivery_mode()
        webhook_listen = _listener(
            "TELEGRAM_WEBHOOK_LISTEN",
            "127.0.0.1",
            containerized=containerized,
        )

        log_name = (_env("LOG_LEVEL", "INFO") or "").upper()
        log_level = getattr(logging, log_name, None)
        if not isinstance(log_level, int):
            raise ConfigurationError("LOG_LEVEL must be a standard Python logging level.")

        max_input_length = _integer("MAX_INPUT_LENGTH", 256, 32, 1024)
        max_references = _integer("MAX_REFERENCES", 8, 1, 16)
        max_verses_per_reference = _integer("MAX_VERSES_PER_REFERENCE", 100, 1, 200)
        max_total_verses = _integer("MAX_TOTAL_VERSES", 100, 1, 200)
        if max_total_verses < max_verses_per_reference:
            raise ConfigurationError(
                "MAX_TOTAL_VERSES cannot be lower than MAX_VERSES_PER_REFERENCE."
            )

        health_host = _listener(
            "HEALTH_HOST",
            "127.0.0.1",
            containerized=containerized,
        )

        mini_app_enabled = _boolean("MINI_APP_ENABLED", False)
        mini_app_listen = _listener(
            "MINI_APP_LISTEN",
            "127.0.0.1",
            containerized=containerized,
        )
        webhook_port = _integer("TELEGRAM_WEBHOOK_PORT", 9001, 1024, 65_535)
        health_port = _integer("HEALTH_PORT", 8081, 0, 65_535)
        mini_app_port = _integer("MINI_APP_PORT", 9201, 1024, 65_535)
        if mini_app_enabled:
            occupied = {port for port in (health_port,) if port != 0}
            if delivery_mode == "webhook":
                occupied.add(webhook_port)
            if mini_app_port in occupied:
                raise ConfigurationError(
                    "MINI_APP_PORT must differ from the enabled health and "
                    "Telegram webhook listener ports."
                )

        return cls(
            telegram_api_token=token,
            telegram_delivery_mode=delivery_mode,
            webhook_public_url=_webhook_public_url(delivery_mode),
            webhook_listen=webhook_listen,
            webhook_port=webhook_port,
            webhook_secret_token=_webhook_secret(delivery_mode),
            webhook_ip_address=_webhook_ip_address(),
            webhook_max_connections=_integer(
                "TELEGRAM_WEBHOOK_MAX_CONNECTIONS", 16, 1, 100
            ),
            bot_name=_profile_text("BOT_NAME", "GetBible Robot", 64),
            bot_description=_profile_text(
                "BOT_DESCRIPTION",
                "Read and search Scripture in Telegram with GetBible.",
                512,
            ),
            bot_short_description=_profile_text(
                "BOT_SHORT_DESCRIPTION",
                "Read and search Scripture with GetBible.",
                120,
            ),
            default_translation=translation,
            user_preferences_file=_preferences_file(),
            user_preference_limit=_integer(
                "USER_PREFERENCE_LIMIT",
                10_000,
                100,
                1_000_000,
            ),
            api_base_url=_base_url("GETBIBLE_API_BASE_URL", "https://api.getbible.net"),
            web_base_url=_base_url("GETBIBLE_WEB_BASE_URL", "https://getbible.life"),
            welcome_message=_message(
                "WELCOME_MESSAGE",
                "Welcome to the official getBible.net telegram bot.\n"
                "/help for more info.",
            ),
            help_message=_message(
                "HELP_MESSAGE",
                "Available commands:\n\n"
                "You can use a reference to get verses like:\n"
                "/bible 1 John 3:16\n"
                "/bible John 3:16-19;1 John 3:10-17\n"
                "/bible Gen 1:1-5 codex\n"
                "/bible Ps 1:1-5 aov\n\n"
                "/search grace — search and scroll through complete matching verses\n"
                "/search — configure search filters\n"
                "/help — get this help message again",
            ),
            connect_timeout=_number("GETBIBLE_CONNECT_TIMEOUT", 3.05, 0.1, 30.0),
            read_timeout=_number("GETBIBLE_READ_TIMEOUT", 6.0, 0.5, 60.0),
            lookup_timeout=_number("LOOKUP_TIMEOUT", 20.0, 1.0, 90.0),
            queue_timeout=_number("LOOKUP_QUEUE_TIMEOUT", 2.0, 0.1, 30.0),
            request_retries=_integer("GETBIBLE_REQUEST_RETRIES", 1, 0, 5),
            max_response_bytes=_integer(
                "GETBIBLE_MAX_RESPONSE_BYTES",
                40 * 1024 * 1024,
                1024,
                128 * 1024 * 1024,
            ),
            search_max_response_bytes=_integer(
                "SEARCH_MAX_RESPONSE_BYTES",
                4 * 1024 * 1024,
                64 * 1024,
                16 * 1024 * 1024,
            ),
            reference_cache_limit=_integer(
                "REFERENCE_CACHE_LIMIT", 1000, 100, 50_000
            ),
            books_cache_limit=_integer("BOOKS_CACHE_LIMIT", 16, 1, 1000),
            chapter_cache_limit=_integer(
                "CHAPTER_CACHE_LIMIT", 256, 16, 10_000
            ),
            search_corpus_limit=_integer("SEARCH_CORPUS_LIMIT", 1, 1, 4),
            translation_cache_limit=_integer(
                "TRANSLATION_CACHE_LIMIT", 1, 1, 8
            ),
            cache_max_bytes=_integer(
                "CACHE_MAX_BYTES",
                256 * 1024 * 1024,
                32 * 1024 * 1024,
                8 * 1024 * 1024 * 1024,
            ),
            cache_maintenance_interval_seconds=_integer(
                "CACHE_MAINTENANCE_INTERVAL_SECONDS",
                6 * 60 * 60,
                300,
                7 * 24 * 60 * 60,
            ),
            max_input_length=max_input_length,
            max_references=max_references,
            max_verses_per_reference=max_verses_per_reference,
            max_total_verses=max_total_verses,
            max_output_chunks=_integer("MAX_OUTPUT_CHUNKS", 8, 1, 32),
            search_result_limit=_integer("SEARCH_RESULT_LIMIT", 50, 1, 200),
            search_deadline_seconds=_number(
                "SEARCH_DEADLINE_SECONDS", 5.0, 0.1, 30.0
            ),
            max_concurrent_lookups=_integer("MAX_CONCURRENT_LOOKUPS", 2, 1, 32),
            max_concurrent_searches=_integer("MAX_CONCURRENT_SEARCHES", 1, 1, 8),
            max_concurrent_updates=_integer("MAX_CONCURRENT_UPDATES", 4, 1, 64),
            user_rate_capacity=_integer("USER_RATE_CAPACITY", 4, 1, 100),
            user_rate_refill_per_second=_number(
                "USER_RATE_REFILL_PER_SECOND", 0.2, 0.01, 100.0
            ),
            chat_rate_capacity=_integer("CHAT_RATE_CAPACITY", 20, 1, 500),
            chat_rate_refill_per_second=_number(
                "CHAT_RATE_REFILL_PER_SECOND", 1.0, 0.01, 500.0
            ),
            rate_limit_cache_size=_integer("RATE_LIMIT_CACHE_SIZE", 2000, 100, 100_000),
            rate_limit_notice_cooldown=_number(
                "RATE_LIMIT_NOTICE_COOLDOWN", 10.0, 1.0, 300.0
            ),
            abuse_rejection_threshold=_integer(
                "ABUSE_REJECTION_THRESHOLD", 6, 2, 100
            ),
            abuse_window_seconds=_number(
                "ABUSE_WINDOW_SECONDS", 60.0, 10.0, 3600.0
            ),
            abuse_block_seconds=_number(
                "ABUSE_BLOCK_SECONDS", 300.0, 10.0, 86_400.0
            ),
            abuse_warning_message=_message(
                "ABUSE_WARNING_MESSAGE",
                "Your requests have been paused because the bot received repeated "
                "requests too quickly. Please stop repeated or automated requests "
                "and try again later.",
            ),
            interaction_session_limit=_integer(
                "INTERACTION_SESSION_LIMIT", 200, 10, 20_000
            ),
            interaction_ttl_seconds=_number(
                "INTERACTION_TTL_SECONDS", 600.0, 60.0, 3600.0
            ),
            catalog_cache_ttl_seconds=_number(
                "CATALOG_CACHE_TTL_SECONDS", 3600.0, 60.0, 86_400.0
            ),
            circuit_failure_threshold=_integer("CIRCUIT_FAILURE_THRESHOLD", 5, 1, 50),
            circuit_recovery_seconds=_number(
                "CIRCUIT_RECOVERY_SECONDS", 30.0, 1.0, 3600.0
            ),
            delete_command_messages=_boolean("DELETE_COMMAND_MESSAGES", False),
            drop_pending_updates=_boolean("DROP_PENDING_UPDATES", True),
            prewarm_default_translation=_boolean("PREWARM_DEFAULT_TRANSLATION", True),
            health_host=health_host,
            health_port=health_port,
            instance_name=_instance_name(),
            log_file=_log_file(),
            log_max_bytes=_integer(
                "LOG_MAX_BYTES",
                10 * 1024 * 1024,
                1024 * 1024,
                1024 * 1024 * 1024,
            ),
            audit_log_mode=audit_log_mode,
            audit_identity_mode=audit_identity_mode,
            log_level=log_level,
            containerized=containerized,
            mini_app_enabled=mini_app_enabled,
            mini_app_public_url=_mini_app_public_url(mini_app_enabled),
            mini_app_listen=mini_app_listen,
            mini_app_port=mini_app_port,
            mini_app_init_data_max_age_seconds=_integer(
                "MINI_APP_INIT_DATA_MAX_AGE_SECONDS", 300, 30, 900
            ),
            mini_app_launch_ttl_seconds=_integer(
                "MINI_APP_LAUNCH_TTL_SECONDS", 300, 30, 900
            ),
            mini_app_session_ttl_seconds=_integer(
                "MINI_APP_SESSION_TTL_SECONDS", 900, 60, 3600
            ),
            mini_app_session_limit=_integer(
                "MINI_APP_SESSION_LIMIT", 200, 10, 20_000
            ),
            mini_app_sessions_per_user=_integer(
                "MINI_APP_SESSIONS_PER_USER", 2, 1, 10
            ),
            mini_app_max_searches_per_session=_integer(
                "MINI_APP_MAX_SEARCHES_PER_SESSION", 2, 1, 8
            ),
            mini_app_max_available_selections=_integer(
                "MINI_APP_MAX_AVAILABLE_SELECTIONS", 256, 250, 1000
            ),
            mini_app_max_selections=_integer(
                "MINI_APP_MAX_SELECTIONS", 100, 1, 200
            ),
            mini_app_body_timeout_seconds=_number(
                "MINI_APP_BODY_TIMEOUT_SECONDS", 10.0, 1.0, 60.0
            ),
            mini_app_idle_timeout_seconds=_number(
                "MINI_APP_IDLE_TIMEOUT_SECONDS", 30.0, 5.0, 300.0
            ),
            mini_app_max_header_bytes=_integer(
                "MINI_APP_MAX_HEADER_BYTES", 16 * 1024, 4096, 64 * 1024
            ),
            mini_app_trusted_proxy_cidrs=_network_list(
                "MINI_APP_TRUSTED_PROXY_CIDRS",
                "127.0.0.1/32,::1/128",
            ),
            mini_app_ip_rate_capacity=_integer(
                "MINI_APP_IP_RATE_CAPACITY", 60, 10, 10_000
            ),
            mini_app_ip_rate_refill_per_second=_number(
                "MINI_APP_IP_RATE_REFILL_PER_SECOND",
                10.0,
                0.1,
                10_000.0,
            ),
            mini_app_session_exchange_rate_capacity=_integer(
                "MINI_APP_SESSION_EXCHANGE_RATE_CAPACITY",
                10,
                1,
                10_000,
            ),
            mini_app_session_exchange_rate_refill_per_second=_number(
                "MINI_APP_SESSION_EXCHANGE_RATE_REFILL_PER_SECOND",
                0.2,
                0.01,
                10_000.0,
            ),
            mini_app_navigation_rate_cost=_number(
                "MINI_APP_NAVIGATION_RATE_COST",
                0.25,
                0.05,
                1.0,
            ),
            mini_app_access_log=_boolean("MINI_APP_ACCESS_LOG", True),
        )
