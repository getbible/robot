const CACHE_KEY_PREFIX = "public:v2:";
const DEFAULT_DATABASE_NAME = "getbible-miniapp-public-v2";
const DEFAULT_STORE_NAME = "resources";
const DEFAULT_MAX_ENTRIES = 160;
const DEFAULT_MAX_BYTES = 32 * 1024 * 1024;
const DEFAULT_MAX_RECORD_BYTES = 2 * 1024 * 1024;
// An IndexedDB request that never settles is a real failure mode of a WebView
// whose backing store was left damaged or locked by an earlier session, and
// that state survives closing the Mini App and restarting Telegram. Every
// database operation therefore has an upper bound; past it the cache falls
// back to memory exactly as it does when the database rejects.
const DEFAULT_OPERATION_TIMEOUT_MS = 4_000;

export const PUBLIC_CACHE_OPERATION_TIMEOUT_MS = DEFAULT_OPERATION_TIMEOUT_MS;

/**
 * Minimal asynchronous store used when IndexedDB is unavailable or fails.
 * The store is intentionally limited to public, identity-free API payloads.
 */
export class MemoryPublicStore {
  #records = new Map();

  async get(key) {
    return cloneRecord(this.#records.get(key) ?? null);
  }

  async put(record) {
    this.#records.set(record.key, cloneRecord(record));
  }

  async delete(key) {
    this.#records.delete(key);
  }

  async entries() {
    return [...this.#records.values()].map(cloneRecord);
  }
}

/**
 * IndexedDB adapter isolated behind a small store contract for testability.
 */
export class IndexedDbPublicStore {
  #clearTimeout;
  #databaseName;
  #indexedDB;
  #openPromise = null;
  #setTimeout;
  #storeName;
  #timeoutMs;

  constructor({
    indexedDBImplementation = globalThis.indexedDB,
    databaseName = DEFAULT_DATABASE_NAME,
    storeName = DEFAULT_STORE_NAME,
    timeoutMs = DEFAULT_OPERATION_TIMEOUT_MS,
    setTimeoutImplementation = globalThis.setTimeout,
    clearTimeoutImplementation = globalThis.clearTimeout,
  } = {}) {
    if (!indexedDBImplementation || typeof indexedDBImplementation.open !== "function") {
      throw new TypeError("IndexedDB is unavailable.");
    }
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60_000) {
      throw new RangeError("IndexedDB operation timeout is invalid.");
    }
    if (
      typeof setTimeoutImplementation !== "function" ||
      typeof clearTimeoutImplementation !== "function"
    ) {
      throw new TypeError("Browser timer implementations are required.");
    }
    this.#indexedDB = indexedDBImplementation;
    this.#databaseName = databaseName;
    this.#storeName = storeName;
    this.#timeoutMs = timeoutMs;
    this.#setTimeout = (...args) =>
      Reflect.apply(setTimeoutImplementation, globalThis, args);
    this.#clearTimeout = (...args) =>
      Reflect.apply(clearTimeoutImplementation, globalThis, args);
  }

  async get(key) {
    const database = await this.#open();
    return this.#bounded(readRecord(database, this.#storeName, key), "read");
  }

  async put(record) {
    const database = await this.#open();
    await this.#bounded(putRecord(database, this.#storeName, record), "write");
  }

  async delete(key) {
    const database = await this.#open();
    await this.#bounded(deleteRecord(database, this.#storeName, key), "delete");
  }

  async entries() {
    const database = await this.#open();
    return this.#bounded(allRecords(database, this.#storeName), "scan");
  }

  #open() {
    if (this.#openPromise) {
      return this.#openPromise;
    }
    const opening = new Promise((resolve, reject) => {
      const request = this.#indexedDB.open(this.#databaseName, 1);
      request.addEventListener("upgradeneeded", () => {
        const database = request.result;
        if (!database.objectStoreNames.contains(this.#storeName)) {
          database.createObjectStore(this.#storeName, { keyPath: "key" });
        }
      }, { once: true });
      request.addEventListener("success", () => {
        const database = request.result;
        // Never hold a newer client's upgrade hostage from a background tab.
        database.addEventListener?.("versionchange", () => database.close?.());
        resolve(database);
      }, { once: true });
      request.addEventListener(
        "error",
        () => reject(request.error ?? new Error("IndexedDB could not be opened.")),
        { once: true },
      );
      request.addEventListener(
        "blocked",
        () => reject(new Error("IndexedDB upgrade was blocked.")),
        { once: true },
      );
    });
    this.#openPromise = this.#bounded(opening, "open").catch((error) => {
      this.#openPromise = null;
      throw error;
    });
    return this.#openPromise;
  }

