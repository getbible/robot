import contextlib
import inspect
import io
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from modules.contributions import ContributionError, ContributionStore
from modules.getbible_bookmarks import BookmarksTransportError, parse_catalog_document
from modules.getbible_query import VerseReference
from scripts.contribution_review import (
    BUILDER_TOKEN_ENVIRONMENT_VARIABLE,
    EXPECTED_GITHUB_REPOSITORY,
    GITHUB_API_PULLS_URL,
    MAX_EFFECTIVE_TOPICS,
    AcceptanceCancelled,
    Association,
    CanonicalTopic,
    ContributionBundle,
    GitPublisher,
    ReviewError,
    _all_events,
    _load_base_associations,
    _load_canonical_topics,
    _load_store,
    accept_contributions,
    atomic_write,
    build_publication_plan,
    compare_url_for,
    export_current_catalog,
    fetch_catalog,
    main,
    parse_porcelain_paths,
    print_status,
    review_applications,
    review_topics,
    review_verses,
    sanitize_terminal,
    validate_aliases,
    validate_english_topic_name,
)

ROOT = Path(__file__).resolve().parents[1]

# A small catalogue in the exact shape the public Bookmarks API publishes as
# catalog.json.  It stands in for the saved document that ``fetch-catalog``
# writes and every review command reads through ``--catalog-file``.
CATALOG_TOPICS: list[dict[str, object]] = [
    {
        "id": "faith",
        "name": "Faith",
        "color": "#fde68a",
        "aliases": ["Trust in God"],
        "default": True,
        "verses": [[40, 17, 20], [58, 11, 1], [58, 11, 6]],
    },
    {
        "id": "grace",
        "name": "Grace",
        "color": "#bbf7d0",
        "aliases": [],
        "default": True,
        "verses": [[43, 1, 17], [43, 3, 16], [45, 3, 24], [45, 5, 20], [49, 2, 8], [56, 3, 5]],
    },
    {
        "id": "prayer",
        "name": "Prayer",
        "color": "#93c5fd",
        "aliases": [],
        "default": True,
        "verses": [[40, 6, 9], [52, 5, 17]],
    },
    {
        "id": "wisdom-cause",
        "name": "Wisdom Cause",
        "color": "#fef08a",
        "aliases": [],
        "default": False,
        "verses": [[20, 1, 7], [20, 9, 10], [59, 1, 5]],
    },
]
_FIXTURES = tempfile.TemporaryDirectory()
CATALOG = Path(_FIXTURES.name) / "bookmarks-catalog.json"


