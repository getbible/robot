import copy
import json
import unittest

from modules.bookmark_backup import (
    MAX_BOOKMARK_BACKUP_BYTES,
    MAX_BOOKMARK_MARKINGS,
    MAX_BOOKMARK_TOPICS,
    MAX_BOOKMARK_TOPICS_PER_MARKING,
    BookmarkBackupError,
    BookmarkRestoreFile,
    bookmark_backup_document,
    bookmark_restore_callback_data,
    parse_bookmark_backup_bytes,
    valid_bookmark_restore_callback,
)


def _backup() -> dict[str, object]:
    return {
        "format": "getbible-life-markings",
        "version": 2,
        "exportedAt": "2026-08-20T10:00:00.000Z",
        "colors": [
            {"id": "grace", "name": "Grace", "value": "#bbf7d0"},
        ],
        "markings": [
            {
                "id": "bookmark_1",
                "passage": {"translation": "kjv", "book": 43, "chapter": 3},
                "verse": 16,
                "start": None,
                "end": None,
                "quote": "For God so loved the world.",
                "reference": "John 3:16",
                "colorId": "grace",
                "createdAt": 1_777_000_000_000,
            }
        ],
        "notes": [],
    }


def _backup_v4() -> dict[str, object]:
    backup = _backup()
    backup["version"] = 4
    marking = backup["markings"][0]  # type: ignore[index]
    marking.pop("reference")
    marking.pop("colorId")
    marking["bookName"] = "John"
    marking["colorIndexes"] = [0]
    return backup


