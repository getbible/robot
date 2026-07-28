import sqlite3
import tempfile
import unittest
from pathlib import Path

from modules.preferences import ReaderLocation, SearchDefaults, UserPreferenceStore


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
        self.assertEqual(
            store.preferences_for(200).search_defaults,
            SearchDefaults(),
        )

    def test_search_modes_persist_without_search_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "preferences.sqlite3"
            first = UserPreferenceStore(
                path=str(path),
                default_translation="kjv",
                max_users=100,
            )
            expected = SearchDefaults.validated(
                {
                    "words": "phrase",
                    "match": "substring",
                    "scope": "new_testament",
                    "case_sensitive": True,
                    "diacritics": "insensitive",
                    "sort": "relevance",
                }
            )
            first.set_search_defaults(200, expected)
            first.set_translation(200, "asv")
            first.close()

            second = UserPreferenceStore(
                path=str(path),
                default_translation="kjv",
                max_users=100,
            )
            self.addCleanup(second.close)
            preferences = second.preferences_for(200)
            self.assertEqual(preferences.translation, "asv")
            self.assertEqual(preferences.search_defaults, expected)
            self.assertNotIn("query", preferences.as_dict())

    def test_reader_location_persists_only_small_content_free_identifiers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "preferences.sqlite3"
            first = UserPreferenceStore(
                path=str(path),
                default_translation="kjv",
                max_users=100,
            )
            expected = ReaderLocation("kjv", 43, 3, 16)
            first.set_reader_location(200, expected)
            first.close()

            second = UserPreferenceStore(
                path=str(path),
                default_translation="kjv",
                max_users=100,
            )
            self.addCleanup(second.close)
            preferences = second.preferences_for(200)
            self.assertEqual(preferences.reader_location, expected)
            encoded = str(preferences.as_dict())
            self.assertNotIn("For God", encoded)
            self.assertLess(len(encoded), 512)

    def test_existing_translation_database_is_migrated_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "preferences.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    translation TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO user_preferences (user_id, translation, updated_at)
                VALUES (200, 'chiuns', 1)
                """
            )
            connection.commit()
            connection.close()

            store = UserPreferenceStore(
                path=str(path),
                default_translation="kjv",
                max_users=100,
            )
            self.addCleanup(store.close)
            self.assertEqual(store.translation_for(200), "chiuns")
            self.assertEqual(
                store.preferences_for(200).search_defaults,
                SearchDefaults(),
            )

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
        with self.assertRaises(ValueError):
            store.set_search_defaults(1, {"words": "everything"})
        with self.assertRaises(ValueError):
            store.set_search_defaults(1, {"query": "grace"})
        with self.assertRaises(ValueError):
            store.set_reader_location(
                1,
                {
                    "translation": "kjv",
                    "book": 43,
                    "chapter": 3,
                    "verse": 16,
                    "text": "must never persist",
                },
            )


if __name__ == "__main__":
    unittest.main()
