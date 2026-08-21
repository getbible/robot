import assert from "node:assert/strict";
import test from "node:test";

import {
  GLOBAL_BOOKMARK_CATALOG,
  GLOBAL_BOOKMARK_CATALOG_VERSION,
  GLOBAL_BOOKMARK_TOPIC_DEFINITIONS,
} from "../lib/global-bookmark-catalog.js";
import {
  GLOBAL_BOOKMARK_PREFERENCES_KEY,
  GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX,
  GlobalBookmarkPreferences,
} from "../lib/global-bookmark-preferences.js";

class MemoryStorage {
  values = new Map();
  removed = [];

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.removed.push(key);
    this.values.delete(key);
  }
}

const CATALOG_TOPICS = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) => ({
  id: topic.id,
  name: topic.name,
}));
const CATALOG_TOPIC_IDS = CATALOG_TOPICS.map((topic) => topic.id);
const CATALOG_BOOKMARK_IDS = GLOBAL_BOOKMARK_CATALOG.bookmarkIds();
const FIRST_SCOPE = "a".repeat(64);
const SECOND_SCOPE = "b".repeat(64);

function globalBookmarksFor(topicId) {
  return GLOBAL_BOOKMARK_CATALOG.bookmarksForTopic(topicId, CATALOG_TOPICS);
}

test("persists per-topic visibility under one v2 preference record", () => {
  const storage = new MemoryStorage();
  const firstAccount = new GlobalBookmarkPreferences({
    storage,
    scope: "first-account-scope-is-deliberately-ignored",
  });

  assert.equal(GLOBAL_BOOKMARK_PREFERENCES_KEY, "getbible.miniapp.global-bookmarks.v2");
  assert.equal(firstAccount.persistent, true);
  assert.equal(firstAccount.enabled, false);
  assert.equal(
    firstAccount.enableTopic("grace", GLOBAL_BOOKMARK_CATALOG_VERSION),
    true,
  );
  assert.deepEqual([...storage.values.keys()], [GLOBAL_BOOKMARK_PREFERENCES_KEY]);

  const secondAccount = new GlobalBookmarkPreferences({
    storage,
    scope: "second-account-scope-is-also-ignored",
  });
  assert.equal(secondAccount.catalogVersion, GLOBAL_BOOKMARK_CATALOG_VERSION);
  assert.deepEqual(secondAccount.enabledTopicIds, ["grace"]);
  assert.equal(secondAccount.hasTopic("grace"), true);
});

test("keeps canonical numeric-topic mappings scoped and durable across rename", () => {
  const storage = new MemoryStorage();
  const localTopics = [
    { id: "42", name: "Grace" },
    { id: "77", name: "Biblical Love" },
  ];
  const firstAccount = new GlobalBookmarkPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });
  firstAccount.enableTopic("grace", GLOBAL_BOOKMARK_CATALOG_VERSION);
  assert.equal(
    firstAccount.setTopicMappings(
      { grace: "42", "biblical-love": "77" },
      localTopics,
    ),
    true,
  );

  const mappingKey = `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${FIRST_SCOPE}`;
  assert.deepEqual(JSON.parse(storage.getItem(mappingKey)), {
    version: 1,
    topic_ids: { "biblical-love": "77", grace: "42" },
  });
  const reopened = new GlobalBookmarkPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });
  assert.deepEqual(reopened.topicMappings, {
    "biblical-love": "77",
    grace: "42",
  });
  assert.equal(reopened.hasTopic("grace"), true);

  const otherAccount = new GlobalBookmarkPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: SECOND_SCOPE,
    storage,
  });
  assert.equal(otherAccount.hasTopic("grace"), true);
  assert.deepEqual(otherAccount.topicMappings, {});

  assert.equal(
    reopened.pruneTopicMappings([{ id: "42", name: "Grace renamed" }]),
    true,
  );
  assert.deepEqual(reopened.topicMappings, { grace: "42" });
  assert.deepEqual(JSON.parse(storage.getItem(mappingKey)).topic_ids, {
    grace: "42",
  });
});

test("enables all topics, disables one topic, and can clear the overlay", () => {
  const storage = new MemoryStorage();
  const preferences = new GlobalBookmarkPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    storage,
  });
  const allTopicIds = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) => topic.id);

  assert.equal(
    preferences.enableTopics(allTopicIds, GLOBAL_BOOKMARK_CATALOG_VERSION),
    true,
  );
  assert.equal(preferences.enabledTopicIds.length, 61);
  assert.equal(preferences.hasTopic("fear-not"), true);
  assert.equal(
    preferences.enableTopics(allTopicIds, GLOBAL_BOOKMARK_CATALOG_VERSION),
    false,
  );

  assert.equal(preferences.disableTopic("fear-not"), true);
  assert.equal(preferences.disableTopic("fear-not"), false);
  assert.equal(preferences.hasTopic("fear-not"), false);
  assert.equal(preferences.enabledTopicIds.length, 60);

  assert.equal(preferences.clear(), true);
  assert.equal(preferences.clear(), false);
  assert.equal(preferences.enabled, false);
  assert.deepEqual(preferences.enabledTopicIds, []);
});

