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

class SerialLockManager {
  tails = new Map();

  request(name, _options, operation) {
    const run = () => Promise.resolve().then(operation);
    const result = (this.tails.get(name) ?? Promise.resolve()).then(run, run);
    this.tails.set(name, result.catch(() => undefined));
    return result;
  }
}

class FailingMappingStorage extends MemoryStorage {
  failMappingWrites = false;

  setItem(key, value) {
    if (this.failMappingWrites && key.startsWith(GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX)) {
      throw new Error("mapping write failed");
    }
    super.setItem(key, value);
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
const INSTANCE_SCOPE = "1".repeat(16);
const SECOND_INSTANCE_SCOPE = "2".repeat(16);

function openPreferences(options = {}) {
  return new GlobalBookmarkPreferences({
    instanceScope: INSTANCE_SCOPE,
    ...options,
  });
}

function globalBookmarksFor(topicId) {
  return GLOBAL_BOOKMARK_CATALOG.bookmarksForTopic(topicId, CATALOG_TOPICS);
}

test("requires an explicit Mini App instance scope", () => {
  assert.throws(
    () => new GlobalBookmarkPreferences(),
    /instance scope/i,
  );
  assert.throws(
    () => new GlobalBookmarkPreferences({ instanceScope: "invalid" }),
    /instance scope/i,
  );
});

test("persists per-topic visibility under one v2 preference record", () => {
  const storage = new MemoryStorage();
  const firstAccount = openPreferences({
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

  const secondAccount = openPreferences({
    storage,
    scope: "second-account-scope-is-also-ignored",
  });
  assert.equal(secondAccount.catalogVersion, GLOBAL_BOOKMARK_CATALOG_VERSION);
  assert.deepEqual(secondAccount.enabledTopicIds, ["grace"]);
  assert.equal(secondAccount.hasTopic("grace"), true);
});

test("keeps canonical numeric-topic mappings scoped and durable across rename", async () => {
  const storage = new MemoryStorage();
  const localTopics = [
    { id: "42", name: "Grace" },
    { id: "77", name: "Biblical Love" },
  ];
  const firstAccount = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });
  firstAccount.enableTopic("grace", GLOBAL_BOOKMARK_CATALOG_VERSION);
  assert.equal(
    await firstAccount.setTopicMappings(
      { grace: "42", "biblical-love": "77" },
      localTopics,
    ),
    true,
  );

  const mappingKey =
    `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${INSTANCE_SCOPE}:${FIRST_SCOPE}`;
  assert.deepEqual(JSON.parse(storage.getItem(mappingKey)), {
    version: 2,
    catalog_version: 0,
    contribution_topic_ids: {},
    promoted_topic_ids: [],
    topic_ids: { "biblical-love": "77", grace: "42" },
  });
  const reopened = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });
  assert.deepEqual(reopened.topicMappings, {
    "biblical-love": "77",
    grace: "42",
  });
  assert.equal(reopened.hasTopic("grace"), true);

  const otherAccount = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: SECOND_SCOPE,
    storage,
  });
  assert.equal(otherAccount.hasTopic("grace"), true);
  assert.deepEqual(otherAccount.topicMappings, {});

  const otherInstance = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    instanceScope: SECOND_INSTANCE_SCOPE,
    scope: FIRST_SCOPE,
    storage,
  });
  assert.equal(otherInstance.hasTopic("grace"), true);
  assert.deepEqual(otherInstance.topicMappings, {});

  assert.equal(
    await reopened.pruneTopicMappings([{ id: "42", name: "Grace renamed" }]),
    true,
  );
  assert.deepEqual(reopened.topicMappings, { grace: "42" });
  assert.deepEqual(JSON.parse(storage.getItem(mappingKey)).topic_ids, {
    grace: "42",
  });
});

