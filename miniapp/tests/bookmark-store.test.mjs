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
  MAX_BOOKMARKS,
  bookmarkStorageScope,
  parseBookmarkBackup,
} from "../lib/bookmark-store.js";
import { GLOBAL_BOOKMARK_TOPIC_DEFINITIONS } from "../lib/global-bookmark-catalog.js";

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
  assert.equal(DEFAULT_BOOKMARK_TOPICS.length, 61);
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
    61,
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
  assert.deepEqual(
    DEFAULT_BOOKMARK_TOPICS.find((topic) => topic.id === "biblical-love"),
    { id: "biblical-love", name: "Biblical Love", color: "#a16207" },
  );
  assert.deepEqual(
    DEFAULT_BOOKMARK_TOPICS.find((topic) => topic.id === "fear-not"),
    { id: "fear-not", name: "Fear Not", color: "#fef08a" },
  );
  assert.equal(
    createHash("sha256")
      .update(JSON.stringify(DEFAULT_BOOKMARK_TOPICS))
      .digest("hex"),
    // Full ordered id/name/color fingerprint of the reference deployment.
    "e0365d53dda5dc5d992b10bedea272f850b1a7bdff96e4e19a49ff8fd1bc8ba8", // pragma: allowlist secret
  );
});

test("restores canonical global topics without replacing personal topic ids", () => {
  const storage = new MemoryStorage();
  let id = 40;
  const bookmarks = scopedStore(storage, "a".repeat(64), {
    idFactory: (prefix) => prefix === "topic" ? String(++id) : `bookmark_${++id}`,
  });
  bookmarks.removeTopic("blessings-and-curses");
  bookmarks.removeTopic("fear-not");
  const legacy = bookmarks.addTopic("Blessings & Curses", "#123456");
  bookmarks.apply(verse(), legacy.id);

  const definitions = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.filter((definition) =>
    ["blessings-and-curses", "fear-not"].includes(definition.id)
  );
  const restored = bookmarks.ensureTopics(definitions);

  assert.equal(restored.topics_added, 1);
  assert.equal(restored.topics_updated, 0);
  assert.equal(restored.topic_ids["blessings-and-curses"], legacy.id);
  assert.equal(restored.topic_ids["fear-not"], "fear-not");
  assert.equal(bookmarks.topic(legacy.id)?.name, "Blessings & Curses");
  assert.equal(bookmarks.topic(legacy.id)?.color, "#123456");
  assert.equal(bookmarks.bookmarkFor(verse())?.topic_id, legacy.id);
  assert.equal(bookmarks.topic("fear-not")?.name, "Fear Not");
  const repeated = bookmarks.ensureTopics(definitions);
  assert.equal(repeated.topics_added, 0);
  assert.equal(repeated.topics_updated, 0);
  assert.equal(repeated.topic_ids["blessings-and-curses"], legacy.id);
  assert.equal(repeated.topic_ids["fear-not"], "fear-not");
});

test("reuses a mapped numeric topic after the user renames it", () => {
  const storage = new MemoryStorage();
  let id = 40;
  const bookmarks = scopedStore(storage, "a".repeat(64), {
    idFactory: (prefix) => prefix === "topic" ? String(++id) : `bookmark_${++id}`,
  });
  bookmarks.removeTopic("grace");
  const renamed = bookmarks.addTopic("Grace renamed by the user", "#123456");
  const definitions = GLOBAL_BOOKMARK_TOPIC_DEFINITIONS.filter(
    (definition) => definition.id === "grace",
  );

  const restored = bookmarks.ensureTopics(definitions, {
    grace: renamed.id,
  });

  assert.equal(restored.topics_added, 0);
  assert.equal(restored.topic_ids.grace, renamed.id);
  assert.equal(bookmarks.topic("grace"), null);
  assert.equal(bookmarks.topic(renamed.id)?.name, "Grace renamed by the user");
  assert.equal(bookmarks.topic(renamed.id)?.color, "#123456");
});

