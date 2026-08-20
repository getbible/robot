const STORAGE_PREFIX = "getbible.miniapp.reading-history.v1";
const LEGACY_STORAGE_KEY = "getbible.miniapp.reading-history";
const RECORD_VERSION = 1;
const DEFAULT_MAXIMUM = 1_000;
const SCOPE_PATTERN = /^[a-f0-9]{64}$/;
const TRANSLATION_PATTERN = /^[a-z0-9][a-z0-9_-]{0,29}$/;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const HISTORY_KINDS = new Set(["chapter", "selection"]);

export class ReadingHistoryStore {
  #clock;
  #idFactory;
  #items;
  #key;
  #maximum;
  #persistent;
  #sequence = 0;
  #storage;

  constructor({
    scope,
    storage = browserLocalStorage(),
    maximum = DEFAULT_MAXIMUM,
    clock = Date.now,
    idFactory = null,
  } = {}) {
    if (typeof scope !== "string" || !SCOPE_PATTERN.test(scope)) {
      throw new TypeError(
        "An authenticated reading history storage scope is required.",
      );
    }
    if (!Number.isInteger(maximum) || maximum < 1 || maximum > 1_000) {
      throw new RangeError("Reading history maximum must be between 1 and 1000.");
    }
    if (typeof clock !== "function") {
      throw new TypeError("A reading history clock is required.");
    }
    if (idFactory !== null && typeof idFactory !== "function") {
      throw new TypeError("Reading history idFactory must be a function.");
    }
    this.#key = readingHistoryStorageKey(scope);
    this.#storage = storageLike(storage) ? storage : null;
    this.#persistent = Boolean(this.#storage);
    this.#maximum = maximum;
    this.#clock = clock;
    this.#idFactory = idFactory;
    this.#items = this.#read();
  }

  get size() {
    return this.#items.length;
  }

  get persistent() {
    return this.#persistent;
  }

  record(visit) {
    const visitedAt = Number(this.#clock());
    if (!Number.isFinite(visitedAt) || visitedAt < 0) {
      throw new TypeError("Reading history clock returned an invalid value.");
    }
    this.#sequence += 1;
    const id = this.#idFactory
      ? this.#idFactory({ visitedAt, sequence: this.#sequence })
      : defaultIdentifier(visitedAt, this.#sequence);
    const entry = normalizeEntry({
      ...visit,
      id,
      visited_at: Math.floor(visitedAt),
    });
    const existing = this.#items.find((item) =>
      sameHistoryLocation(item, entry)
    );
    if (existing) {
      entry.id = existing.id;
    }
    this.#items = this.#items.filter(
      (item) => !sameHistoryLocation(item, entry),
    );
    this.#items.unshift(entry);
    if (this.#items.length > this.#maximum) {
      this.#items.length = this.#maximum;
    }
    this.#persist();
    return cloneEntry(entry);
  }

  remove(id) {
    if (typeof id !== "string" || !IDENTIFIER_PATTERN.test(id)) {
      return false;
    }
    const index = this.#items.findIndex((entry) => entry.id === id);
    if (index < 0) {
      return false;
    }
    this.#items.splice(index, 1);
    this.#persist();
    return true;
  }

  clear() {
    if (this.#items.length === 0) {
      return false;
    }
    this.#items = [];
    this.#persist();
    return true;
  }

  snapshot() {
    return this.#items.map(cloneEntry);
  }

