const STORAGE_PREFIX = "getbible.miniapp.bookmarks.v1";
const STORAGE_VERSION = 1;
const BACKUP_VERSION = 2;
const MAX_TOPICS = 100;
// Telegram CloudStorage is limited to 1,024 keys per bot/user. Reserving one
// record per verse, up to 100 topic keys, and a manifest leaves useful room for
// future user settings without risking a quota-edge write.
const MAX_BOOKMARK_ENTRIES = 800;
const MAX_BACKUP_BYTES = 4 * 1024 * 1024;
// One complete bookmark, including its escaped JSON wrapper, must fit inside
// Telegram CloudStorage's 4,096-character value limit.
const MAX_BOOKMARK_TEXT = 1_024;
const MAX_IMPORTED_BOOKMARK_TEXT = 3_000;
const TELEGRAM_USER_ID_MAXIMUM = (2 ** 52) - 1;
const ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const SCOPE_PATTERN = /^[a-f0-9]{64}$/;
const TRANSLATION_PATTERN = /^[a-z0-9][a-z0-9_-]{0,29}$/;
const COLOR_PATTERN = /^#[a-f0-9]{6}$/;
const FORBIDDEN_CONTROL_PATTERN = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/;
const FORBIDDEN_CONTROL_GLOBAL_PATTERN = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g;

const DEPLOYMENT_TOPICS = [
  ["Adultery", "#f9a8b8"],
  ["Authority of the Bible", "#93c5fd"],
  ["Baptism", "#7dd3fc"],
  ["Biblical Love", "#f9a8d4"],
  ["Blessings and Curses", "#fde68a"],
  ["Christian Clothing", "#d8b4fe"],
  ["Christian Offices", "#a5b4fc"],
  ["Communion", "#fca5a5"],
  ["Conditional Security", "#fdba74"],
  ["Dating", "#f0abfc"],
  ["Dietary Guidance", "#bef264"],
  ["Discipline", "#fcd34d"],
  ["Education", "#67e8f9"],
  ["Effective Prayer", "#c4b5fd"],
  ["Family Planning", "#fda4af"],
  ["First Day", "#fed7aa"],
  ["Flattery", "#fecdd3"],
  ["Free Will", "#99f6e4"],
  ["God's Judgment", "#fb7185"],
  ["Grace", "#bbf7d0"],
  ["Home Church", "#86efac"],
  ["Immutability", "#bfdbfe"],
  ["Jesus Christ's Deity", "#c7d2fe"],
  ["Jesus Christ's Humanity", "#fecaca"],
  ["Leadership", "#a7f3d0"],
  ["Longevity", "#d9f99d"],
  ["Man's Role", "#bae6fd"],
  ["Marriage", "#f5b7d2"],
  ["Music's Influence", "#e9d5ff"],
  ["No Fellowship", "#fdc4a8"],
  ["No One is Good", "#fda4a4"],
  ["Non-Resistance", "#a7f3e8"],
  ["Not Under the Law", "#ddd6fe"],
  ["Obey God's Commandments", "#fde047"],
  ["Obey Government Laws", "#cbd5e1"],
  ["Omnipotent", "#818cf8"],
  ["Omnipresent", "#60a5fa"],
  ["Omniscient", "#a78bfa"],
  ["Orderly Home", "#b7e4c7"],
  ["Ordinances", "#67d6e8"],
  ["Prince of this World", "#c4a7a7"],
  ["Providence", "#8dd3c7"],
  ["Renewing of the Mind", "#a5f3fc"],
  ["Repentance", "#fbbf8a"],
  ["Saved by Faith", "#86efac"],
  ["Sodomy", "#f59e9e"],
  ["Spirit of Prophecy", "#d8b4fe"],
  ["Spiritual Gifts", "#c084fc"],
  ["Spiritual Judgment", "#f0c36e"],
  ["Spiritual Rebirth", "#6ee7b7"],
  ["Temptation", "#fca5a5"],
  ["What is Life", "#5eead4"],
  ["Wine", "#e8a0bf"],
  ["Wisdom Cause", "#fef08a"],
  ["Wisdom Fruit", "#bef264"],
  ["Wisdom Origin", "#fcd34d"],
  ["Wisdom Value", "#fbbf24"],
  ["Woman's Role", "#fbcfe8"],
  ["Word of God", "#7dd3fc"],
  ["Worldly Wisdom", "#d6d3d1"],
];

