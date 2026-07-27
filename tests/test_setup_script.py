import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"
UNIT = ROOT / "deploy" / "getbible-robot@.service"
LIFECYCLE = ROOT / "tests" / "setup_manager_lifecycle.sh"


class SetupScriptTestCase(unittest.TestCase):
    def test_shell_syntax_and_self_test(self) -> None:
        subprocess.run(["bash", "-n", str(SETUP)], check=True)
        subprocess.run(["bash", "-n", str(LIFECYCLE)], check=True)
        result = subprocess.run(
            ["bash", str(SETUP), "self-test"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Manager self-test passed.", result.stdout)

        help_result = subprocess.run(
            ["bash", str(SETUP), "help"],
            check=True,
            capture_output=True,
            text=True,
        )
        for command in (
            "install",
            "list",
            "start",
            "stop",
            "restart",
            "status",
            "runtime",
            "logs",
            "follow",
            "doctor",
            "repair",
            "delivery",
            "miniapp",
            "content",
            "config",
            "upgrade",
            "rollback",
            "uninstall",
            "menu",
            "self-test",
        ):
            with self.subTest(command=command):
                self.assertIn(command, help_result.stdout)

        version_result = subprocess.run(
            ["bash", str(SETUP), "version"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("getbible-robot setup manager", version_result.stdout)

    def test_complete_multi_instance_lifecycle(self) -> None:
        result = subprocess.run(
            ["bash", str(LIFECYCLE)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertIn(
            "Setup manager lifecycle test passed.",
            result.stdout,
        )

    def test_hardened_unit_is_instance_scoped(self) -> None:
        unit = UNIT.read_text(encoding="utf-8")
        required = {
            "User=gb-%i",
            "EnvironmentFile=/etc/getbible-robot/%i.env",
            "WorkingDirectory=/opt/getbible-robot/%i/app",
            "Environment=ROBOT_INSTANCE=%i",
            "Environment=XDG_CACHE_HOME=/var/cache/getbible-robot/%i",
            "ReadWritePaths=/var/cache/getbible-robot/%i "
            "/var/lib/getbible-robot/%i "
            "/var/log/getbible-robot/%i.jsonl",
            "NoNewPrivileges=true",
            "PrivateIPC=true",
            "ProtectSystem=strict",
            "ProcSubset=pid",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
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

    def test_mini_app_manager_enforces_https_and_loopback(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        self.assertIn("validate_mini_app_url", script)
        self.assertIn('"MINI_APP_LISTEN" "127.0.0.1"', script)
        self.assertIn("MINI_APP_INIT_DATA_MAX_AGE_SECONDS", script)
        self.assertIn("MINI_APP_LAUNCH_TTL_SECONDS", script)
        self.assertIn("The Mini App and webhook listeners require different ports.", script)
        self.assertIn("The Mini App and health listeners require different ports.", script)
        self.assertIn("preflight_mini_app_dns", script)
        self.assertIn("render_caddy_routes", script)
        self.assertIn("caddy validate --config", script)
        self.assertIn("rollback_caddy_transaction", script)
        self.assertIn("verify_mini_app_public", script)
        self.assertIn('systemctl enable --now "$service"', script)
        self.assertNotIn("Traefik", (ROOT / "docs" / "MINI_APP.md").read_text())
        uninstall_doc = (ROOT / "docs" / "UNINSTALL.md").read_text()
        self.assertIn("does not migrate an older Robot installation", uninstall_doc)
        self.assertIn("no broad", uninstall_doc)
        self.assertIn("managed Caddy route", uninstall_doc)

    def test_real_service_account_access_is_a_release_contract(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        self.assertIn('chown -R "root:${service_user}" "$app_dir"', script)
        self.assertIn('chmod -R u=rwX,g=rX,o= "$app_dir"', script)
        self.assertIn(
            'runuser --user "$service_user" -- /bin/sh -c',
            script,
        )
        self.assertIn(
            'verify_service_account_access "$destination" "$service_user"',
            script,
        )


if __name__ == "__main__":
    unittest.main()
