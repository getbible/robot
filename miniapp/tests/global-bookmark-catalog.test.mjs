import assert from "node:assert/strict";
import test from "node:test";

import {
  EMPTY_GLOBAL_BOOKMARK_CATALOG,
  GLOBAL_BOOKMARK_SOURCE,
  GlobalBookmarkCatalog,
  globalBookmarkCatalogDocumentFromApi,
  globalBookmarkCatalogFromApi,
  globalBookmarkTopicName,
} from "../lib/global-bookmark-catalog.js";
import {
  BOOKMARKS_API_FIXTURE,
  bookmarksApiAssignmentCount,
  bookmarksApiCatalog,
  bookmarksApiDocuments,
  bookmarksApiTopicCount,
} from "./fixtures/bookmarks-api.mjs";

const CATALOG = bookmarksApiCatalog();
const DEFINITIONS = CATALOG.topicDefinitions();
const CATALOG_TOPIC_COUNT = bookmarksApiTopicCount();
const CATALOG_ASSIGNMENT_COUNT = bookmarksApiAssignmentCount();

const LEGACY_TOPIC_NAMES = new Map([
  ["blessings-and-curses", "Blessings & Curses"],
  ["gods-judgment", "God's Judgement"],
]);

function apiDocuments(overrides = {}) {
  return bookmarksApiDocuments(overrides);
}

// Mutates the rendered documents: the catalogue normaliser is what is under
// test here, and it never rehashes the bytes it is given.
function withTopics(mutate) {
  const fixture = apiDocuments();
  mutate(fixture.all.topics);
  return fixture;
}

test("builds the catalogue from the API documents with every published name", () => {
  assert.equal(CATALOG.version, BOOKMARKS_API_FIXTURE.index.catalog_version);
  assert.equal(CATALOG.checksum, BOOKMARKS_API_FIXTURE.checksum);
  assert.equal(CATALOG.topicCount, CATALOG_TOPIC_COUNT);
  assert.equal(CATALOG.assignmentCount, CATALOG_ASSIGNMENT_COUNT);
  assert.ok(CATALOG.uniqueVerseCount < CATALOG_ASSIGNMENT_COUNT);
  assert.deepEqual(
    DEFINITIONS.map((definition) => definition.id),
    [...DEFINITIONS.map((definition) => definition.id)].sort(),
  );
  assert.deepEqual(CATALOG.topicDefinition("grace"), {
    id: "grace",
    name: "Grace",
    name_key: "bookmark_topics.grace",
    color: "#bbf7d0",
    aliases: [],
    default: true,
    names: { en: "Grace", af: "Genade", de: "Gnade", "zh-hant": "恩典" },
  });
  assert.deepEqual(CATALOG.topicDefinition("fear-not").names, {
    en: "Fear Not",
    af: "Moenie vrees nie",
  });
  assert.equal(CATALOG.topicDefinition("missing"), null);
  assert.equal(Object.isFrozen(CATALOG), true);

  const bookmarkIds = CATALOG.bookmarkIds();
  assert.equal(bookmarkIds.length, CATALOG_ASSIGNMENT_COUNT);
  assert.equal(Object.isFrozen(bookmarkIds), true);
  assert.strictEqual(CATALOG.bookmarkIds(), bookmarkIds);
  assert.deepEqual(bookmarkIds, [...bookmarkIds].sort());
  assert.equal(bookmarkIds.every((id) => CATALOG.hasBookmarkId(id)), true);
  assert.equal(CATALOG.hasBookmarkId("global_grace_43_3_16"), true);
  assert.equal(CATALOG.hasBookmarkId("global_grace_1_1_1"), false);
  assert.equal(
    CATALOG.assignmentCountForCanonicalTopics(DEFINITIONS.map((topic) => topic.id)),
    CATALOG_ASSIGNMENT_COUNT,
  );
  // Definitions are copies: a caller cannot reach into the catalogue.
  DEFINITIONS[0].names.en = "changed";
  assert.equal(CATALOG.topicDefinitions()[0].names.en, DEFINITIONS[0].name);
});

test("the cached document round-trips through JSON into the same catalogue", () => {
  const document = globalBookmarkCatalogDocumentFromApi(
    BOOKMARKS_API_FIXTURE.all,
    BOOKMARKS_API_FIXTURE.index,
  );
  const reopened = new GlobalBookmarkCatalog(JSON.parse(JSON.stringify(document)));

  assert.equal(document.catalog_version, CATALOG.version);
  assert.equal(document.checksum, CATALOG.checksum);
  assert.deepEqual(reopened.bookmarkIds(), CATALOG.bookmarkIds());
  assert.deepEqual(reopened.topicDefinitions(), CATALOG.topicDefinitions());
});

