import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from modules.contributions import ContributionStore

ROOT = Path(__file__).resolve().parents[1]
CONTAINER_SETUP = ROOT / "container" / "setup.sh"


class ContainerContributionReviewTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.app = root / "app"
        self.data = root / "data"
        self.config = root / "config"
        (self.app / "scripts").mkdir(parents=True)
        (self.app / "modules").mkdir()
        self.config.mkdir()
        state = self.data / "production" / "state"
        state.mkdir(parents=True, mode=0o700)
        shutil.copy2(
            ROOT / "scripts" / "contribution_review.py",
            self.app / "scripts" / "contribution_review.py",
        )
        shutil.copy2(
            ROOT / "scripts" / "contribution_publish.py",
            self.app / "scripts" / "contribution_publish.py",
        )
        # The review CLI needs the store, the canon shared with the Bookmarks
        # API client, that client, and the Query client; no catalogue sources
        # ship with the application any more.
        for name in (
            "contributions.py",
            "bookmark_sources.py",
            "contribution_publication.py",
            "bible_canon.py",
            "getbible_bookmarks.py",
            "getbible_query.py",
        ):
            shutil.copy2(
                ROOT / "modules" / name,
                self.app / "modules" / name,
            )
        self.store_path = state / "contributions.sqlite3"
        store = ContributionStore(path=str(self.store_path))
        store.close()
        self.environment = {
            **os.environ,
            "INSTANCE_NAME": "production",
            "ROBOT_APP_ROOT": str(self.app),
            "ROBOT_CONFIG_DIR": str(self.config),
            "ROBOT_DATA_DIR": str(self.data),
            "ROBOT_MODE": "single",
            "ROBOT_PYTHON": sys.executable,
            "TRANSLATION": "kjv",
        }
        self.environment.pop("PYTHONPATH", None)
        for key in ("CONTRIBUTION_GITHUB_TOKEN", "CONTRIBUTION_OPENAI_API_KEY"):
            self.environment.pop(key, None)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_setup(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(CONTAINER_SETUP), *arguments],
            cwd=ROOT,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_status_uses_the_private_instance_store(self) -> None:
        result = self.run_setup("contributions", "production", "status")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Contribution review status", result.stdout)
        self.assertIn("Accepted ledger revision: none", result.stdout)
        self.assertIn("Shared API catalogue version", result.stdout)
        self.assertNotIn("live", result.stdout.casefold())
        self.assertNotIn(str(self.store_path), result.stdout)
        self.assertFalse((self.store_path.parent / "contribution-exports").exists())

    def test_commit_retains_accepted_work_without_credentials(self) -> None:
        self.store_path.unlink()
        with sqlite3.connect(self.store_path) as database:
            database.executescript(
                (ROOT / "tests" / "support" / "contributions-v5.sql").read_text(encoding="utf-8")
            )
        result = self.run_setup("contributions", "production", "commit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No GitHub token", result.stdout)
        self.assertIn("remain queued", result.stdout)
        with sqlite3.connect(self.store_path) as database:
            self.assertGreater(
                database.execute(
                    "SELECT COUNT(*) FROM contribution_events WHERE state='applied'"
                ).fetchone()[0],
                0,
            )
        self.assertTrue(Path(str(self.store_path) + ".publication.sqlite3").is_file())

    def test_multi_instance_commit_does_not_inherit_other_instance_credentials(self) -> None:
        self.environment.update(
            {
                "ROBOT_MODE": "multi",
                "CONTRIBUTION_GITHUB_TOKEN": "other-instance",
                "CONTRIBUTION_OPENAI_API_KEY": "other-instance",  # pragma: allowlist secret
            }
        )
        (self.config / "production.env").write_text(
            f'INSTANCE_NAME="production"\nCONTRIBUTION_STORE_FILE="{self.store_path}"\n',
            encoding="utf-8",
        )
        self.test_commit_retains_accepted_work_without_credentials()

    def test_export_is_private_deterministic_and_identity_free(self) -> None:
        result = self.run_setup("contributions", "production", "export")

        self.assertEqual(result.returncode, 0, result.stderr)
        exports = list(
            (self.store_path.parent / "contribution-exports").glob("reviewed-catalog-*.json")
        )
        self.assertEqual(len(exports), 1)
        document = json.loads(exports[0].read_text(encoding="utf-8"))
        self.assertEqual(set(document), {"associations", "schema_version", "topics"})
        self.assertNotIn("contributor", exports[0].read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(exports[0].stat().st_mode), 0o600)
        self.assertIn("Privacy-safe repository export:", result.stdout)
        self.assertIn("commit publishes the accepted ledger directly", result.stdout)
        self.assertFalse(
            (self.store_path.parent / "contribution-exports" / "bookmarks-catalog.json").exists()
        )

    def test_concurrent_exports_reserve_distinct_private_files(self) -> None:
        command = [
            "bash",
            str(CONTAINER_SETUP),
            "contributions",
            "production",
            "export",
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=ROOT,
                env=self.environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=20) for process in processes]

        for process, (stdout, stderr) in zip(processes, results, strict=True):
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("Privacy-safe repository export:", stdout)
        exports = sorted(
            (self.store_path.parent / "contribution-exports").glob("reviewed-catalog-*.json")
        )
        self.assertEqual(len(exports), 2)
        self.assertNotEqual(exports[0].name, exports[1].name)
        for export in exports:
            document = json.loads(export.read_text(encoding="utf-8"))
            self.assertEqual(
                set(document),
                {"associations", "schema_version", "topics"},
            )
            self.assertEqual(stat.S_IMODE(export.stat().st_mode), 0o600)

    def test_review_mutations_require_a_terminal(self) -> None:
        for action in ("applications", "topics", "verses", "accept"):
            with self.subTest(action=action):
                result = self.run_setup("contributions", "production", action)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires an interactive terminal", result.stderr)
        # No catalogue download is attempted before the terminal check.
        self.assertFalse((self.store_path.parent / "contribution-exports").exists())
        result = self.run_setup("contributions", "production", "publish-live")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown contribution action", result.stderr)

    def test_container_menu_uses_the_same_api_publication_as_native(self) -> None:
        script = CONTAINER_SETUP.read_text(encoding="utf-8")

        self.assertNotIn("git push", script.casefold())
        self.assertNotIn("publish-repository", script.casefold())
        self.assertIn("run_contribution_publication commit", script)
        self.assertIn("CONTRIBUTION_OPENAI_API_KEY", script)
        for retired in (
            "global-bookmarks",
            "--topics-file",
            "--associations-file",
            "publish-live",
            "live instance",
            "Git branch",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, script)
        self.assertIn("fetch-catalog", script)
        self.assertIn("bookmarks.getbible.net", script)
        self.assertIn("5) Accept approved changes and commit to the builder", script)
        # Every catalogue-dependent action fetches first and fails closed.
        for command in ("topics", "accept"):
            self.assertIn(f"run_catalogue_review {command}", script)
        self.assertIn("run_catalogue_review verses", script)
        review_start = script.index("run_catalogue_review() {")
        review = script[review_start : script.index("\n}\n", review_start)]
        self.assertLess(
            review.index("fetch_contribution_catalog || return 1"),
            review.index('--catalog-file "$CONTRIBUTION_CATALOG"'),
        )

    def test_symlinked_private_store_is_rejected(self) -> None:
        self.store_path.unlink()
        outside = Path(self.temporary.name) / "outside.sqlite3"
        outside.write_bytes(b"not a contribution store")
        self.store_path.symlink_to(outside)

        result = self.run_setup("contributions", "production", "status")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private contribution store is unavailable", result.stderr)

    def test_symlinked_export_directory_is_rejected(self) -> None:
        outside = Path(self.temporary.name) / "outside-exports"
        outside.mkdir()
        (self.store_path.parent / "contribution-exports").symlink_to(
            outside,
            target_is_directory=True,
        )

        result = self.run_setup("contributions", "production", "export")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("export directory is unsafe", result.stderr)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
