import tempfile
import unittest
from pathlib import Path

from scripts.audit_runtime import AuditConfigurationError, audit_command


class RuntimeAuditTestCase(unittest.TestCase):
    def test_complete_lock_is_audited_strictly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "requirements.txt"
            lock.write_text("requests==2.0\n", encoding="utf-8")

            command = audit_command(lock, python="python")

        self.assertEqual(
            command,
            ["python", "-m", "pip_audit", "--strict", "-r", str(lock)],
        )

    def test_missing_lock_fails_closed(self) -> None:
        with self.assertRaises(AuditConfigurationError):
            audit_command(Path("/nonexistent/requirements.txt"))


if __name__ == "__main__":
    unittest.main()
