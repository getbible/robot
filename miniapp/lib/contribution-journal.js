const DATABASE_NAME = "getbible-miniapp-contributions";
const DATABASE_VERSION = 1;
const STORE_NAME = "journals";
const KEY_PATTERN = /^[A-Za-z0-9:._-]{1,260}$/;
const MAX_RAW_BYTES = 1024 * 1024;
// A contributor's boot awaits this journal before the reader renders. A
// WebView whose IndexedDB never answers (a damaged or locked backing store
// left behind by an earlier session) must therefore fail within a bound so
// the scoped localStorage journal can take over instead of the spinner
// staying up forever.
const DEFAULT_OPERATION_TIMEOUT_MS = 4_000;

/** Transactional, origin-local storage for private contributor retry state. */
export class IndexedDbContributionJournal {
  #database;
  #key;
  #timeoutMs;

  static async open({
    key,
    indexedDB = globalThis.indexedDB,
    timeoutMs = DEFAULT_OPERATION_TIMEOUT_MS,
  } = {}) {
    if (!indexedDB || typeof indexedDB.open !== "function") {
      throw new TypeError("IndexedDB contribution storage is unavailable.");
    }
    if (typeof key !== "string" || !KEY_PATTERN.test(key)) {
      throw new TypeError("The contribution journal key is invalid.");
    }
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60_000) {
      throw new RangeError("The contribution journal timeout is invalid.");
    }
    const opening = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.addEventListener("upgradeneeded", () => {
        if (!request.result.objectStoreNames.contains(STORE_NAME)) {
          request.result.createObjectStore(STORE_NAME);
        }
      }, { once: true });
      request.addEventListener("success", () => resolve(request.result), {
        once: true,
      });
      request.addEventListener("error", () => reject(
        request.error ?? new Error("IndexedDB contribution storage failed."),
      ), { once: true });
      request.addEventListener("blocked", () => reject(
        new Error("IndexedDB contribution storage is blocked."),
      ), { once: true });
    });
    const database = await bounded(opening, timeoutMs, "open");
    // Never hold a newer client's upgrade hostage from a background tab.
    database.addEventListener?.("versionchange", () => database.close?.());
    return new IndexedDbContributionJournal(database, key, timeoutMs);
  }

  constructor(database, key, timeoutMs = DEFAULT_OPERATION_TIMEOUT_MS) {
    this.#database = database;
    this.#key = key;
    this.#timeoutMs = timeoutMs;
  }

  read() {
    return bounded(this.#read(), this.#timeoutMs, "read");
  }

  write(raw) {
    if (
      typeof raw !== "string" ||
      new TextEncoder().encode(raw).byteLength > MAX_RAW_BYTES
    ) {
      return Promise.reject(
        new RangeError("The contribution journal is too large."),
      );
    }
    return bounded(this.#write(raw), this.#timeoutMs, "write");
  }

  remove() {
    return bounded(this.#remove(), this.#timeoutMs, "delete");
  }

  async #read() {
    const transaction = this.#database.transaction(STORE_NAME, "readonly");
    const done = transactionDone(transaction);
    const request = transaction.objectStore(STORE_NAME).get(this.#key);
    const value = await requestResult(request);
    await done;
    return typeof value === "string" ? value : null;
  }

  async #write(raw) {
    const transaction = this.#database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).put(raw, this.#key);
    await transactionDone(transaction);
    return true;
  }

  async #remove() {
    const transaction = this.#database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).delete(this.#key);
    await transactionDone(transaction);
    return true;
  }
}

export const CONTRIBUTION_JOURNAL_MAX_BYTES = MAX_RAW_BYTES;
export const CONTRIBUTION_JOURNAL_TIMEOUT_MS = DEFAULT_OPERATION_TIMEOUT_MS;

function bounded(operation, timeoutMs, label) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error(`IndexedDB contribution ${label} timed out.`));
      }
    }, timeoutMs);
    operation.then(
      (value) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          resolve(value);
        }
      },
      (error) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(error);
        }
      },
    );
  });
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.addEventListener("success", () => resolve(request.result), {
      once: true,
    });
    request.addEventListener("error", () => reject(
      request.error ?? new Error("IndexedDB request failed."),
    ), { once: true });
  });
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.addEventListener("complete", () => resolve(true), {
      once: true,
    });
    transaction.addEventListener("abort", () => reject(
      transaction.error ?? new Error("IndexedDB transaction aborted."),
    ), { once: true });
    transaction.addEventListener("error", () => reject(
      transaction.error ?? new Error("IndexedDB transaction failed."),
    ), { once: true });
  });
}
