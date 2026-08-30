import assert from "node:assert/strict";
import test from "node:test";

import {
  LIVE_GLOBAL_BOOKMARK_CATALOG_MAX_BYTES,
  LIVE_GLOBAL_BOOKMARK_CATALOG_STORAGE_PREFIX,
  loadLiveGlobalBookmarkCatalog,
} from "../lib/global-bookmark-live-catalog.js";
import { GLOBAL_BOOKMARK_CATALOG } from "../lib/global-bookmark-catalog.js";

const SCOPE = "d".repeat(64);
const CHECKSUM = "a".repeat(64);
const INSTANCE_SCOPE = "1".repeat(16);

class MemoryStorage {
  values = new Map();
  removed = [];
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) {
    this.removed.push(key);
    this.values.delete(key);
  }
}

function envelope(overrides = {}) {
  return {
    revision: 2,
    checksum: CHECKSUM,
    etag: '"catalog-2"',
    catalog: {
      schema_version: 1,
      topics: [],
      associations: { add: [], remove: [] },
    },
    ...overrides,
  };
}

test("uses a valid live catalogue immediately and revalidates its cache by ETag", async () => {
  const storage = new MemoryStorage();
  const first = await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog(etag) {
      assert.equal(etag, null);
      return envelope();
    } },
  });
  assert.equal(first.source, "network");
  assert.equal(first.revision, 2);
  assert.equal(first.catalog.version, GLOBAL_BOOKMARK_CATALOG.version + 2);

  const key = `${LIVE_GLOBAL_BOOKMARK_CATALOG_STORAGE_PREFIX}:${INSTANCE_SCOPE}:${SCOPE}`;
  const raw = storage.getItem(key);
  assert.ok(new TextEncoder().encode(raw).byteLength <=
    LIVE_GLOBAL_BOOKMARK_CATALOG_MAX_BYTES);

  const second = await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog(etag) {
      assert.equal(etag, '"catalog-2"');
      return { not_modified: true, etag };
    } },
  });
  assert.equal(second.source, "network");
  assert.equal(second.checksum, CHECKSUM);
});

test("malformed, unavailable, and cacheless 304 responses fall back safely", async () => {
  for (const response of [
    Promise.reject(new Error("offline")),
    Promise.resolve({ ...envelope(), checksum: "not-a-checksum" }),
    Promise.resolve({ ...envelope(), private_user_id: 42 }),
    Promise.resolve({ not_modified: true, etag: '"missing"' }),
  ]) {
    const result = await loadLiveGlobalBookmarkCatalog({
      scope: SCOPE,
      instanceScope: INSTANCE_SCOPE,
      storage: new MemoryStorage(),
      api: { bookmarkCatalog: () => response },
    });
    assert.equal(result.source, "bundled");
    assert.equal(result.catalog, GLOBAL_BOOKMARK_CATALOG);
  }
});

test("an oversized or corrupt cached envelope is discarded before fallback", async () => {
  const storage = new MemoryStorage();
  const key = `${LIVE_GLOBAL_BOOKMARK_CATALOG_STORAGE_PREFIX}:${INSTANCE_SCOPE}:${SCOPE}`;
  storage.values.set(key, "x".repeat(LIVE_GLOBAL_BOOKMARK_CATALOG_MAX_BYTES + 1));

  const oversized = await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog() { throw new Error("offline"); } },
  });
  assert.equal(oversized.source, "bundled");
  assert.equal(storage.values.has(key), false);
  assert.deepEqual(storage.removed, [key]);

  storage.values.set(key, JSON.stringify({ version: 1, private_user_id: 42 }));
  const corrupt = await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog() { throw new Error("offline"); } },
  });
  assert.equal(corrupt.source, "bundled");
  assert.equal(storage.values.has(key), false);
});

test("an invalid refresh preserves the last valid cached catalogue", async () => {
  const storage = new MemoryStorage();
  await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog() { return envelope(); } },
  });
  const result = await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog() {
      return { ...envelope(), catalog: { schema_version: 1 } };
    } },
  });
  assert.equal(result.source, "cache");
  assert.equal(result.checksum, CHECKSUM);
});

