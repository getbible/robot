"""Typed application dependencies shared by Telegram command handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from config import Settings

from .interactions import InteractionStore
from .preferences import UserPreferenceStore
from .rate_limit import InboundRateLimiter
from .service import ScriptureService

if TYPE_CHECKING:
    from .contributions import ContributionStore
    from .miniapp_tornado import MiniAppServer


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    """Explicit command-layer dependencies assembled by the composition root.

    Python Telegram Bot requires handlers to retain its ``(update, context)``
    signature.  The services object keeps the actual application dependencies
    typed and cohesive instead of making every handler know the individual
    ``bot_data`` storage keys.
    """

    settings: Settings
    scripture: ScriptureService
    limiter: InboundRateLimiter
    interactions: InteractionStore
    preferences: UserPreferenceStore
    mini_app: MiniAppServer | None = None
    contributions: ContributionStore | None = None
