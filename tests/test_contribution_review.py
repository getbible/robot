import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules.contributions import ContributionError, ContributionStore
from modules.getbible_query import VerseReference
from scripts.contribution_review import (
    Association,
    CanonicalTopic,
    ContributionBundle,
    GitPublisher,
    ReviewError,
    _all_events,
    _load_base_associations,
    _load_store,
    atomic_write,
    build_publication_plan,
    export_current_catalog,
    parse_porcelain_paths,
    publish_live,
    review_applications,
    review_topics,
    review_verses,
    sanitize_terminal,
    validate_aliases,
    validate_english_topic_name,
)

ROOT = Path(__file__).resolve().parents[1]
TOPICS = ROOT / "data" / "global-bookmarks" / "topics.json"
ASSOCIATIONS = ROOT / "data" / "global-bookmarks" / "tag-verse.csv"


def _topic_event(
    event_id: str,
    local_id: str,
    name: str,
    color: str = "#123456",
) -> dict[str, object]:
    return {
        "client_event_id": event_id,
        "type": "topic_upsert",
        "topic": {"local_topic_id": local_id, "name": name, "color": color},
    }


def _verse_event(
    event_id: str,
    local_id: str,
    *,
    operation: str = "verse_add",
    book: int = 1,
    chapter: int = 1,
    verse: int = 1,
) -> dict[str, object]:
    return {
        "client_event_id": event_id,
        "type": operation,
        "topic": {"local_topic_id": local_id},
        "verse": {"book": book, "chapter": chapter, "verse": verse},
    }


def _definition(index: int = 1) -> dict[str, object]:
    return {
        "id": f"review-topic-{index}",
        "name": f"Review Topic {index}",
        "color": "#123456",
        "aliases": [],
    }


def _approve(store: ContributionStore, user_id: int = 42) -> None:
    store.submit_application(user_id, first_name="Reviewer")
    store.decide_application(user_id, "approved", actor="test-admin")
    store.acknowledge_disclosure(user_id)


class ContributionBundleTestCase(unittest.TestCase):
    def test_terminal_text_is_single_line_control_free_and_bounded(self) -> None:
        hostile = "\x1b[31mred\x1b[0m\nnext\x00\tfield"
        self.assertEqual(sanitize_terminal(hostile), "red next field")
        self.assertEqual(sanitize_terminal("abcdef", maximum=4), "abc…")

    def test_bundle_is_deterministic_private_and_canon_bounded(self) -> None:
        bundle = ContributionBundle(
            topics=(CanonicalTopic("review-topic", "Review Topic", "#123456"),),
            additions=(Association("review-topic", 66, 22, 21),),
            removals=(),
        )
        payload = bundle.json_bytes()
        self.assertEqual(payload, bundle.normalized().json_bytes())
        self.assertNotIn(b"contributor", payload)
        with self.assertRaisesRegex(ReviewError, "book"):
            ContributionBundle.validated(
                {
                    "schema_version": 1,
                    "topics": [],
                    "associations": {
                        "add": [{"topic_id": "review-topic", "book": 67, "chapter": 1, "verse": 1}],
                        "remove": [],
                    },
                }
            )
        with self.assertRaisesRegex(ReviewError, "chapter"):
            ContributionBundle.validated(
                {
                    "schema_version": 1,
                    "topics": [],
                    "associations": {
                        "add": [{"topic_id": "review-topic", "book": 1, "chapter": 51, "verse": 1}],
                        "remove": [],
                    },
                }
            )
        with self.assertRaisesRegex(ReviewError, "derived slug"):
            ContributionBundle(
                (CanonicalTopic("arbitrary-id", "Review Topic", "#123456"),),
                (Association("arbitrary-id", 1, 1, 1),),
                (),
            ).json_bytes()

    def test_atomic_write_preserves_previous_file_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "catalogue.json"
            destination.write_bytes(b"previous")
            with (
                patch("scripts.contribution_review.os.replace", side_effect=OSError("fail")),
                self.assertRaises(OSError),
            ):
                atomic_write(destination, b"replacement")
            self.assertEqual(destination.read_bytes(), b"previous")
            self.assertEqual(list(destination.parent.glob(".*.tmp")), [])

    def test_store_open_failures_are_safe_operator_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corrupt = root / "corrupt.sqlite3"
            corrupt.write_bytes(b"not a sqlite database")
            with self.assertRaisesRegex(ReviewError, "opened safely"):
                _load_store(corrupt)

            newer = root / "newer.sqlite3"
            with sqlite3.connect(newer) as connection:
                connection.execute("PRAGMA user_version=99")
            with self.assertRaisesRegex(ReviewError, "opened safely"):
                _load_store(newer)

            missing = root / "permission.sqlite3"
            with (
                patch(
                    "modules.contributions.ContributionStore",
                    side_effect=sqlite3.OperationalError("permission denied: private path"),
                ),
                self.assertRaisesRegex(ReviewError, "opened safely"),
            ):
                _load_store(missing)

    def test_all_events_accepts_exact_scan_limit_and_rejects_true_overflow(self) -> None:
        class BoundaryStore:
            def __init__(self, *, overflow: bool) -> None:
                self.overflow = overflow
                self.calls: list[tuple[int, int]] = []

            def list_events(
                self,
                *,
                states: set[str] | None = None,
                types: set[str] | None = None,
                limit: int = 500,
                after_id: int = 0,
            ) -> list[dict[str, int]]:
                del states, types
                self.calls.append((limit, after_id))
                remaining = 250_000 - after_id
                if remaining > 0:
                    size = min(limit, remaining)
                    return [{"id": after_id + size}] * size
                return [{"id": 250_001}] if self.overflow else []

        exact = BoundaryStore(overflow=False)
        self.assertEqual(len(_all_events(exact)), 250_000)  # type: ignore[arg-type]
        self.assertEqual(exact.calls[-1], (1, 250_000))

        overflow = BoundaryStore(overflow=True)
        with self.assertRaisesRegex(ReviewError, "safety limit"):
            _all_events(overflow)  # type: ignore[arg-type]
        self.assertEqual(overflow.calls[-1], (1, 250_000))


class ContributionReviewIntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "state" / "contributions.sqlite3"
        self.store = ContributionStore(path=str(self.database))
        _approve(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def _map_new(self, index: int = 1, *, local_id: str | None = None) -> str:
        local = local_id or f"local.topic.{index}"
        definition = _definition(index)
        self.store.set_topic_mapping(
            42,
            local,
            str(definition["id"]),
            state="mapped",
            actor="test-admin",
            canonical_definition=definition,
            name=str(definition["name"]),
            color=str(definition["color"]),
            aliases=[],
        )
        return local

    def _approve_pending(self) -> None:
        for event in self.store.list_events(states={"pending", "deferred"}):
            self.store.decide_event(event.id, "approved", actor="test-admin")

    def _publish_topic_links(self, *verses: int) -> str:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.links", local, "Review Topic 1"),
                *[
                    _verse_event(f"verse.link.{verse}", local, verse=verse)
                    for verse in verses
                ],
            ],
        )
        self._map_new(local_id=local)
        self._approve_pending()
        publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        return local

    def test_removed_contributor_can_be_reinstated_from_the_review_menu(self) -> None:
        self.store.submit_application(77, first_name="Returning", username="second_chance")
        self.store.decide_application(77, "approved", actor="test-admin")
        self.store.acknowledge_disclosure(77)
        self.store.decide_application(77, "revoked", actor="test-admin")

        # A fresh /contributor request refreshes the profile but must never
        # silently resurrect a revoked record on its own.
        application, created = self.store.submit_application(77, first_name="Returning")
        self.assertFalse(created)
        self.assertEqual(application.state, "revoked")

        responses = iter([
            "n",  # skip the revocation review
            "y",  # open the reinstatement review
            "r",  # reinstate the removed contributor
            "",   # no decision note
        ])
        output = io.StringIO()
        review_applications(
            self.store,
            actor="test-admin",
            input_fn=lambda _prompt: next(responses),
            output=output,
        )

        self.assertIn("Removed contributor", output.getvalue())
        self.assertIn("Contributor reinstated", output.getvalue())
        self.assertEqual(self.store.application_for(77).state, "approved")
        # Reinstatement deliberately requires a fresh disclosure before any
        # new submission is accepted.
        self.assertTrue(self.store.contribution_status(77)["disclosure_required"])
        self.store.acknowledge_disclosure(77)
        result = self.store.record_events(
            77,
            [_topic_event("topic.back", "local.back", "Back Again")],
        )
        self.assertEqual(result.accepted, 1)

    def test_reinstatement_review_reports_when_nobody_was_removed(self) -> None:
        responses = iter(["n", "y"])
        output = io.StringIO()
        review_applications(
            self.store,
            actor="test-admin",
            input_fn=lambda _prompt: next(responses),
            output=output,
        )
        self.assertIn("No revoked or rejected contributors.", output.getvalue())

    def test_cli_topic_text_validation_matches_store_boundaries(self) -> None:
        local = "local.valid"
        self.store.record_events(42, [_topic_event("topic.valid", local, "Valid Topic")])

        for invalid_name in ("?Alpha", "Alpha?"):
            with self.subTest(name=invalid_name):
                with self.assertRaises(ReviewError):
                    validate_english_topic_name(invalid_name)
                with self.assertRaises(ContributionError):
                    self.store.set_topic_mapping(
                        42,
                        local,
                        "valid-topic",
                        state="mapped",
                        actor="test-admin",
                        name=invalid_name,
                        color="#123456",
                        aliases=[],
                    )

        for invalid_alias in ("?Alias", "Alias?", "12"):
            with self.subTest(alias=invalid_alias):
                with self.assertRaises(ReviewError):
                    validate_aliases([invalid_alias], name="Valid Topic")
                with self.assertRaises(ContributionError):
                    self.store.set_topic_mapping(
                        42,
                        local,
                        "valid-topic",
                        state="mapped",
                        actor="test-admin",
                        name="Valid Topic",
                        color="#123456",
                        aliases=[invalid_alias],
                    )

        source = self.store.list_source_topics()[0]
        self.assertEqual(source.state, "pending")
        self.assertIsNone(source.canonical_topic_id)

    def test_topic_resolution_also_reviews_topic_event(self) -> None:
        self.store.record_events(42, [_topic_event("topic.grace", "local.grace", "Grace")])
        answers = iter(("e", "grace", "1", "a"))
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: next(answers),
            output=io.StringIO(),
        )
        source = self.store.list_source_topics()[0]
        event = self.store.list_events()[0]
        self.assertEqual(source.state, "mapped")
        self.assertEqual(source.canonical_topic_id, "grace")
        self.assertIsNone(source.canonical_definition)
        self.assertEqual(event.state, "approved")

    def test_two_pending_topics_can_merge_into_one_corrected_definition(self) -> None:
        self.store.record_events(
            42,
            [
                _topic_event("topic.alpha", "local.alpha", "Alpha Topic"),
                _topic_event(
                    "topic.beta",
                    "local.beta",
                    "Beta Topic",
                    color="#654321",
                ),
            ],
        )
        answers = iter(("m", "1", "Merged Topic", "", "", "a", "a"))
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: next(answers),
            output=io.StringIO(),
        )
        sources = self.store.list_source_topics()
        self.assertEqual({source.state for source in sources}, {"mapped"})
        self.assertEqual(
            {source.canonical_topic_id for source in sources},
            {"merged-topic"},
        )
        definitions = self.store.list_canonical_topics()
        self.assertEqual(len(definitions), 1)
        self.assertEqual(definitions[0].name, "Merged Topic")
        self.assertEqual(
            {event.state for event in self.store.list_events()},
            {"approved"},
        )

    def test_pending_merge_cannot_overwrite_a_published_definition(self) -> None:
        published_local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.published", published_local, "Review Topic 1"),
                _verse_event("verse.published", published_local),
            ],
        )
        self._map_new(local_id=published_local)
        self._approve_pending()
        publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        self.store.record_events(
            42,
            [
                _topic_event("topic.alpha", "local.alpha", "Alpha Topic"),
                _topic_event(
                    "topic.beta",
                    "local.beta",
                    "Beta Topic",
                    color="#654321",
                ),
            ],
        )

        answers = iter(("m", "1", "Review Topic 1", "", ""))
        with self.assertRaisesRegex(ReviewError, "authoritative definition"):
            review_topics(
                self.store,
                actor="test-admin",
                topics_file=TOPICS,
                input_fn=lambda _prompt: next(answers),
                output=io.StringIO(),
            )

        pending = {
            source.local_topic_id: source
            for source in self.store.list_source_topics(states={"pending"})
        }
        self.assertEqual(set(pending), {"local.alpha", "local.beta"})
        self.assertTrue(all(source.canonical_topic_id is None for source in pending.values()))
        definition = self.store.list_canonical_topics()[0]
        self.assertEqual((definition.name, definition.color), ("Review Topic 1", "#123456"))

    def test_pending_merge_alias_collision_has_no_mapping_side_effect(self) -> None:
        self.store.record_events(
            42,
            [
                _topic_event("topic.alpha", "local.alpha", "Alpha Topic"),
                _topic_event(
                    "topic.beta",
                    "local.beta",
                    "Beta Topic",
                    color="#654321",
                ),
            ],
        )
        answers = iter(("m", "1", "Merged Topic", "", "Grace"))
        with self.assertRaisesRegex(ReviewError, "reuse an English name or alias"):
            review_topics(
                self.store,
                actor="test-admin",
                topics_file=TOPICS,
                input_fn=lambda _prompt: next(answers),
                output=io.StringIO(),
            )

        sources = self.store.list_source_topics()
        self.assertEqual({source.state for source in sources}, {"pending"})
        self.assertTrue(all(source.canonical_topic_id is None for source in sources))
        self.assertEqual(self.store.list_canonical_topics(), ())

    def test_deferred_topic_blocks_verse_review_before_query(self) -> None:
        self.store.record_events(
            42,
            [
                _topic_event("topic.deferred", "local.deferred", "Deferred Topic"),
                _verse_event("verse.deferred", "local.deferred"),
            ],
        )
        self.store.set_topic_mapping(
            42,
            "local.deferred",
            state="deferred",
            actor="test-admin",
        )

        class FailingClient:
            def fetch_verses(self, _references: object) -> object:
                raise AssertionError("Query API must not run before topic resolution")

        with self.assertRaisesRegex(ReviewError, "Resolve all pending"):
            review_verses(
                self.store,
                actor="test-admin",
                translation="kjv",
                topics_file=TOPICS,
                associations_file=ASSOCIATIONS,
                verse_client=FailingClient(),
            )

    def test_mapped_verse_waits_for_pending_or_deferred_topic_upsert(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.wait", local, "Review Topic 1"),
                _verse_event("verse.wait", local),
            ],
        )
        self._map_new(local_id=local)
        topic_event = self.store.list_events(types={"topic_upsert"})[0]
        self.store.decide_event(topic_event.id, "deferred", actor="test-admin")

        class FailingClient:
            def fetch_verses(self, _references: object) -> object:
                raise AssertionError("Query API must not run before topic approval")

        output = io.StringIO()
        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            verse_client=FailingClient(),
            output=output,
        )
        verse_event = self.store.list_events(types={"verse_add"})[0]
        self.assertEqual(verse_event.state, "deferred")
        self.assertIn("until its topic decision is final", output.getvalue())

    def test_rejected_new_topic_rejects_dependent_verse_and_planner_fails_closed(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.reject.dependency", local, "Review Topic 1"),
                _verse_event("verse.reject.dependency", local),
            ],
        )
        self._map_new(local_id=local)
        topic_event = self.store.list_events(types={"topic_upsert"})[0]
        verse_event = self.store.list_events(types={"verse_add"})[0]
        self.store.decide_event(topic_event.id, "rejected", actor="test-admin")

        class FailingClient:
            def fetch_verses(self, _references: object) -> object:
                raise AssertionError("Query API must not run for a rejected topic")

        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            verse_client=FailingClient(),
            output=io.StringIO(),
        )
        self.assertEqual(
            self.store.list_events(types={"verse_add"})[0].state,
            "rejected",
        )

        self.store.decide_event(verse_event.id, "approved", actor="test-admin")
        with self.assertRaisesRegex(ReviewError, "without an accepted canonical topic"):
            build_publication_plan(
                self.store,
                topics_file=TOPICS,
                associations_file=ASSOCIATIONS,
            )

    def test_rejected_later_metadata_edit_does_not_revoke_accepted_establishment(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.accepted.first", local, "Review Topic 1"),
                _verse_event("verse.after.rejected.edit", local),
            ],
        )
        self._map_new(local_id=local)
        first_upsert = self.store.list_events(types={"topic_upsert"})[0]
        self.store.decide_event(first_upsert.id, "approved", actor="test-admin")

        self.store.record_events(
            42,
            [
                _topic_event(
                    "topic.rejected.later",
                    local,
                    "Review Topic 1",
                    color="#654321",
                )
            ],
        )
        self.store.set_topic_mapping(
            42,
            local,
            "review-topic-1",
            state="mapped",
            actor="test-admin",
            canonical_definition=_definition(),
        )
        later_upsert = self.store.list_events(types={"topic_upsert"})[-1]
        self.store.decide_event(later_upsert.id, "rejected", actor="test-admin")

        class VerseClient:
            def fetch_verses(self, references: object) -> dict[VerseReference, object]:
                return {
                    VerseReference(*reference): SimpleNamespace(
                        display_reference="Genesis 1:1",
                        text="Authoritative text",
                    )
                    for reference in references
                }

        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        verse = self.store.list_events(types={"verse_add"})[0]
        self.assertEqual(verse.state, "approved")
        plan = build_publication_plan(
            self.store,
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
        )
        self.assertEqual({topic.id for topic in plan.bundle.topics}, {"review-topic-1"})
        self.assertIn(first_upsert.id, plan.event_ids)
        self.assertIn(verse.id, plan.event_ids)

    def test_baseline_core_topic_id_maps_to_authoritative_definition(self) -> None:
        self.store.record_events(
            42,
            [_verse_event("verse.grace.baseline", "grace", book=43, chapter=3, verse=16)],
        )
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        source = self.store.list_source_topics()[0]
        event = self.store.list_events()[0]
        self.assertEqual(source.state, "mapped")
        self.assertEqual(source.canonical_topic_id, "grace")
        self.assertEqual(source.name, "Grace")
        self.assertIsNone(source.canonical_definition)
        self.assertEqual(event.canonical_topic_id, "grace")

    def test_exact_core_id_fast_mapping_ignores_contributor_metadata(self) -> None:
        event = _verse_event(
            "verse.grace.with-context",
            "grace",
            book=43,
            chapter=3,
            verse=16,
        )
        event["topic"] = {
            "local_topic_id": "grace",
            "name": "Contributor Spelling",
            "color": "#123456",
        }
        self.store.record_events(42, [event])
        output = io.StringIO()
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: "yes",
            output=output,
        )
        source = self.store.list_source_topics()[0]
        self.assertEqual(source.state, "mapped")
        self.assertEqual(source.canonical_topic_id, "grace")
        self.assertEqual(source.name, "Grace")
        self.assertEqual(source.color, "#bbf7d0")
        self.assertIsNone(source.canonical_definition)
        self.assertIn("Proposed metadata will be ignored", output.getvalue())

    def test_rejecting_source_rejects_topic_and_verse_events(self) -> None:
        self.store.record_events(
            42,
            [
                _topic_event("topic.rejected", "local.rejected", "Rejected Topic"),
                _verse_event("verse.rejected", "local.rejected"),
            ],
        )
        answers = iter(("r", "not suitable"))
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: next(answers),
            output=io.StringIO(),
        )
        self.assertEqual(self.store.list_source_topics()[0].state, "rejected")
        self.assertEqual(
            {event.state for event in self.store.list_events()},
            {"rejected"},
        )

    def test_unpublished_mapped_source_can_be_deferred_without_invalid_mapping(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.initial.defer", local, "Review Topic 1"),
                _verse_event("verse.initial.defer", local),
            ],
        )
        self._map_new(local_id=local)
        self.store.record_events(
            42,
            [_topic_event("topic.reopen.defer", local, "Review Topic One")],
        )
        answers = iter(("d", "needs more review"))
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: next(answers),
            output=io.StringIO(),
        )
        source = self.store.list_source_topics()[0]
        self.assertEqual(source.state, "deferred")
        self.assertIsNone(source.canonical_topic_id)
        self.assertEqual({event.state for event in self.store.list_events()}, {"deferred"})

    def test_both_sides_of_conflicting_verse_changes_require_individual_review(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.conflict", local, "Review Topic 1"),
                _verse_event("verse.add", local),
                _verse_event("verse.remove", local, operation="verse_remove"),
            ],
        )
        self._map_new(local_id=local)
        topic_event = self.store.list_events(types={"topic_upsert"})[0]
        self.store.decide_event(topic_event.id, "approved", actor="test-admin")

        class VerseClient:
            def fetch_verses(self, references: object) -> dict[VerseReference, object]:
                return {
                    VerseReference(*reference): SimpleNamespace(
                        display_reference="Genesis 1:1",
                        text="Authoritative text",
                    )
                    for reference in references
                }

        answers = iter(("d", "conflict", "d", "conflict"))
        output = io.StringIO()
        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: next(answers),
            output=output,
            verse_client=VerseClient(),
        )
        self.assertNotIn("safe additions", output.getvalue())
        self.assertEqual(output.getvalue().count("conflict"), 2)
        self.assertEqual(
            {event.state for event in self.store.list_events(types={"verse_add", "verse_remove"})},
            {"deferred"},
        )

    def test_conflict_after_display_limit_prevents_safe_bulk_approval(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.boundary.conflict", local, "Review Topic 1"),
                _verse_event("verse.boundary.add.1", local),
            ],
        )
        self._map_new(local_id=local)
        topic_event = self.store.list_events(types={"topic_upsert"})[0]
        self.store.decide_event(topic_event.id, "approved", actor="test-admin")
        later_events = [
            *[
                _verse_event(f"verse.boundary.add.{verse}", local, verse=verse)
                for verse in range(2, 501)
            ],
            _verse_event("verse.boundary.remove.1", local, operation="verse_remove"),
        ]
        for start in range(0, len(later_events), 50):
            self.store.record_events(42, later_events[start : start + 50])

        class VerseClient:
            def fetch_verses(self, references: object) -> dict[VerseReference, object]:
                return {
                    VerseReference(*reference): SimpleNamespace(
                        display_reference=f"Genesis 1:{reference[2]}",
                        text="Authoritative text",
                    )
                    for reference in references
                }

        answers = iter(("d", "d", "boundary conflict"))
        output = io.StringIO()
        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: next(answers),
            output=output,
            verse_client=VerseClient(),
        )

        self.assertIn("499 safe additions", output.getvalue())
        self.assertIn("Review flags: conflict", output.getvalue())
        states = {
            event.client_event_id: event.state
            for event in _all_events(
                self.store,
                states={"pending", "deferred"},
                types={"verse_add", "verse_remove"},
            )
        }
        self.assertEqual(states["verse.boundary.add.1"], "deferred")
        self.assertEqual(states["verse.boundary.remove.1"], "pending")

    def test_live_publication_is_cumulative_durable_and_idempotent(self) -> None:
        local = "local.topic.1"
        first = self.store.record_events(
            42,
            [
                _topic_event("topic.1", local, "Review Topic 1"),
                _verse_event("verse.1", local, verse=1),
            ],
        )
        self._map_new(local_id=local)
        self._approve_pending()
        first_revision = publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        self.assertEqual(first_revision.revision, 1)
        self.assertEqual(
            {event.state for event in self.store.list_events()},
            {"applied"},
        )
        self.assertEqual(
            set(first.event_ids.values()), {event.id for event in self.store.list_events()}
        )
        export_path = Path(self.directory.name) / "reviewed.json"
        export_current_catalog(self.store, export_path, output=io.StringIO())
        checked = subprocess.run(
            [
                "node",
                str(ROOT / "scripts" / "import_contribution_bundle.mjs"),
                "--check",
                str(export_path),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(checked.returncode, 0, msg=checked.stderr or checked.stdout)

        self.store.close()
        self.store = ContributionStore(path=str(self.database))
        second = self.store.record_events(
            42,
            [_verse_event("verse.2", local, verse=2)],
        )
        second_event = next(
            event
            for event in self.store.list_events()
            if event.id == next(iter(second.event_ids.values()))
        )
        self.assertEqual(second_event.canonical_topic_id, "review-topic-1")

        class VerseClient:
            def fetch_verses(self, references: object) -> dict[VerseReference, object]:
                return {
                    VerseReference(*reference): SimpleNamespace(
                        display_reference=f"Genesis 1:{reference[2]}",
                        text="Authoritative text",
                    )
                    for reference in references
                }

        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        second_revision = publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        self.assertEqual(second_revision.revision, 2)
        current = ContributionBundle.validated(self.store.current_catalog().catalog)
        self.assertEqual({item.verse for item in current.additions}, {1, 2})

        removal = self.store.record_events(
            42,
            [_verse_event("verse.1.remove", local, operation="verse_remove", verse=1)],
        )
        removal_event = next(
            event
            for event in self.store.list_events()
            if event.id == next(iter(removal.event_ids.values()))
        )
        self.assertEqual(removal_event.canonical_topic_id, "review-topic-1")
        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        third_revision = publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        self.assertEqual(third_revision.revision, 3)
        current = ContributionBundle.validated(self.store.current_catalog().catalog)
        self.assertEqual({item.verse for item in current.additions}, {2})

        no_change = publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        self.assertIsNone(no_change)
        self.assertEqual(self.store.current_catalog().revision, 3)

    def test_export_metadata_is_bound_to_the_exact_written_snapshot(self) -> None:
        snapshot = self.store.current_catalog()
        replaced = SimpleNamespace(
            revision=snapshot.revision + 1,
            checksum="0" * 64,
            catalog=ContributionBundle.empty().as_dict(),
        )
        destination = Path(self.directory.name) / "snapshot.json"
        with patch.object(
            self.store,
            "current_catalog",
            side_effect=(snapshot, replaced),
        ) as current_catalog:
            exported = export_current_catalog(
                self.store,
                destination,
                output=io.StringIO(),
            )
        self.assertEqual(current_catalog.call_count, 1)
        self.assertEqual(exported.revision, snapshot.revision)
        self.assertEqual(exported.checksum, snapshot.checksum)
        self.assertEqual(destination.read_bytes(), exported.bundle.json_bytes())

    def test_live_publication_refuses_stale_plan_without_losing_newer_catalog(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.stale", local, "Review Topic 1"),
                _verse_event("verse.stale", local),
            ],
        )
        self._map_new(local_id=local)
        self._approve_pending()
        competing_store = ContributionStore(path=str(self.database))
        competing_bundle = ContributionBundle(
            (CanonicalTopic("competing-topic", "Competing Topic", "#654321"),),
            (Association("competing-topic", 1, 1, 2),),
            (),
        )

        def publish_competing_revision(_prompt: str) -> str:
            competing_store.publish_catalog(competing_bundle.as_dict(), actor="other-admin")
            return "yes"

        try:
            with self.assertRaisesRegex(ValueError, "changed"):
                publish_live(
                    self.store,
                    actor="test-admin",
                    topics_file=TOPICS,
                    associations_file=ASSOCIATIONS,
                    input_fn=publish_competing_revision,
                    output=io.StringIO(),
                )
            current = ContributionBundle.validated(self.store.current_catalog().catalog)
            self.assertEqual(current, competing_bundle.normalized())
            self.assertEqual(
                {event.state for event in self.store.list_events()},
                {"approved"},
            )
        finally:
            competing_store.close()

    def test_published_topic_cannot_be_renamed_into_a_split_canonical_id(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.initial", local, "Review Topic 1"),
                _verse_event("verse.initial", local),
            ],
        )
        self._map_new(local_id=local)
        self._approve_pending()
        publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )

        self.store.record_events(
            42,
            [
                _topic_event("topic.rename", local, "Renamed Topic"),
                _verse_event("verse.after.rename", local, verse=2),
            ],
        )
        rename_answers = iter(("n", "", "", ""))
        with self.assertRaisesRegex(ReviewError, "cannot be replaced"):
            review_topics(
                self.store,
                actor="test-admin",
                topics_file=TOPICS,
                input_fn=lambda _prompt: next(rename_answers),
                output=io.StringIO(),
            )
        source = self.store.list_source_topics()[0]
        self.assertEqual(source.canonical_topic_id, "review-topic-1")
        self.assertEqual(self.store.list_events(states={"pending"})[0].event_type, "topic_upsert")

        keep_answers = iter(("e", "review topic 1", "1", "a"))
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: next(keep_answers),
            output=io.StringIO(),
        )

        class VerseClient:
            def fetch_verses(self, references: object) -> dict[VerseReference, object]:
                return {
                    VerseReference(*reference): SimpleNamespace(
                        display_reference="Genesis 1:2",
                        text="Authoritative text",
                    )
                    for reference in references
                }

        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        current = ContributionBundle.validated(self.store.current_catalog().catalog)
        self.assertEqual({topic.id for topic in current.topics}, {"review-topic-1"})
        self.assertEqual(current.topics[0].name, "Review Topic 1")
        self.assertEqual({item.topic_id for item in current.additions}, {"review-topic-1"})
        self.assertEqual({item.verse for item in current.additions}, {1, 2})

    def test_live_overlay_compacts_after_repository_catalog_catches_up(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.compact", local, "Review Topic 1"),
                _verse_event("verse.compact", local),
            ],
        )
        self._map_new(local_id=local)
        self._approve_pending()
        publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )

        root = Path(self.directory.name) / "updated-base"
        root.mkdir()
        updated_topics = root / "topics.json"
        updated_associations = root / "tag-verse.csv"
        topic_payload = json.loads(TOPICS.read_text(encoding="utf-8"))
        topic_payload["topics"].append(
            {
                **_definition(),
                "default": False,
            }
        )
        updated_topics.write_text(
            json.dumps(topic_payload),
            encoding="utf-8",
        )
        updated_associations.write_text(
            ASSOCIATIONS.read_text(encoding="utf-8") + "1 1:1,Review Topic 1\n",
            encoding="utf-8",
        )
        plan = build_publication_plan(
            self.store,
            topics_file=updated_topics,
            associations_file=updated_associations,
        )
        self.assertEqual(plan.bundle, ContributionBundle.empty())
        compacted = publish_live(
            self.store,
            actor="test-admin",
            topics_file=updated_topics,
            associations_file=updated_associations,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        self.assertEqual(compacted.revision, 2)
        self.assertEqual(
            ContributionBundle.validated(self.store.current_catalog().catalog),
            ContributionBundle.empty(),
        )

        self.store.record_events(
            42,
            [
                _topic_event("topic.after.compact", "local.topic.2", "Review Topic 2"),
                _verse_event("verse.after.compact", "local.topic.2", verse=2),
            ],
        )
        self._map_new(2)
        self._approve_pending()
        after = build_publication_plan(
            self.store,
            topics_file=updated_topics,
            associations_file=updated_associations,
        )
        self.assertEqual({topic.id for topic in after.bundle.topics}, {"review-topic-2"})

    def test_zero_association_topic_is_not_published_or_applied(self) -> None:
        self.store.record_events(
            42,
            [_topic_event("topic.orphan", "local.topic.1", "Review Topic 1")],
        )
        self._map_new()
        self._approve_pending()
        plan = build_publication_plan(
            self.store,
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
        )
        self.assertEqual(plan.bundle.topics, ())
        self.assertEqual(plan.event_ids, ())
        self.assertEqual(self.store.list_events()[0].state, "approved")

    def test_unpublished_topic_definition_can_be_corrected_before_first_publication(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.correct.before.publish", local, "Review Topic 1"),
                _verse_event("verse.correct.before.publish", local),
            ],
        )
        answers = iter(("n", "Corrected Review Topic", "#654321", "Review Alias", "a"))
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: next(answers),
            output=io.StringIO(),
        )
        self._approve_pending()
        publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        topic = ContributionBundle.validated(self.store.current_catalog().catalog).topics[0]
        self.assertEqual(topic.id, "corrected-review-topic")
        self.assertEqual(topic.name, "Corrected Review Topic")
        self.assertEqual(topic.color, "#654321")
        self.assertEqual(topic.aliases, ("Review Alias",))

    def test_repeated_topic_edits_use_latest_source_definition(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.edit.first", local, "Review Topic 1"),
                _topic_event("topic.edit.second", local, "Review Topic 1"),
            ],
        )
        self._map_new(local_id=local)
        answers = iter(
            (
                "e",
                "Renamed Review Topic",
                "",
                "Current Alias",
                "e",
                "",
                "",
                "",
            )
        )
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: next(answers),
            output=io.StringIO(),
        )

        source = self.store.list_source_topics()[0]
        self.assertEqual(source.canonical_topic_id, "renamed-review-topic")
        self.assertEqual(source.name, "Renamed Review Topic")
        self.assertEqual(source.aliases, ("Current Alias",))
        events = self.store.list_events(types={"topic_upsert"})
        self.assertEqual({event.state for event in events}, {"approved"})
        self.assertEqual(
            {event.canonical_topic_id for event in events},
            {"renamed-review-topic"},
        )

    def test_topic_edit_refreshes_canonical_uniqueness_before_next_event(self) -> None:
        self.store.record_events(
            42,
            [
                _topic_event("topic.alias.first", "local.topic.1", "Review Topic 1"),
                _topic_event("topic.alias.second", "local.topic.2", "Review Topic 2"),
            ],
        )
        self._map_new(1)
        self._map_new(2)
        answers = iter(
            (
                "e",
                "",
                "",
                "Shared Review Alias",
                "e",
                "",
                "",
                "Shared Review Alias",
            )
        )
        with self.assertRaisesRegex(ReviewError, "reuse an English name or alias"):
            review_topics(
                self.store,
                actor="test-admin",
                topics_file=TOPICS,
                input_fn=lambda _prompt: next(answers),
                output=io.StringIO(),
            )

        sources = {
            source.local_topic_id: source for source in self.store.list_source_topics()
        }
        self.assertEqual(sources["local.topic.1"].aliases, ("Shared Review Alias",))
        self.assertEqual(sources["local.topic.2"].aliases, ())
        events = {
            event.client_event_id: event for event in self.store.list_events(types={"topic_upsert"})
        }
        self.assertEqual(events["topic.alias.first"].state, "approved")
        self.assertEqual(events["topic.alias.second"].state, "pending")

    def test_published_topic_definition_and_existence_are_immutable(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.initial", local, "Review Topic 1"),
                _verse_event("verse.initial", local, verse=1),
            ],
        )
        self._map_new(local_id=local)
        self._approve_pending()
        publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )

        self.store.record_events(
            42,
            [
                _topic_event(
                    "topic.recolour",
                    local,
                    "Review Topic 1",
                    color="#654321",
                ),
                _verse_event("verse.second", local, verse=2),
            ],
        )
        answers = iter(
            (
                "e",
                "review topic 1",
                "1",
                "e",
                "",
                "#654321",
                "",
            )
        )
        with self.assertRaisesRegex(ReviewError, "definition cannot be changed"):
            review_topics(
                self.store,
                actor="test-admin",
                topics_file=TOPICS,
                input_fn=lambda _prompt: next(answers),
                output=io.StringIO(),
            )
        alias_answers = iter(("e", "", "", "Changed Alias"))
        with self.assertRaisesRegex(ReviewError, "definition cannot be changed"):
            review_topics(
                self.store,
                actor="test-admin",
                topics_file=TOPICS,
                input_fn=lambda _prompt: next(alias_answers),
                output=io.StringIO(),
            )
        reject_answers = iter(("r", "Published topic metadata is locked"))
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: next(reject_answers),
            output=io.StringIO(),
        )

        class VerseClient:
            def fetch_verses(self, references: object) -> dict[VerseReference, object]:
                return {
                    VerseReference(*reference): SimpleNamespace(
                        display_reference=f"Genesis 1:{reference[2]}",
                        text="Authoritative text",
                    )
                    for reference in references
                }

        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        pending_batch = self.store.list_events(states={"approved"})
        self.assertEqual({event.event_type for event in pending_batch}, {"verse_add"})
        definition = self.store.list_canonical_topics()[0]
        self.assertEqual(definition.color, "#123456")
        self.assertEqual(definition.aliases, ())
        publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )

        self.store.record_events(
            42,
            [
                {
                    "client_event_id": "topic.delete",
                    "type": "topic_delete",
                    "topic": {"local_topic_id": local},
                },
                _verse_event(
                    "verse.second.remove",
                    local,
                    operation="verse_remove",
                    verse=2,
                ),
            ],
        )
        delete_answers = iter(
            ("e", "review topic 1", "1", "r", "Published topic is permanent")
        )
        delete_output = io.StringIO()
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: next(delete_answers),
            output=delete_output,
        )
        self.assertIn("permanent after their first live publication", delete_output.getvalue())
        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        current = ContributionBundle.validated(self.store.current_catalog().catalog)
        self.assertIn("review-topic-1", {topic.id for topic in current.topics})
        self.assertEqual(
            {item.verse for item in current.additions if item.topic_id == "review-topic-1"},
            {1},
        )
        delete_batch = [
            event
            for event in self.store.list_events()
            if event.client_event_id in {"topic.delete", "verse.second.remove"}
        ]
        self.assertEqual(
            {event.client_event_id: event.state for event in delete_batch},
            {"topic.delete": "rejected", "verse.second.remove": "applied"},
        )
        self.assertFalse(
            self.store.list_events(
                states={"pending", "deferred"},
                types={"verse_add", "verse_remove"},
            )
        )

    def test_prepublication_upsert_then_delete_terminalizes_the_full_chain(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.before-delete", local, "Review Topic 1"),
                {
                    "client_event_id": "topic.delete-before-live",
                    "type": "topic_delete",
                    "topic": {"local_topic_id": local},
                },
            ],
        )
        self._map_new(local_id=local)
        self._approve_pending()

        plan = build_publication_plan(
            self.store,
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
        )
        self.assertEqual(plan.bundle, ContributionBundle.empty())
        self.assertEqual(plan.event_ids, (1, 2))
        publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        self.assertEqual({event.state for event in self.store.list_events()}, {"applied"})
        self.assertEqual(self.store.published_topic_ids(), ())

    def test_prepublication_delete_then_upsert_waits_for_effective_coverage(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                {
                    "client_event_id": "topic.delete-first",
                    "type": "topic_delete",
                    "topic": {"local_topic_id": local},
                },
                _topic_event("topic.recreate", local, "Review Topic 1"),
            ],
        )
        self._map_new(local_id=local)
        self._approve_pending()

        uncovered = build_publication_plan(
            self.store,
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
        )
        self.assertEqual(uncovered.bundle, ContributionBundle.empty())
        self.assertEqual(uncovered.event_ids, ())

        self.store.record_events(42, [_verse_event("verse.recreate", local)])
        self._approve_pending()
        covered = build_publication_plan(
            self.store,
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
        )
        self.assertEqual({topic.id for topic in covered.bundle.topics}, {"review-topic-1"})
        self.assertEqual(covered.event_ids, (1, 2, 3))
        publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        self.assertEqual({event.state for event in self.store.list_events()}, {"applied"})

    def test_planner_rejects_delete_after_live_and_pushed_history(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.published", local, "Review Topic 1"),
                _verse_event("verse.published", local),
            ],
        )
        self._map_new(local_id=local)
        self._approve_pending()
        live = publish_live(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        lease = self.store.begin_repo_publication(
            live.revision,
            live.checksum,
            actor="test-publisher",
        )
        self.store.finish_repo_publication(
            lease,
            live.revision,
            state="pushed",
            actor="test-publisher",
            branch="contributions/test",
            commit="a" * 40,
        )
        # Even if the current overlay is later compacted/restored, immutable
        # catalogue history records that this ID may already exist in Git.
        self.store.publish_catalog(ContributionBundle.empty().as_dict(), actor="test-admin")
        self.assertEqual(self.store.published_topic_ids(), ("review-topic-1",))

        self.store.record_events(
            42,
            [
                {
                    "client_event_id": "topic.delete-after-push",
                    "type": "topic_delete",
                    "topic": {"local_topic_id": local},
                }
            ],
        )
        self.store.set_topic_mapping(
            42,
            local,
            "review-topic-1",
            state="mapped",
            actor="test-admin",
            name="Review Topic 1",
            color="#123456",
            aliases=[],
        )
        delete_event = self.store.list_events(types={"topic_delete"})[0]
        self.store.decide_event(
            delete_event.id,
            "approved",
            actor="test-admin",
            canonical_topic_id="review-topic-1",
        )
        with self.assertRaisesRegex(ReviewError, "delete permanent topic review-topic-1"):
            build_publication_plan(
                self.store,
                topics_file=TOPICS,
                associations_file=ASSOCIATIONS,
            )

    def test_core_delete_rejection_does_not_suppress_meaningful_removal(self) -> None:
        local = "local.core.grace"
        self.store.record_events(
            42,
            [
                _verse_event(
                    "verse.core.remove",
                    local,
                    operation="verse_remove",
                    book=56,
                    chapter=3,
                    verse=5,
                )
            ],
        )
        self.store.set_topic_mapping(
            42,
            local,
            "grace",
            state="mapped",
            actor="test-admin",
            name="Grace",
            color="#bbf7d0",
            aliases=[],
        )
        self.store.record_events(
            42,
            [
                {
                    "client_event_id": "topic.core.delete",
                    "type": "topic_delete",
                    "topic": {"local_topic_id": local},
                }
            ],
        )
        answers = iter(("e", "grace", "1", "r", "invalid core deletion"))
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: next(answers),
            output=io.StringIO(),
        )

        class VerseClient:
            def fetch_verses(self, references: object) -> dict[VerseReference, object]:
                return {
                    VerseReference(*reference): SimpleNamespace(
                        display_reference="Titus 3:5",
                        text="Authoritative text",
                    )
                    for reference in references
                }

        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        delete_event = self.store.list_events(types={"topic_delete"})[0]
        removal_event = self.store.list_events(types={"verse_remove"})[0]
        self.assertEqual(delete_event.state, "rejected")
        self.assertEqual(removal_event.state, "approved")

    def test_approved_delete_blocks_addition_but_allows_cross_contributor_removal(self) -> None:
        local = "local.topic.1"
        self.store.record_events(
            42,
            [
                _topic_event("topic.initial.delete-scope", local, "Review Topic 1"),
                _verse_event("verse.initial.delete-scope", local),
            ],
        )
        self._map_new(local_id=local)
        self._approve_pending()

        _approve(self.store, 43)
        other_local = "other.local.topic"
        self.store.record_events(
            43,
            [
                _verse_event(
                    "verse.other.remove",
                    other_local,
                    operation="verse_remove",
                ),
                _verse_event("verse.other.add", other_local, verse=2),
            ],
        )
        self.store.set_topic_mapping(
            43,
            other_local,
            "review-topic-1",
            state="mapped",
            actor="test-admin",
            canonical_definition=_definition(),
        )
        self.store.record_events(
            42,
            [
                {
                    "client_event_id": "topic.delete.cross-contributor",
                    "type": "topic_delete",
                    "topic": {"local_topic_id": local},
                }
            ],
        )
        delete_answers = iter(("e", "review topic 1", "1", "a"))
        review_topics(
            self.store,
            actor="test-admin",
            topics_file=TOPICS,
            input_fn=lambda _prompt: next(delete_answers),
            output=io.StringIO(),
        )

        class VerseClient:
            def fetch_verses(self, references: object) -> dict[VerseReference, object]:
                return {
                    VerseReference(*reference): SimpleNamespace(
                        display_reference=f"Genesis 1:{reference[2]}",
                        text="Authoritative text",
                    )
                    for reference in references
                }

        review_answers = iter(("a", "r", "delete conflict"))
        review_output = io.StringIO()
        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: next(review_answers),
            output=review_output,
            verse_client=VerseClient(),
        )
        other_events = {
            event.client_event_id: event
            for event in self.store.list_events()
            if event.contributor_id == 43
        }
        self.assertEqual(other_events["verse.other.remove"].state, "approved")
        self.assertEqual(other_events["verse.other.add"].state, "rejected")
        self.assertIn("approved deletion", review_output.getvalue())

    def test_effective_topic_limit_allows_39_new_and_refuses_40th(self) -> None:
        topic_events = []
        verse_events = []
        for index in range(1, 41):
            local = f"local.topic.{index}"
            topic_events.append(_topic_event(f"topic.{index}", local, f"Review Topic {index}"))
            chapter = 1 if index <= 31 else 2
            verse = index if index <= 31 else index - 31
            verse_events.append(
                _verse_event(
                    f"verse.{index}",
                    local,
                    chapter=chapter,
                    verse=verse,
                )
            )
        self.store.record_events(42, topic_events[:25] + verse_events[:25])
        self.store.record_events(42, topic_events[25:] + verse_events[25:])
        for index in range(1, 41):
            self._map_new(index)
        events = self.store.list_events()
        for event in events:
            if event.canonical_topic_id != "review-topic-40":
                self.store.decide_event(event.id, "approved", actor="test-admin")
        plan = build_publication_plan(
            self.store,
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
        )
        self.assertEqual(len(plan.bundle.topics), 39)

        for event in self.store.list_events(states={"pending"}):
            self.store.decide_event(event.id, "approved", actor="test-admin")
        with self.assertRaisesRegex(ReviewError, "100"):
            build_publication_plan(
                self.store,
                topics_file=TOPICS,
                associations_file=ASSOCIATIONS,
            )

    def test_topic_aliases_cannot_collide_with_core_or_contributed_names(self) -> None:
        self.store.record_events(
            42,
            [
                _topic_event("topic.alias.core", "local.audit", "Audit Topic"),
                _verse_event("verse.alias.core", "local.audit"),
            ],
        )
        self.store.set_topic_mapping(
            42,
            "local.audit",
            "audit-topic",
            state="mapped",
            actor="test-admin",
            canonical_definition={
                "id": "audit-topic",
                "name": "Audit Topic",
                "color": "#123456",
                "aliases": ["Grace"],
            },
        )
        self._approve_pending()
        with self.assertRaisesRegex(ReviewError, "reuse an English name or alias"):
            build_publication_plan(
                self.store,
                topics_file=TOPICS,
                associations_file=ASSOCIATIONS,
            )

        other_store = ContributionStore(
            path=str(Path(self.directory.name) / "state" / "alias-collision.sqlite3")
        )
        try:
            _approve(other_store)
            other_store.record_events(
                42,
                [
                    _topic_event("topic.alpha.alias", "local.alpha", "Alpha Topic"),
                    _verse_event("verse.alpha.alias", "local.alpha", verse=1),
                    _topic_event("topic.beta.alias", "local.beta", "Beta Topic"),
                    _verse_event("verse.beta.alias", "local.beta", verse=2),
                ],
            )
            for local_id, topic_id, name in (
                ("local.alpha", "alpha-topic", "Alpha Topic"),
                ("local.beta", "beta-topic", "Beta Topic"),
            ):
                other_store.set_topic_mapping(
                    42,
                    local_id,
                    topic_id,
                    state="mapped",
                    actor="test-admin",
                    canonical_definition={
                        "id": topic_id,
                        "name": name,
                        "color": "#123456",
                        "aliases": ["Shared Alias"],
                    },
                )
            for event in other_store.list_events():
                other_store.decide_event(event.id, "approved", actor="test-admin")
            with self.assertRaisesRegex(ReviewError, "reuse an English name or alias"):
                build_publication_plan(
                    other_store,
                    topics_file=TOPICS,
                    associations_file=ASSOCIATIONS,
                )
        finally:
            other_store.close()

    def test_review_defers_a_permanent_topics_last_link_removal(self) -> None:
        local = self._publish_topic_links(1)
        self.store.record_events(
            42,
            [
                _verse_event(
                    "verse.final.remove",
                    local,
                    operation="verse_remove",
                )
            ],
        )

        class VerseClient:
            def fetch_verses(self, references: object) -> dict[VerseReference, object]:
                return {
                    VerseReference(*reference): SimpleNamespace(
                        display_reference="Genesis 1:1",
                        text="Authoritative text",
                    )
                    for reference in references
                }

        output = io.StringIO()
        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: self.fail("The unsafe removal must be deferred"),
            output=output,
            verse_client=VerseClient(),
        )
        removal = self.store.list_events(types={"verse_remove"})[0]
        self.assertEqual(removal.state, "deferred")
        self.assertFalse(self.store.list_events(states={"approved"}))
        self.assertIn("final verse association", output.getvalue())
        plan = build_publication_plan(
            self.store,
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
        )
        self.assertEqual(plan.event_ids, ())

    def test_review_allows_a_permanent_topics_nonfinal_link_removal(self) -> None:
        local = self._publish_topic_links(1, 2)
        self.store.record_events(
            42,
            [
                _verse_event(
                    "verse.second.remove",
                    local,
                    operation="verse_remove",
                    verse=2,
                )
            ],
        )

        class VerseClient:
            def fetch_verses(self, references: object) -> dict[VerseReference, object]:
                return {
                    VerseReference(*reference): SimpleNamespace(
                        display_reference="Genesis 1:2",
                        text="Authoritative text",
                    )
                    for reference in references
                }

        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        removal = self.store.list_events(types={"verse_remove"})[0]
        self.assertEqual(removal.state, "approved")
        plan = build_publication_plan(
            self.store,
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
        )
        self.assertEqual(
            {item.verse for item in plan.bundle.additions if item.topic_id == "review-topic-1"},
            {1},
        )

    def test_sequential_removal_approvals_project_the_remaining_links(self) -> None:
        local = self._publish_topic_links(1, 2)
        self.store.record_events(
            42,
            [
                _verse_event(
                    "verse.first.remove",
                    local,
                    operation="verse_remove",
                    verse=1,
                ),
                _verse_event(
                    "verse.second.remove",
                    local,
                    operation="verse_remove",
                    verse=2,
                ),
            ],
        )

        class VerseClient:
            def fetch_verses(self, references: object) -> dict[VerseReference, object]:
                return {
                    VerseReference(*reference): SimpleNamespace(
                        display_reference=f"Genesis 1:{reference[2]}",
                        text="Authoritative text",
                    )
                    for reference in references
                }

        answers = iter(("a",))
        output = io.StringIO()
        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
            input_fn=lambda _prompt: next(answers),
            output=output,
            verse_client=VerseClient(),
        )
        removals = {
            event.client_event_id: event.state
            for event in self.store.list_events(types={"verse_remove"})
        }
        self.assertEqual(
            removals,
            {"verse.first.remove": "approved", "verse.second.remove": "deferred"},
        )
        self.assertIn("final verse association", output.getvalue())
        plan = build_publication_plan(
            self.store,
            topics_file=TOPICS,
            associations_file=ASSOCIATIONS,
        )
        self.assertEqual(
            {item.verse for item in plan.bundle.additions if item.topic_id == "review-topic-1"},
            {2},
        )

    def test_removing_last_association_requires_explicit_topic_resolution(self) -> None:
        core_associations = sorted(
            (
                item
                for item in _load_base_associations(TOPICS, ASSOCIATIONS)
                if item.topic_id == "wisdom-cause"
            ),
            key=lambda item: (item.book, item.chapter, item.verse),
        )
        self.assertTrue(core_associations)
        self.store.record_events(
            42,
            [
                _verse_event(
                    f"wisdom.remove.{index}",
                    "wisdom-cause",
                    operation="verse_remove",
                    book=item.book,
                    chapter=item.chapter,
                    verse=item.verse,
                )
                for index, item in enumerate(core_associations, 1)
            ],
        )
        definition = next(
            topic
            for topic in json.loads(TOPICS.read_text(encoding="utf-8"))["topics"]
            if topic["id"] == "wisdom-cause"
        )
        self.store.set_topic_mapping(
            42,
            "wisdom-cause",
            "wisdom-cause",
            state="mapped",
            actor="test-admin",
            name=definition["name"],
            color=definition["color"],
            aliases=definition["aliases"],
        )
        self._approve_pending()
        with self.assertRaisesRegex(ReviewError, "without verse associations"):
            build_publication_plan(
                self.store,
                topics_file=TOPICS,
                associations_file=ASSOCIATIONS,
            )

        contributed_store = ContributionStore(
            path=str(Path(self.directory.name) / "state" / "last-link.sqlite3")
        )
        try:
            _approve(contributed_store)
            contributed_store.record_events(
                42,
                [
                    _topic_event("topic.last.link", "local.last", "Last Link Topic"),
                    _verse_event("verse.last.link", "local.last"),
                ],
            )
            contributed_store.set_topic_mapping(
                42,
                "local.last",
                "last-link-topic",
                state="mapped",
                actor="test-admin",
                canonical_definition={
                    "id": "last-link-topic",
                    "name": "Last Link Topic",
                    "color": "#123456",
                    "aliases": [],
                },
            )
            for event in contributed_store.list_events():
                contributed_store.decide_event(event.id, "approved", actor="test-admin")
            publish_live(
                contributed_store,
                actor="test-admin",
                topics_file=TOPICS,
                associations_file=ASSOCIATIONS,
                input_fn=lambda _prompt: "yes",
                output=io.StringIO(),
            )
            removal = contributed_store.record_events(
                42,
                [
                    _verse_event(
                        "verse.last.link.remove",
                        "local.last",
                        operation="verse_remove",
                    )
                ],
            )
            contributed_store.decide_event(
                next(iter(removal.event_ids.values())),
                "approved",
                actor="test-admin",
            )
            with self.assertRaisesRegex(ReviewError, "without verse associations"):
                build_publication_plan(
                    contributed_store,
                    topics_file=TOPICS,
                    associations_file=ASSOCIATIONS,
                )
        finally:
            contributed_store.close()


class FakeProcessRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.remote_collision = True
        self.fetch_url = "git@github.com:getbible/robot.git"
        self.push_url = "git@github.com:getbible/robot.git"
        self.index_flags = "H bot.py\0"
        self.sparse_checkout: str | None = None
        self.fetch_head = "a" * 40
        self.committed_tree = "c" * 40
        self.local_name: str | None = "GetBible Contribution Publisher"
        self.local_email: str | None = "publisher@getbible.net"

    def __call__(self, arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        if arguments[:2] == ["node", "--version"]:
            return subprocess.CompletedProcess(arguments, 0, "v22.17.1\n", "")
        if arguments[:2] == ["npm", "--version"]:
            return subprocess.CompletedProcess(arguments, 0, "10.9.2\n", "")
        if arguments[0] == "node" or arguments[0] == "npm":
            return subprocess.CompletedProcess(arguments, 0, "", "")
        git_arguments = arguments[3:]
        while git_arguments[:1] == ["-c"]:
            git_arguments = git_arguments[2:]
        if git_arguments == ["rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(arguments, 0, "true\n", "")
        if git_arguments == ["rev-parse", "--path-format=absolute", "--show-toplevel"]:
            return subprocess.CompletedProcess(arguments, 0, f"{arguments[2]}\n", "")
        if git_arguments == ["rev-parse", "--absolute-git-dir"]:
            return subprocess.CompletedProcess(arguments, 0, f"{arguments[2]}/.git\n", "")
        if git_arguments == ["rev-parse", "--path-format=absolute", "--git-common-dir"]:
            return subprocess.CompletedProcess(arguments, 0, f"{arguments[2]}/.git\n", "")
        if git_arguments == ["remote", "get-url", "--all", "origin"]:
            return subprocess.CompletedProcess(arguments, 0, f"{self.fetch_url}\n", "")
        if git_arguments == ["remote", "get-url", "--push", "--all", "origin"]:
            return subprocess.CompletedProcess(arguments, 0, f"{self.push_url}\n", "")
        if git_arguments == ["config", "--local", "--get", "user.name"]:
            if self.local_name is None:
                return subprocess.CompletedProcess(arguments, 1, "", "")
            return subprocess.CompletedProcess(arguments, 0, f"{self.local_name}\n", "")
        if git_arguments == ["config", "--local", "--get", "user.email"]:
            if self.local_email is None:
                return subprocess.CompletedProcess(arguments, 1, "", "")
            return subprocess.CompletedProcess(arguments, 0, f"{self.local_email}\n", "")
        if git_arguments[:4] == ["config", "--bool", "--get", "core.sparseCheckout"]:
            if self.sparse_checkout is not None:
                return subprocess.CompletedProcess(arguments, 0, f"{self.sparse_checkout}\n", "")
            return subprocess.CompletedProcess(arguments, 1, "", "")
        if git_arguments[:4] == ["config", "--bool", "--get", "core.sparseCheckoutCone"]:
            return subprocess.CompletedProcess(arguments, 1, "", "")
        if git_arguments == ["ls-files", "-v", "-z"]:
            return subprocess.CompletedProcess(arguments, 0, self.index_flags, "")
        if git_arguments[:2] == ["status", "--porcelain=v1"]:
            output = " M data/global-bookmarks/topics.json\0" if "-z" in git_arguments else ""
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if git_arguments == [
            "rev-parse",
            "--verify",
            "refs/remotes/origin/master^{commit}",
        ]:
            return subprocess.CompletedProcess(arguments, 0, "a" * 40 + "\n", "")
        if git_arguments == ["rev-parse", "--verify", "FETCH_HEAD^{commit}"]:
            return subprocess.CompletedProcess(arguments, 0, self.fetch_head + "\n", "")
        if git_arguments[:3] == ["show-ref", "--verify", "--quiet"]:
            return subprocess.CompletedProcess(arguments, 1, "", "")
        if git_arguments[:3] == ["ls-remote", "--exit-code", "--heads"]:
            if self.remote_collision:
                self.remote_collision = False
                return subprocess.CompletedProcess(arguments, 0, "collision\n", "")
            return subprocess.CompletedProcess(arguments, 2, "", "")
        if git_arguments == ["write-tree"]:
            return subprocess.CompletedProcess(arguments, 0, "c" * 40 + "\n", "")
        if git_arguments[:3] == ["hash-object", "--no-filters", "--"]:
            return subprocess.CompletedProcess(arguments, 0, "d" * 40 + "\n", "")
        if git_arguments[:3] == ["rev-parse", "--verify", ":data/global-bookmarks/topics.json"]:
            return subprocess.CompletedProcess(arguments, 0, "d" * 40 + "\n", "")
        if git_arguments == ["rev-parse", "--verify", "HEAD^{commit}"]:
            return subprocess.CompletedProcess(arguments, 0, "b" * 40 + "\n", "")
        if git_arguments == ["rev-parse", "--verify", "HEAD^{tree}"]:
            return subprocess.CompletedProcess(arguments, 0, self.committed_tree + "\n", "")
        if git_arguments == ["rev-list", "--parents", "--max-count=1", "HEAD"]:
            return subprocess.CompletedProcess(arguments, 0, f"{'b' * 40} {'a' * 40}\n", "")
        if git_arguments[:5] == [
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
        ]:
            return subprocess.CompletedProcess(
                arguments,
                0,
                "data/global-bookmarks/topics.json\0",
                "",
            )
        return subprocess.CompletedProcess(arguments, 0, "", "")


class GitPublisherTestCase(unittest.TestCase):
    @staticmethod
    def _publisher_checkout(root: Path) -> Path:
        checkout = root / "checkout"
        for relative in (
            ".git",
            "scripts",
            "data/global-bookmarks",
            "miniapp/lib",
        ):
            (checkout / relative).mkdir(parents=True, exist_ok=True)
        for relative in (
            ".git/HEAD",
            ".git/config",
            ".git/index",
            "bot.py",
            "setup.sh",
            "scripts/import_contribution_bundle.mjs",
            "data/global-bookmarks/tag-verse.csv",
            "data/global-bookmarks/topics.json",
            "miniapp/package.json",
            "miniapp/lib/bookmark-topic-definitions.js",
            "miniapp/lib/global-bookmark-data.js",
            "miniapp/lib/messages.en.js",
        ):
            (checkout / relative).write_text("", encoding="utf-8")
        return checkout

    @staticmethod
    def _bundle_file(root: Path) -> tuple[Path, ContributionBundle]:
        bundle_path = root / "bundle.json"
        bundle = ContributionBundle(
            (CanonicalTopic("review-topic", "Review Topic", "#123456"),),
            (Association("review-topic", 1, 1, 1),),
            (),
        )
        bundle_path.write_bytes(bundle.json_bytes())
        return bundle_path, bundle

    def test_porcelain_parser_preserves_status_and_unusual_paths(self) -> None:
        self.assertEqual(
            parse_porcelain_paths(
                " M data/global-bookmarks/topics.json\0"
                "M  miniapp/lib/global-bookmark-data.js\0"
                "?? data/global-bookmarks/name with spaces\nline\0"
            ),
            [
                "data/global-bookmarks/name with spaces\nline",
                "data/global-bookmarks/topics.json",
                "miniapp/lib/global-bookmark-data.js",
            ],
        )
        for malformed in ("M missing-index-space\0", "R  renamed\0", "C  copied\0"):
            with self.subTest(malformed=malformed), self.assertRaises(ReviewError):
                parse_porcelain_paths(malformed)

    def test_publisher_checks_identity_tooling_remote_collision_and_pushes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = self._publisher_checkout(root)
            bundle_path, bundle = self._bundle_file(root)
            runner = FakeProcessRunner()
            output = io.StringIO()
            publisher = GitPublisher(
                checkout=checkout,
                expected_user="publisher",
                runner=runner,
                stdout=output,
            )
            with patch.object(publisher, "_validate_identity"):
                result = publisher.publish(bundle_path)
            self.assertTrue(result.branch.endswith("-2"))
            self.assertEqual(result.commit, "b" * 40)
            self.assertIn("Pushed contributions/", output.getvalue())
            self.assertTrue(
                any("push" in call and "--set-upstream" in call for call in runner.calls)
            )
            self.assertTrue(
                any(
                    "+refs/heads/master:refs/remotes/origin/master" in call for call in runner.calls
                )
            )
            self.assertTrue(
                all("core.hooksPath=/dev/null" in call for call in runner.calls if call[0] == "git")
            )
            for key in ("user.name", "user.email"):
                self.assertTrue(
                    any(
                        call[-4:] == ["config", "--local", "--get", key]
                        for call in runner.calls
                    )
                )

    def test_publisher_requires_checkout_local_git_identity_before_mutation(self) -> None:
        for attribute, message in (
            ("local_name", "requires local Git user.name"),
            ("local_email", "requires local Git user.email"),
        ):
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                checkout = self._publisher_checkout(root)
                bundle_path, _bundle = self._bundle_file(root)
                runner = FakeProcessRunner()
                setattr(runner, attribute, None)
                publisher = GitPublisher(
                    checkout=checkout,
                    expected_user="publisher",
                    runner=runner,
                )
                with (
                    patch.object(publisher, "_validate_identity"),
                    self.assertRaisesRegex(ReviewError, message),
                ):
                    publisher.publish(bundle_path)
                self.assertFalse(
                    any(call[0] in {"node", "npm"} for call in runner.calls),
                )
                self.assertFalse(
                    any(
                        argument in {"fetch", "switch", "add", "commit", "push"}
                        for call in runner.calls
                        for argument in call
                    ),
                )

    def test_publisher_rejects_unsafe_checkout_local_git_identity(self) -> None:
        runner = FakeProcessRunner()
        publisher = GitPublisher(
            checkout=Path("/unused").resolve(),
            expected_user="publisher",
            runner=runner,
        )
        for attribute, value, message in (
            ("local_name", "Publisher\nInjected", "unsafe local Git user.name"),
            ("local_email", "publisher.example", "unsafe local Git user.email"),
            ("local_email", "p" * 255 + "@example.net", "unsafe local Git user.email"),
        ):
            with self.subTest(attribute=attribute, value=value):
                runner.local_name = "GetBible Contribution Publisher"
                runner.local_email = "publisher@getbible.net"
                setattr(runner, attribute, value)
                with self.assertRaisesRegex(ReviewError, message):
                    publisher._validate_git_identity()

    def test_publisher_rejects_noncanonical_push_url_and_unsafe_index_modes(self) -> None:
        runner = FakeProcessRunner()
        runner.push_url = "git@github.com:attacker/fork.git"
        publisher = GitPublisher(
            checkout=Path("/unused").resolve(),
            expected_user="publisher",
            runner=runner,
        )
        with self.assertRaisesRegex(ReviewError, "push URL"):
            publisher._validate_origin()

        runner.push_url = "git@github.com:getbible/robot.git"
        runner.index_flags = "h scripts/import_contribution_bundle.mjs\0"
        with self.assertRaisesRegex(ReviewError, "assume-unchanged"):
            publisher._validate_index_mode()

        runner.index_flags = "H bot.py\0"
        runner.sparse_checkout = "true"
        with self.assertRaisesRegex(ReviewError, "sparse checkout"):
            publisher._validate_index_mode()

    def test_publisher_rejects_symlink_and_writable_critical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = self._publisher_checkout(Path(directory))
            package = checkout / "miniapp" / "package.json"
            package.unlink()
            package.symlink_to(checkout / "bot.py")
            publisher = GitPublisher(checkout=checkout, expected_user="publisher")
            with self.assertRaisesRegex(ReviewError, "regular file"):
                publisher._validate_checkout_filesystem()

        with tempfile.TemporaryDirectory() as directory:
            checkout = self._publisher_checkout(Path(directory))
            importer = checkout / "scripts" / "import_contribution_bundle.mjs"
            importer.chmod(0o666)
            publisher = GitPublisher(checkout=checkout, expected_user="publisher")
            with self.assertRaisesRegex(ReviewError, "group- or other-writable"):
                publisher._validate_checkout_filesystem()

    def test_publisher_verifies_fetch_head_and_final_commit_tree(self) -> None:
        for attribute, value, message in (
            ("fetch_head", "e" * 40, "fetched origin/master"),
            ("committed_tree", "e" * 40, "commit tree changed"),
        ):
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                checkout = self._publisher_checkout(root)
                bundle_path, _bundle = self._bundle_file(root)
                runner = FakeProcessRunner()
                setattr(runner, attribute, value)
                publisher = GitPublisher(
                    checkout=checkout,
                    expected_user="publisher",
                    runner=runner,
                )
                with (
                    patch.object(publisher, "_validate_identity"),
                    self.assertRaisesRegex(ReviewError, message),
                ):
                    publisher.publish(bundle_path)

    def test_publisher_refuses_bundle_swapped_after_revision_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_path = root / "bundle.json"
            bundle_path.write_bytes(
                ContributionBundle(
                    (CanonicalTopic("review-topic", "Review Topic", "#123456"),),
                    (Association("review-topic", 1, 1, 1),),
                    (),
                ).json_bytes()
            )
            runner = FakeProcessRunner()
            with self.assertRaisesRegex(ReviewError, "publication lease"):
                GitPublisher(
                    checkout=root / "checkout",
                    expected_user="publisher",
                    runner=runner,
                ).publish(bundle_path, expected_bundle_checksum="0" * 64)
            self.assertEqual(runner.calls, [])

    def test_publisher_refuses_root_and_unexpected_import_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            publisher = GitPublisher(checkout=checkout, expected_user="root")
            with (
                patch("scripts.contribution_review.os.geteuid", return_value=0),
                self.assertRaisesRegex(ReviewError, "non-root"),
            ):
                publisher._validate_identity()
        with self.assertRaisesRegex(ReviewError, "unexpected paths"):
            GitPublisher._validate_changed_paths(["private/telegram-users.json"])


if __name__ == "__main__":
    unittest.main()