test("atomically replaces mappings without changing visibility or exclusions", async () => {
  const storage = new MemoryStorage();
  const localTopics = [
    { id: "42", name: "A reviewed topic" },
    { id: "77", name: "An ordinary topic" },
  ];
  const preferences = openPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });
  preferences.enableTopic("grace", GLOBAL_BOOKMARK_CATALOG_VERSION);
  const hiddenGrace = globalBookmarksFor("grace")[0].id;
  preferences.hideBookmark(hiddenGrace);
  await preferences.setTopicMappings(
    { grace: "42", "biblical-love": "77" },
    localTopics,
  );

  assert.equal(
    await preferences.replaceTopicMappings(
      { "spiritual-rebirth": "42", "biblical-love": "77" },
      localTopics,
    ),
    true,
  );
  assert.deepEqual(preferences.topicMappings, {
    "biblical-love": "77",
    "spiritual-rebirth": "42",
  });
  assert.deepEqual(preferences.enabledTopicIds, ["grace"]);
  assert.deepEqual(preferences.hiddenBookmarkIds, [hiddenGrace]);
  assert.equal(
    await preferences.replaceTopicMappings(
      { "spiritual-rebirth": "42", "biblical-love": "77" },
      localTopics,
    ),
    false,
  );
  await assert.rejects(
    () => preferences.replaceTopicMappings(
      { grace: "missing-local-topic" },
      localTopics,
    ),
    /missing topic/i,
  );
});

test("keeps ordinary and contribution mapping layers isolated across clients", async () => {
  const storage = new MemoryStorage();
  const lockManager = new SerialLockManager();
  const localTopics = [
    { id: "42", name: "Ordinary grace" },
    { id: "77", name: "Published fear not" },
    { id: "99", name: "Ordinary love" },
  ];
  const first = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    lockManager,
    scope: FIRST_SCOPE,
    storage,
  });
  const second = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    lockManager,
    scope: FIRST_SCOPE,
    storage,
  });

  await first.setTopicMappings({ grace: "42" }, localTopics);
  await Promise.all([
    first.reconcileContributionTopicMappings({
      mappings: { "fear-not": "77" },
      replacedLocalTopicIds: ["77"],
    }, localTopics),
    second.setTopicMappings({ "biblical-love": "99" }, localTopics),
  ]);

  const reopened = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    lockManager,
    scope: FIRST_SCOPE,
    storage,
  });
  assert.deepEqual(reopened.contributionTopicMappings, { "fear-not": "77" });
  assert.deepEqual(reopened.topicMappings, {
    "biblical-love": "99",
    "fear-not": "77",
    grace: "42",
  });
  assert.deepEqual(JSON.parse(storage.getItem(
    `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${INSTANCE_SCOPE}:${FIRST_SCOPE}`,
  )), {
    version: 2,
    catalog_version: 0,
    contribution_topic_ids: { "fear-not": "77" },
    promoted_topic_ids: [],
    topic_ids: { "biblical-love": "99", grace: "42" },
  });
});

test("enables a promoted topic once and preserves later user exclusions", async () => {
  const storage = new MemoryStorage();
  const localTopics = [{ id: "42", name: "Grace" }];
  const preferences = openPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });
  await preferences.setTopicMappings({ grace: "42" }, localTopics);

  const firstPromotion = await preferences.reconcileContributionTopicMappings({
    catalogVersion: GLOBAL_BOOKMARK_CATALOG_VERSION,
    mappings: { grace: "42" },
    promotedTopicIds: ["grace"],
    replacedLocalTopicIds: ["42"],
  }, localTopics);
  assert.equal(firstPromotion.enabled, 1);
  assert.equal(preferences.hasTopic("grace"), true);
  assert.deepEqual(preferences.promotedTopicIds, ["grace"]);

  const hiddenGrace = globalBookmarksFor("grace")[0].id;
  preferences.hideBookmark(hiddenGrace);
  preferences.disableTopic("grace");
  const repeated = await preferences.reconcileContributionTopicMappings({
    catalogVersion: GLOBAL_BOOKMARK_CATALOG_VERSION,
    mappings: { grace: "42" },
    promotedTopicIds: ["grace"],
    replacedLocalTopicIds: ["42"],
  }, localTopics);

  assert.equal(repeated.enabled, 0);
  assert.equal(preferences.hasTopic("grace"), false);
  assert.deepEqual(preferences.hiddenBookmarkIds, [hiddenGrace]);
  assert.equal(
    await preferences.reconcileContributionTopicMappings({ clear: true }, localTopics)
      .then((result) => result.mappingChanged),
    true,
  );
  assert.deepEqual(preferences.contributionTopicMappings, {});
  assert.deepEqual(preferences.topicMappings, { grace: "42" });
  assert.deepEqual(preferences.promotedTopicIds, ["grace"]);
});

