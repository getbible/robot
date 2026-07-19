"""Application-level failures that are safe to classify for users."""

from __future__ import annotations


class RobotError(Exception):
    """Base class for expected robot failures."""


class RobotInputError(RobotError):
    """The caller supplied invalid or unsupported input."""


class RobotRateLimited(RobotError):
    """An inbound user or chat budget has been exhausted."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("Inbound rate limit exceeded.")
        self.retry_after = max(1, int(retry_after + 0.999))


class RobotBusy(RobotError):
    """The bounded lookup queue could not accept more work."""


class CircuitOpen(RobotError):
    """The upstream circuit breaker is temporarily open."""


class ScriptureUnavailable(RobotError):
    """The Scripture repository or worker failed safely."""
