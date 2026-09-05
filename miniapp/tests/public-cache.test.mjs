import assert from "node:assert/strict";
import test from "node:test";

import {
  BrowserPublicCache,
  IndexedDbPublicStore,
  MemoryPublicStore,
  publicCacheKey,
} from "../lib/public-cache.js";

function stallingIndexedDB() {
  return { open: () => ({ addEventListener() {} }) };
}

function settlingIndexedDB(event, database = null) {
  return {
    open() {
      const listeners = new Map();
      const request = {
        result: database,
        error: null,
        addEventListener(name, handler) {
          listeners.set(name, handler);
        },
      };
      queueMicrotask(() => listeners.get(event)?.());
      return request;
    },
  };
}

class StallingDatabase {
  closed = false;
  listeners = new Map();
  objectStoreNames = { contains: () => true };

  addEventListener(name, handler) {
    this.listeners.set(name, handler);
  }

  close() {
    this.closed = true;
  }

  transaction() {
    const request = { addEventListener() {} };
    return {
      addEventListener() {},
      objectStore: () => ({
        get: () => request,
        getAll: () => request,
        put() {},
        delete() {},
      }),
    };
  }
}

test("an IndexedDB that never opens fails within its bound and the cache falls back to memory", async () => {
  const store = new IndexedDbPublicStore({
    indexedDBImplementation: stallingIndexedDB(),
    timeoutMs: 20,
  });
  const key = publicCacheKey("translations");
  await assert.rejects(store.get(key), /IndexedDB open timed out/);

  const cache = new BrowserPublicCache({ store });
  assert.equal(await cache.get(key), null);
  assert.equal(await cache.put(key, { ok: true }), true);
  assert.deepEqual((await cache.get(key)).value, { ok: true });
});

test("IndexedDB requests that never settle fail within their bound", async () => {
  const database = new StallingDatabase();
  const store = new IndexedDbPublicStore({
    indexedDBImplementation: settlingIndexedDB("success", database),
    timeoutMs: 20,
  });
  const key = publicCacheKey("translations");
  await assert.rejects(store.get(key), /IndexedDB read timed out/);
  await assert.rejects(store.entries(), /IndexedDB scan timed out/);
  await assert.rejects(store.put({ key, value: 1 }), /IndexedDB write timed out/);
  await assert.rejects(store.delete(key), /IndexedDB delete timed out/);

  // A newer client's upgrade is never held hostage by this connection.
  database.listeners.get("versionchange")();
  assert.equal(database.closed, true);
});

test("a blocked IndexedDB open rejects instead of waiting", async () => {
  const store = new IndexedDbPublicStore({
    indexedDBImplementation: settlingIndexedDB("blocked"),
    timeoutMs: 1_000,
  });
  await assert.rejects(store.get(publicCacheKey("translations")), /blocked/);
});

test("the IndexedDB store validates its operation bound", () => {
  for (const timeoutMs of [0, 60_001, 1.5, "4000"]) {
    assert.throws(
      () => new IndexedDbPublicStore({
        indexedDBImplementation: stallingIndexedDB(),
        timeoutMs,
      }),
      RangeError,
    );
  }
});

test("public cache persists identity-free data and returns defensive copies", async () => {
  let now = 100;
  const cache = new BrowserPublicCache({
    store: new MemoryPublicStore(),
    now: () => now,
  });
  const key = publicCacheKey("chapter", "kjv", 43, 3);
  const source = { items: [{ verse: 16, text: "Love" }] };

  assert.equal(await cache.put(key, source, { validator: "a".repeat(40) }), true);
  source.items[0].text = "mutated";
  now = 101;
  const record = await cache.get(key);

  assert.equal(record.value.items[0].text, "Love");
  record.value.items[0].text = "changed again";
  assert.equal((await cache.get(key)).value.items[0].text, "Love");
  assert.equal(record.validator, "a".repeat(40));
  assert.equal(record.checkedAt, 100);
});

test("public cache enforces bounded LRU storage", async () => {
  let now = 1;
  const store = new MemoryPublicStore();
  const cache = new BrowserPublicCache({
    store,
    maxEntries: 2,
    maxBytes: 10_000,
    maxRecordBytes: 5_000,
    now: () => now,
  });
  const first = publicCacheKey("chapter", "kjv", 1, 1);
  const second = publicCacheKey("chapter", "kjv", 1, 2);
  const third = publicCacheKey("chapter", "kjv", 1, 3);

  await cache.put(first, { value: 1 });
  now += 1;
  await cache.put(second, { value: 2 });
  now += 1;
  await cache.get(first);
  now += 1;
  await cache.put(third, { value: 3 });

  assert.equal(await cache.get(second), null);
  assert.equal((await cache.get(first)).value.value, 1);
  assert.equal((await cache.get(third)).value.value, 3);
});

test("public cache invalidates one translation scope without touching another", async () => {
  const cache = new BrowserPublicCache({ store: new MemoryPublicStore() });
  const kjv = publicCacheKey("chapter", "kjv", 43, 3);
  const aov = publicCacheKey("chapter", "aov", 43, 3);
  await cache.put(kjv, { translation: "kjv" });
  await cache.put(aov, { translation: "aov" });

  await cache.invalidatePrefix(publicCacheKey("chapter", "kjv"));

  assert.equal(await cache.get(kjv), null);
  assert.equal((await cache.get(aov)).value.translation, "aov");
});

test("public cache rejects keys outside the public namespace", async () => {
  const cache = new BrowserPublicCache({ store: new MemoryPublicStore() });
  await assert.rejects(
    cache.put("telegram:user:42", { token: "secret" }),
    /Only bounded public GetBible cache keys/,
  );
  assert.throws(() => publicCacheKey("chapter:secret"), /key part is invalid/);
});
