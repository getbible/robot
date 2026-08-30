import assert from "node:assert/strict";
import test from "node:test";

import {
  BOOKMARK_TOPIC_COLORS,
  BOOKMARK_STORAGE_PREFIX,
  BookmarkStore,
  MAX_BOOKMARKS,
  MAX_BOOKMARK_TOPICS,
  MAX_RECENT_BOOKMARK_TOPICS,
} from "../lib/bookmark-store.js";
import {
  TELEGRAM_BOOKMARK_CLOUD_MAX_KEYS,
  TELEGRAM_CLOUD_VALUE_MAX_CHARS,
  TELEGRAM_LAST_READ_STORAGE_PREFIX,
  TelegramBookmarkStorage,
} from "../lib/telegram-bookmark-storage.js";

const SCOPE = "a".repeat(64);
const ROOT = `gb_bm_v1_${SCOPE}`;

class MemoryStorage {
  values = new Map();

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

class TelegramStorageMock {
  calls = [];
  values = new Map();
  timeoutMethods = new Set();
  errors = new Map();
  falseMethods = new Set();
  falseSetKeysOnce = new Set();

  getItem(key, callback) {
    this.calls.push(["getItem", key]);
    this.#reply("getItem", callback, this.values.get(key) ?? "");
  }

  getItems(keys, callback) {
    this.calls.push(["getItems", [...keys]]);
    this.#reply(
      "getItems",
      callback,
      Object.fromEntries(
        keys.filter((key) => this.values.has(key))
          .map((key) => [key, this.values.get(key)]),
      ),
    );
  }

  getKeys(callback) {
    this.calls.push(["getKeys"]);
    this.#reply("getKeys", callback, [...this.values.keys()]);
  }

  setItem(key, value, callback) {
    this.calls.push(["setItem", key, value]);
    const refusedOnce = this.falseSetKeysOnce.delete(key);
    if (
      !this.timeoutMethods.has("setItem") &&
      !this.errors.has("setItem") &&
      !this.falseMethods.has("setItem") &&
      !refusedOnce
    ) {
      this.values.set(key, String(value));
    }
    this.#reply(
      "setItem",
      callback,
      !this.falseMethods.has("setItem") && !refusedOnce,
    );
  }

  removeItem(key, callback) {
    this.calls.push(["removeItem", key]);
    if (
      !this.timeoutMethods.has("removeItem") &&
      !this.errors.has("removeItem") &&
      !this.falseMethods.has("removeItem")
    ) {
      this.values.delete(key);
    }
    this.#reply(
      "removeItem",
      callback,
      !this.falseMethods.has("removeItem"),
    );
  }

  removeItems(keys, callback) {
    this.calls.push(["removeItems", [...keys]]);
    if (
      !this.timeoutMethods.has("removeItems") &&
      !this.errors.has("removeItems") &&
      !this.falseMethods.has("removeItems")
    ) {
      for (const key of keys) {
        this.values.delete(key);
      }
    }
    this.#reply(
      "removeItems",
      callback,
      !this.falseMethods.has("removeItems"),
    );
  }

  #reply(method, callback, value) {
    if (this.timeoutMethods.has(method)) {
      return;
    }
    queueMicrotask(() => callback(this.errors.get(method) ?? null, value));
  }
}

class ControlledTelegramStorage {
  calls = [];
  completions = [];
  pending = [];
  values = new Map();

  getItem(key, callback) {
    this.calls.push(["getItem", key]);
    queueMicrotask(() => callback(null, this.values.get(key) ?? ""));
  }

  getKeys(callback) {
    this.calls.push(["getKeys"]);
    queueMicrotask(() => callback(null, [...this.values.keys()]));
  }

  setItem(key, value, callback) {
    this.calls.push(["setItem", key, value]);
    this.pending.push({ method: "setItem", key, value, callback });
  }

  removeItem(key, callback) {
    this.calls.push(["removeItem", key]);
    this.pending.push({ method: "removeItem", key, callback });
  }

  completeNext(method) {
    const index = this.pending.findIndex((operation) =>
      operation.method === method
    );
    assert.notEqual(index, -1, `No pending ${method} callback.`);
    const [operation] = this.pending.splice(index, 1);
    if (method === "setItem") {
      this.values.set(operation.key, String(operation.value));
      this.completions.push([
        method,
        JSON.parse(operation.value).record_updated_at,
      ]);
    } else {
      this.values.delete(operation.key);
      this.completions.push([method]);
    }
    operation.callback(null, true);
  }

  pendingCount(method) {
    return this.pending.filter((operation) => operation.method === method)
      .length;
  }
}

async function waitForMicrotask(predicate, message) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (predicate()) {
      return;
    }
    await Promise.resolve();
  }
  assert.fail(message);
}

