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
  globalBookmarkCatalogWithOverlay,
} from "../lib/global-bookmark-catalog.js";
import { GLOBAL_BOOKMARK_DATA } from "../lib/global-bookmark-data.js";

const SOURCE_CSV = new URL(
  "../../data/global-bookmarks/tag-verse.csv",
  import.meta.url,
);
const TOPIC_SOURCE = new URL(
  "../../data/global-bookmarks/topics.json",
  import.meta.url,
);
const GENERATED_SOURCE = new URL("../lib/global-bookmark-data.js", import.meta.url);
const CATALOG_TOPIC_COUNT = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.length;
const CATALOG_ASSIGNMENT_COUNT = Object.values(
  GLOBAL_BOOKMARK_DATA.bookmarks_by_topic,
).reduce((count, coordinates) => count + coordinates.length, 0);

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

test("tracks the exact sources used to generate the catalogue", async () => {
  const [csvSource, topicSource, generatedSource] = await Promise.all([
    readFile(SOURCE_CSV),
    readFile(TOPIC_SOURCE),
    readFile(GENERATED_SOURCE, "utf8"),
  ]);
  const rows = csvSource.toString("utf8").split(/\r?\n/u).filter(Boolean);

  assert.equal(rows.length, CATALOG_ASSIGNMENT_COUNT);
  assert.match(
    generatedSource,
    new RegExp(`Source CSV SHA-256: ${createHash("sha256").update(csvSource).digest("hex")}`),
  );
  assert.match(
    generatedSource,
    new RegExp(`Topic source SHA-256: ${createHash("sha256").update(topicSource).digest("hex")}`),
  );
  assert.match(
    generatedSource,
    new RegExp(
      `Normalized catalogue SHA-256: ${createHash("sha256")
        .update(JSON.stringify(GLOBAL_BOOKMARK_DATA))
        .digest("hex")}`,
    ),
  );
});

test("keeps canonical English topic metadata in the generated definitions", async () => {
  const source = JSON.parse(await readFile(TOPIC_SOURCE, "utf8"));

  assert.equal(source.schema_version, 1);
  assert.equal(source.catalog_version, GLOBAL_BOOKMARK_DATA.catalog_version);
  assert.deepEqual(
    source.topics,
    GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) => ({
      id: topic.id,
      name: topic.name,
      color: topic.color,
      aliases: topic.aliases,
      default: topic.default,
    })),
  );
});

test("ships the complete immutable global bookmark catalogue", () => {
  const topicIds = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) => topic.id);
  const topicNameKeys = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map(
    (topic) => topic.name_key,
  );

  assert.equal(GLOBAL_BOOKMARK_CATALOG.assignmentCount, CATALOG_ASSIGNMENT_COUNT);
  assert.ok(GLOBAL_BOOKMARK_CATALOG.uniqueVerseCount > 0);
  assert.ok(CATALOG_TOPIC_COUNT > 0);
  assert.equal(
    DEFAULT_BOOKMARK_TOPIC_DEFINITIONS.length,
    GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.filter((topic) => topic.default).length,
  );
  assert.equal(new Set(topicIds).size, CATALOG_TOPIC_COUNT);
  assert.equal(new Set(topicNameKeys).size, CATALOG_TOPIC_COUNT);
  assert.equal(
    topicNameKeys.every((key) => /^bookmark_topics\.[a-z0-9-]+$/.test(key)),
    true,
  );
  assert.equal(Object.isFrozen(GLOBAL_BOOKMARK_TOPIC_DEFINITIONS), true);
  assert.equal(Object.isFrozen(GLOBAL_BOOKMARK_TOPIC_DEFINITIONS[0]), true);
  assert.equal(Object.isFrozen(GLOBAL_BOOKMARK_TOPIC_DEFINITIONS[0].aliases), true);
  assert.equal(
    GLOBAL_BOOKMARK_CATALOG.assignmentCountForCanonicalTopics(topicIds),
    CATALOG_ASSIGNMENT_COUNT,
  );
  const verseAssignments = new Map();
  for (const coordinates of Object.values(GLOBAL_BOOKMARK_DATA.bookmarks_by_topic)) {
    for (const coordinate of coordinates) {
      const key = coordinate.join("/");
      verseAssignments.set(key, (verseAssignments.get(key) ?? 0) + 1);
    }
  }
  assert.equal(
    [...verseAssignments.values()].reduce((count, value) => count + value, 0),
    CATALOG_ASSIGNMENT_COUNT,
  );
  assert.equal(verseAssignments.size, GLOBAL_BOOKMARK_CATALOG.uniqueVerseCount);
  assert.ok(Math.max(...verseAssignments.values()) <= CATALOG_TOPIC_COUNT);

  const bookmarkIds = GLOBAL_BOOKMARK_CATALOG.bookmarkIds();
  assert.equal(bookmarkIds.length, CATALOG_ASSIGNMENT_COUNT);
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