test("first promotion preserves an explicit disable and hidden exclusions", async () => {
  const storage = new MemoryStorage();
  const hiddenGrace = globalBookmarksFor("grace")[0].id;
  const localTopics = [{ id: "42", name: "Grace" }];
  const preferences = openPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });

  assert.equal(preferences.hideBookmark(hiddenGrace), true);
  assert.equal(preferences.disableTopic("grace"), false);
  const promotion = await preferences.reconcileContributionTopicMappings({
    catalogVersion: GLOBAL_BOOKMARK_CATALOG_VERSION,
    mappings: { grace: "42" },
    promotedTopicIds: ["grace"],
    replacedLocalTopicIds: ["42"],
  }, localTopics);

  assert.equal(promotion.enabled, 0);
  assert.equal(preferences.hasTopic("grace"), false);
  assert.deepEqual(preferences.hiddenBookmarkIds, [hiddenGrace]);

  const reopened = openPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });
  assert.equal(reopened.hasTopic("grace"), false);
  assert.deepEqual(reopened.hiddenBookmarkIds, [hiddenGrace]);
  assert.deepEqual(reopened.contributionTopicMappings, { grace: "42" });
});

test("promotion metadata keeps a later exclusion readable after reopening", async () => {
  const storage = new MemoryStorage();
  const hiddenGrace = globalBookmarksFor("grace")[0].id;
  const options = {
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  };
  const first = openPreferences(options);
  await first.reconcileContributionTopicMappings({
    catalogVersion: GLOBAL_BOOKMARK_CATALOG_VERSION,
    mappings: { grace: "42" },
    promotedTopicIds: ["grace"],
    replacedLocalTopicIds: ["42"],
  }, [{ id: "42", name: "Grace" }]);

  const second = openPreferences(options);
  assert.equal(second.catalogVersion, GLOBAL_BOOKMARK_CATALOG_VERSION);
  assert.equal(second.hideBookmark(hiddenGrace), true);

  const third = openPreferences(options);
  assert.equal(third.hasTopic("grace"), true);
  assert.equal(third.isBookmarkHidden(hiddenGrace), true);
});

test("promotion cannot overwrite a newer shared visibility snapshot", async () => {
  const storage = new MemoryStorage();
  const lockManager = new SerialLockManager();
  const hiddenGrace = globalBookmarksFor("grace")[0].id;
  const localTopics = [{ id: "77", name: "Fear not" }];
  const promoter = openPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    lockManager,
    scope: FIRST_SCOPE,
    storage,
  });
  const visibilityEditor = openPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    lockManager,
    scope: FIRST_SCOPE,
    storage,
  });

  visibilityEditor.enableTopic("grace", GLOBAL_BOOKMARK_CATALOG_VERSION);
  visibilityEditor.hideBookmark(hiddenGrace);
  await promoter.reconcileContributionTopicMappings({
    catalogVersion: GLOBAL_BOOKMARK_CATALOG_VERSION,
    mappings: { "fear-not": "77" },
    promotedTopicIds: ["fear-not"],
    replacedLocalTopicIds: ["77"],
  }, localTopics);

  const reopened = openPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    lockManager,
    scope: FIRST_SCOPE,
    storage,
  });
  assert.equal(reopened.hasTopic("grace"), true);
  assert.equal(reopened.isBookmarkHidden(hiddenGrace), true);
  assert.equal(reopened.hasTopic("fear-not"), true);
});

