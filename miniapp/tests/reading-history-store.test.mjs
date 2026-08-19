import assert from "node:assert/strict";
import test from "node:test";

import {
  READING_HISTORY_STORAGE_KEY,
  ReadingHistoryStore,
} from "../lib/reading-history-store.js";

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
    storage,
    maximum,
    clock: () => now++,
    idFactory: () => `visit_${++id}`,
  });
}

test("records every successful chapter and selection newest first", () => {
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

test("retains repeated coordinates and keeps translations distinct", () => {
  const history = store(new MemoryStorage());
  history.record(visit());
  history.record(visit());
  history.record(visit({ translation: "aov" }));

  assert.deepEqual(
    history.snapshot().map((entry) => entry.translation),
    ["aov", "kjv", "kjv"],
  );
});

test("persists bounded coordinate-only history in session storage", () => {
  const storage = new MemoryStorage();
  const history = store(storage, { maximum: 2 });

  history.record(visit({ chapter: 1, reference: "John 1:1" }));
  history.record(visit({ chapter: 2, reference: "John 2:1" }));
  history.record(visit({ chapter: 3, reference: "John 3:1" }));

  const raw = storage.getItem(READING_HISTORY_STORAGE_KEY);
  assert.ok(raw);
  assert.doesNotMatch(raw, /text|session_token|init_data|user_id/);
  assert.deepEqual(
    history.snapshot().map((entry) => entry.chapter),
    [3, 2],
  );
});

test("restores, removes, and clears individual session history entries", () => {
  const storage = new MemoryStorage();
  const first = store(storage);
  const one = first.record(visit());
  first.record(visit({ chapter: 4, reference: "John 4:1" }));

  const restored = new ReadingHistoryStore({ storage });
  assert.equal(restored.size, 2);
  assert.equal(restored.remove(one.id), true);
  assert.deepEqual(
    restored.snapshot().map((entry) => entry.reference),
    ["John 4:1"],
  );
  assert.equal(restored.remove("missing"), false);
  assert.equal(restored.clear(), true);
  assert.equal(restored.size, 0);
  assert.equal(storage.getItem(READING_HISTORY_STORAGE_KEY), null);
  assert.equal(restored.clear(), false);
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
  corrupt.setItem(READING_HISTORY_STORAGE_KEY, "{not-json");
  const recovered = store(corrupt);
  assert.deepEqual(recovered.snapshot(), []);
  assert.equal(corrupt.getItem(READING_HISTORY_STORAGE_KEY), null);

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
  assert.doesNotThrow(() => memoryOnly.record(visit()));
  assert.equal(memoryOnly.size, 1);
});