export const DEFAULT_BOOKMARK_TOPICS = Object.freeze(
  DEPLOYMENT_TOPICS.map(([name, color]) => Object.freeze({
    id: topicSlug(name),
    name,
    color,
  })),
);

export const BOOKMARK_TOPIC_COLORS = Object.freeze(
  [...new Set(DEFAULT_BOOKMARK_TOPICS.map((topic) => topic.color))],
);

export class BookmarkBackupError extends Error {
  constructor(message) {
    super(message);
    this.name = "BookmarkBackupError";
  }
}

export class BookmarkStore {
  #activeTopicId;
  #bookmarks;
  #clock;
  #idFactory;
  #key;
  #persistent;
  #recordUpdatedAt;
  #storage;
  #topics;

  constructor({
    scope,
    storage = browserLocalStorage(),
    clock = Date.now,
    idFactory = defaultIdentifier,
  } = {}) {
    if (typeof scope !== "string" || !SCOPE_PATTERN.test(scope)) {
      throw new TypeError("An authenticated bookmark storage scope is required.");
    }
    if (typeof clock !== "function" || typeof idFactory !== "function") {
      throw new TypeError("Bookmark store dependencies are invalid.");
    }
    this.#key = `${STORAGE_PREFIX}:${scope}`;
    this.#storage = storageLike(storage) ? storage : null;
    this.#persistent = Boolean(this.#storage);
    this.#clock = clock;
    this.#idFactory = idFactory;
    const record = this.#read();
    this.#topics = record.topics;
    this.#bookmarks = record.bookmarks;
    this.#activeTopicId = record.active_topic_id;
    this.#recordUpdatedAt = record.record_updated_at;
  }

  get activeTopicId() {
    return this.#activeTopicId;
  }

  get persistent() {
    return this.#persistent;
  }

  get size() {
    return this.#bookmarks.length;
  }

  get topicCount() {
    return this.#topics.length;
  }

  snapshot() {
    return {
      active_topic_id: this.#activeTopicId,
      topics: this.#topics.map(cloneTopic),
      bookmarks: this.#bookmarks.map(cloneBookmark),
    };
  }

  topic(id) {
    const topic = this.#topics.find((item) => item.id === id);
    return topic ? cloneTopic(topic) : null;
  }

