"""Loopback-only health, readiness, and aggregate metrics endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any

from .rate_limit import InboundRateLimiter
from .service import ScriptureService

LOGGER = logging.getLogger(__name__)


class HealthServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        service: ScriptureService,
        limiter: InboundRateLimiter,
    ) -> None:
        self._host = host
        self._port = port
        self._service = service
        self._limiter = limiter
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if self._port == 0:
            LOGGER.info("Health endpoint disabled")
            return
        self._server = await asyncio.start_server(
            self._handle,
            host=self._host,
            port=self._port,
            limit=2048,
        )
        LOGGER.info("Health endpoint listening on %s:%d", self._host, self._port)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=1.0)
            if len(request_line) > 1024:
                await self._write(writer, 414, {"status": "request_too_large"})
                return
            pieces = request_line.decode("ascii", errors="replace").strip().split()
            if len(pieces) != 3 or pieces[0] != "GET":
                await self._write(writer, 405, {"status": "method_not_allowed"})
                return
            path = pieces[1].split("?", 1)[0]
            if path == "/healthz":
                await self._write(writer, 200, {"status": "ok"})
            elif path == "/readyz":
                ready = await self._service.ready()
                await self._write(
                    writer,
                    200 if ready else 503,
                    {"status": "ready" if ready else "not_ready"},
                )
            elif path == "/metrics":
                await self._write_metrics(writer)
            else:
                await self._write(writer, 404, {"status": "not_found"})
        except (TimeoutError, UnicodeError, ValueError):
            await self._write(writer, 400, {"status": "bad_request"})
        except Exception:
            LOGGER.exception("Health endpoint failed safely")
            await self._write(writer, 500, {"status": "error"})
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def _write_metrics(self, writer: asyncio.StreamWriter) -> None:
        service = await self._service.snapshot()
        limiter = self._limiter.snapshot()
        lines = [
            "# TYPE getbible_robot_ready gauge",
            f"getbible_robot_ready {1 if await self._service.ready() else 0}",
        ]
        for name, value in sorted(service["metrics"].items()):
            lines.append(f"getbible_robot_{_metric_name(name)} {int(value)}")
        for name, value in sorted(limiter.items()):
            lines.append(f"getbible_robot_rate_limit_{_metric_name(name)} {int(value)}")
        circuit = service["circuit"]
        lines.append(
            "getbible_robot_circuit_open "
            f"{1 if circuit['state'] == 'open' else 0}"
        )
        body = ("\n".join(lines) + "\n").encode("utf-8")
        await self._write_bytes(writer, 200, body, "text/plain; version=0.0.4")

    async def _write(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, Any],
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        await self._write_bytes(writer, status, body, "application/json")

    @staticmethod
    async def _write_bytes(
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        content_type: str,
    ) -> None:
        reasons = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            414: "URI Too Long",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }
        header = (
            f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(header + body)
        await writer.drain()


def _metric_name(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value)