test("strict refresh requires a valid network response while preserving cache", async () => {
  const storage = new MemoryStorage();
  await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog() { return envelope(); } },
  });

  await assert.rejects(
    loadLiveGlobalBookmarkCatalog({
      scope: SCOPE,
      instanceScope: INSTANCE_SCOPE,
      storage,
      requireNetwork: true,
      api: { async bookmarkCatalog(etag) {
        assert.equal(etag, '"catalog-2"');
        throw new Error("offline");
      } },
    }),
    /offline/,
  );

  const validated = await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    requireNetwork: true,
    api: { async bookmarkCatalog(etag) {
      assert.equal(etag, '"catalog-2"');
      return { not_modified: true, etag };
    } },
  });
  assert.equal(validated.source, "network");
  assert.equal(validated.checksum, CHECKSUM);
});

test("distinguishes a validated 304 from an error fallback to the same cache", async () => {
  const storage = new MemoryStorage();
  await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog() { return envelope(); } },
  });

  const validated = await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog(etag) {
      return { not_modified: true, etag };
    } },
  });
  const fallback = await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog() { throw new Error("offline"); } },
  });

  assert.equal(validated.source, "network");
  assert.equal(fallback.source, "cache");
  assert.equal(validated.checksum, fallback.checksum);
});

test("strict refresh rejects a cacheless not-modified response", async () => {
  await assert.rejects(
    loadLiveGlobalBookmarkCatalog({
      scope: SCOPE,
      instanceScope: INSTANCE_SCOPE,
      storage: new MemoryStorage(),
      requireNetwork: true,
      api: { async bookmarkCatalog(etag) {
        assert.equal(etag, null);
        return { not_modified: true, etag: '"missing"' };
      } },
    }),
    /cannot be not-modified/,
  );
});

test("an authenticated lower revision replaces cache after a database restore", async () => {
  const storage = new MemoryStorage();
  await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog() {
      return envelope({ revision: 5, etag: '"catalog-5"' });
    } },
  });
  const key = `${LIVE_GLOBAL_BOOKMARK_CATALOG_STORAGE_PREFIX}:${INSTANCE_SCOPE}:${SCOPE}`;
  const result = await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog(etag) {
      assert.equal(etag, '"catalog-5"');
      return envelope({
        revision: 4,
        checksum: "b".repeat(64),
        etag: '"catalog-4-restored"',
      });
    } },
  });

  assert.equal(result.source, "network");
  assert.equal(result.revision, 4);
  assert.equal(result.checksum, "b".repeat(64));
  assert.equal(JSON.parse(storage.getItem(key)).revision, 4);
});

test("keeps server catalog caches isolated by Mini App instance", async () => {
  const storage = new MemoryStorage();
  const firstInstance = "2".repeat(16);
  const secondInstance = "3".repeat(16);
  await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: firstInstance,
    storage,
    api: { async bookmarkCatalog() { return envelope({ revision: 9 }); } },
  });
  const second = await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: secondInstance,
    storage,
    api: { async bookmarkCatalog(etag) {
      assert.equal(etag, null);
      return envelope({ revision: 1, checksum: "b".repeat(64) });
    } },
  });
  assert.equal(second.source, "network");
  assert.equal(second.revision, 1);
  assert.equal(storage.values.size, 2);
});

test("same-revision changed content replaces a divergent restored cache", async () => {
  const storage = new MemoryStorage();
  await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog() {
      return envelope({ revision: 5, etag: '"catalog-5"' });
    } },
  });
  const key = `${LIVE_GLOBAL_BOOKMARK_CATALOG_STORAGE_PREFIX}:${INSTANCE_SCOPE}:${SCOPE}`;
  const changedCatalog = {
    schema_version: 1,
    topics: [{
      id: "restored-topic",
      name: "Restored Topic",
      color: "#abcdef",
      aliases: [],
    }],
    associations: {
      add: [{ topic_id: "restored-topic", book: 1, chapter: 1, verse: 1 }],
      remove: [],
    },
  };
  const result = await loadLiveGlobalBookmarkCatalog({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api: { async bookmarkCatalog(etag) {
      assert.equal(etag, '"catalog-5"');
      return envelope({
        revision: 5,
        checksum: "c".repeat(64),
        etag: '"catalog-5-restored"',
        catalog: changedCatalog,
      });
    } },
  });

  assert.equal(result.source, "network");
  assert.equal(result.revision, 5);
  assert.equal(result.checksum, "c".repeat(64));
  assert.equal(result.catalog.topicDefinition("restored-topic").name, "Restored Topic");
  const cached = JSON.parse(storage.getItem(key));
  assert.equal(cached.checksum, "c".repeat(64));
  assert.deepEqual(cached.catalog, changedCatalog);
});