def catalog_document(topics: list[dict[str, object]] | None = None) -> bytes:
    payload = {
        "schema_version": 1,
        "topics": list(CATALOG_TOPICS if topics is None else topics),
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def setUpModule() -> None:
    CATALOG.write_bytes(catalog_document())


def tearDownModule() -> None:
    _FIXTURES.cleanup()


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
        accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
        accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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
                catalog_file=CATALOG,
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
                catalog_file=CATALOG,
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
                catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
                catalog_file=CATALOG,
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
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        verse = self.store.list_events(types={"verse_add"})[0]
        self.assertEqual(verse.state, "approved")
        plan = build_publication_plan(
            self.store,
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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

    def test_acceptance_is_cumulative_durable_and_idempotent(self) -> None:
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
        first_revision = accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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
        exported = export_current_catalog(self.store, export_path, output=io.StringIO())
        # The export is the unchanged schema-1 bundle the builder's
        # ``import-bundle`` command consumes: only these members, no identities.
        document = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertEqual(set(document), {"schema_version", "topics", "associations"})
        self.assertEqual(ContributionBundle.read(export_path), exported.bundle)
        self.assertEqual({topic.id for topic in exported.bundle.topics}, {"review-topic-1"})

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
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        second_revision = accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        third_revision = accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        self.assertEqual(third_revision.revision, 3)
        current = ContributionBundle.validated(self.store.current_catalog().catalog)
        self.assertEqual({item.verse for item in current.additions}, {2})

        no_change = accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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

    def test_acceptance_refuses_stale_plan_without_losing_newer_revision(self) -> None:
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
                accept_contributions(
                    self.store,
                    actor="test-admin",
                    catalog_file=CATALOG,
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
        accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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
                catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        current = ContributionBundle.validated(self.store.current_catalog().catalog)
        self.assertEqual({topic.id for topic in current.topics}, {"review-topic-1"})
        self.assertEqual(current.topics[0].name, "Review Topic 1")
        self.assertEqual({item.topic_id for item in current.additions}, {"review-topic-1"})
        self.assertEqual({item.verse for item in current.additions}, {1, 2})

    def test_accepted_ledger_compacts_after_the_api_catalogue_catches_up(self) -> None:
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
        accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )

        # Upstream merged the pull request and the API now publishes the topic.
        updated_catalog = Path(self.directory.name) / "updated-catalog.json"
        updated_catalog.write_bytes(
            catalog_document(
                [
                    *CATALOG_TOPICS,
                    {**_definition(), "default": False, "verses": [[1, 1, 1]]},
                ]
            )
        )
        plan = build_publication_plan(self.store, catalog_file=updated_catalog)
        self.assertEqual(plan.bundle, ContributionBundle.empty())
        compacted = accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=updated_catalog,
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
        after = build_publication_plan(self.store, catalog_file=updated_catalog)
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
            input_fn=lambda _prompt: next(answers),
            output=io.StringIO(),
        )
        self._approve_pending()
        accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
                catalog_file=CATALOG,
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
        accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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
                catalog_file=CATALOG,
                input_fn=lambda _prompt: next(answers),
                output=io.StringIO(),
            )
        alias_answers = iter(("e", "", "", "Changed Alias"))
        with self.assertRaisesRegex(ReviewError, "definition cannot be changed"):
            review_topics(
                self.store,
                actor="test-admin",
                catalog_file=CATALOG,
                input_fn=lambda _prompt: next(alias_answers),
                output=io.StringIO(),
            )
        reject_answers = iter(("r", "Published topic metadata is locked"))
        review_topics(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        pending_batch = self.store.list_events(states={"approved"})
        self.assertEqual({event.event_type for event in pending_batch}, {"verse_add"})
        definition = self.store.list_canonical_topics()[0]
        self.assertEqual(definition.color, "#123456")
        self.assertEqual(definition.aliases, ())
        accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
            input_fn=lambda _prompt: next(delete_answers),
            output=delete_output,
        )
        self.assertIn("permanent once accepted for the shared catalogue", delete_output.getvalue())
        review_verses(
            self.store,
            actor="test-admin",
            translation="kjv",
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
        )
        self.assertEqual(plan.bundle, ContributionBundle.empty())
        self.assertEqual(plan.event_ids, (1, 2))
        accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
        )
        self.assertEqual(uncovered.bundle, ContributionBundle.empty())
        self.assertEqual(uncovered.event_ids, ())

        self.store.record_events(42, [_verse_event("verse.recreate", local)])
        self._approve_pending()
        covered = build_publication_plan(
            self.store,
            catalog_file=CATALOG,
        )
        self.assertEqual({topic.id for topic in covered.bundle.topics}, {"review-topic-1"})
        self.assertEqual(covered.event_ids, (1, 2, 3))
        accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        self.assertEqual({event.state for event in self.store.list_events()}, {"applied"})

    def test_planner_rejects_delete_after_acceptance_and_pushed_history(self) -> None:
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
        accepted = accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "yes",
            output=io.StringIO(),
        )
        lease = self.store.begin_repo_publication(
            accepted.revision,
            accepted.checksum,
            actor="test-publisher",
        )
        self.store.finish_repo_publication(
            lease,
            accepted.revision,
            state="pushed",
            actor="test-publisher",
            branch="contributions/test",
            commit="a" * 40,
        )
        # Even if the ledger is later compacted/restored, immutable revision
        # history records that this ID may already exist upstream.
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
                catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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

    def test_effective_topic_limit_counts_the_api_catalogue_and_refuses_the_next(self) -> None:
        allowed = MAX_EFFECTIVE_TOPICS - len(CATALOG_TOPICS)
        topic_events = []
        verse_events = []
        for index in range(1, allowed + 2):
            local = f"local.topic.{index}"
            topic_events.append(_topic_event(f"topic.{index}", local, f"Review Topic {index}"))
            verse_events.append(
                _verse_event(
                    f"verse.{index}",
                    local,
                    chapter=(index - 1) // 31 + 1,
                    verse=(index - 1) % 31 + 1,
                )
            )
        events = topic_events + verse_events
        for start in range(0, len(events), 50):
            self.store.record_events(42, events[start : start + 50])
        for index in range(1, allowed + 2):
            self._map_new(index)
        last_topic = f"review-topic-{allowed + 1}"
        for event in self.store.list_events():
            if event.canonical_topic_id != last_topic:
                self.store.decide_event(event.id, "approved", actor="test-admin")
        plan = build_publication_plan(self.store, catalog_file=CATALOG)
        self.assertEqual(len(plan.bundle.topics), allowed)

        for event in self.store.list_events(states={"pending"}):
            self.store.decide_event(event.id, "approved", actor="test-admin")
        with self.assertRaisesRegex(ReviewError, str(MAX_EFFECTIVE_TOPICS)):
            build_publication_plan(self.store, catalog_file=CATALOG)

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
                catalog_file=CATALOG,
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
                    catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
            input_fn=lambda _prompt: "a",
            output=io.StringIO(),
            verse_client=VerseClient(),
        )
        removal = self.store.list_events(types={"verse_remove"})[0]
        self.assertEqual(removal.state, "approved")
        plan = build_publication_plan(
            self.store,
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
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
            catalog_file=CATALOG,
        )
        self.assertEqual(
            {item.verse for item in plan.bundle.additions if item.topic_id == "review-topic-1"},
            {2},
        )

    def test_removing_last_association_requires_explicit_topic_resolution(self) -> None:
        core_associations = sorted(
            (
                item
                for item in _load_base_associations(CATALOG)
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
            for topic in json.loads(CATALOG.read_text(encoding="utf-8"))["topics"]
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
                catalog_file=CATALOG,
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
            accept_contributions(
                contributed_store,
                actor="test-admin",
                catalog_file=CATALOG,
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
                    catalog_file=CATALOG,
                )
        finally:
            contributed_store.close()


class FakeProcessRunner:
    """Answer the publisher's git and builder invocations without a checkout."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.remote_collision = True
        self.fetch_url = "git@github.com:getbible/v1_bookmark_builder.git"
        self.push_url = "git@github.com:getbible/v1_bookmark_builder.git"
        self.index_flags = "H src/builder.py\0"
        self.sparse_checkout: str | None = None
        self.fetch_head = "a" * 40
        self.committed_tree = "c" * 40
        self.local_name: str | None = "GetBible Contribution Publisher"
        self.local_email: str | None = "publisher@getbible.net"
        self.python_version = "Python 3.12.4\n"
        self.git_version = "git version 2.43.0\n"
        self.changed_paths = ["data/links/review-topic.json", "data/topics.json"]

    def __call__(self, arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        environment = kwargs.get("env")
        if isinstance(environment, dict):
            self.environments.append(environment)
        if arguments[0] != "git":
            # The builder interpreter: a version probe or src/builder.py.
            if arguments[1:] == ["--version"]:
                return subprocess.CompletedProcess(arguments, 0, self.python_version, "")
            return subprocess.CompletedProcess(
                arguments, 0, "OK: 5 topics, 15 verse links, 0 locales.\n", ""
            )
        if arguments[1:] == ["--version"]:
            return subprocess.CompletedProcess(arguments, 0, self.git_version, "")
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
            output = ""
            if "-z" in git_arguments:
                output = " M data/topics.json\0?? data/links/review-topic.json\0"
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if git_arguments == ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
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
        if git_arguments[:2] == ["rev-parse", "--verify"] and git_arguments[2].startswith(":"):
            return subprocess.CompletedProcess(arguments, 0, "d" * 40 + "\n", "")
        if git_arguments == ["rev-parse", "--verify", "HEAD^{commit}"]:
            return subprocess.CompletedProcess(arguments, 0, "b" * 40 + "\n", "")
        if git_arguments == ["rev-parse", "--verify", "HEAD^{tree}"]:
            return subprocess.CompletedProcess(arguments, 0, self.committed_tree + "\n", "")
        if git_arguments == ["rev-list", "--parents", "--max-count=1", "HEAD"]:
            return subprocess.CompletedProcess(arguments, 0, f"{'b' * 40} {'a' * 40}\n", "")
        if git_arguments[:5] == ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z"]:
            return subprocess.CompletedProcess(
                arguments,
                0,
                "".join(f"{path}\0" for path in self.changed_paths),
                "",
            )
        return subprocess.CompletedProcess(arguments, 0, "", "")


class FakeGitHubResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self.headers: dict[str, str] = {}

    def getcode(self) -> int:
        return self.status

    def read(self, amount: int = -1) -> bytes:
        return self.body

    def __enter__(self) -> "FakeGitHubResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class FakeUrlopen:
    def __init__(
        self,
        response: FakeGitHubResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.requests: list[tuple[object, object]] = []

    def __call__(self, request: object, timeout: object = None) -> FakeGitHubResponse:
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


PULL_REQUEST_URL = f"https://github.com/{EXPECTED_GITHUB_REPOSITORY}/pull/12"
TOKEN = "github_pat_example_secret_value"


class GitPublisherTestCase(unittest.TestCase):
    @staticmethod
    def _publisher_checkout(root: Path) -> Path:
        checkout = root / "checkout"
        for relative in (".git", "src", "data/links", "data/locales"):
            (checkout / relative).mkdir(parents=True, exist_ok=True)
        for relative in (
            ".git/HEAD",
            ".git/config",
            ".git/index",
            "src/builder.py",
            "data/topics.json",
            "data/links/review-topic.json",
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

    def _publish(
        self,
        root: Path,
        runner: FakeProcessRunner,
        **options: object,
    ) -> tuple[object, io.StringIO, ContributionBundle]:
        checkout = self._publisher_checkout(root)
        bundle_path, bundle = self._bundle_file(root)
        output = io.StringIO()
        publisher = GitPublisher(
            checkout=checkout,
            expected_user="publisher",
            runner=runner,
            stdout=output,
            **options,  # type: ignore[arg-type]
        )
        with patch.object(publisher, "_validate_identity"):
            result = publisher.publish(bundle_path)
        return result, output, bundle

    def test_porcelain_parser_preserves_status_and_unusual_paths(self) -> None:
        self.assertEqual(
            parse_porcelain_paths(
                " M data/topics.json\0"
                "?? data/links/review-topic.json\0"
                "?? data/links/name with spaces\nline\0"
            ),
            [
                "data/links/name with spaces\nline",
                "data/links/review-topic.json",
                "data/topics.json",
            ],
        )
        for malformed in ("M missing-index-space\0", "R  renamed\0", "C  copied\0"):
            with self.subTest(malformed=malformed), self.assertRaises(ReviewError):
                parse_porcelain_paths(malformed)

    def test_publisher_imports_validates_commits_and_pushes_a_builder_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeProcessRunner()
            result, output, bundle = self._publish(root, runner, environment={})
            self.assertTrue(result.branch.startswith("contributions/"))
            self.assertTrue(result.branch.endswith(f"-{bundle.checksum[:10]}-2"))
            self.assertEqual(result.commit, "b" * 40)
            self.assertIsNone(result.pull_request)
            self.assertIn("Pushed contributions/", output.getvalue())
            self.assertIn(compare_url_for(result.branch), output.getvalue())

            bundle_argument = str((root / "bundle.json").resolve())
            builder_calls = [call for call in runner.calls if call[0] != "git"]
            self.assertEqual(
                builder_calls,
                [
                    ["python3", "--version"],
                    ["python3", "src/builder.py", "import-bundle", bundle_argument],
                    ["python3", "src/builder.py", "validate"],
                ],
            )
            self.assertIn(["git", "--version"], runner.calls)
            self.assertFalse(any(call[0] in {"node", "npm"} for call in runner.calls))

            def position(*fragment: str) -> int:
                return next(
                    index
                    for index, call in enumerate(runner.calls)
                    if all(part in call for part in fragment)
                )

            self.assertLess(position("import-bundle"), position("validate"))
            self.assertLess(position("validate"), position("add", "--"))
            self.assertLess(position("commit", "--message"), position("push", "--set-upstream"))
            self.assertTrue(
                any(
                    call[-4:] == ["push", "--set-upstream", "origin", result.branch]
                    for call in runner.calls
                )
            )
            self.assertTrue(
                any("+refs/heads/main:refs/remotes/origin/main" in call for call in runner.calls)
            )
            self.assertFalse(
                any("master" in argument for call in runner.calls for argument in call)
            )
            self.assertTrue(
                all(
                    "core.hooksPath=/dev/null" in call
                    for call in runner.calls
                    if call[:2] == ["git", "-C"]
                )
            )
            commit_call = next(call for call in runner.calls if "commit" in call)
            self.assertEqual(
                commit_call[-1],
                "Add reviewed getBible robot bookmark contributions\n\n"
                "Topics: 1\n"
                "Verse associations: 1\n"
                f"Bundle SHA-256: {bundle.checksum}\n",
            )
            add_call = next(call for call in runner.calls if "add" in call)
            self.assertEqual(
                add_call[-2:], ["data/links/review-topic.json", "data/topics.json"]
            )
            for key in ("user.name", "user.email"):
                self.assertTrue(
                    any(call[-4:] == ["config", "--local", "--get", key] for call in runner.calls)
                )

    def test_publisher_uses_the_configured_interpreter_and_requires_python_3_12(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeProcessRunner()
            result, _output, _bundle = self._publish(
                root, runner, environment={}, builder_python="/opt/python3.12/bin/python3"
            )
            self.assertEqual(result.commit, "b" * 40)
            builder_calls = [call for call in runner.calls if call[0] != "git"]
            self.assertTrue(builder_calls)
            self.assertTrue(all(call[0] == "/opt/python3.12/bin/python3" for call in builder_calls))

        for version in ("Python 3.11.9\n", "Python 2.7.18\n", "nonsense\n", ""):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner = FakeProcessRunner()
                runner.python_version = version
                with self.assertRaisesRegex(ReviewError, "Python 3.12 or newer"):
                    self._publish(root, runner, environment={})
                self.assertFalse(
                    any(
                        argument in {"fetch", "switch", "import-bundle", "push"}
                        for call in runner.calls
                        for argument in call
                    )
                )

        with tempfile.TemporaryDirectory() as directory:
            runner = FakeProcessRunner()
            runner.git_version = "not git\n"
            with self.assertRaisesRegex(ReviewError, "usable git"):
                self._publish(Path(directory), runner, environment={})

        for invalid in ("", "python3 -u", "../python", "py;rm", "relative/python"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ReviewError, "command name or an absolute path"
            ):
                GitPublisher(
                    checkout=Path("/unused"),
                    expected_user="publisher",
                    builder_python=invalid,
                )

    def test_publisher_requires_checkout_local_git_identity_before_mutation(self) -> None:
        for attribute, message in (
            ("local_name", "requires local Git user.name"),
            ("local_email", "requires local Git user.email"),
        ):
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runner = FakeProcessRunner()
                setattr(runner, attribute, None)
                with self.assertRaisesRegex(ReviewError, message):
                    self._publish(root, runner, environment={})
                self.assertFalse(any(call[0] != "git" for call in runner.calls))
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

    def test_publisher_accepts_every_canonical_builder_origin_form_only(self) -> None:
        runner = FakeProcessRunner()
        publisher = GitPublisher(
            checkout=Path("/unused").resolve(),
            expected_user="publisher",
            runner=runner,
        )
        for url in (
            "https://github.com/getbible/v1_bookmark_builder.git",
            "https://github.com/getbible/v1_bookmark_builder",
            "https://github.com/getbible/v1_bookmark_builder/",
            "HTTPS://GITHUB.COM/GetBible/V1_Bookmark_Builder.git",
            "ssh://git@github.com/getbible/v1_bookmark_builder.git",
            "ssh://git@github.com/getbible/v1_bookmark_builder",
            "git@github.com:getbible/v1_bookmark_builder.git",
            "git@github.com:getbible/v1_bookmark_builder",
        ):
            with self.subTest(url=url):
                runner.fetch_url = url
                runner.push_url = url
                publisher._validate_origin()

        canonical = "git@github.com:getbible/v1_bookmark_builder.git"
        for url, label in (
            ("git@github.com:getbible/robot.git", "fetch URL"),
            ("https://github.com/attacker/v1_bookmark_builder.git", "fetch URL"),
            ("https://gitlab.com/getbible/v1_bookmark_builder.git", "fetch URL"),
            ("https://github.com/getbible/v1_bookmark_builder.git/extra", "fetch URL"),
            ("", "fetch URL"),
        ):
            with self.subTest(url=url):
                runner.fetch_url = url
                runner.push_url = canonical
                with self.assertRaisesRegex(ReviewError, label):
                    publisher._validate_origin()
        runner.fetch_url = canonical
        runner.push_url = "git@github.com:attacker/fork.git"
        with self.assertRaisesRegex(ReviewError, "push URL"):
            publisher._validate_origin()

        runner.push_url = canonical
        runner.index_flags = "h src/builder.py\0"
        with self.assertRaisesRegex(ReviewError, "assume-unchanged"):
            publisher._validate_index_mode()

        runner.index_flags = "H src/builder.py\0"
        runner.sparse_checkout = "true"
        with self.assertRaisesRegex(ReviewError, "sparse checkout"):
            publisher._validate_index_mode()

    def test_publisher_requires_the_builder_layout_and_safe_critical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = self._publisher_checkout(Path(directory))
            topics = checkout / "data" / "topics.json"
            topics.unlink()
            topics.symlink_to(checkout / "src" / "builder.py")
            publisher = GitPublisher(checkout=checkout, expected_user="publisher")
            with self.assertRaisesRegex(ReviewError, "regular file"):
                publisher._validate_checkout_filesystem()

        with tempfile.TemporaryDirectory() as directory:
            checkout = self._publisher_checkout(Path(directory))
            (checkout / "src" / "builder.py").chmod(0o666)
            publisher = GitPublisher(checkout=checkout, expected_user="publisher")
            with self.assertRaisesRegex(ReviewError, "group- or other-writable"):
                publisher._validate_checkout_filesystem()

        with tempfile.TemporaryDirectory() as directory:
            checkout = self._publisher_checkout(Path(directory))
            (checkout / "data" / "locales").rmdir()
            publisher = GitPublisher(checkout=checkout, expected_user="publisher")
            with self.assertRaisesRegex(ReviewError, "unavailable"):
                publisher._validate_checkout_filesystem()

        with tempfile.TemporaryDirectory() as directory:
            # A getbible/robot checkout is not a builder checkout.
            checkout = Path(directory) / "robot"
            for relative in (".git", "scripts", "miniapp"):
                (checkout / relative).mkdir(parents=True)
            for relative in (".git/HEAD", ".git/config", ".git/index", "bot.py", "setup.sh"):
                (checkout / relative).write_text("", encoding="utf-8")
            publisher = GitPublisher(checkout=checkout, expected_user="publisher")
            with self.assertRaisesRegex(ReviewError, "unavailable"):
                publisher._validate_checkout_filesystem()

    def test_publisher_verifies_fetch_head_and_final_commit_tree(self) -> None:
        for attribute, value, message in (
            ("fetch_head", "e" * 40, "fetched origin/main"),
            ("committed_tree", "e" * 40, "commit tree changed"),
        ):
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as directory:
                runner = FakeProcessRunner()
                setattr(runner, attribute, value)
                with self.assertRaisesRegex(ReviewError, message):
                    self._publish(Path(directory), runner, environment={})

    def test_publisher_refuses_bundle_swapped_after_revision_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_path, _bundle = self._bundle_file(root)
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
        GitPublisher._validate_changed_paths(
            ["data/links/review-topic.json", "data/links/gods-judgment.json", "data/topics.json"]
        )
        for unexpected in (
            ["private/telegram-users.json"],
            ["data/locales/fr.json"],
            ["src/builder.py"],
            ["data/links/Bad_Slug.json"],
            ["data/links/nested/topic.json"],
            ["data/links/-leading.json"],
            ["data/topics.json", "README.md"],
        ):
            with self.subTest(paths=unexpected), self.assertRaisesRegex(
                ReviewError, "unexpected paths"
            ):
                GitPublisher._validate_changed_paths(unexpected)

        with tempfile.TemporaryDirectory() as directory:
            runner = FakeProcessRunner()
            runner.changed_paths = ["data/locales/fr.json", "data/topics.json"]
            with self.assertRaisesRegex(ReviewError, "unexpected paths"):
                self._publish(Path(directory), runner, environment={})
            self.assertFalse(any("push" in call for call in runner.calls))

    def test_pull_request_is_opened_with_the_builder_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeProcessRunner()
            urlopen = FakeUrlopen(
                FakeGitHubResponse(201, json.dumps({"html_url": PULL_REQUEST_URL}).encode())
            )
            result, output, bundle = self._publish(
                Path(directory),
                runner,
                environment={BUILDER_TOKEN_ENVIRONMENT_VARIABLE: TOKEN},
                urlopen=urlopen,
                instance_name="alpha",
            )
            self.assertEqual(result.pull_request, PULL_REQUEST_URL)
            self.assertEqual(result.commit, "b" * 40)
            self.assertIn(f"Opened pull request {PULL_REQUEST_URL}", output.getvalue())
            self.assertNotIn(TOKEN, output.getvalue())

            self.assertEqual(len(urlopen.requests), 1)
            request, timeout = urlopen.requests[0]
            self.assertEqual(request.full_url, GITHUB_API_PULLS_URL)
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(timeout, 30.0)
            self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
            self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
            self.assertEqual(request.get_header("X-github-api-version"), "2022-11-28")
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(payload["head"], result.branch)
            self.assertEqual(payload["base"], "main")
            self.assertTrue(payload["title"].startswith("Reviewed bookmark contributions "))
            self.assertTrue(payload["title"].endswith(f"({bundle.checksum[:10]})"))
            self.assertIn("- Topics: 1", payload["body"])
            self.assertIn("- Verse associations added: 1", payload["body"])
            self.assertIn(f"- Bundle SHA-256: {bundle.checksum}", payload["body"])
            self.assertIn("- Robot instance: alpha", payload["body"])
            self.assertNotIn("contributor", payload["body"].casefold())

            # The push happened before the pull request, and no child process
            # ever saw the token.
            push_index = next(
                index for index, call in enumerate(runner.calls) if "push" in call
            )
            self.assertEqual(push_index, len(runner.calls) - 1)
            self.assertTrue(runner.environments)
            self.assertFalse(
                any(BUILDER_TOKEN_ENVIRONMENT_VARIABLE in env for env in runner.environments)
            )

    def test_pull_request_is_skipped_without_a_token_and_the_compare_url_is_printed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            urlopen = FakeUrlopen()
            result, output, _bundle = self._publish(
                Path(directory),
                FakeProcessRunner(),
                environment={"HOME": "/srv/publisher"},
                urlopen=urlopen,
            )
            self.assertIsNone(result.pull_request)
            self.assertEqual(urlopen.requests, [])
            self.assertIn(
                f"https://github.com/{EXPECTED_GITHUB_REPOSITORY}/compare/main..."
                f"{result.branch}?expand=1",
                output.getvalue(),
            )
            self.assertIn(BUILDER_TOKEN_ENVIRONMENT_VARIABLE, output.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            urlopen = FakeUrlopen()
            result, output, _bundle = self._publish(
                Path(directory),
                FakeProcessRunner(),
                environment={BUILDER_TOKEN_ENVIRONMENT_VARIABLE: "bad token\nvalue"},
                urlopen=urlopen,
            )
            self.assertIsNone(result.pull_request)
            self.assertEqual(urlopen.requests, [])
            self.assertIn("malformed", output.getvalue())
            self.assertNotIn("bad token", output.getvalue())

    def test_pull_request_api_failure_keeps_the_push_result(self) -> None:
        cases = (
            (
                FakeUrlopen(error=HTTPError(GITHUB_API_PULLS_URL, 422, "Unprocessable", {}, None)),
                "HTTP 422",
            ),
            (FakeUrlopen(error=URLError("connection refused")), "could not be reached"),
            (FakeUrlopen(error=TimeoutError()), "could not be reached"),
            (
                FakeUrlopen(FakeGitHubResponse(201, b"<html>not json</html>")),
                "unexpected response",
            ),
            (
                FakeUrlopen(
                    FakeGitHubResponse(
                        201,
                        json.dumps({"html_url": "https://github.com/attacker/x/pull/1"}).encode(),
                    )
                ),
                "unexpected response",
            ),
            (FakeUrlopen(FakeGitHubResponse(204, b"")), "HTTP 204"),
        )
        for urlopen, detail in cases:
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as directory:
                result, output, _bundle = self._publish(
                    Path(directory),
                    FakeProcessRunner(),
                    environment={BUILDER_TOKEN_ENVIRONMENT_VARIABLE: TOKEN},
                    urlopen=urlopen,
                )
                self.assertTrue(result.branch.startswith("contributions/"))
                self.assertEqual(result.commit, "b" * 40)
                self.assertIsNone(result.pull_request)
                self.assertEqual(len(urlopen.requests), 1)
                self.assertIn(detail, output.getvalue())
                self.assertIn(compare_url_for(result.branch), output.getvalue())
                self.assertNotIn(TOKEN, output.getvalue())

    def test_publish_repository_command_prints_branch_commit_and_pull_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = self._publisher_checkout(root)
            bundle_path, bundle = self._bundle_file(root)
            runner = FakeProcessRunner()
            urlopen = FakeUrlopen(
                FakeGitHubResponse(201, json.dumps({"html_url": PULL_REQUEST_URL}).encode())
            )
            stdout = io.StringIO()
            with (
                patch("scripts.contribution_review.GitPublisher._validate_identity"),
                patch("scripts.contribution_review.subprocess.run", runner),
                patch("scripts.contribution_review._PULL_REQUEST_OPENER") as opener,
                patch.dict(os.environ, {BUILDER_TOKEN_ENVIRONMENT_VARIABLE: TOKEN}),
                contextlib.redirect_stdout(stdout),
            ):
                opener.open = urlopen
                code = main(
                    [
                        "publish-repository",
                        "--bundle",
                        str(bundle_path),
                        "--checkout",
                        str(checkout),
                        "--expected-user",
                        "publisher",
                        "--expected-bundle-checksum",
                        bundle.checksum,
                        "--builder-python",
                        "python3.12",
                        "--instance-name",
                        "alpha",
                    ]
                )
            self.assertEqual(code, 0)
            result = json.loads(stdout.getvalue().splitlines()[-1])
            self.assertEqual(set(result), {"branch", "commit", "pull_request"})
            self.assertEqual(result["commit"], "b" * 40)
            self.assertEqual(result["pull_request"], PULL_REQUEST_URL)
            self.assertNotIn(TOKEN, stdout.getvalue())
            self.assertTrue(
                any(call[:2] == ["python3.12", "src/builder.py"] for call in runner.calls)
            )


class CatalogLoaderTestCase(unittest.TestCase):
    def test_loaders_read_the_saved_api_catalogue(self) -> None:
        topics = _load_canonical_topics(CATALOG)
        self.assertEqual(set(topics), {topic["id"] for topic in CATALOG_TOPICS})
        self.assertEqual(topics["grace"], CanonicalTopic("grace", "Grace", "#bbf7d0"))
        self.assertEqual(topics["faith"].aliases, ("Trust in God",))

        associations = _load_base_associations(CATALOG)
        self.assertIn(Association("grace", 43, 3, 16), associations)
        self.assertIn(Association("wisdom-cause", 59, 1, 5), associations)
        self.assertEqual(
            len(associations),
            sum(len(topic["verses"]) for topic in CATALOG_TOPICS),  # type: ignore[arg-type]
        )

    def test_loaders_do_not_require_api_ids_to_be_derived_slugs(self) -> None:
        # Upstream may rename a topic while keeping its id stable; the API is
        # authoritative, so the loader must not reject that.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_bytes(
                catalog_document(
                    [
                        {
                            "id": "gods-judgment",
                            "name": "Judgment of God",
                            "color": "#fb7185",
                            "aliases": ["God's Judgment"],
                            "default": True,
                            "verses": [[45, 2, 5]],
                        }
                    ]
                )
            )
            topics = _load_canonical_topics(path)
            self.assertEqual(topics["gods-judgment"].name, "Judgment of God")
            self.assertEqual(topics["gods-judgment"].aliases, ("God's Judgment",))

    def test_loaders_reject_missing_stale_and_malformed_catalogues(self) -> None:
        with self.assertRaisesRegex(ReviewError, "fetch-catalog"):
            _load_canonical_topics(None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ReviewError, "missing"):
                _load_base_associations(root / "absent.json")

            stale = root / "stale.json"
            stale.write_bytes(catalog_document())
            two_days_ago = time.time() - 2 * 24 * 3600
            os.utime(stale, (two_days_ago, two_days_ago))
            with self.assertRaisesRegex(ReviewError, "older than one day"):
                _load_canonical_topics(stale)

            linked = root / "linked.json"
            linked.symlink_to(CATALOG)
            with self.assertRaisesRegex(ReviewError, "regular file"):
                _load_canonical_topics(linked)

            malformed = root / "malformed.json"
            for payload in (
                b"not json",
                catalog_document() + b"}",
                json.dumps({"schema_version": 1, "topics": [], "extra": True}).encode(),
                json.dumps({"schema_version": 2, "topics": []}).encode(),
                catalog_document(
                    [{**CATALOG_TOPICS[1], "verses": [[67, 1, 1]]}]  # type: ignore[dict-item]
                ),
                catalog_document(
                    [{**CATALOG_TOPICS[1], "verses": [[1, 51, 1]]}]  # type: ignore[dict-item]
                ),
                catalog_document(
                    [{**CATALOG_TOPICS[1], "verses": [[1, 1, 2], [1, 1, 1]]}]  # type: ignore[dict-item]
                ),
                catalog_document([{**CATALOG_TOPICS[1], "color": "#BBF7D0"}]),  # type: ignore[dict-item]
                catalog_document([{**CATALOG_TOPICS[1], "id": "Grace"}]),  # type: ignore[dict-item]
                catalog_document([CATALOG_TOPICS[1], CATALOG_TOPICS[1]]),
                catalog_document(
                    [CATALOG_TOPICS[1], {**CATALOG_TOPICS[0], "aliases": ["grace"]}]  # type: ignore[dict-item]
                ),
            ):
                with self.subTest(payload=payload[:60]):
                    malformed.write_bytes(payload)
                    with self.assertRaisesRegex(ReviewError, "not a valid Bookmarks API catalogue"):
                        _load_base_associations(malformed)


class FakeBookmarksClient:
    def __init__(self, document: bytes, *, version: int = 7) -> None:
        self.document = document
        self.version = version
        self.calls: list[str] = []

    def index(self) -> SimpleNamespace:
        self.calls.append("index")
        return SimpleNamespace(
            catalog_version=self.version,
            checksum="f" * 64,
            topics=len(json.loads(self.document)["topics"]),
            verses=0,
            locales=(),
        )

    def catalog(self) -> object:
        self.calls.append("catalog")
        return parse_catalog_document(self.document)


class FetchCatalogTestCase(unittest.TestCase):
    def test_fetch_catalog_writes_the_verified_document_privately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "exports" / "bookmarks-catalog.json"
            client = FakeBookmarksClient(catalog_document(), version=9)
            output = io.StringIO()
            result = fetch_catalog(destination, client=client, output_stream=output)
            self.assertEqual(client.calls, ["index", "catalog"])
            self.assertEqual(destination.read_bytes(), catalog_document())
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)
            expected_checksum = parse_catalog_document(catalog_document()).checksum
            self.assertEqual(
                result,
                {
                    "catalog_version": 9,
                    "checksum": expected_checksum,
                    "topics": len(CATALOG_TOPICS),
                    "path": str(destination.resolve()),
                },
            )
            self.assertIn("catalogue version 9", output.getvalue())
            self.assertEqual(list(destination.parent.glob(".*.tmp")), [])

            # The saved copy is what the review commands then load.
            self.assertEqual(
                set(_load_canonical_topics(destination)),
                set(_load_canonical_topics(CATALOG)),
            )

    def test_fetch_catalog_keeps_the_previous_copy_when_the_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bookmarks-catalog.json"
            destination.write_bytes(b"previous")
            client = FakeBookmarksClient(catalog_document())
            with (
                patch("scripts.contribution_review.os.replace", side_effect=OSError("fail")),
                self.assertRaises(OSError),
            ):
                fetch_catalog(destination, client=client, output_stream=io.StringIO())
            self.assertEqual(destination.read_bytes(), b"previous")
            self.assertEqual(list(destination.parent.glob(".*.tmp")), [])

    def test_fetch_catalog_reports_api_failures_and_rejects_unsafe_outputs(self) -> None:
        class FailingClient:
            def index(self) -> object:
                raise BookmarksTransportError()

            def catalog(self) -> object:  # pragma: no cover - never reached
                raise AssertionError("index() failed first")

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bookmarks-catalog.json"
            with self.assertRaisesRegex(ReviewError, "could not be fetched"):
                fetch_catalog(destination, client=FailingClient(), output_stream=io.StringIO())
            self.assertFalse(destination.exists())
            with self.assertRaisesRegex(ReviewError, "absolute"):
                fetch_catalog(Path("relative.json"), client=FailingClient())
            linked = Path(directory) / "linked.json"
            linked.symlink_to(destination)
            with self.assertRaisesRegex(ReviewError, "symbolic link"):
                fetch_catalog(linked, client=FailingClient())

    def test_fetch_catalog_command_uses_the_client_and_prints_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bookmarks-catalog.json"
            client = FakeBookmarksClient(catalog_document(), version=3)
            stdout = io.StringIO()
            with (
                patch(
                    "modules.getbible_bookmarks.GetBibleBookmarksClient",
                    return_value=client,
                ) as factory,
                contextlib.redirect_stdout(stdout),
            ):
                code = main(
                    [
                        "fetch-catalog",
                        "--output",
                        str(destination),
                        "--base-url",
                        "https://bookmarks.example.test/v1",
                    ]
                )
            self.assertEqual(code, 0)
            factory.assert_called_once_with(base_url="https://bookmarks.example.test/v1")
            result = json.loads(stdout.getvalue().splitlines()[-1])
            self.assertEqual(set(result), {"catalog_version", "checksum", "topics", "path"})
            self.assertEqual(result["catalog_version"], 3)
            self.assertEqual(destination.read_bytes(), catalog_document())

            with (
                patch(
                    "modules.getbible_bookmarks.GetBibleBookmarksClient",
                    return_value=client,
                ) as factory,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main(["fetch-catalog", "--output", str(destination)])
            factory.assert_called_once_with(base_url="https://bookmarks.getbible.net/v1")


class FakeCompletionStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

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
        pull_request: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "token": token,
                "revision": revision,
                "state": state,
                "actor": actor,
                "branch": branch,
                "commit": commit,
                "error": error,
                "pull_request": pull_request,
            }
        )


class FakeStatusStore:
    def __init__(self, publication: dict[str, object]) -> None:
        self.publication = publication

    def list_applications(self, *, states: object = None, limit: int = 100) -> list[object]:
        return []

    def list_source_topics(self, *, states: object = None, limit: int = 500) -> list[object]:
        return []

    def list_events(self, **_kwargs: object) -> list[object]:
        return []

    def current_catalog(self) -> SimpleNamespace:
        return SimpleNamespace(
            revision=0,
            checksum="0" * 64,
            catalog=ContributionBundle.empty().as_dict(),
        )

    def publication_state(self) -> dict[str, object]:
        return dict(self.publication)


class PublicationRecordingTestCase(unittest.TestCase):
    def _finish(self, store: FakeCompletionStore, *extra: str) -> int:
        with patch("scripts.contribution_review._load_store", return_value=store):
            return main(
                [
                    "finish-repository-publication",
                    "--store",
                    "/var/lib/getbible-robot/alpha/contributions.sqlite3",
                    "--actor",
                    "setup:test",
                    "--lease-token",
                    "1" * 48,
                    "--revision",
                    "4",
                    "--state",
                    "pushed",
                    "--branch",
                    "contributions/20260917-101010-0123456789",
                    "--commit",
                    "b" * 40,
                    *extra,
                ]
            )

    def test_finish_passes_the_pull_request_url_to_the_store(self) -> None:
        store = FakeCompletionStore()
        self.assertEqual(self._finish(store, "--pull-request", PULL_REQUEST_URL), 0)
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(store.calls[0]["pull_request"], PULL_REQUEST_URL)
        self.assertEqual(store.calls[0]["state"], "pushed")
        self.assertEqual(store.calls[0]["revision"], 4)

        store = FakeCompletionStore()
        self.assertEqual(self._finish(store), 0)
        self.assertIsNone(store.calls[0]["pull_request"])

        store = FakeCompletionStore()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            self._finish(store, "--pull-request", "https://github.com/attacker/x/pull/1")
        self.assertEqual(store.calls, [])
        self.assertIn("pull request URL", stderr.getvalue())

    def test_real_store_records_the_pull_request_when_the_contract_is_present(self) -> None:
        signature = inspect.signature(ContributionStore.finish_repo_publication)
        if "pull_request" not in signature.parameters:
            self.skipTest("ContributionStore.finish_repo_publication(pull_request=) not landed")
        with tempfile.TemporaryDirectory() as directory:
            store = ContributionStore(path=str(Path(directory) / "contributions.sqlite3"))
            try:
                _approve(store)
                store.record_events(
                    42,
                    [
                        _topic_event("topic.pr", "local.topic.1", "Review Topic 1"),
                        _verse_event("verse.pr", "local.topic.1"),
                    ],
                )
                store.set_topic_mapping(
                    42,
                    "local.topic.1",
                    "review-topic-1",
                    state="mapped",
                    actor="test-admin",
                    canonical_definition=_definition(),
                )
                for event in store.list_events():
                    store.decide_event(event.id, "approved", actor="test-admin")
                accepted = accept_contributions(
                    store,
                    actor="test-admin",
                    catalog_file=CATALOG,
                    input_fn=lambda _prompt: "y",
                    output=io.StringIO(),
                )
                lease = store.begin_repo_publication(
                    accepted.revision, accepted.checksum, actor="setup:test"
                )
                with patch("scripts.contribution_review._load_store", return_value=store):
                    code = main(
                        [
                            "finish-repository-publication",
                            "--store",
                            "/unused/contributions.sqlite3",
                            "--actor",
                            "setup:test",
                            "--lease-token",
                            lease,
                            "--revision",
                            str(accepted.revision),
                            "--state",
                            "pushed",
                            "--branch",
                            "contributions/20260917-101010-0123456789",
                            "--commit",
                            "b" * 40,
                            "--pull-request",
                            PULL_REQUEST_URL,
                        ]
                    )
                self.assertEqual(code, 0)
                state = store.publication_state()
                self.assertEqual(state["repo_state"], "pushed")
                self.assertEqual(state["repo_pull_request"], PULL_REQUEST_URL)
                output = io.StringIO()
                print_status(store, output=output)
                self.assertIn(f"Last pull request: {PULL_REQUEST_URL}", output.getvalue())
            finally:
                store.close()

    def test_status_shows_api_catalogue_and_pull_request_when_the_store_provides_them(self) -> None:
        checked_at = time.time_ns()
        store = FakeStatusStore(
            {
                "repo_state": "pushed",
                "repo_revision": 3,
                "repo_branch": "contributions/20260917-101010-0123456789",
                "repo_pull_request": PULL_REQUEST_URL,
                "api_catalog_version": 12,
                "api_checksum": "ab" * 32,
                "api_checked_at": checked_at,
            }
        )
        output = io.StringIO()
        print_status(store, output=output)  # type: ignore[arg-type]
        text = output.getvalue()
        self.assertIn("Accepted ledger revision: none", text)
        self.assertIn("Approved changes awaiting acceptance: 0", text)
        self.assertIn("Upstream publication: pushed (ledger revision 3)", text)
        self.assertIn("Upstream branch: contributions/20260917-101010-0123456789", text)
        self.assertIn(f"Last pull request: {PULL_REQUEST_URL}", text)
        self.assertIn("Shared API catalogue version: 12", text)
        self.assertIn(f"Shared API catalogue checksum: {'ab' * 32}", text)
        self.assertIn("Shared API catalogue checked: 20", text)
        self.assertNotIn("live", text.casefold())

        older = FakeStatusStore({"repo_state": "not started", "repo_revision": 0})
        output = io.StringIO()
        print_status(older, output=output)  # type: ignore[arg-type]
        self.assertIn("Shared API catalogue version: not observed yet", output.getvalue())
        self.assertNotIn("Last pull request", output.getvalue())


class AcceptanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = ContributionStore(
            path=str(Path(self.directory.name) / "contributions.sqlite3")
        )
        _approve(self.store)
        self.store.record_events(
            42,
            [
                _topic_event("topic.accept", "local.topic.1", "Review Topic 1"),
                _verse_event("verse.accept", "local.topic.1"),
            ],
        )
        self.store.set_topic_mapping(
            42,
            "local.topic.1",
            "review-topic-1",
            state="mapped",
            actor="test-admin",
            canonical_definition=_definition(),
        )
        for event in self.store.list_events():
            self.store.decide_event(event.id, "approved", actor="test-admin")

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_accept_prompts_with_ledger_wording_and_records_the_revision(self) -> None:
        output = io.StringIO()
        with self.assertRaises(AcceptanceCancelled):
            accept_contributions(
                self.store,
                actor="test-admin",
                catalog_file=CATALOG,
                input_fn=lambda _prompt: "",
                output=output,
            )
        self.assertIn(
            "Accept 1 topics, 1 additions and 0 removals into the submission ledger?",
            output.getvalue(),
        )
        self.assertIn("Acceptance cancelled.", output.getvalue())
        self.assertEqual({event.state for event in self.store.list_events()}, {"approved"})
        self.assertEqual(self.store.current_catalog().revision, 0)

        prompts: list[str] = []

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            return "y"

        output = io.StringIO()
        revision = accept_contributions(
            self.store,
            actor="test-admin",
            catalog_file=CATALOG,
            input_fn=answer,
            output=output,
        )
        self.assertEqual(prompts, ["Accept now? [y/N]: "])
        self.assertEqual(revision.revision, 1)
        self.assertIn("Recorded accepted ledger revision 1", output.getvalue())
        self.assertNotIn("live", output.getvalue().casefold())
        self.assertEqual({event.state for event in self.store.list_events()}, {"applied"})

        output = io.StringIO()
        self.assertIsNone(
            accept_contributions(
                self.store,
                actor="test-admin",
                catalog_file=CATALOG,
                input_fn=lambda _prompt: "y",
                output=output,
            )
        )
        self.assertIn("no approved contribution changes to accept", output.getvalue())

    def test_accept_command_requires_the_catalogue_and_signals_cancellation(self) -> None:
        arguments = [
            "accept",
            "--store",
            "/unused/contributions.sqlite3",
            "--actor",
            "setup:test",
            "--catalog-file",
            str(CATALOG),
        ]
        with (
            patch("scripts.contribution_review._load_store", return_value=self.store),
            patch("sys.stdin", io.StringIO("n\n")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(arguments), 3)
        self.assertEqual(self.store.current_catalog().revision, 0)

        with (
            patch("scripts.contribution_review._load_store", return_value=self.store),
            patch("sys.stdin", io.StringIO("y\n")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(arguments), 0)
        self.assertEqual(self.store.current_catalog().revision, 1)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(arguments[:-2])
        self.assertIn("--catalog-file", stderr.getvalue())
        for retired in (
            ["publish-live", *arguments[1:]],
            ["topics", *arguments[1:-2], "--topics-file", "x"],
            ["verses", *arguments[1:], "--associations-file", "x"],
        ):
            with (
                self.subTest(retired=retired[0]),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                main(retired)
