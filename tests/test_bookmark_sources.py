"""Publisher validation must reject every source the builder cannot consume."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from modules.bookmark_sources import SourceCatalogue, SourceError, translated_name
from tests.test_contribution_publication import BUNDLE, TOPIC, source_files


class BuilderValidationTests(unittest.TestCase):
    def test_translation_rejects_every_unicode_control_category(self) -> None:
        # Builder catalog.translated_name excludes every Unicode category C*:
        # format/bidi characters, private-use and unassigned code points too.
        for character in ("\x00", "\u202e", "\ue000", "\u0378", "\ud800"):
            with self.subTest(character=repr(character)), self.assertRaises(SourceError):
                translated_name("Mercy" + character)
        self.assertEqual(translated_name("  Mise\u0301ricorde  "), "Miséricorde")

    def test_language_name_is_validated_before_translator_input(self) -> None:
        for name in ("", "\u202eArabic", "x" * 81, 7):
            with self.subTest(name=name):
                files = source_files()
                document = json.loads(files["data/locales/ar.json"])
                document["name"] = name
                files["data/locales/ar.json"] = json.dumps(document)
                with self.assertRaises(SourceError):
                    SourceCatalogue.read(files)

    def test_case_only_english_definition_preserves_upstream_name_and_colour(self) -> None:
        catalogue = SourceCatalogue.read(source_files())
        catalogue.apply([{"id": "faith", "name": "FAITH", "color": "#654321", "aliases": []}], [])
        self.assertEqual(catalogue.changes(), {})
        self.assertEqual(catalogue.topics["faith"]["name"], "Faith")

    def test_generated_files_are_bounded_before_a_git_tree_can_be_created(self) -> None:
        catalogue = SourceCatalogue.read(source_files())
        catalogue.apply([TOPIC], [{**BUNDLE["associations"]["add"][0], "action": "add"}])
        with patch("modules.bookmark_sources.MAX_FILE_BYTES", 1), self.assertRaises(SourceError):
            catalogue.changes()
        with patch("modules.bookmark_sources.MAX_SOURCE_BYTES", 1), self.assertRaises(SourceError):
            catalogue.changes()

    def test_partial_translation_response_cannot_commit(self) -> None:
        catalogue = SourceCatalogue.read(source_files())
        catalogue.apply([TOPIC], [{**BUNDLE["associations"]["add"][0], "action": "add"}])
        with self.assertRaises(SourceError):
            catalogue.add_translations("mercy", {"af": "Barmhartigheid"})


if __name__ == "__main__":
    unittest.main()
