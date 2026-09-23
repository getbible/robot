import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from modules.contributions import (
    LIVE_NOTIFICATION_KIND,
    MAX_LIVE_CATALOG_TOPICS,
    ContributionError,
    ContributionIdempotencyConflict,
    ContributionNotAllowed,
    ContributionNotificationConflict,
    ContributionPublicationConflict,
    ContributionRepositoryConflict,
    ContributionStore,
    normalize_catalog,
)

PULL_REQUEST_URL = "https://github.com/getbible/v1_bookmark_builder/pull/12"


def _topic_event(
    event_id: str = "topic.grace.v1",
    *,
    name: str = "Grace",
    color: str = "#BBF7D0",
) -> dict[str, object]:
    return {
        "client_event_id": event_id,
        "type": "topic_upsert",
        "topic": {
            "local_topic_id": "local.grace",
            "name": name,
            "color": color,
        },
    }


def _verse_event(
    event_id: str = "verse.grace.43.3.16.add",
    *,
    operation: str = "verse_add",
    book: int = 43,
    chapter: int = 3,
    verse: int = 16,
) -> dict[str, object]:
    return {
        "client_event_id": event_id,
        "type": operation,
        "topic": {"local_topic_id": "local.grace"},
        "verse": {"book": book, "chapter": chapter, "verse": verse},
    }


def _catalog() -> dict[str, object]:
    return {
        "schema_version": 1,
        "topics": [{"id": "grace", "name": "Grace", "color": "#bbf7d0", "aliases": []}],
        "associations": {
            "add": [{"topic_id": "grace", "book": 43, "chapter": 3, "verse": 16}],
            "remove": [],
        },
    }


def _empty_catalog() -> dict[str, object]:
    return {
        "schema_version": 1,
        "topics": [],
        "associations": {"add": [], "remove": []},
    }


def _live_topic(
    topic_id: str = "grace",
    *,
    name: str = "Grace",
    color: str = "#bbf7d0",
    aliases: tuple[str, ...] = (),
    default: bool = False,
    verses: tuple[tuple[int, int, int], ...] = ((43, 3, 16),),
) -> dict[str, object]:
    """One topic exactly as ``BookmarkTopic.as_dict()`` returns it."""
    return {
        "id": topic_id,
        "name": name,
        "color": color,
        "aliases": list(aliases),
        "default": default,
        "verses": [list(triple) for triple in verses],
    }


