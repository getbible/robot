const STORAGE_KEY = "getbible.miniapp.global-bookmarks.v2";
const STORAGE_VERSION = 2;
const TOPIC_MAPPING_PREFIX = "getbible.miniapp.global-topic-map.v1";
const TOPIC_MAPPING_VERSION = 1;
const MAX_ENABLED_TOPICS = 100;
const FALLBACK_MAX_HIDDEN_BOOKMARKS = 10_000;
const TOPIC_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const SCOPE_PATTERN = /^[a-f0-9]{64}$/;
const GLOBAL_BOOKMARK_ID_PATTERN =
  /^global_([A-Za-z0-9_-]{1,128})_([1-9]\d{0,1})_([1-9]\d{0,3})_([1-9]\d{0,3})$/;

/**
 * Persists only which bundled global topics are visible on this device.
 *
 * The catalogue itself stays in the application's static assets. The supplied
 * storage adapter scopes both preferences and the canonical-to-local mapping
 * to the authenticated user and may mirror them through Telegram
 * DeviceStorage. Neither record enters CloudStorage or backups.
 */
export class GlobalBookmarkPreferences {
  #allowedBookmarkIds;
  #allowedTopicIds;
  #catalogVersion = 0;
  #enabledTopicIds = new Set();
  #hiddenBookmarkIds = new Set();
  #mappingKey = null;
  #mappingPersistent = false;
  #maxHiddenBookmarks;
  #persistent;
  #storage;
  #topicMappings = new Map();

  constructor({
    allowedBookmarkIds = null,
    allowedTopicIds = null,
    scope = null,
    storage = browserLocalStorage(),
  } = {}) {
    this.#allowedTopicIds = allowedTopicIds === null
      ? null
      : new Set(normalizeTopicIds(allowedTopicIds));
    const normalizedBookmarkIds = allowedBookmarkIds === null
      ? null
      : normalizeBookmarkIds(
        allowedBookmarkIds,
        this.#allowedTopicIds,
        FALLBACK_MAX_HIDDEN_BOOKMARKS,
      );
    this.#allowedBookmarkIds = normalizedBookmarkIds === null
      ? null
      : new Set(normalizedBookmarkIds.map((bookmark) => bookmark.id));
    this.#maxHiddenBookmarks = this.#allowedBookmarkIds?.size ??
      FALLBACK_MAX_HIDDEN_BOOKMARKS;
    this.#storage = storageLike(storage) ? storage : null;
    this.#persistent = Boolean(this.#storage);
    this.#mappingKey = typeof scope === "string" && SCOPE_PATTERN.test(scope)
      ? `${TOPIC_MAPPING_PREFIX}:${scope}`
      : null;
    this.#mappingPersistent = Boolean(this.#storage && this.#mappingKey);
    this.#readPreferences();
    this.#readTopicMappings();
  }

  get catalogVersion() {
    return this.#catalogVersion;
  }

  get enabled() {
    return this.#enabledTopicIds.size > 0;
  }

