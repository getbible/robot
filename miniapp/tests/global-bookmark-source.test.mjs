import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { PublicApiError } from "../lib/getbible-transport.js";
import {
  GLOBAL_BOOKMARK_ALL_MAX_BYTES,
  GLOBAL_BOOKMARK_CATALOG_CACHE_KEY,
  GLOBAL_BOOKMARK_CATALOG_MAX_AGE_MS,
  GLOBAL_BOOKMARK_INDEX_MAX_BYTES,
  loadGlobalBookmarkCatalog,
} from "../lib/global-bookmark-source.js";
import { BrowserPublicCache, MemoryPublicStore } from "../lib/public-cache.js";
import {
  BOOKMARKS_API_FIXTURE,
  BOOKMARKS_API_TOPICS,
  bookmarksApiDocuments,
} from "./fixtures/bookmarks-api.mjs";

const HOUR_MS = 60 * 60 * 1_000;

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

/**
 * A Bookmarks API origin answering from static documents, with the same
 * `bookmarks()` contract as the real transport: exact bytes plus their
 * SHA-256. `documents` maps a path to a Buffer, a function returning one, or
 * an Error to throw.
 */
function fakeTransport(documents) {
  const requests = [];
  return {
    requests,
    async bookmarks(relativePath, { maximumBytes } = {}) {
      requests.push({ path: relativePath, maximumBytes });
      let body = documents[relativePath];
      if (typeof body === "function") {
        body = body();
      }
      if (body instanceof Error) {
        throw body;
      }
      if (body === undefined) {
        throw new PublicApiError("not found", { code: "public_api_not_found" });
      }
      const bytes = new Uint8Array(body);
      return { bytes, sha256: sha256(bytes) };
    },
  };
}

function apiDocuments(fixture = BOOKMARKS_API_FIXTURE) {
  return { "index.json": fixture.indexBytes, "all.json": fixture.allBytes };
}

function harness({ documents = apiDocuments(), start = 1_700_000_000_000 } = {}) {
  let now = start;
  const clock = { now: () => now, advance(ms) { now += ms; } };
  const cache = new BrowserPublicCache({
    store: new MemoryPublicStore(),
    now: clock.now,
    maxRecordBytes: 8 * 1024 * 1024,
  });
  const transport = fakeTransport(documents);
  const load = (options = {}) => loadGlobalBookmarkCatalog({
    transport,
    cache,
    now: clock.now,
    ...options,
  });
  return { cache, clock, transport, load };
}

test("downloads and verifies the catalogue once, then serves the cache for a day", async () => {
  const { clock, transport, load } = harness();

  const first = await load();
  assert.equal(first.source, "network");
  assert.equal(first.catalogVersion, BOOKMARKS_API_FIXTURE.index.catalog_version);
  assert.equal(first.checksum, BOOKMARKS_API_FIXTURE.checksum);
  assert.equal(first.checkedAt, clock.now());
  assert.equal(first.catalog.topicDefinition("grace").names.af, "Genade");
  assert.deepEqual(transport.requests, [
    { path: "index.json", maximumBytes: GLOBAL_BOOKMARK_INDEX_MAX_BYTES },
    { path: "all.json", maximumBytes: GLOBAL_BOOKMARK_ALL_MAX_BYTES },
  ]);
  assert.equal(Object.isFrozen(first), true);

  clock.advance(HOUR_MS);
  const second = await load();
  assert.equal(second.source, "cache");
  assert.equal(second.checksum, first.checksum);
  assert.equal(second.checkedAt, first.checkedAt);
  assert.deepEqual(second.catalog.bookmarkIds(), first.catalog.bookmarkIds());
  assert.equal(transport.requests.length, 2);

  clock.advance(GLOBAL_BOOKMARK_CATALOG_MAX_AGE_MS);
  const third = await load();
  assert.equal(third.source, "network");
  assert.equal(third.checkedAt, clock.now());
  assert.deepEqual(
    transport.requests.slice(2).map((request) => request.path),
    ["index.json"],
  );

  clock.advance(HOUR_MS);
  const fourth = await load();
  assert.equal(fourth.source, "cache");
  assert.equal(fourth.checkedAt, third.checkedAt);
  assert.equal(transport.requests.length, 3);
});