def _checksum(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _snapshot(
    *,
    topic_id: str = "local.grace",
    name: str = "Grace",
    color: str = "#BBF7D0",
    assignments: tuple[tuple[int, int, int], ...] = ((43, 3, 16),),
) -> dict[str, object]:
    return {
        "topics": [{"id": topic_id, "name": name, "color": color}],
        "assignments": [
            {
                "topic_id": topic_id,
                "book": book,
                "chapter": chapter,
                "verse": verse,
            }
            for book, chapter, verse in assignments
        ],
    }


class ContributionStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "private" / "contributions.sqlite3"
        self.store = ContributionStore(path=str(self.path))

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def approve(self, user_id: int = 42) -> None:
        application, created = self.store.submit_application(
            user_id,
            first_name="Grace",
            username="grace_reader",
        )
        self.assertTrue(created)
        self.assertEqual(application.state, "pending")
        self.store.decide_application(user_id, "approved", actor="admin")
        self.store.acknowledge_disclosure(user_id)

    def test_ordinary_reader_status_has_no_private_work_or_identity(self) -> None:
        statements: list[str] = []
        connection = self.store._connection_required()
        connection.set_trace_callback(statements.append)
        try:
            status = self.store.contribution_status(999)
        finally:
            connection.set_trace_callback(None)

        self.assertEqual(
            status,
            {
                "enabled": True,
                "state": "not_applied",
                "can_contribute": False,
                "disclosure_required": False,
                "topics": [],
                "summary": {
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
                        "live": 0,
                    },
                },
            },
        )
        selects = [
            statement
            for statement in statements
            if statement.lstrip().startswith("SELECT")
        ]
        self.assertEqual(len(selects), 1)
        self.assertIn("FROM contributor_applications", selects[0])

    def test_identity_observation_does_not_lock_writer_for_ordinary_reader(self) -> None:
        statements: list[str] = []
        connection = self.store._connection_required()
        connection.set_trace_callback(statements.append)
        try:
            self.store.observe_identity(999, first_name="Ordinary")
        finally:
            connection.set_trace_callback(None)

        self.assertEqual(len(statements), 1)
        self.assertIn("FROM contributor_applications", statements[0])
        self.assertFalse(
            any(
                statement.lstrip().startswith(
                    ("BEGIN", "COMMIT", "UPDATE", "INSERT")
                )
                for statement in statements
            )
        )

    def test_identity_observation_skips_unchanged_contributor_profile(self) -> None:
        self.store.submit_application(42, first_name="Grace")
        statements: list[str] = []
        connection = self.store._connection_required()
        connection.set_trace_callback(statements.append)
        try:
            self.store.observe_identity(42, first_name="Grace")
        finally:
            connection.set_trace_callback(None)

        self.assertEqual(len(statements), 1)
        self.assertIn("FROM contributor_applications", statements[0])
        self.assertFalse(
            any(
                statement.lstrip().startswith(
                    ("BEGIN", "COMMIT", "UPDATE", "INSERT")
                )
                for statement in statements
            )
        )

    def test_disclosure_acknowledgement_is_write_and_audit_idempotent(self) -> None:
        self.store.submit_application(42, first_name="Grace")
        self.store.decide_application(42, "approved", actor="admin")

        first_status = self.store.acknowledge_disclosure(42)
        first_application = self.store.application_for(42)
        assert first_application is not None
        first_audit = [
            item
            for item in self.store.list_audit()
            if item["action"] == "disclosure_acknowledged"
        ]

        statements: list[str] = []
        connection = self.store._connection_required()
        connection.set_trace_callback(statements.append)
        try:
            repeated_status = self.store.acknowledge_disclosure(42)
        finally:
            connection.set_trace_callback(None)
        repeated_application = self.store.application_for(42)
        assert repeated_application is not None
        repeated_audit = [
            item
            for item in self.store.list_audit()
            if item["action"] == "disclosure_acknowledged"
        ]

        self.assertFalse(first_status["disclosure_required"])
        self.assertEqual(repeated_status, first_status)
        self.assertEqual(repeated_application.updated_at, first_application.updated_at)
        self.assertEqual(
            repeated_application.disclosure_acknowledged_at,
            first_application.disclosure_acknowledged_at,
        )
        self.assertEqual(len(first_audit), 1)
        self.assertEqual(repeated_audit, first_audit)
        self.assertFalse(
            any(
                statement.lstrip().startswith(
                    ("BEGIN", "COMMIT", "UPDATE", "INSERT")
                )
                for statement in statements
            )
        )

    def test_browser_safe_separator_prefixed_ids_are_accepted(self) -> None:
        self.approve()

        for index, prefix in enumerate("._:-", start=1):
            result = self.store.record_events(
                42,
                [
                    {
                        "client_event_id": f"{prefix}imported-event-{index}",
                        "type": "topic_upsert",
                        "topic": {
                            "local_topic_id": f"{prefix}imported-topic-{index}",
                            "name": f"Imported Topic {index}",
                            "color": "#bbf7d0",
                        },
                    }
                ],
            )
            self.assertEqual(result.accepted, 1)

        self.assertEqual(
            {topic.local_topic_id for topic in self.store.list_source_topics()},
            {
                ".imported-topic-1",
                "_imported-topic-2",
                ":imported-topic-3",
                "-imported-topic-4",
            },
        )

    def test_database_is_durable_wal_versioned_and_reopens(self) -> None:
        self.approve()
        self.store.close()

        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)

        self.store = ContributionStore(path=str(self.path))
        application = self.store.application_for(42)
        self.assertIsNotNone(application)
        assert application is not None
        self.assertEqual(application.state, "approved")

    def test_accepted_revision_history_is_paged_and_does_not_mutate_the_ledger(self) -> None:
        first = self.store.publish_catalog(_catalog(), actor="first")
        second = self.store.publish_catalog(_empty_catalog(), actor="second")
        third = self.store.publish_catalog(_catalog(), actor="third")
        self.assertEqual(
            [record.revision for record in self.store.list_catalog_revisions(limit=2)],
            [third.revision, second.revision],
        )
        self.assertEqual(
            self.store.list_catalog_revisions(before_revision=second.revision), (first,)
        )
        self.assertEqual(self.store.catalog_revision(first.revision), first)
        self.assertIsNone(self.store.catalog_revision(999))
        self.assertEqual(self.store.current_catalog(), third)
        for invalid in (True, -1, "1"):
            with self.subTest(invalid=invalid), self.assertRaises(ContributionError):
                self.store.catalog_revision(invalid)  # type: ignore[arg-type]
        with self.assertRaises(ContributionError):
            self.store.list_catalog_revisions(limit=101)

    def test_acceptance_provenance_survives_retries_and_orders_deferred_events(self) -> None:
        self.approve()
        recorded = self.store.record_events(
            42,
            [
                _topic_event(),
                _verse_event("older-remove", operation="verse_remove"),
                _verse_event("newer-add"),
            ],
        )
        older = recorded.event_ids["older-remove"]
        newer = recorded.event_ids["newer-add"]
        self.store.decide_event(older, "deferred", actor="admin")
        self.store.decide_event(newer, "approved", canonical_topic_id="grace", actor="admin")
        first = self.store.publish_approved_events_atomically(_catalog(), [newer], actor="admin")
        self.store.decide_event(older, "approved", canonical_topic_id="grace", actor="admin")
        second = self.store.publish_approved_events_atomically(
            _empty_catalog(), [older], actor="admin"
        )
        with sqlite3.connect(self.path) as connection:
            history = connection.execute(
                "SELECT event_id, accepted_at, revision FROM contribution_event_acceptance "
                "ORDER BY accepted_at, event_id"
            ).fetchall()
        self.assertEqual([row[0] for row in history], [newer, older])
        self.assertEqual([row[2] for row in history], [first.revision, second.revision])
        self.assertLess(history[0][1], history[1][1])
        self.store.record_events(42, [_verse_event("newer-add")])
        self.store.publish_approved_events_atomically(_empty_catalog(), [older], actor="admin")
        self.store.close()
        self.store = ContributionStore(path=str(self.path))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT event_id, accepted_at, revision FROM contribution_event_acceptance "
                    "ORDER BY accepted_at, event_id"
                ).fetchall(),
                history,
            )

    def test_existing_store_adds_provenance_without_inventing_legacy_order(self) -> None:
        self.approve()
        recorded = self.store.record_events(42, [_topic_event()])
        event_id = next(iter(recorded.event_ids.values()))
        self.store.decide_event(event_id, "approved", canonical_topic_id="grace", actor="admin")
        first = self.store.publish_approved_events_atomically(_catalog(), [event_id], actor="admin")
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE contribution_event_acceptance")
        self.store = ContributionStore(path=str(self.path))
        self.assertEqual(self.store.current_catalog(), first)
        self.assertEqual(self.store.list_events()[0].state, "applied")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT * FROM contribution_event_acceptance").fetchall(), []
            )

    def test_v1_catalog_schema_migrates_without_losing_revisions(self) -> None:
        first = self.store.publish_catalog(_catalog(), actor="admin")
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                ALTER TABLE contribution_catalog_revisions
                    RENAME TO contribution_catalog_revisions_v2;
                CREATE TABLE contribution_catalog_revisions (
                    revision INTEGER PRIMARY KEY,
                    checksum TEXT NOT NULL UNIQUE,
                    catalog_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    actor TEXT NOT NULL
                );
                INSERT INTO contribution_catalog_revisions
                SELECT * FROM contribution_catalog_revisions_v2;
                DROP TABLE contribution_catalog_revisions_v2;
                PRAGMA user_version=1;
                """
            )

        self.store = ContributionStore(path=str(self.path))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertEqual(self.store.current_catalog().checksum, first.checksum)

    def test_v3_notification_schema_migrates_and_recovers_unclaimable_leases(
        self,
    ) -> None:
        self.store.submit_application(42, first_name="Grace")
        self.store.decide_application(42, "approved", actor="admin")
        claimed = self.store.claim_notifications(lease_seconds=60)[0]
        self.assertTrue(claimed.claim_token)
        self.store.close()

        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                ALTER TABLE contribution_notifications
                    RENAME TO contribution_notifications_v4;
                CREATE TABLE contribution_notifications (
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
                    last_error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (contributor_id)
                        REFERENCES contributor_applications(user_id)
                );
                INSERT INTO contribution_notifications (
                    id, contributor_id, kind, message, state, attempts,
                    available_at, lease_until, last_error, created_at, updated_at
                )
                SELECT id, contributor_id, kind, message, state, attempts,
                       available_at, lease_until, last_error, created_at, updated_at
                FROM contribution_notifications_v4;
                DROP TABLE contribution_notifications_v4;
                CREATE INDEX contribution_notifications_delivery
                    ON contribution_notifications (state, available_at, id);
                PRAGMA user_version=3;
                """
            )

        self.store = ContributionStore(path=str(self.path))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(contribution_notifications)")
            }
            self.assertIn("claim_token", columns)
        recovered = self.store.claim_notifications(lease_seconds=60)
        self.assertEqual(len(recovered), 1)
        self.assertNotEqual(recovered[0].claim_token, claimed.claim_token)

    def test_v4_schema_migrates_snapshot_receipts_and_capabilities(self) -> None:
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                DROP TABLE contribution_sync_receipts;
                DROP TABLE contribution_client_snapshots;
                DROP TABLE contributor_capabilities;
                PRAGMA user_version=4;
                """
            )

        self.store = ContributionStore(path=str(self.path))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue(
            {
                "contribution_sync_receipts",
                "contribution_client_snapshots",
                "contributor_capabilities",
            }.issubset(tables)
        )

    def test_current_version_store_missing_a_table_is_repaired_on_open(self) -> None:
        # A store already at the current version takes no migration branch.
        # Before self-healing, a capabilities table lost to an interrupted
        # upgrade stayed missing forever: reads succeeded, so an approved
        # contributor saw the panel, while every token issuance failed.
        self.store.submit_application(42, first_name="Grace")
        self.store.decide_application(42, "approved", actor="admin")
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                DROP TABLE contributor_capabilities;
                PRAGMA user_version=6;
                """
            )

        self.store = ContributionStore(path=str(self.path))
        self.assertRegex(self.store.issue_capability(42), r"\Agbc_[A-Za-z0-9_-]{43}\Z")
        self.store.verify_writable()

    def test_healthy_store_opens_without_touching_its_schema(self) -> None:
        # Self-healing must only act on a damaged store. SQLite bumps the
        # schema cookie on any DDL that changes the schema, so a healthy
        # current-version store must open with the cookie untouched.
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            before = connection.execute("PRAGMA schema_version").fetchone()[0]
        self.store = ContributionStore(path=str(self.path))
        self.store.verify_writable()
        with sqlite3.connect(self.path) as connection:
            after = connection.execute("PRAGMA schema_version").fetchone()[0]
        self.assertEqual(after, before)

    def test_verify_writable_names_missing_tables_and_passes_a_healthy_store(self) -> None:
        self.store.verify_writable()
        with sqlite3.connect(self.path) as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM contributor_capabilities"
            ).fetchone()[0]
        # The probe must not leave any domain change behind.
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM contributor_capabilities"
                ).fetchone()[0],
                before,
            )

        # Bypass the self-healing open to model a store damaged while running.
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP TABLE contributor_capabilities")
        with self.assertRaises(sqlite3.DatabaseError) as raised:
            self.store.verify_writable()
        self.assertIn("contributor_capabilities", str(raised.exception))

    def test_v6_push_transport_layout_is_upgraded_in_place(self) -> None:
        # A deployment that briefly ran the withdrawn web_app_data push
        # transport left its database at user_version 6 with two staging
        # tables and none of the live catalogue state. Opening it must
        # install the live catalogue layout while preserving every
        # contributor, event, and ledger row.
        self.approve()
        self.store.record_events(42, [_topic_event(), _verse_event()])
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys=OFF;
                DROP TABLE contribution_live_catalog;
                ALTER TABLE contribution_events DROP COLUMN live_at;
                ALTER TABLE contribution_publication_state DROP COLUMN repo_pull_request;
                ALTER TABLE contribution_publication_state DROP COLUMN api_catalog_version;
                ALTER TABLE contribution_publication_state DROP COLUMN api_checksum;
                ALTER TABLE contribution_publication_state DROP COLUMN api_checked_at;
                CREATE TABLE contribution_push_bundles (
                    contributor_id INTEGER NOT NULL,
                    bundle_id TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (contributor_id, bundle_id)
                );
                CREATE INDEX contribution_push_bundles_expiry
                    ON contribution_push_bundles (updated_at);
                CREATE TABLE contribution_push_chunks (
                    contributor_id INTEGER NOT NULL,
                    bundle_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    PRIMARY KEY (contributor_id, bundle_id, chunk_index)
                );
                INSERT INTO contribution_push_bundles VALUES (42, 'b1', 1);
                INSERT INTO contribution_push_chunks VALUES (42, 'b1', 0);
                PRAGMA user_version=6;
                """
            )

        self.store = ContributionStore(path=str(self.path))
        status = self.store.contribution_status(42)
        self.assertEqual(status["state"], "approved")
        self.assertEqual(status["summary"]["events"]["pending"], 2)
        self.assertEqual(status["summary"]["events"]["live"], 0)
        self.assertIsNone(self.store.live_catalog())
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertNotIn("contribution_push_bundles", tables)
        self.assertNotIn("contribution_push_chunks", tables)
        self.assertIn("contribution_live_catalog", tables)
        self.assert_v6_columns()

        # A database written by a genuinely newer application still fails
        # closed instead of being altered.
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version=7")
        with self.assertRaises(sqlite3.DatabaseError):
            ContributionStore(path=str(self.path))
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version=6")
        self.store = ContributionStore(path=str(self.path))

    def assert_v6_columns(self) -> None:
        with sqlite3.connect(self.path) as connection:
            event_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(contribution_events)")
            }
            publication_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(contribution_publication_state)"
                )
            }
        self.assertIn("live_at", event_columns)
        self.assertTrue(
            {
                "repo_pull_request",
                "api_catalog_version",
                "api_checksum",
                "api_checked_at",
            }.issubset(publication_columns)
        )

    def test_v5_schema_migrates_to_live_catalogue_layout_without_losing_data(self) -> None:
        # The last release wrote user_version 5: no live catalogue table, no
        # live_at on events, no pull request or API columns on the
        # publication row. Rebuild exactly that layout from a current store
        # and prove the upgrade keeps every row and adds the new state.
        self.approve()
        result = self.store.record_events(42, [_topic_event(), _verse_event()])
        self.store.set_topic_mapping(
            42,
            "local.grace",
            "grace",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "grace",
                "name": "Grace",
                "color": "#bbf7d0",
                "aliases": [],
            },
        )
        for event_id in result.event_ids.values():
            self.store.decide_event(event_id, "approved", actor="admin")
        revision = self.store.publish_approved_events_atomically(
            _catalog(),
            list(result.event_ids.values()),
            actor="admin",
        )
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys=OFF;
                DROP TABLE contribution_live_catalog;
                ALTER TABLE contribution_events DROP COLUMN live_at;
                ALTER TABLE contribution_publication_state DROP COLUMN repo_pull_request;
                ALTER TABLE contribution_publication_state DROP COLUMN api_catalog_version;
                ALTER TABLE contribution_publication_state DROP COLUMN api_checksum;
                ALTER TABLE contribution_publication_state DROP COLUMN api_checked_at;
                PRAGMA user_version=5;
                """
            )
            self.assertNotIn(
                "live_at",
                {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(contribution_events)")
                },
            )

        self.store = ContributionStore(path=str(self.path))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("contribution_live_catalog", tables)
        self.assert_v6_columns()
        self.store.verify_writable()

        status = self.store.contribution_status(42)
        self.assertEqual(status["state"], "approved")
        self.assertEqual(status["summary"]["events"]["applied"], 2)
        self.assertEqual(status["summary"]["events"]["live"], 0)
        # Nothing is published until the API has been observed.
        self.assertFalse(status["topics"][0]["published"])
        self.assertEqual(self.store.current_catalog().checksum, revision.checksum)
        publication = self.store.publication_state()
        self.assertEqual(publication["live_revision"], revision.revision)
        for key in ("repo_pull_request", "api_catalog_version", "api_checksum", "api_checked_at"):
            self.assertIn(key, publication)
            self.assertIsNone(publication[key])
        self.assertEqual(self.store.live_catalog_state()["observed"], False)

        # The upgraded store observes the API and settles liveness normally.
        update = self.store.record_live_catalog(3, _checksum("v3"), [_live_topic()])
        self.assertTrue(update.changed)
        self.assertEqual(len(update.newly_live_event_ids), 2)
        self.assertEqual(update.notified_contributor_ids, (42,))
        migrated = self.store.contribution_status(42)
        self.assertTrue(migrated["topics"][0]["published"])
        self.assertEqual(migrated["summary"]["events"]["live"], 2)

    def test_open_repairs_missing_catalogue_seeds_for_v3_and_current_schema(
        self,
    ) -> None:
        for version in (3, 4, 6):
            with self.subTest(version=version):
                self.store.close()
                with sqlite3.connect(self.path) as connection:
                    connection.execute("DELETE FROM contribution_publication_state")
                    connection.execute("DELETE FROM contribution_catalog_revisions")
                    connection.execute(f"PRAGMA user_version={version}")

                self.store = ContributionStore(path=str(self.path))
                current = self.store.current_catalog()
                self.assertEqual(current.revision, 0)
                self.assertEqual(current.catalog, _empty_catalog())
                publication = self.store.publication_state()
                self.assertEqual(publication["live_revision"], 0)
                self.assertEqual(publication["live_checksum"], current.checksum)

    def test_catalog_can_restore_an_older_payload_with_monotonic_revision(self) -> None:
        published = self.store.publish_approved_events_atomically(_catalog(), [], actor="admin")
        restored = self.store.publish_approved_events_atomically(
            _empty_catalog(), [], actor="admin"
        )
        self.assertEqual((published.revision, restored.revision), (1, 2))

        self.store.close()
        self.store = ContributionStore(path=str(self.path))
        current = self.store.current_catalog()
        self.assertEqual(current.revision, 2)
        self.assertEqual(current.catalog, _empty_catalog())
        self.assertEqual(self.store.published_topic_ids(), ("grace",))
        repeated = self.store.publish_approved_events_atomically(
            _empty_catalog(), [], actor="admin"
        )
        self.assertEqual(repeated.revision, 2)

    def test_application_is_idempotent_and_decision_notice_is_out_of_band(self) -> None:
        first, created = self.store.submit_application(42, first_name="Grace")
        second, repeated = self.store.submit_application(42, first_name="Grace Updated")
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first.user_id, second.user_id)

        approved = self.store.decide_application(42, "approved", actor="admin")
        self.assertEqual(approved.state, "approved")
        notices = self.store.claim_notifications(limit=10, lease_seconds=60)
        self.assertEqual(len(notices), 1)
        self.assertIn("enrolled", notices[0].message)
        self.store.mark_notification_sent(notices[0].id, notices[0].claim_token)
        listed = self.store.list_notifications()[0]
        self.assertEqual(listed.state, "sent")
        self.assertFalse(hasattr(listed, "claim_token"))

        # Repeating the same decision is auditable but must not notify twice.
        self.store.decide_application(42, "approved", actor="admin")
        self.assertEqual(len(self.store.list_notifications()), 1)

    def test_notification_completion_requires_the_active_cross_process_claim(
        self,
    ) -> None:
        self.store.submit_application(42, first_name="Grace")
        self.store.decide_application(42, "approved", actor="admin")
        worker_a_claim = self.store.claim_notifications(lease_seconds=60)[0]
        worker_b = ContributionStore(path=str(self.path))
        try:
            with sqlite3.connect(self.path) as connection:
                connection.execute(
                    "UPDATE contribution_notifications SET lease_until = 0 WHERE id = ?",
                    (worker_a_claim.id,),
                )

            with self.assertRaises(ContributionNotificationConflict):
                self.store.mark_notification_sent(
                    worker_a_claim.id,
                    worker_a_claim.claim_token,
                )

            worker_b_claim = worker_b.claim_notifications(lease_seconds=60)[0]
            self.assertEqual(worker_b_claim.id, worker_a_claim.id)
            self.assertNotEqual(worker_b_claim.claim_token, worker_a_claim.claim_token)
            with self.assertRaises(ContributionNotificationConflict):
                self.store.mark_notification_sent(
                    worker_a_claim.id,
                    worker_a_claim.claim_token,
                )
            with self.assertRaises(ContributionNotificationConflict):
                self.store.mark_notification_failed(
                    worker_a_claim.id,
                    worker_a_claim.claim_token,
                    "stale worker",
                )

            worker_b.mark_notification_sent(
                worker_b_claim.id,
                worker_b_claim.claim_token,
            )
            with self.assertRaises(ContributionNotificationConflict):
                worker_b.mark_notification_failed(
                    worker_b_claim.id,
                    worker_b_claim.claim_token,
                    "late failure",
                )
            self.assertEqual(self.store.list_notifications()[0].state, "sent")
        finally:
            worker_b.close()

    def test_submission_is_approval_gated_and_accepts_only_coordinates(self) -> None:
        self.store.submit_application(42, first_name="Grace")
        with self.assertRaises(ContributionNotAllowed):
            self.store.record_events(42, [_topic_event()])
        self.store.decide_application(42, "approved", actor="admin")
        with self.assertRaisesRegex(ContributionNotAllowed, "disclosure"):
            self.store.record_events(42, [_topic_event()])
        self.store.acknowledge_disclosure(42)

        with self.assertRaisesRegex(ContributionError, "unsupported fields"):
            self.store.record_events(
                42,
                [
                    {
                        **_verse_event(),
                        "text": "The browser must never author authoritative verse text.",
                    }
                ],
            )
        with self.assertRaisesRegex(ContributionError, "English"):
            self.store.record_events(42, [_topic_event(name="恩典")])

    def test_submission_enforces_catalogue_book_and_chapter_boundaries(self) -> None:
        self.approve()
        accepted = self.store.record_events(
            42,
            [
                _verse_event(
                    "verse.revelation.22.21",
                    book=66,
                    chapter=22,
                    verse=21,
                )
            ],
        )
        self.assertEqual(accepted.accepted, 1)

        invalid = (
            _verse_event("verse.book.67", book=67, chapter=1),
            _verse_event("verse.genesis.51", book=1, chapter=51),
            _verse_event("verse.revelation.23", book=66, chapter=23),
        )
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(ContributionError):
                self.store.record_events(42, [event])

    def test_verse_event_may_supply_non_authoritative_topic_review_context(self) -> None:
        self.approve()
        event = _verse_event("verse.context.1")
        event["topic"] = {
            "local_topic_id": "local.grace",
            "name": "Grace",
            "color": "#bbf7d0",
        }
        self.store.record_events(42, [event])
        source = self.store.list_source_topics()[0]
        self.assertEqual(source.name, "Grace")
        self.assertEqual(source.color, "#bbf7d0")
        self.assertIsNone(source.canonical_definition)

    def test_verse_context_cannot_overwrite_a_newer_explicit_topic_upsert(self) -> None:
        self.approve()
        stale_verse = _verse_event("verse.context.stale")
        stale_verse["topic"] = {
            "local_topic_id": "local.grace",
            "name": "Old Name",
            "color": "#123456",
        }

        self.store.record_events(
            42,
            [
                _topic_event(
                    "topic.latest",
                    name="New Name",
                    color="#abcdef",
                ),
                stale_verse,
            ],
        )

        source = self.store.list_source_topics()[0]
        self.assertEqual(source.name, "New Name")
        self.assertEqual(source.color, "#abcdef")

    def test_event_idempotency_replays_exact_body_and_conflicts_atomically(self) -> None:
        self.approve()
        first = self.store.record_events(42, [_topic_event(), _verse_event()])
        replay = self.store.record_events(42, [_topic_event(), _verse_event()])
        self.assertEqual(first.accepted, 2)
        self.assertEqual(replay.replayed, 2)
        self.assertEqual(first.event_ids, replay.event_ids)
        self.assertEqual([event.replay_count for event in self.store.list_events()], [1, 1])

        with self.assertRaises(ContributionIdempotencyConflict):
            self.store.record_events(
                42,
                [_verse_event(verse=17)],
            )
        self.assertEqual(len(self.store.list_events()), 2)

    def test_private_sync_status_tracks_publication_without_leaking_contributors(
        self,
    ) -> None:
        self.approve(42)
        self.approve(84)
        result = self.store.record_events(42, [_topic_event()])
        self.store.record_events(
            84,
            [
                {
                    "client_event_id": "topic.mercy.v1",
                    "type": "topic_upsert",
                    "topic": {
                        "local_topic_id": "private.mercy",
                        "name": "Mercy",
                        "color": "#123456",
                    },
                }
            ],
        )
        self.store.set_topic_mapping(
            42,
            "local.grace",
            "grace",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "grace",
                "name": "Grace",
                "color": "#bbf7d0",
                "aliases": ["God's grace"],
            },
        )
        upsert_id = result.event_ids["topic.grace.v1"]
        self.store.decide_event(upsert_id, "approved", actor="admin")

        awaiting_publication = self.store.contribution_status(42)
        self.assertFalse(awaiting_publication["topics"][0]["published"])
        self.assertEqual(awaiting_publication["summary"]["topics"]["mapped"], 1)
        self.assertEqual(awaiting_publication["summary"]["topics"]["published"], 0)
        self.assertEqual(awaiting_publication["summary"]["events"]["approved"], 1)
        self.assertNotIn("private.mercy", json.dumps(awaiting_publication))

        self.store.publish_approved_events_atomically(
            _catalog(),
            [upsert_id],
            actor="admin",
        )
        # Acceptance into the ledger is not publication: the shared catalogue
        # lives in the Bookmarks API, and only its observed content counts.
        accepted = self.store.contribution_status(42)
        self.assertFalse(accepted["topics"][0]["published"])
        self.assertEqual(accepted["summary"]["topics"]["published"], 0)
        self.assertEqual(accepted["summary"]["events"]["applied"], 1)
        self.assertEqual(accepted["summary"]["events"]["live"], 0)
        without_grace = self.store.record_live_catalog(
            1, _checksum("v1"), [_live_topic("faith", name="Faith", color="#abcdef")]
        )
        self.assertEqual(without_grace.newly_live_event_ids, ())
        self.assertFalse(self.store.contribution_status(42)["topics"][0]["published"])

        self.store.record_live_catalog(2, _checksum("v2"), [_live_topic()])
        published = self.store.contribution_status(42)
        self.assertEqual(
            published["topics"],
            [
                {
                    "local_topic_id": "local.grace",
                    "state": "mapped",
                    "published": True,
                    "canonical_topic_id": "grace",
                    "canonical_topic": {
                        "id": "grace",
                        "name": "Grace",
                        "color": "#bbf7d0",
                        "aliases": ["God's grace"],
                    },
                }
            ],
        )
        self.assertEqual(published["summary"]["topics"]["published"], 1)
        self.assertEqual(published["summary"]["events"]["applied"], 1)
        self.assertEqual(published["summary"]["events"]["live"], 1)
        # The other contributor's private proposal never became live and was
        # never told anything.
        self.assertEqual(
            [
                notification.contributor_id
                for notification in self.store.list_notifications()
                if notification.kind == LIVE_NOTIFICATION_KIND
            ],
            [42],
        )

        # A later edit is a new proposal, but it does not make the already-live
        # canonical topic personal again while that proposal awaits review.
        self.store.record_events(
            42,
            [_topic_event("topic.grace.v2", name="Amazing Grace", color="#123456")],
        )
        reopened = self.store.contribution_status(42)
        self.assertEqual(reopened["topics"][0]["state"], "pending")
        self.assertTrue(reopened["topics"][0]["published"])
        self.assertEqual(reopened["summary"]["topics"]["mapped"], 1)
        self.assertEqual(reopened["summary"]["topics"]["published"], 1)
        self.assertEqual(reopened["topics"][0]["canonical_topic_id"], "grace")
        self.assertEqual(reopened["topics"][0]["canonical_topic"]["name"], "Grace")

    def test_private_sync_status_marks_an_applied_delete_as_not_published(self) -> None:
        self.approve()
        result = self.store.record_events(
            42,
            [
                _topic_event(),
                {
                    "client_event_id": "topic.grace.delete",
                    "type": "topic_delete",
                    "topic": {"local_topic_id": "local.grace"},
                },
            ],
        )
        self.store.set_topic_mapping(
            42,
            "local.grace",
            "grace",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "grace",
                "name": "Grace",
                "color": "#bbf7d0",
                "aliases": [],
            },
        )
        for event in self.store.list_events():
            self.store.decide_event(
                event.id,
                "approved",
                actor="admin",
                canonical_topic_id="grace",
            )
        self.store.publish_approved_events_atomically(
            _empty_catalog(),
            list(result.event_ids.values()),
            actor="admin",
        )

        status = self.store.contribution_status(42)
        self.assertFalse(status["topics"][0]["published"])
        self.assertEqual(status["summary"]["topics"]["published"], 0)
        # Even while the API still publishes the topic, the contributor's
        # latest applied transition is a delete, so it is not theirs any more.
        self.store.record_live_catalog(1, _checksum("v1"), [_live_topic()])
        lingering = self.store.contribution_status(42)
        self.assertFalse(lingering["topics"][0]["published"])
        self.assertEqual(lingering["summary"]["topics"]["published"], 0)
        self.assertEqual(lingering["summary"]["events"]["live"], 1)
        self.store.record_live_catalog(2, _checksum("v2"), [])
        removed = self.store.contribution_status(42)
        self.assertFalse(removed["topics"][0]["published"])
        self.assertEqual(removed["summary"]["events"]["live"], 2)

    def test_private_sync_status_publishes_bundled_mapping_from_applied_verse(
        self,
    ) -> None:
        self.approve()
        verse = _verse_event("verse.existing.grace")
        verse["topic"] = {
            "local_topic_id": "local.grace",
            "name": "Grace",
            "color": "#bbf7d0",
        }
        result = self.store.record_events(42, [verse])
        self.store.set_topic_mapping(
            42,
            "local.grace",
            "grace",
            state="mapped",
            actor="admin",
            name="Grace",
            color="#bbf7d0",
            aliases=["God's grace"],
        )
        event_id = result.event_ids["verse.existing.grace"]
        self.store.decide_event(event_id, "approved", actor="admin")
        self.store.publish_approved_events_atomically(
            {
                "schema_version": 1,
                "topics": [],
                "associations": {
                    "add": [
                        {
                            "topic_id": "grace",
                            "book": 43,
                            "chapter": 3,
                            "verse": 16,
                        }
                    ],
                    "remove": [],
                },
            },
            [event_id],
            actor="admin",
        )
        self.assertFalse(self.store.contribution_status(42)["topics"][0]["published"])

        # A mapping onto a topic the API publishes is published as soon as
        # the catalogue has been observed, whether or not the verse is there.
        self.store.record_live_catalog(
            1, _checksum("v1"), [_live_topic(verses=((1, 1, 1),))]
        )
        status = self.store.contribution_status(42)
        self.assertTrue(status["topics"][0]["published"])
        self.assertEqual(status["topics"][0]["canonical_topic_id"], "grace")
        self.assertEqual(status["topics"][0]["canonical_topic"]["name"], "Grace")
        self.assertEqual(status["summary"]["topics"]["published"], 1)
        self.assertEqual(status["summary"]["events"]["live"], 0)
        self.store.record_live_catalog(2, _checksum("v2"), [_live_topic()])
        self.assertEqual(self.store.contribution_status(42)["summary"]["events"]["live"], 1)

    def test_canonical_definition_is_separate_and_topic_edits_reopen_review(self) -> None:
        self.approve()
        self.store.record_events(42, [_topic_event()])

        mapped = self.store.set_topic_mapping(
            42,
            "local.grace",
            "grace",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "grace",
                "name": "Grace",
                "color": "#bbf7d0",
                "aliases": ["God's grace"],
            },
        )
        self.assertEqual(mapped.canonical_definition["name"], "Grace")
        self.assertEqual(self.store.list_canonical_topics()[0].name, "Grace")

        verse_with_context = _verse_event("verse.context.after.mapping")
        verse_with_context["topic"] = {
            "local_topic_id": "local.grace",
            "name": "Amazing Grace",
            "color": "#123456",
        }
        self.store.record_events(42, [verse_with_context])
        after_verse = self.store.list_source_topics()[0]
        self.assertEqual(after_verse.state, "mapped")
        self.assertEqual(after_verse.name, "Grace")
        self.assertEqual(after_verse.color, "#bbf7d0")

        self.store.record_events(
            42,
            [_topic_event("topic.grace.v2", name="Amazing Grace", color="#123456")],
        )
        reopened = self.store.list_source_topics()[0]
        self.assertEqual(reopened.state, "pending")
        self.assertEqual(reopened.canonical_topic_id, "grace")
        # Raw proposed spelling/colour is visible, while the authoritative
        # definition remains unchanged until a moderator explicitly replaces it.
        self.assertEqual(reopened.name, "Amazing Grace")
        self.assertEqual(reopened.canonical_definition["name"], "Grace")

    def test_definition_change_reopens_every_unpublished_topic_authorization(self) -> None:
        self.approve()
        self.store.record_events(42, [_topic_event()])
        self.store.set_topic_mapping(
            42,
            "local.grace",
            "grace",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "grace",
                "name": "Grace",
                "color": "#bbf7d0",
                "aliases": [],
            },
        )
        upsert = self.store.list_events(types={"topic_upsert"})[0]
        self.store.decide_event(upsert.id, "approved", actor="admin")

        self.store.set_topic_mapping(
            42,
            "local.grace",
            "grace",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "grace",
                "name": "Grace",
                "color": "#123456",
                "aliases": [],
            },
        )
        reopened = self.store.list_events(types={"topic_upsert"})[0]
        self.assertEqual(reopened.state, "pending")
        self.assertEqual(self.store.list_canonical_topics()[0].color, "#123456")
        with sqlite3.connect(self.path) as connection:
            decision = connection.execute(
                """
                SELECT previous_state, decision
                FROM contribution_decisions
                WHERE subject_type = 'event' AND subject_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (str(upsert.id),),
            ).fetchone()
        self.assertEqual(decision, ("approved", "pending"))
        self.assertTrue(
            any(
                item["action"] == "topic_event_reopened_after_definition_change"
                and item["subject_id"] == str(upsert.id)
                for item in self.store.list_audit()
            )
        )

    def test_mapped_source_is_snapshotted_on_later_verse_events(self) -> None:
        self.approve()
        self.store.record_events(42, [_topic_event()])
        self.store.set_topic_mapping(
            42,
            "local.grace",
            "grace",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "grace",
                "name": "Grace",
                "color": "#bbf7d0",
                "aliases": [],
            },
        )
        result = self.store.record_events(
            42,
            [_verse_event("verse.after.mapping", verse=17)],
        )
        event_id = next(iter(result.event_ids.values()))
        event = next(item for item in self.store.list_events() if item.id == event_id)
        self.assertEqual(event.canonical_topic_id, "grace")
        self.assertEqual(self.store.list_source_topics()[0].state, "mapped")

    def test_remapping_reopens_approved_events_for_explicit_review(self) -> None:
        self.approve()
        result = self.store.record_events(42, [_topic_event(), _verse_event()])
        self.store.set_topic_mapping(
            42,
            "local.grace",
            "grace",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "grace",
                "name": "Grace",
                "color": "#bbf7d0",
                "aliases": [],
            },
        )
        for event in self.store.list_events():
            self.store.decide_event(event.id, "approved", actor="admin")

        self.store.set_topic_mapping(
            42,
            "local.grace",
            "mercy",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "mercy",
                "name": "Mercy",
                "color": "#123456",
                "aliases": [],
            },
        )
        events = self.store.list_events()
        self.assertEqual({event.state for event in events}, {"pending"})
        self.assertEqual({event.canonical_topic_id for event in events}, {"mercy"})
        self.assertTrue(
            any(
                item["action"] == "events_reopened_after_topic_remap"
                and item["detail"]["count"] == 2
                for item in self.store.list_audit()
            )
        )
        with self.assertRaisesRegex(ContributionError, "Only approved"):
            self.store.publish_approved_events_atomically(
                _catalog(),
                list(result.event_ids.values()),
                actor="admin",
            )

    def test_catalog_publication_and_event_application_are_one_transaction(self) -> None:
        self.approve()
        result = self.store.record_events(42, [_topic_event(), _verse_event()])
        self.store.set_topic_mapping(
            42,
            "local.grace",
            "grace",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "grace",
                "name": "Grace",
                "color": "#bbf7d0",
                "aliases": [],
            },
        )
        for event in self.store.list_events():
            self.store.decide_event(event.id, "approved", actor="admin")

        revision = self.store.publish_approved_events_atomically(
            _catalog(),
            list(result.event_ids.values()),
            actor="admin",
        )
        self.assertEqual(revision.revision, 1)
        self.assertEqual({event.state for event in self.store.list_events()}, {"applied"})
        self.assertNotIn("contributor", json.dumps(revision.catalog))

        repeated = self.store.publish_approved_events_atomically(
            _catalog(),
            list(result.event_ids.values()),
            actor="admin",
        )
        self.assertEqual(repeated.revision, 1)
        self.assertEqual(self.store.current_catalog().checksum, revision.checksum)
        with self.assertRaisesRegex(ContributionError, "published events"):
            self.store.set_topic_mapping(
                42,
                "local.grace",
                "mercy",
                state="mapped",
                actor="admin",
                canonical_definition={
                    "id": "mercy",
                    "name": "Mercy",
                    "color": "#123456",
                    "aliases": [],
                },
            )
        with self.assertRaisesRegex(ContributionError, "published events"):
            self.store.set_topic_mapping(
                42,
                "local.grace",
                state="rejected",
                actor="admin",
            )

    def test_atomic_publication_refuses_a_stale_cross_process_plan(self) -> None:
        self.approve()
        result = self.store.record_events(42, [_topic_event(), _verse_event()])
        self.store.set_topic_mapping(
            42,
            "local.grace",
            "grace",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "grace",
                "name": "Grace",
                "color": "#bbf7d0",
                "aliases": [],
            },
        )
        for event in self.store.list_events():
            self.store.decide_event(event.id, "approved", actor="admin")
        planned_from = self.store.current_catalog()

        other_process = ContributionStore(path=str(self.path))
        try:
            other_process.publish_catalog(_catalog(), actor="other-admin")
        finally:
            other_process.close()

        with self.assertRaises(ContributionPublicationConflict):
            self.store.publish_approved_events_atomically(
                _catalog(),
                list(result.event_ids.values()),
                actor="admin",
                expected_revision=planned_from.revision,
                expected_checksum=planned_from.checksum,
            )
        self.assertEqual(
            {event.state for event in self.store.list_events()},
            {"approved"},
        )

    def test_catalog_rejects_orphan_topics_conflicting_ops_and_private_fields(self) -> None:
        orphan = _catalog()
        orphan["associations"] = {"add": [], "remove": []}
        with self.assertRaisesRegex(ContributionError, "effective verse"):
            normalize_catalog(orphan)

        conflict = _catalog()
        conflict["associations"]["remove"] = list(conflict["associations"]["add"])
        with self.assertRaisesRegex(ContributionError, "both added and removed"):
            normalize_catalog(conflict)

        private = _catalog()
        private["contributor_id"] = 42
        with self.assertRaisesRegex(ContributionError, "must contain"):
            normalize_catalog(private)

    def test_repository_publication_lease_is_exclusive_and_recoverable(self) -> None:
        current = self.store.current_catalog()
        first_token = self.store.begin_repo_publication(
            current.revision,
            current.checksum,
            actor="publisher-one",
            lease_seconds=60,
        )
        other_process = ContributionStore(path=str(self.path))
        try:
            with self.assertRaises(ContributionRepositoryConflict):
                other_process.begin_repo_publication(
                    current.revision,
                    current.checksum,
                    actor="publisher-two",
                )
            with sqlite3.connect(self.path) as connection:
                connection.execute(
                    "UPDATE contribution_publication_state "
                    "SET repo_lease_until = 0 WHERE singleton = 1"
                )
            recovered_token = other_process.begin_repo_publication(
                current.revision,
                current.checksum,
                actor="publisher-two",
            )
            with self.assertRaises(ContributionRepositoryConflict):
                self.store.finish_repo_publication(
                    first_token,
                    current.revision,
                    state="failed",
                    actor="publisher-one",
                    error="stale",
                )
            other_process.finish_repo_publication(
                recovered_token,
                current.revision,
                state="pushed",
                actor="publisher-two",
                branch="contributions/revision-0",
                commit="a" * 40,
            )
            with self.assertRaises(ContributionRepositoryConflict):
                self.store.begin_repo_publication(
                    current.revision,
                    current.checksum,
                    actor="publisher-one",
                )
        finally:
            other_process.close()
        state = self.store.publication_state()
        self.assertNotIn("repo_token", state)
        self.assertEqual(state["repo_state"], "pushed")
        self.assertEqual(state["repo_commit"], "a" * 40)

    def accept_every_event(
        self,
        *,
        user_id: int = 42,
        bundle: dict[str, object],
    ) -> tuple[int, ...]:
        """Approve and accept every pending event of one contributor."""
        event_ids = tuple(
            event.id
            for event in self.store.list_events(states={"pending"})
            if event.contributor_id == user_id
        )
        for event_id in event_ids:
            self.store.decide_event(event_id, "approved", actor="admin")
        self.store.publish_approved_events_atomically(bundle, list(event_ids), actor="admin")
        return event_ids

    def seed_every_event_type(self) -> dict[str, int]:
        """Queue and accept one contributor's upsert, adds, delete and remove."""
        self.approve(42)
        result = self.store.record_events(
            42,
            [
                _topic_event(),
                _verse_event(),
                _verse_event("verse.grace.43.3.17.add", verse=17),
                {
                    "client_event_id": "topic.mercy.v1",
                    "type": "topic_upsert",
                    "topic": {
                        "local_topic_id": "local.mercy",
                        "name": "Mercy",
                        "color": "#123456",
                    },
                },
                {
                    "client_event_id": "topic.mercy.delete",
                    "type": "topic_delete",
                    "topic": {"local_topic_id": "local.mercy"},
                },
                {
                    "client_event_id": "verse.faith.40.5.3.remove",
                    "type": "verse_remove",
                    "topic": {
                        "local_topic_id": "local.faith",
                        "name": "Faith",
                        "color": "#abcdef",
                    },
                    "verse": {"book": 40, "chapter": 5, "verse": 3},
                },
            ],
        )
        for local_id, canonical, name, color in (
            ("local.grace", "grace", "Grace", "#bbf7d0"),
            ("local.mercy", "mercy", "Mercy", "#123456"),
        ):
            self.store.set_topic_mapping(
                42,
                local_id,
                canonical,
                state="mapped",
                actor="admin",
                canonical_definition={
                    "id": canonical,
                    "name": name,
                    "color": color,
                    "aliases": [],
                },
            )
        self.store.set_topic_mapping(
            42,
            "local.faith",
            "faith",
            state="mapped",
            actor="admin",
            name="Faith",
            color="#abcdef",
            aliases=[],
        )
        self.accept_every_event(
            bundle={
                "schema_version": 1,
                "topics": [
                    {"id": "grace", "name": "Grace", "color": "#bbf7d0", "aliases": []}
                ],
                "associations": {
                    "add": [
                        {"topic_id": "grace", "book": 43, "chapter": 3, "verse": 16},
                        {"topic_id": "grace", "book": 43, "chapter": 3, "verse": 17},
                    ],
                    "remove": [
                        {"topic_id": "faith", "book": 40, "chapter": 5, "verse": 3},
                    ],
                },
            }
        )
        return result.event_ids

    def live_notifications(self) -> list[tuple[int, str]]:
        return [
            (notification.contributor_id, notification.message)
            for notification in self.store.list_notifications()
            if notification.kind == LIVE_NOTIFICATION_KIND
        ]

    def test_live_catalog_observation_settles_every_event_type(self) -> None:
        ids = self.seed_every_event_type()
        self.assertIsNone(self.store.live_catalog())
        self.assertEqual(self.store.live_catalog_state()["observed"], False)
        self.assertEqual(self.store.contribution_status(42)["summary"]["events"]["live"], 0)

        # First publication: grace has only 43:3:16, mercy still exists and
        # faith still carries 40:5:3.
        first = self.store.record_live_catalog(
            1,
            _checksum("v1"),
            [
                _live_topic(
                    "faith", name="Faith", color="#abcdef", verses=((40, 5, 3), (40, 5, 4))
                ),
                _live_topic(),
                _live_topic("mercy", name="Mercy", color="#123456", verses=((1, 1, 1),)),
            ],
            fetched_at=1_700_000_000_000_000_000,
        )
        self.assertEqual((first.catalog_version, first.checksum), (1, _checksum("v1")))
        self.assertTrue(first.changed)
        self.assertEqual(
            set(first.newly_live_event_ids),
            {ids["topic.grace.v1"], ids["verse.grace.43.3.16.add"], ids["topic.mercy.v1"]},
        )
        self.assertEqual(first.notified_contributor_ids, (42,))
        live = self.store.live_catalog()
        assert live is not None
        self.assertEqual(live.catalog_version, 1)
        self.assertEqual(live.fetched_at, 1_700_000_000_000_000_000)
        self.assertEqual(live.topic_ids, frozenset({"faith", "grace", "mercy"}))
        self.assertIn(("faith", 40, 5, 4), live.associations)
        self.assertEqual(
            self.store.live_catalog_state(),
            {
                "observed": True,
                "catalog_version": 1,
                "checksum": _checksum("v1"),
                "fetched_at": 1_700_000_000_000_000_000,
                "checked_at": 1_700_000_000_000_000_000,
                "topics": 3,
                "associations": 4,
            },
        )
        status = self.store.contribution_status(42)
        self.assertEqual(status["summary"]["events"]["applied"], 6)
        self.assertEqual(status["summary"]["events"]["live"], 3)
        by_topic = {topic["local_topic_id"]: topic["published"] for topic in status["topics"]}
        # grace: mapped and present; faith: mapped onto an existing API
        # topic; mercy: present, but the latest applied transition deletes it.
        self.assertEqual(
            by_topic, {"local.grace": True, "local.faith": True, "local.mercy": False}
        )
        self.assertEqual(status["summary"]["topics"]["published"], 2)
        self.assertEqual(
            self.live_notifications(),
            [
                (
                    42,
                    "Your reviewed bookmark contributions are now part of the shared "
                    "getBible catalogue (catalogue version 1). Open the Mini App to see "
                    "them marked as global.",
                )
            ],
        )
        observed = [
            entry for entry in self.store.list_audit() if entry["action"] == "live_catalog_observed"
        ]
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["detail"]["catalog_version"], 1)
        self.assertEqual(observed[0]["detail"]["checksum"], _checksum("v1"))
        self.assertEqual(
            observed[0]["detail"]["newly_live"],
            {"topic_delete": 0, "topic_upsert": 2, "verse_add": 1, "verse_remove": 0, "total": 3},
        )
        self.assertEqual(observed[0]["detail"]["notified_contributors"], 1)

        # Second publication: 43:3:17 arrives, mercy is gone, faith lost 40:5:3.
        second = self.store.record_live_catalog(
            2,
            _checksum("v2"),
            [
                _live_topic("faith", name="Faith", color="#abcdef", verses=((40, 5, 4),)),
                _live_topic(verses=((43, 3, 16), (43, 3, 17))),
            ],
        )
        self.assertTrue(second.changed)
        self.assertEqual(
            set(second.newly_live_event_ids),
            {
                ids["verse.grace.43.3.17.add"],
                ids["topic.mercy.delete"],
                ids["verse.faith.40.5.3.remove"],
            },
        )
        status = self.store.contribution_status(42)
        self.assertEqual(status["summary"]["events"]["live"], 6)
        by_topic = {topic["local_topic_id"]: topic["published"] for topic in status["topics"]}
        self.assertEqual(
            by_topic, {"local.grace": True, "local.faith": True, "local.mercy": False}
        )
        self.assertEqual(len(self.live_notifications()), 2)
        self.assertIn("catalogue version 2", self.live_notifications()[1][1])
        for event in self.store.list_events(states={"applied"}):
            self.assertEqual(event.state, "applied")

    def test_live_catalog_reobservation_is_idempotent(self) -> None:
        self.seed_every_event_type()
        # grace and its first verse are present; mercy and faith's verse are
        # absent, so the delete and the removal are live as well.
        first = self.store.record_live_catalog(1, _checksum("v1"), [_live_topic()])
        self.assertEqual(len(first.newly_live_event_ids), 4)
        audit_before = len(self.store.list_audit())
        with sqlite3.connect(self.path) as connection:
            before = connection.execute(
                "SELECT catalog_version, checksum, fetched_at, topics_json "
                "FROM contribution_live_catalog"
            ).fetchall()

        again = self.store.record_live_catalog(
            1, _checksum("v1"), [_live_topic()], fetched_at=1_800_000_000_000_000_000
        )
        self.assertFalse(again.changed)
        self.assertEqual(again.newly_live_event_ids, ())
        self.assertEqual(again.notified_contributor_ids, ())
        self.assertEqual(len(self.live_notifications()), 1)
        self.assertEqual(len(self.store.list_audit()), audit_before)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT catalog_version, checksum, fetched_at, topics_json "
                    "FROM contribution_live_catalog"
                ).fetchall(),
                before,
            )
        # Only the check timestamp moves.
        self.assertEqual(
            self.store.live_catalog_state()["checked_at"], 1_800_000_000_000_000_000
        )
        self.assertEqual(
            self.store.publication_state()["api_checked_at"], 1_800_000_000_000_000_000
        )

        # The stored catalogue survives reopening and another process sees it.
        self.store.close()
        self.store = ContributionStore(path=str(self.path))
        live = self.store.live_catalog()
        assert live is not None
        self.assertEqual(live.associations, frozenset({("grace", 43, 3, 16)}))
        self.assertEqual(self.store.contribution_status(42)["summary"]["events"]["live"], 4)

    def test_live_catalog_check_settles_events_accepted_since_the_observation(self) -> None:
        self.seed_every_event_type()
        self.store.record_live_catalog(
            1, _checksum("v1"), [_live_topic(verses=((43, 3, 16), (43, 3, 17)))]
        )
        self.assertEqual(len(self.store.list_notifications()), 2)  # enrolment + live

        # A verse the API never carried is accepted for removal: the very next
        # check must settle it without any catalogue download.
        self.store.record_events(
            42,
            [_verse_event("verse.grace.43.3.18.remove", operation="verse_remove", verse=18)],
        )
        self.accept_every_event(
            bundle={
                "schema_version": 1,
                "topics": [
                    {"id": "grace", "name": "Grace", "color": "#bbf7d0", "aliases": []}
                ],
                "associations": {
                    "add": [
                        {"topic_id": "grace", "book": 43, "chapter": 3, "verse": 16},
                        {"topic_id": "grace", "book": 43, "chapter": 3, "verse": 17},
                    ],
                    "remove": [
                        {"topic_id": "faith", "book": 40, "chapter": 5, "verse": 3},
                        {"topic_id": "grace", "book": 43, "chapter": 3, "verse": 18},
                    ],
                },
            }
        )
        with self.assertRaisesRegex(ContributionError, "not the recorded"):
            self.store.record_live_catalog_check(2, _checksum("v1"))
        with self.assertRaisesRegex(ContributionError, "not the recorded"):
            self.store.record_live_catalog_check(1, _checksum("other"))
        checked = self.store.record_live_catalog_check(
            1, _checksum("v1"), checked_at=1_900_000_000_000_000_000
        )
        self.assertFalse(checked.changed)
        self.assertEqual(len(checked.newly_live_event_ids), 1)
        self.assertEqual(checked.notified_contributor_ids, (42,))
        self.assertEqual(len(self.live_notifications()), 2)
        state = self.store.live_catalog_state()
        self.assertEqual(state["catalog_version"], 1)
        self.assertEqual(state["checked_at"], 1_900_000_000_000_000_000)
        self.assertLess(state["fetched_at"], state["checked_at"])
        idle = self.store.record_live_catalog_check(1, _checksum("v1"))
        self.assertEqual(idle.newly_live_event_ids, ())
        self.assertEqual(len(self.live_notifications()), 2)

        # Before any observation there is nothing to check against.
        fresh = ContributionStore(path=None)
        try:
            with self.assertRaises(ContributionError):
                fresh.record_live_catalog_check(1, _checksum("v1"))
            self.assertIsNone(fresh.live_catalog_state()["checked_at"])
        finally:
            fresh.close()

    def test_live_notification_is_one_per_contributor_per_observation(self) -> None:
        self.approve(42)
        self.approve(84)
        self.approve(126)
        contributors = ((42, "local.grace", "grace"), (84, "local.mercy", "mercy"))
        for user_id, local_id, canonical in contributors:
            self.store.record_events(
                user_id,
                [
                    {
                        "client_event_id": f"topic.{canonical}.v1",
                        "type": "topic_upsert",
                        "topic": {
                            "local_topic_id": local_id,
                            "name": canonical.title(),
                            "color": "#bbf7d0",
                        },
                    },
                    {
                        "client_event_id": f"verse.{canonical}.1",
                        "type": "verse_add",
                        "topic": {"local_topic_id": local_id},
                        "verse": {"book": 43, "chapter": 3, "verse": 16},
                    },
                    {
                        "client_event_id": f"verse.{canonical}.2",
                        "type": "verse_add",
                        "topic": {"local_topic_id": local_id},
                        "verse": {"book": 43, "chapter": 3, "verse": 17},
                    },
                ],
            )
            self.store.set_topic_mapping(
                user_id,
                local_id,
                canonical,
                state="mapped",
                actor="admin",
                canonical_definition={
                    "id": canonical,
                    "name": canonical.title(),
                    "color": "#bbf7d0",
                    "aliases": [],
                },
            )
        # 126 contributes nothing that is ever accepted.
        self.store.record_events(126, [_topic_event("topic.private.v1", name="Private")])
        for user_id in (42, 84):
            self.accept_every_event(
                user_id=user_id,
                bundle={
                    "schema_version": 1,
                    "topics": [
                        {"id": "grace", "name": "Grace", "color": "#bbf7d0", "aliases": []},
                        {"id": "mercy", "name": "Mercy", "color": "#bbf7d0", "aliases": []},
                    ],
                    "associations": {
                        "add": [
                            {"topic_id": topic, "book": 43, "chapter": 3, "verse": verse}
                            for topic in ("grace", "mercy")
                            for verse in (16, 17)
                        ],
                        "remove": [],
                    },
                },
            )

        update = self.store.record_live_catalog(
            5,
            _checksum("v5"),
            [
                _live_topic(verses=((43, 3, 16), (43, 3, 17))),
                _live_topic("mercy", name="Mercy", verses=((43, 3, 16), (43, 3, 17))),
            ],
        )
        self.assertEqual(len(update.newly_live_event_ids), 6)
        self.assertEqual(update.notified_contributor_ids, (42, 84))
        self.assertEqual(
            [contributor for contributor, _ in self.live_notifications()],
            [42, 84],
        )
        self.assertEqual(self.store.contribution_status(126)["summary"]["events"]["live"], 0)
        # The outbox is the ordinary one: the notice is claimable and its
        # message carries no moderation detail or other contributor's data.
        claimed = [
            notification
            for notification in self.store.claim_notifications(limit=10)
            if notification.kind == LIVE_NOTIFICATION_KIND
        ]
        self.assertEqual({notification.contributor_id for notification in claimed}, {42, 84})
        for notification in claimed:
            self.assertNotIn("84", notification.message)
            self.assertNotIn("42", notification.message)
            self.store.mark_notification_sent(notification.id, notification.claim_token)

    def test_live_catalog_input_is_validated_and_bounded(self) -> None:
        with self.assertRaisesRegex(ContributionError, "catalog_version"):
            self.store.record_live_catalog(0, _checksum("v1"), [])
        with self.assertRaisesRegex(ContributionError, "checksum"):
            self.store.record_live_catalog(1, "not-a-checksum", [])
        with self.assertRaisesRegex(ContributionError, "must be an array"):
            self.store.record_live_catalog(1, _checksum("v1"), {"id": "grace"})  # type: ignore[arg-type]
        with self.assertRaisesRegex(ContributionError, "fetched_at"):
            self.store.record_live_catalog(1, _checksum("v1"), [], fetched_at=-1)
        with self.assertRaisesRegex(ContributionError, "unsupported fields"):
            self.store.record_live_catalog(1, _checksum("v1"), [{"id": "grace"}])
        with self.assertRaisesRegex(ContributionError, "unsupported fields"):
            self.store.record_live_catalog(
                1, _checksum("v1"), [{**_live_topic(), "names": {"en": "Grace"}}]
            )
        with self.assertRaisesRegex(ContributionError, "unique"):
            self.store.record_live_catalog(1, _checksum("v1"), [_live_topic(), _live_topic()])
        with self.assertRaisesRegex(ContributionError, "Canonical topic ID"):
            self.store.record_live_catalog(1, _checksum("v1"), [_live_topic("Grace")])
        with self.assertRaisesRegex(ContributionError, "color"):
            self.store.record_live_catalog(1, _checksum("v1"), [_live_topic(color="green")])
        with self.assertRaisesRegex(ContributionError, "default"):
            self.store.record_live_catalog(
                1, _checksum("v1"), [{**_live_topic(), "default": 1}]
            )
        with self.assertRaisesRegex(ContributionError, "aliases"):
            self.store.record_live_catalog(
                1, _checksum("v1"), [{**_live_topic(), "aliases": "Grace"}]
            )
        with self.assertRaisesRegex(ContributionError, "triple"):
            self.store.record_live_catalog(
                1, _checksum("v1"), [{**_live_topic(), "verses": [[43, 3]]}]
            )
        with self.assertRaisesRegex(ContributionError, "chapter"):
            self.store.record_live_catalog(
                1, _checksum("v1"), [_live_topic(verses=((43, 22, 1),))]
            )
        with self.assertRaisesRegex(ContributionError, "book"):
            self.store.record_live_catalog(
                1, _checksum("v1"), [_live_topic(verses=((67, 1, 1),))]
            )
        with self.assertRaisesRegex(ContributionError, "too many topics"):
            self.store.record_live_catalog(
                1,
                _checksum("v1"),
                [_live_topic(f"topic-{index}") for index in range(MAX_LIVE_CATALOG_TOPICS + 1)],
            )
        self.assertIsNone(self.store.live_catalog())

        # Colours are normalised, duplicate verses collapse and the stored
        # form keeps only the reduced topic shape.
        update = self.store.record_live_catalog(
            1,
            _checksum("v1"),
            [
                {
                    **_live_topic(color="#BBF7D0", aliases=("Favour",), default=True),
                    "verses": [[43, 3, 17], [43, 3, 16], [43, 3, 16]],
                }
            ],
        )
        self.assertTrue(update.changed)
        live = self.store.live_catalog()
        assert live is not None
        self.assertEqual(len(live.associations), 2)
        with sqlite3.connect(self.path) as connection:
            stored = json.loads(
                connection.execute(
                    "SELECT topics_json FROM contribution_live_catalog"
                ).fetchone()[0]
            )
        self.assertEqual(
            stored,
            [
                {
                    "aliases": ["Favour"],
                    "color": "#bbf7d0",
                    "default": True,
                    "id": "grace",
                    "name": "Grace",
                    "verses": [[43, 3, 16], [43, 3, 17]],
                }
            ],
        )

        # A damaged stored document is a moderation problem that surfaces as
        # a ContributionError (a ValueError) rather than a crash.
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE contribution_live_catalog SET topics_json = '{'")
        self.store.close()
        self.store = ContributionStore(path=str(self.path))
        with self.assertRaisesRegex(ContributionError, "stored live catalogue"):
            self.store.live_catalog()

    def test_publication_state_exposes_pull_request_and_api_observation(self) -> None:
        initial = self.store.publication_state()
        self.assertNotIn("repo_token", initial)
        for key in ("repo_pull_request", "api_catalog_version", "api_checksum", "api_checked_at"):
            self.assertIsNone(initial[key])

        self.store.record_live_catalog(
            7, _checksum("v7"), [_live_topic()], fetched_at=1_700_000_000_000_000_000
        )
        observed = self.store.publication_state()
        self.assertEqual(observed["api_catalog_version"], 7)
        self.assertEqual(observed["api_checksum"], _checksum("v7"))
        self.assertEqual(observed["api_checked_at"], 1_700_000_000_000_000_000)
        self.assertIsNone(observed["repo_pull_request"])

    def test_repository_publication_records_the_pull_request(self) -> None:
        revision = self.store.publish_approved_events_atomically(_catalog(), [], actor="admin")
        token = self.store.begin_repo_publication(
            revision.revision, revision.checksum, actor="publisher"
        )
        with self.assertRaisesRegex(ContributionError, "https URL"):
            self.store.finish_repo_publication(
                token,
                revision.revision,
                state="pushed",
                actor="publisher",
                branch="contributions/one",
                commit="a" * 40,
                pull_request="http://github.com/getbible/v1_bookmark_builder/pull/12",
            )
        with self.assertRaisesRegex(ContributionError, "cannot carry a pull request"):
            self.store.finish_repo_publication(
                token,
                revision.revision,
                state="failed",
                actor="publisher",
                error="push refused",
                pull_request=PULL_REQUEST_URL,
            )
        self.store.finish_repo_publication(
            token,
            revision.revision,
            state="pushed",
            actor="publisher",
            branch="contributions/one",
            commit="a" * 40,
            pull_request=PULL_REQUEST_URL,
        )
        state = self.store.publication_state()
        self.assertEqual(state["repo_state"], "pushed")
        self.assertEqual(state["repo_pull_request"], PULL_REQUEST_URL)
        updated = [
            entry
            for entry in self.store.list_audit()
            if entry["action"] == "repository_publication_updated"
        ]
        self.assertEqual(updated[0]["detail"]["pull_request"], PULL_REQUEST_URL)

        # A push without an opened pull request stores None, and a new lease
        # forgets the previous pull request with the other lease fields.
        second = self.store.publish_approved_events_atomically(
            _empty_catalog(), [], actor="admin"
        )
        token = self.store.begin_repo_publication(
            second.revision, second.checksum, actor="publisher"
        )
        self.assertIsNone(self.store.publication_state()["repo_pull_request"])
        self.store.finish_repo_publication(
            token,
            second.revision,
            state="pushed",
            actor="publisher",
            branch="contributions/two",
            commit="b" * 40,
        )
        self.assertIsNone(self.store.publication_state()["repo_pull_request"])


if __name__ == "__main__":
    unittest.main()