  bookmarkFor(verse) {
    const coordinates = normalizeCoordinates(verse);
    const bookmark = this.#bookmarks.find((item) =>
      sameVerse(item, coordinates)
    );
    return bookmark ? cloneBookmark(bookmark) : null;
  }

  bookmarksForTopic(topicId) {
    return this.#bookmarks
      .filter((bookmark) => bookmark.topic_id === topicId)
      .sort(compareBookmarks)
      .map(cloneBookmark);
  }

  topicUsage(topicId) {
    return this.#bookmarks.filter(
      (bookmark) => bookmark.topic_id === topicId,
    ).length;
  }

  setActiveTopic(topicId) {
    this.#requireTopic(topicId);
    if (this.#activeTopicId === topicId) {
      return false;
    }
    this.#activeTopicId = topicId;
    this.#persist();
    return true;
  }

  apply(verse, topicId = this.#activeTopicId) {
    this.#requireTopic(topicId);
    const now = validTimestamp(this.#clock());
    const normalized = normalizeBookmark({
      ...verse,
      text: bookmarkExcerpt(verse?.text),
      id: this.#idFactory("bookmark"),
      topic_id: topicId,
      created_at: now,
      updated_at: now,
    });
    const existing = this.#bookmarks.find((bookmark) =>
      sameVerse(bookmark, normalized)
    );
    if (!existing && this.#bookmarks.length >= MAX_BOOKMARK_ENTRIES) {
      throw new RangeError("The bookmark limit has been reached.");
    }
    if (existing) {
      normalized.id = existing.id;
      normalized.created_at = existing.created_at;
    }
    this.#bookmarks = this.#bookmarks.filter(
      (bookmark) => !sameVerse(bookmark, normalized),
    );
    this.#bookmarks.push(normalized);
    this.#activeTopicId = topicId;
    this.#persist(now);
    return cloneBookmark(normalized);
  }

  removeBookmark(id) {
    if (typeof id !== "string" || !ID_PATTERN.test(id)) {
      return false;
    }
    const next = this.#bookmarks.filter((bookmark) => bookmark.id !== id);
    if (next.length === this.#bookmarks.length) {
      return false;
    }
    this.#bookmarks = next;
    this.#persist();
    return true;
  }

  removeVerse(verse) {
    const coordinates = normalizeCoordinates(verse);
    const next = this.#bookmarks.filter(
      (bookmark) => !sameVerse(bookmark, coordinates),
    );
    if (next.length === this.#bookmarks.length) {
      return false;
    }
    this.#bookmarks = next;
    this.#persist();
    return true;
  }

  clearBookmarks() {
    if (this.#bookmarks.length === 0) {
      return false;
    }
    this.#bookmarks = [];
    this.#persist();
    return true;
  }

  addTopic(name, color = "#fde68a") {
    if (this.#topics.length >= MAX_TOPICS) {
      throw new RangeError("The bookmark topic limit has been reached.");
    }
    let id = this.#idFactory("topic");
    let suffix = 1;
    while (!ID_PATTERN.test(id) || this.#topics.some((topic) => topic.id === id)) {
      id = `${topicSlug(name)}-${suffix++}`;
    }
    const topic = normalizeTopic({ id, name, color });
    this.#topics.push(topic);
    this.#activeTopicId = topic.id;
    this.#persist();
    return cloneTopic(topic);
  }

  updateTopic(id, changes) {
    const index = this.#topics.findIndex((topic) => topic.id === id);
    if (index < 0) {
      throw new TypeError("Bookmark topic was not found.");
    }
    const topic = normalizeTopic({
      ...this.#topics[index],
      name: changes?.name ?? this.#topics[index].name,
      color: changes?.color ?? this.#topics[index].color,
    });
    this.#topics[index] = topic;
    this.#persist();
    return cloneTopic(topic);
  }

  removeTopic(id) {
    if (this.#topics.length === 1) {
      return false;
    }
    const index = this.#topics.findIndex((topic) => topic.id === id);
    if (index < 0) {
      return false;
    }
    const removedBookmarks = this.topicUsage(id);
    this.#topics.splice(index, 1);
    this.#bookmarks = this.#bookmarks.filter(
      (bookmark) => bookmark.topic_id !== id,
    );
    if (this.#activeTopicId === id) {
      this.#activeTopicId = this.#topics[0].id;
    }
    this.#persist();
    return { removed_bookmarks: removedBookmarks };
  }

  backup(exportedAt = new Date(this.#clock()).toISOString()) {
    if (typeof exportedAt !== "string" || !Number.isFinite(Date.parse(exportedAt))) {
      throw new TypeError("A valid bookmark backup date is required.");
    }
    return {
      format: "getbible-life-markings",
      version: BACKUP_VERSION,
      exportedAt,
      colors: this.#topics.map((topic) => ({
        id: topic.id,
        name: topic.name,
        value: topic.color,
      })),
      markings: this.#bookmarks.map((bookmark) => ({
        id: bookmark.id,
        passage: {
          translation: bookmark.translation,
          book: bookmark.book,
          chapter: bookmark.chapter,
        },
        verse: bookmark.verse,
        start: null,
        end: null,
        quote: bookmark.text,
        reference: bookmark.reference,
        colorId: bookmark.topic_id,
        createdAt: bookmark.created_at,
      })),
      notes: [],
    };
  }

  importBackup(value, { byteLength = null } = {}) {
    const imported = parseBookmarkBackup(value, { byteLength });
    const nextTopics = this.#topics.map(cloneTopic);
    const topicIds = new Set(nextTopics.map((topic) => topic.id));
    let topicsAdded = 0;
    for (const topic of imported.topics) {
      if (!topicIds.has(topic.id)) {
        if (nextTopics.length >= MAX_TOPICS) {
          throw new BookmarkBackupError("The imported topics exceed the limit.");
        }
        nextTopics.push(topic);
        topicIds.add(topic.id);
        topicsAdded += 1;
      }
    }

    const nextBookmarks = this.#bookmarks.map(cloneBookmark);
    const coordinates = new Set(nextBookmarks.map(bookmarkKey));
    const bookmarkIds = new Set(nextBookmarks.map((bookmark) => bookmark.id));
    let bookmarksAdded = 0;
    let conflictsSkipped = 0;
    for (const importedBookmark of imported.bookmarks) {
      if (!topicIds.has(importedBookmark.topic_id)) {
        throw new BookmarkBackupError("The backup contains an unknown topic.");
      }
      const coordinate = bookmarkKey(importedBookmark);
      if (coordinates.has(coordinate)) {
        conflictsSkipped += 1;
        continue;
      }
      if (nextBookmarks.length >= MAX_BOOKMARK_ENTRIES) {
        throw new BookmarkBackupError("The imported bookmarks exceed the limit.");
      }
      const bookmark = cloneBookmark(importedBookmark);
      let suffix = 1;
      const originalId = bookmark.id;
      while (bookmarkIds.has(bookmark.id)) {
        bookmark.id = `${originalId.slice(0, 108)}-imported-${suffix++}`;
      }
      nextBookmarks.push(bookmark);
      coordinates.add(coordinate);
      bookmarkIds.add(bookmark.id);
      bookmarksAdded += 1;
    }

    this.#topics = nextTopics;
    this.#bookmarks = nextBookmarks;
    if (!topicIds.has(this.#activeTopicId)) {
      this.#activeTopicId = nextTopics[0].id;
    }
    this.#persist();
    return {
      topics_added: topicsAdded,
      bookmarks_added: bookmarksAdded,
      conflicts_skipped: conflictsSkipped,
      range_markings_skipped: imported.range_markings_skipped,
      notes_skipped: imported.notes_skipped,
    };
  }

  #read() {
    const fresh = freshRecord();
    if (!this.#storage) {
      return fresh;
    }
    let raw;
    try {
      raw = this.#storage.getItem(this.#key);
    } catch {
      this.#disablePersistence();
      return fresh;
    }
    if (!raw) {
      return fresh;
    }
    try {
      const value = JSON.parse(raw);
      if (
        value &&
        typeof value === "object" &&
        !Array.isArray(value) &&
        Number.isInteger(value.version) &&
        value.version > STORAGE_VERSION
      ) {
        this.#disablePersistence();
        return fresh;
      }
      if (
        !value ||
        typeof value !== "object" ||
        Array.isArray(value) ||
        value.version !== STORAGE_VERSION ||
        !Array.isArray(value.topics) ||
        !Array.isArray(value.bookmarks) ||
        value.topics.length < 1 ||
        value.topics.length > MAX_TOPICS ||
        value.bookmarks.length > MAX_BOOKMARK_ENTRIES
      ) {
        throw new TypeError("Bookmark record is invalid.");
      }
      const topics = uniqueTopics(value.topics.map(normalizeTopic));
      if (topics.length === 0) {
        throw new TypeError("Bookmark record has no topics.");
      }
      const topicIds = new Set(topics.map((topic) => topic.id));
      const bookmarks = compactBookmarks(
        value.bookmarks.map(normalizeBookmark),
      ).filter((bookmark) => topicIds.has(bookmark.topic_id));
      const activeTopicId = topicIds.has(value.active_topic_id)
        ? value.active_topic_id
        : topics[0].id;
      const recordUpdatedAt = Object.hasOwn(value, "record_updated_at")
        ? validTimestamp(value.record_updated_at)
        : 0;
      const record = {
        version: STORAGE_VERSION,
        active_topic_id: activeTopicId,
        record_updated_at: recordUpdatedAt,
        topics,
        bookmarks,
      };
      if (
        topics.length !== value.topics.length ||
        bookmarks.length !== value.bookmarks.length ||
        activeTopicId !== value.active_topic_id
      ) {
        this.#write(record);
      }
      return record;
    } catch {
      try {
        this.#storage.removeItem(this.#key);
      } catch {
        this.#disablePersistence();
      }
      return fresh;
    }
  }

  #persist(updatedAt = null) {
    const nextUpdatedAt = updatedAt === null
      ? validTimestamp(this.#clock())
      : validTimestamp(updatedAt);
    this.#recordUpdatedAt = Math.max(
      this.#recordUpdatedAt + 1,
      nextUpdatedAt,
    );
    this.#write({
      version: STORAGE_VERSION,
      active_topic_id: this.#activeTopicId,
      record_updated_at: this.#recordUpdatedAt,
      topics: this.#topics,
      bookmarks: this.#bookmarks,
    });
  }

  #write(record) {
    if (!this.#storage) {
      return;
    }
    try {
      this.#storage.setItem(this.#key, JSON.stringify(record));
    } catch {
      this.#disablePersistence();
    }
  }

  #disablePersistence() {
    this.#storage = null;
    this.#persistent = false;
  }

  #requireTopic(topicId) {
    if (!this.#topics.some((topic) => topic.id === topicId)) {
      throw new TypeError("Bookmark topic was not found.");
    }
  }
}

