from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modules.contribution_publication import (
    AcceptedSnapshot,
    PublicationJournal,
    PublicationSettings,
)
from modules.contribution_status import describe_publication
from modules.contributions import ContributionStore


class PublicationStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "contributions.sqlite3"
        self.store = ContributionStore(path=str(self.path))
        self.addCleanup(self.store.close)
        self.journal_path = self.path.with_name(self.path.name + ".publication.sqlite3")

    def accept(self) -> None:
        self.store.publish_catalog(
            {
                "schema_version": 1,
                "topics": [{"id": "hope", "name": "Hope", "color": "#008800", "aliases": []}],
                "associations": {
                    "add": [{"topic_id": "hope", "book": 43, "chapter": 3, "verse": 16}],
                    "remove": [],
                },
            },
            actor="reviewer",
        )

    def status(self, settings: PublicationSettings | None = None) -> str:
        lines: list[str] = []
        describe_publication(self.path, settings or PublicationSettings(), output=lines.append)
        return "\n".join(lines)

    def test_acceptance_is_visible_before_first_commit_without_creating_journal(self) -> None:
        self.accept()
        before = self.path.read_bytes()
        status = self.status()
        self.assertIn("Direct API publication: pending", status)
        self.assertIn("first publication check", status)
        self.assertIn("GitHub token: MISSING", status)
        self.assertFalse(self.journal_path.exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_empty_store_does_not_claim_queued_work(self) -> None:
        self.assertIn("no queued commit", self.status())

    def test_receipt_has_review_and_build_links_without_exposing_credentials(self) -> None:
        self.accept()
        journal = PublicationJournal(self.journal_path)
        job = journal.prepare(AcceptedSnapshot.read(self.path))
        assert job is not None
        journal.finish(job, "a" * 40, "main", changed=True)
        journal.close()
        before = self.journal_path.read_bytes()
        status = self.status(PublicationSettings("private-github", "private-openai"))
        self.assertIn("no queued commit", status)
        self.assertIn("/commit/" + "a" * 40, status)
        self.assertIn("/actions/workflows/build.yml", status)
        self.assertNotIn("private-github", status)
        self.assertNotIn("private-openai", status)
        self.assertEqual(self.journal_path.read_bytes(), before)

    def test_saved_failure_has_actionable_retry_without_dumping_error_payload(self) -> None:
        self.accept()
        journal = PublicationJournal(self.journal_path)
        journal.prepare(AcceptedSnapshot.read(self.path))
        journal.put("error", "untrusted-private-provider-payload")
        journal.close()
        status = self.status(PublicationSettings("private-github", "private-openai"))
        self.assertIn("last attempt failed", status)
        self.assertIn("resumes saved work", status)
        self.assertNotIn("untrusted-private-provider-payload", status)
