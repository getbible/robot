import tempfile
import unittest
from pathlib import Path

from modules.preferences import UserPreferenceStore


class UserPreferenceStoreTestCase(unittest.TestCase):
    def test_memory_store_falls_back_and_remembers_translation(self) -> None:
        store = UserPreferenceStore(
            path=None,
            default_translation="kjv",
            max_users=100,
        )

        self.assertEqual(store.translation_for(200), "kjv")
        store.set_translation(200, "ChiUns")
        self.assertEqual(store.translation_for(200), "chiuns")

    def test_sqlite_store_survives_restart_without_personal_profile_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "preferences.sqlite3"
            first = UserPreferenceStore(
                path=str(path),
                default_translation="kjv",
                max_users=100,
            )
            first.set_translation(200, "chiuns")
            first.close()

            second = UserPreferenceStore(
                path=str(path),
                default_translation="kjv",
                max_users=100,
            )
            self.addCleanup(second.close)
            self.assertEqual(second.translation_for(200), "chiuns")
            self.assertEqual(second.translation_for(201), "kjv")

    def test_store_evicts_oldest_user_when_bound_is_exceeded(self) -> None:
        store = UserPreferenceStore(
            path=None,
            default_translation="kjv",
            max_users=2,
        )
        store.set_translation(1, "asv")
        store.set_translation(2, "aov")
        store.set_translation(3, "chiuns")

        self.assertEqual(store.translation_for(1), "kjv")
        self.assertEqual(store.translation_for(2), "aov")
        self.assertEqual(store.translation_for(3), "chiuns")

    def test_invalid_identity_and_translation_are_rejected(self) -> None:
        store = UserPreferenceStore(
            path=None,
            default_translation="kjv",
            max_users=100,
        )
        with self.assertRaises(ValueError):
            store.set_translation(0, "kjv")
        with self.assertRaises(ValueError):
            store.set_translation(1, "../kjv")


if __name__ == "__main__":
    unittest.main()
