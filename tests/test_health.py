import asyncio
import socket
import unittest

from modules.health import HealthServer
from modules.interactions import InteractionStore


class _Service:
    def __init__(self) -> None:
        self.is_ready = True

    async def ready(self) -> bool:
        return self.is_ready

    async def snapshot(self) -> dict:
        return {
            "closed": False,
            "metrics": {"scripture_lookups": 2},
            "circuit": {"state": "closed"},
            "search_circuit": {"state": "open"},
        }


class _Limiter:
    def snapshot(self) -> dict[str, int]:
        return {
            "entries": 2,
            "max_entries": 100,
            "allowed": 3,
            "rejected": 1,
            "evictions": 0,
        }


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class HealthServerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = _Service()
        self.port = _unused_port()
        self.health = HealthServer(
            host="127.0.0.1",
            port=self.port,
            service=self.service,
            limiter=_Limiter(),
            interactions=InteractionStore(
                max_sessions=10,
                ttl_seconds=60,
            ),
        )
        await self.health.start()

    async def asyncTearDown(self) -> None:
        await self.health.close()

    async def request(self, path: str, method: str = "GET") -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        return response

    async def test_health_readiness_metrics_and_method_boundaries(self) -> None:
        health = await self.request("/healthz")
        self.assertIn(b"200 OK", health)
        self.assertIn(b'{"status":"ok"}', health)

        ready = await self.request("/readyz")
        self.assertIn(b"200 OK", ready)
        self.service.is_ready = False
        not_ready = await self.request("/readyz")
        self.assertIn(b"503 Service Unavailable", not_ready)

        metrics = await self.request("/metrics")
        self.assertIn(b"getbible_robot_scripture_lookups 2", metrics)
        self.assertIn(b"getbible_robot_rate_limit_rejected 1", metrics)
        self.assertIn(b"getbible_robot_interaction_sessions 0", metrics)
        self.assertIn(b"getbible_robot_search_circuit_open 1", metrics)

        rejected = await self.request("/healthz", method="POST")
        self.assertIn(b"405 Method Not Allowed", rejected)


if __name__ == "__main__":
    unittest.main()
