import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  DEFAULT_BOOKMARK_TOPIC_DEFINITIONS,
  GlobalBookmarkCatalog,
  GLOBAL_BOOKMARK_CATALOG,
  GLOBAL_BOOKMARK_SOURCE,
  GLOBAL_BOOKMARK_TOPIC_DEFINITIONS,
} from "../lib/global-bookmark-catalog.js";
import { GLOBAL_BOOKMARK_DATA } from "../lib/global-bookmark-data.js";

const SOURCE_CSV = new URL(
  "../../data/global-bookmarks/tag-verse.csv",
  import.meta.url,
);
const SOURCE_CSV_SHA256 =
  "85ecb22f34a0d3e59a5f8a6d902d1b45100adfb4a2176722e7f3eb87fe838cf5"; // pragma: allowlist secret

const LEGACY_TOPIC_NAMES = new Map([
  ["blessings-and-curses", "Blessings & Curses"],
  ["gods-judgment", "God's Judgement"],
  ["non-resistance", "Nonresistance"],
  ["omnipotent", "Omnipotence"],
  ["omnipresent", "Omnipresence"],
  ["omniscient", "Omniscience"],
  ["ordinances", "Ordinance"],
  ["spiritual-judgment", "Spiritual Judgement"],
]);

test("tracks the exact source CSV used to generate the catalogue", async () => {
  const source = await readFile(SOURCE_CSV);
  const rows = source.toString("utf8").split(/\r?\n/u).filter(Boolean);

  assert.equal(rows.length, 2_155);
  assert.equal(createHash("sha256").update(source).digest("hex"), SOURCE_CSV_SHA256);
});

test("ships the complete immutable global bookmark catalogue", () => {
  const topicIds = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) => topic.id);
  const topicNameKeys = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map(
    (topic) => topic.name_key,
  );

  assert.equal(GLOBAL_BOOKMARK_CATALOG.assignmentCount, 2_155);
  assert.equal(GLOBAL_BOOKMARK_CATALOG.uniqueVerseCount, 1_916);
  assert.equal(GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.length, 61);
  assert.equal(DEFAULT_BOOKMARK_TOPIC_DEFINITIONS.length, 61);
  assert.equal(new Set(topicIds).size, 61);
  assert.equal(new Set(topicNameKeys).size, 61);
  assert.equal(
    topicNameKeys.every((key) => /^bookmark_topics\.[a-z0-9-]+$/.test(key)),
    true,
  );
  assert.equal(Object.isFrozen(GLOBAL_BOOKMARK_TOPIC_DEFINITIONS), true);
  assert.equal(Object.isFrozen(GLOBAL_BOOKMARK_TOPIC_DEFINITIONS[0]), true);
  assert.equal(Object.isFrozen(GLOBAL_BOOKMARK_TOPIC_DEFINITIONS[0].aliases), true);
  assert.equal(
    GLOBAL_BOOKMARK_CATALOG.assignmentCountForCanonicalTopics(topicIds),
    2_155,
  );
  assert.equal(
    createHash("sha256")
      .update(JSON.stringify(GLOBAL_BOOKMARK_DATA))
      .digest("hex"),
    "38a6dabe556e28fe4428be6f0ec5d4f8c39c53f9dbaa76210a80064119969022", // pragma: allowlist secret
  );
  const verseAssignments = new Map();
  for (const coordinates of Object.values(GLOBAL_BOOKMARK_DATA.bookmarks_by_topic)) {
    for (const coordinate of coordinates) {
      const key = coordinate.join("/");
      verseAssignments.set(key, (verseAssignments.get(key) ?? 0) + 1);
    }
  }
  assert.equal([...verseAssignments.values()].filter((count) => count > 1).length, 210);
  assert.equal(
    [...verseAssignments.values()].reduce((extra, count) => extra + count - 1, 0),
    239,
  );
  assert.equal(Math.max(...verseAssignments.values()), 4);

  const bookmarkIds = GLOBAL_BOOKMARK_CATALOG.bookmarkIds();
  assert.equal(bookmarkIds.length, 2_155);
  assert.equal(Object.isFrozen(bookmarkIds), true);
  assert.strictEqual(GLOBAL_BOOKMARK_CATALOG.bookmarkIds(), bookmarkIds);
  assert.deepEqual(bookmarkIds, [...bookmarkIds].sort());
  assert.equal(bookmarkIds.every((id) => GLOBAL_BOOKMARK_CATALOG.hasBookmarkId(id)), true);
  assert.equal(GLOBAL_BOOKMARK_CATALOG.hasBookmarkId("global_grace_1_1_1"), false);
});

test("rejects unsupported catalogue schema versions", () => {
  assert.throws(
    () => new GlobalBookmarkCatalog({
      data: { schema_version: 2, bookmarks_by_topic: {} },
      topics: [],
      bookNames: [],
    }),
    /invalid/i,
  );
});

test("uses the requested global topic colors", () => {
  assert.deepEqual(
    GLOBAL_BOOKMARK_CATALOG.topicDefinition("biblical-love"),
    {
      id: "biblical-love",
      name: "Biblical Love",
      name_key: "bookmark_topics.biblical-love",
      color: "#a16207",
      aliases: [],
      default: true,
    },
  );
  assert.deepEqual(
    GLOBAL_BOOKMARK_CATALOG.topicDefinition("fear-not"),
    {
      id: "fear-not",
      name: "Fear Not",
      name_key: "bookmark_topics.fear-not",
      color: "#fef08a",
      aliases: [],
      default: true,
    },
  );
});

