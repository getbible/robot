"""The bookmark builder's source contract, without a checkout or a runtime corpus.

Only ``data/topics.json``, ``data/links/*.json`` and ``data/locales/*.json``
are writable. A publication is validated as one complete source snapshot.
See getbible/v1_bookmark_builder/docs/DATA.md for the upstream contract.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .bible_canon import BOOK_CHAPTER_COUNTS

TOPICS_PATH = "data/topics.json"
TOPIC_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
LOCALE_RE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*\Z")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 &'():?-]*[A-Za-z0-9)]\Z")
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}\Z")
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024


class SourceError(ValueError):
    """A source or accepted change does not satisfy the builder contract."""


def json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys, including inside nested objects."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceError("A JSON document contains duplicate keys.")
        result[key] = value
    return result


def decode_json(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=json_object)
    except (ValueError, RecursionError) as error:
        raise SourceError("A JSON document is invalid.") from error


def source_path(path: str) -> bool:
    if path == TOPICS_PATH:
        return True
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "data" or not parts[2].endswith(".json"):
        return False
    stem = parts[2][:-5]
    if parts[1] == "links":
        return bool(len(stem) <= 80 and TOPIC_RE.fullmatch(stem))
    return bool(
        parts[1] == "locales" and stem != "en" and len(stem) <= 16 and LOCALE_RE.fullmatch(stem)
    )


def _object(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or (value.keys() - required - (optional or set()))
    ):
        raise SourceError("A source document has missing or unsupported fields.")


def _schema(value: Any) -> None:
    if type(value) is not int or value != 1:
        raise SourceError("The source schema version must be 1.")


def _topic_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 80 or not TOPIC_RE.fullmatch(value):
        raise SourceError("A topic identifier is invalid.")
    return value


def _english(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 2 <= len(value) <= 80
        or "  " in value
        or not NAME_RE.fullmatch(value)
    ):
        raise SourceError("An English topic name or alias is invalid.")
    return value


def translated_name(value: Any) -> str:
    if not isinstance(value, str):
        raise SourceError("A translated topic name must be a string.")
    result = unicodedata.normalize("NFC", value.strip())
    if not 1 <= len(result) <= 120 or any(
        unicodedata.category(char).startswith("C") for char in result
    ):
        raise SourceError("A translated topic name is empty, too long or contains controls.")
    return result


def coordinate(value: Any) -> tuple[int, int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 3
        or any(type(part) is not int for part in value)
    ):
        raise SourceError("A verse coordinate must contain three integers.")
    book, chapter, verse = value
    if not 1 <= book <= len(BOOK_CHAPTER_COUNTS) or not (
        1 <= chapter <= BOOK_CHAPTER_COUNTS[book - 1] and 1 <= verse <= 2000
    ):
        raise SourceError("A verse coordinate is outside the builder's canon.")
    return book, chapter, verse


def encode_document(path: str, document: Mapping[str, Any]) -> str:
    """Match the builder's format, including one verse triple per line."""
    if path.startswith("data/links/"):
        rows = ["    " + json.dumps(row) for row in document["verses"]]
        verses = "[\n" + ",\n".join(rows) + "\n  ]" if rows else "[]"
        return (
            '{\n  "schema_version": 1,\n  "topic": '
            + json.dumps(document["topic"], ensure_ascii=False)
            + ',\n  "verses": '
            + verses
            + "\n}\n"
        )
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