test("presents a topic name through the locale, its base language, then English", () => {
  const grace = CATALOG.topicDefinition("grace");
  const fearNot = CATALOG.topicDefinition("fear-not");

  assert.equal(globalBookmarkTopicName(grace, "af"), "Genade");
  assert.equal(globalBookmarkTopicName(grace, "af-ZA"), "Genade");
  assert.equal(globalBookmarkTopicName(grace, "zh-hant"), "恩典");
  assert.equal(globalBookmarkTopicName(grace, "zh-hans"), "Grace");
  assert.equal(globalBookmarkTopicName(grace, "DE"), "Gnade");
  assert.equal(globalBookmarkTopicName(fearNot, "de"), "Fear Not");
  assert.equal(globalBookmarkTopicName(grace, "xx"), "Grace");
  assert.equal(globalBookmarkTopicName(grace, null), "Grace");
  assert.equal(
    globalBookmarkTopicName({ name: "Only English", names: {} }, "af"),
    "Only English",
  );
  assert.equal(globalBookmarkTopicName({ name: "No names" }, "af"), "No names");
  assert.equal(globalBookmarkTopicName(null, "af"), "");
});

test("maps every canonical and legacy topic name onto numeric local ids", () => {
  const localTopics = DEFINITIONS.map((definition, index) => ({
    id: String(index + 1),
    name: LEGACY_TOPIC_NAMES.get(definition.id) ?? definition.name,
  }));
  const resolved = CATALOG.resolveTopics(localTopics);

  assert.equal(resolved.size, CATALOG_TOPIC_COUNT);
  assert.equal(
    CATALOG.assignmentCountForTopics(localTopics),
    CATALOG_ASSIGNMENT_COUNT,
  );

  for (const [canonicalId, legacyName] of LEGACY_TOPIC_NAMES) {
    const local = localTopics.find((topic) => topic.name === legacyName);
    assert.ok(local, `missing numeric fixture for ${legacyName}`);
    assert.equal(resolved.get(canonicalId), local.id);
    assert.equal(CATALOG.canonicalTopicId(local.id, localTopics), canonicalId);

    const [bookmark] = CATALOG.bookmarksForTopic(local.id, localTopics);
    assert.equal(bookmark.topic_id, local.id);
    assert.equal(bookmark.catalog_topic_id, canonicalId);
    assert.equal(bookmark.source, GLOBAL_BOOKMARK_SOURCE);
    assert.equal(bookmark.text, "");
  }
});

test("does not promote a custom topic that reuses a built-in English name", () => {
  const topics = [{ id: "custom-grace", name: "Grace" }];

  assert.equal(CATALOG.resolveTopics(topics).size, 0);
  assert.equal(CATALOG.canonicalTopicId("custom-grace", topics), null);
  assert.deepEqual(CATALOG.bookmarksForTopic("custom-grace", topics), []);
});

test("reads an already-resolved canonical topic without repeating name matching", () => {
  const direct = CATALOG.bookmarksForCanonicalTopic("grace", "private-grace");
  const mapped = CATALOG.bookmarksForTopic(
    "private-grace",
    [{ id: "private-grace", name: "My renamed topic" }],
    { grace: "private-grace" },
  );

  assert.deepEqual(direct, mapped);
  assert.equal(direct.length, 4);
  assert.deepEqual(direct[1], {
    id: "global_grace_43_3_16",
    source: GLOBAL_BOOKMARK_SOURCE,
    catalog_topic_id: "grace",
    topic_id: "private-grace",
    translation: "kjv",
    reference: "John 3:16",
    book: 43,
    book_name: "John",
    chapter: 3,
    verse: 16,
    text: "",
    created_at: 0,
    updated_at: 0,
  });
  assert.throws(
    () => CATALOG.bookmarksForCanonicalTopic("missing-topic", "private-grace"),
    /identifiers/i,
  );
  assert.throws(
    () => CATALOG.bookmarksForCanonicalTopic("grace", "bad.id"),
    /identifiers/i,
  );
});

test("presents an unmapped numeric legacy topic as its built-in definition", () => {
  const topics = [{ id: "21", name: "Grace" }];

  assert.deepEqual(
    CATALOG.topicDefinitionForLocalTopic("21", topics),
    CATALOG.topicDefinition("grace"),
  );
});

