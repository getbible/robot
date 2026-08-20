import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  BOOKMARK_BACKUP_MAX_BYTES,
  BOOKMARK_STORAGE_PREFIX,
  BOOKMARK_TEXT_MAX_CHARS,
  BOOKMARK_TOPIC_COLORS,
  BookmarkBackupError,
  BookmarkStore,
  DEFAULT_BOOKMARK_TOPICS,
  bookmarkStorageScope,
  parseBookmarkBackup,
} from "../lib/bookmark-store.js";

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

function verse(overrides = {}) {
  return {
    translation: "kjv",
    reference: "John 3:16",
    book: 43,
    book_name: "John",
    chapter: 3,
    verse: 16,
    text: "For God so loved the world.",
    ...overrides,
  };
}

function scopedStore(storage, scope = "a".repeat(64), options = {}) {
  let id = 0;
  let now = 1_000;
  return new BookmarkStore({
    storage,
    scope,
    idFactory: () => `bookmark_${++id}`,
    clock: () => now++,
    ...options,
  });
}

test("ships the exact ordered GetBible deployment topics and palette", () => {
  assert.equal(DEFAULT_BOOKMARK_TOPICS.length, 60);
  assert.deepEqual(DEFAULT_BOOKMARK_TOPICS[0], {
    id: "adultery",
    name: "Adultery",
    color: "#f9a8b8",
  });
  assert.deepEqual(DEFAULT_BOOKMARK_TOPICS.at(-1), {
    id: "worldly-wisdom",
    name: "Worldly Wisdom",
    color: "#d6d3d1",
  });
  assert.equal(
    new Set(DEFAULT_BOOKMARK_TOPICS.map((topic) => topic.id)).size,
    60,
  );
  assert.ok(
    DEFAULT_BOOKMARK_TOPICS.every((topic) =>
      BOOKMARK_TOPIC_COLORS.includes(topic.color)
    ),
  );
  assert.ok(DEFAULT_BOOKMARK_TOPICS.some((topic) => topic.name === "Grace"));
  assert.ok(
    DEFAULT_BOOKMARK_TOPICS.some(
      (topic) => topic.name === "Authority of the Bible",
    ),
  );
  assert.equal(
    createHash("sha256")
      .update(JSON.stringify(DEFAULT_BOOKMARK_TOPICS))
      .digest("hex"),
    // Full ordered id/name/color fingerprint of the reference deployment.
    "70f658e009dbad243519bbf3b853aa14ed100b3ffede29bf5dddfab2df81d83e",
  );
});

test("derives a stable account scope only from the authenticated user", async () => {
  const first = await bookmarkStorageScope(42);
  const reopened = await bookmarkStorageScope(42);
  const other = await bookmarkStorageScope(43);

  assert.match(first, /^[a-f0-9]{64}$/);
  assert.equal(reopened, first);
  assert.notEqual(other, first);
  assert.doesNotMatch(first, /(^|\D)42(\D|$)/);
  await assert.rejects(bookmarkStorageScope(0), /authenticated/i);
  await assert.rejects(bookmarkStorageScope(Number.MAX_SAFE_INTEGER), /authenticated/i);
});

test("persists per-account bookmarks across launches without cross-account reads", async () => {
  const storage = new MemoryStorage();
  const aliceScope = await bookmarkStorageScope(101);
  const bobScope = await bookmarkStorageScope(202);
  const alice = scopedStore(storage, aliceScope);
  alice.apply(verse(), "grace");

  const reopened = scopedStore(storage, aliceScope);
  const bob = scopedStore(storage, bobScope);

  assert.equal(reopened.size, 1);
  assert.equal(reopened.bookmarkFor(verse())?.topic_id, "grace");
  assert.equal(bob.size, 0);
  assert.equal(storage.values.size, 1);
  const [key, raw] = storage.values.entries().next().value;
  assert.match(key, new RegExp(`^${BOOKMARK_STORAGE_PREFIX}:`));
  assert.doesNotMatch(key, /101|202/);
  assert.doesNotMatch(raw, /user_id|session_token|init_data|launch_token/);
});

