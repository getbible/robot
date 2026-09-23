"""Source-contract, transport, upgrade and retry tests; all network calls are fake."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

from modules.bookmark_sources import (
    SourceCatalogue,
    SourceError,
    coordinate,
    decode_json,
    encode_document,
    source_path,
    translated_name,
)
from modules.contribution_publication import (
    API_PATH,
    DEFAULT_TRANSLATION_MODEL,
    AcceptedSnapshot,
    ContributionPublisher,
    GitHubSourceRepository,
    HttpJsonClient,
    PublicationError,
    PublicationJournal,
    PublicationSettings,
    RemoteError,
    TopicTranslator,
)
from modules.contributions import ContributionStore
from scripts.contribution_publish import (
    KEYS,
    configure,
    main,
    read_configuration,
    write_configuration,
)

ROOT = Path(__file__).resolve().parents[1]
TOPIC = {"id": "mercy", "name": "Mercy", "color": "#123456", "aliases": []}
BUNDLE = {
    "schema_version": 1,
    "topics": [TOPIC],
    "associations": {
        "add": [{"topic_id": "mercy", "book": 43, "chapter": 3, "verse": 16}],
        "remove": [],
    },
}


def source_files() -> dict[str, str]:
    docs = {
        "data/topics.json": {
            "schema_version": 1,
            "topics": [
                {"id": "faith", "name": "Faith", "color": "#654321", "aliases": [], "default": True}
            ],
        },
        "data/links/faith.json": {"schema_version": 1, "topic": "faith", "verses": [[58, 11, 1]]},
        "data/locales/af.json": {
            "schema_version": 1,
            "locale": "af",
            "name": "Afrikaans",
            "topics": {"faith": "Geloof"},
        },
        "data/locales/fr.json": {
            "schema_version": 1,
            "locale": "fr",
            "name": "French",
            "topics": {"faith": "Foi"},
        },
        "data/locales/ar.json": {
            "schema_version": 1,
            "locale": "ar",
            "name": "Arabic",
            "topics": {"faith": "إيمان"},
        },
    }
    return {path: encode_document(path, document) for path, document in docs.items()}


def git_hash(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(
        b"blob " + str(len(data)).encode() + b"\0" + data, usedforsecurity=False
    ).hexdigest()


class FakeGitHub:
    """A tiny content-addressed repository, including non-fast-forward rejection."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = files or source_files()
        self.head = "1" * 40
        self.branch = "main"
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.blobs: dict[str, str] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.advances = 0
        self.before_advance: Any = None
        self.lose_ack = False
        self.reject_status: int | None = None
        self.register(self.head, self.files, [])

    def register(self, commit: str, files: dict[str, str], parents: list[str]) -> None:
        tree = hashlib.sha1(
            json.dumps(files, sort_keys=True).encode(), usedforsecurity=False
        ).hexdigest()
        self.trees[tree] = dict(files)
        self.commits[commit] = {"tree": {"sha": tree}, "parents": parents}
        for text in files.values():
            self.blobs[git_hash(text)] = text

    def external_edit(self, path: str, value: Any) -> None:
        files = dict(self.files)
        files[path] = encode_document(path, value)
        commit = hashlib.sha1(
            (self.head + json.dumps(value)).encode(), usedforsecurity=False
        ).hexdigest()
        self.register(commit, files, [self.head])
        self.head, self.files = commit, files

    def request(self, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and path == API_PATH:
            return {"default_branch": self.branch}
        if method == "GET" and "/git/ref/heads/" in path:
            return {"object": {"type": "commit", "sha": self.head}}
        if method == "GET" and "/git/commits/" in path:
            return copy.deepcopy(self.commits[path.rsplit("/", 1)[1]])
        if method == "GET" and "/git/trees/" in path:
            tree = path.rsplit("/", 1)[1].split("?")[0]
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": name,
                        "type": "blob",
                        "mode": "100644",
                        "sha": git_hash(text),
                        "size": len(text.encode()),
                    }
                    for name, text in self.trees[tree].items()
                ],
            }
        if method == "GET" and "/git/blobs/" in path:
            text = self.blobs[path.rsplit("/", 1)[1]]
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if method == "GET" and "/compare/" in path:
            candidate, head = path.split("/compare/", 1)[1].split("?")[0].split("...")
            current = head
            while current != candidate and self.commits[current]["parents"]:
                current = self.commits[current]["parents"][0]
            return {"status": "ahead" if current == candidate else "diverged"}
        if method == "POST" and path.endswith("/git/trees"):
            files = dict(self.trees[payload["base_tree"]])
            for entry in payload["tree"]:
                if not source_path(entry["path"]):
                    raise AssertionError("Write outside source contract")
                files[entry["path"]] = entry["content"]
            tree = hashlib.sha1(
                json.dumps(files, sort_keys=True).encode(), usedforsecurity=False
            ).hexdigest()
            self.trees[tree] = files
            return {"sha": tree}
        if method == "POST" and path.endswith("/git/commits"):
            sha = hashlib.sha1(
                json.dumps(payload, sort_keys=True).encode(), usedforsecurity=False
            ).hexdigest()
            self.register(sha, self.trees[payload["tree"]], payload["parents"])
            return {"sha": sha}
        if method == "PATCH" and "/git/refs/heads/" in path:
            assert payload["force"] is False
            self.advances += 1
            if self.before_advance:
                callback, self.before_advance = self.before_advance, None
                callback()
            if self.reject_status:
                raise RemoteError("GitHub", self.reject_status)
            if self.commits[payload["sha"]]["parents"] != [self.head]:
                raise RemoteError("GitHub", 422)
            self.head = payload["sha"]
            self.files = dict(self.trees[self.commits[self.head]["tree"]["sha"]])
            if self.lose_ack:
                self.lose_ack = False
                raise RemoteError("GitHub")
            return {"object": {"type": "commit", "sha": self.head}}
        raise AssertionError(f"Unexpected GitHub operation: {method} {path}")