test("rejects a contribution promotion when its mapping cannot persist", async () => {
  const storage = new FailingMappingStorage();
  const mappingKey =
    `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${INSTANCE_SCOPE}:${FIRST_SCOPE}`;
  const preferences = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });
  storage.failMappingWrites = true;

  await assert.rejects(
    () => preferences.reconcileContributionTopicMappings({
      catalogVersion: GLOBAL_BOOKMARK_CATALOG_VERSION,
      mappings: { grace: "42" },
      promotedTopicIds: ["grace"],
      replacedLocalTopicIds: ["42"],
    }, [{ id: "42", name: "Grace" }]),
    /mapping write failed/i,
  );
  assert.equal(storage.getItem(mappingKey), null);
  assert.deepEqual(preferences.contributionTopicMappings, {});
  assert.equal(preferences.hasTopic("grace"), false);

  const reopened = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });
  assert.deepEqual(reopened.contributionTopicMappings, {});
  assert.equal(reopened.hasTopic("grace"), false);
});

test("an authoritative empty review is a no-op without a mapping record", async () => {
  const storage = new MemoryStorage();
  const preferences = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });

  const result = await preferences.reconcileContributionTopicMappings(
    { clear: true },
    [],
  );
  assert.equal(result.changed, false);
  assert.deepEqual(preferences.contributionTopicMappings, {});
  assert.equal(
    storage.getItem(
      `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${INSTANCE_SCOPE}:${FIRST_SCOPE}`,
    ),
    null,
  );
});

test("a fresh review patch removes an unpublished owned mapping only", async () => {
  const storage = new MemoryStorage();
  const localTopics = [
    { id: "42", name: "No longer published" },
    { id: "77", name: "Still published" },
    { id: "99", name: "Ordinary mapping" },
  ];
  const original = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });
  await original.setTopicMappings({ "biblical-love": "99" }, localTopics);
  await original.reconcileContributionTopicMappings({
    mappings: { grace: "42", "fear-not": "77" },
    replacedLocalTopicIds: ["42", "77"],
  }, localTopics);

  const reopened = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });
  await reopened.reconcileContributionTopicMappings({
    mappings: { "fear-not": "77" },
    replacedLocalTopicIds: ["42", "77"],
  }, localTopics);

  assert.deepEqual(reopened.contributionTopicMappings, { "fear-not": "77" });
  assert.deepEqual(reopened.topicMappings, {
    "biblical-love": "99",
    "fear-not": "77",
  });
});

test("does not import an unscoped private topic mapping", () => {
  const storage = new MemoryStorage();
  const legacyMappingKey =
    `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${FIRST_SCOPE}`;
  const rawLegacyMapping = JSON.stringify({
    version: 1,
    topic_ids: { grace: "42" },
  });
  storage.setItem(legacyMappingKey, rawLegacyMapping);

  const preferences = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });

  assert.deepEqual(preferences.topicMappings, {});
  assert.equal(storage.getItem(legacyMappingKey), rawLegacyMapping);
});