function webApp({ cloud = null, device = null, version = "9.0" } = {}) {
  return {
    version,
    CloudStorage: cloud,
    DeviceStorage: device,
    isVersionAtLeast(required) {
      return Number(version) >= Number(required);
    },
  };
}

function bookmark(overrides = {}) {
  return {
    id: "bookmark_1",
    topic_id: "grace",
    translation: "kjv",
    reference: "John 3:16",
    book: 43,
    book_name: "John",
    chapter: 3,
    verse: 16,
    text: "For God so loved the world.",
    created_at: 10,
    updated_at: 10,
    ...overrides,
  };
}

function aggregate(updatedAt, overrides = {}) {
  return {
    version: 1,
    active_topic_id: "grace",
    topics: [{ id: "grace", name: "Grace", color: "#bbf7d0" }],
    bookmarks: [bookmark()],
    record_updated_at: updatedAt,
    ...overrides,
  };
}

function aggregateKey() {
  return `${BOOKMARK_STORAGE_PREFIX}:${SCOPE}`;
}

function seedCloud(cloud, record) {
  const timestamp = record.record_updated_at;
  cloud.values.set(`${ROOT}_meta`, JSON.stringify({
    version: 1,
    record_updated_at: timestamp,
    active_topic_id: record.active_topic_id,
    topic_count: record.topics.length,
    bookmark_count: record.bookmarks.length,
  }));
  record.topics.forEach((topic, index) => {
    cloud.values.set(
      `${ROOT}_topic_${String(index).padStart(3, "0")}`,
      JSON.stringify({ record_updated_at: timestamp, topic }),
    );
  });
  for (const item of record.bookmarks) {
    cloud.values.set(
      `${ROOT}_verse_${String(item.book).padStart(3, "0")}_${
        String(item.chapter).padStart(4, "0")
      }_${String(item.verse).padStart(4, "0")}`,
      JSON.stringify({ record_updated_at: timestamp, bookmark: item }),
    );
  }
}

function setAggregate(storage, record) {
  storage.setItem(aggregateKey(), JSON.stringify(record));
}

test("hydrates the newest account record and repairs local and device caches", async () => {
  const local = new MemoryStorage();
  const device = new TelegramStorageMock();
  const cloud = new TelegramStorageMock();
  setAggregate(local, aggregate(10));
  device.values.set(`${ROOT}_cache`, JSON.stringify(aggregate(20)));
  seedCloud(cloud, aggregate(30, {
    bookmarks: [bookmark({ topic_id: "grace", text: "Cloud winner" })],
  }));

  const statuses = [];
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, device }),
    onStatus: (status) => statuses.push(status),
  });

  assert.equal(storage.status.source, "cloud");
  const hydrated = JSON.parse(storage.getItem(aggregateKey()));
  assert.equal(hydrated.version, 3);
  assert.equal(hydrated.record_updated_at, 30);
  assert.deepEqual(hydrated.bookmarks[0].topic_ids, ["grace"]);
  assert.equal(JSON.parse(local.getItem(aggregateKey())).bookmarks[0].text, "Cloud winner");
  await storage.flush();
  assert.equal(JSON.parse(device.values.get(`${ROOT}_cache`)).record_updated_at, 30);
  assert.equal(JSON.parse(cloud.values.get(`${ROOT}_meta`)).version, 3);
  const cloudBookmark = JSON.parse(
    cloud.values.get(`${ROOT}_verse_043_0003_0016`),
  ).bookmark;
  assert.deepEqual(cloudBookmark.topic_indexes, [0]);
  assert.equal(Object.hasOwn(cloudBookmark, "topic_ids"), false);
  assert.equal(Object.hasOwn(cloudBookmark, "topic_id"), false);
  assert.equal(storage.status.pending, false);
  assert.ok(statuses.some((status) => status.phase === "syncing"));
});

test("hydrates bookmarks and last-read concurrently during startup", async () => {
  const cloud = new TelegramStorageMock();
  seedCloud(cloud, aggregate(30));
  cloud.values.set(`${ROOT}_last_read`, JSON.stringify(lastRead(40)));

  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp({ cloud }),
    hydrateLastRead: true,
  });

  const lastReadCall = cloud.calls.findIndex(
    ([method, key]) => method === "getItem" && key === `${ROOT}_last_read`,
  );
  const aggregateValuesCall = cloud.calls.findIndex(
    ([method]) => method === "getItems",
  );
  assert.ok(lastReadCall >= 0);
  assert.ok(aggregateValuesCall >= 0);
  assert.ok(lastReadCall < aggregateValuesCall);
  assert.equal((await storage.readLastRead()).record_updated_at, 40);
  assert.equal(
    cloud.calls.filter(
      ([method, key]) => method === "getItem" && key === `${ROOT}_last_read`,
    ).length,
    1,
  );
});