test("uses a durable canonical mapping after numeric topics are renamed", () => {
  const localTopics = DEFINITIONS.map((definition, index) => ({
    id: String(index + 1),
    name: LEGACY_TOPIC_NAMES.get(definition.id) ?? definition.name,
  }));
  const initial = CATALOG.resolveTopics(localTopics);
  const mappings = Object.fromEntries(initial);
  const renamedTopics = localTopics.map((topic) => ({
    ...topic,
    name: `My topic ${topic.id}`,
  }));

  assert.equal(CATALOG.resolveTopics(renamedTopics).size, 0);
  const resolved = CATALOG.resolveTopics(renamedTopics, mappings);
  assert.equal(resolved.size, CATALOG_TOPIC_COUNT);
  assert.equal(
    CATALOG.canonicalTopicId(mappings.grace, renamedTopics, mappings),
    "grace",
  );
  const [bookmark] = CATALOG.bookmarksForTopic(
    mappings.grace,
    renamedTopics,
    mappings,
  );
  assert.equal(bookmark.topic_id, mappings.grace);
  assert.equal(bookmark.catalog_topic_id, "grace");
  assert.equal(
    CATALOG.assignmentCountForTopics(renamedTopics, mappings),
    CATALOG_ASSIGNMENT_COUNT,
  );
});

test("resolves every global assignment attached to one verse", () => {
  const localTopics = DEFINITIONS.map((topic) => ({
    id: topic.id,
    name: topic.name,
  }));

  const bookmarks = CATALOG.bookmarksForVerse(
    { book_number: 43, chapter: 3, verse: 16 },
    localTopics,
  );

  assert.deepEqual(
    bookmarks.map((bookmark) => bookmark.catalog_topic_id),
    ["biblical-love", "grace"],
  );
  assert.equal(bookmarks.every((bookmark) => bookmark.source === "global"), true);
  assert.deepEqual(
    CATALOG.bookmarksForVerse({ book: 43, chapter: 3, verse: 2 }, localTopics),
    [],
  );
  assert.equal(
    CATALOG.bookmarkById("global_grace_43_3_16", localTopics)?.topic_id,
    "grace",
  );
  assert.equal(CATALOG.bookmarkById("global_grace_1_1_1", localTopics), null);
});

test("the empty catalogue has no topics and resolves nothing", () => {
  assert.equal(EMPTY_GLOBAL_BOOKMARK_CATALOG.version, 0);
  assert.equal(EMPTY_GLOBAL_BOOKMARK_CATALOG.checksum, null);
  assert.equal(EMPTY_GLOBAL_BOOKMARK_CATALOG.topicCount, 0);
  assert.equal(EMPTY_GLOBAL_BOOKMARK_CATALOG.assignmentCount, 0);
  assert.deepEqual(EMPTY_GLOBAL_BOOKMARK_CATALOG.topicDefinitions(), []);
  assert.deepEqual(EMPTY_GLOBAL_BOOKMARK_CATALOG.bookmarkIds(), []);
  assert.equal(
    EMPTY_GLOBAL_BOOKMARK_CATALOG.resolveTopics([{ id: "grace", name: "Grace" }]).size,
    0,
  );
  assert.deepEqual(
    EMPTY_GLOBAL_BOOKMARK_CATALOG.bookmarksForVerse(
      { book: 43, chapter: 3, verse: 16 },
      [{ id: "grace", name: "Grace" }],
    ),
    [],
  );
  assert.equal(EMPTY_GLOBAL_BOOKMARK_CATALOG.topicDefinition("grace"), null);
  assert.equal(
    EMPTY_GLOBAL_BOOKMARK_CATALOG.assignmentCountForCanonicalTopics(["grace"]),
    0,
  );
});

test("keeps a topic without verse coordinates yet and sorts arriving coordinates", () => {
  const fixture = withTopics((topics) => {
    topics.push({
      id: "new-topic",
      name: "New Topic",
      color: "#abcdef",
      aliases: [],
      default: false,
      verses: [],
    });
    topics.find((entry) => entry.id === "grace").verses.reverse();
  });
  const catalog = globalBookmarkCatalogFromApi(fixture.all, fixture.index);

  assert.deepEqual(catalog.bookmarksForCanonicalTopic("new-topic", "new-topic"), []);
  assert.equal(catalog.topicDefinition("new-topic").default, false);
  assert.equal(catalog.topicDefinition("new-topic").names.en, "New Topic");
  assert.equal(
    catalog.topicDefinitions({ defaultsOnly: true })
      .some((definition) => definition.id === "new-topic"),
    false,
  );
  assert.deepEqual(
    catalog.bookmarksForCanonicalTopic("grace", "grace").map((bookmark) => bookmark.id),
    CATALOG.bookmarksForCanonicalTopic("grace", "grace").map((bookmark) => bookmark.id),
  );
});