class FakeOpenAI:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.invalid = False
        self.error = False

    def request(self, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        assert method == "POST" and path == "/v1/responses"
        self.calls.append(copy.deepcopy(payload))
        if self.error:
            raise RemoteError("OpenAI", 429)
        content = json.loads(payload["input"][1]["content"])
        names = {
            entry["locale"]: {"fr": "Miséricorde", "af": "Barmhartigheid", "ar": "رحمة"}.get(
                entry["locale"], "Translated topic"
            )
            for entry in content["languages"]
        }
        if self.invalid:
            names.pop(next(iter(names)))
        return {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(names)}]}
            ],
        }


class SourceContractTests(unittest.TestCase):
    def test_only_builder_source_paths_are_writable(self) -> None:
        for path in ["data/topics.json", "data/links/faith.json", "data/locales/zh-hant.json"]:
            self.assertTrue(source_path(path))
        for path in [
            "data/../topics.json",
            ".github/workflows/build.yml",
            "data/locales/en.json",
            "data/links/Bad.json",
            "data/locales/a.json",
            "data/links/a/b.json",
        ]:
            self.assertFalse(source_path(path))

    def test_noop_keeps_original_bytes_and_english_change_preserves_locales(self) -> None:
        files = source_files()
        catalogue = SourceCatalogue.read(files)
        self.assertEqual(catalogue.changes(), {})
        new = catalogue.apply([TOPIC], [{**BUNDLE["associations"]["add"][0], "action": "add"}])
        self.assertEqual([topic["id"] for topic in new], ["mercy"])
        changes = catalogue.changes()
        self.assertEqual(set(changes), {"data/topics.json", "data/links/mercy.json"})
        self.assertIn("    [43, 3, 16]", changes["data/links/mercy.json"])
        self.assertEqual(
            json.loads(files["data/topics.json"])["topics"][0], catalogue.topics["faith"]
        )

    def test_atomic_translations_cover_every_existing_locale(self) -> None:
        catalogue = SourceCatalogue.read(source_files())
        catalogue.apply([TOPIC], [{**BUNDLE["associations"]["add"][0], "action": "add"}])
        names = {"af": "Barmhartigheid", "fr": "Mise\u0301ricorde", "ar": "رحمة"}
        catalogue.add_translations("mercy", names)
        self.assertEqual(len(catalogue.changes()), 5)
        self.assertEqual(catalogue.locales["fr"]["topics"]["mercy"], "Miséricorde")
        catalogue.add_translations("mercy", dict.fromkeys(names, "Do not replace"))
        self.assertEqual(catalogue.locales["ar"]["topics"]["mercy"], "رحمة")

    def test_rejects_invalid_and_cross_file_source_shapes(self) -> None:
        mutations = [
            ("data/topics.json", lambda doc: doc["topics"].append(copy.deepcopy(doc["topics"][0]))),
            ("data/topics.json", lambda doc: doc["topics"][0].update(default="true")),
            ("data/topics.json", lambda doc: doc["topics"][0].update(color="red")),
            ("data/topics.json", lambda doc: doc["topics"][0].update(name="Broken\nName")),
            ("data/topics.json", lambda doc: doc["topics"][0].update(aliases=["Faith"])),
            ("data/links/faith.json", lambda doc: doc.update(topic="unknown")),
            ("data/links/faith.json", lambda doc: doc.update(verses=[[58, 11, 1], [58, 11, 1]])),
            ("data/locales/fr.json", lambda doc: doc.update(locale="de")),
            ("data/locales/fr.json", lambda doc: doc["topics"].update(unknown="Unknown")),
            ("data/locales/fr.json", lambda doc: doc["topics"].update(faith="\u0000")),
        ]
        for path, change in mutations:
            with self.subTest(path=path, change=change):
                files = source_files()
                document = json.loads(files[path])
                change(document)
                files[path] = json.dumps(document)
                with self.assertRaises(SourceError):
                    SourceCatalogue.read(files)
        with self.assertRaises(SourceError):
            decode_json('{"x":1,"x":2}')
        for point in [[True, 1, 1], [67, 1, 1], [1, 51, 1], [1, 1, 0], [1, 1]]:
            with self.assertRaises(SourceError):
                coordinate(point)
        for name in [None, "", " " * 5, "x" * 121, "text\rmore"]:
            with self.assertRaises(SourceError):
                translated_name(name)

    def test_upstream_metadata_conflicts_never_overwrite_corrections(self) -> None:
        catalogue = SourceCatalogue.read(source_files())
        with self.assertRaises(SourceError):
            catalogue.apply(
                [{"id": "faith", "name": "New Name", "color": "#654321", "aliases": []}], []
            )


class PublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "contributions.sqlite3"
        self.store = ContributionStore(path=str(self.path))
        self.journal = PublicationJournal(self.directory / "publication.sqlite3")
        self.github, self.openai = FakeGitHub(), FakeOpenAI()
        self.messages: list[str] = []

    def tearDown(self) -> None:
        self.journal.close()
        self.store.close()
        self.temporary.cleanup()

    def publisher(
        self, *, github_token: str = "fixture-github", openai_key: str = "fixture-ai"
    ) -> ContributionPublisher:
        return ContributionPublisher(
            PublicationSettings(github_token, openai_key),
            self.journal,
            github=self.github,
            openai=self.openai,
            output=self.messages.append,
        )

    def seed(self) -> None:
        self.store.publish_catalog(BUNDLE, actor="private-reviewer")

    def enroll(self) -> None:
        self.store.submit_application(42, first_name="Private Person", username="private_name")
        self.store.decide_application(42, "approved", actor="private-reviewer")
        self.store.acknowledge_disclosure(42)
        self.store.record_events(
            42,
            [
                {
                    "client_event_id": "topic.mercy",
                    "type": "topic_upsert",
                    "topic": {"local_topic_id": "local.mercy", "name": "Mercy", "color": "#123456"},
                }
            ],
        )
        self.store.set_topic_mapping(
            42,
            "local.mercy",
            canonical_topic_id="mercy",
            state="mapped",
            actor="private-reviewer",
            canonical_definition=TOPIC,
        )

    def event(self, verse: int, *, state: str = "approved", operation: str = "verse_add") -> int:
        self.store.record_events(
            42,
            [
                {
                    "client_event_id": f"{operation}.{verse}",
                    "type": operation,
                    "topic": {"local_topic_id": "local.mercy"},
                    "verse": {"book": 43, "chapter": 3, "verse": verse},
                }
            ],
        )
        event_id = self.store.list_events(limit=1000)[-1].id
        self.store.decide_event(
            event_id, state, actor="private-reviewer", canonical_topic_id="mercy"
        )
        return event_id

    def accept(self, event_ids: list[int]) -> None:
        # The publisher follows accepted events even when the cumulative
        # ledger bytes did not change, as is valid for a repeated addition.
        self.store.publish_approved_events_atomically(BUNDLE, event_ids, actor="private-reviewer")

    def test_no_accepted_work_never_contacts_services(self) -> None:
        self.assertIsNone(self.publisher(openai_key="fixture-ai").publish(self.path))
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.openai.calls, [])

    def test_missing_tokens_retain_work_until_all_translations_can_commit(self) -> None:
        self.seed()
        before = AcceptedSnapshot.read(self.path)
        with self.assertRaisesRegex(PublicationError, "CONTRIBUTION_GITHUB_TOKEN"):
            self.publisher(github_token="").publish(self.path)
        self.assertIsNotNone(self.journal.get("pending"))
        self.assertEqual(self.github.calls, [])
        self.assertEqual(AcceptedSnapshot.read(self.path), before)
        with self.assertRaisesRegex(PublicationError, "CONTRIBUTION_OPENAI_API_KEY"):
            self.publisher(openai_key="").publish(self.path)
        self.assertEqual(self.github.advances, 0)
        self.assertIsNotNone(self.journal.get("pending"))
        result = self.publisher().publish(self.path)
        self.assertTrue(result["changed"])
        tree_calls = [
            body
            for method, path, body in self.github.calls
            if method == "POST" and path.endswith("/git/trees")
        ]
        self.assertEqual(len(tree_calls), 1)
        self.assertEqual(
            {entry["path"] for entry in tree_calls[0]["tree"]},
            {
                "data/topics.json",
                "data/links/mercy.json",
                "data/locales/af.json",
                "data/locales/ar.json",
                "data/locales/fr.json",
            },
        )
        self.assertEqual(len(self.openai.calls), 1)
        self.assertEqual(AcceptedSnapshot.read(self.path), before)
        self.assertEqual(self.github.advances, 1)
        self.publisher().publish(self.path)
        self.assertEqual(self.github.advances, 1)

    def test_new_topic_and_all_translations_use_one_commit(self) -> None:
        self.seed()
        self.publisher(openai_key="fixture-ai").publish(self.path)
        self.assertEqual(self.github.advances, 1)
        self.assertEqual(len(self.openai.calls), 1)
        catalogue = SourceCatalogue.read(self.github.files)
        self.assertEqual(catalogue.locales["ar"]["topics"]["mercy"], "رحمة")
        request = self.openai.calls[0]
        self.assertEqual(request["model"], DEFAULT_TRANSLATION_MODEL)
        self.assertFalse(request["store"])
        self.assertTrue(request["text"]["format"]["strict"])
        serialized = json.dumps(self.github.calls) + json.dumps(self.openai.calls)
        for private in [
            "private-reviewer",
            "fixture-ai",
            "fixture-github",
            "contributor_id",
            "pulls",
        ]:
            self.assertNotIn(private, serialized)

    def test_existing_topic_missing_locales_is_repaired_without_replacing_human_labels(
        self,
    ) -> None:
        catalogue = SourceCatalogue.read(self.github.files)
        catalogue.apply([TOPIC], [{**BUNDLE["associations"]["add"][0], "action": "add"}])
        catalogue.locales["af"]["topics"]["mercy"] = "Reviewed human translation"
        self.github = FakeGitHub({**self.github.files, **catalogue.changes()})
        self.seed()
        self.publisher().publish(self.path)
        names = json.loads(self.openai.calls[0]["input"][1]["content"])["languages"]
        self.assertEqual({entry["locale"] for entry in names}, {"fr", "ar"})
        updated = SourceCatalogue.read(self.github.files)
        self.assertEqual(updated.locales["af"]["topics"]["mercy"], "Reviewed human translation")
        self.assertEqual(updated.locales["fr"]["topics"]["mercy"], "Miséricorde")
        self.assertEqual(self.github.advances, 1)

    def test_upgrade_repairs_published_english_only_topics_without_replaying_verse_changes(
        self,
    ) -> None:
        self.seed()
        self.publisher().publish(self.path)
        for locale in ("af", "ar", "fr"):
            path = f"data/locales/{locale}.json"
            document = json.loads(self.github.files[path])
            del document["topics"]["mercy"]
            self.github.external_edit(path, document)
        self.github.external_edit(
            "data/links/mercy.json",
            {"schema_version": 1, "topic": "mercy", "verses": [[43, 3, 18]]},
        )
        with self.journal.db:
            self.journal.db.execute(
                "DELETE FROM publication_metadata WHERE key = 'translation_contract_version'"
            )
        self.github.calls.clear()
        self.publisher().publish(self.path)
        changed = [
            entry["path"]
            for method, path, payload in self.github.calls
            if method == "POST" and path.endswith("/git/trees")
            for entry in payload["tree"]
        ]
        self.assertEqual(
            set(changed), {f"data/locales/{locale}.json" for locale in ("af", "ar", "fr")}
        )
        self.assertEqual(
            json.loads(self.github.files["data/links/mercy.json"])["verses"], [[43, 3, 18]]
        )
        self.github.calls.clear()
        self.assertIsNone(self.publisher().publish(self.path))
        self.assertEqual(self.github.calls, [])

    def test_verse_only_changes_work_without_openai_when_topic_has_all_locales(self) -> None:
        self.enroll()
        self.seed()
        self.publisher().publish(self.path)
        event_id = self.event(17)
        self.accept([event_id])
        self.openai.calls.clear()
        self.publisher(openai_key="").publish(self.path)
        self.assertEqual(self.openai.calls, [])
        self.assertIn([43, 3, 17], json.loads(self.github.files["data/links/mercy.json"])["verses"])

    def test_delayed_acceptance_wins_over_submission_order_and_mutable_update_time(self) -> None:
        self.enroll()
        self.seed()
        self.publisher().publish(self.path)
        older = self.event(16, state="deferred", operation="verse_remove")
        newer = self.event(16)
        self.accept([newer])
        self.store.decide_event(older, "approved", actor="reviewer", canonical_topic_id="mercy")
        bundle = copy.deepcopy(BUNDLE)
        bundle["topics"] = []
        bundle["associations"] = {"add": [], "remove": BUNDLE["associations"]["add"]}
        self.store.publish_approved_events_atomically(bundle, [older], actor="reviewer")
        # API observation or a repeated client sync can update an older accepted
        # row after the later acceptance. Publication must not reorder it.
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE contribution_events SET updated_at = ? WHERE id = ?", (2**63 - 1, newer)
            )
        snapshot = AcceptedSnapshot.read(self.path)
        self.assertEqual([event["id"] for event in snapshot.events][-2:], [newer, older])
        self.publisher().publish(self.path)
        self.assertEqual(json.loads(self.github.files["data/links/mercy.json"])["verses"], [])

    def test_incremental_verse_can_create_its_accepted_canonical_definition(self) -> None:
        self.enroll()
        self.journal.put("initial_import_complete", True)
        self.journal.put("translation_contract_version", 1)
        event_id = self.event(16)
        self.accept([event_id])
        self.publisher().publish(self.path)
        self.assertEqual(
            json.loads(self.github.files["data/links/mercy.json"])["verses"], [[43, 3, 16]]
        )
        self.assertEqual(self.github.advances, 1)

    def test_legacy_opposing_events_follow_current_cumulative_intent(self) -> None:
        self.enroll()
        self.seed()
        self.publisher().publish(self.path)
        older = self.event(16, state="deferred", operation="verse_remove")
        newer = self.event(16)
        self.accept([newer])
        self.store.decide_event(older, "approved", actor="reviewer", canonical_topic_id="mercy")
        bundle = copy.deepcopy(BUNDLE)
        bundle["topics"] = []
        bundle["associations"] = {"add": [], "remove": BUNDLE["associations"]["add"]}
        self.store.publish_approved_events_atomically(bundle, [older], actor="reviewer")
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM contribution_event_acceptance")
        self.publisher().publish(self.path)
        self.assertEqual(json.loads(self.github.files["data/links/mercy.json"])["verses"], [])

    def test_legacy_unknown_order_blocks_until_a_fresh_accepted_action_resolves_it(self) -> None:
        self.enroll()
        self.seed()
        self.publisher().publish(self.path)
        older = self.event(16, operation="verse_remove")
        newer = self.event(16)
        empty = {"schema_version": 1, "topics": [], "associations": {"add": [], "remove": []}}
        self.store.publish_approved_events_atomically(empty, [older, newer], actor="reviewer")
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM contribution_event_acceptance")
        with self.assertRaisesRegex(PublicationError, "Opposing legacy.*mercy at 43:3:16"):
            self.publisher().publish(self.path)
        self.assertEqual(self.github.advances, 1)
        self.assertNotIn(
            older, [row[0] for row in self.journal.db.execute("SELECT id FROM published_events")]
        )
        self.store.record_events(
            42,
            [
                {
                    "client_event_id": "fresh-reviewed-removal",
                    "type": "verse_remove",
                    "topic": {"local_topic_id": "local.mercy"},
                    "verse": {"book": 43, "chapter": 3, "verse": 16},
                }
            ],
        )
        fresh = self.store.list_events(limit=1000)[-1].id
        self.store.decide_event(fresh, "approved", actor="reviewer", canonical_topic_id="mercy")
        bundle = copy.deepcopy(empty)
        bundle["associations"]["remove"] = BUNDLE["associations"]["add"]
        self.store.publish_approved_events_atomically(bundle, [fresh], actor="reviewer")
        self.publisher().publish(self.path)
        self.assertEqual(json.loads(self.github.files["data/links/mercy.json"])["verses"], [])
        self.assertIsNone(self.journal.get("error"))

    def test_concurrent_human_translation_is_preserved_on_rebase(self) -> None:
        self.seed()

        def concurrent_edit() -> None:
            catalogue = SourceCatalogue.read(self.github.files)
            catalogue.apply([TOPIC], [{**BUNDLE["associations"]["add"][0], "action": "add"}])
            catalogue.locales["af"]["topics"]["mercy"] = "Human-approved wording"
            for path, text in catalogue.changes().items():
                self.github.external_edit(path, json.loads(text))

        self.github.before_advance = concurrent_edit
        self.publisher().publish(self.path)
        catalogue = SourceCatalogue.read(self.github.files)
        self.assertEqual(catalogue.locales["af"]["topics"]["mercy"], "Human-approved wording")
        self.assertEqual(catalogue.locales["fr"]["topics"]["mercy"], "Miséricorde")
        self.assertEqual(len(self.openai.calls), 1, "cached missing locales survive a rebase")
        self.assertEqual(self.github.advances, 2)

    def test_upgrade_rebuilds_an_uncommitted_legacy_job_before_publication(self) -> None:
        self.enroll()
        self.seed()
        self.publisher().publish(self.path)
        older = self.event(16, state="deferred", operation="verse_remove")
        newer = self.event(16)
        self.accept([newer])
        pending = self.journal.prepare(AcceptedSnapshot.read(self.path))
        del pending["translation_topics"]
        del pending["dependencies"]
        self.journal.put("pending", pending)
        self.store.decide_event(older, "approved", actor="reviewer", canonical_topic_id="mercy")
        bundle = {
            "schema_version": 1,
            "topics": [],
            "associations": {
                "add": [],
                "remove": BUNDLE["associations"]["add"],
            },
        }
        self.store.publish_approved_events_atomically(bundle, [older], actor="reviewer")
        self.publisher().publish(self.path)
        self.assertEqual(json.loads(self.github.files["data/links/mercy.json"])["verses"], [])

    def test_upgrade_recovers_legacy_committed_candidate_without_duplicate_commit(self) -> None:
        self.seed()
        self.github.lose_ack = True
        with self.assertRaises(RemoteError):
            self.publisher().publish(self.path)
        pending = self.journal.get("pending")
        del pending["translation_topics"]
        del pending["dependencies"]
        self.journal.put("pending", pending)
        self.publisher().publish(self.path)
        self.assertEqual(self.github.advances, 1)
        self.assertIsNone(self.journal.get("pending"))
        self.assertEqual(self.journal.get("translation_contract_version"), 1)

    def test_existing_topic_addition_never_retranslates_or_replays_old_changes(self) -> None:
        self.enroll()
        self.seed()
        self.publisher(openai_key="fixture-ai").publish(self.path)
        self.github.external_edit(
            "data/links/mercy.json",
            {"schema_version": 1, "topic": "mercy", "verses": [[43, 3, 18]]},
        )
        event_id = self.event(17)
        self.accept([event_id])
        self.publisher(openai_key="fixture-ai").publish(self.path)
        self.assertEqual(len(self.openai.calls), 1)
        self.assertEqual(
            json.loads(self.github.files["data/links/mercy.json"])["verses"],
            [[43, 3, 17], [43, 3, 18]],
        )

    def test_deferred_older_event_is_not_lost_after_newer_event_publishes(self) -> None:
        self.enroll()
        self.seed()
        self.publisher().publish(self.path)
        older = self.event(17, state="deferred")
        newer = self.event(18)
        self.accept([newer])
        self.publisher().publish(self.path)
        self.store.decide_event(older, "approved", actor="reviewer", canonical_topic_id="mercy")
        self.accept([older])
        self.publisher().publish(self.path)
        self.assertEqual(
            json.loads(self.github.files["data/links/mercy.json"])["verses"],
            [[43, 3, 16], [43, 3, 17], [43, 3, 18]],
        )

    def test_move_of_default_branch_rebases_without_overwriting_colleague(self) -> None:
        self.seed()
        self.github.before_advance = lambda: self.github.external_edit(
            "data/links/faith.json",
            {"schema_version": 1, "topic": "faith", "verses": [[58, 11, 1], [58, 11, 6]]},
        )
        self.publisher(openai_key="fixture-ai").publish(self.path)
        self.assertEqual(self.github.advances, 2)
        self.assertEqual(len(self.openai.calls), 1, "translations must be reused on rebase")
        self.assertIn([58, 11, 6], json.loads(self.github.files["data/links/faith.json"])["verses"])

    def test_lost_commit_reply_recovers_from_ancestor_without_duplicate_commit(self) -> None:
        self.seed()
        self.github.lose_ack = True
        with self.assertRaises(RemoteError):
            self.publisher(openai_key="fixture-ai").publish(self.path)
        candidate = self.journal.get("pending")["candidate"]
        self.github.external_edit(
            "data/links/faith.json",
            {"schema_version": 1, "topic": "faith", "verses": [[58, 11, 1], [58, 11, 6]]},
        )
        self.publisher(openai_key="fixture-ai").publish(self.path)
        self.assertEqual(self.github.advances, 1)
        self.assertEqual(self.journal.get("last")["commit"], candidate)
        self.assertEqual(len(self.openai.calls), 1)

    def test_rejects_bad_translation_without_publishing_then_retries(self) -> None:
        self.seed()
        self.openai.invalid = True
        with self.assertRaises(PublicationError):
            self.publisher(openai_key="fixture-ai").publish(self.path)
        self.assertEqual(self.github.advances, 0)
        self.assertIsNotNone(self.journal.get("pending"))
        self.openai.invalid = False
        self.publisher(openai_key="fixture-ai").publish(self.path)
        self.assertEqual(self.github.advances, 1)

    def test_supplied_but_unavailable_openai_key_does_not_silently_publish_english(self) -> None:
        self.seed()
        self.openai.error = True
        with self.assertRaises(RemoteError):
            self.publisher(openai_key="fixture-ai").publish(self.path)
        self.assertEqual(self.github.advances, 0)
        with self.assertRaisesRegex(PublicationError, "CONTRIBUTION_OPENAI_API_KEY"):
            self.publisher(openai_key="").publish(self.path)
        self.assertEqual(self.github.advances, 0)
        self.openai.error = False
        self.publisher().publish(self.path)
        self.assertEqual(self.github.advances, 1)

    def test_branch_protection_error_leaves_pending_job_and_no_forced_retry(self) -> None:
        self.seed()
        self.github.reject_status = 422
        with self.assertRaises(RemoteError):
            self.publisher().publish(self.path)
        self.assertEqual(self.github.advances, 1)
        self.assertIsNotNone(self.journal.get("pending"))

    def test_master_default_branch_and_already_present_bundle(self) -> None:
        catalogue = SourceCatalogue.read(self.github.files)
        catalogue.apply([TOPIC], [{**BUNDLE["associations"]["add"][0], "action": "add"}])
        catalogue.add_translations(
            "mercy", dict.fromkeys(catalogue.locales, "Existing translation")
        )
        self.github = FakeGitHub({**self.github.files, **catalogue.changes()})
        self.github.branch = "master"
        self.seed()
        receipt = self.publisher(openai_key="fixture-ai").publish(self.path)
        self.assertEqual(receipt["branch"], "master")
        self.assertFalse(receipt["changed"])
        self.assertEqual(self.github.advances, 0)
        self.assertEqual(self.openai.calls, [])

    def test_two_publishers_cannot_hold_the_instance_lock(self) -> None:
        other = PublicationJournal(self.journal.path)
        try:
            with self.journal.locked(), self.assertRaises(PublicationError), other.locked():
                pass
        finally:
            other.close()

    def test_private_journal_refuses_symlinks(self) -> None:
        link = self.directory / "unsafe.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            PublicationJournal(link)

    def test_multiple_acceptances_while_token_missing_are_drained(self) -> None:
        self.enroll()
        self.seed()
        with self.assertRaisesRegex(PublicationError, "CONTRIBUTION_GITHUB_TOKEN"):
            self.publisher(github_token="").publish(self.path)
        event_id = self.event(17)
        self.accept([event_id])
        self.publisher().publish(self.path)
        self.assertIn([43, 3, 17], json.loads(self.github.files["data/links/mercy.json"])["verses"])
        self.assertIsNone(self.journal.get("pending"))

    def test_pending_and_unapproved_records_never_reach_github(self) -> None:
        self.enroll()
        self.event(17, state="deferred")
        self.publisher().publish(self.path)
        self.assertEqual(self.github.calls, [])