test("uses the Telegram cloud record as the canonical tie-breaker", async () => {
  const local = new MemoryStorage();
  const cloud = new TelegramStorageMock();
  setAggregate(local, aggregate(40, {
    bookmarks: [bookmark({ text: "Local" })],
  }));
  seedCloud(cloud, aggregate(40, {
    bookmarks: [bookmark({ text: "Cloud" })],
  }));
  cloud.values.set(
    `${ROOT}_verse_001_0001_0001`,
    JSON.stringify({
      record_updated_at: 39,
      bookmark: bookmark({ book: 1, chapter: 1, verse: 1 }),
    }),
  );

  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, version: "6.9" }),
  });

  assert.equal(JSON.parse(storage.getItem(aggregateKey())).bookmarks[0].text, "Cloud");
  assert.equal(storage.status.source, "cloud");
  await storage.flush();
  assert.equal(cloud.values.has(`${ROOT}_verse_001_0001_0001`), false);
});

test("ignores a corrupt newer cloud commit instead of masking valid local data", async () => {
  const local = new MemoryStorage();
  const cloud = new TelegramStorageMock();
  setAggregate(local, aggregate(40, {
    bookmarks: [bookmark({ text: "Safe local copy" })],
  }));
  seedCloud(cloud, aggregate(90));
  cloud.values.set(
    `${ROOT}_verse_043_0003_0016`,
    JSON.stringify({
      record_updated_at: 90,
      bookmark: bookmark({ text: "" }),
    }),
  );

  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, version: "6.9" }),
  });

  assert.equal(storage.status.source, "local");
  assert.equal(
    JSON.parse(storage.getItem(aggregateKey())).bookmarks[0].text,
    "Safe local copy",
  );
  await storage.flush();
  assert.equal(
    JSON.parse(cloud.values.get(`${ROOT}_verse_043_0003_0016`)).bookmark.text,
    "Safe local copy",
  );
});

test("preserves a future local aggregate for a newer app version", async () => {
  const local = new MemoryStorage();
  const future = JSON.stringify({ version: 4, opaque: "keep me" });
  local.setItem(aggregateKey(), future);

  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ version: "6.8" }),
  });

  assert.equal(storage.getItem(aggregateKey()), future);
  assert.equal(local.getItem(aggregateKey()), future);
  assert.equal(new BookmarkStore({ scope: SCOPE, storage }).persistent, false);
  assert.equal(local.getItem(aggregateKey()), future);
});

test("preserves a future cloud aggregate instead of overwriting it", async () => {
  const local = new MemoryStorage();
  const cloud = new TelegramStorageMock();
  setAggregate(local, aggregate(10));
  cloud.values.set(`${ROOT}_meta`, JSON.stringify({
    version: 4,
    record_updated_at: 90,
  }));

  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, version: "6.9" }),
  });

  assert.equal(storage.status.source, "cloud");
  assert.equal(JSON.parse(storage.getItem(aggregateKey())).version, 4);
  await storage.flush();
  assert.equal(JSON.parse(cloud.values.get(`${ROOT}_meta`)).version, 4);
  assert.equal(new BookmarkStore({ scope: SCOPE, storage }).persistent, false);
});

test("round-trips multi-topic verses through compact cloud topic indexes", async () => {
  const cloud = new TelegramStorageMock();
  const topics = [
    { id: "grace", name: "Grace", color: "#bbf7d0" },
    { id: "biblical-love", name: "Biblical Love", color: "#a16207" },
  ];
  const record = {
    version: 2,
    active_topic_id: "grace",
    topics,
    bookmarks: [{
      ...bookmark(),
      topic_ids: ["grace", "biblical-love"],
      topic_id: "grace",
    }],
    record_updated_at: 61,
  };
  const writer = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp({ cloud, version: "6.9" }),
  });

  writer.setItem(aggregateKey(), JSON.stringify(record));
  await writer.flush();

  const wrapper = JSON.parse(
    cloud.values.get(`${ROOT}_verse_043_0003_0016`),
  );
  assert.deepEqual(wrapper.bookmark.topic_indexes, [0, 1]);
  assert.equal(Object.hasOwn(wrapper.bookmark, "topic_ids"), false);
  assert.ok(
    cloud.values.get(`${ROOT}_verse_043_0003_0016`).length <=
      TELEGRAM_CLOUD_VALUE_MAX_CHARS,
  );

  const reader = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp({ cloud, version: "6.9" }),
  });
  const reopened = JSON.parse(reader.getItem(aggregateKey()));
  assert.equal(reopened.version, 3);
  assert.deepEqual(reopened.bookmarks[0].topic_ids, [
    "grace",
    "biblical-love",
  ]);
  assert.equal(reopened.bookmarks[0].topic_id, "grace");
});

