import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from modules.cache_maintenance import (
    CacheJanitor,
    CachePruneResult,
    getbible_cache_root,
    prune_translation_cache,
)


class CacheMaintenanceTestCase(unittest.TestCase):
    def test_cache_root_respects_explicit_xdg_and_home_locations(self) -> None:
        with patch.dict(
            os.environ,
            {"GETBIBLE_CACHE_DIR": "~/robot-cache", "XDG_CACHE_HOME": "/ignored"},
            clear=True,
        ):
            self.assertEqual(
                getbible_cache_root(),
                Path.home() / "robot-cache",
            )

        with patch.dict(
            os.environ,
            {"XDG_CACHE_HOME": "~/xdg-cache"},
            clear=True,
        ):
            self.assertEqual(
                getbible_cache_root(),
                Path.home() / "xdg-cache" / "getbible",
            )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "modules.cache_maintenance.Path.home",
                return_value=Path("/home/robot"),
            ),
        ):
            self.assertEqual(
                getbible_cache_root(),
                Path("/home/robot/.cache/getbible"),
            )

    def test_invalid_budget_and_unavailable_roots_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            prune_translation_cache(max_bytes=0)

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            link = Path(directory) / "cache-link"
            target = Path(directory) / "cache"
            target.mkdir()
            link.symlink_to(target, target_is_directory=True)
            for root in (missing, link):
                with self.subTest(root=root), patch.dict(
                    os.environ,
                    {"GETBIBLE_CACHE_DIR": str(root)},
                    clear=False,
                ):
                    self.assertEqual(
                        prune_translation_cache(max_bytes=1024),
                        CachePruneResult(0, 0, 0, False),
                    )

    def test_stale_unreferenced_objects_are_removed_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = root / "namespace" / "v2"
            objects = version / "objects"
            objects.mkdir(parents=True)
            referenced_name = f"{'a' * 40}.json"
            stale_name = f"{'b' * 40}.json"
            referenced = objects / referenced_name
            stale = objects / stale_name
            referenced.write_bytes(b"current")
            stale.write_bytes(b"old")
            (version / "kjv.metadata.json").write_text(
                json.dumps({"payload": referenced_name}),
                encoding="utf-8",
            )
            old = time.time() - 2 * 24 * 60 * 60
            os.utime(referenced, (old, old))
            os.utime(stale, (old, old))

            with patch.dict(
                os.environ,
                {"GETBIBLE_CACHE_DIR": str(root)},
                clear=False,
            ):
                result = prune_translation_cache(max_bytes=32 * 1024 * 1024)

            self.assertTrue(referenced.is_file())
            self.assertFalse(stale.exists())
            self.assertEqual(result.files_removed, 1)
            self.assertLess(result.bytes_after, result.bytes_before)

    def test_oldest_complete_entries_are_evicted_to_enforce_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = root / "namespace" / "v2"
            objects = version / "objects"
            objects.mkdir(parents=True)
            old_name = f"{'a' * 40}.json"
            current_name = f"{'b' * 40}.json"
            old_payload = objects / old_name
            current_payload = objects / current_name
            old_metadata = version / "old.metadata.json"
            current_metadata = version / "current.metadata.json"
            old_payload.write_bytes(b"old")
            current_payload.write_bytes(b"current")
            old_metadata.write_text(
                json.dumps({"payload": old_name}),
                encoding="utf-8",
            )
            current_metadata.write_text(
                json.dumps({"payload": current_name}),
                encoding="utf-8",
            )
            old = time.time() - 2 * 24 * 60 * 60
            os.utime(old_payload, (old, old))
            os.utime(old_metadata, (old, old))
            current_entry_bytes = (
                current_payload.stat().st_size + current_metadata.stat().st_size
            )

            with patch.dict(
                os.environ,
                {"GETBIBLE_CACHE_DIR": str(root)},
                clear=False,
            ):
                result = prune_translation_cache(max_bytes=current_entry_bytes)

            self.assertFalse(old_metadata.exists())
            self.assertFalse(old_payload.exists())
            self.assertTrue(current_metadata.is_file())
            self.assertTrue(current_payload.is_file())
            self.assertEqual(result.files_removed, 2)
            self.assertLessEqual(result.bytes_after, current_entry_bytes)

    def test_recent_objects_and_malformed_metadata_are_handled_conservatively(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = root / "namespace" / "v2"
            objects = version / "objects"
            objects.mkdir(parents=True)
            recent = objects / f"{'c' * 40}.json"
            stale = objects / f"{'d' * 40}.json"
            recent.write_bytes(b"recent")
            stale.write_bytes(b"stale")
            (version / "broken.metadata.json").write_text(
                "{not-json",
                encoding="utf-8",
            )
            old = time.time() - 2 * 24 * 60 * 60
            os.utime(stale, (old, old))

            with patch.dict(
                os.environ,
                {"GETBIBLE_CACHE_DIR": str(root)},
                clear=False,
            ):
                result = prune_translation_cache(
                    max_bytes=32 * 1024 * 1024,
                )

            self.assertTrue(recent.is_file())
            self.assertFalse(stale.exists())
            self.assertTrue((version / "broken.metadata.json").is_file())
            self.assertEqual(result.files_removed, 1)

    def test_shared_payload_is_retained_until_its_last_metadata_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = root / "namespace" / "v2"
            objects = version / "objects"
            objects.mkdir(parents=True)
            payload_name = f"{'e' * 40}.json"
            payload = objects / payload_name
            payload.write_bytes(b"shared-payload")
            first = version / "first.metadata.json"
            second = version / "second.metadata.json"
            metadata = json.dumps({"payload": payload_name})
            first.write_text(metadata, encoding="utf-8")
            second.write_text(metadata, encoding="utf-8")
            old = time.time() - 3 * 24 * 60 * 60
            newer = time.time() - 2 * 24 * 60 * 60
            os.utime(payload, (old, old))
            os.utime(first, (old, old))
            os.utime(second, (newer, newer))
            total = sum(path.stat().st_size for path in (payload, first, second))
            budget = total - first.stat().st_size

            with patch.dict(
                os.environ,
                {"GETBIBLE_CACHE_DIR": str(root)},
                clear=False,
            ):
                result = prune_translation_cache(max_bytes=budget)

            self.assertFalse(first.exists())
            self.assertTrue(second.is_file())
            self.assertTrue(payload.is_file())
            self.assertEqual(result.files_removed, 1)
            self.assertEqual(result.bytes_after, budget)

    def test_scan_limit_and_unlink_failures_are_reported_without_escaping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            objects = root / "namespace" / "v2" / "objects"
            objects.mkdir(parents=True)
            stale = objects / f"{'f' * 40}.json"
            stale.write_bytes(b"stale")
            (root / "extra.txt").write_bytes(b"extra")
            old = time.time() - 2 * 24 * 60 * 60
            os.utime(stale, (old, old))

            with (
                patch.dict(
                    os.environ,
                    {"GETBIBLE_CACHE_DIR": str(root)},
                    clear=False,
                ),
                patch(
                    "modules.cache_maintenance._MAX_SCANNED_FILES",
                    1,
                ),
                patch.object(Path, "unlink", side_effect=OSError("busy")),
                self.assertLogs(
                    "modules.cache_maintenance",
                    level="WARNING",
                ) as captured,
            ):
                result = prune_translation_cache(max_bytes=1)

            self.assertTrue(stale.is_file())
            self.assertTrue(result.scan_truncated)
            self.assertEqual(result.files_removed, 0)
            self.assertIn("remains above its byte budget", captured.output[0])


class CacheJanitorTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_start_is_idempotent_and_close_cancels_the_worker(self) -> None:
        janitor = CacheJanitor(max_bytes=1024, interval_seconds=60)
        janitor._run = AsyncMock(side_effect=asyncio.Event().wait)

        janitor.start()
        first_task = janitor._task
        janitor.start()
        await asyncio.sleep(0)

        self.assertIsNotNone(first_task)
        self.assertIs(janitor._task, first_task)
        janitor._run.assert_awaited_once()

        await janitor.close()
        await janitor.close()

        self.assertIsNone(janitor._task)
        self.assertTrue(first_task.cancelled())

    async def test_worker_logs_pruning_scan_limits_and_safe_failures(self) -> None:
        janitor = CacheJanitor(max_bytes=1024, interval_seconds=60)
        pruned = CachePruneResult(
            bytes_before=4096,
            bytes_after=1024,
            files_removed=3,
            scan_truncated=True,
        )
        sleep = AsyncMock(side_effect=asyncio.CancelledError)
        with (
            patch(
                "modules.cache_maintenance.asyncio.to_thread",
                AsyncMock(return_value=pruned),
            ) as to_thread,
            patch("modules.cache_maintenance.asyncio.sleep", sleep),
            self.assertLogs("modules.cache_maintenance") as captured,
            self.assertRaises(asyncio.CancelledError),
        ):
            await janitor._run()

        to_thread.assert_awaited_once_with(
            prune_translation_cache,
            max_bytes=1024,
        )
        self.assertTrue(any("Pruned stale" in line for line in captured.output))
        self.assertTrue(any("file safety limit" in line for line in captured.output))

        failure_sleep = AsyncMock(side_effect=asyncio.CancelledError)
        with (
            patch(
                "modules.cache_maintenance.asyncio.to_thread",
                AsyncMock(side_effect=OSError("unavailable")),
            ),
            patch(
                "modules.cache_maintenance.asyncio.sleep",
                failure_sleep,
            ),
            self.assertLogs(
                "modules.cache_maintenance",
                level="WARNING",
            ) as failure_log,
            self.assertRaises(asyncio.CancelledError),
        ):
            await janitor._run()

        self.assertIn("failed safely (OSError)", failure_log.output[0])


if __name__ == "__main__":
    unittest.main()