test("uses a separate content version while accepting legacy data", () => {
  const topics = [{
    id: "grace",
    name: "Grace",
    name_key: "bookmark_topics.grace",
    color: "#bbf7d0",
    aliases: [],
    default: true,
  }];
  const bookmarksByTopic = { grace: [[1, 1, 1]] };

  assert.equal(new GlobalBookmarkCatalog({
    data: {
      schema_version: 1,
      catalog_version: 7,
      bookmarks_by_topic: bookmarksByTopic,
    },
    topics,
    bookNames: ["Genesis"],
  }).version, 7);
  assert.equal(new GlobalBookmarkCatalog({
    data: { schema_version: 1, bookmarks_by_topic: bookmarksByTopic },
    topics,
    bookNames: ["Genesis"],
  }).version, 1);
  for (const catalogVersion of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => new GlobalBookmarkCatalog({
      data: {
        schema_version: 1,
        catalog_version: catalogVersion,
        bookmarks_by_topic: bookmarksByTopic,
      },
      topics,
      bookNames: ["Genesis"],
    }), /invalid/i);
  }
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

  assert.equal(resolved.size, CATALOG_TOPIC_COUNT);
  assert.equal(
    GLOBAL_BOOKMARK_CATALOG.assignmentCountForTopics(localTopics),
    CATALOG_ASSIGNMENT_COUNT,
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

test("reads an already-resolved canonical topic without repeating name matching", () => {
  const direct = GLOBAL_BOOKMARK_CATALOG.bookmarksForCanonicalTopic(
    "grace",
    "private-grace",
  );
  const mapped = GLOBAL_BOOKMARK_CATALOG.bookmarksForTopic(
    "private-grace",
    [{ id: "private-grace", name: "My renamed topic" }],
    { grace: "private-grace" },
  );

  assert.deepEqual(direct, mapped);
  assert.ok(direct.length > 0);
  assert.equal(direct.every((bookmark) => (
    bookmark.topic_id === "private-grace" &&
    bookmark.catalog_topic_id === "grace" &&
    bookmark.source === GLOBAL_BOOKMARK_SOURCE
  )), true);
  assert.throws(
    () => GLOBAL_BOOKMARK_CATALOG.bookmarksForCanonicalTopic(
      "missing-topic",
      "private-grace",
    ),
    /identifiers/i,
  );
  assert.throws(
    () => GLOBAL_BOOKMARK_CATALOG.bookmarksForCanonicalTopic("grace", "bad.id"),
    /identifiers/i,
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
  assert.equal(resolved.size, CATALOG_TOPIC_COUNT);
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
    CATALOG_ASSIGNMENT_COUNT,
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

test("strictly merges reviewed additions and bundled removals", () => {
  const [removed] = GLOBAL_BOOKMARK_DATA.bookmarks_by_topic.grace;
  const overlay = {
    schema_version: 1,
    topics: [{
      id: "steadfast-hope",
      name: "Steadfast Hope",
      color: "#bbf7d0",
      aliases: [],
    }],
    associations: {
      add: [
        { topic_id: "grace", book: 43, chapter: 3, verse: 16 },
        { topic_id: "steadfast-hope", book: 45, chapter: 5, verse: 5 },
      ],
      remove: [{
        topic_id: "grace",
        book: removed[0],
        chapter: removed[1],
        verse: removed[2],
      }],
    },
  };

  const catalog = globalBookmarkCatalogWithOverlay(overlay, 7);

  assert.equal(catalog.version, GLOBAL_BOOKMARK_CATALOG.version + 7);
  assert.equal(catalog.topicDefinition("steadfast-hope")?.name, "Steadfast Hope");
  assert.equal(
    catalog.bookmarksForTopic("steadfast-hope", [{
      id: "steadfast-hope",
      name: "Steadfast Hope",
    }]).length,
    1,
  );
  assert.equal(
    catalog.bookmarksForTopic("grace", [{ id: "grace", name: "Grace" }])
      .some((bookmark) => (
        bookmark.book === removed[0] &&
        bookmark.chapter === removed[1] &&
        bookmark.verse === removed[2]
      )),
    false,
  );
});

test("keeps bundled metadata authoritative without hiding later live topics", () => {
  const bundledGrace = GLOBAL_BOOKMARK_CATALOG.topicDefinition("grace");
  const catalog = globalBookmarkCatalogWithOverlay({
    schema_version: 1,
    topics: [
      {
        id: bundledGrace.id,
        name: "Old Grace Spelling",
        color: "#fde68a",
        aliases: ["Stale Grace Alias"],
      },
      {
        id: "still-live-topic",
        name: "Still Live Topic",
        color: "#bbf7d0",
        aliases: [],
      },
    ],
    associations: {
      add: [{
        topic_id: "still-live-topic",
        book: 43,
        chapter: 3,
        verse: 16,
      }],
      remove: [],
    },
  }, 2);
  assert.equal(catalog.topicDefinition("grace")?.name, bundledGrace.name);
  assert.equal(catalog.topicDefinition("grace")?.color, bundledGrace.color);
  assert.equal(catalog.topicDefinition("still-live-topic")?.name, "Still Live Topic");
});

test("rejects malformed, conflicting, unknown, and orphan live overlays", () => {
  const valid = {
    schema_version: 1,
    topics: [],
    associations: { add: [], remove: [] },
  };
  assert.throws(
    () => globalBookmarkCatalogWithOverlay({ ...valid, private_user_id: 42 }),
    /invalid/i,
  );
  assert.throws(
    () => globalBookmarkCatalogWithOverlay({
      ...valid,
      associations: {
        add: [{ topic_id: "unknown", book: 1, chapter: 1, verse: 1 }],
        remove: [],
      },
    }),
    /invalid/i,
  );
  assert.throws(
    () => globalBookmarkCatalogWithOverlay({
      ...valid,
      associations: {
        add: [
          { topic_id: "grace", book: 1, chapter: 1, verse: 1 },
          { topic_id: "grace", book: 1, chapter: 1, verse: 1 },
        ],
        remove: [],
      },
    }),
    /duplicate/i,
  );
  assert.throws(
    () => globalBookmarkCatalogWithOverlay({
      ...valid,
      associations: {
        add: [{ topic_id: "grace", book: 1, chapter: 51, verse: 1 }],
        remove: [],
      },
    }),
    /invalid/i,
  );
  assert.throws(
    () => globalBookmarkCatalogWithOverlay({
      ...valid,
      topics: [{
        id: "orphan-topic",
        name: "Orphan Topic",
        color: "#bbf7d0",
        aliases: [],
      }],
    }),
    /no verse associations/i,
  );
  for (const topic of [
    { id: "Upper_Case", name: "Upper Case", color: "#bbf7d0", aliases: [] },
    { id: "name-collision", name: "Grace", color: "#bbf7d0", aliases: [] },
    {
      id: "alias-collision",
      name: "Fresh Topic",
      color: "#bbf7d0",
      aliases: ["GRACE"],
    },
    {
      id: "invalid-alias",
      name: "Fresh Topic",
      color: "#bbf7d0",
      aliases: ["Oración"],
    },
  ]) {
    assert.throws(
      () => globalBookmarkCatalogWithOverlay({
        ...valid,
        topics: [topic],
        associations: {
          add: [{ topic_id: topic.id, book: 1, chapter: 1, verse: 1 }],
          remove: [],
        },
      }),
      /invalid|reuses|conflicts/i,
    );
  }
});

test("accepts 39 new live topics and rejects the fortieth", () => {
  const topics = Array.from({ length: 40 }, (_, index) => ({
    id: `reviewed-topic-${index + 1}`,
    name: `Reviewed Topic ${index + 1}`,
    color: "#bbf7d0",
    aliases: [],
  }));
  const additions = topics.map((topic, index) => ({
    topic_id: topic.id,
    book: 1,
    chapter: 1,
    verse: index + 1,
  }));
  const overlay = (count) => ({
    schema_version: 1,
    topics: topics.slice(0, count),
    associations: { add: additions.slice(0, count), remove: [] },
  });

  assert.equal(
    globalBookmarkCatalogWithOverlay(overlay(39)).topicDefinitions().length,
    100,
  );
  assert.throws(
    () => globalBookmarkCatalogWithOverlay(overlay(40)),
    /too many topics/i,
  );
});

test("enforces the 10,000-assignment limit after merging the bundled catalogue", () => {
  const capacity = 10_000 - GLOBAL_BOOKMARK_CATALOG.assignmentCount;
  const topic = {
    id: "bulk-reviewed-topic",
    name: "Bulk Reviewed Topic",
    color: "#bbf7d0",
    aliases: [],
  };
  const additions = Array.from({ length: capacity + 1 }, (_, index) => ({
    topic_id: topic.id,
    book: 1,
    chapter: Math.floor(index / 2_000) + 1,
    verse: (index % 2_000) + 1,
  }));
  const overlay = (count) => ({
    schema_version: 1,
    topics: [topic],
    associations: { add: additions.slice(0, count), remove: [] },
  });

  assert.equal(
    globalBookmarkCatalogWithOverlay(overlay(capacity)).assignmentCount,
    10_000,
  );
  assert.throws(
    () => globalBookmarkCatalogWithOverlay(overlay(capacity + 1)),
    /too large/i,
  );
});