test("keeps one canonical whole-verse bookmark across translations", () => {
  const bookmarks = scopedStore(new MemoryStorage());
  const first = bookmarks.apply(verse(), "grace");
  const replacement = bookmarks.apply(verse({
    translation: "aov",
    reference: "John 3:16",
    text: "Want so lief het God die wêreld gehad.",
  }), "biblical-love");

  assert.equal(bookmarks.size, 1);
  assert.equal(replacement.id, first.id);
  assert.equal(replacement.created_at, first.created_at);
  assert.equal(replacement.updated_at, 1_001);
  assert.equal(bookmarks.bookmarkFor(verse())?.translation, "aov");
  assert.equal(bookmarks.bookmarkFor(verse())?.topic_id, "biblical-love");
  assert.equal(bookmarks.removeVerse(verse({ translation: "web" })), true);
  assert.equal(bookmarks.size, 0);
});

test("bounds and sanitizes verse excerpts for one CloudStorage value", () => {
  const bookmarks = scopedStore(new MemoryStorage());
  const saved = bookmarks.apply(verse({
    text: `${"\\".repeat(BOOKMARK_TEXT_MAX_CHARS + 200)}\u0000ignored`,
  }), "grace");

  assert.equal(saved.text.length, BOOKMARK_TEXT_MAX_CHARS);
  assert.doesNotMatch(saved.text, /\u0000/);
  const sanitizedUnicode = bookmarks.apply(verse({
    chapter: 4,
    text: `valid \ud800 text \udc00 ${String.fromCodePoint(0x1f64f)}`,
  }), "grace");
  assert.equal(
    sanitizedUnicode.text,
    `valid \ufffd text \ufffd ${String.fromCodePoint(0x1f64f)}`,
  );
  assert.equal(BOOKMARK_BACKUP_MAX_BYTES, 4 * 1024 * 1024);
});

test("manages custom topics and cascades deletion while preserving one topic", () => {
  const bookmarks = scopedStore(new MemoryStorage());
  const custom = bookmarks.addTopic("Promises", "#123456");
  bookmarks.apply(verse(), custom.id);
  bookmarks.updateTopic(custom.id, {
    name: "God's promises",
    color: "#654321",
  });

  assert.equal(bookmarks.topic(custom.id)?.name, "God's promises");
  assert.equal(bookmarks.topic(custom.id)?.color, "#654321");
  assert.equal(bookmarks.topicUsage(custom.id), 1);
  assert.deepEqual(bookmarks.removeTopic(custom.id), { removed_bookmarks: 1 });
  assert.equal(bookmarks.size, 0);
  assert.throws(
    () => bookmarks.addTopic("Unsafe", "not-a-color"),
    /invalid/i,
  );
  assert.throws(
    () => bookmarks.updateTopic("grace", { name: "   " }),
    /invalid/i,
  );

  const topicIds = bookmarks.snapshot().topics.map((topic) => topic.id);
  for (const id of topicIds.slice(1)) {
    assert.notEqual(bookmarks.removeTopic(id), false);
  }
  assert.equal(bookmarks.topicCount, 1);
  assert.equal(bookmarks.removeTopic(topicIds[0]), false);
});

test("exports a portable GetBible v2 JSON backup and merges it idempotently", () => {
  const original = scopedStore(new MemoryStorage());
  original.apply(verse(), "grace");
  const backup = original.backup("2026-08-20T10:00:00.000Z");
  const encoded = JSON.stringify(backup);

  assert.equal(backup.version, 2);
  assert.equal(backup.format, "getbible-life-markings");
  assert.deepEqual(backup.notes, []);
  assert.equal(backup.markings[0].start, null);
  assert.equal(backup.markings[0].end, null);
  assert.doesNotMatch(
    encoded,
    /user_id|storage_scope|session_token|init_data|launch_token/,
  );

  const restored = scopedStore(new MemoryStorage(), "b".repeat(64));
  const first = restored.importBackup(backup, { byteLength: encoded.length });
  const repeated = restored.importBackup(backup, { byteLength: encoded.length });

  assert.equal(first.bookmarks_added, 1);
  assert.equal(first.topics_added, 0);
  assert.equal(restored.bookmarkFor(verse())?.topic_id, "grace");
  assert.equal(repeated.bookmarks_added, 0);
  assert.equal(repeated.conflicts_skipped, 1);
  assert.equal(restored.size, 1);
});