export async function bookmarkStorageScope(
  authenticatedUserId,
  cryptoProvider = globalThis.crypto,
) {
  const userId = Number(authenticatedUserId);
  if (
    !Number.isSafeInteger(userId) ||
    userId < 1 ||
    userId > TELEGRAM_USER_ID_MAXIMUM
  ) {
    throw new TypeError("An authenticated Telegram user is required.");
  }
  if (!cryptoProvider?.subtle?.digest) {
    throw new TypeError("Secure browser hashing is unavailable.");
  }
  const bytes = new TextEncoder().encode(
    `getbible.miniapp.bookmarks.v1\u0000${userId}`,
  );
  const digest = await cryptoProvider.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export function parseBookmarkBackup(value, { byteLength = null } = {}) {
  if (
    byteLength !== null &&
    (!Number.isSafeInteger(byteLength) || byteLength < 0 || byteLength > MAX_BACKUP_BYTES)
  ) {
    throw new BookmarkBackupError("The bookmark backup is too large.");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new BookmarkBackupError("This is not a bookmark backup.");
  }
  if (
    (value.version !== 1 && value.version !== BACKUP_VERSION) ||
    !Array.isArray(value.colors) ||
    !Array.isArray(value.markings) ||
    value.colors.length < 1 ||
    value.colors.length > MAX_TOPICS ||
    value.markings.length > MAX_BOOKMARK_ENTRIES
  ) {
    throw new BookmarkBackupError("This bookmark backup format is unsupported.");
  }
  if (
    Object.hasOwn(value, "format") &&
    value.format !== "getbible-life-markings"
  ) {
    throw new BookmarkBackupError("This bookmark backup format is unsupported.");
  }
  if (
    typeof value.exportedAt !== "string" ||
    !Number.isFinite(Date.parse(value.exportedAt))
  ) {
    throw new BookmarkBackupError("The bookmark backup date is invalid.");
  }

  let topics;
  try {
    topics = uniqueTopics(value.colors.map((color) => normalizeTopic({
      id: color?.id,
      name: color?.name,
      color: color?.value,
    })));
  } catch {
    throw new BookmarkBackupError("The bookmark backup topics are invalid.");
  }
  if (topics.length !== value.colors.length) {
    throw new BookmarkBackupError("The bookmark backup contains duplicate topics.");
  }
  const topicIds = new Set(topics.map((topic) => topic.id));
  const bookmarks = [];
  let rangeMarkingsSkipped = 0;
  try {
    for (const marking of value.markings) {
      const hasRange = marking?.start !== null || marking?.end !== null;
      if (hasRange) {
        if (
          !Number.isInteger(marking?.start) ||
          !Number.isInteger(marking?.end) ||
          marking.start < 0 ||
          marking.end <= marking.start
        ) {
          throw new TypeError("Invalid range marking.");
        }
        rangeMarkingsSkipped += 1;
        continue;
      }
      const bookmark = normalizeBookmark({
        id: marking?.id,
        translation: marking?.passage?.translation,
        book: marking?.passage?.book,
        book_name: referenceBookName(marking?.reference),
        chapter: marking?.passage?.chapter,
        verse: marking?.verse,
        reference: marking?.reference,
        text: bookmarkExcerpt(
          boundedText(marking?.quote, MAX_IMPORTED_BOOKMARK_TEXT),
        ),
        topic_id: marking?.colorId,
        created_at: marking?.createdAt,
        updated_at: marking?.createdAt,
      });
      if (!topicIds.has(bookmark.topic_id)) {
        throw new TypeError("Orphan bookmark.");
      }
      bookmarks.push(bookmark);
    }
  } catch {
    throw new BookmarkBackupError("The bookmark backup entries are invalid.");
  }

  const notes = value.notes ?? [];
  if (!Array.isArray(notes) || notes.length > MAX_BOOKMARK_ENTRIES) {
    throw new BookmarkBackupError("The bookmark backup notes are invalid.");
  }
  return {
    topics,
    bookmarks: compactBookmarks(bookmarks),
    range_markings_skipped: rangeMarkingsSkipped,
    notes_skipped: notes.length,
  };
}

export const BOOKMARK_STORAGE_PREFIX = STORAGE_PREFIX;
export const BOOKMARK_BACKUP_MAX_BYTES = MAX_BACKUP_BYTES;
export const MAX_BOOKMARK_TOPICS = MAX_TOPICS;
export const MAX_BOOKMARKS = MAX_BOOKMARK_ENTRIES;
export const BOOKMARK_TEXT_MAX_CHARS = MAX_BOOKMARK_TEXT;

function freshRecord() {
  const topics = DEFAULT_BOOKMARK_TOPICS.map(cloneTopic);
  return {
    version: STORAGE_VERSION,
    active_topic_id: topics[0].id,
    record_updated_at: 0,
    topics,
    bookmarks: [],
  };
}

function normalizeTopic(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("Bookmark topic is invalid.");
  }
  const id = boundedText(value.id, 128);
  const name = boundedText(value.name, 80).normalize("NFC");
  const color = boundedText(value.color, 7).toLowerCase();
  if (!ID_PATTERN.test(id) || !COLOR_PATTERN.test(color)) {
    throw new TypeError("Bookmark topic is invalid.");
  }
  return { id, name, color };
}

