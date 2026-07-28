import asyncio
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from container import runtime

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


class ContainerRuntimeTestCase(unittest.TestCase):
    def test_instance_file_gets_isolated_state_and_public_listeners(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "instances"
            data = root / "data"
            config.mkdir()
            path = config / "production.env"
            path.write_text(
                "\n".join(
                    (
                        f'TELEGRAM_API_TOKEN="{TOKEN}"',
                        'MINI_APP_ENABLED="true"',
                        'MINI_APP_PUBLIC_URL="https://bot.example.com/app"',
                        'MINI_APP_PORT="9201"',
                        'HEALTH_PORT="8081"',
                    )
                ),
                encoding="utf-8",
            )
            with (
                patch.object(runtime, "DATA_ROOT", data),
                patch.dict(os.environ, {}, clear=True),
            ):
                spec = runtime.InstanceSpec.from_file(path)

            self.assertEqual(spec.name, "production")
            self.assertEqual(spec.mini_app_port, 9201)
            self.assertEqual(spec.health_port, 8081)
            self.assertEqual(spec.environment["CONTAINERIZED"], "true")
            self.assertEqual(spec.environment["MINI_APP_LISTEN"], "0.0.0.0")
            self.assertEqual(spec.environment["LOG_FILE"], "")
            self.assertTrue(
                spec.environment["USER_PREFERENCES_FILE"].endswith(
                    "/production/state/preferences.sqlite3"
                )
            )

    def test_duplicate_ports_and_unsafe_environment_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            first_path = root / "first.env"
            second_path = root / "second.env"
            common = f'TELEGRAM_API_TOKEN="{TOKEN}"\nHEALTH_PORT="8081"\n'
            first_path.write_text(common, encoding="utf-8")
            second_path.write_text(common, encoding="utf-8")
            with (
                patch.object(runtime, "DATA_ROOT", data),
                patch.dict(os.environ, {}, clear=True),
            ):
                first = runtime.InstanceSpec.from_file(first_path)
                second = runtime.InstanceSpec.from_file(second_path)
                with self.assertRaises(runtime.ContainerConfigurationError):
                    runtime.validate_port_assignments([first, second])

                unsafe = root / "unsafe.env"
                unsafe.write_text(
                    f'TELEGRAM_API_TOKEN="{TOKEN}"\nPYTHONPATH="/tmp/attack"\n',
                    encoding="utf-8",
                )
                with self.assertRaises(runtime.ContainerConfigurationError):
                    runtime.InstanceSpec.from_file(unsafe)

    def test_container_assets_leave_tls_and_routing_external(self) -> None:
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        multi_compose = (root / "compose.multi.yaml").read_text(encoding="utf-8")
        secret_compose = (root / "compose.secret.yaml").read_text(encoding="utf-8")
        single_compose = (root / "compose.single.yaml").read_text(encoding="utf-8")
        container_setup = root / "container" / "setup.sh"
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("/app/setup.sh", dockerfile)
        self.assertNotIn("caddy", dockerfile.casefold())
        self.assertIn("read_only: true", compose)
        self.assertIn("cap_drop:", compose)
        self.assertIn('ROBOT_MODE: "single"', compose)
        self.assertIn('TELEGRAM_API_TOKEN: "${TELEGRAM_API_TOKEN:-}"', compose)
        self.assertIn("./docker/instances:/config/instances:ro", multi_compose)
        self.assertIn('ROBOT_MODE: "single"', single_compose)
        self.assertIn("environment: TELEGRAM_API_TOKEN", secret_compose)
        self.assertTrue(container_setup.stat().st_mode & stat.S_IXUSR)

    def test_configuration_errors_are_structured_for_docker_logs(self) -> None:
        output = io.StringIO()
        with patch("sys.stdout", output):
            runtime._event(
                "container_configuration_error",
                level="ERROR",
                error="TELEGRAM_API_TOKEN is required.",
            )
        self.assertIn('"level":"ERROR"', output.getvalue())
        self.assertIn("TELEGRAM_API_TOKEN is required.", output.getvalue())


class ContainerSupervisorTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_missing_application_configuration_is_logged_without_restart_loop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            with (
                patch.object(runtime, "DATA_ROOT", data),
                patch.dict(
                    os.environ,
                    {
                        "ROBOT_MODE": "single",
                        "INSTANCE_NAME": "production",
                        "HEALTH_PORT": "8081",
                    },
                    clear=True,
                ),
            ):
                spec = runtime.InstanceSpec.from_environment()
                supervisor = runtime.ContainerSupervisor()
                instance = runtime.InstanceRuntime(spec=spec)
                with patch("container.runtime._event") as event:
                    await supervisor._start(instance)

            self.assertFalse(instance.desired)
            self.assertIsNone(instance.process)
            self.assertIn("TELEGRAM_API_TOKEN is required", instance.last_error or "")
            event.assert_called_once_with(
                "instance_configuration_rejected",
                level="ERROR",
                instance="production",
                error=instance.last_error,
            )

    async def test_container_health_ignores_intentionally_stopped_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            with (
                patch.object(runtime, "DATA_ROOT", data),
                patch.dict(
                    os.environ,
                    {
                        "ROBOT_MODE": "single",
                        "INSTANCE_NAME": "production",
                        "TELEGRAM_API_TOKEN": TOKEN,
                        "HEALTH_PORT": "8081",
                    },
                    clear=True,
                ),
            ):
                spec = runtime.InstanceSpec.from_environment()
                supervisor = runtime.ContainerSupervisor()
                running = runtime.InstanceRuntime(spec=spec)
                running.process = Mock(pid=1234, returncode=None)
                running.last_health_ok = runtime.time.monotonic()
                stopped_spec = runtime.InstanceSpec._build(
                    "study",
                    {
                        "TELEGRAM_API_TOKEN": TOKEN,
                        "HEALTH_PORT": "8082",
                    },
                )
                stopped = runtime.InstanceRuntime(
                    spec=stopped_spec,
                    desired=False,
                )
                supervisor.instances = {
                    running.spec.name: running,
                    stopped.spec.name: stopped,
                }

                response = await supervisor._dispatch_control({"command": "health"})

            self.assertTrue(response["ok"])

    async def test_duplicate_poller_and_restart_storm_stop_one_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            with (
                patch.object(runtime, "DATA_ROOT", data),
                patch.dict(
                    os.environ,
                    {
                        "ROBOT_MODE": "single",
                        "INSTANCE_NAME": "production",
                        "TELEGRAM_API_TOKEN": TOKEN,
                        "HEALTH_PORT": "8081",
                    },
                    clear=True,
                ),
            ):
                spec = runtime.InstanceSpec.from_environment()
                supervisor = runtime.ContainerSupervisor()
                instance = runtime.InstanceRuntime(spec=spec)

                await supervisor._record_exit(instance, 75)
                self.assertFalse(instance.desired)
                self.assertIn("duplicate", instance.last_error or "")

                instance.desired = True
                instance.last_error = None
                for _ in range(runtime.RESTART_LIMIT + 1):
                    supervisor._schedule_failure_restart(instance)
                self.assertFalse(instance.desired)
                self.assertIn("restart circuit", instance.last_error or "")

    async def test_termination_reaps_the_complete_child_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            with (
                patch.object(runtime, "DATA_ROOT", data),
                patch.dict(
                    os.environ,
                    {
                        "ROBOT_MODE": "single",
                        "INSTANCE_NAME": "production",
                        "TELEGRAM_API_TOKEN": TOKEN,
                        "HEALTH_PORT": "8081",
                    },
                    clear=True,
                ),
            ):
                spec = runtime.InstanceSpec.from_environment()
                supervisor = runtime.ContainerSupervisor()
                instance = runtime.InstanceRuntime(spec=spec)
                instance.process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    "import time; time.sleep(60)",
                    start_new_session=True,
                )
                await supervisor._terminate(instance, restart=False)

            self.assertIsNone(instance.process)
            self.assertIsNotNone(instance.last_exit)


if __name__ == "__main__":
    unittest.main()