class UpgradeAndConfigurationTests(unittest.TestCase):
    def test_live_schema_migrates_without_changing_any_existing_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contributions.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript((ROOT / "tests/support/contributions-v5.sql").read_text())
                tables = [
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                ]
                columns = {
                    table: [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
                    for table in tables
                }
                before = {
                    table: db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
                    for table in tables
                }
            for _ in range(2):
                store = ContributionStore(path=str(path))
                store.verify_writable()
                store.close()
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
                for table, names in columns.items():
                    projection = ",".join('"' + name + '"' for name in names)
                    self.assertEqual(
                        db.execute(f'SELECT {projection} FROM "{table}" ORDER BY rowid').fetchall(),
                        before[table],
                        table,
                    )
            journal = PublicationJournal(Path(temporary) / "publication.sqlite3")
            try:
                github = FakeGitHub()
                publisher = ContributionPublisher(
                    PublicationSettings("fixture-github", "fixture-ai"),
                    journal,
                    github=github,
                    openai=FakeOpenAI(),
                    output=lambda _: None,
                )
                publisher.publish(path)
                self.assertEqual(
                    json.loads(github.files["data/links/mercy.json"])["verses"], [[43, 3, 16]]
                )
                self.assertNotIn("Fixture Contributor", json.dumps(github.calls))
                self.assertNotIn("fixture_reader", json.dumps(github.calls))
                self.assertEqual(AcceptedSnapshot.read(path).events[0]["id"], 1)
            finally:
                journal.close()

    def test_optional_upgrade_prompts_preserve_all_existing_configuration(self) -> None:
        for inputs in [
            ("", ""),
            ("fixture-github", ""),
            ("", "fixture-openai"),
            ("fixture-github", "fixture-openai"),
        ]:
            with self.subTest(inputs=inputs), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "robot.env"
                original = (
                    'CONTRIBUTION_STORE_FILE="/var/lib/custom/state.sqlite3"\nTRANSLATION="aov"\n'
                )
                path.write_text(original)
                path.chmod(0o600)
                prompt = Mock(side_effect=list(inputs))
                with redirect_stdout(io.StringIO()) as output:
                    configure(path, prompt=prompt, interactive=True)
                self.assertEqual(prompt.call_count, 2)
                self.assertIn(original, path.read_text())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                for value in inputs:
                    if value:
                        self.assertNotIn(value, output.getvalue())
                values = read_configuration(path)
                self.assertEqual(values.get(KEYS[0], ""), inputs[0])
                self.assertEqual(values.get(KEYS[1], ""), inputs[1])

    def test_existing_credentials_are_not_prompted_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "robot.env"
            path.write_text(
                'CONTRIBUTION_GITHUB_TOKEN="existing-github"\n'
                'CONTRIBUTION_OPENAI_API_KEY="existing-openai"\n'  # pragma: allowlist secret
                'CONTRIBUTION_TRANSLATION_MODEL="custom-model"\n'
            )
            before = path.read_bytes()
            prompt = Mock(side_effect=AssertionError("Unexpected prompt"))
            with redirect_stdout(io.StringIO()):
                configure(path, prompt=prompt, interactive=True)
            self.assertEqual(path.read_bytes(), before)

    def test_headless_or_eof_upgrade_never_waits_for_tokens(self) -> None:
        for interactive in [True, False]:
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "robot.env"
                path.write_text('TRANSLATION="kjv"\n')
                with redirect_stdout(io.StringIO()):
                    configure(path, prompt=Mock(side_effect=EOFError), interactive=interactive)
                self.assertEqual(read_configuration(path)[KEYS[2]], DEFAULT_TRANSLATION_MODEL)

    def test_configuration_writer_refuses_shell_syntax_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "robot.env"
            path.write_text('TRANSLATION="kjv"\n')
            with self.assertRaises(PublicationError):
                write_configuration(path, {KEYS[0]: 'bad"\nINJECT=1'})
            link = Path(temporary) / "link.env"
            link.symlink_to(path)
            with self.assertRaises(PublicationError):
                read_configuration(link)
            self.assertEqual(path.read_text(), 'TRANSLATION="kjv"\n')

    def test_commit_entrypoint_with_missing_tokens_retains_accepted_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contributions.sqlite3"
            store = ContributionStore(path=str(path))
            store.publish_catalog(BUNDLE, actor="reviewer")
            store.close()
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/contribution_publish.py"),
                    "commit",
                    "--store",
                    str(path),
                ],
                check=False,
                env={"PATH": os.defpath},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("accepted contributions remain queued", result.stderr)
            self.assertEqual(AcceptedSnapshot.read(path).bundle, BUNDLE)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["status", "--store", str(path)]), 0)
            self.assertIn("pending", output.getvalue())


