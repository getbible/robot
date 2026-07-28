import unittest
from pathlib import Path

from modules.miniapp_tornado import ClientAddressResolver

ROOT = Path(__file__).resolve().parents[1]
TORNADO_ADAPTER = ROOT / "modules" / "miniapp_tornado.py"


class MiniAppStaticCacheTestCase(unittest.TestCase):
    def test_index_and_packaged_assets_cannot_remain_stale(self) -> None:
        source = TORNADO_ADAPTER.read_text(encoding="utf-8")

        self.assertIn('if path in {"", "index.html"}:', source)
        self.assertIn('"no-store, max-age=0"', source)
        self.assertIn(
            '"no-cache, max-age=0, must-revalidate"',
            source,
        )
        self.assertNotIn('"public, max-age=3600"', source)

    def test_forwarded_client_address_requires_a_trusted_direct_peer(self) -> None:
        resolver = ClientAddressResolver(
            ("127.0.0.1/32", "172.20.0.0/24"),
        )
        self.assertEqual(
            resolver.resolve("198.51.100.10", "192.0.2.1"),
            "198.51.100.10",
        )
        self.assertEqual(
            resolver.resolve("127.0.0.1", "192.0.2.1"),
            "192.0.2.1",
        )
        self.assertEqual(
            resolver.resolve(
                "172.20.0.4",
                "192.0.2.1, 172.20.0.3",
            ),
            "192.0.2.1",
        )
        self.assertEqual(
            resolver.resolve("127.0.0.1", "invalid, 192.0.2.1"),
            "127.0.0.1",
        )


if __name__ == "__main__":
    unittest.main()
