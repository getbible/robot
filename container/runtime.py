#!/usr/bin/env python3
"""PID-1 supervisor and operator client for container deployments."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

APP_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = Path(os.environ.get("ROBOT_CONFIG_DIR", "/config/instances"))
DATA_ROOT = Path(os.environ.get("ROBOT_DATA_DIR", "/data"))
CONTROL_SOCKET = Path(
    os.environ.get(
        "ROBOT_CONTROL_SOCKET",
        str(Path(tempfile.gettempdir()) / "getbible-robot-control.sock"),
    )
)
PYTHON = Path(sys.executable)
INSTANCE_RE = re.compile(r"[a-z][a-z0-9-]{0,22}[a-z0-9]\Z")
FORBIDDEN_ENV = frozenset(
    {
        "HOME",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PATH",
        "PYTHONHOME",
        "PYTHONPATH",
    }
)
RESTART_LIMIT = 5
RESTART_WINDOW_SECONDS = 5 * 60
DEFAULT_START_GRACE_SECONDS = 120.0
DEFAULT_HEALTH_INTERVAL_SECONDS = 15.0
# Leave 256 MiB between the per-bot child guard and the recommended 2 GiB
# aggregate container ceiling for the PID-1 supervisor and transient overhead.
DEFAULT_MEMORY_LIMIT_MB = 1792
CONTAINER_BIND_ADDRESS = str(IPv4Address(0))


class ContainerConfigurationError(RuntimeError):
    """Raised when instance files cannot form a safe container deployment."""


@dataclass(frozen=True, slots=True)
class InstanceSpec:
    name: str
    environment: dict[str, str]
    fingerprint: str
    health_port: int
    mini_app_port: int | None
    webhook_port: int | None
    memory_limit_bytes: int
    memory_warning_percent: int

    @classmethod
    def from_file(cls, path: Path) -> InstanceSpec:
        name = path.name.removesuffix(".env")
        if INSTANCE_RE.fullmatch(name) is None or "--" in name:
            raise ContainerConfigurationError(
                f"{path.name}: instance filenames must match the application name rules."
            )
        if path.is_symlink() or not path.is_file():
            raise ContainerConfigurationError(f"{path.name}: configuration is not a regular file.")
        values = dotenv_values(path, interpolate=False)
        if any(value is None for value in values.values()):
            raise ContainerConfigurationError(f"{path.name}: every key requires a value.")
        return cls._build(
            name,
            {key: value for key, value in values.items() if value is not None},
        )

    @classmethod
    def from_environment(cls) -> InstanceSpec:
        name = os.environ.get("INSTANCE_NAME", "production").strip()
        return cls._build(
            name,
            dict(os.environ),
            reject_forbidden_config=False,
        )

    @classmethod
    def _build(
        cls,
        name: str,
        configured: Mapping[str, str],
        *,
        reject_forbidden_config: bool = True,
    ) -> InstanceSpec:
        if INSTANCE_RE.fullmatch(name) is None or "--" in name:
            raise ContainerConfigurationError(f"{name!r}: invalid instance name.")
        environment = _child_environment(
            name,
            configured,
            reject_forbidden_config=reject_forbidden_config,
        )
        instance_root = (DATA_ROOT / name).resolve()
        data_root = DATA_ROOT.resolve()
        if data_root not in instance_root.parents:
            raise ContainerConfigurationError(f"{name}: instance data path escaped its root.")
        cache_root = instance_root / "cache"
        state_root = instance_root / "state"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_root.mkdir(mode=0o700, parents=True, exist_ok=True)

        environment.update(
            {
                "CONTAINERIZED": "true",
                "INSTANCE_NAME": name,
                "ROBOT_INSTANCE": name,
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "XDG_CACHE_HOME": str(cache_root),
                "USER_PREFERENCES_FILE": str(state_root / "preferences.sqlite3"),
                # Contributor identities, moderation history, and live catalogue
                # revisions are private per-instance state. Never accept a
                # configured path that could make two container bots share it.
                "CONTRIBUTION_STORE_FILE": str(
                    state_root / "contributions.sqlite3"
                ),
                # Container stdout is the single bounded-by-runtime log stream.
                "LOG_FILE": "",
            }
        )
        environment.setdefault("HEALTH_HOST", CONTAINER_BIND_ADDRESS)
        environment.setdefault("HEALTH_PORT", "8081")
        environment.setdefault("MINI_APP_LISTEN", CONTAINER_BIND_ADDRESS)
        environment.setdefault("TELEGRAM_WEBHOOK_LISTEN", CONTAINER_BIND_ADDRESS)

        health_port = _port(environment, "HEALTH_PORT", allow_zero=False)
        mini_app_port = (
            _port(environment, "MINI_APP_PORT")
            if _truthy(environment.get("MINI_APP_ENABLED", "false"))
            else None
        )
        webhook_port = (
            _port(environment, "TELEGRAM_WEBHOOK_PORT")
            if environment.get("TELEGRAM_DELIVERY_MODE", "polling").casefold() == "webhook"
            else None
        )
        raw_memory_limit = environment.get(
            "CONTAINER_INSTANCE_MEMORY_LIMIT_MB",
            str(DEFAULT_MEMORY_LIMIT_MB),
        )
        try:
            memory_limit_mb = int(raw_memory_limit)
        except ValueError as error:
            raise ContainerConfigurationError(
                f"{name}: CONTAINER_INSTANCE_MEMORY_LIMIT_MB must be an integer."
            ) from error
        if not 96 <= memory_limit_mb <= 262_144:
            raise ContainerConfigurationError(
                f"{name}: CONTAINER_INSTANCE_MEMORY_LIMIT_MB must be 96-262144."
            )
        raw_memory_warning = environment.get(
            "CONTAINER_INSTANCE_MEMORY_WARNING_PERCENT",
            "80",
        )
        try:
            memory_warning_percent = int(raw_memory_warning)
        except ValueError as error:
            raise ContainerConfigurationError(
                f"{name}: CONTAINER_INSTANCE_MEMORY_WARNING_PERCENT must be an integer."
            ) from error
        if not 50 <= memory_warning_percent <= 95:
            raise ContainerConfigurationError(
                f"{name}: CONTAINER_INSTANCE_MEMORY_WARNING_PERCENT must be 50-95."
            )

        fingerprint_source = {
            key: value
            for key, value in environment.items()
            if key not in {"PWD", "SHLVL", "_"}
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_source,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            name=name,
            environment=environment,
            fingerprint=fingerprint,
            health_port=health_port,
            mini_app_port=mini_app_port,
            webhook_port=webhook_port,
            memory_limit_bytes=memory_limit_mb * 1024 * 1024,
            memory_warning_percent=memory_warning_percent,
        )


def _child_environment(
    instance_name: str,
    configured: Mapping[str, str],
    *,
    reject_forbidden_config: bool,
) -> dict[str, str]:
    """Build a child environment without process-control variables."""
    forbidden = sorted(FORBIDDEN_ENV.intersection(configured))
    if forbidden and reject_forbidden_config:
        raise ContainerConfigurationError(
            f"{instance_name}: unsafe environment key in instance file: {forbidden[0]}"
        )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in FORBIDDEN_ENV
    }
    environment.update(
        {
            key: value
            for key, value in configured.items()
            if key not in FORBIDDEN_ENV
        }
    )
    return environment


@dataclass(slots=True)
class InstanceRuntime:
    spec: InstanceSpec
    desired: bool = True
    process: asyncio.subprocess.Process | None = None
    started_at: float | None = None
    restart_at: float = 0.0
    restart_times: deque[float] = field(default_factory=deque)
    restart_count: int = 0
    last_exit: int | None = None
    last_error: str | None = None
    health_failures: int = 0
    last_health_ok: float | None = None
    memory_bytes: int = 0
    memory_pressure: bool = False
    stopping: bool = False

    @property
    def state(self) -> str:
        if not self.desired:
            return "stopped" if self.last_error is None else "failed"
        if self.process is not None and self.process.returncode is None:
            return "running"
        if self.restart_at > time.monotonic():
            return "backoff"
        return "starting"

    def public_status(self) -> dict[str, Any]:
        return {
            "instance": self.spec.name,
            "state": self.state,
            "desired": self.desired,
            "pid": self.process.pid if self.process is not None else None,
            "health_port": self.spec.health_port,
            "mini_app_port": self.spec.mini_app_port,
            "webhook_port": self.spec.webhook_port,
            "memory_bytes": self.memory_bytes,
            "memory_limit_bytes": self.spec.memory_limit_bytes,
            "memory_warning_percent": self.spec.memory_warning_percent,
            "memory_pressure": self.memory_pressure,
            "restart_count": self.restart_count,
            "last_exit": self.last_exit,
            "last_error": self.last_error,
            "last_health_ok_seconds_ago": (
                round(time.monotonic() - self.last_health_ok, 1)
                if self.last_health_ok is not None
                else None
            ),
        }


class ContainerSupervisor:
    def __init__(self) -> None:
        self.instances: dict[str, InstanceRuntime] = {}
        self._stop = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self._health_interval = _positive_float(
            os.environ.get(
                "ROBOT_HEALTH_INTERVAL_SECONDS",
                str(DEFAULT_HEALTH_INTERVAL_SECONDS),
            ),
            "ROBOT_HEALTH_INTERVAL_SECONDS",
            minimum=5.0,
            maximum=300.0,
        )
        self._start_grace = _positive_float(
            os.environ.get(
                "ROBOT_START_GRACE_SECONDS",
                str(DEFAULT_START_GRACE_SECONDS),
            ),
            "ROBOT_START_GRACE_SECONDS",
            minimum=30.0,
            maximum=900.0,
        )
        self._last_health_check = 0.0

    async def run(self) -> int:
        await self.reload()
        if not self.instances:
            raise ContainerConfigurationError("No bot instances are configured.")
        await self._start_control_server()
        loop = asyncio.get_running_loop()
        for watched_signal in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(watched_signal, self._stop.set)
        _event("supervisor_started", instances=sorted(self.instances))
        try:
            while not self._stop.is_set():
                await self._reconcile()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
        finally:
            await self._shutdown()
        return 0

    async def reload(self) -> dict[str, Any]:
        specs = load_specs()
        validate_port_assignments(specs)
        incoming = {spec.name: spec for spec in specs}
        for name in tuple(self.instances):
            if name not in incoming:
                runtime = self.instances.pop(name)
                runtime.desired = False
                await self._terminate(runtime, restart=False)
                _event("instance_removed", instance=name)
        for name, spec in incoming.items():
            current = self.instances.get(name)
            if current is None:
                self.instances[name] = InstanceRuntime(spec=spec)
                _event("instance_added", instance=name)
            elif current.spec.fingerprint != spec.fingerprint:
                current.spec = spec
                current.desired = True
                current.last_error = None
                current.restart_times.clear()
                await self._terminate(current, restart=True)
                _event("instance_configuration_changed", instance=name)
        return {"ok": True, "instances": sorted(self.instances)}

    async def _reconcile(self) -> None:
        now = time.monotonic()
        for runtime in self.instances.values():
            process = runtime.process
            if process is not None and process.returncode is not None:
                await self._record_exit(runtime, process.returncode)
            if (
                runtime.desired
                and runtime.process is None
                and now >= runtime.restart_at
            ):
                await self._start(runtime)
        if now - self._last_health_check >= self._health_interval:
            self._last_health_check = now
            await asyncio.gather(
                *(self._check_runtime(runtime) for runtime in self.instances.values()),
                return_exceptions=True,
            )

    async def _start(self, runtime: InstanceRuntime) -> None:
        validation = await asyncio.create_subprocess_exec(
            str(PYTHON),
            "-c",
            (
                "import sys\n"
                "from config import ConfigurationError, Settings\n"
                "try:\n"
                "    Settings.from_env(load_environment_file=False)\n"
                "except ConfigurationError as error:\n"
                "    print(f'Configuration error: {error}', file=sys.stderr)\n"
                "    raise SystemExit(2)\n"
            ),
            cwd=APP_ROOT,
            env=runtime.spec.environment,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await validation.communicate()
        if validation.returncode != 0:
            runtime.desired = False
            runtime.last_error = _bounded_error(stderr)
            runtime.last_exit = validation.returncode
            _event(
                "instance_configuration_rejected",
                level="ERROR",
                instance=runtime.spec.name,
                error=runtime.last_error,
            )
            return

        runtime.process = await asyncio.create_subprocess_exec(
            str(PYTHON),
            str(APP_ROOT / "bot.py"),
            cwd=APP_ROOT,
            env=runtime.spec.environment,
            start_new_session=True,
        )
        runtime.started_at = time.monotonic()
        runtime.health_failures = 0
        runtime.stopping = False
        runtime.last_error = None
        _event(
            "instance_started",
            instance=runtime.spec.name,
            pid=runtime.process.pid,
        )

    async def _record_exit(self, runtime: InstanceRuntime, returncode: int) -> None:
        runtime.process = None
        runtime.started_at = None
        runtime.last_exit = returncode
        runtime.memory_bytes = 0
        runtime.memory_pressure = False
        if runtime.stopping:
            runtime.stopping = False
            return
        if returncode == 75:
            runtime.desired = False
            runtime.last_error = "duplicate Telegram poller detected"
            _event(
                "instance_duplicate_poller",
                level="ERROR",
                instance=runtime.spec.name,
            )
            return
        if not runtime.desired:
            return

        now = time.monotonic()
        while runtime.restart_times and now - runtime.restart_times[0] > RESTART_WINDOW_SECONDS:
            runtime.restart_times.popleft()
        runtime.restart_times.append(now)
        runtime.restart_count += 1
        if len(runtime.restart_times) > RESTART_LIMIT:
            runtime.desired = False
            runtime.last_error = "restart circuit opened after repeated failures"
            _event(
                "instance_restart_circuit_open",
                level="ERROR",
                instance=runtime.spec.name,
                returncode=returncode,
            )
            return
        delay = min(60.0, 5.0 * (2 ** (len(runtime.restart_times) - 1)))
        runtime.restart_at = now + delay
        _event(
            "instance_exited",
            level="WARNING",
            instance=runtime.spec.name,
            returncode=returncode,
            restart_in_seconds=delay,
        )

    async def _check_runtime(self, runtime: InstanceRuntime) -> None:
        process = runtime.process
        if process is None or process.returncode is not None:
            return
        runtime.memory_bytes = _rss_bytes(process.pid)
        warning_bytes = (
            runtime.spec.memory_limit_bytes
            * runtime.spec.memory_warning_percent
            // 100
        )
        if runtime.memory_bytes >= warning_bytes and not runtime.memory_pressure:
            runtime.memory_pressure = True
            _event(
                "instance_memory_pressure",
                level="WARNING",
                instance=runtime.spec.name,
                memory_bytes=runtime.memory_bytes,
                warning_bytes=warning_bytes,
                limit_bytes=runtime.spec.memory_limit_bytes,
                used_percent=round(
                    runtime.memory_bytes
                    * 100
                    / runtime.spec.memory_limit_bytes,
                    1,
                ),
            )
        elif (
            runtime.memory_pressure
            and runtime.memory_bytes < warning_bytes * 85 // 100
        ):
            runtime.memory_pressure = False
            _event(
                "instance_memory_pressure_cleared",
                instance=runtime.spec.name,
                memory_bytes=runtime.memory_bytes,
                warning_bytes=warning_bytes,
            )
        if runtime.memory_bytes > runtime.spec.memory_limit_bytes:
            runtime.last_error = "per-instance memory guard exceeded"
            _event(
                "instance_memory_limit_exceeded",
                level="ERROR",
                instance=runtime.spec.name,
                memory_bytes=runtime.memory_bytes,
                limit_bytes=runtime.spec.memory_limit_bytes,
            )
            await self._terminate(runtime, restart=True, failure=True)
            return
        within_start_grace = (
            runtime.started_at is not None
            and time.monotonic() - runtime.started_at < self._start_grace
        )
        healthy = await _http_health(runtime.spec.health_port)
        if healthy:
            runtime.health_failures = 0
            runtime.last_health_ok = time.monotonic()
            return
        if within_start_grace:
            return
        runtime.health_failures += 1
        if runtime.health_failures < 3:
            return
        runtime.last_error = "liveness endpoint failed three consecutive checks"
        _event(
            "instance_liveness_failed",
            level="ERROR",
            instance=runtime.spec.name,
        )
        await self._terminate(runtime, restart=True, failure=True)

    async def _terminate(
        self,
        runtime: InstanceRuntime,
        *,
        restart: bool,
        failure: bool = False,
    ) -> None:
        process = runtime.process
        if process is None:
            runtime.restart_at = time.monotonic() if restart else 0.0
            return
        runtime.stopping = True
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=30.0)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        runtime.last_exit = process.returncode
        runtime.process = None
        runtime.started_at = None
        runtime.memory_bytes = 0
        runtime.memory_pressure = False
        runtime.stopping = False
        if restart and failure:
            self._schedule_failure_restart(runtime)
        else:
            runtime.restart_at = time.monotonic() + (5.0 if restart else 0.0)

    def _schedule_failure_restart(self, runtime: InstanceRuntime) -> None:
        now = time.monotonic()
        while runtime.restart_times and now - runtime.restart_times[0] > RESTART_WINDOW_SECONDS:
            runtime.restart_times.popleft()
        runtime.restart_times.append(now)
        runtime.restart_count += 1
        if len(runtime.restart_times) > RESTART_LIMIT:
            runtime.desired = False
            runtime.last_error = "restart circuit opened after repeated failures"
            runtime.restart_at = 0.0
            _event(
                "instance_restart_circuit_open",
                level="ERROR",
                instance=runtime.spec.name,
            )
            return
        runtime.restart_at = now + min(
            60.0,
            5.0 * (2 ** (len(runtime.restart_times) - 1)),
        )

    async def _start_control_server(self) -> None:
        CONTROL_SOCKET.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(FileNotFoundError):
            CONTROL_SOCKET.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_control,
            path=CONTROL_SOCKET,
            limit=8192,
        )
        CONTROL_SOCKET.chmod(0o600)

    async def _handle_control(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if len(raw) > 4096:
                raise ValueError("control request is too large")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("control request must be an object")
            response = await self._dispatch_control(request)
        except Exception as error:
            response = {"ok": False, "error": str(error)[:256]}
        writer.write(
            json.dumps(response, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _dispatch_control(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command")
        if command in {"list", "status", "doctor", "health"}:
            statuses = [
                runtime.public_status()
                for runtime in sorted(
                    self.instances.values(),
                    key=lambda item: item.spec.name,
                )
            ]
            requested = request.get("instance")
            if requested:
                statuses = [
                    status for status in statuses if status["instance"] == requested
                ]
                if not statuses:
                    return {"ok": False, "error": "instance not found"}
            desired_statuses = [
                status for status in statuses if status["desired"]
            ]
            healthy = bool(desired_statuses) and all(
                status["state"] == "running"
                and status["last_health_ok_seconds_ago"] is not None
                and status["last_health_ok_seconds_ago"]
                <= self._health_interval * 4
                for status in desired_statuses
            )
            return {
                "ok": healthy if command == "health" else True,
                "instances": statuses,
            }
        if command == "reload":
            return await self.reload()
        if command not in {"start", "stop", "restart"}:
            return {"ok": False, "error": "unsupported command"}
        name = request.get("instance")
        runtime = self.instances.get(name) if isinstance(name, str) else None
        if runtime is None:
            return {"ok": False, "error": "instance not found"}
        if command == "stop":
            runtime.desired = False
            runtime.last_error = None
            await self._terminate(runtime, restart=False)
        else:
            runtime.desired = True
            runtime.last_error = None
            runtime.restart_times.clear()
            if command == "restart":
                await self._terminate(runtime, restart=True)
            else:
                runtime.restart_at = time.monotonic()
        return {"ok": True, "instances": [runtime.public_status()]}

    async def _shutdown(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await asyncio.gather(
            *(
                self._terminate(runtime, restart=False)
                for runtime in self.instances.values()
            )
        )
        with suppress(FileNotFoundError):
            CONTROL_SOCKET.unlink()
        _event("supervisor_stopped")


def load_specs() -> list[InstanceSpec]:
    mode = os.environ.get("ROBOT_MODE", "multi").casefold()
    if mode == "single":
        return [InstanceSpec.from_environment()]
    if mode != "multi":
        raise ContainerConfigurationError("ROBOT_MODE must be single or multi.")
    if not CONFIG_ROOT.is_dir():
        raise ContainerConfigurationError(f"Configuration directory is missing: {CONFIG_ROOT}")
    paths = sorted(CONFIG_ROOT.glob("*.env"))
    return [InstanceSpec.from_file(path) for path in paths]


def validate_port_assignments(specs: list[InstanceSpec]) -> None:
    assigned: dict[int, str] = {}
    for spec in specs:
        for label, port in (
            ("health", spec.health_port),
            ("mini_app", spec.mini_app_port),
            ("webhook", spec.webhook_port),
        ):
            if port is None:
                continue
            owner = assigned.get(port)
            if owner is not None:
                raise ContainerConfigurationError(
                    f"Port {port} is assigned to both {owner} and {spec.name}:{label}."
                )
            assigned[port] = f"{spec.name}:{label}"


async def _http_health(port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port),
            timeout=2.0,
        )
        writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.close()
        await writer.wait_closed()
        return b" 200 " in status
    except (OSError, TimeoutError):
        return False


def _rss_bytes(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                value = line.split()
                return int(value[1]) * 1024
    except (OSError, UnicodeError, ValueError, IndexError):
        pass
    return 0


def _port(
    environment: dict[str, str],
    name: str,
    *,
    allow_zero: bool = False,
) -> int:
    defaults = {
        "HEALTH_PORT": 8081,
        "MINI_APP_PORT": 9201,
        "TELEGRAM_WEBHOOK_PORT": 9001,
    }
    try:
        value = int(environment.get(name, str(defaults[name])))
    except ValueError as error:
        raise ContainerConfigurationError(f"{name} must be an integer.") from error
    minimum = 0 if allow_zero else 1024
    if not minimum <= value <= 65535 or (not allow_zero and value == 0):
        raise ContainerConfigurationError(f"{name} must be between 1024 and 65535.")
    return value


def _truthy(value: str) -> bool:
    return value.casefold() in {"1", "true", "yes", "on"}


def _positive_float(
    value: str,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise ContainerConfigurationError(f"{name} must be numeric.") from error
    if not minimum <= parsed <= maximum:
        raise ContainerConfigurationError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return parsed


def _bounded_error(stderr: bytes | None) -> str:
    if not stderr:
        return "configuration validation failed"
    value = stderr.decode("utf-8", errors="replace").strip().splitlines()
    return (value[-1] if value else "configuration validation failed")[:256]


def _event(event: str, *, level: str = "INFO", **fields: Any) -> None:
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "logger": "container.supervisor",
        "event": event,
        **fields,
    }
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), flush=True)


async def _client(command: str, instance: str | None) -> int:
    try:
        reader, writer = await asyncio.open_unix_connection(CONTROL_SOCKET)
    except OSError:
        if command == "health" and os.environ.get("ROBOT_MODE", "").casefold() == "single":
            port = int(os.environ.get("HEALTH_PORT", "8081"))
            return 0 if await _http_health(port) else 1
        print("Container supervisor is not reachable.", file=sys.stderr)
        return 1
    request: dict[str, str] = {"command": command}
    if instance is not None:
        request["instance"] = instance
    writer.write(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
    await writer.drain()
    raw = await asyncio.wait_for(reader.readline(), timeout=35.0)
    writer.close()
    await writer.wait_closed()
    response = json.loads(raw)
    if command != "health":
        print(json.dumps(response, indent=2, sort_keys=True))
    return 0 if response.get("ok") else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or control GetBible Robot container instances."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=(
            "run",
            "list",
            "status",
            "doctor",
            "health",
            "start",
            "stop",
            "restart",
            "reload",
        ),
    )
    parser.add_argument("instance", nargs="?")
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        if arguments.command == "run":
            return asyncio.run(ContainerSupervisor().run())
        return asyncio.run(_client(arguments.command, arguments.instance))
    except (ContainerConfigurationError, OSError) as error:
        _event(
            "container_configuration_error",
            level="ERROR",
            error=str(error)[:512],
        )
        print(f"Container configuration error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
