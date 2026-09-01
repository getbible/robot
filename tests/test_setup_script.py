import http.server
import os
import stat
import subprocess
import sys
import tempfile
import threading
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
            "contributions",
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

    def test_upgrade_preserves_an_explicitly_disabled_contribution_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "robot.env"
            env_file.write_text(
                'CONTRIBUTION_STORE_FILE=""\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; ensure_env_value "$2" "$3" '
                    'CONTRIBUTION_STORE_FILE "$4"',
                    "setup-contribution-migration-test",
                    str(SETUP),
                    sys.executable,
                    str(env_file),
                    "/var/lib/getbible-robot/live/contributions.sqlite3",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(
                env_file.read_text(encoding="utf-8"),
                'CONTRIBUTION_STORE_FILE=""\n',
            )

    def test_contribution_review_is_privilege_separated_and_catalogue_aware(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        for fragment in (
            "29) Review and publish trusted contributions",
            'runuser --user "$ACTIVE_USER"',
            'runuser --user "$git_user"',
            '"CONTRIBUTION_STORE_FILE"',
            '"CONTRIBUTION_GIT_CHECKOUT"',
            '"CONTRIBUTION_GIT_USER"',
            '--topics-file "$topics_file"',
            '--associations-file "$associations_file"',
            "begin-repository-publication",
            "finish-repository-publication",
            '--lease-token "$lease_token"',
            "flock --nonblock",
            "--lease-seconds 3600",
            "getbible-robot-contribution-${checkout_lock_key}.lock",
            '--checksum "$checksum"',
            '--expected-bundle-checksum "$bundle_checksum"',
            "copy_verified_contribution_bundle",
            'mktemp --tmpdir="$export_dir"',
            "--state failed",
            "--state pushed",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, script)

        verified_copy = script.index("copy_verified_contribution_bundle")
        helper_install = script.index(
            'install -o "$git_user" -g "$publisher_group" -m 0700',
            verified_copy,
        )
        parent_handoff = script.index(
            'chown "$git_user:$publisher_group" "$CONTRIBUTION_TEMP_DIR"',
            helper_install,
        )
        publisher_run = script.index('runuser --user "$git_user"', parent_handoff)
        self.assertLess(verified_copy, helper_install)
        self.assertLess(helper_install, parent_handoff)
        self.assertLess(parent_handoff, publisher_run)

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
            # This exercises two installs, an upgrade, rollback, and failed
            # upgrade recovery. The contribution asset assertions expand the
            # exact-checkout fixture, and branch-coverage instrumentation slows
            # every Python helper it invokes. Leave headroom for slower CI
            # disks without weakening any lifecycle assertion.
            timeout=300,
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
            "--health-port",
            "--webhook-port",
            "--memory-max-mb",
            "--cpu-quota-percent",
        ):
            self.assertIn(option, script)
        self.assertIn("write_resource_dropin", script)
        self.assertIn('REVERSE_PROXY_MODE" "$reverse_proxy_mode"', script)
        self.assertNotIn("Trusted HAProxy source CIDR", script)
        self.assertNotIn("--trusted-proxy-cidrs", script)
        self.assertIn('mini_app_listen=${requested_mini_app_listen:-0.0.0.0}', script)
        self.assertIn(
            "Port where this bot's Mini App should be publicly available",
            script,
        )
        self.assertIn(
            'mini_app_port_conflicts "$instance" "$mini_app_port"',
            script,
        )
        self.assertIn(
            "already assigned to another managed bot",
            script,
        )
        self.assertNotIn("Private Mini App backend port", script)
        self.assertIn("Caddy is not used here", script)
        self.assertNotIn("Mini App listen address (use 127.0.0.1", script)

    def test_wildcard_mini_app_listener_uses_loopback_for_local_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app_dir = root / "app"
            bin_dir = app_dir / "venv" / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").symlink_to(sys.executable)
            env_file = root / "instance.env"
            env_file.write_text(
                'MINI_APP_PUBLIC_URL="https://bot.example.com/getbible/alpha"\n'
                'MINI_APP_LISTEN="0.0.0.0"\n'
                'MINI_APP_PORT="9250"\n',
                encoding="utf-8",
            )
            (root / "dotenv.py").write_text(
                "def dotenv_values(path):\n"
                "    values = {}\n"
                "    for line in open(path, encoding='utf-8'):\n"
                "        key, value = line.rstrip().split('=', 1)\n"
                "        values[key] = value.strip('\\\"')\n"
                "    return values\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; mini_app_local_url "$2" "$3"',
                    "test",
                    str(SETUP),
                    str(app_dir),
                    str(env_file),
                ],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(root)},
            )
        self.assertEqual(
            result.stdout.strip(),
            "http://127.0.0.1:9250/getbible/alpha/",
        )

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

    def test_store_preflight_and_doctor_prove_the_write_path(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        # Reads alone once passed a store the service could not write, which
        # left approved contributors with a visible panel and no token.
        access = script[script.index("verify_contribution_store_access() {"):]
        access = access[: access.index("\nverify_contribution_store_readonly() {")]
        self.assertIn("store.verify_writable()", access)

        doctor = script[script.index("cmd_doctor() {"):]
        if "\ncmd_repair() {" in doctor:
            doctor = doctor[: doctor.index("\ncmd_repair() {")]
        self.assertIn(
            'verify_contribution_store_access "$app_dir" "$env_file" "$ACTIVE_USER"',
            doctor,
        )

        validate = script[script.index("validate_contribution_store_path() {"):]
        validate = validate[: validate.index("\nload_contribution_context() {")]
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertIn(f'"{suffix}"', validate)

    def test_upgrade_refreshes_managed_caddy_transactionally(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        start = script.index("cmd_upgrade() {")
        end = script.index("\ncmd_rollback() {", start)
        upgrade = script[start:end]

        full_upgrade = upgrade.index("prepare_application")
        begin = upgrade.index("begin_caddy_transaction", full_upgrade)
        cutover = upgrade.index('systemctl stop "$service"')
        verify = upgrade.index("verify_mini_app_instance", cutover)
        commit = upgrade.index("commit_caddy_transaction", verify)
        rollback = upgrade.index("rollback_caddy_transaction", commit)
        self.assertLess(begin, cutover)
        self.assertLess(cutover, verify)
        self.assertLess(verify, commit)
        self.assertLess(commit, rollback)

        refresh = upgrade[:full_upgrade]
        self.assertIn('[[ "$target_sha" == "$ACTIVE_SHA" ]]', refresh)
        snapshot = refresh.index("begin_upgrade_refresh_transaction")
        migrate = refresh.index("migrate_instance_configuration")
        refresh_verify = refresh.index("verify_mini_app_instance")
        state_commit = refresh.index(
            "commit_upgrade_refresh_transaction",
            refresh_verify,
        )
        state_rollback = refresh.index(
            "rollback_upgrade_refresh_transaction",
            state_commit,
        )
        self.assertLess(snapshot, migrate)
        self.assertLess(migrate, refresh_verify)
        self.assertLess(refresh_verify, state_commit)
        self.assertLess(state_commit, state_rollback)
        self.assertIn("migrate_instance_configuration", refresh)
        self.assertIn("begin_caddy_transaction", refresh)
        self.assertIn("verify_mini_app_instance", refresh)
        self.assertIn("commit_caddy_transaction", refresh)

    def test_upgrade_hands_off_to_changed_target_manager(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        handoff = script.index("handoff_upgrade_to_target_manager() {")
        upgrade = script.index("cmd_upgrade() {", handoff)
        handoff_body = script[handoff:upgrade]
        upgrade_body = script[upgrade:script.index("\ncmd_rollback() {", upgrade)]
        self.assertIn('[[ "$SCRIPT_PATH" == "$target_manager" ]]', handoff_body)
        self.assertIn('exec "$target_manager" "${arguments[@]}"', handoff_body)
        self.assertLess(
            upgrade_body.index("handoff_upgrade_to_target_manager"),
            upgrade_body.index("migrate_instance_configuration"),
        )

    def test_upgrade_requires_the_service_account_contribution_database(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        upgrade_start = script.index("cmd_upgrade() {")
        upgrade_end = script.index("\ncmd_rollback() {", upgrade_start)
        upgrade = script[upgrade_start:upgrade_end]
        prepare = upgrade.index("prepare_application")
        target_preflight = upgrade.index("verify_contribution_store_access", prepare)
        cutover = upgrade.index('systemctl stop "$service"', target_preflight)
        self.assertLess(prepare, target_preflight)
        self.assertLess(target_preflight, cutover)
        self.assertIn('"$next_dir" "$env_file" "$ACTIVE_USER"', upgrade)
        self.assertIn('ensure_env_value "$python_bin" "$env_file"', script)

        readonly_start = script.index("verify_contribution_store_readonly() {")
        readonly_end = script.index("\ndotenv_value() {", readonly_start)
        readonly = script[readonly_start:readonly_end]
        self.assertIn('?mode=rw', readonly)
        self.assertIn('connection.execute("BEGIN IMMEDIATE")', readonly)
        self.assertIn('connection.execute("ROLLBACK")', readonly)

        context_start = script.index("load_contribution_context() {")
        context_end = script.index("\nrun_contribution_cli() {", context_start)
        context = script[context_start:context_end]
        self.assertNotIn(
            'store_file="${STATE_ROOT}/${ACTIVE_INSTANCE}/contributions.sqlite3"',
            context,
        )

    def test_mini_app_postflight_requires_robot_json_from_sync_routes(self) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def _respond(self) -> None:
                allowed_origin = (
                    self.command == "GET"
                    or self.headers.get("Origin") == "https://bot.example.com"
                )
                if self.path == "/robot" and allowed_origin:
                    status = 401
                    content_type = "application/json; charset=utf-8"
                    body = b'{"error":"unauthorized","message":"fixture"}'
                elif self.path == "/robot":
                    status = 403
                    content_type = "application/json; charset=utf-8"
                    body = b'{"error":"forbidden","message":"fixture"}'
                else:
                    status = 200
                    content_type = "text/html; charset=utf-8"
                    body = b"<html><title>getBible.Life</title></html>"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _respond
            do_POST = _respond

            def log_message(self, _format: str, *args: object) -> None:
                del args

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            for method in ("GET", "POST"):
                accepted = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'source "$1"; probe_mini_app_api_url "$2" "$3" "$4" "$5"',
                        "setup-api-postflight-test",
                        str(SETUP),
                        sys.executable,
                        f"{base_url}/robot",
                        method,
                        "https://bot.example.com",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(accepted.returncode, 0, msg=accepted.stderr)
            html_fallback = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; probe_mini_app_api_url "$2" "$3" GET',
                    "setup-api-postflight-test",
                    str(SETUP),
                    sys.executable,
                    f"{base_url}/html",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(html_fallback.returncode, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

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
        self.assertNotIn("max_size 1MiB", script)
        self.assertIn("max_size 5MB", script)
        self.assertIn("/api/v1/bookmarks/backup", script)
        render_start = script.index("render_caddy_routes() {")
        render_end = script.index("\nmanaged_caddy_routes_required() {", render_start)
        caddy_renderer = script[render_start:render_end]
        self.assertEqual(caddy_renderer.count("path {path}/api/v1/*"), 1)
        self.assertEqual(caddy_renderer.count("path /api/v1/*"), 1)
        # Contribution routes ride the general bounded API matcher: the drip
        # transport needs no dedicated path or body budget in the proxy.
        self.assertNotIn("contributions", caddy_renderer)
        for nested in (True, False):
            marker = "f\"        path {path}/api/v1/*\"" if nested else (
                '"        path /api/v1/*"'
            )
            prefix_at = caddy_renderer.index(marker)
            backup_at = caddy_renderer.rfind("_bookmark_backup", 0, prefix_at)
            self.assertGreater(prefix_at, backup_at)
        self.assertIn("caddy validate --config", script)
        self.assertIn("rollback_caddy_transaction", script)
        self.assertIn("wait_for_mini_app_surface", script)
        surface_start = script.index("probe_mini_app_surface() {")
        surface_end = script.index("\nwait_for_mini_app_surface() {", surface_start)
        surface = script[surface_start:surface_end]
        self.assertIn('"${base_url}/api/v1/bookmarks/catalog" GET', surface)
        self.assertIn('"${base_url}/api/v1/contributions/status" GET', surface)
        self.assertIn('"${base_url}/api/v1/contributions/events" POST', surface)
        self.assertNotIn("/api/v1/contributions/sync", surface)
        self.assertIn("verify_mini_app_public", script)
        self.assertIn('systemctl enable --now "$service"', script)
        self.assertNotIn("Traefik", (ROOT / "docs" / "MINI_APP.md").read_text())
        uninstall_doc = (ROOT / "docs" / "UNINSTALL.md").read_text()
        self.assertIn("does not migrate an older Robot installation", uninstall_doc)
        self.assertIn("no broad", uninstall_doc)
        self.assertIn("managed Caddy route", uninstall_doc)

    def test_managed_caddy_routes_bound_root_and_nested_api_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary)
            for instance, public_url, port in (
                ("nested", "https://nested.example.com/getbible/app", 9201),
                ("root", "https://root.example.com", 9202),
            ):
                app_dir = task_root / instance / "app"
                (app_dir / "venv" / "bin").mkdir(parents=True)
                os.symlink(sys.executable, app_dir / "venv" / "bin" / "python")
                (task_root / f"{instance}.env").write_text(
                    "MINI_APP_ENABLED=true\n"
                    "REVERSE_PROXY_MODE=caddy\n"
                    f"MINI_APP_PUBLIC_URL={public_url}\n"
                    f"MINI_APP_PORT={port}\n",
                    encoding="utf-8",
                )
            destination = task_root / "routes.caddy"
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    """
source "$1"
task_root=$2
task_python=$3
instance_names() { printf '%s\\n' nested root; }
application_dir_for() { printf '%s/%s/app\\n' "$task_root" "$1"; }
environment_file_for() { printf '%s/%s.env\\n' "$task_root" "$1"; }
select_python() { printf '%s\\n' "$task_python"; }
dotenv_value() {
    awk -F= -v key="$3" '$1 == key {sub(/^[^=]*=/, ""); gsub(/^"|"$/, ""); print; exit}' "$2"
}
render_caddy_routes "$4"
""",
                    "caddy-prefix-test",
                    str(SETUP),
                    str(task_root),
                    sys.executable,
                    str(destination),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            routes = destination.read_text(encoding="utf-8")
            self.assertIn("path /getbible/app/api/v1/*", routes)
            self.assertIn("path /api/v1/*", routes)
            self.assertNotIn("contributions", routes)
            self.assertEqual(routes.count("max_size 1MiB"), 0)
            self.assertEqual(routes.count("max_size 5MB"), 2)
            self.assertEqual(routes.count("max_size 64KB"), 2)
            for matcher in ("gb_nested", "gb_root"):
                backup = routes.index(f"@{matcher}_bookmark_backup")
                api = routes.index(f"@{matcher}_api ")
                self.assertLess(backup, api)
            self.assertNotIn("header_up -Authorization", routes)
            self.assertNotIn("header_up -Origin", routes)

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