  #bounded(operation, label) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const timer = this.#setTimeout(() => {
        if (!settled) {
          settled = true;
          reject(new Error(`IndexedDB ${label} timed out.`));
        }
      }, this.#timeoutMs);
      operation.then(
        (value) => {
          if (!settled) {
            settled = true;
            this.#clearTimeout(timer);
            resolve(value);
          }
        },
        (error) => {
          if (!settled) {
            settled = true;
            this.#clearTimeout(timer);
            reject(error);
          }
        },
      );
    });
  }
}

async function readRecord(database, storeName, key) {
  const transaction = database.transaction(storeName, "readonly");
  const request = transaction.objectStore(storeName).get(key);
  const result = await requestResult(request);
  await transactionDone(transaction);
  return cloneRecord(result ?? null);
}

async function putRecord(database, storeName, record) {
  const transaction = database.transaction(storeName, "readwrite");
  transaction.objectStore(storeName).put(cloneRecord(record));
  await transactionDone(transaction);
}

async function deleteRecord(database, storeName, key) {
  const transaction = database.transaction(storeName, "readwrite");
  transaction.objectStore(storeName).delete(key);
  await transactionDone(transaction);
}

async function allRecords(database, storeName) {
  const transaction = database.transaction(storeName, "readonly");
  const request = transaction.objectStore(storeName).getAll();
  const result = await requestResult(request);
  await transactionDone(transaction);
  return Array.isArray(result) ? result.map(cloneRecord) : [];
}

/**
 * Bounded persistent cache for public GetBible API data only.
 *
 * Authentication material, Telegram identities, preferences, searches, and
 * baskets cannot be stored because cache keys are restricted to the public
 * namespace and callers can only write JSON-compatible values.
 */
export class BrowserPublicCache {
  #fallback = new MemoryPublicStore();
  #maxBytes;
  #maxEntries;
  #maxRecordBytes;
  #now;
  #store;

  constructor({
    store = null,
    indexedDBImplementation = globalThis.indexedDB,
    databaseName = DEFAULT_DATABASE_NAME,
    maxEntries = DEFAULT_MAX_ENTRIES,
    maxBytes = DEFAULT_MAX_BYTES,
    maxRecordBytes = DEFAULT_MAX_RECORD_BYTES,
    now = Date.now,
  } = {}) {
    if (!Number.isInteger(maxEntries) || maxEntries < 1 || maxEntries > 10_000) {
      throw new RangeError("Cache entry limit is invalid.");
    }
    if (!Number.isInteger(maxBytes) || maxBytes < 1024 || maxBytes > 512 * 1024 * 1024) {
      throw new RangeError("Cache byte limit is invalid.");
    }
    if (
      !Number.isInteger(maxRecordBytes) ||
      maxRecordBytes < 1024 ||
      maxRecordBytes > maxBytes
    ) {
      throw new RangeError("Cache record byte limit is invalid.");
    }
    if (typeof now !== "function") {
      throw new TypeError("A cache clock is required.");
    }
    this.#store = store ?? createIndexedStore(
      indexedDBImplementation,
      databaseName,
    );
    this.#maxEntries = maxEntries;
    this.#maxBytes = maxBytes;
    this.#maxRecordBytes = maxRecordBytes;
    this.#now = now;
  }

  async get(key) {
    assertPublicKey(key);
    const record = await this.#read(key);
    if (!validRecord(record, key)) {
      if (record !== null) {
        await this.delete(key);
      }
      return null;
    }
    record.lastAccessed = this.#now();
    await this.#write(record, { prune: false });
    return {
      value: cloneValue(record.value),
      validator: record.validator,
      checkedAt: record.checkedAt,
    };
  }

  async put(key, value, {
    validator = null,
    checkedAt = this.#now(),
  } = {}) {
    assertPublicKey(key);
    if (!Number.isFinite(checkedAt) || checkedAt < 0) {
      throw new TypeError("Cache validation time is invalid.");
    }
    const cloned = cloneValue(value);
    const serialized = JSON.stringify(cloned);
    if (serialized === undefined) {
      throw new TypeError("Cache value must be JSON-compatible.");
    }
    const size = utf8Size(serialized);
    if (size > this.#maxRecordBytes) {
      return false;
    }
    const record = {
      key,
      value: cloned,
      validator: normalizeValidator(validator),
      checkedAt,
      lastAccessed: this.#now(),
      size,
    };
    await this.#write(record, { prune: true });
    return true;
  }

  async markChecked(key, validator, checkedAt = this.#now()) {
    assertPublicKey(key);
    const record = await this.#read(key);
    if (!validRecord(record, key)) {
      return false;
    }
    record.validator = normalizeValidator(validator);
    record.checkedAt = checkedAt;
    record.lastAccessed = this.#now();
    await this.#write(record, { prune: false });
    return true;
  }

  async delete(key) {
    assertPublicKey(key);
    await this.#deleteFrom(this.#store, key);
    await this.#fallback.delete(key);
  }

  async invalidatePrefix(prefix) {
    assertPublicKey(prefix);
    const records = await this.#entries();
    await Promise.all(
      records
        .filter((record) => record?.key?.startsWith(prefix))
        .map((record) => this.delete(record.key)),
    );
  }

  async #read(key) {
    if (this.#store) {
      try {
        return await this.#store.get(key);
      } catch {
        this.#store = null;
      }
    }
    return this.#fallback.get(key);
  }

  async #write(record, { prune }) {
    if (this.#store) {
      try {
        await this.#store.put(record);
        if (prune) {
          await this.#prune(this.#store);
        }
        return;
      } catch {
        this.#store = null;
      }
    }
    await this.#fallback.put(record);
    if (prune) {
      await this.#prune(this.#fallback);
    }
  }

  async #deleteFrom(store, key) {
    if (!store) {
      return;
    }
    try {
      await store.delete(key);
    } catch {
      if (store === this.#store) {
        this.#store = null;
      }
    }
  }

  async #entries() {
    if (this.#store) {
      try {
        return await this.#store.entries();
      } catch {
        this.#store = null;
      }
    }
    return this.#fallback.entries();
  }

  async #prune(store) {
    const records = (await store.entries())
      .filter((record) => validRecord(record, record?.key))
      .sort((left, right) => left.lastAccessed - right.lastAccessed);
    let bytes = records.reduce((total, record) => total + record.size, 0);
    while (
      records.length > this.#maxEntries ||
      bytes > this.#maxBytes
    ) {
      const oldest = records.shift();
      if (!oldest) {
        break;
      }
      bytes -= oldest.size;
      await store.delete(oldest.key);
    }
  }
}

