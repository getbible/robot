const DATABASE_NAME = "getbible-miniapp-contributions";
const DATABASE_VERSION = 1;
const STORE_NAME = "journals";
const KEY_PATTERN = /^[A-Za-z0-9:._-]{1,260}$/;
const MAX_RAW_BYTES = 1024 * 1024;

/** Transactional, origin-local storage for private contributor retry state. */
export class IndexedDbContributionJournal {
  #database;
  #key;

  static async open({ key, indexedDB = globalThis.indexedDB } = {}) {
    if (!indexedDB || typeof indexedDB.open !== "function") {
      throw new TypeError("IndexedDB contribution storage is unavailable.");
    }
    if (typeof key !== "string" || !KEY_PATTERN.test(key)) {
      throw new TypeError("The contribution journal key is invalid.");
    }
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.addEventListener("upgradeneeded", () => {
      if (!request.result.objectStoreNames.contains(STORE_NAME)) {
        request.result.createObjectStore(STORE_NAME);
      }
    }, { once: true });
    const database = await requestResult(request);
    return new IndexedDbContributionJournal(database, key);
  }

  constructor(database, key) {
    this.#database = database;
    this.#key = key;
  }

  async read() {
    const transaction = this.#database.transaction(STORE_NAME, "readonly");
    const done = transactionDone(transaction);
    const request = transaction.objectStore(STORE_NAME).get(this.#key);
    const value = await requestResult(request);
    await done;
    return typeof value === "string" ? value : null;
  }

  async write(raw) {
    if (
      typeof raw !== "string" ||
      new TextEncoder().encode(raw).byteLength > MAX_RAW_BYTES
    ) {
      throw new RangeError("The contribution journal is too large.");
    }
    const transaction = this.#database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).put(raw, this.#key);
    await transactionDone(transaction);
    return true;
  }

  async remove() {
    const transaction = this.#database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).delete(this.#key);
    await transactionDone(transaction);
    return true;
  }
}

export const CONTRIBUTION_JOURNAL_MAX_BYTES = MAX_RAW_BYTES;

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