  get enabledTopicIds() {
    return [...this.#enabledTopicIds].sort();
  }

  get hiddenBookmarkIds() {
    return [...this.#hiddenBookmarkIds].sort();
  }

  get persistent() {
    return this.#persistent;
  }

  get topicMappings() {
    return Object.fromEntries([...this.#topicMappings].sort(([left], [right]) =>
      left.localeCompare(right)
    ));
  }

  async flush() {
    if (typeof this.#storage?.flush === "function") {
      await this.#storage.flush();
    }
  }

  setTopicMappings(value, localTopics) {
    const localTopicIds = normalizeLocalTopicIds(localTopics);
    const mappings = normalizeTopicMappings(value, this.#allowedTopicIds);
    let changed = false;
    for (const [canonicalTopicId, localTopicId] of mappings) {
      if (!localTopicIds.has(localTopicId)) {
        continue;
      }
      if (this.#topicMappings.get(canonicalTopicId) !== localTopicId) {
        this.#topicMappings.set(canonicalTopicId, localTopicId);
        changed = true;
      }
    }
    if (this.#pruneTopicMappings(localTopicIds)) {
      changed = true;
    }
    if (changed) {
      this.#writeTopicMappings();
    }
    return changed;
  }

  pruneTopicMappings(localTopics) {
    const changed = this.#pruneTopicMappings(normalizeLocalTopicIds(localTopics));
    if (changed) {
      this.#writeTopicMappings();
    }
    return changed;
  }

  hasTopic(topicId) {
    const normalized = normalizeTopicId(topicId);
    return (
      (!this.#allowedTopicIds || this.#allowedTopicIds.has(normalized)) &&
      this.#enabledTopicIds.has(normalized)
    );
  }

  isBookmarkHidden(bookmarkId) {
    const normalized = normalizeAllowedBookmarkId(
      bookmarkId,
      this.#allowedTopicIds,
      this.#allowedBookmarkIds,
    );
    return this.#hiddenBookmarkIds.has(normalized.id);
  }

  hideBookmark(bookmarkId) {
    const normalized = normalizeAllowedBookmarkId(
      bookmarkId,
      this.#allowedTopicIds,
      this.#allowedBookmarkIds,
    );
    if (this.#hiddenBookmarkIds.has(normalized.id)) {
      return false;
    }
    if (this.#hiddenBookmarkIds.size >= this.#maxHiddenBookmarks) {
      throw new RangeError("Too many global bookmark exclusions are stored.");
    }
    this.#hiddenBookmarkIds.add(normalized.id);
    this.#write();
    return true;
  }

  restoreTopic(topicId) {
    const normalized = normalizeAllowedTopicId(topicId, this.#allowedTopicIds);
    const changed = this.#restoreTopics(new Set([normalized]));
    if (changed) {
      this.#write();
    }
    return changed;
  }

  enableTopic(topicId, catalogVersion) {
    return this.enableTopics([topicId], catalogVersion);
  }

  enableTopics(topicIds, catalogVersion) {
    const normalizedIds = normalizeTopicIds(topicIds);
    const normalizedVersion = normalizeCatalogVersion(catalogVersion);
    if (
      this.#allowedTopicIds &&
      normalizedIds.some((topicId) => !this.#allowedTopicIds.has(topicId))
    ) {
      throw new TypeError("A global bookmark topic preference is unknown.");
    }
    let changed = this.#catalogVersion !== normalizedVersion;
    for (const topicId of normalizedIds) {
      if (!this.#enabledTopicIds.has(topicId)) {
        this.#enabledTopicIds.add(topicId);
        changed = true;
      }
    }
    if (this.#restoreTopics(new Set(normalizedIds))) {
      changed = true;
    }
    this.#catalogVersion = normalizedVersion;
    this.#write();
    return changed;
  }

  disableTopic(topicId) {
    const changed = this.#enabledTopicIds.delete(normalizeTopicId(topicId));
    if (changed) {
      this.#write();
    }
    return changed;
  }

  clear() {
    const changed = this.#enabledTopicIds.size > 0;
    this.#enabledTopicIds.clear();
    if (changed) {
      this.#write();
    }
    return changed;
  }

  #readPreferences() {
    if (!this.#storage) {
      return;
    }
    let raw;
    try {
      raw = this.#storage.getItem(STORAGE_KEY);
    } catch {
      this.#disablePersistence();
      return;
    }
    if (!raw) {
      return;
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
        return;
      }
      if (
        !value ||
        typeof value !== "object" ||
        Array.isArray(value) ||
        value.version !== STORAGE_VERSION ||
        !Number.isSafeInteger(value.catalog_version) ||
        value.catalog_version < 1
      ) {
        throw new TypeError("Global bookmark preferences are invalid.");
      }
      const enabledTopicIds = normalizeTopicIds(value.enabled_topic_ids);
      const hiddenBookmarks = normalizeBookmarkIds(
        value.hidden_bookmark_ids ?? [],
        null,
        FALLBACK_MAX_HIDDEN_BOOKMARKS,
      );
      const retainedTopicIds = this.#allowedTopicIds
        ? enabledTopicIds.filter((topicId) => this.#allowedTopicIds.has(topicId))
        : enabledTopicIds;
      const retainedHiddenBookmarks = hiddenBookmarks.filter((bookmark) =>
        (!this.#allowedTopicIds || this.#allowedTopicIds.has(bookmark.topicId)) &&
        (!this.#allowedBookmarkIds || this.#allowedBookmarkIds.has(bookmark.id))
      );
      this.#catalogVersion = value.catalog_version;
      this.#enabledTopicIds = new Set(retainedTopicIds);
      this.#hiddenBookmarkIds = new Set(
        retainedHiddenBookmarks.map((bookmark) => bookmark.id),
      );
      if (
        retainedTopicIds.length !== enabledTopicIds.length ||
        retainedHiddenBookmarks.length !== hiddenBookmarks.length
      ) {
        this.#write();
      }
    } catch {
      try {
        this.#storage.removeItem(STORAGE_KEY);
      } catch {
        this.#disablePersistence();
      }
    }
  }

  #readTopicMappings() {
    if (!this.#storage || !this.#mappingKey) {
      return;
    }
    let raw;
    try {
      raw = this.#storage.getItem(this.#mappingKey);
    } catch {
      this.#mappingPersistent = false;
      return;
    }
    if (!raw) {
      return;
    }
    try {
      const value = JSON.parse(raw);
      if (
        value &&
        typeof value === "object" &&
        !Array.isArray(value) &&
        Number.isInteger(value.version) &&
        value.version > TOPIC_MAPPING_VERSION
      ) {
        this.#mappingPersistent = false;
        return;
      }
      if (
        !value ||
        typeof value !== "object" ||
        Array.isArray(value) ||
        value.version !== TOPIC_MAPPING_VERSION
      ) {
        throw new TypeError("Global topic mappings are invalid.");
      }
      this.#topicMappings = normalizeTopicMappings(
        value.topic_ids,
        this.#allowedTopicIds,
      );
    } catch {
      try {
        this.#storage.removeItem(this.#mappingKey);
      } catch {
        this.#mappingPersistent = false;
      }
    }
  }

  #write() {
    if (!this.#storage) {
      return;
    }
    try {
      this.#storage.setItem(STORAGE_KEY, JSON.stringify({
        version: STORAGE_VERSION,
        catalog_version: this.#catalogVersion,
        enabled_topic_ids: this.enabledTopicIds,
        hidden_bookmark_ids: this.hiddenBookmarkIds,
      }));
    } catch {
      this.#disablePersistence();
    }
  }