test("rejects malformed version-two topic membership", async () => {
  for (const changes of [
    { topic_ids: [] },
    { topic_ids: ["grace", "grace"] },
    { topic_ids: ["grace"], topic_id: "biblical-love" },
  ]) {
    const local = new MemoryStorage();
    const invalid = aggregate(62, {
      version: 2,
      bookmarks: [bookmark({
        topic_ids: ["grace"],
        ...changes,
      })],
    });
    setAggregate(local, invalid);

    const storage = await TelegramBookmarkStorage.open({
      scope: SCOPE,
      localStorage: local,
      webApp: webApp({ version: "6.8" }),
    });

    assert.equal(storage.getItem(aggregateKey()), null);
    assert.equal(local.getItem(aggregateKey()), null);
  }
});

test("coalesces rapid writes, diffs verse keys, and commits metadata last", async () => {
  const local = new MemoryStorage();
  const cloud = new TelegramStorageMock();
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, version: "6.9" }),
  });

  storage.setItem(aggregateKey(), JSON.stringify(aggregate(1)));
  storage.setItem(aggregateKey(), JSON.stringify(aggregate(2, {
    bookmarks: [bookmark({ text: "Second" })],
  })));
  storage.setItem(aggregateKey(), JSON.stringify(aggregate(3, {
    bookmarks: [
      bookmark({ text: "Final" }),
      bookmark({
        id: "bookmark_2",
        book: 1,
        book_name: "Genesis",
        chapter: 1,
        verse: 1,
        reference: "Genesis 1:1",
        text: "In the beginning.",
      }),
    ],
  })));
  await storage.flush();

  const firstMutation = cloud.calls.filter(([method]) =>
    method === "setItem" || method === "removeItems"
  );
  assert.equal(firstMutation.at(-1)[1], `${ROOT}_meta`);
  assert.equal(
    JSON.parse(cloud.values.get(`${ROOT}_meta`)).record_updated_at,
    3,
  );
  assert.equal(
    [...cloud.values.keys()].filter((key) => key.includes("_verse_")).length,
    2,
  );

  cloud.calls.length = 0;
  storage.setItem(aggregateKey(), JSON.stringify(aggregate(4, {
    bookmarks: [bookmark({ text: "Only verse" })],
  })));
  await storage.flush();
  const finalMutation = cloud.calls.filter(([method]) =>
    method === "setItem" || method === "removeItems"
  );
  assert.equal(finalMutation.at(-1)[1], `${ROOT}_meta`);
  assert.equal(
    [...cloud.values.keys()].filter((key) => key.includes("_verse_")).length,
    1,
  );
});

test("rewrites only a changed bookmark and the last-written commit metadata", async () => {
  const cloud = new TelegramStorageMock();
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp({ cloud, version: "6.9" }),
  });
  const secondVerse = bookmark({
    id: "bookmark_2",
    book: 1,
    book_name: "Genesis",
    chapter: 1,
    verse: 1,
    reference: "Genesis 1:1",
    text: "In the beginning.",
  });
  storage.setItem(aggregateKey(), JSON.stringify(aggregate(1, {
    bookmarks: [bookmark(), secondVerse],
  })));
  await storage.flush();

  const topicKey = `${ROOT}_topic_000`;
  const changedKey = `${ROOT}_verse_043_0003_0016`;
  const unchangedKey = `${ROOT}_verse_001_0001_0001`;
  const topicBefore = cloud.values.get(topicKey);
  const unchangedBefore = cloud.values.get(unchangedKey);
  cloud.calls.length = 0;

  storage.setItem(aggregateKey(), JSON.stringify(aggregate(2, {
    bookmarks: [
      bookmark({ text: "Changed verse", updated_at: 11 }),
      secondVerse,
    ],
  })));
  await storage.flush();

  assert.deepEqual(
    cloud.calls
      .filter(([method]) => method === "setItem")
      .map(([, key]) => key),
    [changedKey, `${ROOT}_meta`],
  );
  assert.equal(cloud.values.get(topicKey), topicBefore);
  assert.equal(cloud.values.get(unchangedKey), unchangedBefore);
  assert.equal(
    Object.hasOwn(JSON.parse(cloud.values.get(changedKey)), "record_updated_at"),
    false,
  );
  assert.match(
    JSON.parse(cloud.values.get(`${ROOT}_meta`)).payload_fingerprint,
    /^fnv2x32-[0-9a-f]{16}$/,
  );
});