function normalizeBookmark(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("Bookmark is invalid.");
  }
  const id = boundedText(value.id, 128);
  const topicId = boundedText(value.topic_id, 128);
  const translation = boundedText(value.translation, 30).toLowerCase();
  const reference = boundedText(value.reference, 180).normalize("NFC");
  const bookName = boundedText(value.book_name, 128).normalize("NFC");
  const text = boundedText(value.text, MAX_BOOKMARK_TEXT).normalize("NFC");
  const coordinates = normalizeCoordinates(value);
  const createdAt = validTimestamp(value.created_at);
  const updatedAt = validTimestamp(value.updated_at ?? value.created_at);
  if (
    !ID_PATTERN.test(id) ||
    !ID_PATTERN.test(topicId) ||
    !TRANSLATION_PATTERN.test(translation) ||
    updatedAt < createdAt
  ) {
    throw new TypeError("Bookmark is invalid.");
  }
  return {
    id,
    topic_id: topicId,
    translation,
    reference,
    book: coordinates.book,
    book_name: bookName,
    chapter: coordinates.chapter,
    verse: coordinates.verse,
    text,
    created_at: createdAt,
    updated_at: updatedAt,
  };
}

function normalizeCoordinates(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("Bookmark coordinates are invalid.");
  }
  return {
    book: boundedInteger(value.book ?? value.book_number, 1, 200),
    chapter: boundedInteger(value.chapter, 1, 1_000),
    verse: boundedInteger(value.verse, 1, 2_000),
  };
}