  #disablePersistence() {
    this.#storage = null;
    this.#persistent = false;
    this.#mappingPersistent = false;
  }

  #writeTopicMappings() {
    if (!this.#storage || !this.#mappingKey || !this.#mappingPersistent) {
      return;
    }
    try {
      this.#storage.setItem(this.#mappingKey, JSON.stringify({
        version: TOPIC_MAPPING_VERSION,
        topic_ids: this.topicMappings,
      }));
    } catch {
      this.#mappingPersistent = false;
    }
  }

  #pruneTopicMappings(localTopicIds) {
    let changed = false;
    for (const [canonicalTopicId, localTopicId] of this.#topicMappings) {
      if (
        !localTopicIds.has(localTopicId) ||
        (this.#allowedTopicIds && !this.#allowedTopicIds.has(canonicalTopicId))
      ) {
        this.#topicMappings.delete(canonicalTopicId);
        changed = true;
      }
    }
    return changed;
  }

  #restoreTopics(topicIds) {
    let changed = false;
    for (const bookmarkId of this.#hiddenBookmarkIds) {
      if (topicIds.has(normalizeBookmarkId(bookmarkId).topicId)) {
        this.#hiddenBookmarkIds.delete(bookmarkId);
        changed = true;
      }
    }
    return changed;
  }
}

export const GLOBAL_BOOKMARK_PREFERENCES_KEY = STORAGE_KEY;
export const GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX = TOPIC_MAPPING_PREFIX;

function normalizeCatalogVersion(value) {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new TypeError("The global bookmark catalogue version is invalid.");
  }
  return value;
}