class HttpContractTests(unittest.TestCase):
    @staticmethod
    def response(value: Any, status: int = 200) -> Any:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.getcode.return_value = status
        response.read.return_value = json.dumps(value).encode()
        return response

    def test_headers_json_bounds_and_retry_do_not_expose_credentials(self) -> None:
        opener = Mock()
        sleep = Mock()
        opener.open.side_effect = [URLError("private-response"), self.response({"ok": True})]
        client = HttpJsonClient("GitHub", "fixture-secret", opener=opener, sleep=sleep)
        self.assertEqual(client.request("POST", "/test", {"a": 1}), {"ok": True})
        self.assertEqual(sleep.call_count, 1)
        request = opener.open.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.github.com/test")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-secret")
        self.assertEqual(json.loads(request.data), {"a": 1})
        opener.open.side_effect = HTTPError(
            "https://api.github.com/test", 403, "fixture-secret", {}, None
        )
        with self.assertRaises(RemoteError) as captured:
            client.request("GET", "/test")
        self.assertNotIn("fixture-secret", str(captured.exception))

    def test_invalid_response_and_path_fail_closed(self) -> None:
        for value in [[], 1, None]:
            opener = Mock()
            opener.open.return_value = self.response(value)
            with self.assertRaises(PublicationError):
                HttpJsonClient("GitHub", "fixture-secret", opener=opener).request("GET", "/test")
        with self.assertRaises(PublicationError):
            HttpJsonClient("GitHub", "fixture-secret").request("GET", "//other-host/path")
        for response in [
            {"status": "incomplete"},
            {"status": "completed", "output": []},
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal"}]}],
            },
        ]:
            with self.assertRaises(PublicationError):
                TopicTranslator._strings(response)

    def test_source_response_truncation_and_blob_integrity_are_verified(self) -> None:
        fake = FakeGitHub()
        repository = GitHubSourceRepository(fake)
        original = fake.request

        def truncated(method: str, path: str, payload: Any = None) -> dict[str, Any]:
            result = original(method, path, payload)
            if "/git/trees/" in path:
                result["truncated"] = True
            return result

        fake.request = truncated
        with self.assertRaises(PublicationError):
            repository.sources(fake.head)
        fake.request = original
        sha = next(iter(fake.blobs))
        fake.blobs[sha] = '{"changed":true}'
        with self.assertRaises(PublicationError):
            repository.sources(fake.head)

    def test_malformed_nested_remote_objects_fail_closed(self) -> None:
        for value in (None, [], 1, "bad"):
            fake = Mock()
            fake.request.return_value = {"object": value, "tree": value, "default_branch": value}
            repository = GitHubSourceRepository(fake)
            for operation in (
                repository.branch,
                lambda repository=repository: repository.head("main"),
                lambda repository=repository: repository.sources("a" * 40),
            ):
                with self.subTest(value=value), self.assertRaises(PublicationError):
                    operation()
            with self.subTest(content=value), self.assertRaises(PublicationError):
                TopicTranslator._strings(
                    {"status": "completed", "output": [{"type": "message", "content": value}]}
                )

    def test_rate_limit_retries_and_redirects_never_change_origin(self) -> None:
        opener = Mock()
        pause = Mock()
        opener.open.side_effect = [
            HTTPError("https://api.github.com/test", 429, "rate", {"Retry-After": "120"}, None),
            self.response({"ok": True}),
        ]
        client = HttpJsonClient("GitHub", "fixture-secret", opener=opener, sleep=pause)
        self.assertEqual(client.request("GET", "/test"), {"ok": True})
        pause.assert_called_once_with(60)
        opener.open.side_effect = HTTPError(
            "https://api.github.com/test",
            302,
            "redirect",
            {"Location": "https://other.example"},
            None,
        )
        with self.assertRaises(RemoteError) as caught:
            client.request("GET", "/test")
        self.assertEqual(caught.exception.status, 302)
        self.assertTrue(
            all(
                call.args[0].full_url.startswith("https://api.github.com/")
                for call in opener.open.call_args_list
            )
        )

    def test_translation_batches_and_model_cache_are_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = PublicationJournal(Path(temporary) / "receipts.sqlite3")
            self.addCleanup(journal.close)
            locales = {
                f"xx-{index:02d}": {"name": f"Language {index}", "topics": {}}
                for index in range(70)
            }
            client = FakeOpenAI()
            translator = TopicTranslator(client, journal, DEFAULT_TRANSLATION_MODEL)
            names = translator.translate(TOPIC, locales)
            self.assertEqual(set(names), set(locales))
            self.assertEqual(len(client.calls), 3)
            self.assertEqual(translator.translate(TOPIC, locales), names)
            self.assertEqual(len(client.calls), 3)
            TopicTranslator(client, journal, "another-model").translate(TOPIC, locales)
            self.assertEqual(len(client.calls), 6)

    def test_settings_are_secret_safe_and_model_is_configurable(self) -> None:
        settings = PublicationSettings.from_mapping(
            {KEYS[0]: "fixture-gh", KEYS[1]: "fixture-ai", KEYS[2]: "custom-model"}
        )
        self.assertNotIn("fixture-", repr(settings))
        self.assertEqual(settings.model, "custom-model")
        for values in [{KEYS[0]: "token with spaces"}, {KEYS[2]: "../model"}, {KEYS[0]: True}]:
            with self.assertRaises(PublicationError):
                PublicationSettings.from_mapping(values)


if __name__ == "__main__":
    unittest.main()