@dataclass
class SourceCatalogue:
    """A complete, immutable-on-input source tree, with narrowly scoped changes."""

    original: dict[str, str]
    documents: dict[str, Any]

    @classmethod
    def read(cls, files: Mapping[str, str]) -> SourceCatalogue:
        if TOPICS_PATH not in files or len(files) > 1501:
            raise SourceError("The source catalogue is missing or exceeds its file limit.")
        if any(not source_path(path) for path in files):
            raise SourceError("The source snapshot includes an unsupported data path.")
        sizes = [len(text.encode("utf-8")) for text in files.values()]
        if any(size > MAX_FILE_BYTES for size in sizes) or sum(sizes) > MAX_SOURCE_BYTES:
            raise SourceError("The source catalogue exceeds its byte limits.")
        catalogue = cls(dict(files), {path: decode_json(text) for path, text in files.items()})
        catalogue.validate()
        return catalogue

    @property
    def topics(self) -> dict[str, Any]:
        return {topic["id"]: topic for topic in self.documents[TOPICS_PATH]["topics"]}

    @property
    def locales(self) -> dict[str, Any]:
        return {
            document["locale"]: document
            for path, document in sorted(self.documents.items())
            if path.startswith("data/locales/")
        }

    def validate(self) -> None:
        root = self.documents[TOPICS_PATH]
        _object(root, {"schema_version", "topics"})
        _schema(root["schema_version"])
        if not isinstance(root["topics"], list) or len(root["topics"]) > 1000:
            raise SourceError("The topic list is invalid or too large.")
        identifiers: set[str] = set()
        names: set[str] = set()
        for topic in root["topics"]:
            _object(topic, {"id", "name", "color", "aliases", "default"})
            topic_id = _topic_id(topic["id"])
            if topic_id in identifiers:
                raise SourceError("The source has duplicate topic identifiers.")
            identifiers.add(topic_id)
            if not isinstance(topic["color"], str) or not COLOR_RE.fullmatch(topic["color"]):
                raise SourceError("A topic colour is invalid.")
            if type(topic["default"]) is not bool:
                raise SourceError("A topic default flag must be a boolean.")
            aliases = topic["aliases"]
            if not isinstance(aliases, list) or len(aliases) > 20:
                raise SourceError("The topic alias list is invalid.")
            for label in [topic["name"], *aliases]:
                name = _english(label).casefold()
                if name in names:
                    raise SourceError("English topic names and aliases must be unique.")
                names.add(name)
        link_count = 0
        locale_count = 0
        for path, document in self.documents.items():
            if path == TOPICS_PATH:
                continue
            _schema(document.get("schema_version") if isinstance(document, dict) else None)
            stem = path.rsplit("/", 1)[1][:-5]
            if path.startswith("data/links/"):
                _object(document, {"schema_version", "topic", "verses"})
                if document["topic"] != stem or stem not in identifiers:
                    raise SourceError("A links file refers to an unknown or mismatched topic.")
                if not isinstance(document["verses"], list):
                    raise SourceError("A links file must contain a verse list.")
                verses = [coordinate(row) for row in document["verses"]]
                if verses != sorted(set(verses)):
                    raise SourceError("Verse coordinates must be sorted and unique.")
                link_count += len(verses)
            else:
                locale_count += 1
                _object(document, {"schema_version", "locale", "topics"}, {"name"})
                if document["locale"] != stem:
                    raise SourceError("A locale code does not match its filename.")
                if document.get("name") is not None and len(translated_name(document["name"])) > 80:
                    raise SourceError("A locale language name is invalid.")
                entries = document["topics"]
                if not isinstance(entries, dict) or list(entries) != sorted(entries):
                    raise SourceError("Locale topic keys must be sorted.")
                if not entries.keys() <= identifiers:
                    raise SourceError("A locale names an unknown topic.")
                for value in entries.values():
                    translated_name(value)
        if link_count > 100_000 or locale_count > 500:
            raise SourceError("The catalogue exceeds the builder's link or locale limits.")

    def apply(
        self, definitions: Sequence[Mapping[str, Any]], operations: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Apply accepted operations, never replacing unrelated repository data.

        Topic-deletion proposals are resolved by moderation before this point.
        The version-1 bundle cannot delete or rename established topics.
        """
        topics = self.topics
        new_topics: list[dict[str, Any]] = []
        for definition in definitions:
            topic_id = _topic_id(definition["id"])
            if topic_id in topics:
                previous = topics[topic_id]
                if previous["name"].casefold() != str(definition["name"]).casefold() or (
                    previous["color"].lower() != str(definition["color"]).lower()
                ):
                    raise SourceError("An accepted topic conflicts with the upstream definition.")
                aliases = {alias.casefold(): alias for alias in previous["aliases"]}
                for alias in definition["aliases"]:
                    aliases.setdefault(alias.casefold(), alias)
                previous["aliases"] = sorted(aliases.values(), key=str.casefold)
            else:
                topic = copy.deepcopy(dict(definition))
                topic["default"] = False
                topic["color"] = str(topic["color"]).lower()
                topic["aliases"] = sorted(topic["aliases"], key=str.casefold)
                topics[topic_id] = topic
                new_topics.append(topic)
                self.documents[f"data/links/{topic_id}.json"] = {
                    "schema_version": 1,
                    "topic": topic_id,
                    "verses": [],
                }
        self.documents[TOPICS_PATH]["topics"] = sorted(topics.values(), key=lambda item: item["id"])
        for operation in operations:
            topic_id = _topic_id(operation["topic_id"])
            if topic_id not in topics:
                raise SourceError("An accepted verse change has no canonical topic.")
            action = operation["action"]
            if action not in {"add", "remove"}:
                raise SourceError("An accepted verse operation is invalid.")
            point = coordinate([operation["book"], operation["chapter"], operation["verse"]])
            path = f"data/links/{topic_id}.json"
            document = self.documents.setdefault(
                path, {"schema_version": 1, "topic": topic_id, "verses": []}
            )
            verses = {coordinate(row) for row in document["verses"]}
            if action == "add":
                verses.add(point)
            else:
                verses.discard(point)
            document["verses"] = [list(row) for row in sorted(verses)]
        for topic in new_topics:
            if not self.documents[f"data/links/{topic['id']}.json"]["verses"]:
                raise SourceError("A new contributed topic must have an accepted verse link.")
        self.validate()
        return new_topics

    def missing_translations(self, topic_id: str) -> dict[str, Any]:
        """Return locales which still need this topic, preserving human translations."""
        if topic_id not in self.topics:
            raise SourceError("Translations refer to an unknown topic.")
        return {
            locale: document
            for locale, document in self.locales.items()
            if topic_id not in document["topics"]
        }

    def add_translations(self, topic_id: str, names: Mapping[str, str]) -> None:
        if topic_id not in self.topics:
            raise SourceError("Translations refer to an unknown topic.")
        locales = self.locales
        if not set(self.missing_translations(topic_id)) <= set(names) <= set(locales):
            raise SourceError("Translations do not cover exactly the requested locales.")
        for locale, value in names.items():
            entries = locales[locale]["topics"]
            # Never replace a human translation, including during a retry.
            entries.setdefault(topic_id, translated_name(value))
            locales[locale]["topics"] = dict(sorted(entries.items()))

    def changes(self) -> dict[str, str]:
        self.validate()
        changed: dict[str, str] = {}
        total_bytes = 0
        for path, document in self.documents.items():
            before = self.original.get(path)
            if before is None or decode_json(before) != document:
                changed[path] = encode_document(path, document)
            size = len((changed.get(path) or before or "").encode("utf-8"))
            if size > MAX_FILE_BYTES:
                raise SourceError("A generated builder source exceeds its byte limit.")
            total_bytes += size
        if total_bytes > MAX_SOURCE_BYTES:
            raise SourceError("The generated builder source tree exceeds its byte limit.")
        return changed
