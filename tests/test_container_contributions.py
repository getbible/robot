import json
import os
import shutil
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
        (self.app / "data" / "global-bookmarks").mkdir(parents=True)
        self.config.mkdir()
        state = self.data / "production" / "state"
        state.mkdir(parents=True, mode=0o700)
        shutil.copy2(
            ROOT / "scripts" / "contribution_review.py",
            self.app / "scripts" / "contribution_review.py",
        )
        for name in ("contributions.py", "getbible_query.py"):
            shutil.copy2(
                ROOT / "modules" / name,
                self.app / "modules" / name,
            )
        for name in ("topics.json", "tag-verse.csv"):
            shutil.copy2(
                ROOT / "data" / "global-bookmarks" / name,
                self.app / "data" / "global-bookmarks" / name,
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
        self.assertIn("Live catalogue revision", result.stdout)
        self.assertNotIn(str(self.store_path), result.stdout)

    def test_export_is_private_deterministic_and_identity_free(self) -> None:
        result = self.run_setup("contributions", "production", "export")

        self.assertEqual(result.returncode, 0, result.stderr)
        exports = list(
            (self.store_path.parent / "contribution-exports").glob(
                "reviewed-catalog-*.json"
            )
        )
        self.assertEqual(len(exports), 1)
        document = json.loads(exports[0].read_text(encoding="utf-8"))
        self.assertEqual(set(document), {"associations", "schema_version", "topics"})
        self.assertNotIn("contributor", exports[0].read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(exports[0].stat().st_mode), 0o600)
        self.assertIn("Privacy-safe repository export:", result.stdout)
        self.assertIn(
            "Automated Git branch publication from a container export is not supported",
            result.stdout,
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
            (self.store_path.parent / "contribution-exports").glob(
                "reviewed-catalog-*.json"
            )
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
        result = self.run_setup("contributions", "production", "applications")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires an interactive terminal", result.stderr)

    def test_container_menu_never_claims_repository_push_support(self) -> None:
        script = CONTAINER_SETUP.read_text(encoding="utf-8")

        self.assertNotIn("git push", script.casefold())
        self.assertNotIn("publish-repository", script.casefold())
        self.assertIn(
            "Automated repository branch publication is unavailable for container instances",
            script,
        )
        self.assertIn("Use a native deployment", script)

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
