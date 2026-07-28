"""Privacy-controlled structured audit events for operator diagnostics."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import Mapping
from typing import Protocol

_FIELD_RE = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_EVENT_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_TEXT_LENGTH = 1024


class AuditSettings(Protocol):
    @property
    def audit_log_mode(self) -> str: ...

    @property
    def audit_identity_mode(self) -> str: ...

    @property
    def telegram_api_token(self) -> str: ...


AuditValue = str | int | float | bool | None


def audit_event(
    logger: logging.Logger,
    settings: AuditSettings,
    event: str,
    *,
    metadata: Mapping[str, AuditValue] | None = None,
    content: Mapping[str, AuditValue] | None = None,
    identity: Mapping[str, AuditValue] | None = None,
    level: int = logging.INFO,
) -> None:
    """Emit one safe audit record, adding user content only when explicitly enabled."""
    if _EVENT_RE.fullmatch(event) is None:
        raise ValueError("Invalid audit event name.")

    details: dict[str, AuditValue] = {}
    _merge_fields(details, metadata)
    _merge_fields(details, identity)
    if settings.audit_log_mode == "content":
        _merge_fields(details, content)

    if level == logging.INFO:
        logger.info(
            "Audit event: %s",
            event,
            extra={"event": event, "audit": details},
        )
    else:
        logger.log(
            level,
            "Audit event: %s",
            event,
            extra={"event": event, "audit": details},
        )


def audit_identity(
    settings: AuditSettings,
    *,
    user_id: int | None = None,
    chat_id: int | None = None,
    client_ip: str | None = None,
) -> dict[str, AuditValue]:
    """Return raw or stable pseudonymous identity fields for operator events."""
    mode = getattr(settings, "audit_identity_mode", "disabled")
    if mode == "disabled":
        return {}
    values: tuple[tuple[str, int | str | None], ...] = (
        ("user", user_id),
        ("chat", chat_id),
        ("client_ip", client_ip),
    )
    if mode == "raw":
        return {
            "telegram_user_id": user_id,
            "telegram_chat_id": chat_id,
            "client_ip": client_ip,
        }
    if mode != "pseudonymous":
        return {}
    token = getattr(settings, "telegram_api_token", "")
    if not isinstance(token, str) or not token:
        return {}
    output: dict[str, AuditValue] = {}
    for label, value in values:
        if value is None or value == "" or value == "unknown":
            continue
        digest = hmac.new(
            token.encode("utf-8"),
            f"{label}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()
        output[f"{label}_key"] = digest[:16]
    return output


def _merge_fields(
    target: dict[str, AuditValue],
    fields: Mapping[str, AuditValue] | None,
) -> None:
    if fields is None:
        return
    for key, value in fields.items():
        if _FIELD_RE.fullmatch(key) is None:
            raise ValueError("Invalid audit field name.")
        if isinstance(value, str):
            normalized = " ".join(value.split())
            target[key] = normalized[:_MAX_TEXT_LENGTH]
        elif value is None or isinstance(value, (bool, int, float)):
            target[key] = value
        else:
            raise TypeError(f"Unsupported audit field type for {key}.")
