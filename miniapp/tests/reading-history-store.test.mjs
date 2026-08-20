import assert from "node:assert/strict";
import test from "node:test";

import {
  READING_HISTORY_STORAGE_KEY,
  ReadingHistoryStore,
  readingHistoryStorageKey,
} from "../lib/reading-history-store.js";

const ACCOUNT_SCOPE = "a".repeat(64);
const OTHER_ACCOUNT_SCOPE = "b".repeat(64);

class MemoryStorage {
  values = new Map();

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

function visit(overrides = {}) {
  return {
    kind: "chapter",
    translation: "kjv",
    reference: "John 3:1",
    book: 43,
    book_name: "John",
    chapter: 3,
    verse: 1,
    ...overrides,
  };
}

function store(storage, { maximum = 1_000, start = 1_000 } = {}) {
  let now = start;
  let id = 0;
  return new ReadingHistoryStore({
    scope: ACCOUNT_SCOPE,
    storage,
    maximum,
    clock: () => now++,
    idFactory: () => `visit_${++id}`,
  });
}

test("records distinct successful chapter and selection locations newest first", () => {
  const history = store(new MemoryStorage());

  history.record(visit());
  history.record(visit({
    kind: "selection",
    reference: "John 3:16",
    verse: 16,
  }));

  assert.deepEqual(
    history.snapshot().map(({ kind, reference, visited_at }) => ({
      kind,
      reference,
      visited_at,
    })),
    [
      { kind: "selection", reference: "John 3:16", visited_at: 1_001 },
      { kind: "chapter", reference: "John 3:1", visited_at: 1_000 },
    ],
  );
});

test("moves a repeated coordinate to the front with its stable id", () => {
  const history = store(new MemoryStorage());
  const first = history.record(visit());
  const second = history.record(visit({
    reference: "John 4:1",
    chapter: 4,
  }));
  const revisited = history.record(visit({
    kind: "selection",
    reference: "John 3:1",
  }));

  assert.equal(history.size, 2);
  assert.equal(revisited.id, first.id);
  assert.deepEqual(history.snapshot(), [
    {
      ...revisited,
      id: first.id,
      kind: "selection",
      visited_at: 1_002,
    },
    second,
  ]);
});

test("coalesces an exact coordinate across event kinds and translations", () => {
  const history = store(new MemoryStorage());
  const chapter = history.record(visit());
  const selection = history.record(visit({
    kind: "selection",
    translation: "aov",
  }));

  assert.equal(history.size, 1);
  assert.equal(selection.id, chapter.id);
  assert.deepEqual(
    history.snapshot().map(({ id, kind }) => ({ id, kind })),
    [
      { id: selection.id, kind: "selection" },
    ],
  );
});

test("keeps exact verses distinct and coalesces translations by coordinate", () => {
  const history = store(new MemoryStorage());
  const first = history.record(visit({ kind: "selection" }));
  history.record(visit({
    kind: "selection",
    reference: "John 3:2",
    verse: 2,
  }));
  const translated = history.record(visit({
    kind: "selection",
    translation: "aov",
  }));

  assert.equal(translated.id, first.id);
  assert.deepEqual(
    history.snapshot().map(({ translation, verse }) => ({ translation, verse })),
    [
      { translation: "aov", verse: 1 },
      { translation: "kjv", verse: 2 },
    ],
  );
});

test("moves a repeated chapter to the front regardless of target verse", () => {
  const history = store(new MemoryStorage());
  const first = history.record(visit());
  history.record(visit({
    kind: "selection",
    reference: "John 3:8",
    verse: 8,
  }));
  const revisited = history.record(visit({
    reference: "John 3:16",
    verse: 16,
  }));

  assert.equal(history.size, 2);
  assert.equal(revisited.id, first.id);
  assert.deepEqual(
    history.snapshot().map(({ kind, reference }) => ({ kind, reference })),
    [
      { kind: "chapter", reference: "John 3:16" },
      { kind: "selection", reference: "John 3:8" },
    ],
  );
});

test("revisiting a full history moves one item without evicting another", () => {
  const history = store(new MemoryStorage(), { maximum: 2 });
  const first = history.record(visit());
  const second = history.record(visit({
    reference: "John 4:1",
    chapter: 4,
  }));

  history.record(visit());

  assert.equal(history.size, 2);
  assert.deepEqual(
    history.snapshot().map((entry) => entry.id),
    [first.id, second.id],
  );
});

test("compacts repeated coordinates restored from version one storage", () => {
  const storage = new MemoryStorage();
  const key = readingHistoryStorageKey(ACCOUNT_SCOPE);
  storage.setItem(key, JSON.stringify({
    version: 1,
    items: [
      {
        ...visit({ kind: "selection" }),
        id: "visit_newest",
        visited_at: 3,
      },
      {
        ...visit({ chapter: 4, reference: "John 4:16", verse: 16 }),
        id: "visit_other",
        visited_at: 2,
      },
      {
        ...visit({ kind: "selection", translation: "aov" }),
        id: "visit_legacy_duplicate",
        visited_at: 1,
      },
      {
        ...visit({ chapter: 4, reference: "John 4:1" }),
        id: "visit_legacy_chapter_duplicate",
        visited_at: 0,
      },
    ],
  }));

  const history = new ReadingHistoryStore({ scope: ACCOUNT_SCOPE, storage });

  assert.equal(history.size, 2);
  assert.deepEqual(
    history.snapshot().map(({ id, kind }) => ({ id, kind })),
    [
      { id: "visit_newest", kind: "selection" },
      { id: "visit_other", kind: "chapter" },
    ],
  );
  assert.deepEqual(
    JSON.parse(storage.getItem(key)).items,
    history.snapshot(),
  );
});

test("persists bounded coordinate-only history in scoped local storage", () => {
  const storage = new MemoryStorage();
  const history = store(storage, { maximum: 2 });

  history.record(visit({ chapter: 1, reference: "John 1:1" }));
  history.record(visit({ chapter: 2, reference: "John 2:1" }));
  history.record(visit({ chapter: 3, reference: "John 3:1" }));

  const key = readingHistoryStorageKey(ACCOUNT_SCOPE);
  const raw = storage.getItem(key);
  assert.ok(raw);
  assert.doesNotMatch(raw, /text|session_token|init_data|user_id/);
  assert.equal(storage.getItem(READING_HISTORY_STORAGE_KEY), null);
  assert.equal(key.includes(ACCOUNT_SCOPE), true);
  assert.deepEqual(
    history.snapshot().map((entry) => entry.chapter),
    [3, 2],
  );
});

test("restores, removes, and clears individual durable history entries", () => {
  const storage = new MemoryStorage();
  const first = store(storage);
  const one = first.record(visit());
  first.record(visit({ chapter: 4, reference: "John 4:1" }));

  const restored = new ReadingHistoryStore({
    scope: ACCOUNT_SCOPE,
    storage,
  });
  assert.equal(restored.size, 2);
  assert.equal(restored.remove(one.id), true);
  assert.deepEqual(
    restored.snapshot().map((entry) => entry.reference),
    ["John 4:1"],
  );
  assert.equal(restored.remove("missing"), false);
  assert.equal(restored.clear(), true);
  assert.equal(restored.size, 0);
  assert.equal(
    storage.getItem(readingHistoryStorageKey(ACCOUNT_SCOPE)),
    null,
  );
  assert.equal(restored.clear(), false);
});

test("isolates durable history between authenticated account scopes", () => {
  const storage = new MemoryStorage();
  const firstAccount = store(storage);
  firstAccount.record(visit());

  const otherAccount = new ReadingHistoryStore({
    scope: OTHER_ACCOUNT_SCOPE,
    storage,
  });
  assert.deepEqual(otherAccount.snapshot(), []);
  otherAccount.record(visit({
    chapter: 4,
    reference: "John 4:1",
  }));

  assert.equal(
    new ReadingHistoryStore({
      scope: ACCOUNT_SCOPE,
      storage,
    }).snapshot()[0].chapter,
    3,
  );
  assert.equal(
    new ReadingHistoryStore({
      scope: OTHER_ACCOUNT_SCOPE,
      storage,
    }).snapshot()[0].chapter,
    4,
  );
  assert.equal(storage.values.size, 2);
});

test("requires a lowercase SHA-256 account scope", () => {
  assert.throws(
    () => new ReadingHistoryStore({ storage: new MemoryStorage() }),
    /scope/i,
  );
  assert.throws(
    () => new ReadingHistoryStore({
      scope: "A".repeat(64),
      storage: new MemoryStorage(),
    }),
    /scope/i,
  );
  assert.throws(
    () => readingHistoryStorageKey("short"),
    /scope/i,
  );
});

test("returns defensive snapshots and rejects malformed visits", () => {
  const history = store(new MemoryStorage());
  history.record(visit());
  const snapshot = history.snapshot();
  snapshot[0].reference = "Changed";
  snapshot.push(visit());

  assert.equal(history.size, 1);
  assert.equal(history.snapshot()[0].reference, "John 3:1");
  assert.throws(
    () => history.record(visit({ translation: "../token" })),
    /invalid/i,
  );
  assert.throws(
    () => history.record(visit({ verse: 0 })),
    /invalid/i,
  );
  assert.doesNotThrow(() =>
    history.record(visit({ chapter: 1_000, verse: 2_000 })),
  );
  assert.throws(
    () => history.record(visit({ chapter: 1_001 })),
    /invalid/i,
  );
});

test("discards corrupt persistence and falls back to memory on storage failure", () => {
  const corrupt = new MemoryStorage();
  const key = readingHistoryStorageKey(ACCOUNT_SCOPE);
  corrupt.setItem(key, "{not-json");
  const recovered = store(corrupt);
  assert.deepEqual(recovered.snapshot(), []);
  assert.equal(corrupt.getItem(key), null);
  assert.equal(recovered.persistent, true);

  const unavailable = {
    getItem() {
      throw new Error("blocked");
    },
    setItem() {
      throw new Error("blocked");
    },
    removeItem() {
      throw new Error("blocked");
    },
  };
  const memoryOnly = store(unavailable);
  assert.equal(memoryOnly.persistent, false);
  assert.doesNotThrow(() => memoryOnly.record(visit()));
  assert.equal(memoryOnly.size, 1);
});

test("keeps new history in memory after a quota failure", () => {
  const storage = new MemoryStorage();
  storage.setItem = () => {
    throw new Error("quota");
  };
  const history = store(storage);

  assert.doesNotThrow(() => history.record(visit()));
  assert.equal(history.size, 1);
  assert.equal(history.persistent, false);
  assert.deepEqual(history.snapshot().map((entry) => entry.chapter), [3]);
});