test("rejects an item mutation until its fingerprint metadata is committed", async () => {
  const local = new MemoryStorage();
  const cloud = new TelegramStorageMock();
  const writer = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp({ cloud, version: "6.9" }),
  });
  writer.setItem(aggregateKey(), JSON.stringify(aggregate(1)));
  await writer.flush();
  setAggregate(local, aggregate(1));

  const verseKey = `${ROOT}_verse_043_0003_0016`;
  const partialWrapper = JSON.parse(cloud.values.get(verseKey));
  partialWrapper.bookmark.text = "Uncommitted partial value";
  cloud.values.set(verseKey, JSON.stringify(partialWrapper));

  const reader = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, version: "6.9" }),
  });

  assert.equal(reader.status.source, "local");
  assert.equal(
    JSON.parse(reader.getItem(aggregateKey())).bookmarks[0].text,
    "For God so loved the world.",
  );
});

test("automatically retries a transient partial cloud commit", async () => {
  const cloud = new TelegramStorageMock();
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp({ cloud, version: "6.9" }),
  });
  const secondVerse = bookmark({
    id: "bookmark_2",
    book: 1,
    book_name: "Genesis",
    chapter: 1,
    verse: 1,
    reference: "Genesis 1:1",
    text: "In the beginning.",
  });
  storage.setItem(aggregateKey(), JSON.stringify(aggregate(1, {
    bookmarks: [bookmark(), secondVerse],
  })));
  await storage.flush();

  const refusedKey = `${ROOT}_verse_043_0003_0016`;
  const partialSuccessKey = `${ROOT}_verse_001_0001_0001`;
  cloud.calls.length = 0;
  cloud.falseSetKeysOnce.add(refusedKey);
  storage.setItem(aggregateKey(), JSON.stringify(aggregate(2, {
    bookmarks: [
      bookmark({ text: "Retried", updated_at: 11 }),
      { ...secondVerse, text: "Written on first attempt", updated_at: 11 },
    ],
  })));
  await storage.flush();

  const setKeys = cloud.calls
    .filter(([method]) => method === "setItem")
    .map(([, key]) => key);
  assert.equal(setKeys.filter((key) => key === refusedKey).length, 2);
  assert.equal(setKeys.filter((key) => key === partialSuccessKey).length, 1);
  assert.equal(setKeys.at(-1), `${ROOT}_meta`);
  assert.equal(storage.status.cloud, "synced");
  assert.equal(storage.status.lastError, null);

  const reader = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp({ cloud, version: "6.9" }),
  });
  const reopened = JSON.parse(reader.getItem(aggregateKey()));
  assert.equal(reader.status.source, "cloud");
  assert.equal(reopened.record_updated_at, 2);
  assert.deepEqual(
    reopened.bookmarks.map((item) => item.text).sort(),
    ["Retried", "Written on first attempt"].sort(),
  );
});

test("does not advance cloud metadata when Telegram refuses a verse deletion", async () => {
  const cloud = new TelegramStorageMock();
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp({ cloud, version: "6.9" }),
  });
  const secondVerse = bookmark({
    id: "bookmark_2",
    book: 1,
    book_name: "Genesis",
    chapter: 1,
    verse: 1,
    reference: "Genesis 1:1",
    text: "In the beginning.",
  });
  storage.setItem(aggregateKey(), JSON.stringify(aggregate(1, {
    bookmarks: [bookmark(), secondVerse],
  })));
  await storage.flush();

  const removedKey = `${ROOT}_verse_001_0001_0001`;
  cloud.falseMethods.add("removeItems");
  storage.setItem(aggregateKey(), JSON.stringify(aggregate(2)));
  await storage.flush();

  assert.equal(storage.status.cloud, "error");
  assert.match(storage.status.lastError, /did not remove/i);
  assert.equal(cloud.values.has(removedKey), true);
  assert.equal(
    JSON.parse(cloud.values.get(`${ROOT}_meta`)).record_updated_at,
    1,
  );

  cloud.falseMethods.delete("removeItems");
  storage.setItem(aggregateKey(), JSON.stringify(aggregate(2)));
  await storage.flush();
  assert.equal(cloud.values.has(removedKey), false);
  assert.equal(
    JSON.parse(cloud.values.get(`${ROOT}_meta`)).record_updated_at,
    2,
  );
});

test("keeps the device cache when Telegram reports deletion false", async () => {
  const local = new MemoryStorage();
  const device = new TelegramStorageMock();
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ device }),
  });
  storage.setItem(aggregateKey(), JSON.stringify(aggregate(1)));
  await storage.flush();

  device.falseMethods.add("removeItem");
  storage.removeItem(aggregateKey());
  await storage.flush();

  assert.equal(local.getItem(aggregateKey()), null);
  assert.equal(device.values.has(`${ROOT}_cache`), true);
  assert.equal(storage.status.device, "error");
  assert.match(storage.status.lastError, /did not remove/i);
});