test("hides individual global assignments and persists only their stable ids", () => {
  const storage = new MemoryStorage();
  const allowedTopicIds = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) =>
    topic.id
  );
  const [firstGrace, secondGrace] = globalBookmarksFor("grace");
  const preferences = new GlobalBookmarkPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds,
    storage,
  });

  preferences.enableTopic("grace", GLOBAL_BOOKMARK_CATALOG_VERSION);
  assert.equal(preferences.hideBookmark(firstGrace.id), true);
  assert.equal(preferences.hideBookmark(firstGrace.id), false);
  assert.equal(preferences.isBookmarkHidden(firstGrace.id), true);
  assert.equal(preferences.isBookmarkHidden(secondGrace.id), false);
  assert.deepEqual(preferences.hiddenBookmarkIds, [firstGrace.id]);

  const saved = JSON.parse(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY));
  assert.deepEqual(saved.hidden_bookmark_ids, [firstGrace.id]);
  assert.equal(Object.hasOwn(saved, "bookmarks"), false);

  const reopened = new GlobalBookmarkPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds,
    storage,
  });
  assert.equal(reopened.isBookmarkHidden(firstGrace.id), true);
  assert.deepEqual(reopened.hiddenBookmarkIds, [firstGrace.id]);
});

test("restores one topic without changing exclusions from other topics", () => {
  const storage = new MemoryStorage();
  const [grace] = globalBookmarksFor("grace");
  const [fearNot] = globalBookmarksFor("fear-not");
  const preferences = new GlobalBookmarkPreferences({
    allowedTopicIds: ["grace", "fear-not"],
    storage,
  });

  preferences.enableTopics(
    ["grace", "fear-not"],
    GLOBAL_BOOKMARK_CATALOG_VERSION,
  );
  preferences.hideBookmark(grace.id);
  preferences.hideBookmark(fearNot.id);

  assert.equal(preferences.restoreTopic("grace"), true);
  assert.equal(preferences.restoreTopic("grace"), false);
  assert.equal(preferences.isBookmarkHidden(grace.id), false);
  assert.equal(preferences.isBookmarkHidden(fearNot.id), true);
  assert.deepEqual(preferences.hiddenBookmarkIds, [fearNot.id]);
});

test("loading a topic or all topics restores their removed assignments", () => {
  const storage = new MemoryStorage();
  const allTopicIds = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) => topic.id);
  const [grace] = globalBookmarksFor("grace");
  const [fearNot] = globalBookmarksFor("fear-not");
  const preferences = new GlobalBookmarkPreferences({
    allowedTopicIds: allTopicIds,
    storage,
  });

  preferences.enableTopics(allTopicIds, GLOBAL_BOOKMARK_CATALOG_VERSION);
  preferences.hideBookmark(grace.id);
  preferences.hideBookmark(fearNot.id);

  assert.equal(
    preferences.enableTopic("grace", GLOBAL_BOOKMARK_CATALOG_VERSION),
    true,
  );
  assert.equal(preferences.isBookmarkHidden(grace.id), false);
  assert.equal(preferences.isBookmarkHidden(fearNot.id), true);

  assert.equal(
    preferences.enableTopics(allTopicIds, GLOBAL_BOOKMARK_CATALOG_VERSION),
    true,
  );
  assert.deepEqual(preferences.hiddenBookmarkIds, []);
  assert.equal(
    preferences.enableTopics(allTopicIds, GLOBAL_BOOKMARK_CATALOG_VERSION),
    false,
  );
});

test("stores only catalogue metadata and topic ids, never verse data", () => {
  const storage = new MemoryStorage();
  const preferences = new GlobalBookmarkPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    storage,
  });
  const allTopicIds = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) => topic.id);

  preferences.enableTopics(allTopicIds, GLOBAL_BOOKMARK_CATALOG_VERSION);
  preferences.hideBookmark(globalBookmarksFor("grace")[0].id);
  const raw = storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY);
  const saved = JSON.parse(raw);

  assert.deepEqual(Object.keys(saved).sort(), [
    "catalog_version",
    "enabled_topic_ids",
    "hidden_bookmark_ids",
    "version",
  ]);
  assert.equal(saved.version, 2);
  assert.deepEqual(saved.enabled_topic_ids, [...allTopicIds].sort());
  assert.doesNotMatch(
    raw,
    /bookmarks_by_topic|book_name|chapter|reference|translation|"verse"|text/i,
  );
});