  #read() {
    if (!this.#storage) {
      return [];
    }
    let raw;
    try {
      raw = this.#storage.getItem(this.#key);
    } catch {
      this.#disablePersistence();
      return [];
    }
    if (!raw) {
      return [];
    }
    try {
      const record = JSON.parse(raw);
      if (
        record?.version !== RECORD_VERSION ||
        !Array.isArray(record.items) ||
        record.items.length > 1_000
      ) {
        throw new TypeError("Reading history record is invalid.");
      }
      const items = uniqueEntries(record.items.map(normalizeEntry))
        .slice(0, this.#maximum);
      if (items.length !== record.items.length) {
        try {
          this.#storage.setItem(
            this.#key,
            JSON.stringify({ version: RECORD_VERSION, items }),
          );
        } catch {
          this.#disablePersistence();
        }
      }
      return items;
    } catch {
      try {
        this.#storage.removeItem(this.#key);
      } catch {
        this.#disablePersistence();
      }
      return [];
    }
  }

  #persist() {
    if (!this.#storage) {
      return;
    }
    try {
      if (this.#items.length === 0) {
        this.#storage.removeItem(this.#key);
        return;
      }
      this.#storage.setItem(
        this.#key,
        JSON.stringify({
          version: RECORD_VERSION,
          items: this.#items,
        }),
      );
    } catch {
      this.#disablePersistence();
    }
  }

  #disablePersistence() {
    this.#storage = null;
    this.#persistent = false;
  }
}

export const READING_HISTORY_STORAGE_PREFIX = STORAGE_PREFIX;
// Kept for callers that imported the original constant. The store deliberately
// does not migrate this unscoped record because its account ownership is
// unknowable.
export const READING_HISTORY_STORAGE_KEY = LEGACY_STORAGE_KEY;
export const DEFAULT_READING_HISTORY_MAXIMUM = DEFAULT_MAXIMUM;

export function readingHistoryStorageKey(scope) {
  if (typeof scope !== "string" || !SCOPE_PATTERN.test(scope)) {
    throw new TypeError(
      "An authenticated reading history storage scope is required.",
    );
  }
  return `${STORAGE_PREFIX}:${scope}`;
}

function normalizeEntry(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("Reading history entry must be an object.");
  }
  const id = boundedText(value.id, 128);
  const kind = boundedText(value.kind, 16);
  const translation = boundedText(value.translation, 30).toLowerCase();
  const reference = boundedText(value.reference, 180);
  const bookName = boundedText(value.book_name, 128);
  const book = boundedInteger(value.book, 1, 200);
  const chapter = boundedInteger(value.chapter, 1, 1_000);
  const verse = boundedInteger(value.verse, 1, 2_000);
  const visitedAt = Number(value.visited_at);
  if (
    !IDENTIFIER_PATTERN.test(id) ||
    !HISTORY_KINDS.has(kind) ||
    !TRANSLATION_PATTERN.test(translation) ||
    !Number.isSafeInteger(visitedAt) ||
    visitedAt < 0
  ) {
    throw new TypeError("Reading history entry is invalid.");
  }
  return {
    id,
    kind,
    translation,
    reference,
    book,
    book_name: bookName,
    chapter,
    verse,
    visited_at: visitedAt,
  };
}

function boundedText(value, maximum) {
  if (typeof value !== "string") {
    throw new TypeError("Reading history text is invalid.");
  }
  const normalized = value.trim();
  if (normalized.length === 0 || normalized.length > maximum) {
    throw new TypeError("Reading history text is invalid.");
  }
  return normalized;
}

function boundedInteger(value, minimum, maximum) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < minimum || number > maximum) {
    throw new TypeError("Reading history coordinate is invalid.");
  }
  return number;
}

function cloneEntry(entry) {
  return { ...entry };
}

function sameHistoryLocation(left, right) {
  const sameChapter =
    left.book === right.book &&
    left.chapter === right.chapter;
  return (
    sameChapter &&
    (
      left.verse === right.verse ||
      (left.kind === "chapter" && right.kind === "chapter")
    )
  );
}

function uniqueEntries(entries) {
  const unique = [];
  for (const entry of entries) {
    if (!unique.some((item) => sameHistoryLocation(item, entry))) {
      unique.push(entry);
    }
  }
  return unique;
}

function defaultIdentifier(visitedAt, sequence) {
  const entropy = Math.floor(Math.random() * 0x1_0000_0000)
    .toString(36)
    .padStart(7, "0");
  return `visit_${Math.floor(visitedAt).toString(36)}_${sequence.toString(36)}_${entropy}`;
}

function browserLocalStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

function storageLike(storage) {
  return Boolean(
    storage &&
    typeof storage.getItem === "function" &&
    typeof storage.setItem === "function" &&
    typeof storage.removeItem === "function",
  );
}
