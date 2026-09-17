import {
  GlobalBookmarkCatalog,
  globalBookmarkCatalogDocumentFromApi,
} from "./global-bookmark-catalog.js";
import { PublicApiError } from "./getbible-transport.js";
import { publicCacheKey } from "./public-cache.js";

const SUPPORTED_SCHEMA_VERSION = 1;
const CHECKSUM_PATTERN = /^[a-f0-9]{64}$/;
// The catalogue is revalidated against the API's index at most once a day
// unless a caller asks for a network-verified copy explicitly. The index is
// small and tells whether the complete collection changed at all.
const DEFAULT_MAX_AGE_MS = 24 * 60 * 60 * 1_000;
const MAX_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1_000;
const INDEX_MAX_BYTES = 64 * 1024;
const ALL_MAX_BYTES = 8 * 1024 * 1024;
// index.json and all.json are published together. A deployment landing
// between the two reads leaves a checksum that does not match; one more pass
// picks up the consistent pair before giving up.
const CONSISTENCY_ATTEMPTS = 2;
// A distinguishing first part keeps the catalogue out of the Bible API's key
// space in the shared public cache; the second names the API document.
const CACHE_KEY = publicCacheKey("bookmarks", "all");

/**
 * Loads the global bookmark catalogue from the public Bookmarks API,
 * cache first.
 *
 * A verified copy lives in the shared public cache and is served without a
 * request while it is younger than `maxAgeMs`. Otherwise the API's index is
 * read: an unchanged checksum only refreshes the copy's validation time, a
 * changed one downloads `all.json`, verifies its SHA-256 against the index and
 * replaces the copy. On any failure the cached copy is returned when it exists,
 * unless `requireNetwork` insists on a network-verified catalogue. Nothing is
 * bundled with the application: when no copy exists and the network fails,
 * the error surfaces and the caller treats "no catalogue yet" as a state.
 */
export async function loadGlobalBookmarkCatalog({
  transport,
  cache,
  now = Date.now,
  requireNetwork = false,
  maxAgeMs = DEFAULT_MAX_AGE_MS,
} = {}) {
  if (!transport || typeof transport.bookmarks !== "function") {
    throw new TypeError("A Bookmarks API transport is required.");
  }
  if (
    !cache ||
    typeof cache.get !== "function" ||
    typeof cache.put !== "function" ||
    typeof cache.markChecked !== "function" ||
    typeof cache.delete !== "function"
  ) {
    throw new TypeError("A public data cache is required.");
  }
  if (typeof now !== "function") {
    throw new TypeError("A catalogue clock is required.");
  }
  if (typeof requireNetwork !== "boolean") {
    throw new TypeError("The network requirement is invalid.");
  }
  if (!Number.isInteger(maxAgeMs) || maxAgeMs < 0 || maxAgeMs > MAX_MAX_AGE_MS) {
    throw new RangeError("Catalogue revalidation interval is invalid.");
  }

  const cached = await readCachedCatalog(cache);
  if (cached && !requireNetwork && now() - cached.checkedAt < maxAgeMs) {
    return result(cached, "cache");
  }
  try {
    return result(await revalidate({ transport, cache, now, cached }), "network");
  } catch (error) {
    if (requireNetwork || !cached) {
      throw error;
    }
    return result(cached, "cache");
  }
}

export const GLOBAL_BOOKMARK_CATALOG_CACHE_KEY = CACHE_KEY;
export const GLOBAL_BOOKMARK_CATALOG_MAX_AGE_MS = DEFAULT_MAX_AGE_MS;
export const GLOBAL_BOOKMARK_INDEX_MAX_BYTES = INDEX_MAX_BYTES;
export const GLOBAL_BOOKMARK_ALL_MAX_BYTES = ALL_MAX_BYTES;

async function revalidate({ transport, cache, now, cached }) {
  let lastIndex = null;
  let lastActual = null;
  for (let attempt = 0; attempt < CONSISTENCY_ATTEMPTS; attempt += 1) {
    const index = normalizeIndex(
      decodeJson((await transport.bookmarks("index.json", {
        maximumBytes: INDEX_MAX_BYTES,
      })).bytes),
    );
    if (cached && cached.checksum === index.checksum) {
      const checkedAt = now();
      await cache.markChecked(CACHE_KEY, index.checksum, checkedAt);
      return { ...cached, checkedAt };
    }
    const { bytes, sha256 } = await transport.bookmarks("all.json", {
      maximumBytes: ALL_MAX_BYTES,
    });
    lastIndex = index.checksum;
    lastActual = sha256;
    if (sha256 !== index.checksum) {
      continue;
    }
    const document = globalBookmarkCatalogDocumentFromApi(decodeJson(bytes), index);
    const catalog = new GlobalBookmarkCatalog(document);
    const checkedAt = now();
    await cache.put(CACHE_KEY, document, {
      validator: index.checksum,
      checkedAt,
    });
    return {
      catalog,
      catalogVersion: catalog.version,
      checksum: index.checksum,
      checkedAt,
    };
  }
  throw new PublicApiError(
    `The Bookmarks API catalogue did not match its index (${lastIndex} != ${lastActual}).`,
    { code: "checksum_mismatch", retryable: true },
  );
}

async function readCachedCatalog(cache) {
  let record;
  try {
    record = await cache.get(CACHE_KEY);
  } catch {
    return null;
  }
  if (!record) {
    return null;
  }
  try {
    const catalog = new GlobalBookmarkCatalog(record.value);
    if (
      catalog.checksum === null ||
      record.validator !== catalog.checksum ||
      !Number.isFinite(record.checkedAt) ||
      record.checkedAt < 0
    ) {
      throw new TypeError("The cached bookmark catalogue is invalid.");
    }
    return {
      catalog,
      catalogVersion: catalog.version,
      checksum: catalog.checksum,
      checkedAt: record.checkedAt,
    };
  } catch {
    // A copy this client cannot read any more is only a stale artefact of an
    // earlier version; the network replaces it on the next pass.
    await cache.delete(CACHE_KEY).catch(() => undefined);
    return null;
  }
}

function normalizeIndex(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    value.schema_version !== SUPPORTED_SCHEMA_VERSION ||
    !Number.isSafeInteger(value.catalog_version) ||
    value.catalog_version < 1 ||
    typeof value.checksum !== "string" ||
    !CHECKSUM_PATTERN.test(value.checksum)
  ) {
    throw new PublicApiError("The Bookmarks API index is invalid.", {
      code: "invalid_public_response",
    });
  }
  return {
    schema_version: SUPPORTED_SCHEMA_VERSION,
    catalog_version: value.catalog_version,
    checksum: value.checksum,
  };
}

function decodeJson(bytes) {
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new PublicApiError("The Bookmarks API returned malformed JSON.", {
      code: "invalid_public_response",
    });
  }
}

function result(value, source) {
  return Object.freeze({
    catalog: value.catalog,
    catalogVersion: value.catalogVersion,
    checksum: value.checksum,
    checkedAt: value.checkedAt,
    source,
  });
}
