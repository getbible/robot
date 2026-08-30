import {
  GLOBAL_BOOKMARK_CATALOG,
  globalBookmarkCatalogWithOverlay,
} from "./global-bookmark-catalog.js";
import { isMiniAppInstanceScope } from "./instance-scope.js";

const STORAGE_PREFIX = "getbible.miniapp.global-catalog.v1";
const STORAGE_VERSION = 1;
const SCOPE_PATTERN = /^[a-f0-9]{64}$/;
const CHECKSUM_PATTERN = /^[a-f0-9]{64}$/;
const MAX_CACHE_BYTES = 2 * 1024 * 1024;

/** Loads a reviewed live overlay without making the Mini App depend on it. */
export async function loadLiveGlobalBookmarkCatalog({
  api,
  scope,
  instanceScope,
  storage = browserLocalStorage(),
  requireNetwork = false,
} = {}) {
  if (!api || typeof api.bookmarkCatalog !== "function") {
    throw new TypeError("A bookmark catalogue API client is required.");
  }
  if (
    typeof scope !== "string" ||
    !SCOPE_PATTERN.test(scope) ||
    !isMiniAppInstanceScope(instanceScope) ||
    typeof requireNetwork !== "boolean"
  ) {
    throw new TypeError("An authenticated bookmark catalogue scope is required.");
  }
  const persistent = storageLike(storage) ? storage : null;
  const key = `${STORAGE_PREFIX}:${instanceScope}:${scope}`;
  const cached = readCachedCatalog(persistent, key);

  try {
    const response = await api.bookmarkCatalog(cached?.etag ?? null);
    if (response?.not_modified === true) {
      if (cached) {
        // The body still comes from the validated cache, but the server has
        // authoritatively confirmed that its ETag is current. Callers must be
        // able to distinguish this successful network round trip from the
        // catch-path cache fallback after a request or validation failure.
        return Object.freeze({ ...cached, source: "network" });
      }
      throw new TypeError(
        "The bookmark catalogue cannot be not-modified without a cached copy.",
      );
    }
    const accepted = acceptEnvelope(response, "network");
    // A validated authenticated 200 describes the server's current durable
    // state. It remains authoritative after an operator restores an older or
    // divergent database backup; a normal unchanged response takes the 304
    // path above without rewriting the cache.
    writeCachedCatalog(persistent, key, response);
    return accepted;
  } catch (error) {
    if (requireNetwork) {
      throw error;
    }
    return cached ?? bundledResult();
  }
}

export const LIVE_GLOBAL_BOOKMARK_CATALOG_STORAGE_PREFIX = STORAGE_PREFIX;
export const LIVE_GLOBAL_BOOKMARK_CATALOG_MAX_BYTES = MAX_CACHE_BYTES;

function readCachedCatalog(storage, key) {
  if (!storage) {
    return null;
  }
  try {
    const raw = storage.getItem(key);
    if (!raw || utf8Length(raw) > MAX_CACHE_BYTES) {
      if (raw) storage.removeItem(key);
      return null;
    }
    const value = JSON.parse(raw);
    if (!value || value.version !== STORAGE_VERSION) {
      throw new TypeError("The cached bookmark catalogue is invalid.");
    }
    return acceptEnvelope(value, "cache");
  } catch {
    try {
      storage.removeItem(key);
    } catch {
      // The bundled catalogue remains available when storage is unavailable.
    }
    return null;
  }
}

function writeCachedCatalog(storage, key, response) {
  if (!storage) {
    return false;
  }
  const value = {
    version: STORAGE_VERSION,
    revision: response.revision,
    checksum: response.checksum,
    etag: validEtag(response.etag) ? response.etag : null,
    catalog: response.catalog,
  };
  const raw = JSON.stringify(value);
  if (utf8Length(raw) > MAX_CACHE_BYTES) {
    return false;
  }
  try {
    storage.setItem(key, raw);
    return true;
  } catch {
    return false;
  }
}

function acceptEnvelope(value, source) {
  const expectedKeys = source === "cache"
    ? ["version", "revision", "checksum", "etag", "catalog"]
    : ["revision", "checksum", "etag", "catalog"];
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !hasExactKeys(value, expectedKeys) ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 0 ||
    typeof value.checksum !== "string" ||
    !CHECKSUM_PATTERN.test(value.checksum) ||
    (value.etag !== null && value.etag !== undefined && !validEtag(value.etag))
  ) {
    throw new TypeError("The bookmark catalogue response is invalid.");
  }
  return Object.freeze({
    catalog: globalBookmarkCatalogWithOverlay(value.catalog, value.revision),
    revision: value.revision,
    checksum: value.checksum,
    etag: value.etag ?? null,
    source,
  });
}

function hasExactKeys(value, expected) {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length &&
    actual.every((key, index) => key === wanted[index]);
}

function bundledResult() {
  return Object.freeze({
    catalog: GLOBAL_BOOKMARK_CATALOG,
    revision: 0,
    checksum: null,
    etag: null,
    source: "bundled",
  });
}

function validEtag(value) {
  return typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 160 &&
    !/[\u0000-\u001f\u007f]/.test(value);
}

function utf8Length(value) {
  return new TextEncoder().encode(value).byteLength;
}

function storageLike(value) {
  return Boolean(
    value &&
    typeof value.getItem === "function" &&
    typeof value.setItem === "function" &&
    typeof value.removeItem === "function",
  );
}

function browserLocalStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