function validTimestamp(value) {
  const timestamp = Number(value);
  if (!Number.isSafeInteger(timestamp) || timestamp < 0) {
    throw new TypeError("Bookmark timestamp is invalid.");
  }
  return timestamp;
}

function boundedInteger(value, minimum, maximum) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < minimum || number > maximum) {
    throw new TypeError("Bookmark coordinate is invalid.");
  }
  return number;
}

function boundedText(value, maximum) {
  if (typeof value !== "string") {
    throw new TypeError("Bookmark text is invalid.");
  }
  const normalized = value.trim();
  if (
    normalized.length === 0 ||
    normalized.length > maximum ||
    FORBIDDEN_CONTROL_PATTERN.test(normalized) ||
    hasUnpairedSurrogate(normalized)
  ) {
    throw new TypeError("Bookmark text is invalid.");
  }
  return normalized;
}

function bookmarkExcerpt(value) {
  if (typeof value !== "string") {
    return value;
  }
  let excerpt = replaceUnpairedSurrogates(value)
    .replace(FORBIDDEN_CONTROL_GLOBAL_PATTERN, " ")
    .trim()
    .slice(0, MAX_BOOKMARK_TEXT);
  // Do not persist a dangling UTF-16 surrogate when a long verse is clipped.
  if (/[\ud800-\udbff]$/.test(excerpt)) {
    excerpt = excerpt.slice(0, -1);
  }
  return excerpt;
}