test("validates exclusion ids against the exact current catalogue", () => {
  const storage = new MemoryStorage();
  const preferences = new GlobalBookmarkPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    storage,
  });

  assert.throws(
    () => preferences.hideBookmark("global_grace_1_1_1"),
    /unknown/i,
  );
  assert.throws(
    () => preferences.hideBookmark("personal_grace_1_1_1"),
    /invalid/i,
  );
  assert.throws(
    () => preferences.hideBookmark("global_unknown_1_1_1"),
    /unknown/i,
  );
  assert.throws(
    () => preferences.hideBookmark("global_grace_67_1_1"),
    /invalid/i,
  );
});

test("caps exclusions at a safe fallback when no catalogue is supplied", () => {
  const storage = new MemoryStorage();
  const hiddenBookmarkIds = Array.from({ length: 10_000 }, (_, index) => {
    const book = (index % 66) + 1;
    const chapter = Math.floor(index / 66) + 1;
    return `global_grace_${book}_${chapter}_1`;
  });
  storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, JSON.stringify({
    version: 2,
    catalog_version: GLOBAL_BOOKMARK_CATALOG_VERSION,
    enabled_topic_ids: ["grace"],
    hidden_bookmark_ids: hiddenBookmarkIds,
  }));
  const preferences = new GlobalBookmarkPreferences({ storage });

  assert.equal(preferences.hiddenBookmarkIds.length, 10_000);
  assert.throws(
    () => preferences.hideBookmark("global_grace_1_999_2"),
    RangeError,
  );
});

test("prunes fabricated and retired ids against the current bundled catalogue", () => {
  const storage = new MemoryStorage();
  const [validGrace] = globalBookmarksFor("grace");
  storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, JSON.stringify({
    version: 2,
    catalog_version: GLOBAL_BOOKMARK_CATALOG_VERSION,
    enabled_topic_ids: ["grace", "retired-topic"],
    hidden_bookmark_ids: [
      validGrace.id,
      "global_grace_1_1_1",
      "global_retired-topic_1_1_1",
    ],
  }));
  const allowedTopicIds = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) =>
    topic.id
  );

  const preferences = new GlobalBookmarkPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds,
    storage,
  });

  assert.deepEqual(preferences.enabledTopicIds, ["grace"]);
  assert.deepEqual(preferences.hiddenBookmarkIds, [validGrace.id]);
  const saved = JSON.parse(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY));
  assert.deepEqual(saved.enabled_topic_ids, ["grace"]);
  assert.deepEqual(saved.hidden_bookmark_ids, [validGrace.id]);
  assert.throws(
    () => preferences.enableTopic("retired-topic", GLOBAL_BOOKMARK_CATALOG_VERSION),
    /unknown/i,
  );
});

test("removes corrupt preferences without disabling usable browser storage", () => {
  const corruptValues = [
    "not json",
    JSON.stringify({ version: 1, catalog_version: 1, enabled_topic_ids: [] }),
    JSON.stringify({ version: 2, catalog_version: 0, enabled_topic_ids: [] }),
    JSON.stringify({
      version: 2,
      catalog_version: 1,
      enabled_topic_ids: ["not a valid topic id"],
    }),
    JSON.stringify({
      version: 2,
      catalog_version: 1,
      enabled_topic_ids: Array.from({ length: 101 }, (_, index) => `topic-${index}`),
    }),
    JSON.stringify({
      version: 2,
      catalog_version: 1,
      enabled_topic_ids: ["grace"],
      hidden_bookmark_ids: ["global_grace_67_1_1"],
    }),
    JSON.stringify({
      version: 2,
      catalog_version: 1,
      enabled_topic_ids: ["grace"],
      hidden_bookmark_ids: Array.from(
        { length: 10_001 },
        (_, index) => `global_grace_1_${index + 1}_1`,
      ),
    }),
  ];

  for (const raw of corruptValues) {
    const storage = new MemoryStorage();
    storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, raw);

    const preferences = new GlobalBookmarkPreferences({ storage });

    assert.equal(preferences.persistent, true);
    assert.equal(preferences.enabled, false);
    assert.deepEqual(preferences.enabledTopicIds, []);
    assert.equal(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), null);
    assert.deepEqual(storage.removed, [GLOBAL_BOOKMARK_PREFERENCES_KEY]);
  }
});

test("preserves preferences written by a newer application version", () => {
  const storage = new MemoryStorage();
  const raw = JSON.stringify({
    version: 3,
    catalog_version: 2,
    enabled_topic_ids: ["grace"],
    hidden_bookmark_ids: ["global_grace_43_3_16"],
  });
  storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, raw);

  const preferences = new GlobalBookmarkPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    storage,
  });

  assert.equal(preferences.persistent, false);
  assert.equal(preferences.enabled, false);
  assert.equal(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), raw);
  assert.deepEqual(storage.removed, []);
});