test("imports whole-verse reference markings and reports skipped ranges and notes", () => {
  const backup = scopedStore(new MemoryStorage()).backup(
    "2026-08-20T10:00:00.000Z",
  );
  backup.markings.push({
    id: "range_1",
    passage: { translation: "kjv", book: 43, chapter: 3 },
    verse: 16,
    start: 0,
    end: 3,
    quote: "For",
    reference: "John 3:16",
    colorId: "grace",
    createdAt: 4,
  });
  backup.notes.push({ id: "note_ignored" });

  const parsed = parseBookmarkBackup(backup);
  assert.equal(parsed.bookmarks.length, 0);
  assert.equal(parsed.range_markings_skipped, 1);
  assert.equal(parsed.notes_skipped, 1);
});

test("accepts compatible long backup quotes but stores a cloud-safe excerpt", () => {
  const source = scopedStore(new MemoryStorage());
  source.apply(verse(), "grace");
  const backup = source.backup("2026-08-20T10:00:00.000Z");
  backup.markings[0].quote = "x".repeat(3_000);

  const parsed = parseBookmarkBackup(backup);

  assert.equal(parsed.bookmarks[0].text.length, BOOKMARK_TEXT_MAX_CHARS);
});

test("rejects malformed or oversized backups atomically", () => {
  const bookmarks = scopedStore(new MemoryStorage());
  bookmarks.apply(verse(), "grace");
  const before = bookmarks.snapshot();
  const backup = bookmarks.backup("2026-08-20T10:00:00.000Z");

  assert.throws(
    () => bookmarks.importBackup(
      { ...backup, version: 99 },
    ),
    BookmarkBackupError,
  );
  assert.throws(
    () => bookmarks.importBackup(backup, {
      byteLength: BOOKMARK_BACKUP_MAX_BYTES + 1,
    }),
    /large/i,
  );
  const orphan = structuredClone(backup);
  orphan.markings[0].colorId = "missing";
  assert.throws(() => bookmarks.importBackup(orphan), /entries/i);
  const unsafeColor = structuredClone(backup);
  unsafeColor.colors[0].value = "#12zz99";
  assert.throws(() => bookmarks.importBackup(unsafeColor), /topics/i);
  const unsafeUnicode = structuredClone(backup);
  unsafeUnicode.markings[0].quote = "broken \ud800 quote";
  assert.throws(() => bookmarks.importBackup(unsafeUnicode), /entries/i);
  assert.deepEqual(bookmarks.snapshot(), before);
});

test("compacts known duplicates, protects future records, and falls back to memory", () => {
  const storage = new MemoryStorage();
  const key = `${BOOKMARK_STORAGE_PREFIX}:${"a".repeat(64)}`;
  const seed = scopedStore(new MemoryStorage());
  const record = seed.snapshot();
  const first = seed.apply(verse(), "grace");
  const newer = {
    ...first,
    id: "newer",
    topic_id: "biblical-love",
    updated_at: first.updated_at + 10,
  };
  storage.setItem(key, JSON.stringify({
    version: 1,
    active_topic_id: "grace",
    topics: record.topics,
    bookmarks: [first, newer],
  }));
  const compacted = scopedStore(storage);
  assert.equal(compacted.size, 1);
  assert.equal(compacted.bookmarkFor(verse())?.id, "newer");

  const futureStorage = new MemoryStorage();
  futureStorage.setItem(key, JSON.stringify({ version: 2 }));
  const future = scopedStore(futureStorage);
  assert.equal(future.persistent, false);
  assert.equal(future.size, 0);
  assert.equal(JSON.parse(futureStorage.getItem(key)).version, 2);

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
  const memoryOnly = scopedStore(unavailable);
  assert.equal(memoryOnly.persistent, false);
  assert.doesNotThrow(() => memoryOnly.apply(verse(), "grace"));
  assert.equal(memoryOnly.size, 1);
});

test("returns defensive snapshots", () => {
  const bookmarks = scopedStore(new MemoryStorage());
  bookmarks.apply(verse(), "grace");
  const snapshot = bookmarks.snapshot();
  snapshot.topics[0].name = "Changed";
  snapshot.bookmarks[0].reference = "Changed";
  snapshot.bookmarks.push(verse());

  assert.equal(bookmarks.topic("adultery")?.name, "Adultery");
  assert.equal(bookmarks.bookmarkFor(verse())?.reference, "John 3:16");
  assert.equal(bookmarks.size, 1);
});
