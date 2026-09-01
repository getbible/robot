import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from modules.contributions import (
    ContributionError,
    ContributionIdempotencyConflict,
    ContributionNotAllowed,
    ContributionNotificationConflict,
    ContributionPublicationConflict,
    ContributionRepositoryConflict,
    ContributionStore,
    normalize_catalog,
)


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
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)

        self.store = ContributionStore(path=str(self.path))
        application = self.store.application_for(42)
        self.assertIsNotNone(application)
        assert application is not None
        self.assertEqual(application.state, "approved")

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
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
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
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
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
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
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

    def test_open_repairs_missing_catalogue_seeds_for_v3_and_current_schema(
        self,
    ) -> None:
        for version in (3, 4):
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

    def test_snapshot_sync_baseline_and_deletion_diff_are_atomic(self) -> None:
        self.approve()

        baseline = self.store.synchronize_snapshot(
            42,
            sync_id="sync.baseline",
            client_id="browser.primary",
            snapshot=_snapshot(),
        )
        self.assertEqual(baseline.accepted, 2)
        self.assertFalse(baseline.replayed_sync)
        self.assertEqual(
            [event.event_type for event in self.store.list_events()],
            ["topic_upsert", "verse_add"],
        )

        deletion = self.store.synchronize_snapshot(
            42,
            sync_id="sync.deletion",
            client_id="browser.primary",
            snapshot={"topics": [], "assignments": []},
        )
        self.assertEqual(deletion.accepted, 2)
        self.assertEqual(
            [event.event_type for event in self.store.list_events()][-2:],
            ["verse_remove", "topic_delete"],
        )

    def test_snapshot_sync_exact_receipt_replays_after_reopen(self) -> None:
        self.approve()
        first = self.store.synchronize_snapshot(
            42,
            sync_id="sync.durable",
            client_id="browser.primary",
            snapshot=_snapshot(),
        )
        self.store.close()
        self.store = ContributionStore(path=str(self.path))

        replay = self.store.synchronize_snapshot(
            42,
            sync_id="sync.durable",
            client_id="browser.primary",
            snapshot=_snapshot(),
        )
        self.assertTrue(replay.replayed_sync)
        self.assertEqual(replay.accepted, first.accepted)
        self.assertEqual(replay.replayed, first.replayed)
        self.assertEqual(replay.event_ids, first.event_ids)
        self.assertEqual(replay.snapshot_digest, first.snapshot_digest)
        self.assertEqual(len(self.store.list_events()), 2)
        self.assertEqual([event.replay_count for event in self.store.list_events()], [0, 0])

    def test_snapshot_sync_idempotency_conflict_has_no_mutation(self) -> None:
        self.approve()
        first = self.store.synchronize_snapshot(
            42,
            sync_id="sync.conflict",
            client_id="browser.primary",
            snapshot={"topics": [], "assignments": []},
            operations=[_topic_event("explicit.first")],
        )
        events_before = self.store.list_events()

        with self.assertRaises(ContributionIdempotencyConflict):
            self.store.synchronize_snapshot(
                42,
                sync_id="sync.conflict",
                client_id="browser.primary",
                snapshot={"topics": [], "assignments": []},
                operations=[_topic_event("explicit.changed")],
            )

        self.assertEqual(self.store.list_events(), events_before)
        self.assertEqual(first.accepted, 1)

    def test_snapshot_sync_validation_and_disclosure_are_all_or_nothing(self) -> None:
        self.store.submit_application(42, first_name="Grace")
        self.store.decide_application(42, "approved", actor="admin")
        invalid = _snapshot()
        invalid["assignments"] = [
            {
                "topic_id": "local.grace",
                "book": 67,
                "chapter": 1,
                "verse": 1,
            }
        ]
        with self.assertRaises(ContributionError):
            self.store.synchronize_snapshot(
                42,
                sync_id="sync.invalid",
                client_id="browser.primary",
                snapshot=invalid,
                disclosure_acknowledged=True,
            )
        application = self.store.application_for(42)
        assert application is not None
        self.assertIsNone(application.disclosure_acknowledged_at)
        self.assertEqual(self.store.list_events(), ())

        accepted = self.store.synchronize_snapshot(
            42,
            sync_id="sync.acknowledged",
            client_id="browser.primary",
            snapshot=_snapshot(),
            disclosure_acknowledged=True,
        )
        self.assertEqual(accepted.accepted, 2)
        application = self.store.application_for(42)
        assert application is not None
        self.assertIsNotNone(application.disclosure_acknowledged_at)
        self.assertEqual(
            len(
                [
                    entry
                    for entry in self.store.list_audit()
                    if entry["action"] == "disclosure_acknowledged"
                ]
            ),
            1,
        )

    def test_snapshot_sync_enforces_distinct_verse_limit_without_writes(self) -> None:
        self.approve()
        oversized = _snapshot(
            assignments=tuple((1, 1, verse) for verse in range(1, 802))
        )
        with self.assertRaisesRegex(ContributionError, "800 unique verses"):
            self.store.synchronize_snapshot(
                42,
                sync_id="sync.too-many-verses",
                client_id="browser.primary",
                snapshot=oversized,
            )
        self.assertEqual(self.store.list_events(), ())

    def test_first_snapshot_reconstructs_legacy_journal_before_diffing(self) -> None:
        self.approve()
        self.store.record_events(42, [_topic_event(), _verse_event()])

        result = self.store.synchronize_snapshot(
            42,
            sync_id="sync.legacy-delete",
            client_id="browser.primary",
            snapshot={"topics": [], "assignments": []},
        )

        self.assertEqual(result.accepted, 2)
        self.assertEqual(
            [event.event_type for event in self.store.list_events()],
            ["topic_upsert", "verse_add", "verse_remove", "topic_delete"],
        )

    def test_second_client_first_snapshot_is_additive_not_legacy_reconstructed(self) -> None:
        self.approve()
        first = self.store.synchronize_snapshot(
            42,
            sync_id="sync.first-client",
            client_id="browser.primary",
            snapshot=_snapshot(),
        )
        second = self.store.synchronize_snapshot(
            42,
            sync_id="sync.second-client",
            client_id="browser.secondary",
            snapshot=_snapshot(
                topic_id="local.mercy",
                name="Mercy",
                color="#123456",
                assignments=(),
            ),
        )

        self.assertEqual(first.accepted, 2)
        self.assertEqual(second.accepted, 1)
        self.assertNotIn("topic_delete", [event.event_type for event in self.store.list_events()])

    def test_contributor_capability_persists_without_storing_raw_token(self) -> None:
        self.approve()
        token = self.store.issue_capability(42)
        self.assertRegex(token, r"\Agbc_[A-Za-z0-9_-]{43}\Z")
        with sqlite3.connect(self.path) as connection:
            stored = connection.execute(
                "SELECT token_digest FROM contributor_capabilities"
            ).fetchone()
            assert stored is not None
            self.assertNotIn(token.encode("ascii"), bytes(stored[0]))

        self.store.close()
        self.store = ContributionStore(path=str(self.path))
        self.assertEqual(self.store.authenticate_capability(token), 42)

    def test_contributor_capability_expires_and_is_pruned(self) -> None:
        self.approve()
        token = self.store.issue_capability(42)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE contributor_capabilities SET expires_at = 0")

        with self.assertRaises(ContributionNotAllowed):
            self.store.authenticate_capability(token)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM contributor_capabilities").fetchone()[0],
                0,
            )

    def test_contributor_capabilities_are_bounded_and_revoked_with_application(self) -> None:
        self.approve()
        tokens = [self.store.issue_capability(42) for _ in range(20)]
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM contributor_capabilities").fetchone()[0],
                16,
            )
        with self.assertRaises(ContributionNotAllowed):
            self.store.authenticate_capability(tokens[0])
        self.assertEqual(self.store.authenticate_capability(tokens[-1]), 42)

        self.store.decide_application(42, "revoked", actor="admin")
        with self.assertRaises(ContributionNotAllowed):
            self.store.authenticate_capability(tokens[-1])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM contributor_capabilities").fetchone()[0],
                0,
            )

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

        status = self.store.contribution_status(42)
        self.assertTrue(status["topics"][0]["published"])
        self.assertEqual(status["topics"][0]["canonical_topic_id"], "grace")
        self.assertEqual(status["topics"][0]["canonical_topic"]["name"], "Grace")

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


if __name__ == "__main__":
    unittest.main()
