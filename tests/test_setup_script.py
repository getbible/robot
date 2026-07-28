import stat
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"
UNIT = ROOT / "deploy" / "getbible-robot@.service"
LIFECYCLE = ROOT / "tests" / "setup_manager_lifecycle.sh"
CADDY_INSTALLATION = ROOT / "tests" / "caddy_installation.sh"
MINIAPP_READINESS = ROOT / "tests" / "miniapp_readiness.sh"
DOCKER_MANAGER = ROOT / "tests" / "docker_manager.sh"


class SetupScriptTestCase(unittest.TestCase):
    def test_setup_entrypoint_is_committed_executable(self) -> None:
        self.assertTrue(SETUP.stat().st_mode & stat.S_IXUSR)

    def test_shell_syntax_and_self_test(self) -> None:
        subprocess.run(["bash", "-n", str(SETUP)], check=True)
        subprocess.run(["bash", "-n", str(LIFECYCLE)], check=True)
        subprocess.run(["bash", "-n", str(CADDY_INSTALLATION)], check=True)
        subprocess.run(["bash", "-n", str(MINIAPP_READINESS)], check=True)
        subprocess.run(["bash", "-n", str(DOCKER_MANAGER)], check=True)
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
            "update",
            "upgrade",
            "rollback",
            "uninstall",
            "docker-deploy",
            "docker-list",
            "docker-status",
            "docker-logs",
            "docker-follow",
            "docker-manage",
            "docker-shell",
            "docker-doctor",
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

    def test_container_setup_entrypoint_is_executable_and_valid(self) -> None:
        container_setup = ROOT / "container" / "setup.sh"
        self.assertTrue(container_setup.stat().st_mode & stat.S_IXUSR)
        subprocess.run(["bash", "-n", str(container_setup)], check=True)
        result = subprocess.run(
            ["bash", str(container_setup), "help"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("GetBible Robot container setup", result.stdout)
        self.assertIn("standard output/error", result.stdout)

    def test_docker_manager_lifecycle(self) -> None:
        result = subprocess.run(
            ["bash", str(DOCKER_MANAGER)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertIn("Docker manager test passed.", result.stdout)

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

    def test_official_caddy_repository_installation(self) -> None:
        result = subprocess.run(
            ["bash", str(CADDY_INSTALLATION)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("Caddy installation test passed.", result.stdout)

    def test_mini_app_startup_waits_for_listener_readiness(self) -> None:
        result = subprocess.run(
            ["bash", str(MINIAPP_READINESS)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertIn("Mini App readiness test passed.", result.stdout)

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
            "Type=notify",
            "WatchdogSec=45s",
            "MemoryHigh=180M",
            "MemoryMax=256M",
            "MemorySwapMax=16M",
        }
        for directive in required:
            with self.subTest(directive=directive):
                self.assertIn(directive, unit)

    def test_manager_never_accepts_token_as_argument(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        self.assertNotIn("--token ", script)
        self.assertIn('read -r -s -p "Telegram Bot API token: "', script)
        self.assertIn("ensure_unique_token", script)

    def test_root_manager_does_not_write_the_operator_git_index(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        self.assertIn("git_source_read()", script)
        self.assertIn(
            'GIT_OPTIONAL_LOCKS=0 git -C "$directory" "$@"',
            script,
        )
        self.assertIn(
            'target_sha=$(git_source_read "$source_dir" rev-parse HEAD)',
            script,
        )
        self.assertIn(
            'git_source_read "$candidate" diff --quiet',
            script,
        )

    def test_mini_app_manager_enforces_https_and_loopback(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        self.assertIn("validate_mini_app_url", script)
        self.assertIn('"MINI_APP_LISTEN" "127.0.0.1"', script)
        self.assertIn("MINI_APP_INIT_DATA_MAX_AGE_SECONDS", script)
        self.assertIn("MINI_APP_LAUNCH_TTL_SECONDS", script)
        self.assertIn("The Mini App and webhook listeners require different ports.", script)
        self.assertIn("The Mini App and health listeners require different ports.", script)
        self.assertIn("preflight_mini_app_dns", script)
        self.assertIn("repair_apt_package_state", script)
        self.assertIn("install_caddy_with_apt", script)
        self.assertIn("caddy-stable-archive-keyring.gpg", script)
        self.assertIn("dl.cloudsmith.io/public/caddy/stable", script)
        self.assertIn("render_caddy_routes", script)
        self.assertIn("max_size 64KB", script)
        self.assertIn("caddy validate --config", script)
        self.assertIn("rollback_caddy_transaction", script)
        self.assertIn("wait_for_mini_app_url", script)
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