test("keeps BookmarkStore synchronous while Telegram replication is async", async () => {
  const local = new MemoryStorage();
  const cloud = new TelegramStorageMock();
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, version: "6.9" }),
  });
  let timestamp = 100;
  const bookmarks = new BookmarkStore({
    scope: SCOPE,
    storage,
    clock: () => timestamp++,
    idFactory: () => "bookmark_created",
  });

  bookmarks.apply({
    translation: "kjv",
    reference: "John 3:16",
    book: 43,
    book_name: "John",
    chapter: 3,
    verse: 16,
    text: "For God so loved the world.",
  }, "grace");

  assert.equal(bookmarks.size, 1);
  assert.equal(JSON.parse(local.getItem(aggregateKey())).bookmarks.length, 1);
  await storage.flush();
  assert.ok(cloud.values.has(`${ROOT}_verse_043_0003_0016`));
});

test("retains the local record when cloud callbacks time out", async () => {
  const local = new MemoryStorage();
  const cloud = new TelegramStorageMock();
  const statuses = [];
  cloud.timeoutMethods.add("getKeys");
  setAggregate(local, aggregate(50));

  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, version: "6.9" }),
    timeoutMs: 5,
    onStatus: (status) => statuses.push(status),
  });

  assert.equal(storage.status.source, "local");
  assert.ok(statuses.some((status) => /timed out/i.test(status.lastError)));
  assert.equal(JSON.parse(storage.getItem(aggregateKey())).record_updated_at, 50);
  await storage.flush();
  assert.equal(JSON.parse(cloud.values.get(`${ROOT}_meta`)).record_updated_at, 50);
});

test("commits the maximum escaped bookmark within one cloud value", async () => {
  const local = new MemoryStorage();
  const cloud = new TelegramStorageMock();
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, version: "6.9" }),
  });
  const topics = Array.from({ length: MAX_BOOKMARK_TOPICS }, (_, index) => ({
    id: `topic_${index}`,
    name: `Topic ${index}`,
    color: BOOKMARK_TOPIC_COLORS[index % BOOKMARK_TOPIC_COLORS.length],
  }));
  const record = aggregate(60, {
    version: 2,
    active_topic_id: topics[0].id,
    topics,
    bookmarks: [bookmark({
      topic_ids: topics.map((topic) => topic.id),
      topic_id: topics[0].id,
      reference: "\\".repeat(180),
      book_name: "\\".repeat(128),
      text: "\\".repeat(1_024),
    })],
  });

  storage.setItem(aggregateKey(), JSON.stringify(record));
  await storage.flush();

  const cloudVerse = cloud.values.get(`${ROOT}_verse_043_0003_0016`);
  assert.equal(JSON.parse(local.getItem(aggregateKey())).bookmarks[0].text.length, 1_024);
  assert.ok(cloudVerse.length <= TELEGRAM_CLOUD_VALUE_MAX_CHARS);
  assert.equal(cloud.values.has(`${ROOT}_meta`), true);
  assert.equal(storage.status.cloud, "synced");
});

test("fits the maximum topics, bookmarks, and last-read record in Telegram storage", async () => {
  const local = new MemoryStorage();
  const cloud = new TelegramStorageMock();
  const device = new TelegramStorageMock();
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, device }),
  });
  const topics = Array.from({ length: MAX_BOOKMARK_TOPICS }, (_, index) => ({
    id: `topic_${index}`,
    name: `Topic ${index}`,
    color: BOOKMARK_TOPIC_COLORS[index % BOOKMARK_TOPIC_COLORS.length],
  }));
  const bookmarks = Array.from({ length: MAX_BOOKMARKS }, (_, index) => ({
    ...bookmark({
      id: `bookmark_${index}`,
      topic_id: topics[index % topics.length].id,
      reference: `Book 1:${index + 1}`,
      book: 1,
      book_name: "Book",
      chapter: 1,
      verse: index + 1,
      text: "\\".repeat(1_024),
    }),
  }));
  const record = aggregate(70, {
    active_topic_id: topics[0].id,
    topics,
    bookmarks,
  });

  storage.setItem(aggregateKey(), JSON.stringify(record));
  await storage.flush();
  await storage.writeLastRead(lastRead(71));

  assert.equal(cloud.values.size, TELEGRAM_BOOKMARK_CLOUD_MAX_KEYS + 1);
  assert.equal(cloud.values.size, 902);
  assert.ok(cloud.values.size < 1_024);
  assert.ok(device.values.get(`${ROOT}_cache`).length < 5 * 1024 * 1024);
  assert.equal(JSON.parse(device.values.get(`${ROOT}_cache`)).bookmarks.length, 800);
});