test("replaces the cached copy when the index announces a new checksum", async () => {
  const updated = bookmarksApiDocuments({
    topics: [
      ...BOOKMARKS_API_TOPICS,
      {
        id: "steadfast-hope",
        name: "Steadfast Hope",
        color: "#abcdef",
        aliases: [],
        default: true,
        verses: [[45, 5, 5]],
      },
    ],
    catalogVersion: 4,
  });
  const documents = apiDocuments();
  const { clock, transport, load } = harness({ documents });

  const first = await load();
  assert.equal(first.catalog.topicDefinition("steadfast-hope"), null);

  documents["index.json"] = updated.indexBytes;
  documents["all.json"] = updated.allBytes;
  clock.advance(GLOBAL_BOOKMARK_CATALOG_MAX_AGE_MS + 1);
  const second = await load();
  assert.equal(second.source, "network");
  assert.equal(second.catalogVersion, 4);
  assert.equal(second.checksum, updated.checksum);
  assert.equal(second.catalog.topicDefinition("steadfast-hope")?.name, "Steadfast Hope");
  assert.deepEqual(
    transport.requests.slice(2).map((request) => request.path),
    ["index.json", "all.json"],
  );

  clock.advance(HOUR_MS);
  const third = await load();
  assert.equal(third.source, "cache");
  assert.equal(third.catalogVersion, 4);
});

test("rejects a catalogue whose bytes do not match the index checksum", async () => {
  const tampered = Buffer.from(
    BOOKMARKS_API_FIXTURE.allBytes.toString("utf8").replace("Genade", "Genadé"),
    "utf8",
  );
  const { transport, load } = harness({
    documents: { "index.json": BOOKMARKS_API_FIXTURE.indexBytes, "all.json": tampered },
  });

  await assert.rejects(
    load(),
    (error) => error instanceof PublicApiError && error.code === "checksum_mismatch",
  );
  // The consistent-publication retry read the pair twice before giving up.
  assert.deepEqual(
    transport.requests.map((request) => request.path),
    ["index.json", "all.json", "index.json", "all.json"],
  );
});

test("a deployment landing between index and catalogue is retried once", async () => {
  const stale = bookmarksApiDocuments({
    topics: BOOKMARKS_API_TOPICS.slice(1),
    catalogVersion: 2,
  });
  let allReads = 0;
  const { transport, load } = harness({
    documents: {
      "index.json": BOOKMARKS_API_FIXTURE.indexBytes,
      "all.json": () => (allReads++ === 0 ? stale.allBytes : BOOKMARKS_API_FIXTURE.allBytes),
    },
  });

  const result = await load();

  assert.equal(result.source, "network");
  assert.equal(result.checksum, BOOKMARKS_API_FIXTURE.checksum);
  assert.equal(transport.requests.length, 4);
});

test("requireNetwork insists on the network even with a fresh cache", async () => {
  const documents = apiDocuments();
  const { clock, transport, load } = harness({ documents });
  await load();
  clock.advance(HOUR_MS);

  const verified = await load({ requireNetwork: true });
  assert.equal(verified.source, "network");
  assert.equal(verified.checkedAt, clock.now());
  assert.deepEqual(
    transport.requests.slice(2).map((request) => request.path),
    ["index.json"],
  );

  documents["index.json"] = new PublicApiError("offline", {
    code: "public_api_network_error",
    retryable: true,
  });
  clock.advance(HOUR_MS);
  await assert.rejects(
    load({ requireNetwork: true }),
    (error) => error instanceof PublicApiError && error.code === "public_api_network_error",
  );
  const fallback = await load();
  assert.equal(fallback.source, "cache");
  assert.equal(fallback.checkedAt, verified.checkedAt);

  clock.advance(GLOBAL_BOOKMARK_CATALOG_MAX_AGE_MS);
  const stale = await load();
  assert.equal(stale.source, "cache");
  assert.equal(stale.checksum, verified.checksum);
});