test("maps every canonical and legacy topic name onto numeric local ids", () => {
  const localTopics = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((definition, index) => ({
    id: String(index + 1),
    name: LEGACY_TOPIC_NAMES.get(definition.id) ?? definition.name,
  }));
  const resolved = GLOBAL_BOOKMARK_CATALOG.resolveTopics(localTopics);

  assert.equal(resolved.size, 61);
  assert.equal(
    GLOBAL_BOOKMARK_CATALOG.assignmentCountForTopics(localTopics),
    2_155,
  );

  for (const [canonicalId, legacyName] of LEGACY_TOPIC_NAMES) {
    const local = localTopics.find((topic) => topic.name === legacyName);
    assert.ok(local, `missing numeric fixture for ${legacyName}`);
    assert.equal(resolved.get(canonicalId), local.id);
    assert.equal(
      GLOBAL_BOOKMARK_CATALOG.canonicalTopicId(local.id, localTopics),
      canonicalId,
    );

    const [bookmark] = GLOBAL_BOOKMARK_CATALOG.bookmarksForTopic(
      local.id,
      localTopics,
    );
    assert.equal(bookmark.topic_id, local.id);
    assert.equal(bookmark.catalog_topic_id, canonicalId);
    assert.equal(bookmark.source, GLOBAL_BOOKMARK_SOURCE);
    assert.equal(bookmark.text, "");
  }
});

test("does not promote a custom topic that reuses a built-in English name", () => {
  const topics = [{ id: "custom-grace", name: "Grace" }];

  assert.equal(GLOBAL_BOOKMARK_CATALOG.resolveTopics(topics).size, 0);
  assert.equal(
    GLOBAL_BOOKMARK_CATALOG.canonicalTopicId("custom-grace", topics),
    null,
  );
  assert.deepEqual(
    GLOBAL_BOOKMARK_CATALOG.bookmarksForTopic("custom-grace", topics),
    [],
  );
});

test("presents an unmapped numeric legacy topic as its built-in definition", () => {
  const topics = [{ id: "21", name: "Grace" }];

  assert.deepEqual(
    GLOBAL_BOOKMARK_CATALOG.topicDefinitionForLocalTopic("21", topics),
    GLOBAL_BOOKMARK_CATALOG.topicDefinition("grace"),
  );
});

test("uses a durable canonical mapping after numeric topics are renamed", () => {
  const localTopics = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((definition, index) => ({
    id: String(index + 1),
    name: LEGACY_TOPIC_NAMES.get(definition.id) ?? definition.name,
  }));
  const initial = GLOBAL_BOOKMARK_CATALOG.resolveTopics(localTopics);
  const mappings = Object.fromEntries(initial);
  const renamedTopics = localTopics.map((topic) => ({
    ...topic,
    name: `My topic ${topic.id}`,
  }));

  assert.equal(GLOBAL_BOOKMARK_CATALOG.resolveTopics(renamedTopics).size, 0);
  const resolved = GLOBAL_BOOKMARK_CATALOG.resolveTopics(
    renamedTopics,
    mappings,
  );
  assert.equal(resolved.size, 61);
  assert.equal(
    GLOBAL_BOOKMARK_CATALOG.canonicalTopicId(
      mappings.grace,
      renamedTopics,
      mappings,
    ),
    "grace",
  );
  const [bookmark] = GLOBAL_BOOKMARK_CATALOG.bookmarksForTopic(
    mappings.grace,
    renamedTopics,
    mappings,
  );
  assert.equal(bookmark.topic_id, mappings.grace);
  assert.equal(bookmark.catalog_topic_id, "grace");
  assert.equal(
    GLOBAL_BOOKMARK_CATALOG.assignmentCountForTopics(renamedTopics, mappings),
    2_155,
  );
});

test("resolves every global assignment attached to one verse", () => {
  const localTopics = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) => ({
    id: topic.id,
    name: topic.name,
  }));
  const multiTopicEntry = Object.entries(GLOBAL_BOOKMARK_DATA.bookmarks_by_topic)
    .flatMap(([topicId, coordinates]) => coordinates.map((coordinate) => ({
      topicId,
      coordinate,
      key: coordinate.join("/"),
    })))
    .reduce((grouped, entry) => {
      const matches = grouped.get(entry.key) ?? [];
      matches.push(entry);
      grouped.set(entry.key, matches);
      return grouped;
    }, new Map());
  const assignments = [...multiTopicEntry.values()].find((items) => items.length > 1);
  assert.ok(assignments);
  const [book, chapter, verse] = assignments[0].coordinate;
  const bookmarks = GLOBAL_BOOKMARK_CATALOG.bookmarksForVerse(
    { book_number: book, chapter, verse },
    localTopics,
  );

  assert.equal(bookmarks.length, assignments.length);
  assert.deepEqual(
    bookmarks.map((bookmark) => bookmark.catalog_topic_id).sort(),
    assignments.map((entry) => entry.topicId).sort(),
  );
  assert.equal(bookmarks.every((bookmark) => bookmark.source === "global"), true);
});
