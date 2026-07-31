import assert from "node:assert/strict";
import test from "node:test";

import {
  BrowserPublicCache,
  MemoryPublicStore,
  publicCacheKey,
} from "../lib/public-cache.js";

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