test("malformed documents are refused and never replace a verified copy", async () => {
  const fixture = BOOKMARKS_API_FIXTURE;
  const badIndex = Buffer.from(JSON.stringify({ ...fixture.index, schema_version: 2 }));
  const brokenTopics = {
    ...fixture.all,
    topics: fixture.all.topics.map((topic, index) =>
      index === 0 ? { ...topic, color: "#abcde" } : topic
    ),
  };
  const brokenBytes = Buffer.from(JSON.stringify(brokenTopics));
  const brokenIndex = Buffer.from(JSON.stringify({
    ...fixture.index,
    checksum: sha256(brokenBytes),
  }));

  await assert.rejects(
    harness({ documents: { "index.json": badIndex } }).load(),
    (error) => error instanceof PublicApiError && error.code === "invalid_public_response",
  );
  await assert.rejects(
    harness({ documents: { "index.json": Buffer.from("{not json") } }).load(),
    (error) => error instanceof PublicApiError && error.code === "invalid_public_response",
  );
  await assert.rejects(
    harness({ documents: { "index.json": brokenIndex, "all.json": brokenBytes } }).load(),
    TypeError,
  );

  const documents = apiDocuments();
  const { clock, load } = harness({ documents });
  const verified = await load();
  documents["index.json"] = brokenIndex;
  documents["all.json"] = brokenBytes;
  clock.advance(GLOBAL_BOOKMARK_CATALOG_MAX_AGE_MS + 1);
  const kept = await load();
  assert.equal(kept.source, "cache");
  assert.equal(kept.checksum, verified.checksum);
  await assert.rejects(load({ requireNetwork: true }), TypeError);
});

test("a cached copy this client cannot read is discarded and downloaded again", async () => {
  const { cache, transport, load } = harness();
  await cache.put(GLOBAL_BOOKMARK_CATALOG_CACHE_KEY, { schema_version: 9 }, {
    validator: "a".repeat(64),
  });

  const result = await load();

  assert.equal(result.source, "network");
  assert.equal(transport.requests.length, 2);
  const stored = await cache.get(GLOBAL_BOOKMARK_CATALOG_CACHE_KEY);
  assert.equal(stored.validator, BOOKMARKS_API_FIXTURE.checksum);
  assert.equal(stored.value.catalog_version, BOOKMARKS_API_FIXTURE.index.catalog_version);

  // A record whose validator no longer matches its own document is treated
  // the same way rather than trusted.
  await cache.put(GLOBAL_BOOKMARK_CATALOG_CACHE_KEY, stored.value, {
    validator: "b".repeat(64),
  });
  const replaced = await load();
  assert.equal(replaced.source, "network");
  assert.equal(transport.requests.length, 4);
});

test("keeps the catalogue under its own key in the public cache", () => {
  assert.equal(GLOBAL_BOOKMARK_CATALOG_CACHE_KEY, "public:v2:bookmarks:all");
  assert.equal(GLOBAL_BOOKMARK_CATALOG_MAX_AGE_MS, 24 * HOUR_MS);
});

test("validates its dependencies", async () => {
  const { cache, transport } = harness();
  await assert.rejects(loadGlobalBookmarkCatalog({ cache }), TypeError);
  await assert.rejects(loadGlobalBookmarkCatalog({ transport }), TypeError);
  await assert.rejects(
    loadGlobalBookmarkCatalog({ transport, cache, now: 5 }),
    TypeError,
  );
  await assert.rejects(
    loadGlobalBookmarkCatalog({ transport, cache, requireNetwork: "yes" }),
    TypeError,
  );
  await assert.rejects(
    loadGlobalBookmarkCatalog({ transport, cache, maxAgeMs: -1 }),
    RangeError,
  );
  await assert.rejects(
    loadGlobalBookmarkCatalog({ transport, cache, maxAgeMs: 31 * 24 * HOUR_MS }),
    RangeError,
  );
});