class BookmarkBackupTestCase(unittest.TestCase):
    def test_validates_and_canonicalizes_portable_json(self) -> None:
        document = bookmark_backup_document(_backup())
        reopened = parse_bookmark_backup_bytes(document.payload)

        self.assertEqual(document.bookmark_count, 1)
        self.assertEqual(document.topic_count, 1)
        self.assertEqual(document.filename, "getbible-bookmarks-20260820-100000Z.json")
        self.assertEqual(reopened.value, _backup())
        self.assertNotIn(b"user_id", document.payload)
        self.assertEqual(
            document.payload,
            json.dumps(
                _backup(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )

    def test_accepts_legacy_backup_without_format_marker(self) -> None:
        backup = _backup()
        backup.pop("format")
        backup["version"] = 1

        document = bookmark_backup_document(backup)

        self.assertEqual(document.value, backup)

    def test_accepts_v3_multi_topic_markings_and_counts_verses(self) -> None:
        backup = _backup()
        backup["version"] = 3
        backup["colors"].append(  # type: ignore[union-attr]
            {"id": "love", "name": "Love", "value": "#a16207"}
        )
        marking = backup["markings"][0]  # type: ignore[index]
        marking.pop("colorId")
        marking["colorIds"] = ["grace", "love"]

        document = bookmark_backup_document(backup)

        self.assertEqual(document.bookmark_count, 1)
        self.assertEqual(document.topic_count, 2)
        self.assertEqual(document.value, backup)

    def test_v3_rejects_invalid_multi_topic_assignments(self) -> None:
        invalid_color_ids: tuple[object, ...] = (
            None,
            "grace",
            [],
            ["grace", "grace"],
            ["missing"],
            ["grace"] * (MAX_BOOKMARK_TOPICS_PER_MARKING + 1),
        )
        for color_ids in invalid_color_ids:
            backup = _backup()
            backup["version"] = 3
            marking = backup["markings"][0]  # type: ignore[index]
            marking.pop("colorId")
            marking["colorIds"] = color_ids
            with self.subTest(color_ids=color_ids), self.assertRaisesRegex(
                BookmarkBackupError,
                "topics?|unknown topic",
            ):
                bookmark_backup_document(backup)

        mixed = _backup()
        mixed["version"] = 3
        mixed["markings"][0]["colorIds"] = ["grace"]  # type: ignore[index]
        with self.assertRaisesRegex(BookmarkBackupError, "topics"):
            bookmark_backup_document(mixed)

        legacy_with_v3_field = _backup()
        legacy_with_v3_field["markings"][0]["colorIds"] = ["grace"]  # type: ignore[index]
        with self.assertRaisesRegex(BookmarkBackupError, "topics"):
            bookmark_backup_document(legacy_with_v3_field)

    def test_accepts_v4_indexed_topics_and_rejects_mixed_fields(self) -> None:
        backup = _backup_v4()
        backup["colors"].append(  # type: ignore[union-attr]
            {"id": "love", "name": "Love", "value": "#a16207"}
        )
        backup["markings"][0]["colorIndexes"] = [0, 1]  # type: ignore[index]

        document = bookmark_backup_document(backup)

        self.assertEqual(document.bookmark_count, 1)
        self.assertEqual(document.topic_count, 2)
        self.assertEqual(document.value, backup)

        invalid_indexes: tuple[object, ...] = (
            None,
            "0",
            [],
            [0, 0],
            [-1],
            [2],
            [True],
            list(range(MAX_BOOKMARK_TOPICS_PER_MARKING + 1)),
        )
        for indexes in invalid_indexes:
            invalid = copy.deepcopy(backup)
            invalid["markings"][0]["colorIndexes"] = indexes  # type: ignore[index]
            with self.subTest(indexes=indexes), self.assertRaisesRegex(
                BookmarkBackupError,
                "topics?|topic index",
            ):
                bookmark_backup_document(invalid)

        for field, value in (
            ("colorId", "grace"),
            ("colorIds", ["grace"]),
            ("reference", "John 3:16"),
        ):
            mixed = copy.deepcopy(backup)
            mixed["markings"][0][field] = value  # type: ignore[index]
            with self.subTest(field=field), self.assertRaises(
                BookmarkBackupError,
            ):
                bookmark_backup_document(mixed)

    def test_worst_case_utf8_v4_pretty_json_round_trips_under_cap(self) -> None:
        maximum_name = "漢" * 80
        maximum_book_name = "漢" * 128
        maximum_quote = "漢" * 1024

        def fixed_id(prefix: str, index: int) -> str:
            lead = f"{prefix}{index:03d}"
            return lead + ("x" * (128 - len(lead)))

        colors = [
            {
                "id": fixed_id("t", index),
                "name": maximum_name,
                "value": "#abcdef",
            }
            for index in range(MAX_BOOKMARK_TOPICS)
        ]
        markings = [
            {
                "id": fixed_id("b", index),
                "passage": {
                    "translation": "a" * 30,
                    "book": 200,
                    "chapter": 1000,
                },
                "verse": index + 1,
                "start": None,
                "end": None,
                "quote": maximum_quote,
                "bookName": maximum_book_name,
                "colorIndexes": list(range(MAX_BOOKMARK_TOPICS)),
                "createdAt": (2**53) - 1,
            }
            for index in range(MAX_BOOKMARK_MARKINGS)
        ]
        backup = {
            "format": "getbible-life-markings",
            "version": 4,
            "exportedAt": "2026-08-20T10:00:00.000Z",
            "colors": colors,
            "markings": markings,
            "notes": [],
        }
        pretty = json.dumps(backup, ensure_ascii=False, indent=2).encode("utf-8")

        self.assertLessEqual(len(pretty), MAX_BOOKMARK_BACKUP_BYTES)
        reopened = parse_bookmark_backup_bytes(pretty)
        self.assertEqual(reopened.bookmark_count, MAX_BOOKMARK_MARKINGS)
        self.assertEqual(reopened.topic_count, MAX_BOOKMARK_TOPICS)
        self.assertEqual(reopened.value, backup)

    def test_restore_callback_is_durable_and_owner_bound(self) -> None:
        callback_key = ":".join(("123", "fixture-key"))
        callback = bookmark_restore_callback_data(200, callback_key)

        self.assertLessEqual(len(callback.encode("utf-8")), 64)
        self.assertTrue(
            valid_bookmark_restore_callback(
                callback,
                user_id=200,
                secret=callback_key,
            )
        )
        self.assertFalse(
            valid_bookmark_restore_callback(
                callback,
                user_id=201,
                secret=callback_key,
            )
        )

    def test_native_maximum_ascii_bookmark_export_fits_delivery_cap(self) -> None:
        backup = _backup()
        template = backup["markings"][0]  # type: ignore[index]
        markings = []
        for index in range(800):
            marking = copy.deepcopy(template)
            marking["id"] = f"bookmark_{index}"
            marking["quote"] = "x" * 3000
            markings.append(marking)
        backup["markings"] = markings

        document = bookmark_backup_document(backup)

        self.assertLessEqual(len(document.payload), MAX_BOOKMARK_BACKUP_BYTES)

    def test_rejects_invalid_topics_entries_and_oversized_documents(self) -> None:
        orphan = _backup()
        orphan["markings"][0]["colorId"] = "missing"  # type: ignore[index]
        with self.assertRaisesRegex(BookmarkBackupError, "unknown topic"):
            bookmark_backup_document(orphan)

        duplicate = _backup()
        duplicate["colors"] = [
            duplicate["colors"][0],  # type: ignore[index]
            duplicate["colors"][0],  # type: ignore[index]
        ]
        with self.assertRaisesRegex(BookmarkBackupError, "topic"):
            bookmark_backup_document(duplicate)

        with self.assertRaisesRegex(BookmarkBackupError, "large"):
            parse_bookmark_backup_bytes(b" " * (MAX_BOOKMARK_BACKUP_BYTES + 1))

    def test_restore_file_is_small_json_metadata_only(self) -> None:
        restore = BookmarkRestoreFile.validated(
            file_id="telegram-file-id",
            file_unique_id="telegram-unique-id",
            file_name="getbible-bookmarks.json",
            file_size=1024,
        )

        self.assertEqual(restore.file_size, 1024)
        with self.assertRaisesRegex(BookmarkBackupError, "JSON"):
            BookmarkRestoreFile.validated(
                file_id="telegram-file-id",
                file_unique_id="telegram-unique-id",
                file_name="bookmarks.zip",
                file_size=1024,
            )

    def test_rejects_invalid_json_and_envelope_metadata(self) -> None:
        for payload in (b"\xff", b"{"):
            with self.subTest(payload=payload), self.assertRaisesRegex(
                BookmarkBackupError,
                "UTF-8 JSON",
            ):
                parse_bookmark_backup_bytes(payload)

        invalid_values: tuple[tuple[object, str], ...] = (
            ([], "JSON object"),
            ({**_backup(), "format": "another-format"}, "format"),
            ({**_backup(), "version": 5}, "version"),
            ({**_backup(), "version": True}, "version"),
            ({**_backup(), "exportedAt": None}, "date"),
            ({**_backup(), "exportedAt": "not-a-date"}, "date"),
            ({**_backup(), "exportedAt": "2026-08-20T10:00:00"}, "timezone"),
            ({**_backup(), "exportedAt": "0001-01-01T00:00:00+23:59"}, "date"),
            ({**_backup(), "exportedAt": "9999-12-31T23:59:59-23:59"}, "date"),
        )
        for value, message in invalid_values:
            with self.subTest(message=message), self.assertRaisesRegex(
                BookmarkBackupError,
                message,
            ):
                bookmark_backup_document(value)

    def test_rejects_invalid_collection_shapes(self) -> None:
        invalid_collections = (
            {**_backup(), "colors": None},
            {**_backup(), "colors": []},
            {
                **_backup(),
                "colors": [
                    {"id": f"topic_{index}", "name": "Topic", "value": "#123456"}
                    for index in range(MAX_BOOKMARK_TOPICS + 1)
                ],
            },
            {**_backup(), "markings": None},
            {**_backup(), "markings": [None] * (MAX_BOOKMARK_MARKINGS + 1)},
            {**_backup(), "notes": None},
            {**_backup(), "notes": [{}] * (MAX_BOOKMARK_MARKINGS + 1)},
        )
        for value in invalid_collections:
            with self.subTest(
                field_types=tuple(type(item) for item in value.values())
            ), self.assertRaisesRegex(BookmarkBackupError, "collections"):
                bookmark_backup_document(value)

    def test_rejects_malformed_topics_and_markings(self) -> None:
        invalid_documents: list[tuple[dict[str, object], str]] = []

        not_a_topic = _backup()
        not_a_topic["colors"] = ["grace"]
        invalid_documents.append((not_a_topic, "topic"))

        for field, topic_value in (
            ("id", "bad topic id"),
            ("name", " "),
            ("value", "green"),
        ):
            document = _backup()
            document["colors"][0][field] = topic_value  # type: ignore[index]
            invalid_documents.append((document, "topic"))

        not_a_marking = _backup()
        not_a_marking["markings"] = ["bookmark"]
        invalid_documents.append((not_a_marking, "entry"))

        invalid_marking_fields: tuple[tuple[str, object, str], ...] = (
            ("id", "bad bookmark id", "entry"),
            ("passage", "John 3", "entry"),
            ("quote", None, "quote"),
            ("reference", " ", "reference"),
            ("createdAt", True, "timestamp"),
            ("verse", 0, "verse"),
        )
        for field, marking_value, message in invalid_marking_fields:
            document = _backup()
            document["markings"][0][field] = marking_value  # type: ignore[index]
            invalid_documents.append((document, message))

        duplicate_marking = _backup()
        duplicate_marking["markings"] = [
            duplicate_marking["markings"][0],  # type: ignore[index]
            duplicate_marking["markings"][0],  # type: ignore[index]
        ]
        invalid_documents.append((duplicate_marking, "entry"))

        invalid_translation = _backup()
        invalid_translation["markings"][0]["passage"]["translation"] = "KJV!"  # type: ignore[index]
        invalid_documents.append((invalid_translation, "translation"))

        invalid_passage_integer = _backup()
        invalid_passage_integer["markings"][0]["passage"]["book"] = "43"  # type: ignore[index]
        invalid_documents.append((invalid_passage_integer, "book"))

        for document, message in invalid_documents:
            with self.subTest(message=message), self.assertRaisesRegex(
                BookmarkBackupError,
                message,
            ):
                bookmark_backup_document(document)

    def test_validates_text_ranges_notes_and_canonical_size(self) -> None:
        partial_marking = _backup()
        partial_marking["markings"][0]["start"] = 0  # type: ignore[index]
        with self.assertRaisesRegex(BookmarkBackupError, "text range"):
            bookmark_backup_document(partial_marking)

        ranged_marking = _backup()
        ranged_marking["markings"][0]["start"] = 0  # type: ignore[index]
        ranged_marking["markings"][0]["end"] = 4  # type: ignore[index]
        document = bookmark_backup_document(ranged_marking)
        self.assertEqual(document.bookmark_count, 0)

        invalid_note = _backup()
        invalid_note["notes"] = ["note"]
        with self.assertRaisesRegex(BookmarkBackupError, "note"):
            bookmark_backup_document(invalid_note)

        invalid_json_value = _backup()
        invalid_json_value["notes"] = [{"value": float("nan")}]
        with self.assertRaisesRegex(BookmarkBackupError, "invalid JSON values"):
            bookmark_backup_document(invalid_json_value)

        invalid_unicode = _backup()
        invalid_unicode["notes"] = [{"value": "broken \ud800 text"}]
        with self.assertRaisesRegex(BookmarkBackupError, "invalid JSON values"):
            bookmark_backup_document(invalid_unicode)

        oversized = _backup()
        oversized["notes"] = [{"value": "x" * MAX_BOOKMARK_BACKUP_BYTES}]
        with self.assertRaisesRegex(BookmarkBackupError, "too large"):
            bookmark_backup_document(oversized)

    def test_rejects_invalid_restore_callback_inputs(self) -> None:
        invalid_identities = (
            (True, "secret"),
            ("200", "secret"),
            (0, "secret"),
            ((2**52), "secret"),
            (200, ""),
            (200, None),
        )
        for user_id, secret in invalid_identities:
            with self.subTest(
                user_id=user_id,
                secret=secret,
            ), self.assertRaisesRegex(ValueError, "identity"):
                bookmark_restore_callback_data(user_id, secret)  # type: ignore[arg-type]

        callback_key = invalid_identities[0][1]
        self.assertFalse(
            valid_bookmark_restore_callback(
                "not-a-callback",
                user_id=200,
                secret=callback_key,
            )
        )
        self.assertFalse(
            valid_bookmark_restore_callback(
                "gbr:200:abcdefghijklmnop",
                user_id=0,
                secret=callback_key,
            )
        )

    def test_restore_file_rejects_invalid_bounded_fields(self) -> None:
        invalid_fields = (
            {"file_id": None},
            {"file_unique_id": " "},
            {"file_name": "x" * 256 + ".json"},
            {"file_size": True},
            {"file_size": 0},
            {"file_size": MAX_BOOKMARK_BACKUP_BYTES + 1},
        )
        defaults: dict[str, object] = {
            "file_id": "telegram-file-id",
            "file_unique_id": "telegram-unique-id",
            "file_name": "getbible-bookmarks.json",
            "file_size": 1024,
        }
        for changes in invalid_fields:
            with self.subTest(changes=changes), self.assertRaises(
                BookmarkBackupError
            ):
                BookmarkRestoreFile.validated(**(defaults | changes))


if __name__ == "__main__":
    unittest.main()