test("rejects documents that break the Bookmarks API contract", () => {
  const rejects = (mutate, pattern = /invalid/i) => {
    const fixture = withTopics(mutate);
    assert.throws(
      () => globalBookmarkCatalogFromApi(fixture.all, fixture.index),
      pattern,
    );
  };

  rejects((topics) => { topics[0].id = "Grace"; });
  rejects((topics) => { topics[0].id = "a".repeat(81); });
  rejects((topics) => { topics[0].name = "grace!"; });
  rejects((topics) => { topics[0].name = "G"; });
  rejects((topics) => { topics[0].color = "#abcde"; });
  rejects((topics) => { topics[0].color = "red"; });
  rejects((topics) => { topics[0].default = "yes"; });
  rejects((topics) => { topics[0].aliases = ["Same", "same"]; });
  rejects((topics) => { topics[0].aliases = Array.from({ length: 21 }, (_, i) => `Alias ${i}`); });
  rejects((topics) => { topics.push({ ...topics[0] }); }, /duplicate topics/i);
  rejects((topics) => { topics[0].verses.push([...topics[0].verses[0]]); }, /duplicate entries/i);
  rejects((topics) => { topics[0].verses.push([1, 51, 1]); });
  rejects((topics) => { topics[0].verses.push([67, 1, 1]); });
  rejects((topics) => { topics[0].verses.push([1, 1, 2_001]); });
  rejects((topics) => { topics[0].verses.push([1, 1]); });
  rejects((topics) => { topics[0].verses = null; });

  const fixture = apiDocuments();
  assert.throws(
    () => globalBookmarkCatalogFromApi({ ...fixture.all, schema_version: 2 }, fixture.index),
    /invalid/i,
  );
  assert.throws(
    () => globalBookmarkCatalogFromApi(fixture.all, { ...fixture.index, checksum: "xyz" }),
    /index/i,
  );
  assert.throws(
    () => globalBookmarkCatalogFromApi(fixture.all, { ...fixture.index, catalog_version: 0 }),
    /index/i,
  );
  assert.throws(
    () => globalBookmarkCatalogFromApi({
      ...fixture.all,
      locales: { ...fixture.all.locales, "EN-us": fixture.all.locales.en },
    }, fixture.index),
    /locale/i,
  );
  assert.throws(
    () => globalBookmarkCatalogFromApi({
      ...fixture.all,
      locales: {
        ...fixture.all.locales,
        af: { ...fixture.all.locales.af, locale: "de" },
      },
    }, fixture.index),
    /locale/i,
  );
  assert.throws(
    () => globalBookmarkCatalogFromApi({
      ...fixture.all,
      locales: {
        ...fixture.all.locales,
        af: { ...fixture.all.locales.af, topics: { grace: "" } },
      },
    }, fixture.index),
    /locale/i,
  );
  assert.throws(
    () => new GlobalBookmarkCatalog({ schema_version: 1, catalog_version: -1, topics: [] }),
    /invalid/i,
  );
  assert.throws(
    () => new GlobalBookmarkCatalog({
      schema_version: 1,
      catalog_version: 1,
      checksum: "nope",
      topics: [],
    }),
    /invalid/i,
  );
  assert.throws(
    () => new GlobalBookmarkCatalog({
      schema_version: 1,
      catalog_version: 1,
      topics: Array.from({ length: 1_001 }, (_, index) => ({
        id: `topic-${index}`,
        name: `Topic ${index}`,
        color: "#abcdef",
        aliases: [],
        default: true,
        verses: [],
      })),
    }),
    /invalid/i,
  );
});

test("ignores translated names for topics the catalogue does not carry", () => {
  const fixture = apiDocuments();
  const catalog = globalBookmarkCatalogFromApi({
    ...fixture.all,
    locales: {
      ...fixture.all.locales,
      af: {
        ...fixture.all.locales.af,
        topics: { ...fixture.all.locales.af.topics, "retired-topic": "Verby" },
      },
    },
  }, fixture.index);

  assert.equal(catalog.topicDefinition("retired-topic"), null);
  assert.equal(catalog.topicDefinition("grace").names.af, "Genade");
});
