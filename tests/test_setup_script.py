import os
import stat
import subprocess
import sys
import tempfile
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
    @staticmethod
    def _fake_python(path: Path, major: int, minor: int) -> None:
        path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.version_info = ({major}, {minor})\n"
            "exec(compile(sys.stdin.read(), '<stdin>', 'exec'))\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

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
            "docker-update",
            "docker-init",
            "docker-config",
            "docker-validate",
            "docker-restart",
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
            timeout=90,
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
            "MemoryHigh=1536M",
            "MemoryMax=2048M",
            "MemorySwapMax=512M",
            "TasksMax=256",
            "LimitNOFILE=4096",
            "CPUQuota=200%",
        }
        for directive in required:
            with self.subTest(directive=directive):
                self.assertIn(directive, unit)

    def test_setup_accepts_python_310_through_314_and_prefers_newest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binaries = Path(directory)
            for minor in range(9, 16):
                self._fake_python(binaries / f"python3.{minor}", 3, minor)

            for minor in range(9, 16):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; python_supported "$2"',
                        "setup-python-test",
                        str(SETUP),
                        str(binaries / f"python3.{minor}"),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                with self.subTest(version=f"3.{minor}"):
                    self.assertEqual(
                        result.returncode,
                        0 if 10 <= minor <= 14 else 1,
                        msg=result.stderr,
                    )

            environment = dict(os.environ)
            environment.pop("PYTHON_BIN", None)
            environment["PATH"] = f"{binaries}:{environment['PATH']}"
            selected = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; select_python',
                    "setup-python-order-test",
                    str(SETUP),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(
                Path(selected.stdout.strip()).resolve(),
                (binaries / "python3.14").resolve(),
            )

    def test_resource_and_port_options_are_wired_to_instance_configuration(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        for option in (
            "--mini-app-port",
            "--mini-app-listen",
            "--trusted-proxy-cidrs",
            "--webhook-port",
            "--health-port",
            "--reverse-proxy",
            "--max-concurrent-lookups",
            "--max-concurrent-searches",
            "--max-concurrent-updates",
            "--memory-high-mb",
            "--memory-max-mb",
            "--memory-swap-max-mb",
            "--tasks-max",
            "--nofile-limit",
            "--cpu-quota-percent",
            "--allow-undersized-host",
        ):
            with self.subTest(option=option):
                self.assertIn(option, script)

        for helper in (
            "resource_dropin_dir_for()",
            "resource_dropin_for()",
            "validate_resource_profile()",
            "write_resource_dropin()",
            "sync_resource_dropin_from_env()",
        ):
            with self.subTest(helper=helper):
                self.assertIn(helper, script)

        for key in (
            "SYSTEMD_MEMORY_HIGH_MB",
            "SYSTEMD_MEMORY_MAX_MB",
            "SYSTEMD_MEMORY_SWAP_MAX_MB",
            "SYSTEMD_TASKS_MAX",
            "SYSTEMD_NOFILE_LIMIT",
            "SYSTEMD_CPU_QUOTA_PERCENT",
        ):
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', script)

        self.assertIn(
            '"$instance" "$memory_high_mb" "$memory_max_mb"',
            script,
        )
        self.assertIn(
            'sync_resource_dropin_from_env "$app_dir" "$env_file" "$ACTIVE_INSTANCE"',
            script,
        )
        self.assertIn('"MAX_CONCURRENT_LOOKUPS" "$max_concurrent_lookups"', script)
        self.assertIn('"MAX_CONCURRENT_SEARCHES" "$max_concurrent_searches"', script)
        self.assertIn('"MAX_CONCURRENT_UPDATES" "$max_concurrent_updates"', script)
        self.assertIn(
            'mini_app_port=${requested_mini_app_port:-$(next_mini_app_port)}',
            script,
        )
        self.assertIn(
            'webhook_port=${requested_webhook_port:-$(next_webhook_port)}',
            script,
        )
        self.assertIn('webhook_listen=${requested_webhook_listen:-127.0.0.1}', script)
        self.assertIn('"TELEGRAM_WEBHOOK_LISTEN" "$webhook_listen"', script)
        self.assertIn('health_port=$requested_health_port', script)

    def test_resource_dropin_is_rendered_from_environment_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    r'''
source "$1"
dropin_root=$2
resource_dropin_dir_for() { printf '%s\n' "$dropin_root"; }
resource_dropin_for() { printf '%s/%s.conf\n' "$dropin_root" "$1"; }
install() { mkdir -p "${@: -1}"; }
chown() { :; }
chmod() { :; }
dotenv_value() {
    case "$3" in
        SYSTEMD_MEMORY_HIGH_MB) printf '1700\n' ;;
        SYSTEMD_MEMORY_MAX_MB) printf '2300\n' ;;
        SYSTEMD_MEMORY_SWAP_MAX_MB) printf '600\n' ;;
        SYSTEMD_TASKS_MAX) printf '300\n' ;;
        SYSTEMD_NOFILE_LIMIT) printf '5000\n' ;;
        SYSTEMD_CPU_QUOTA_PERCENT) printf '250\n' ;;
        *) return 1 ;;
    esac
}
sync_resource_dropin_from_env /unused/app /unused/env alpha
cat "$dropin_root/alpha.conf"
''',
                    "setup-resource-test",
                    str(SETUP),
                    directory,
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(
            result.stdout.splitlines(),
            [
                "[Service]",
                "MemoryHigh=1700M",
                "MemoryMax=2300M",
                "MemorySwapMax=600M",
                "TasksMax=300",
                "LimitNOFILE=5000",
                "CPUQuota=250%",
            ],
        )

    def test_manager_never_accepts_token_as_argument(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        self.assertNotIn("--token ", script)
        self.assertIn('read -r -s -p "Telegram Bot API token: "', script)
        self.assertIn("ensure_unique_token", script)

    def test_install_exposes_ports_capacity_and_external_proxy_controls(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        for option in (
            "--mini-app-port",
            "--mini-app-listen",
            "--trusted-proxy-cidrs",
            "--health-port",
            "--webhook-port",
            "--memory-max-mb",
            "--cpu-quota-percent",
            "--allow-undersized-host",
        ):
            self.assertIn(option, script)
        self.assertIn("write_resource_dropin", script)
        self.assertIn('REVERSE_PROXY_MODE" "$reverse_proxy_mode"', script)

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