export function publicCacheKey(...parts) {
  const normalized = parts.map((part) => String(part).trim().toLowerCase());
  if (normalized.some((part) => !part || part.includes(":"))) {
    throw new TypeError("Public cache key part is invalid.");
  }
  return `${CACHE_KEY_PREFIX}${normalized.join(":")}`;
}

function createIndexedStore(indexedDBImplementation, databaseName) {
  if (!indexedDBImplementation) {
    return null;
  }
  try {
    return new IndexedDbPublicStore({
      indexedDBImplementation,
      databaseName,
    });
  } catch {
    return null;
  }
}

function assertPublicKey(key) {
  if (
    typeof key !== "string" ||
    !key.startsWith(CACHE_KEY_PREFIX) ||
    key.length > 256
  ) {
    throw new TypeError("Only bounded public GetBible cache keys are allowed.");
  }
}

function validRecord(record, key) {
  return Boolean(
    record &&
      typeof record === "object" &&
      record.key === key &&
      Number.isFinite(record.checkedAt) &&
      Number.isFinite(record.lastAccessed) &&
      Number.isInteger(record.size) &&
      record.size >= 0 &&
      (record.validator === null || typeof record.validator === "string") &&
      record.value !== undefined,
  );
}

function normalizeValidator(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  if (typeof value !== "string" || value.length > 256) {
    throw new TypeError("Cache validator is invalid.");
  }
  return value;
}

function cloneRecord(record) {
  return record === null ? null : cloneValue(record);
}

function cloneValue(value) {
  if (typeof structuredClone === "function") {
    return structuredClone(value);
  }
  return JSON.parse(JSON.stringify(value));
}

function utf8Size(value) {
  if (typeof TextEncoder === "function") {
    return new TextEncoder().encode(value).byteLength;
  }
  return unescape(encodeURIComponent(value)).length;
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.addEventListener("success", () => resolve(request.result), { once: true });
    request.addEventListener(
      "error",
      () => reject(request.error ?? new Error("IndexedDB request failed.")),
      { once: true },
    );
  });
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.addEventListener("complete", resolve, { once: true });
    transaction.addEventListener(
      "abort",
      () => reject(transaction.error ?? new Error("IndexedDB transaction aborted.")),
      { once: true },
    );
    transaction.addEventListener(
      "error",
      () => reject(transaction.error ?? new Error("IndexedDB transaction failed.")),
      { once: true },
    );
  });
}
