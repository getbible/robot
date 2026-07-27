import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
