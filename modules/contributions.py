"""Durable moderation queue and live catalogue overlay for trusted contributors.

The browser never grants contribution authority.  Every write reaches this
store only after the Mini App API has revalidated Telegram's signed numeric
user ID and has checked the current application state.  Contributor identity
stays in the private SQLite audit trail; published catalogue documents are
strictly privacy-safe and deterministic.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

ApplicationState = Literal[
    "pending",
    "approved",
    "rejected",
    "deferred",
    "revoked",
]
ReviewState = Literal["pending", "approved", "rejected", "deferred", "applied"]
TopicState = Literal["pending", "mapped", "rejected", "deferred"]
EventType = Literal["topic_upsert", "topic_delete", "verse_add", "verse_remove"]

APPLICATION_STATES = frozenset({"pending", "approved", "rejected", "deferred", "revoked"})
REVIEW_STATES = frozenset({"pending", "approved", "rejected", "deferred", "applied"})
TOPIC_STATES = frozenset({"pending", "mapped", "rejected", "deferred"})
EVENT_TYPES = frozenset({"topic_upsert", "topic_delete", "verse_add", "verse_remove"})

MAX_CONTRIBUTION_BATCH = 50
MAX_CLIENT_EVENT_ID = 128
MAX_LOCAL_TOPIC_ID = 128
MAX_TOPIC_NAME = 80
MAX_TOPIC_ALIASES = 20
MAX_ALIAS_LENGTH = 80
MAX_ACTOR_LENGTH = 128
MAX_DECISION_NOTE = 1000
MAX_NOTIFICATION_TEXT = 2000
MAX_TELEGRAM_ID = (1 << 52) - 1
MAX_CONTRIBUTION_STATUS_TOPICS = 1000
MAX_PUBLIC_OVERLAY_TOPICS = 39
MAX_PUBLIC_OVERLAY_ASSOCIATIONS = 10_000
MAX_PUBLIC_OVERLAY_BYTES = 2 * 1024 * 1024
MAX_ACTIVE_CONTRIBUTOR_CAPABILITIES = 16
CONTRIBUTOR_CAPABILITY_TTL_SECONDS = 90 * 24 * 60 * 60
DATABASE_SCHEMA_VERSION = 5

# The browser contribution journal accepts this complete separator-safe
# alphabet in every position. Personal bookmark IDs are a subset, but imported
# IDs may begin with ``_`` or ``-``. Keep the server contract identical so a
# locally valid topic cannot become unsynchronizable after its owner is approved.
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_CANONICAL_TOPIC_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}\Z")
_CHECKSUM_RE = re.compile(r"[0-9a-f]{64}\Z")
_CONTRIBUTOR_CAPABILITY_RE = re.compile(r"gbc_[A-Za-z0-9_-]{43}\Z")
_ENGLISH_TOPIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 &'():?-]*[A-Za-z0-9)]\Z")
# Protestant canon order used by the bundled global bookmark catalogue and its
# repository CSV. Contributions outside this catalogue are rejected before
# queueing, even when a selected reader translation exposes additional books.
BOOK_CHAPTER_COUNTS = (
    50,
    40,
    27,
    36,
    34,
    24,
    21,
    4,
    31,
    24,
    22,
    25,
    29,
    36,
    10,
    13,
    10,
    42,
    150,
    31,
    12,
    8,
    66,
    52,
    5,
    48,
    12,
    14,
    3,
    9,
    1,
    4,
    7,
    3,
    3,
    3,
    2,
    14,
    4,
    28,
    16,
    24,
    21,
    28,
    16,
    16,
    13,
    6,
    6,
    4,
    4,
    5,
    3,
    6,
    4,
    3,
    1,
    13,
    5,
    5,
    3,
    5,
    1,
    1,
    1,
    22,
)
_APPLICATION_NOTICE = {
    "approved": (
        "You are now enrolled as a GetBible topic contributor. New topics and "
        "verse-tag changes you make in the Mini App will be shared with the "
        "administrators for review. Approved changes become part of the core "
        "catalogue for everyone using the project."
    ),
    "rejected": (
        "Your GetBible contributor application was not approved at this time. "
        "Your personal topics and verse markings remain private on your devices."
    ),
    "deferred": (
        "Your GetBible contributor application is still under review. We will "
        "notify you when a final decision is available."
    ),
    "revoked": (
        "Your GetBible contributor enrolment has ended. Your personal topics and "
        "verse markings remain available on your devices, but new changes will "
        "not be submitted for project review."
    ),
}


def _empty_contribution_summary() -> dict[str, dict[str, int]]:
    return {
        "topics": {
            "pending": 0,
            "mapped": 0,
            "published": 0,
            "rejected": 0,
            "deferred": 0,
        },
        "events": {
            "pending": 0,
            "approved": 0,
            "rejected": 0,
            "deferred": 0,
            "applied": 0,
        },
    }


class ContributionError(ValueError):
    """A contribution or moderation mutation failed validation."""


class ContributionNotAllowed(ContributionError):
    """The authenticated Telegram user is not an approved contributor."""


class ContributionIdempotencyConflict(ContributionError):
    """A client event key was reused with a different payload."""


class ContributionPublicationConflict(ContributionError):
    """The live catalogue changed after a moderator built a publication plan."""


class ContributionRepositoryConflict(ContributionError):
    """A repository publication lease could not be acquired or completed."""


class ContributionNotificationConflict(ContributionError):
    """A notification delivery claim is stale or no longer active."""


@dataclass(frozen=True, slots=True)
class ContributorApplication:
    user_id: int
    state: ApplicationState
    first_name: str | None
    last_name: str | None
    username: str | None
    language_code: str | None
    requested_at: int
    updated_at: int
    decided_at: int | None
    disclosure_acknowledged_at: int | None


@dataclass(frozen=True, slots=True)
class SourceTopicRecord:
    contributor_id: int
    local_topic_id: str
    name: str | None
    color: str | None
    aliases: tuple[str, ...]
    state: TopicState
    canonical_topic_id: str | None
    canonical_definition: dict[str, object] | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ContributionEvent:
    id: int
    contributor_id: int
    client_event_id: str
    event_type: EventType
    local_topic_id: str
    topic_name: str | None
    topic_color: str | None
    book: int | None
    chapter: int | None
    verse: int | None
    payload_digest: str
    state: ReviewState
    canonical_topic_id: str | None
    replay_count: int
    submitted_at: int
    updated_at: int
    decided_at: int | None


@dataclass(frozen=True, slots=True)
class CanonicalTopicDefinition:
    id: str
    name: str
    color: str
    aliases: tuple[str, ...]
    created_at: int
    updated_at: int
    actor: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True, slots=True)
class ContributionNotification:
    id: int
    contributor_id: int
    kind: str
    message: str
    state: str
    attempts: int
    available_at: int
    created_at: int


@dataclass(frozen=True, slots=True)
class ClaimedContributionNotification(ContributionNotification):
    """A notification plus the private capability required to complete its lease."""

    claim_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CatalogRevision:
    revision: int
    checksum: str
    catalog: dict[str, Any]
    created_at: int
    actor: str

    @property
    def etag(self) -> str:
        return f'"gb-catalog-{self.revision}-{self.checksum[:16]}"'


@dataclass(frozen=True, slots=True)
class EventBatchResult:
    accepted: int
    replayed: int
    event_ids: dict[str, int]


NormalizedEvent: TypeAlias = tuple[
    str,
    EventType,
    str,
    str | None,
    str | None,
    int | None,
    int | None,
    int | None,
    str,
    bytes,
]


class ContributionStore:
    """SQLite-backed contributor application, review, and publication state."""

    def __init__(
        self,
        *,
        path: str | None,
        max_contributors: int = 10_000,
        max_events: int = 250_000,
    ) -> None:
        if not 100 <= max_contributors <= 1_000_000:
            raise ValueError("max_contributors must be between 100 and 1000000.")
        if not 1_000 <= max_events <= 5_000_000:
            raise ValueError("max_events must be between 1000 and 5000000.")
        self._path = Path(path) if path is not None else None
        self._max_contributors = max_contributors
        self._max_events = max_events
        self._guard = threading.RLock()
        self._connection: sqlite3.Connection | None = self._open()

    @property
    def path(self) -> Path | None:
        return self._path

    def close(self) -> None:
        """Close this process's connection; repeated calls are safe."""
        with self._guard:
            connection = self._connection
            if connection is None:
                return
            connection.close()
            self._connection = None

    def submit_application(
        self,
        user_id: int,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        username: str | None = None,
        language_code: str | None = None,
    ) -> tuple[ContributorApplication, bool]:
        """Create one idempotent application keyed by signed Telegram user ID."""
        identity = _telegram_user_id(user_id)
        profile = _profile(first_name, last_name, username, language_code)
        now = time.time_ns()
        with self._guard, self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM contributor_applications WHERE user_id = ?",
                (identity,),
            ).fetchone()
            created = row is None
            if created:
                count = int(
                    connection.execute("SELECT COUNT(*) FROM contributor_applications").fetchone()[
                        0
                    ]
                )
                if count >= self._max_contributors:
                    raise ContributionError("Contributor application capacity is full.")
                connection.execute(
                    """
                    INSERT INTO contributor_applications (
                        user_id, state, first_name, last_name, username,
                        language_code, requested_at, updated_at
                    ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?)
                    """,
                    (identity, *profile, now, now),
                )
                self._audit_locked(
                    connection,
                    actor=f"telegram:{identity}",
                    action="application_submitted",
                    contributor_id=identity,
                    subject_type="application",
                    subject_id=str(identity),
                    detail={},
                    now=now,
                )
            else:
                connection.execute(
                    """
                    UPDATE contributor_applications
                    SET first_name = ?, last_name = ?, username = ?,
                        language_code = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (*profile, now, identity),
                )
            result = connection.execute(
                "SELECT * FROM contributor_applications WHERE user_id = ?",
                (identity,),
            ).fetchone()
            assert result is not None
            return _application(result), created

    def observe_identity(
        self,
        user_id: int,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        username: str | None = None,
        language_code: str | None = None,
    ) -> None:
        """Refresh bounded display metadata for an existing application only."""
        identity = _telegram_user_id(user_id)
        profile = _profile(first_name, last_name, username, language_code)
        with self._guard:
            connection = self._connection_required()
            application = connection.execute(
                """
                SELECT first_name, last_name, username, language_code
                FROM contributor_applications WHERE user_id = ?
                """,
                (identity,),
            ).fetchone()
            if application is None:
                # The overwhelming majority of Mini App sessions belong to
                # ordinary readers.  Do not acquire SQLite's writer lock merely
                # to run an UPDATE that cannot match a contributor application.
                return
            if tuple(application) == profile:
                return
            with self._transaction() as connection:
                connection.execute(
                    """
                    UPDATE contributor_applications
                    SET first_name = ?, last_name = ?, username = ?, language_code = ?,
                        updated_at = ?
                    WHERE user_id = ?
                      AND (
                          first_name IS NOT ? OR last_name IS NOT ?
                          OR username IS NOT ? OR language_code IS NOT ?
                      )
                    """,
                    (*profile, time.time_ns(), identity, *profile),
                )

    def application_for(self, user_id: int) -> ContributorApplication | None:
        identity = _telegram_user_id(user_id)
        with self._guard:
            row = (
                self._connection_required()
                .execute(
                    "SELECT * FROM contributor_applications WHERE user_id = ?",
                    (identity,),
                )
                .fetchone()
            )
            return None if row is None else _application(row)

    def contribution_status(self, user_id: int) -> dict[str, object]:
        """Return this contributor's enrolment and private reconciliation state.

        Local topic IDs are meaningful only inside one contributor's browser.
        They therefore never enter the public catalogue and are returned only
        from this numeric-ID-scoped query.  A moderator mapping is not itself
        publication: ``published`` becomes true only after an event has been
        applied and the latest applied topic transition is not a delete.  The
        no-transition case supports mappings to an already bundled topic.
        """
        identity = _telegram_user_id(user_id)
        topic_states = {state: 0 for state in sorted(TOPIC_STATES)}
        event_states = {state: 0 for state in sorted(REVIEW_STATES)}
        with self._guard:
            connection = self._connection_required()
            application = connection.execute(
                """
                SELECT state, disclosure_acknowledged_at
                FROM contributor_applications WHERE user_id = ?
                """,
                (identity,),
            ).fetchone()
            if application is None:
                # Nearly every reader is not a contributor.  Keep ordinary
                # session bootstrap to one indexed application lookup and do
                # not scan private review tables for those readers.
                return {
                    "enabled": True,
                    "state": "not_applied",
                    "can_contribute": False,
                    "disclosure_required": False,
                    "topics": [],
                    "summary": _empty_contribution_summary(),
                }
            topic_count_rows = connection.execute(
                """
                SELECT state, COUNT(*)
                FROM contributor_source_topics
                WHERE contributor_id = ?
                GROUP BY state
                """,
                (identity,),
            ).fetchall()
            for row in topic_count_rows:
                topic_states[str(row[0])] = int(row[1])
            event_count_rows = connection.execute(
                """
                SELECT state, COUNT(*)
                FROM contribution_events
                WHERE contributor_id = ?
                GROUP BY state
                """,
                (identity,),
            ).fetchall()
            for row in event_count_rows:
                event_states[str(row[0])] = int(row[1])
            rows = connection.execute(
                """
                WITH applied_topics AS (
                    SELECT local_topic_id,
                           MAX(id) AS latest_applied_event_id,
                           MAX(CASE
                               WHEN event_type IN ('topic_upsert', 'topic_delete')
                               THEN id
                           END) AS latest_transition_id
                    FROM contribution_events
                    WHERE contributor_id = ? AND state = 'applied'
                    GROUP BY local_topic_id
                )
                SELECT source_topics.local_topic_id,
                       source_topics.name,
                       source_topics.color,
                       source_topics.aliases,
                       source_topics.state,
                       source_topics.canonical_topic_id,
                       canonical.definition_json AS canonical_definition_json,
                       applied.latest_applied_event_id IS NOT NULL AS has_applied_event,
                       transition.event_type AS latest_applied_topic_transition,
                       SUM(CASE
                           WHEN source_topics.canonical_topic_id IS NOT NULL
                            AND applied.latest_applied_event_id IS NOT NULL
                            AND COALESCE(transition.event_type, '') != 'topic_delete'
                           THEN 1 ELSE 0
                       END) OVER () AS published_count,
                       SUM(CASE
                           WHEN source_topics.canonical_topic_id IS NOT NULL
                           THEN 1 ELSE 0
                       END) OVER () AS mapped_count
                FROM contributor_source_topics AS source_topics
                LEFT JOIN contribution_canonical_topics AS canonical
                  ON canonical.topic_id = source_topics.canonical_topic_id
                LEFT JOIN applied_topics AS applied
                  ON applied.local_topic_id = source_topics.local_topic_id
                LEFT JOIN contribution_events AS transition
                  ON transition.id = applied.latest_transition_id
                 AND transition.contributor_id = source_topics.contributor_id
                WHERE source_topics.contributor_id = ?
                ORDER BY source_topics.updated_at DESC,
                         source_topics.local_topic_id
                LIMIT ?
                """,
                (identity, identity, MAX_CONTRIBUTION_STATUS_TOPICS),
            ).fetchall()

        state = str(application[0])
        approved = state == "approved"
        published_count = int(rows[0][9]) if rows else 0
        mapped_count = int(rows[0][10]) if rows else 0
        topics: list[dict[str, object]] = []
        for row in rows:
            source_state = str(row[4])
            canonical_id = cast(str | None, row[5])
            published = bool(
                canonical_id is not None
                and row[7]
                and row[8] != "topic_delete"
            )
            topic: dict[str, object] = {
                "local_topic_id": str(row[0]),
                "state": source_state,
                "published": published,
            }
            if canonical_id is not None:
                topic["canonical_topic_id"] = canonical_id
            if canonical_id is not None and (source_state == "mapped" or published):
                canonical_topic = _private_canonical_topic(row, canonical_id)
                if canonical_topic is not None:
                    topic["canonical_topic"] = canonical_topic
            topics.append(topic)
        topics.sort(key=lambda value: cast(str, value["local_topic_id"]))

        return {
            "enabled": True,
            "state": state,
            "can_contribute": approved,
            "disclosure_required": bool(
                approved
                and application[1] is None
            ),
            "topics": topics,
            "summary": {
                "topics": {
                    "pending": topic_states["pending"],
                    "mapped": mapped_count,
                    "published": published_count,
                    "rejected": topic_states["rejected"],
                    "deferred": topic_states["deferred"],
                },
                "events": {
                    "pending": event_states["pending"],
                    "approved": event_states["approved"],
                    "rejected": event_states["rejected"],
                    "deferred": event_states["deferred"],
                    "applied": event_states["applied"],
                },
            },
        }

    def acknowledge_disclosure(self, user_id: int) -> dict[str, object]:
        identity = _telegram_user_id(user_id)
        with self._guard:
            connection = self._connection_required()
            application = connection.execute(
                """
                SELECT state, disclosure_acknowledged_at
                FROM contributor_applications WHERE user_id = ?
                """,
                (identity,),
            ).fetchone()
            if application is None or application[0] != "approved":
                raise ContributionNotAllowed(
                    "Only approved contributors can acknowledge this notice."
                )
            if application[1] is None:
                # Recheck under the write lock: another process may have
                # acknowledged or revoked the application after the read above.
                with self._transaction() as connection:
                    application = connection.execute(
                        """
                        SELECT state, disclosure_acknowledged_at
                        FROM contributor_applications WHERE user_id = ?
                        """,
                        (identity,),
                    ).fetchone()
                    if application is None or application[0] != "approved":
                        raise ContributionNotAllowed(
                            "Only approved contributors can acknowledge this notice."
                        )
                    if application[1] is None:
                        now = time.time_ns()
                        cursor = connection.execute(
                            """
                            UPDATE contributor_applications
                            SET disclosure_acknowledged_at = ?, updated_at = ?
                            WHERE user_id = ? AND state = 'approved'
                              AND disclosure_acknowledged_at IS NULL
                            """,
                            (now, now, identity),
                        )
                        if cursor.rowcount != 1:
                            raise ContributionNotAllowed(
                                "Only approved contributors can acknowledge this notice."
                            )
                        self._audit_locked(
                            connection,
                            actor=f"telegram:{identity}",
                            action="disclosure_acknowledged",
                            contributor_id=identity,
                            subject_type="application",
                            subject_id=str(identity),
                            detail={},
                            now=now,
                        )
        return self.contribution_status(identity)

    def list_applications(
        self,
        *,
        states: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> tuple[ContributorApplication, ...]:
        choices = _state_filter(states, APPLICATION_STATES, "application")
        bounded = _bounded_limit(limit)
        where, parameters = _where_in("state", choices)
        with self._guard:
            rows = (
                self._connection_required()
                .execute(
                    f"""
                SELECT * FROM contributor_applications
                {where}
                ORDER BY requested_at, user_id
                LIMIT ?
                """,  # nosec B608 -- where is constructed from a fixed identifier
                    (*parameters, bounded),
                )
                .fetchall()
            )
        return tuple(_application(row) for row in rows)

    def decide_application(
        self,
        user_id: int,
        state: str,
        *,
        actor: str,
        note: str = "",
    ) -> ContributorApplication:
        identity = _telegram_user_id(user_id)
        if state not in APPLICATION_STATES - {"pending"}:
            raise ContributionError("Application decision state is invalid.")
        reviewer = _bounded_text(actor, "actor", MAX_ACTOR_LENGTH)
        decision_note = _optional_bounded_text(note, "note", MAX_DECISION_NOTE) or ""
        now = time.time_ns()
        with self._guard, self._transaction() as connection:
            previous = connection.execute(
                "SELECT state FROM contributor_applications WHERE user_id = ?",
                (identity,),
            ).fetchone()
            if previous is None:
                raise ContributionError("Contributor application was not found.")
            connection.execute(
                """
                UPDATE contributor_applications
                SET state = ?, decided_at = ?, updated_at = ?,
                    disclosure_acknowledged_at = CASE
                        WHEN ? = 'approved' AND state = 'approved'
                            THEN disclosure_acknowledged_at
                        WHEN ? = 'approved' THEN NULL
                        ELSE disclosure_acknowledged_at
                    END
                WHERE user_id = ?
                """,
                (state, now, now, state, state, identity),
            )
            if state != "approved":
                # Contribution tokens are deliberately server-revocable.
                # Removing them here makes a moderator decision effective
                # immediately; authenticate_capability also rechecks the
                # application state so an older token can never retain
                # authority by accident.
                connection.execute(
                    "DELETE FROM contributor_capabilities WHERE contributor_id = ?",
                    (identity,),
                )
            connection.execute(
                """
                INSERT INTO contribution_decisions (
                    contributor_id, subject_type, subject_id, previous_state,
                    decision, canonical_topic_id, note, actor, created_at
                ) VALUES (?, 'application', ?, ?, ?, NULL, ?, ?, ?)
                """,
                (identity, str(identity), str(previous[0]), state, decision_note, reviewer, now),
            )
            self._audit_locked(
                connection,
                actor=reviewer,
                action="application_decided",
                contributor_id=identity,
                subject_type="application",
                subject_id=str(identity),
                detail={"previous_state": previous[0], "state": state},
                now=now,
            )
            notice = _APPLICATION_NOTICE.get(state)
            if notice is not None and previous[0] != state:
                connection.execute(
                    """
                    INSERT INTO contribution_notifications (
                        contributor_id, kind, message, state, attempts,
                        available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
                    """,
                    (identity, f"application_{state}", notice, now, now, now),
                )
            row = connection.execute(
                "SELECT * FROM contributor_applications WHERE user_id = ?",
                (identity,),
            ).fetchone()
            assert row is not None
            return _application(row)

    def issue_capability(
        self,
        user_id: int,
        *,
        ttl_seconds: int = CONTRIBUTOR_CAPABILITY_TTL_SECONDS,
    ) -> str:
        """Issue a restart-safe, revocable bearer capability to one contributor.

        Only the SHA-256 digest is retained.  Issuance intentionally creates a
        fresh capability on every successful exchange: if an HTTP response is
        lost the caller can retry safely, while the per-user active-token bound
        prevents unbounded state growth.
        """
        identity = _telegram_user_id(user_id)
        ttl = _positive_integer(
            ttl_seconds,
            "ttl_seconds",
            CONTRIBUTOR_CAPABILITY_TTL_SECONDS,
        )
        now = time.time_ns()
        expires_at = now + ttl * 1_000_000_000
        with self._guard, self._transaction() as connection:
            application = connection.execute(
                "SELECT state FROM contributor_applications WHERE user_id = ?",
                (identity,),
            ).fetchone()
            if application is None or application[0] != "approved":
                raise ContributionNotAllowed(
                    "This Telegram user is not an approved contributor."
                )
            connection.execute(
                "DELETE FROM contributor_capabilities WHERE expires_at <= ?",
                (now,),
            )
            active = connection.execute(
                """
                SELECT token_digest
                FROM contributor_capabilities
                WHERE contributor_id = ?
                ORDER BY last_used_at, issued_at, token_digest
                """,
                (identity,),
            ).fetchall()
            overflow = len(active) - MAX_ACTIVE_CONTRIBUTOR_CAPABILITIES + 1
            if overflow > 0:
                connection.executemany(
                    "DELETE FROM contributor_capabilities WHERE token_digest = ?",
                    ((bytes(row[0]),) for row in active[:overflow]),
                )

            token: str | None = None
            digest: bytes | None = None
            for _ in range(4):
                candidate = f"gbc_{secrets.token_urlsafe(32)}"
                candidate_digest = hashlib.sha256(candidate.encode("ascii")).digest()
                exists = connection.execute(
                    "SELECT 1 FROM contributor_capabilities WHERE token_digest = ?",
                    (candidate_digest,),
                ).fetchone()
                if exists is None:
                    token = candidate
                    digest = candidate_digest
                    break
            if token is None or digest is None:
                raise RuntimeError("Could not allocate a unique contributor capability.")
            connection.execute(
                """
                INSERT INTO contributor_capabilities (
                    token_digest, contributor_id, issued_at, expires_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (digest, identity, now, expires_at, now),
            )
            self._audit_locked(
                connection,
                actor=f"telegram:{identity}",
                action="capability_issued",
                contributor_id=identity,
                subject_type="capability",
                subject_id=digest.hex(),
                detail={"expires_at": expires_at},
                now=now,
            )
        return token

    def authenticate_capability(self, token: str) -> int:
        """Resolve an unexpired capability and recheck current approval."""
        if not isinstance(token, str) or _CONTRIBUTOR_CAPABILITY_RE.fullmatch(token) is None:
            raise ContributionNotAllowed("The contributor capability is invalid or expired.")
        digest = hashlib.sha256(token.encode("ascii")).digest()
        now = time.time_ns()
        identity: int | None = None
        with self._guard, self._transaction() as connection:
            connection.execute(
                "DELETE FROM contributor_capabilities WHERE expires_at <= ?",
                (now,),
            )
            row = connection.execute(
                """
                SELECT capability.contributor_id
                FROM contributor_capabilities AS capability
                JOIN contributor_applications AS application
                  ON application.user_id = capability.contributor_id
                WHERE capability.token_digest = ?
                  AND capability.expires_at > ?
                  AND application.state = 'approved'
                """,
                (digest, now),
            ).fetchone()
            if row is not None:
                identity = int(row[0])
                connection.execute(
                    """
                    UPDATE contributor_capabilities
                    SET last_used_at = ?
                    WHERE token_digest = ? AND expires_at > ?
                    """,
                    (now, digest, now),
                )
        if identity is None:
            raise ContributionNotAllowed("The contributor capability is invalid or expired.")
        return identity

    def record_events(
        self,
        contributor_id: int,
        events: Sequence[Mapping[str, object]],
    ) -> EventBatchResult:
        identity = _telegram_user_id(contributor_id)
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise ContributionError("events must be an array.")
        if not 1 <= len(events) <= MAX_CONTRIBUTION_BATCH:
            raise ContributionError(
                f"events must contain between 1 and {MAX_CONTRIBUTION_BATCH} items."
            )
        normalized = tuple(_normalize_event(value) for value in events)
        client_ids = [value[0] for value in normalized]
        if len(set(client_ids)) != len(client_ids):
            raise ContributionError("client_event_id values must be unique in a batch.")
        now = time.time_ns()
        with self._guard, self._transaction() as connection:
            self._require_contributor_locked(connection, identity, require_disclosure=True)
            return self._record_normalized_events_locked(
                connection,
                identity,
                normalized,
                now=now,
            )

    @staticmethod
    def _require_contributor_locked(
        connection: sqlite3.Connection,
        identity: int,
        *,
        require_disclosure: bool,
    ) -> sqlite3.Row:
        application = connection.execute(
            """
            SELECT state, disclosure_acknowledged_at
            FROM contributor_applications WHERE user_id = ?
            """,
            (identity,),
        ).fetchone()
        if application is None or application[0] != "approved":
            raise ContributionNotAllowed("This Telegram user is not an approved contributor.")
        if require_disclosure and application[1] is None:
            raise ContributionNotAllowed(
                "The contributor disclosure must be acknowledged first."
            )
        return application

    def _record_normalized_events_locked(
        self,
        connection: sqlite3.Connection,
        identity: int,
        normalized: Sequence[NormalizedEvent],
        *,
        now: int,
    ) -> EventBatchResult:
        accepted = 0
        replayed = 0
        event_ids: dict[str, int] = {}
        current_events = int(
            connection.execute("SELECT COUNT(*) FROM contribution_events").fetchone()[0]
        )
        for (
            client_event_id,
            event_type,
            local_topic_id,
            topic_name,
            topic_color,
            book,
            chapter,
            verse,
            payload_json,
            digest,
        ) in normalized:
            existing = connection.execute(
                """
                SELECT id, payload_digest FROM contribution_events
                WHERE contributor_id = ? AND client_event_id = ?
                """,
                (identity, client_event_id),
            ).fetchone()
            if existing is not None:
                if not _constant_digest_equal(bytes(existing[1]), digest):
                    raise ContributionIdempotencyConflict(
                        "client_event_id was already used for different data."
                    )
                connection.execute(
                    """
                    UPDATE contribution_events
                    SET replay_count = replay_count + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, int(existing[0])),
                )
                replayed += 1
                event_ids[client_event_id] = int(existing[0])
                continue
            if current_events + accepted >= self._max_events:
                raise ContributionError("Contribution event capacity is full.")
            connection.execute(
                """
                INSERT INTO contributor_source_topics (
                    contributor_id, local_topic_id, name, color, aliases,
                    state, canonical_topic_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '[]', 'pending', NULL, ?, ?)
                ON CONFLICT(contributor_id, local_topic_id) DO UPDATE SET
                    name = CASE
                        WHEN ? = 'topic_upsert' THEN excluded.name
                        ELSE COALESCE(contributor_source_topics.name, excluded.name)
                    END,
                    color = CASE
                        WHEN ? = 'topic_upsert' THEN excluded.color
                        ELSE COALESCE(contributor_source_topics.color, excluded.color)
                    END,
                    updated_at = excluded.updated_at,
                    state = CASE
                        WHEN contributor_source_topics.state = 'rejected' THEN 'pending'
                        ELSE contributor_source_topics.state
                    END
                """,
                (
                    identity,
                    local_topic_id,
                    topic_name,
                    topic_color,
                    now,
                    now,
                    event_type,
                    event_type,
                ),
            )
            source_mapping = connection.execute(
                """
                SELECT state, canonical_topic_id
                FROM contributor_source_topics
                WHERE contributor_id = ? AND local_topic_id = ?
                """,
                (identity, local_topic_id),
            ).fetchone()
            assert source_mapping is not None
            event_canonical = (
                cast(str | None, source_mapping[1])
                if str(source_mapping[0]) == "mapped"
                else None
            )
            if event_type in {"topic_upsert", "topic_delete"}:
                connection.execute(
                    """
                    UPDATE contributor_source_topics
                    SET state = 'pending', updated_at = ?
                    WHERE contributor_id = ? AND local_topic_id = ?
                    """,
                    (now, identity, local_topic_id),
                )
            cursor = connection.execute(
                """
                INSERT INTO contribution_events (
                    contributor_id, client_event_id, event_type,
                    local_topic_id, topic_name, topic_color, book, chapter,
                    verse, payload_json, payload_digest, state,
                    canonical_topic_id, replay_count, submitted_at,
                    updated_at, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                          ?, 0, ?, ?, NULL)
                """,
                (
                    identity,
                    client_event_id,
                    event_type,
                    local_topic_id,
                    topic_name,
                    topic_color,
                    book,
                    chapter,
                    verse,
                    payload_json,
                    digest,
                    event_canonical,
                    now,
                    now,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a contribution event ID.")
            event_ids[client_event_id] = int(cursor.lastrowid)
            accepted += 1
        if accepted:
            self._audit_locked(
                connection,
                actor=f"telegram:{identity}",
                action="events_submitted",
                contributor_id=identity,
                subject_type="event_batch",
                subject_id=f"{now}:{accepted}",
                detail={"accepted": accepted, "replayed": replayed},
                now=now,
            )
        return EventBatchResult(accepted, replayed, event_ids)

    def list_source_topics(
        self,
        *,
        states: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> tuple[SourceTopicRecord, ...]:
        choices = _state_filter(states, TOPIC_STATES, "topic")
        where, parameters = _where_in("state", choices)
        with self._guard:
            rows = (
                self._connection_required()
                .execute(
                    f"""
                SELECT source_topics.*,
                       canonical.definition_json AS canonical_definition_json
                FROM contributor_source_topics AS source_topics
                LEFT JOIN contribution_canonical_topics AS canonical
                  ON canonical.topic_id = source_topics.canonical_topic_id
                {where}
                ORDER BY lower(COALESCE(source_topics.name, '')),
                         source_topics.contributor_id,
                         source_topics.local_topic_id
                LIMIT ?
                """,  # nosec B608 -- where is constructed from a fixed identifier
                    (*parameters, _bounded_limit(limit)),
                )
                .fetchall()
            )
        return tuple(_source_topic(row) for row in rows)

    def set_topic_mapping(
        self,
        contributor_id: int,
        local_topic_id: str,
        canonical_topic_id: str | None = None,
        *,
        state: str,
        actor: str,
        note: str = "",
        canonical_definition: Mapping[str, object] | None = None,
        name: str | None = None,
        color: str | None = None,
        aliases: Sequence[str] | None = None,
    ) -> SourceTopicRecord:
        identity = _telegram_user_id(contributor_id)
        local_id = _safe_id(local_topic_id, "local_topic_id")
        if state not in TOPIC_STATES - {"pending"}:
            raise ContributionError("Topic decision state is invalid.")
        canonical = _canonical_topic_id(canonical_topic_id) if canonical_topic_id else None
        if state == "mapped" and canonical is None:
            raise ContributionError("Mapped topics require canonical_topic_id.")
        if state != "mapped" and canonical is not None:
            raise ContributionError("Only mapped topics may name a canonical topic.")
        reviewer = _bounded_text(actor, "actor", MAX_ACTOR_LENGTH)
        decision_note = _optional_bounded_text(note, "note", MAX_DECISION_NOTE) or ""
        checked_name = _topic_name(name) if name is not None else None
        checked_color = _color(color) if color is not None else None
        checked_aliases = _aliases(aliases or ()) if aliases is not None else None
        definition = (
            _canonical_definition(canonical_definition)
            if canonical_definition is not None
            else None
        )
        if definition is not None and definition["id"] != canonical:
            raise ContributionError("canonical_definition id must match canonical_topic_id.")
        now = time.time_ns()
        with self._guard, self._transaction() as connection:
            previous = connection.execute(
                """
                SELECT * FROM contributor_source_topics
                WHERE contributor_id = ? AND local_topic_id = ?
                """,
                (identity, local_id),
            ).fetchone()
            if previous is None:
                raise ContributionError("Contributor topic was not found.")
            previous_canonical = cast(str | None, previous[6])
            canonical_changed = state == "mapped" and previous_canonical != canonical
            mapping_identity_changed = previous_canonical is not None and (
                state != "mapped" or previous_canonical != canonical
            )
            if mapping_identity_changed:
                applied = connection.execute(
                    """
                    SELECT 1 FROM contribution_events
                    WHERE contributor_id = ? AND local_topic_id = ?
                      AND state = 'applied'
                    LIMIT 1
                    """,
                    (identity, local_id),
                ).fetchone()
                if applied is not None:
                    raise ContributionError(
                        "A source mapping with published events cannot change canonical topic."
                    )
            if definition is not None:
                encoded_definition = _json(definition)
                previous_definition = connection.execute(
                    """
                    SELECT definition_json FROM contribution_canonical_topics
                    WHERE topic_id = ?
                    """,
                    (definition["id"],),
                ).fetchone()
                definition_changed = (
                    previous_definition is None or str(previous_definition[0]) != encoded_definition
                )
                connection.execute(
                    """
                    INSERT INTO contribution_canonical_topics (
                        topic_id, name, color, aliases, definition_json,
                        created_at, updated_at, actor
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(topic_id) DO UPDATE SET
                        name = excluded.name,
                        color = excluded.color,
                        aliases = excluded.aliases,
                        definition_json = excluded.definition_json,
                        updated_at = excluded.updated_at,
                        actor = excluded.actor
                    """,
                    (
                        definition["id"],
                        definition["name"],
                        definition["color"],
                        _json(definition["aliases"]),
                        encoded_definition,
                        now,
                        now,
                        reviewer,
                    ),
                )
                if definition_changed:
                    approved_topic_events = connection.execute(
                        """
                        SELECT id, contributor_id
                        FROM contribution_events
                        WHERE canonical_topic_id = ?
                          AND event_type = 'topic_upsert'
                          AND state = 'approved'
                        ORDER BY id
                        """,
                        (canonical,),
                    ).fetchall()
                    connection.execute(
                        """
                        UPDATE contribution_events
                        SET state = 'pending', decided_at = NULL, updated_at = ?
                        WHERE canonical_topic_id = ?
                          AND event_type = 'topic_upsert'
                          AND state = 'approved'
                        """,
                        (now, canonical),
                    )
                    for approved_event in approved_topic_events:
                        event_identity = int(approved_event[0])
                        event_contributor = int(approved_event[1])
                        connection.execute(
                            """
                            INSERT INTO contribution_decisions (
                                contributor_id, subject_type, subject_id,
                                previous_state, decision, canonical_topic_id,
                                note, actor, created_at
                            ) VALUES (?, 'event', ?, 'approved', 'pending', ?, ?, ?, ?)
                            """,
                            (
                                event_contributor,
                                str(event_identity),
                                canonical,
                                "Canonical topic definition changed; "
                                "explicit re-review is required.",
                                reviewer,
                                now,
                            ),
                        )
                        self._audit_locked(
                            connection,
                            actor=reviewer,
                            action="topic_event_reopened_after_definition_change",
                            contributor_id=event_contributor,
                            subject_type="event",
                            subject_id=str(event_identity),
                            detail={"canonical_topic_id": canonical},
                            now=now,
                        )
            connection.execute(
                """
                UPDATE contributor_source_topics
                SET state = ?, canonical_topic_id = ?,
                    name = COALESCE(?, name), color = COALESCE(?, color),
                    aliases = COALESCE(?, aliases), updated_at = ?
                WHERE contributor_id = ? AND local_topic_id = ?
                """,
                (
                    state,
                    canonical,
                    checked_name,
                    checked_color,
                    _json(checked_aliases) if checked_aliases is not None else None,
                    now,
                    identity,
                    local_id,
                ),
            )
            if state == "mapped":
                approved_events = (
                    connection.execute(
                        """
                        SELECT id FROM contribution_events
                        WHERE contributor_id = ? AND local_topic_id = ?
                          AND state = 'approved'
                        ORDER BY id
                        """,
                        (identity, local_id),
                    ).fetchall()
                    if canonical_changed
                    else ()
                )
                connection.execute(
                    """
                    UPDATE contribution_events
                    SET canonical_topic_id = ?,
                        state = CASE
                            WHEN ? AND state = 'approved' THEN 'pending'
                            ELSE state
                        END,
                        decided_at = CASE
                            WHEN ? AND state = 'approved' THEN NULL
                            ELSE decided_at
                        END,
                        updated_at = ?
                    WHERE contributor_id = ? AND local_topic_id = ?
                      AND state IN ('pending', 'deferred', 'approved')
                    """,
                    (
                        canonical,
                        int(canonical_changed),
                        int(canonical_changed),
                        now,
                        identity,
                        local_id,
                    ),
                )
                for approved_event in approved_events:
                    connection.execute(
                        """
                        INSERT INTO contribution_decisions (
                            contributor_id, subject_type, subject_id,
                            previous_state, decision, canonical_topic_id,
                            note, actor, created_at
                        ) VALUES (?, 'event', ?, 'approved', 'pending', ?, ?, ?, ?)
                        """,
                        (
                            identity,
                            str(int(approved_event[0])),
                            canonical,
                            "Canonical topic mapping changed; explicit re-review is required.",
                            reviewer,
                            now,
                        ),
                    )
                if approved_events:
                    self._audit_locked(
                        connection,
                        actor=reviewer,
                        action="events_reopened_after_topic_remap",
                        contributor_id=identity,
                        subject_type="topic",
                        subject_id=local_id,
                        detail={
                            "count": len(approved_events),
                            "previous_canonical_topic_id": previous_canonical,
                            "canonical_topic_id": canonical,
                        },
                        now=now,
                    )
            connection.execute(
                """
                INSERT INTO contribution_decisions (
                    contributor_id, subject_type, subject_id, previous_state,
                    decision, canonical_topic_id, note, actor, created_at
                ) VALUES (?, 'topic', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity,
                    local_id,
                    str(previous[5]),
                    state,
                    canonical,
                    decision_note,
                    reviewer,
                    now,
                ),
            )
            self._audit_locked(
                connection,
                actor=reviewer,
                action="topic_decided",
                contributor_id=identity,
                subject_type="topic",
                subject_id=local_id,
                detail={"state": state, "canonical_topic_id": canonical},
                now=now,
            )
            row = connection.execute(
                """
                SELECT source_topics.*,
                       canonical.definition_json AS canonical_definition_json
                FROM contributor_source_topics AS source_topics
                LEFT JOIN contribution_canonical_topics AS canonical
                  ON canonical.topic_id = source_topics.canonical_topic_id
                WHERE source_topics.contributor_id = ?
                  AND source_topics.local_topic_id = ?
                """,
                (identity, local_id),
            ).fetchone()
            assert row is not None
            return _source_topic(row)

    def list_canonical_topics(self) -> tuple[CanonicalTopicDefinition, ...]:
        """Return moderator-created English definitions, never raw proposals."""
        with self._guard:
            rows = (
                self._connection_required()
                .execute(
                    """
                SELECT topic_id, name, color, aliases, created_at, updated_at, actor
                FROM contribution_canonical_topics ORDER BY topic_id
                """
                )
                .fetchall()
            )
        return tuple(
            CanonicalTopicDefinition(
                id=str(row[0]),
                name=str(row[1]),
                color=str(row[2]),
                aliases=tuple(str(value) for value in json.loads(str(row[3]))),
                created_at=int(row[4]),
                updated_at=int(row[5]),
                actor=str(row[6]),
            )
            for row in rows
        )

    def list_events(
        self,
        *,
        states: Iterable[str] | None = None,
        types: Iterable[str] | None = None,
        limit: int = 1000,
        after_id: int = 0,
    ) -> tuple[ContributionEvent, ...]:
        state_choices = _state_filter(states, REVIEW_STATES, "event")
        type_choices = _state_filter(types, EVENT_TYPES, "event type")
        cursor = _positive_integer(
            after_id,
            "after_id",
            2_147_483_647,
            minimum=0,
        )
        clauses: list[str] = ["id > ?"]
        parameters: list[object] = [cursor]
        for column, choices in (("state", state_choices), ("event_type", type_choices)):
            if choices:
                placeholders = ",".join("?" for _ in choices)
                clauses.append(f"{column} IN ({placeholders})")
                parameters.extend(choices)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._guard:
            rows = (
                self._connection_required()
                .execute(
                    f"""
                SELECT * FROM contribution_events
                {where}
                ORDER BY id
                LIMIT ?
                """,  # nosec B608 -- fixed identifiers and parameter placeholders only
                    (*parameters, _bounded_limit(limit)),
                )
                .fetchall()
            )
        return tuple(_event(row) for row in rows)

    def decide_event(
        self,
        event_id: int,
        state: str,
        *,
        actor: str,
        canonical_topic_id: str | None = None,
        note: str = "",
    ) -> ContributionEvent:
        identity = _positive_integer(event_id, "event_id", 2_147_483_647)
        if state not in REVIEW_STATES - {"pending", "applied"}:
            raise ContributionError("Event decision state is invalid.")
        reviewer = _bounded_text(actor, "actor", MAX_ACTOR_LENGTH)
        decision_note = _optional_bounded_text(note, "note", MAX_DECISION_NOTE) or ""
        canonical = _canonical_topic_id(canonical_topic_id) if canonical_topic_id else None
        now = time.time_ns()
        with self._guard, self._transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM contribution_events WHERE id = ?",
                (identity,),
            ).fetchone()
            if previous is None:
                raise ContributionError("Contribution event was not found.")
            if canonical is None:
                mapping = connection.execute(
                    """
                    SELECT canonical_topic_id FROM contributor_source_topics
                    WHERE contributor_id = ? AND local_topic_id = ? AND state = 'mapped'
                    """,
                    (int(previous[1]), str(previous[4])),
                ).fetchone()
                if mapping is not None:
                    canonical = cast(str | None, mapping[0])
            if state == "approved" and canonical is None:
                raise ContributionError("Approved events require a resolved canonical topic.")
            connection.execute(
                """
                UPDATE contribution_events
                SET state = ?, canonical_topic_id = ?, decided_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (state, canonical, now, now, identity),
            )
            connection.execute(
                """
                INSERT INTO contribution_decisions (
                    contributor_id, subject_type, subject_id, previous_state,
                    decision, canonical_topic_id, note, actor, created_at
                ) VALUES (?, 'event', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(previous[1]),
                    str(identity),
                    str(previous[12]),
                    state,
                    canonical,
                    decision_note,
                    reviewer,
                    now,
                ),
            )
            self._audit_locked(
                connection,
                actor=reviewer,
                action="event_decided",
                contributor_id=int(previous[1]),
                subject_type="event",
                subject_id=str(identity),
                detail={"state": state, "canonical_topic_id": canonical},
                now=now,
            )
            row = connection.execute(
                "SELECT * FROM contribution_events WHERE id = ?",
                (identity,),
            ).fetchone()
            assert row is not None
            return _event(row)

    def current_catalog(self) -> CatalogRevision:
        with self._guard:
            row = (
                self._connection_required()
                .execute(
                    """
                SELECT revision, checksum, catalog_json, created_at, actor
                FROM contribution_catalog_revisions
                ORDER BY revision DESC LIMIT 1
                """
                )
                .fetchone()
            )
        assert row is not None
        return CatalogRevision(
            revision=int(row[0]),
            checksum=str(row[1]),
            catalog=cast(dict[str, Any], json.loads(str(row[2]))),
            created_at=int(row[3]),
            actor=str(row[4]),
        )

    def published_topic_ids(self) -> tuple[str, ...]:
        """Return every contributed topic that has appeared in a live revision.

        Catalogue revisions are immutable, so their topic definitions are the
        authoritative provenance for this lifetime rule. Applied event history
        alone is insufficient: a pre-publication upsert followed by a delete can
        be terminalized atomically without ever making that topic live.
        """
        with self._guard:
            rows = (
                self._connection_required()
                .execute(
                    """
                    SELECT catalog_json
                    FROM contribution_catalog_revisions
                    ORDER BY revision
                    """
                )
                .fetchall()
            )
        published: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(str(row[0]))
            except json.JSONDecodeError as error:
                raise ContributionError(
                    "A stored contribution catalogue revision is invalid."
                ) from error
            normalized = normalize_catalog(cast(Mapping[str, object], payload))
            published.update(
                cast(str, topic["id"])
                for topic in cast(list[dict[str, object]], normalized["topics"])
            )
        return tuple(sorted(published))

    def publish_catalog(
        self,
        catalog: Mapping[str, object],
        *,
        actor: str,
    ) -> CatalogRevision:
        """Atomically make a cumulative, sanitized contribution overlay live."""
        reviewer = _bounded_text(actor, "actor", MAX_ACTOR_LENGTH)
        normalized = normalize_catalog(catalog)
        encoded = _json(normalized)
        checksum = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = time.time_ns()
        with self._guard, self._transaction() as connection:
            return self._publish_catalog_locked(
                connection,
                normalized=normalized,
                encoded=encoded,
                checksum=checksum,
                actor=reviewer,
                now=now,
            )

    def publish_approved_events_atomically(
        self,
        catalog: Mapping[str, object],
        event_ids: Sequence[int],
        *,
        actor: str,
        expected_revision: int | None = None,
        expected_checksum: str | None = None,
    ) -> CatalogRevision:
        """Publish a cumulative overlay and mark its approved events applied together.

        ``catalog`` is the complete cumulative contribution overlay, not merely
        the latest review batch.  A retry with the same catalogue and already
        applied event IDs is a no-op and returns the existing live revision.
        """
        reviewer = _bounded_text(actor, "actor", MAX_ACTOR_LENGTH)
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or not 0 <= expected_revision <= 9_007_199_254_740_991
        ):
            raise ContributionError("expected_revision is invalid.")
        if expected_checksum is not None and not valid_checksum(expected_checksum):
            raise ContributionError("expected_checksum is invalid.")
        values = tuple(
            dict.fromkeys(
                _positive_integer(value, "event_id", 2_147_483_647) for value in event_ids
            )
        )
        if len(values) > 10_000:
            raise ContributionError("Too many events were selected.")
        normalized = normalize_catalog(catalog)
        encoded = _json(normalized)
        checksum = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = time.time_ns()
        with self._guard, self._transaction() as connection:
            live = connection.execute(
                """
                SELECT revision, checksum
                FROM contribution_catalog_revisions
                ORDER BY revision DESC LIMIT 1
                """
            ).fetchone()
            assert live is not None
            if (expected_revision is not None and int(live[0]) != expected_revision) or (
                expected_checksum is not None and str(live[1]) != expected_checksum
            ):
                raise ContributionPublicationConflict(
                    "The live contribution catalogue changed; rebuild the publication plan."
                )
            if values:
                placeholders = ",".join("?" for _ in values)
                rows = connection.execute(
                    f"""
                    SELECT id, state FROM contribution_events
                    WHERE id IN ({placeholders})
                    """,  # nosec B608 -- bounded integer placeholders
                    values,
                ).fetchall()
                found = {int(row[0]): str(row[1]) for row in rows}
                if set(found) != set(values):
                    raise ContributionError("One or more contribution events were not found.")
                if any(state not in {"approved", "applied"} for state in found.values()):
                    raise ContributionError("Only approved contribution events can be published.")
            revision = self._publish_catalog_locked(
                connection,
                normalized=normalized,
                encoded=encoded,
                checksum=checksum,
                actor=reviewer,
                now=now,
            )
            changed = 0
            if values:
                placeholders = ",".join("?" for _ in values)
                cursor = connection.execute(
                    f"""
                    UPDATE contribution_events
                    SET state = 'applied', updated_at = ?
                    WHERE id IN ({placeholders}) AND state = 'approved'
                    """,  # nosec B608 -- bounded integer placeholders
                    (now, *values),
                )
                changed = int(cursor.rowcount)
            if changed:
                self._audit_locked(
                    connection,
                    actor=reviewer,
                    action="events_applied",
                    contributor_id=None,
                    subject_type="catalog_revision",
                    subject_id=str(revision.revision),
                    detail={"count": changed},
                    now=now,
                )
            return revision

    def publication_state(self) -> dict[str, object]:
        with self._guard:
            row = (
                self._connection_required()
                .execute("SELECT * FROM contribution_publication_state WHERE singleton = 1")
                .fetchone()
            )
        assert row is not None
        result = dict(row)
        # A lease token is a private compare-and-swap capability, not status.
        result.pop("repo_token", None)
        return result

    def begin_repo_publication(
        self,
        revision: int,
        checksum: str,
        *,
        actor: str,
        lease_seconds: int = 900,
    ) -> str:
        """Lease one live revision for repository publication.

        The returned opaque token is required to finish. An abandoned lease
        becomes recoverable after ``lease_seconds``; a successfully pushed
        revision can never be leased again.
        """
        checked_revision = _positive_integer(revision, "revision", 2_147_483_647, minimum=0)
        if not valid_checksum(checksum):
            raise ContributionError("checksum is invalid.")
        reviewer = _bounded_text(actor, "actor", MAX_ACTOR_LENGTH)
        lease = _positive_integer(lease_seconds, "lease_seconds", 3600)
        token = secrets.token_hex(24)
        now = time.time_ns()
        lease_until = now + lease * 1_000_000_000
        with self._guard, self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM contribution_publication_state WHERE singleton = 1"
            ).fetchone()
            assert row is not None
            if (
                int(row["live_revision"]) != checked_revision
                or str(row["live_checksum"]) != checksum
            ):
                raise ContributionRepositoryConflict(
                    "The requested repository revision is no longer live."
                )
            if (
                row["repo_revision"] is not None
                and int(row["repo_revision"]) == checked_revision
                and str(row["repo_state"]) == "pushed"
            ):
                raise ContributionRepositoryConflict(
                    "This live revision was already pushed to the repository."
                )
            active_lease = (
                str(row["repo_state"]) == "prepared" and int(row["repo_lease_until"] or 0) > now
            )
            if active_lease:
                raise ContributionRepositoryConflict(
                    "Another repository publication is already in progress."
                )
            recovered = str(row["repo_state"]) == "prepared"
            connection.execute(
                """
                UPDATE contribution_publication_state
                SET repo_revision = ?, repo_checksum = ?, repo_state = 'prepared',
                    repo_token = ?, repo_lease_until = ?, repo_branch = NULL,
                    repo_commit = NULL, repo_error = NULL, updated_at = ?
                WHERE singleton = 1
                """,
                (checked_revision, checksum, token, lease_until, now),
            )
            self._audit_locked(
                connection,
                actor=reviewer,
                action=(
                    "repository_publication_recovered"
                    if recovered
                    else "repository_publication_started"
                ),
                contributor_id=None,
                subject_type="catalog_revision",
                subject_id=str(checked_revision),
                detail={"lease_seconds": lease},
                now=now,
            )
        return token

    def finish_repo_publication(
        self,
        token: str,
        revision: int,
        *,
        state: str,
        actor: str,
        branch: str | None = None,
        commit: str | None = None,
        error: str | None = None,
    ) -> None:
        """Complete only the repository lease identified by ``token``."""
        checked_token = _bounded_text(token, "token", 128)
        checked_revision = _positive_integer(revision, "revision", 2_147_483_647, minimum=0)
        checked_state = _bounded_text(state, "state", 32)
        if checked_state not in {"pushed", "failed"}:
            raise ContributionError("Repository completion state is invalid.")
        reviewer = _bounded_text(actor, "actor", MAX_ACTOR_LENGTH)
        checked_branch = _optional_bounded_text(branch, "branch", 256)
        checked_commit = _optional_bounded_text(commit, "commit", 64)
        checked_error = _optional_bounded_text(error, "error", 1000)
        if checked_state == "pushed" and (checked_branch is None or checked_commit is None):
            raise ContributionError("A pushed publication requires a branch and commit.")
        if checked_state == "failed" and checked_error is None:
            raise ContributionError("A failed publication requires an error.")
        now = time.time_ns()
        with self._guard, self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE contribution_publication_state
                SET repo_state = ?, repo_token = NULL, repo_lease_until = 0,
                    repo_branch = ?, repo_commit = ?, repo_error = ?, updated_at = ?
                WHERE singleton = 1 AND repo_revision = ?
                  AND repo_state = 'prepared' AND repo_token = ?
                """,
                (
                    checked_state,
                    checked_branch,
                    checked_commit,
                    checked_error,
                    now,
                    checked_revision,
                    checked_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ContributionRepositoryConflict(
                    "The repository publication lease is stale or no longer active."
                )
            self._audit_locked(
                connection,
                actor=reviewer,
                action="repository_publication_updated",
                contributor_id=None,
                subject_type="catalog_revision",
                subject_id=str(checked_revision),
                detail={"state": checked_state, "branch": checked_branch},
                now=now,
            )

    def claim_notifications(
        self,
        *,
        limit: int = 10,
        lease_seconds: int = 60,
    ) -> tuple[ClaimedContributionNotification, ...]:
        """Claim deliverable notices and return an opaque token for each lease."""
        bounded = min(_bounded_limit(limit), 100)
        lease = _positive_integer(lease_seconds, "lease_seconds", 3600)
        now = time.time_ns()
        lease_until = now + lease * 1_000_000_000
        with self._guard, self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM contribution_notifications
                WHERE (state IN ('pending', 'failed') AND available_at <= ?)
                   OR (state = 'sending' AND lease_until <= ?)
                ORDER BY available_at, id
                LIMIT ?
                """,
                (now, now, bounded),
            ).fetchall()
            ids = [int(row[0]) for row in rows]
            if ids:
                claims = [(secrets.token_hex(24), identity) for identity in ids]
                placeholders = ",".join("?" for _ in ids)
                connection.executemany(
                    """
                    UPDATE contribution_notifications
                    SET state = 'sending', attempts = attempts + 1,
                        claim_token = ?, lease_until = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    ((token, lease_until, now, identity) for token, identity in claims),
                )
                rows = connection.execute(
                    f"""
                    SELECT * FROM contribution_notifications
                    WHERE id IN ({placeholders}) ORDER BY id
                    """,  # nosec B608 -- bounded integer placeholders
                    tuple(ids),
                ).fetchall()
        return tuple(_claimed_notification(row) for row in rows)

    def mark_notification_sent(self, notification_id: int, claim_token: str) -> None:
        """Complete only the active notification lease identified by its token."""
        identity = _positive_integer(notification_id, "notification_id", 2_147_483_647)
        token = _bounded_text(claim_token, "claim_token", 128)
        now = time.time_ns()
        with self._guard, self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE contribution_notifications
                SET state = 'sent', claim_token = NULL, lease_until = 0,
                    last_error = NULL, updated_at = ?
                WHERE id = ? AND state = 'sending' AND claim_token = ?
                  AND lease_until > ?
                """,
                (now, identity, token, now),
            )
            if cursor.rowcount != 1:
                raise ContributionNotificationConflict(
                    "The notification delivery claim is stale or no longer active."
                )

    def mark_notification_failed(
        self,
        notification_id: int,
        claim_token: str,
        error: str,
    ) -> None:
        """Fail only the active notification lease identified by its token."""
        identity = _positive_integer(notification_id, "notification_id", 2_147_483_647)
        token = _bounded_text(claim_token, "claim_token", 128)
        safe_error = _bounded_text(error, "error", 500)
        now = time.time_ns()
        with self._guard, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT attempts FROM contribution_notifications
                WHERE id = ? AND state = 'sending' AND claim_token = ?
                  AND lease_until > ?
                """,
                (identity, token, now),
            ).fetchone()
            if row is None:
                raise ContributionNotificationConflict(
                    "The notification delivery claim is stale or no longer active."
                )
            delay_seconds = min(3600, 15 * (2 ** min(int(row[0]), 8)))
            cursor = connection.execute(
                """
                UPDATE contribution_notifications
                SET state = 'failed', claim_token = NULL, lease_until = 0,
                    last_error = ?, available_at = ?, updated_at = ?
                WHERE id = ? AND state = 'sending' AND claim_token = ?
                  AND lease_until > ?
                """,
                (
                    safe_error,
                    now + delay_seconds * 1_000_000_000,
                    now,
                    identity,
                    token,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ContributionNotificationConflict(
                    "The notification delivery claim is stale or no longer active."
                )

    def list_notifications(
        self,
        *,
        states: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> tuple[ContributionNotification, ...]:
        allowed = frozenset({"pending", "sending", "failed", "sent"})
        choices = _state_filter(states, allowed, "notification")
        where, parameters = _where_in("state", choices)
        with self._guard:
            rows = (
                self._connection_required()
                .execute(
                    f"""
                SELECT * FROM contribution_notifications {where}
                ORDER BY created_at, id LIMIT ?
                """,  # nosec B608 -- fixed identifier and placeholders
                    (*parameters, _bounded_limit(limit)),
                )
                .fetchall()
            )
        return tuple(_notification(row) for row in rows)

    def list_audit(self, *, limit: int = 1000) -> tuple[dict[str, object], ...]:
        with self._guard:
            rows = (
                self._connection_required()
                .execute(
                    """
                SELECT id, actor, action, contributor_id, subject_type,
                       subject_id, detail_json, created_at
                FROM contribution_audit ORDER BY id DESC LIMIT ?
                """,
                    (_bounded_limit(limit),),
                )
                .fetchall()
            )
        return tuple(
            {
                "id": int(row[0]),
                "actor": str(row[1]),
                "action": str(row[2]),
                "contributor_id": int(row[3]) if row[3] is not None else None,
                "subject_type": str(row[4]),
                "subject_id": str(row[5]),
                "detail": json.loads(str(row[6])),
                "created_at": int(row[7]),
            }
            for row in rows
        )

    def _open(self) -> sqlite3.Connection:
        if self._path is None:
            database = ":memory:"
        else:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            database = str(self._path)
        connection = sqlite3.connect(
            database,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if user_version > 6:
                raise sqlite3.DatabaseError(
                    "Contribution database was created by a newer application."
                )
            if user_version == 0:
                self._create_schema(connection)
            elif user_version == 1:
                self._migrate_v1_to_v2(connection)
                self._migrate_v2_to_v3(connection)
                self._migrate_v3_to_v4(connection)
                self._migrate_v4_to_v5(connection)
            elif user_version == 2:
                self._migrate_v2_to_v3(connection)
                self._migrate_v3_to_v4(connection)
                self._migrate_v4_to_v5(connection)
            elif user_version == 3:
                self._migrate_v3_to_v4(connection)
                self._migrate_v4_to_v5(connection)
            elif user_version == 4:
                self._migrate_v4_to_v5(connection)
            elif user_version == 6:
                self._downgrade_v6_to_v5(connection)
            self._ensure_seed_state(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS contributor_applications (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL CHECK (
                    state IN ('pending','approved','rejected','deferred','revoked')
                ),
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                language_code TEXT,
                requested_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                decided_at INTEGER,
                disclosure_acknowledged_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS contributor_applications_state
                ON contributor_applications (state, requested_at, user_id);

            CREATE TABLE IF NOT EXISTS contribution_canonical_topics (
                topic_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                color TEXT NOT NULL,
                aliases TEXT NOT NULL,
                definition_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                actor TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contributor_source_topics (
                contributor_id INTEGER NOT NULL,
                local_topic_id TEXT NOT NULL,
                name TEXT,
                color TEXT,
                aliases TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL CHECK (
                    state IN ('pending','mapped','rejected','deferred')
                ),
                canonical_topic_id TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (contributor_id, local_topic_id),
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
            CREATE INDEX IF NOT EXISTS contributor_source_topics_state
                ON contributor_source_topics (state, updated_at);

            CREATE TABLE IF NOT EXISTS contribution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contributor_id INTEGER NOT NULL,
                client_event_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (
                    event_type IN ('topic_upsert','topic_delete','verse_add','verse_remove')
                ),
                local_topic_id TEXT NOT NULL,
                topic_name TEXT,
                topic_color TEXT,
                book INTEGER,
                chapter INTEGER,
                verse INTEGER,
                payload_json TEXT NOT NULL,
                payload_digest BLOB NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('pending','approved','rejected','deferred','applied')
                ),
                canonical_topic_id TEXT,
                replay_count INTEGER NOT NULL DEFAULT 0,
                submitted_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                decided_at INTEGER,
                UNIQUE (contributor_id, client_event_id),
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id),
                FOREIGN KEY (contributor_id, local_topic_id)
                    REFERENCES contributor_source_topics(contributor_id, local_topic_id)
            );
            CREATE INDEX IF NOT EXISTS contribution_events_review
                ON contribution_events (state, submitted_at, id);
            CREATE INDEX IF NOT EXISTS contribution_events_topic
                ON contribution_events (
                    contributor_id, local_topic_id, state, submitted_at
                );

            CREATE TABLE IF NOT EXISTS contribution_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contributor_id INTEGER,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                previous_state TEXT NOT NULL,
                decision TEXT NOT NULL,
                canonical_topic_id TEXT,
                note TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS contribution_decisions_subject
                ON contribution_decisions (subject_type, subject_id, created_at);

            CREATE TABLE IF NOT EXISTS contribution_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                contributor_id INTEGER,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS contribution_audit_created
                ON contribution_audit (created_at, id);

            CREATE TABLE IF NOT EXISTS contribution_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contributor_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('pending','sending','failed','sent')
                ),
                attempts INTEGER NOT NULL,
                available_at INTEGER NOT NULL,
                lease_until INTEGER NOT NULL DEFAULT 0,
                claim_token TEXT,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
            CREATE INDEX IF NOT EXISTS contribution_notifications_delivery
                ON contribution_notifications (state, available_at, id);

            CREATE TABLE IF NOT EXISTS contribution_catalog_revisions (
                revision INTEGER PRIMARY KEY,
                checksum TEXT NOT NULL,
                catalog_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                actor TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contribution_publication_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                live_revision INTEGER NOT NULL,
                live_checksum TEXT NOT NULL,
                live_updated_at INTEGER NOT NULL,
                repo_revision INTEGER,
                repo_state TEXT,
                repo_checksum TEXT,
                repo_token TEXT,
                repo_lease_until INTEGER NOT NULL DEFAULT 0,
                repo_branch TEXT,
                repo_commit TEXT,
                repo_error TEXT,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contribution_client_snapshots (
                contributor_id INTEGER NOT NULL,
                client_id TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                snapshot_digest BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (contributor_id, client_id),
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );

            CREATE TABLE IF NOT EXISTS contribution_sync_receipts (
                contributor_id INTEGER NOT NULL,
                sync_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                request_digest BLOB NOT NULL,
                accepted INTEGER NOT NULL,
                replayed INTEGER NOT NULL,
                event_ids_json TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (contributor_id, sync_id),
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
            CREATE INDEX IF NOT EXISTS contribution_sync_receipts_created
                ON contribution_sync_receipts (created_at, contributor_id);

            CREATE TABLE IF NOT EXISTS contributor_capabilities (
                token_digest BLOB PRIMARY KEY,
                contributor_id INTEGER NOT NULL,
                issued_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
            CREATE INDEX IF NOT EXISTS contributor_capabilities_owner
                ON contributor_capabilities (contributor_id, expires_at, last_used_at);
            PRAGMA user_version=5;
            COMMIT;
            """
        )

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        """Allow a later revision to intentionally restore older content.

        Version 1 made catalogue checksums unique.  That prevented the valid
        revision sequence A -> B -> A (for example, publishing a topic and
        later accepting its deletion).  Revisions remain monotonic; identical
        content is only a no-op when it is already the live revision.
        """
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            ALTER TABLE contribution_catalog_revisions
                RENAME TO contribution_catalog_revisions_v1;
            CREATE TABLE contribution_catalog_revisions (
                revision INTEGER PRIMARY KEY,
                checksum TEXT NOT NULL,
                catalog_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                actor TEXT NOT NULL
            );
            INSERT INTO contribution_catalog_revisions (
                revision, checksum, catalog_json, created_at, actor
            )
            SELECT revision, checksum, catalog_json, created_at, actor
            FROM contribution_catalog_revisions_v1
            ORDER BY revision;
            DROP TABLE contribution_catalog_revisions_v1;
            PRAGMA user_version=2;
            COMMIT;
            """
        )

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        """Add the repository publication compare-and-swap lease fields."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(contribution_publication_state)"
                ).fetchall()
            }
            additions = {
                "repo_checksum": "TEXT",
                "repo_token": "TEXT",
                "repo_lease_until": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE contribution_publication_state "
                        f"ADD COLUMN {name} {declaration}"
                    )
            connection.execute("PRAGMA user_version=3")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        """Add per-claim capabilities to notification delivery leases."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(contribution_notifications)"
                ).fetchall()
            }
            if "claim_token" not in columns:
                connection.execute(
                    "ALTER TABLE contribution_notifications ADD COLUMN claim_token TEXT"
                )
            # Pre-v4 workers never received a completion capability. Make any
            # lease they left behind immediately reclaimable by a v4 worker.
            connection.execute(
                """
                UPDATE contribution_notifications
                SET claim_token = NULL, lease_until = 0
                WHERE state = 'sending'
                """
            )
            connection.execute("PRAGMA user_version=4")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    @staticmethod
    def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
        """Add atomic snapshot receipts and durable contributor capabilities."""
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS contribution_client_snapshots (
                contributor_id INTEGER NOT NULL,
                client_id TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                snapshot_digest BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (contributor_id, client_id),
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
            CREATE TABLE IF NOT EXISTS contribution_sync_receipts (
                contributor_id INTEGER NOT NULL,
                sync_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                request_digest BLOB NOT NULL,
                accepted INTEGER NOT NULL,
                replayed INTEGER NOT NULL,
                event_ids_json TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (contributor_id, sync_id),
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
            CREATE INDEX IF NOT EXISTS contribution_sync_receipts_created
                ON contribution_sync_receipts (created_at, contributor_id);
            CREATE TABLE IF NOT EXISTS contributor_capabilities (
                token_digest BLOB PRIMARY KEY,
                contributor_id INTEGER NOT NULL,
                issued_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
            CREATE INDEX IF NOT EXISTS contributor_capabilities_owner
                ON contributor_capabilities (contributor_id, expires_at, last_used_at);
            PRAGMA user_version=5;
            COMMIT;
            """
        )

    @staticmethod
    def _downgrade_v6_to_v5(connection: sqlite3.Connection) -> None:
        """Return a database left at v6 by the withdrawn push transport to v5.

        The v6 schema only added durable staging for chunked Telegram
        web_app_data push bundles; no surviving feature reads those tables, so
        dropping them (children before parents, because foreign keys are
        enforced on this connection) restores the exact v5 layout without
        touching any contributor, event, or catalogue state.
        """
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS contribution_push_chunks;
            DROP INDEX IF EXISTS contribution_push_bundles_expiry;
            DROP TABLE IF EXISTS contribution_push_bundles;
            PRAGMA user_version=5;
            COMMIT;
            """
        )

    @staticmethod
    def _ensure_seed_state(connection: sqlite3.Connection) -> None:
        """Idempotently repair and validate mandatory catalogue seed rows."""
        empty = normalize_catalog(
            {
                "schema_version": 1,
                "topics": [],
                "associations": {"add": [], "remove": []},
            }
        )
        encoded_empty = _json(empty)
        empty_checksum = hashlib.sha256(encoded_empty.encode("utf-8")).hexdigest()
        now = time.time_ns()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                INSERT OR IGNORE INTO contribution_catalog_revisions (
                    revision, checksum, catalog_json, created_at, actor
                ) VALUES (0, ?, ?, ?, 'system')
                """,
                (empty_checksum, encoded_empty, now),
            )
            seed = connection.execute(
                """
                SELECT checksum, catalog_json, actor
                FROM contribution_catalog_revisions WHERE revision = 0
                """
            ).fetchone()
            if (
                seed is None
                or str(seed[0]) != empty_checksum
                or str(seed[1]) != encoded_empty
                or str(seed[2]) != "system"
            ):
                raise sqlite3.DatabaseError("Contribution catalogue seed is invalid.")

            latest = connection.execute(
                """
                SELECT revision, checksum, created_at
                FROM contribution_catalog_revisions
                ORDER BY revision DESC LIMIT 1
                """
            ).fetchone()
            assert latest is not None
            connection.execute(
                """
                INSERT OR IGNORE INTO contribution_publication_state (
                    singleton, live_revision, live_checksum, live_updated_at,
                    repo_revision, repo_state, repo_checksum, repo_token,
                    repo_lease_until, repo_branch, repo_commit, repo_error,
                    updated_at
                ) VALUES (
                    1, ?, ?, ?, NULL, NULL, NULL, NULL, 0,
                    NULL, NULL, NULL, ?
                )
                """,
                (int(latest[0]), str(latest[1]), int(latest[2]), now),
            )
            publication = connection.execute(
                """
                SELECT state.live_checksum, revision.checksum
                FROM contribution_publication_state AS state
                LEFT JOIN contribution_catalog_revisions AS revision
                    ON revision.revision = state.live_revision
                WHERE state.singleton = 1
                """
            ).fetchone()
            if (
                publication is None
                or publication[1] is None
                or str(publication[0]) != str(publication[1])
            ):
                raise sqlite3.DatabaseError("Contribution publication seed is invalid.")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def _connection_required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Contribution store is closed.")
        return self._connection

    def _publish_catalog_locked(
        self,
        connection: sqlite3.Connection,
        *,
        normalized: dict[str, Any],
        encoded: str,
        checksum: str,
        actor: str,
        now: int,
    ) -> CatalogRevision:
        previous = connection.execute(
            """
            SELECT revision, checksum, catalog_json, created_at, actor
            FROM contribution_catalog_revisions
            ORDER BY revision DESC LIMIT 1
            """
        ).fetchone()
        assert previous is not None
        if str(previous[1]) == checksum:
            return CatalogRevision(
                revision=int(previous[0]),
                checksum=str(previous[1]),
                catalog=cast(dict[str, Any], json.loads(str(previous[2]))),
                created_at=int(previous[3]),
                actor=str(previous[4]),
            )
        revision = int(previous[0]) + 1
        connection.execute(
            """
            INSERT INTO contribution_catalog_revisions (
                revision, checksum, catalog_json, created_at, actor
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (revision, checksum, encoded, now, actor),
        )
        connection.execute(
            """
            UPDATE contribution_publication_state
            SET live_revision = ?, live_checksum = ?, live_updated_at = ?,
                updated_at = ?
            WHERE singleton = 1
            """,
            (revision, checksum, now, now),
        )
        self._audit_locked(
            connection,
            actor=actor,
            action="catalog_published",
            contributor_id=None,
            subject_type="catalog_revision",
            subject_id=str(revision),
            detail={"checksum": checksum},
            now=now,
        )
        return CatalogRevision(revision, checksum, normalized, now, actor)

    class _Transaction:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def __enter__(self) -> sqlite3.Connection:
            self._connection.execute("BEGIN IMMEDIATE")
            return self._connection

        def __exit__(self, error_type: object, error: object, traceback: object) -> None:
            self._connection.execute("ROLLBACK" if error_type is not None else "COMMIT")

    def _transaction(self) -> ContributionStore._Transaction:
        return self._Transaction(self._connection_required())

    @staticmethod
    def _audit_locked(
        connection: sqlite3.Connection,
        *,
        actor: str,
        action: str,
        contributor_id: int | None,
        subject_type: str,
        subject_id: str,
        detail: Mapping[str, object],
        now: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO contribution_audit (
                actor, action, contributor_id, subject_type, subject_id,
                detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _bounded_text(actor, "actor", MAX_ACTOR_LENGTH),
                _bounded_text(action, "action", 80),
                contributor_id,
                _bounded_text(subject_type, "subject_type", 40),
                _bounded_text(subject_id, "subject_id", 256),
                _json(dict(detail)),
                now,
            ),
        )


def normalize_catalog(value: Mapping[str, object]) -> dict[str, Any]:
    """Validate and deterministically sort the public catalogue overlay schema."""
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "topics",
        "associations",
    }:
        raise ContributionError("Catalogue must contain schema_version, topics, associations.")
    if value.get("schema_version") != 1:
        raise ContributionError("Catalogue schema_version must be 1.")
    raw_topics = value.get("topics")
    raw_associations = value.get("associations")
    if not isinstance(raw_topics, list) or len(raw_topics) > MAX_PUBLIC_OVERLAY_TOPICS:
        raise ContributionError("Catalogue topics must be a bounded array.")
    if not isinstance(raw_associations, Mapping) or set(raw_associations) != {
        "add",
        "remove",
    }:
        raise ContributionError("Catalogue associations must contain add and remove.")
    topics: list[dict[str, object]] = []
    topic_ids: set[str] = set()
    topic_names: dict[str, str] = {}
    for raw in raw_topics:
        if not isinstance(raw, Mapping) or set(raw) != {"id", "name", "color", "aliases"}:
            raise ContributionError("Catalogue topic has unsupported fields.")
        topic_id = _canonical_topic_id(raw.get("id"))
        if topic_id in topic_ids:
            raise ContributionError("Catalogue topic IDs must be unique.")
        topic_ids.add(topic_id)
        name = _topic_name(raw.get("name"))
        aliases = _aliases(raw.get("aliases"))
        for candidate in (name, *aliases):
            normalized_name = " ".join(candidate.lower().split())
            owner = topic_names.get(normalized_name)
            if owner is not None:
                raise ContributionError(
                    f"Catalogue reuses one English topic name for {owner} and {topic_id}."
                )
            topic_names[normalized_name] = topic_id
        topics.append(
            {
                "id": topic_id,
                "name": name,
                "color": _color(raw.get("color")),
                "aliases": list(aliases),
            }
        )
    topics.sort(key=lambda topic: cast(str, topic["id"]))
    associations: dict[str, list[dict[str, object]]] = {}
    seen_operations: dict[tuple[str, int, int, int], str] = {}
    for operation in ("add", "remove"):
        raw_items = raw_associations.get(operation)
        if not isinstance(raw_items, list) or len(raw_items) > MAX_PUBLIC_OVERLAY_ASSOCIATIONS:
            raise ContributionError(f"Catalogue associations.{operation} is invalid.")
        items: list[dict[str, object]] = []
        seen: set[tuple[str, int, int, int]] = set()
        for raw in raw_items:
            if not isinstance(raw, Mapping) or set(raw) != {
                "topic_id",
                "book",
                "chapter",
                "verse",
            }:
                raise ContributionError("Catalogue association has unsupported fields.")
            item: dict[str, object] = {
                "topic_id": _canonical_topic_id(raw.get("topic_id")),
            }
            book, chapter, verse = _scripture_coordinate(
                raw.get("book"),
                raw.get("chapter"),
                raw.get("verse"),
            )
            item.update({"book": book, "chapter": chapter, "verse": verse})
            key = (
                cast(str, item["topic_id"]),
                cast(int, item["book"]),
                cast(int, item["chapter"]),
                cast(int, item["verse"]),
            )
            if key in seen:
                continue
            other = seen_operations.get(key)
            if other is not None and other != operation:
                raise ContributionError("One association cannot be both added and removed.")
            seen.add(key)
            seen_operations[key] = operation
            items.append(item)
        items.sort(
            key=lambda item: (
                cast(str, item["topic_id"]),
                cast(int, item["book"]),
                cast(int, item["chapter"]),
                cast(int, item["verse"]),
            )
        )
        associations[operation] = items
    if sum(len(items) for items in associations.values()) > MAX_PUBLIC_OVERLAY_ASSOCIATIONS:
        raise ContributionError("Catalogue contains too many association changes.")
    added_topic_ids = {cast(str, association["topic_id"]) for association in associations["add"]}
    if topic_ids - added_topic_ids:
        raise ContributionError(
            "Every contributed topic requires at least one effective verse association."
        )
    normalized = {
        "schema_version": 1,
        "topics": topics,
        "associations": associations,
    }
    if len(_json(normalized).encode("utf-8")) > MAX_PUBLIC_OVERLAY_BYTES:
        raise ContributionError("Catalogue exceeds the public overlay byte limit.")
    return normalized


def _normalize_event(raw: Mapping[str, object]) -> NormalizedEvent:
    if not isinstance(raw, Mapping):
        raise ContributionError("Each contribution event must be an object.")
    if set(raw) != {"client_event_id", "type", "topic"} and set(raw) != {
        "client_event_id",
        "type",
        "topic",
        "verse",
    }:
        raise ContributionError("Contribution event contains unsupported fields.")
    client_event_id = _safe_id(raw.get("client_event_id"), "client_event_id")
    event_type = raw.get("type")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise ContributionError("Contribution event type is invalid.")
    raw_topic = raw.get("topic")
    if not isinstance(raw_topic, Mapping):
        raise ContributionError("Contribution event topic must be an object.")
    local_topic_id = _safe_id(raw_topic.get("local_topic_id"), "local_topic_id")
    topic_name: str | None = None
    topic_color: str | None = None
    book: int | None = None
    chapter: int | None = None
    verse: int | None = None
    if event_type == "topic_upsert":
        if set(raw_topic) != {"local_topic_id", "name", "color"} or "verse" in raw:
            raise ContributionError("topic_upsert requires local_topic_id, name, and color.")
        topic_name = _topic_name(raw_topic.get("name"))
        topic_color = _color(raw_topic.get("color"))
    elif event_type == "topic_delete":
        if set(raw_topic) != {"local_topic_id"} or "verse" in raw:
            raise ContributionError("topic_delete accepts only local_topic_id.")
    else:
        topic_fields = set(raw_topic)
        if topic_fields == {"local_topic_id", "name", "color"}:
            # Raw English metadata is review context for a previously untouched
            # bundled topic. It never changes canonical data and, unlike an
            # explicit topic_upsert, never reopens an existing mapping.
            topic_name = _topic_name(raw_topic.get("name"))
            topic_color = _color(raw_topic.get("color"))
        elif topic_fields != {"local_topic_id"}:
            raise ContributionError(
                "Verse event topic must contain an ID with optional name and color."
            )
        raw_verse = raw.get("verse")
        if not isinstance(raw_verse, Mapping) or set(raw_verse) != {
            "book",
            "chapter",
            "verse",
        }:
            raise ContributionError("Verse event coordinates are invalid.")
        book, chapter, verse = _scripture_coordinate(
            raw_verse.get("book"),
            raw_verse.get("chapter"),
            raw_verse.get("verse"),
        )
    normalized: dict[str, object] = {
        "client_event_id": client_event_id,
        "type": event_type,
        "topic": {"local_topic_id": local_topic_id},
    }
    if topic_name is not None and topic_color is not None:
        normalized["topic"] = {
            "local_topic_id": local_topic_id,
            "name": topic_name,
            "color": topic_color,
        }
    if book is not None and chapter is not None and verse is not None:
        normalized["verse"] = {"book": book, "chapter": chapter, "verse": verse}
    payload_json = _json(normalized)
    digest = hashlib.sha256(payload_json.encode("utf-8")).digest()
    return (
        client_event_id,
        cast(EventType, event_type),
        local_topic_id,
        topic_name,
        topic_color,
        book,
        chapter,
        verse,
        payload_json,
        digest,
    )


def _application(row: sqlite3.Row) -> ContributorApplication:
    return ContributorApplication(
        user_id=int(row["user_id"]),
        state=cast(ApplicationState, row["state"]),
        first_name=cast(str | None, row["first_name"]),
        last_name=cast(str | None, row["last_name"]),
        username=cast(str | None, row["username"]),
        language_code=cast(str | None, row["language_code"]),
        requested_at=int(row["requested_at"]),
        updated_at=int(row["updated_at"]),
        decided_at=int(row["decided_at"]) if row["decided_at"] is not None else None,
        disclosure_acknowledged_at=(
            int(row["disclosure_acknowledged_at"])
            if row["disclosure_acknowledged_at"] is not None
            else None
        ),
    )


def _source_topic(row: sqlite3.Row) -> SourceTopicRecord:
    aliases = json.loads(str(row["aliases"]))
    definition_json = row["canonical_definition_json"]
    return SourceTopicRecord(
        contributor_id=int(row["contributor_id"]),
        local_topic_id=str(row["local_topic_id"]),
        name=cast(str | None, row["name"]),
        color=cast(str | None, row["color"]),
        aliases=tuple(str(value) for value in aliases),
        state=cast(TopicState, row["state"]),
        canonical_topic_id=cast(str | None, row["canonical_topic_id"]),
        canonical_definition=(
            cast(dict[str, object], json.loads(str(definition_json)))
            if definition_json is not None
            else None
        ),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _private_canonical_topic(
    row: sqlite3.Row,
    canonical_topic_id: str,
) -> dict[str, object] | None:
    """Return identity-free canonical metadata for one owner's reconciliation.

    New contributed definitions have their own immutable moderation record.
    Existing bundled definitions do not; while the source is mapped its
    moderator-normalized source fields are safe as a convenience.  If a later
    proposal reopens such a bundled mapping, those source fields are proposal
    data again and the browser must use the freshly loaded public catalogue.
    """
    definition_json = row["canonical_definition_json"]
    if definition_json is not None:
        definition = json.loads(str(definition_json))
        if isinstance(definition, dict):
            return cast(dict[str, object], definition)
    if row["state"] != "mapped" or row["name"] is None or row["color"] is None:
        return None
    aliases = json.loads(str(row["aliases"]))
    return {
        "id": canonical_topic_id,
        "name": str(row["name"]),
        "color": str(row["color"]),
        "aliases": [str(value) for value in aliases],
    }


def _event(row: sqlite3.Row) -> ContributionEvent:
    return ContributionEvent(
        id=int(row["id"]),
        contributor_id=int(row["contributor_id"]),
        client_event_id=str(row["client_event_id"]),
        event_type=cast(EventType, row["event_type"]),
        local_topic_id=str(row["local_topic_id"]),
        topic_name=cast(str | None, row["topic_name"]),
        topic_color=cast(str | None, row["topic_color"]),
        book=int(row["book"]) if row["book"] is not None else None,
        chapter=int(row["chapter"]) if row["chapter"] is not None else None,
        verse=int(row["verse"]) if row["verse"] is not None else None,
        payload_digest=bytes(row["payload_digest"]).hex(),
        state=cast(ReviewState, row["state"]),
        canonical_topic_id=cast(str | None, row["canonical_topic_id"]),
        replay_count=int(row["replay_count"]),
        submitted_at=int(row["submitted_at"]),
        updated_at=int(row["updated_at"]),
        decided_at=int(row["decided_at"]) if row["decided_at"] is not None else None,
    )


def _notification(row: sqlite3.Row) -> ContributionNotification:
    return ContributionNotification(
        id=int(row["id"]),
        contributor_id=int(row["contributor_id"]),
        kind=str(row["kind"]),
        message=str(row["message"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        available_at=int(row["available_at"]),
        created_at=int(row["created_at"]),
    )


def _claimed_notification(row: sqlite3.Row) -> ClaimedContributionNotification:
    token = row["claim_token"]
    if not isinstance(token, str) or not token:
        raise sqlite3.DatabaseError("Claimed notification is missing its claim token.")
    return ClaimedContributionNotification(
        id=int(row["id"]),
        contributor_id=int(row["contributor_id"]),
        kind=str(row["kind"]),
        message=str(row["message"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        available_at=int(row["available_at"]),
        created_at=int(row["created_at"]),
        claim_token=token,
    )


def _telegram_user_id(value: object) -> int:
    return _positive_integer(value, "Telegram user ID", MAX_TELEGRAM_ID)


def _positive_integer(
    value: object,
    label: str,
    maximum: int,
    *,
    minimum: int = 1,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ContributionError(f"{label} must be between {minimum} and {maximum}.")
    return value


def _scripture_coordinate(
    raw_book: object,
    raw_chapter: object,
    raw_verse: object,
) -> tuple[int, int, int]:
    book = _positive_integer(raw_book, "book", len(BOOK_CHAPTER_COUNTS))
    chapter = _positive_integer(
        raw_chapter,
        "chapter",
        BOOK_CHAPTER_COUNTS[book - 1],
    )
    verse = _positive_integer(raw_verse, "verse", 2000)
    return book, chapter, verse


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise ContributionError(f"{label} must be a safe 1-{MAX_SAFE_ID_LENGTH} character ID.")
    return value


MAX_SAFE_ID_LENGTH = 128


def _canonical_topic_id(value: object) -> str:
    if not isinstance(value, str) or _CANONICAL_TOPIC_RE.fullmatch(value) is None:
        raise ContributionError("Canonical topic ID is invalid.")
    return value


def _canonical_definition(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"id", "name", "color", "aliases"}:
        raise ContributionError("canonical_definition contains unsupported fields.")
    name = _topic_name(value.get("name"))
    aliases = _aliases(value.get("aliases"))
    if any(alias.casefold() == name.casefold() for alias in aliases):
        raise ContributionError("canonical_definition repeats its name as an alias.")
    return {
        "id": _canonical_topic_id(value.get("id")),
        "name": name,
        "color": _color(value.get("color")),
        "aliases": list(aliases),
    }


def _topic_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 2 <= len(value) <= MAX_TOPIC_NAME
        or value != value.strip()
        or value != unicodedata.normalize("NFC", value)
        or re.search(r"\s{2,}", value) is not None
        or _ENGLISH_TOPIC_RE.fullmatch(value) is None
        or re.search(r"[A-Za-z]", value) is None
    ):
        raise ContributionError("Topic name must be a bounded English topic name.")
    return value


def _color(value: object) -> str:
    if not isinstance(value, str) or _COLOR_RE.fullmatch(value) is None:
        raise ContributionError("Topic color must use #RRGGBB.")
    return value.lower()


def _aliases(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_TOPIC_ALIASES:
        raise ContributionError("Topic aliases must be a bounded array.")
    aliases = tuple(_topic_name(alias) for alias in value)
    if len(set(alias.casefold() for alias in aliases)) != len(aliases):
        raise ContributionError("Topic aliases must be unique.")
    return tuple(sorted(aliases, key=lambda alias: (alias.casefold(), alias)))


def _profile(
    first_name: str | None,
    last_name: str | None,
    username: str | None,
    language_code: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    return (
        _optional_bounded_text(first_name, "first_name", 64),
        _optional_bounded_text(last_name, "last_name", 64),
        _optional_bounded_text(username, "username", 64),
        _optional_bounded_text(language_code, "language_code", 16),
    )


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ContributionError(f"{label} must be text.")
    result = value.strip()
    if (
        not result
        or len(result) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
    ):
        raise ContributionError(f"{label} must contain between 1 and {maximum} characters.")
    return result


def _optional_bounded_text(value: object | None, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _bounded_text(value, label, maximum)


def _state_filter(
    values: Iterable[str] | None,
    allowed: frozenset[str],
    label: str,
) -> tuple[str, ...]:
    if values is None:
        return ()
    result = tuple(dict.fromkeys(values))
    if not result or any(value not in allowed for value in result):
        raise ContributionError(f"{label} state filter is invalid.")
    return tuple(sorted(result))


def _where_in(column: str, choices: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    if not choices:
        return "", ()
    placeholders = ",".join("?" for _ in choices)
    return f"WHERE {column} IN ({placeholders})", tuple(choices)


def _bounded_limit(value: object) -> int:
    return _positive_integer(value, "limit", 10_000)


def _constant_digest_equal(left: bytes, right: bytes) -> bool:
    if len(left) != len(right):
        return False
    mismatch = 0
    for first, second in zip(left, right, strict=True):
        mismatch |= first ^ second
    return mismatch == 0


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def valid_checksum(value: object) -> bool:
    """Return whether a public catalogue checksum has the expected safe form."""
    return isinstance(value, str) and _CHECKSUM_RE.fullmatch(value) is not None