test("keeps fresh Biblical Love brown without rewriting stored color choices", () => {
  const storage = new MemoryStorage();
  const scope = "a".repeat(64);
  const topics = DEFAULT_BOOKMARK_TOPICS.map((topic) => ({ ...topic }));
  topics.find((topic) => topic.id === "biblical-love").color = "#f9a8d4";
  topics.push({ id: "42", name: "Biblical Love", color: "#f9a8d4" });
  topics.push({ id: "custom-love", name: "My Love", color: "#f9a8d4" });
  storage.setItem(`${BOOKMARK_STORAGE_PREFIX}:${scope}`, JSON.stringify({
    version: 1,
    active_topic_id: "biblical-love",
    record_updated_at: 1,
    topics,
    bookmarks: [],
  }));

  const bookmarks = scopedStore(storage, scope);

  assert.equal(bookmarks.topic("biblical-love")?.color, "#f9a8d4");
  assert.equal(bookmarks.topic("42")?.color, "#f9a8d4");
  assert.equal(bookmarks.topic("custom-love")?.color, "#f9a8d4");
  assert.equal(
    JSON.parse(storage.getItem(`${BOOKMARK_STORAGE_PREFIX}:${scope}`))
      .topics.find((topic) => topic.id === "biblical-love").color,
    "#f9a8d4",
  );
  assert.equal(
    scopedStore(new MemoryStorage(), "b".repeat(64))
      .topic("biblical-love")?.color,
    "#a16207",
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
  assert.deepEqual(bookmarks.bookmarkFor(verse())?.topic_ids, [
    "grace",
    "biblical-love",
  ]);
  assert.equal(bookmarks.bookmarkFor(verse())?.topic_id, "grace");
  assert.equal(bookmarks.bookmarksForTopic("grace").length, 1);
  assert.equal(bookmarks.bookmarksForTopic("biblical-love").length, 1);
  assert.equal(bookmarks.topicUsage("grace"), 1);
  assert.equal(bookmarks.topicUsage("biblical-love"), 1);

  const repeated = bookmarks.apply(verse({ translation: "web" }), "grace");
  assert.deepEqual(repeated.topic_ids, ["grace", "biblical-love"]);
  assert.equal(bookmarks.size, 1);

  assert.equal(bookmarks.removeBookmarkTopic(first.id, "grace"), true);
  assert.deepEqual(bookmarks.bookmarkFor(verse())?.topic_ids, ["biblical-love"]);
  assert.equal(bookmarks.bookmarkFor(verse())?.topic_id, "biblical-love");
  assert.equal(bookmarks.removeBookmarkTopic(first.id, "grace"), false);
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

test("counts the 800-item limit by verse while allowing more topic membership", () => {
  const bookmarks = scopedStore(new MemoryStorage());
  for (let index = 1; index <= MAX_BOOKMARKS; index += 1) {
    bookmarks.apply(verse({
      book: 1,
      book_name: "Genesis",
      chapter: 1,
      verse: index,
      reference: `Genesis 1:${index}`,
    }), "grace");
  }

  assert.equal(bookmarks.size, MAX_BOOKMARKS);
  assert.doesNotThrow(() => bookmarks.apply(verse({
    book: 1,
    book_name: "Genesis",
    chapter: 1,
    verse: 1,
    reference: "Genesis 1:1",
  }), "biblical-love"));
  assert.deepEqual(bookmarks.bookmarkFor({ book: 1, chapter: 1, verse: 1 }).topic_ids, [
    "grace",
    "biblical-love",
  ]);
  assert.throws(() => bookmarks.apply(verse({
    book: 2,
    book_name: "Exodus",
    chapter: 1,
    verse: 1,
    reference: "Exodus 1:1",
  }), "grace"), /limit/i);
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

test("removing a topic preserves the same verse in its other topics", () => {
  const bookmarks = scopedStore(new MemoryStorage());
  const saved = bookmarks.apply(verse(), "grace");
  bookmarks.apply(verse(), "biblical-love");

  assert.deepEqual(bookmarks.removeTopic("grace"), { removed_bookmarks: 1 });
  assert.equal(bookmarks.size, 1);
  assert.equal(bookmarks.bookmarkFor(verse())?.id, saved.id);
  assert.deepEqual(bookmarks.bookmarkFor(verse())?.topic_ids, [
    "biblical-love",
  ]);
  assert.equal(bookmarks.topicUsage("biblical-love"), 1);
});

test("exports a compact GetBible v4 JSON backup and merges it idempotently", () => {
  const original = scopedStore(new MemoryStorage());
  original.apply(verse(), "grace");
  original.apply(verse(), "biblical-love");
  const backup = original.backup("2026-08-20T10:00:00.000Z");
  const encoded = JSON.stringify(backup);

  assert.equal(backup.version, 4);
  assert.equal(backup.format, "getbible-life-markings");
  assert.deepEqual(backup.notes, []);
  assert.equal(backup.markings[0].start, null);
  assert.equal(backup.markings[0].end, null);
  assert.deepEqual(backup.markings[0].colorIndexes, [
    backup.colors.findIndex((topic) => topic.id === "grace"),
    backup.colors.findIndex((topic) => topic.id === "biblical-love"),
  ]);
  assert.equal(backup.markings[0].bookName, "John");
  assert.equal(Object.hasOwn(backup.markings[0], "reference"), false);
  assert.equal(Object.hasOwn(backup.markings[0], "colorId"), false);
  assert.equal(Object.hasOwn(backup.markings[0], "colorIds"), false);
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
  assert.deepEqual(restored.bookmarkFor(verse())?.topic_ids, [
    "grace",
    "biblical-love",
  ]);
  assert.equal(repeated.bookmarks_added, 0);
  assert.equal(repeated.conflicts_skipped, 1);
  assert.equal(restored.size, 1);
});

test("imports legacy singular-color backups into one multi-topic verse", () => {
  const backup = scopedStore(new MemoryStorage()).backup(
    "2026-08-20T10:00:00.000Z",
  );
  backup.version = 2;
  backup.markings = [
    {
      id: "legacy_grace",
      passage: { translation: "kjv", book: 43, chapter: 3 },
      verse: 16,
      start: null,
      end: null,
      quote: "For God so loved the world.",
      reference: "John 3:16",
      colorId: "grace",
      createdAt: 10,
    },
    {
      id: "legacy_love",
      passage: { translation: "kjv", book: 43, chapter: 3 },
      verse: 16,
      start: null,
      end: null,
      quote: "For God so loved the world.",
      reference: "John 3:16",
      colorId: "biblical-love",
      createdAt: 20,
    },
  ];

  const parsed = parseBookmarkBackup(backup);

  assert.equal(parsed.bookmarks.length, 1);
  assert.equal(parsed.bookmarks[0].id, "legacy_love");
  assert.deepEqual(parsed.bookmarks[0].topic_ids, [
    "biblical-love",
    "grace",
  ]);
});

test("imports version-three topic identifiers into one multi-topic verse", () => {
  const backup = scopedStore(new MemoryStorage()).backup(
    "2026-08-20T10:00:00.000Z",
  );
  backup.version = 3;
  backup.markings = [{
    id: "v3_multi",
    passage: { translation: "kjv", book: 43, chapter: 3 },
    verse: 16,
    start: null,
    end: null,
    quote: "For God so loved the world.",
    reference: "John 3:16",
    colorIds: ["grace", "biblical-love"],
    createdAt: 20,
  }];

  const parsed = parseBookmarkBackup(backup);

  assert.equal(parsed.bookmarks.length, 1);
  assert.deepEqual(parsed.bookmarks[0].topic_ids, [
    "grace",
    "biblical-love",
  ]);
});

test("merges restored topic membership into an existing verse", () => {
  const source = scopedStore(new MemoryStorage());
  source.apply(verse(), "biblical-love");
  const backup = source.backup("2026-08-20T10:00:00.000Z");
  const restored = scopedStore(new MemoryStorage(), "b".repeat(64));
  restored.apply(verse(), "grace");

  const first = restored.importBackup(backup);
  const repeated = restored.importBackup(backup);

  assert.equal(first.bookmarks_added, 1);
  assert.equal(first.conflicts_skipped, 0);
  assert.deepEqual(restored.bookmarkFor(verse())?.topic_ids, [
    "grace",
    "biblical-love",
  ]);
  assert.equal(restored.size, 1);
  assert.equal(repeated.bookmarks_added, 0);
  assert.equal(repeated.conflicts_skipped, 1);
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
    bookName: "John",
    colorIndexes: [backup.colors.findIndex((topic) => topic.id === "grace")],
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

test("validates portable backup timestamps, marking ids, and note objects", () => {
  const source = scopedStore(new MemoryStorage());
  source.apply(verse(), "grace");
  const backup = source.backup("2026-08-20T10:00:00.000Z");

  for (const exportedAt of [
    "2026-08-20T10:00:00",
    "2026-02-30T10:00:00Z",
    "0001-01-01T00:00:00+23:59",
    "x".repeat(65),
  ]) {
    assert.throws(
      () => parseBookmarkBackup({ ...backup, exportedAt }),
      /date/i,
    );
  }

  const duplicate = structuredClone(backup);
  duplicate.markings.push(structuredClone(duplicate.markings[0]));
  duplicate.markings[1].verse = 17;
  assert.throws(() => parseBookmarkBackup(duplicate), /entries/i);

  const invalidNotes = structuredClone(backup);
  invalidNotes.notes = ["not-an-object"];
  assert.throws(() => parseBookmarkBackup(invalidNotes), /notes/i);

  const optionalRange = structuredClone(backup);
  delete optionalRange.markings[0].start;
  delete optionalRange.markings[0].end;
  assert.equal(parseBookmarkBackup(optionalRange).bookmarks.length, 1);
});

test("round-trips the worst-case UTF-8 v4 backup under four MiB", () => {
  const scope = "c".repeat(64);
  const storageKey = `${BOOKMARK_STORAGE_PREFIX}:${scope}`;
  const maximumName = "漢".repeat(80);
  const maximumBookName = "漢".repeat(128);
  const maximumText = "漢".repeat(BOOKMARK_TEXT_MAX_CHARS);
  const fixedId = (prefix, index) => {
    const lead = `${prefix}${String(index).padStart(3, "0")}`;
    return `${lead}${"x".repeat(128 - lead.length)}`;
  };
  const topics = Array.from({ length: 100 }, (_, index) => ({
    id: fixedId("t", index),
    name: maximumName,
    color: "#abcdef",
  }));
  const topicIds = topics.map((topic) => topic.id);
  const bookmarks = Array.from({ length: MAX_BOOKMARKS }, (_, index) => ({
    id: fixedId("b", index),
    topic_ids: [...topicIds],
    topic_id: topicIds[0],
    translation: "a".repeat(30),
    reference: `${maximumBookName} 1000:${index + 1}`,
    book: 200,
    book_name: maximumBookName,
    chapter: 1_000,
    verse: index + 1,
    text: maximumText,
    created_at: Number.MAX_SAFE_INTEGER,
    updated_at: Number.MAX_SAFE_INTEGER,
  }));
  const sourceStorage = new MemoryStorage();
  sourceStorage.setItem(storageKey, JSON.stringify({
    version: 2,
    active_topic_id: topicIds[0],
    record_updated_at: 1,
    topics,
    bookmarks,
  }));
  const source = new BookmarkStore({ scope, storage: sourceStorage });

  const backup = source.backup("2026-08-20T10:00:00.000Z");
  const pretty = JSON.stringify(backup, null, 2);
  const byteLength = new TextEncoder().encode(pretty).byteLength;

  assert.equal(backup.version, 4);
  assert.deepEqual(backup.markings[0].colorIndexes, [...Array(100).keys()]);
  assert.ok(
    byteLength <= BOOKMARK_BACKUP_MAX_BYTES,
    `Worst-case v4 backup is ${byteLength} bytes.`,
  );

  const targetStorage = new MemoryStorage();
  targetStorage.setItem(storageKey, JSON.stringify({
    version: 2,
    active_topic_id: topicIds[0],
    record_updated_at: 1,
    topics,
    bookmarks: [],
  }));
  const target = new BookmarkStore({ scope, storage: targetStorage });
  const restored = target.importBackup(JSON.parse(pretty), { byteLength });

  assert.equal(restored.bookmarks_added, MAX_BOOKMARKS);
  assert.equal(target.size, MAX_BOOKMARKS);
  assert.equal(target.bookmarkFor({ book: 200, chapter: 1_000, verse: 1 })
    .topic_ids.length, 100);
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
  orphan.markings[0].colorIndexes = [orphan.colors.length];
  assert.throws(() => bookmarks.importBackup(orphan), /entries/i);
  const mixedV4 = structuredClone(backup);
  mixedV4.markings[0].colorIds = ["grace"];
  assert.throws(() => bookmarks.importBackup(mixedV4), /entries/i);
  const mixedV2 = structuredClone(backup);
  mixedV2.version = 2;
  mixedV2.markings[0].colorId = "grace";
  assert.throws(() => bookmarks.importBackup(mixedV2), /entries/i);
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
  assert.deepEqual(compacted.bookmarkFor(verse())?.topic_ids, [
    "biblical-love",
    "grace",
  ]);
  assert.equal(
    JSON.parse(storage.getItem(key)).version,
    2,
  );

  const futureStorage = new MemoryStorage();
  futureStorage.setItem(key, JSON.stringify({ version: 3 }));
  const future = scopedStore(futureStorage);
  assert.equal(future.persistent, false);
  assert.equal(future.size, 0);
  assert.equal(JSON.parse(futureStorage.getItem(key)).version, 3);

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
  snapshot.bookmarks[0].topic_ids.push("biblical-love");
  snapshot.bookmarks.push(verse());

  assert.equal(bookmarks.topic("adultery")?.name, "Adultery");
  assert.equal(bookmarks.bookmarkFor(verse())?.reference, "John 3:16");
  assert.deepEqual(bookmarks.bookmarkFor(verse())?.topic_ids, ["grace"]);
  assert.equal(bookmarks.size, 1);
});