function hasUnpairedSurrogate(value) {
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const following = value.charCodeAt(index + 1);
      if (following < 0xdc00 || following > 0xdfff) {
        return true;
      }
      index += 1;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      return true;
    }
  }
  return false;
}

function replaceUnpairedSurrogates(value) {
  let result = "";
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const following = value.charCodeAt(index + 1);
      if (following >= 0xdc00 && following <= 0xdfff) {
        result += value[index] + value[index + 1];
        index += 1;
      } else {
        result += "\ufffd";
      }
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      result += "\ufffd";
    } else {
      result += value[index];
    }
  }
  return result;
}

function topicSlug(name) {
  return String(name)
    .toLowerCase()
    .replace(/['’]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

function defaultIdentifier(prefix) {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (typeof uuid === "string" && ID_PATTERN.test(uuid)) {
    return uuid;
  }
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 12)}`;
}

function uniqueTopics(topics) {
  const seen = new Set();
  return topics.filter((topic) => {
    if (seen.has(topic.id)) {
      return false;
    }
    seen.add(topic.id);
    return true;
  });
}

function compactBookmarks(bookmarks) {
  const newest = new Map();
  for (const bookmark of bookmarks) {
    const key = bookmarkKey(bookmark);
    const current = newest.get(key);
    if (!current || bookmark.updated_at >= current.updated_at) {
      newest.set(key, bookmark);
    }
  }
  return [...newest.values()];
}

function bookmarkKey(bookmark) {
  return `${bookmark.book}/${bookmark.chapter}/${bookmark.verse}`;
}

function sameVerse(left, right) {
  return bookmarkKey(left) === bookmarkKey(right);
}

function compareBookmarks(left, right) {
  return (
    left.book - right.book ||
    left.chapter - right.chapter ||
    left.verse - right.verse ||
    left.created_at - right.created_at
  );
}

function cloneTopic(topic) {
  return { ...topic };
}

function cloneBookmark(bookmark) {
  return { ...bookmark };
}

function referenceBookName(reference) {
  const normalized = boundedText(reference, 180).normalize("NFC");
  const match = normalized.match(/^(.+?)\s+\d{1,4}:\d{1,4}$/u);
  if (!match) {
    throw new TypeError("Bookmark reference is invalid.");
  }
  return boundedText(match[1], 128);
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