function normalizeTopicIds(value) {
  if (!Array.isArray(value) || value.length > MAX_ENABLED_TOPICS) {
    throw new TypeError("Global bookmark topic preferences are invalid.");
  }
  return [...new Set(value.map(normalizeTopicId))].sort();
}

function normalizeTopicId(value) {
  if (typeof value !== "string" || !TOPIC_ID_PATTERN.test(value)) {
    throw new TypeError("A global bookmark topic preference is invalid.");
  }
  return value;
}

function normalizeLocalTopicIds(value) {
  if (!Array.isArray(value) || value.length > MAX_ENABLED_TOPICS) {
    throw new TypeError("Local bookmark topics are invalid.");
  }
  return new Set(value.map((topic) => normalizeTopicId(topic?.id)));
}

function normalizeTopicMappings(value, allowedTopicIds = null) {
  const entries = value instanceof Map
    ? [...value]
    : value && typeof value === "object" && !Array.isArray(value)
      ? Object.entries(value)
      : null;
  if (!entries || entries.length > MAX_ENABLED_TOPICS) {
    throw new TypeError("Global topic mappings are invalid.");
  }
  const mappings = new Map();
  const localTopicIds = new Set();
  for (const [canonicalValue, localValue] of entries) {
    const canonicalTopicId = normalizeTopicId(canonicalValue);
    const localTopicId = normalizeTopicId(localValue);
    if (
      (allowedTopicIds && !allowedTopicIds.has(canonicalTopicId)) ||
      mappings.has(canonicalTopicId) ||
      localTopicIds.has(localTopicId)
    ) {
      throw new TypeError("Global topic mappings are invalid.");
    }
    mappings.set(canonicalTopicId, localTopicId);
    localTopicIds.add(localTopicId);
  }
  return mappings;
}

function normalizeAllowedTopicId(value, allowedTopicIds) {
  const normalized = normalizeTopicId(value);
  if (allowedTopicIds && !allowedTopicIds.has(normalized)) {
    throw new TypeError("A global bookmark topic preference is unknown.");
  }
  return normalized;
}

function normalizeBookmarkIds(
  value,
  allowedTopicIds = null,
  maximum = FALLBACK_MAX_HIDDEN_BOOKMARKS,
) {
  if (
    !Array.isArray(value) ||
    !Number.isSafeInteger(maximum) ||
    maximum < 0 ||
    value.length > maximum
  ) {
    throw new TypeError("Global bookmark exclusions are invalid.");
  }
  const normalized = value.map((bookmarkId) =>
    normalizeBookmarkId(bookmarkId, allowedTopicIds)
  );
  const unique = new Map(normalized.map((bookmark) => [bookmark.id, bookmark]));
  return [...unique.values()].sort((left, right) =>
    left.id.localeCompare(right.id)
  );
}

function normalizeAllowedBookmarkId(
  value,
  allowedTopicIds,
  allowedBookmarkIds,
) {
  const normalized = normalizeBookmarkId(value, allowedTopicIds);
  if (allowedBookmarkIds && !allowedBookmarkIds.has(normalized.id)) {
    throw new TypeError("A global bookmark exclusion is unknown.");
  }
  return normalized;
}

function normalizeBookmarkId(value, allowedTopicIds = null) {
  if (typeof value !== "string") {
    throw new TypeError("A global bookmark exclusion is invalid.");
  }
  const match = GLOBAL_BOOKMARK_ID_PATTERN.exec(value);
  if (!match) {
    throw new TypeError("A global bookmark exclusion is invalid.");
  }
  const topicId = normalizeAllowedTopicId(match[1], allowedTopicIds);
  const book = Number.parseInt(match[2], 10);
  const chapter = Number.parseInt(match[3], 10);
  const verse = Number.parseInt(match[4], 10);
  if (book > 66 || chapter > 1_000 || verse > 2_000) {
    throw new TypeError("A global bookmark exclusion is invalid.");
  }
  return {
    id: `global_${topicId}_${book}_${chapter}_${verse}`,
    topicId,
  };
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
