"""Privacy-controlled structured audit events for operator diagnostics."""

from __future__ import annotations

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


AuditValue = str | int | float | bool | None


def audit_event(
    logger: logging.Logger,
    settings: AuditSettings,
    event: str,
    *,
    metadata: Mapping[str, AuditValue] | None = None,
    content: Mapping[str, AuditValue] | None = None,
) -> None:
    """Emit one safe audit record, adding user content only when explicitly enabled."""
    if _EVENT_RE.fullmatch(event) is None:
        raise ValueError("Invalid audit event name.")

    details: dict[str, AuditValue] = {}
    _merge_fields(details, metadata)
    if settings.audit_log_mode == "content":
        _merge_fields(details, content)

    logger.info(
        "Audit event: %s",
        event,
        extra={"event": event, "audit": details},
    )


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
