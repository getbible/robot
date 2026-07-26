import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"
UNIT = ROOT / "deploy" / "getbible-robot@.service"


class SetupScriptTestCase(unittest.TestCase):
    def test_shell_syntax_and_self_test(self) -> None:
        subprocess.run(["bash", "-n", str(SETUP)], check=True)
        result = subprocess.run(
            ["bash", str(SETUP), "self-test"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Manager self-test passed.", result.stdout)

    def test_hardened_unit_is_instance_scoped(self) -> None:
        unit = UNIT.read_text(encoding="utf-8")
        required = {
            "User=gb-%i",
            "EnvironmentFile=/etc/getbible-robot/%i.env",
            "WorkingDirectory=/opt/getbible-robot/%i/app",
            "Environment=ROBOT_INSTANCE=%i",
            "Environment=XDG_CACHE_HOME=/var/cache/getbible-robot/%i",
            "ReadWritePaths=/var/cache/getbible-robot/%i /var/log/getbible-robot/%i.jsonl",
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            "CapabilityBoundingSet=",
        }
        for directive in required:
            with self.subTest(directive=directive):
                self.assertIn(directive, unit)

    def test_manager_never_accepts_token_as_argument(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        self.assertNotIn("--token ", script)
        self.assertIn('read -r -s -p "Telegram Bot API token: "', script)
        self.assertIn("ensure_unique_token", script)


if __name__ == "__main__":
    unittest.main()