test("enables all topics, disables one topic, and can clear the overlay", () => {
  const storage = new MemoryStorage();
  const preferences = openPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    storage,
  });
  const allTopicIds = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) => topic.id);

  assert.equal(
    preferences.enableTopics(allTopicIds, GLOBAL_BOOKMARK_CATALOG_VERSION),
    true,
  );
  assert.equal(preferences.enabledTopicIds.length, allTopicIds.length);
  assert.equal(preferences.hasTopic("fear-not"), true);
  assert.equal(
    preferences.enableTopics(allTopicIds, GLOBAL_BOOKMARK_CATALOG_VERSION),
    false,
  );

  assert.equal(preferences.disableTopic("fear-not"), true);
  assert.equal(preferences.disableTopic("fear-not"), false);
  assert.equal(preferences.hasTopic("fear-not"), false);
  assert.equal(preferences.enabledTopicIds.length, allTopicIds.length - 1);

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
  const preferences = openPreferences({
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

  const reopened = openPreferences({
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
  const preferences = openPreferences({
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
  const preferences = openPreferences({
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
  const preferences = openPreferences({
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
    "disabled_topic_ids",
    "enabled_topic_ids",
    "hidden_bookmark_ids",
    "version",
  ]);
  assert.equal(saved.version, 3);
  assert.deepEqual(saved.enabled_topic_ids, [...allTopicIds].sort());
  assert.doesNotMatch(
    raw,
    /bookmarks_by_topic|book_name|chapter|reference|translation|"verse"|text/i,
  );
});

test("validates exclusion ids against the exact current catalogue", () => {
  const storage = new MemoryStorage();
  const preferences = openPreferences({
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
  const preferences = openPreferences({ storage });

  assert.equal(preferences.hiddenBookmarkIds.length, 10_000);
  assert.throws(
    () => preferences.hideBookmark("global_grace_1_999_2"),
    RangeError,
  );
});

test("prunes fabricated and retired ids against the current bundled catalogue", () => {
  const storage = new MemoryStorage();
  const [validGrace] = globalBookmarksFor("grace");
  const mappingKey =
    `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${INSTANCE_SCOPE}:${FIRST_SCOPE}`;
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
  storage.setItem(mappingKey, JSON.stringify({
    version: 1,
    topic_ids: {
      grace: "42",
      "retired-topic": "77",
    },
  }));
  const allowedTopicIds = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.map((topic) =>
    topic.id
  );

  const preferences = openPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds,
    scope: FIRST_SCOPE,
    storage,
  });

  assert.deepEqual(preferences.enabledTopicIds, ["grace"]);
  assert.deepEqual(preferences.hiddenBookmarkIds, [validGrace.id]);
  assert.deepEqual(preferences.topicMappings, { grace: "42" });
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
    JSON.stringify({ version: 2, catalog_version: -1, enabled_topic_ids: [] }),
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

    const preferences = openPreferences({ storage });

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
    version: 4,
    catalog_version: 2,
    enabled_topic_ids: ["grace"],
    hidden_bookmark_ids: ["global_grace_43_3_16"],
  });
  storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, raw);

  const preferences = openPreferences({
    allowedBookmarkIds: CATALOG_BOOKMARK_IDS,
    allowedTopicIds: CATALOG_TOPIC_IDS,
    storage,
  });

  assert.equal(preferences.persistent, false);
  assert.equal(preferences.enabled, false);
  assert.equal(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), raw);
  assert.deepEqual(storage.removed, []);
});

test("preserves a newer private mapping envelope and rejects downgrade writes", async () => {
  const storage = new MemoryStorage();
  const mappingKey =
    `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${INSTANCE_SCOPE}:${FIRST_SCOPE}`;
  const raw = JSON.stringify({
    version: 3,
    contribution_topic_ids: { grace: "42" },
    promoted_topic_ids: ["grace"],
    topic_ids: {},
  });
  storage.setItem(mappingKey, raw);
  const preferences = openPreferences({
    allowedTopicIds: CATALOG_TOPIC_IDS,
    scope: FIRST_SCOPE,
    storage,
  });

  assert.deepEqual(preferences.topicMappings, {});
  await assert.rejects(
    () => preferences.reconcileContributionTopicMappings({
      mappings: { grace: "42" },
      replacedLocalTopicIds: ["42"],
    }, [{ id: "42", name: "Grace" }]),
    /newer application/i,
  );
  assert.equal(storage.getItem(mappingKey), raw);
  assert.deepEqual(storage.removed, []);
});