test("bounds compact recent-topic metadata and clears it without rewriting verses", async () => {
  assert.equal(MAX_RECENT_BOOKMARK_TOPICS, MAX_BOOKMARK_TOPICS);
  const local = new MemoryStorage();
  const cloud = new TelegramStorageMock();
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, version: "6.9" }),
  });
  const topics = Array.from({ length: MAX_BOOKMARK_TOPICS }, (_, index) => ({
    id: `t${String(index).padStart(3, "0")}${"x".repeat(124)}`,
    name: `Topic ${index}`,
    color: BOOKMARK_TOPIC_COLORS[index % BOOKMARK_TOPIC_COLORS.length],
  }));
  const record = aggregate(72, {
    version: 3,
    active_topic_id: topics[0].id,
    recent_topic_ids: topics.map((topic) => topic.id),
    topics,
    bookmarks: [bookmark({
      topic_id: topics[0].id,
      topic_ids: [topics[0].id],
    })],
  });

  storage.setItem(aggregateKey(), JSON.stringify(record));
  await storage.flush();

  const rawMeta = cloud.values.get(`${ROOT}_meta`);
  const meta = JSON.parse(rawMeta);
  assert.ok(rawMeta.length <= TELEGRAM_CLOUD_VALUE_MAX_CHARS);
  assert.equal(meta.recent_topic_indexes.length, MAX_RECENT_BOOKMARK_TOPICS);
  // The active topic id is intentionally present, but MRU entries themselves
  // are represented only by compact indexes.
  assert.equal(rawMeta.includes(topics[1].id), false);
  const persisted = JSON.parse(storage.getItem(aggregateKey()));
  assert.deepEqual(
    persisted.recent_topic_ids,
    topics.slice(0, MAX_RECENT_BOOKMARK_TOPICS).map((topic) => topic.id),
  );

  const reopened = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp({ cloud, version: "6.9" }),
  });
  assert.deepEqual(
    JSON.parse(reopened.getItem(aggregateKey())).recent_topic_ids,
    persisted.recent_topic_ids,
  );

  cloud.calls.length = 0;
  storage.setItem(aggregateKey(), JSON.stringify({
    ...persisted,
    recent_topic_ids: [],
    record_updated_at: 73,
  }));
  await storage.flush();
  assert.deepEqual(
    cloud.calls
      .filter(([method]) => method === "setItem")
      .map(([, key]) => key),
    [`${ROOT}_meta`],
  );
});

test("serializes increasing last-read writes with reversed callback timing", async () => {
  const local = new MemoryStorage();
  const cloud = new ControlledTelegramStorage();
  const localKey = `${TELEGRAM_LAST_READ_STORAGE_PREFIX}:${SCOPE}`;
  const cloudKey = `${ROOT}_last_read`;
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, version: "6.9" }),
  });

  const olderWrite = storage.writeLastRead(lastRead(10, { chapter: 1 }));
  await waitForMicrotask(
    () => cloud.pendingCount("setItem") === 1,
    "The older Telegram write did not start.",
  );
  const newerWrite = storage.writeLastRead(lastRead(20, { chapter: 2 }));

  assert.equal(JSON.parse(local.getItem(localKey)).chapter, 2);
  assert.equal(cloud.pendingCount("setItem"), 1);
  cloud.completeNext("setItem");
  await waitForMicrotask(
    () => cloud.pendingCount("setItem") === 1,
    "The newer Telegram write did not follow the older callback.",
  );
  assert.equal(JSON.parse(cloud.values.get(cloudKey)).chapter, 1);
  cloud.completeNext("setItem");
  await Promise.all([olderWrite, newerWrite]);

  assert.deepEqual(cloud.completions, [
    ["setItem", 10],
    ["setItem", 20],
  ]);
  assert.equal(JSON.parse(cloud.values.get(cloudKey)).chapter, 2);
  assert.equal((await storage.readLastRead()).chapter, 2);
  assert.equal(storage.status.lastRead, "synced");
});

