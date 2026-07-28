import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.cache_maintenance import prune_translation_cache


class CacheMaintenanceTestCase(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