test("a clear queues a durable tombstone behind an in-flight write", async () => {
  const local = new MemoryStorage();
  const cloud = new ControlledTelegramStorage();
  const localKey = `${TELEGRAM_LAST_READ_STORAGE_PREFIX}:${SCOPE}`;
  const cloudKey = `${ROOT}_last_read`;
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, version: "6.9" }),
  });

  const write = storage.writeLastRead(lastRead(30));
  await waitForMicrotask(
    () => cloud.pendingCount("setItem") === 1,
    "The Telegram write did not start.",
  );
  const clear = storage.clearLastRead(40);

  assert.deepEqual(JSON.parse(local.getItem(localKey)), {
    version: 1,
    record_updated_at: 40,
    cleared: true,
  });
  assert.equal(storage.status.lastReadRecordUpdatedAt, 40);
  assert.equal(storage.status.lastReadCleared, true);
  assert.equal(await storage.readLastRead(), null);
  assert.equal(cloud.pendingCount("setItem"), 1);
  cloud.completeNext("setItem");
  await waitForMicrotask(
    () => cloud.pendingCount("setItem") === 1,
    "The Telegram clear did not follow the write callback.",
  );
  assert.equal(JSON.parse(cloud.values.get(cloudKey)).chapter, 3);
  cloud.completeNext("setItem");

  assert.deepEqual(await write, lastRead(30));
  assert.equal(await clear, true);
  assert.equal(JSON.parse(cloud.values.get(cloudKey)).cleared, true);
  assert.equal(await storage.readLastRead(), null);
  assert.equal(await storage.writeLastRead(lastRead(35)), null);
  assert.equal(await storage.writeLastRead(lastRead(40)), null);
  assert.equal(JSON.parse(local.getItem(localKey)).cleared, true);
  assert.equal(storage.status.lastRead, "synced");
});

test("syncs only the compact newest last-read location across devices", async () => {
  const local = new MemoryStorage();
  const cloud = new TelegramStorageMock();
  const device = new TelegramStorageMock();
  const localKey = `${TELEGRAM_LAST_READ_STORAGE_PREFIX}:${SCOPE}`;
  local.setItem(localKey, JSON.stringify(lastRead(10, { chapter: 1 })));
  device.values.set(`${ROOT}_last_read_cache`, JSON.stringify(
    lastRead(20, { chapter: 2 }),
  ));
  cloud.values.set(`${ROOT}_last_read`, JSON.stringify(
    lastRead(30, { chapter: 3 }),
  ));

  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, device }),
  });
  const restored = await storage.readLastRead();

  assert.equal(restored.chapter, 3);
  assert.equal(storage.status.lastReadRecordUpdatedAt, 30);
  assert.equal(storage.status.lastReadCleared, false);
  assert.equal(JSON.parse(local.getItem(localKey)).record_updated_at, 30);
  assert.equal(
    JSON.parse(device.values.get(`${ROOT}_last_read_cache`)).chapter,
    3,
  );

  const saved = await storage.writeLastRead({
    ...lastRead(40, { chapter: 4 }),
    history: [{ chapter: 2 }],
    chapter_text: "must not be stored",
  });
  assert.deepEqual(saved, lastRead(40, { chapter: 4 }));
  assert.deepEqual(
    Object.keys(JSON.parse(cloud.values.get(`${ROOT}_last_read`))),
    ["version", "record_updated_at", "translation", "book", "chapter", "verse"],
  );
  assert.equal(
    (await storage.writeLastRead(lastRead(35, { chapter: 9 }))).chapter,
    4,
  );
  device.falseMethods.add("setItem");
  cloud.falseMethods.add("setItem");
  assert.equal(await storage.clearLastRead(50), false);
  assert.equal(JSON.parse(local.getItem(localKey)).cleared, true);
  assert.equal(JSON.parse(device.values.get(`${ROOT}_last_read_cache`)).chapter, 4);
  assert.equal(JSON.parse(cloud.values.get(`${ROOT}_last_read`)).chapter, 4);
  assert.equal(storage.status.lastRead, "degraded");
  assert.match(storage.status.lastError, /did not store/i);

  device.falseMethods.delete("setItem");
  cloud.falseMethods.delete("setItem");
  const reopened = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp({ cloud, device }),
  });
  assert.equal(await reopened.readLastRead(), null);
  assert.equal(reopened.status.lastReadRecordUpdatedAt, 50);
  assert.equal(reopened.status.lastReadCleared, true);
  assert.equal(JSON.parse(device.values.get(`${ROOT}_last_read_cache`)).cleared, true);
  assert.equal(JSON.parse(cloud.values.get(`${ROOT}_last_read`)).cleared, true);
  assert.ok(TELEGRAM_BOOKMARK_CLOUD_MAX_KEYS + 1 < 1_024);
});

test("rejects malformed last-read records and ignores unsupported Telegram APIs", async () => {
  const cloud = new TelegramStorageMock();
  const device = new TelegramStorageMock();
  const storage = await TelegramBookmarkStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp({ cloud, device, version: "6.8" }),
  });

  assert.equal(storage.status.cloud, "unavailable");
  assert.equal(storage.status.device, "unavailable");
  await assert.rejects(
    storage.writeLastRead(lastRead(10, { book: 0 })),
    /invalid/i,
  );
  assert.equal(cloud.calls.length, 0);
  assert.equal(device.calls.length, 0);
});

function lastRead(recordUpdatedAt, overrides = {}) {
  return {
    version: 1,
    record_updated_at: recordUpdatedAt,
    translation: "kjv",
    book: 43,
    chapter: 3,
    verse: 16,
    ...overrides,
  };
}
