import { isMiniAppInstanceScope } from "./instance-scope.js";

const STORAGE_KEY = "getbible.miniapp.global-bookmarks.v2";
const STORAGE_VERSION = 4;
const TOPIC_MAPPING_PREFIX = "getbible.miniapp.global-topic-map.v1";
const TOPIC_MAPPING_VERSION = 2;
const MAX_ENABLED_TOPICS = 100;
const MAX_MAPPING_PATCH_TOPIC_IDS = 1_000;
const FALLBACK_MAX_HIDDEN_BOOKMARKS = 10_000;
// One coverage entry per personal verse at most: the personal store itself
// never holds more than this many verses.
const MAX_COVERAGE_ENTRIES = 800;
const COVERAGE_KEY_PATTERN = /^([1-9]\d?):([1-9]\d{0,2}):([1-9]\d{0,3})$/;
const MAX_RECORD_TIMESTAMP = 2 ** 46;
const TOPIC_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const SCOPE_PATTERN = /^[a-f0-9]{64}$/;
const GLOBAL_BOOKMARK_ID_PATTERN =
  /^global_([A-Za-z0-9_-]{1,128})_([1-9]\d{0,1})_([1-9]\d{0,3})_([1-9]\d{0,3})$/;

/**
 * Persists only which global topics are visible on this device.
 *
 * The catalogue itself comes from the public Bookmarks API and is cached
 * separately. The supplied storage adapter scopes both preferences and the
 * canonical-to-local mapping to the authenticated user and may mirror them
 * through Telegram DeviceStorage. Neither record enters CloudStorage or
 * backups. Besides visibility the record remembers two things about the
 * catalogue's relationship to personal storage: which catalogue version
 * seeded the default topics, and since when each personal verse coordinate
 * has been covered by an enabled global topic, so a personal bookmark is only
 * merged away once the shared row has stood beside it for a day.
 */
export class GlobalBookmarkPreferences {
  #allowedBookmarkIds;
  #allowedTopicIds;
  #catalogVersion = 0;
  #contributionTopicMappings = new Map();
  #coverage = new Map();
  #disabledTopicIds = new Set();
  #enabledTopicIds = new Set();
  #hiddenBookmarkIds = new Set();
  #lockManager;
  #mappingKey = null;
  #mappingLockName = null;
  #mappingPersistent = false;
  #maxHiddenBookmarks;
  #persistent;
  #promotedTopicIds = new Set();
  #promotionCatalogVersion = 0;
  #seededCatalogVersion = null;
  #storage;
  #topicMappings = new Map();

  constructor({
    allowedBookmarkIds = null,
    allowedTopicIds = null,
    instanceScope,
    lockManager = browserLockManager(),
    scope = null,
    storage = browserLocalStorage(),
  } = {}) {
    if (!isMiniAppInstanceScope(instanceScope)) {
      throw new TypeError("A Mini App instance scope is required.");
    }
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
    this.#lockManager = lockManagerLike(lockManager) ? lockManager : null;
    this.#mappingKey = typeof scope === "string" && SCOPE_PATTERN.test(scope)
      ? `${TOPIC_MAPPING_PREFIX}:${instanceScope}:${scope}`
      : null;
    this.#mappingLockName = this.#mappingKey
      ? `${this.#mappingKey}:write`
      : null;
    this.#mappingPersistent = Boolean(this.#storage && this.#mappingKey);
    this.#readPreferences();
    this.#readTopicMappings();
  }

  get catalogVersion() {
    return this.#catalogVersion;
  }

  get coverage() {
    return Object.fromEntries(
      [...this.#coverage].sort(([left], [right]) => left.localeCompare(right)),
    );
  }

  get seededCatalogVersion() {
    return this.#seededCatalogVersion;
  }

  get enabled() {
    return this.enabledTopicIds.length > 0;
  }

  get enabledTopicIds() {
    const enabledTopicIds = new Set(
      [...this.#enabledTopicIds].filter((topicId) =>
        !this.#disabledTopicIds.has(topicId)
      ),
    );
    for (const topicId of this.#promotedTopicIds) {
      if (
        !this.#disabledTopicIds.has(topicId) &&
        (!this.#allowedTopicIds || this.#allowedTopicIds.has(topicId))
      ) {
        enabledTopicIds.add(topicId);
      }
    }
    return [...enabledTopicIds].sort();
  }

  get hiddenBookmarkIds() {
    return [...this.#hiddenBookmarkIds].sort();
  }

  get persistent() {
    return this.#persistent;
  }

  get topicMappings() {
    const mappings = new Map(this.#topicMappings);
    for (const [canonicalTopicId, localTopicId] of this.#contributionTopicMappings) {
      for (const [ordinaryCanonicalTopicId, ordinaryLocalTopicId] of mappings) {
        if (ordinaryLocalTopicId === localTopicId) {
          mappings.delete(ordinaryCanonicalTopicId);
        }
      }
      mappings.set(canonicalTopicId, localTopicId);
    }
    return Object.fromEntries([...mappings].sort(([left], [right]) =>
      left.localeCompare(right)
    ));
  }

  get contributionTopicMappings() {
    return Object.fromEntries(
      [...this.#contributionTopicMappings].sort(([left], [right]) =>
        left.localeCompare(right)
      ),
    );
  }

  get promotedTopicIds() {
    return [...this.#promotedTopicIds].sort();
  }

  async flush() {
    if (typeof this.#storage?.flush === "function") {
      await this.#storage.flush();
    }
  }

  async setTopicMappings(value, localTopics) {
    const localTopicIds = normalizeLocalTopicIds(localTopics);
    const mappings = normalizeTopicMappings(value, this.#allowedTopicIds);
    return this.#withFreshMappingLock(() => {
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
    });
  }

  async replaceTopicMappings(value, localTopics) {
    const localTopicIds = normalizeLocalTopicIds(localTopics);
    const mappings = normalizeTopicMappings(value, this.#allowedTopicIds);
    if ([...mappings.values()].some((localTopicId) =>
      !localTopicIds.has(localTopicId)
    )) {
      throw new TypeError("A global topic mapping references a missing topic.");
    }
    return this.#withFreshMappingLock(() => {
      if (mapsEqual(mappings, this.#topicMappings)) {
        return false;
      }
      this.#topicMappings = mappings;
      this.#writeTopicMappings();
      return true;
    });
  }

  /**
   * Applies one authoritative contributor-review patch without replacing
   * ordinary catalogue mappings or contribution mappings written by another
   * same-origin client for unrelated local topics.
   */
  async reconcileContributionTopicMappings({
    clear = false,
    mappings: value = {},
    promotedTopicIds = [],
    replacedLocalTopicIds = [],
    catalogVersion,
    guard = null,
  } = {}, localTopics) {
    if (guard !== null && typeof guard !== "function") {
      throw new TypeError("A global topic mapping guard is invalid.");
    }
    const localTopicIds = normalizeLocalTopicIds(localTopics);
    const mappings = normalizeTopicMappings(value, this.#allowedTopicIds);
    if ([...mappings.values()].some((localTopicId) =>
      !localTopicIds.has(localTopicId)
    )) {
      throw new TypeError("A global topic mapping references a missing topic.");
    }
    const replacedIds = new Set(
      normalizeMappingPatchTopicIds(replacedLocalTopicIds),
    );
    const promotionIds = normalizeTopicIds(promotedTopicIds).map((topicId) =>
      normalizeAllowedTopicId(topicId, this.#allowedTopicIds)
    );
    const normalizedCatalogVersion = promotionIds.length > 0
      ? normalizeCatalogVersion(catalogVersion)
      : null;
    const operation = async () => {
      if (guard && !guard()) {
        return {
          changed: false,
          enabled: 0,
          mappingChanged: false,
          newlyPromotedTopicIds: [],
          stale: true,
        };
      }
      const nextMappings = clear
        ? new Map()
        : new Map(this.#contributionTopicMappings);
      if (!clear) {
        for (const [canonicalTopicId, localTopicId] of nextMappings) {
          if (
            replacedIds.has(localTopicId) ||
            mappings.has(canonicalTopicId)
          ) {
            nextMappings.delete(canonicalTopicId);
          }
        }
        for (const [canonicalTopicId, localTopicId] of mappings) {
          nextMappings.set(canonicalTopicId, localTopicId);
        }
      }
      const mappingChanged = !mapsEqual(
        this.#contributionTopicMappings,
        nextMappings,
      );
      const newlyPromotedTopicIds = promotionIds.filter((topicId) =>
        !this.#promotedTopicIds.has(topicId)
      );
      const enabled = newlyPromotedTopicIds.filter((topicId) =>
        !this.#disabledTopicIds.has(topicId) &&
        !this.#enabledTopicIds.has(topicId)
      ).length;
      const previousMappings = this.#contributionTopicMappings;
      const previousPromotedTopicIds = this.#promotedTopicIds;
      const previousPromotionCatalogVersion = this.#promotionCatalogVersion;
      const previousCatalogVersion = this.#catalogVersion;
      this.#contributionTopicMappings = nextMappings;
      this.#promotedTopicIds = new Set(this.#promotedTopicIds);
      for (const topicId of newlyPromotedTopicIds) {
        this.#promotedTopicIds.add(topicId);
      }
      const promotionChanged = newlyPromotedTopicIds.length > 0;
      try {
        if (promotionChanged) {
          this.#promotionCatalogVersion = normalizedCatalogVersion;
          this.#catalogVersion = Math.max(
            this.#catalogVersion,
            normalizedCatalogVersion,
          );
        }
        if (mappingChanged || promotionChanged) {
          this.#writeTopicMappings();
        }
        if (mappingChanged || promotionChanged) {
          await this.#ensureContributionMappingDurable({
            requirePreferences: newlyPromotedTopicIds.some((topicId) =>
              this.#disabledTopicIds.has(topicId)
            ),
          });
        }
      } catch (error) {
        this.#contributionTopicMappings = previousMappings;
        this.#promotedTopicIds = previousPromotedTopicIds;
        this.#promotionCatalogVersion = previousPromotionCatalogVersion;
        this.#catalogVersion = previousCatalogVersion;
        throw error;
      }
      return {
        changed: mappingChanged || promotionChanged || enabled > 0,
        enabled,
        mappingChanged,
        newlyPromotedTopicIds,
      };
    };
    return this.#withFreshMappingLock(operation);
  }

  async pruneTopicMappings(localTopics) {
    const localTopicIds = normalizeLocalTopicIds(localTopics);
    return this.#withFreshMappingLock(() => {
      const changed = this.#pruneTopicMappings(localTopicIds);
      if (changed) {
        this.#writeTopicMappings();
      }
      return changed;
    });
  }

  hasTopic(topicId) {
    const normalized = normalizeTopicId(topicId);
    return (
      (!this.#allowedTopicIds || this.#allowedTopicIds.has(normalized)) &&
      !this.#disabledTopicIds.has(normalized) &&
      (
        this.#enabledTopicIds.has(normalized) ||
        this.#promotedTopicIds.has(normalized)
      )
    );
  }

  hasPromotedTopic(topicId) {
    return this.#promotedTopicIds.has(normalizeTopicId(topicId));
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
      if (this.#disabledTopicIds.delete(topicId)) {
        changed = true;
      }
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
    const normalized = normalizeAllowedTopicId(topicId, this.#allowedTopicIds);
    const wasEnabled = this.hasTopic(normalized);
    const changed = this.#enabledTopicIds.delete(normalized) ||
      !this.#disabledTopicIds.has(normalized);
    this.#disabledTopicIds.add(normalized);
    if (changed) {
      this.#write();
    }
    return wasEnabled;
  }

  clear() {
    const effectiveTopicIds = this.enabledTopicIds;
    const changed = effectiveTopicIds.length > 0;
    for (const topicId of effectiveTopicIds) {
      this.#disabledTopicIds.add(topicId);
    }
    this.#enabledTopicIds.clear();
    if (changed) {
      this.#write();
    }
    return changed;
  }

  /**
   * Records which personal verse coordinates ("book:chapter:verse") an
   * enabled global topic currently covers. A coordinate seen for the first
   * time is stamped with `now`; one no longer covered is forgotten, so its
   * clock restarts if it is covered again later.
   */
  recordCoverage(keys, now = Date.now()) {
    const covered = normalizeCoverageKeys(keys);
    const firstSeenAt = normalizeRecordTimestamp(now);
    let changed = false;
    for (const key of [...this.#coverage.keys()]) {
      if (!covered.has(key)) {
        this.#coverage.delete(key);
        changed = true;
      }
    }
    for (const key of covered) {
      if (!this.#coverage.has(key)) {
        this.#coverage.set(key, firstSeenAt);
        changed = true;
      }
    }
    if (changed) {
      this.#write();
    }
    return changed;
  }

  /** Coordinates whose recorded coverage is at least `minAgeMs` old. */
  stableCoverage(now = Date.now(), minAgeMs = 0) {
    const at = normalizeRecordTimestamp(now);
    if (!Number.isSafeInteger(minAgeMs) || minAgeMs < 0) {
      throw new TypeError("The coverage age is invalid.");
    }
    const stable = new Set();
    for (const [key, firstSeenAt] of this.#coverage) {
      if (at - firstSeenAt >= minAgeMs) {
        stable.add(key);
      }
    }
    return stable;
  }

  /** Remembers that the catalogue's default topics were offered once. */
  markSeeded(catalogVersion) {
    const normalized = normalizeCatalogVersion(catalogVersion);
    if (this.#seededCatalogVersion === normalized) {
      return false;
    }
    this.#seededCatalogVersion = normalized;
    this.#write();
    return true;
  }

  #readPreferences({ replaceCurrent = false } = {}) {
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
      if (replaceCurrent) {
        this.#catalogVersion = 0;
        this.#enabledTopicIds = new Set();
        this.#disabledTopicIds = new Set();
        this.#hiddenBookmarkIds = new Set();
        this.#coverage = new Map();
        this.#seededCatalogVersion = null;
      }
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
        ![2, 3, STORAGE_VERSION].includes(value.version) ||
        !Number.isSafeInteger(value.catalog_version) ||
        value.catalog_version < 0
      ) {
        throw new TypeError("Global bookmark preferences are invalid.");
      }
      const enabledTopicIds = normalizeTopicIds(value.enabled_topic_ids);
      const disabledTopicIds = value.version >= 3
        ? normalizeTopicIds(value.disabled_topic_ids ?? [])
        : [];
      // A version 3 record upgrades in place: it simply has no coverage and
      // no seeding record yet, and is rewritten as version 4 on its next write.
      const coverage = value.version === STORAGE_VERSION
        ? normalizeStoredCoverage(value.coverage)
        : new Map();
      const seededCatalogVersion = value.version === STORAGE_VERSION
        ? normalizeStoredSeededVersion(value.seeded_catalog_version)
        : null;
      const hiddenBookmarks = normalizeBookmarkIds(
        value.hidden_bookmark_ids ?? [],
        null,
        FALLBACK_MAX_HIDDEN_BOOKMARKS,
      );
      const retainedTopicIds = this.#allowedTopicIds
        ? enabledTopicIds.filter((topicId) => this.#allowedTopicIds.has(topicId))
        : enabledTopicIds;
      const retainedDisabledTopicIds = this.#allowedTopicIds
        ? disabledTopicIds.filter((topicId) => this.#allowedTopicIds.has(topicId))
        : disabledTopicIds;
      const retainedHiddenBookmarks = hiddenBookmarks.filter((bookmark) =>
        (!this.#allowedTopicIds || this.#allowedTopicIds.has(bookmark.topicId)) &&
        (!this.#allowedBookmarkIds || this.#allowedBookmarkIds.has(bookmark.id))
      );
      this.#catalogVersion = value.catalog_version;
      this.#enabledTopicIds = new Set(retainedTopicIds);
      this.#disabledTopicIds = new Set(retainedDisabledTopicIds);
      this.#hiddenBookmarkIds = new Set(
        retainedHiddenBookmarks.map((bookmark) => bookmark.id),
      );
      this.#coverage = coverage;
      this.#seededCatalogVersion = seededCatalogVersion;
      if (
        retainedTopicIds.length !== enabledTopicIds.length ||
        retainedDisabledTopicIds.length !== disabledTopicIds.length ||
        retainedHiddenBookmarks.length !== hiddenBookmarks.length
      ) {
        if (!replaceCurrent) {
          this.#write();
        }
      }
    } catch {
      try {
        this.#storage.removeItem(STORAGE_KEY);
      } catch {
        this.#disablePersistence();
      }
    }
  }

  #readTopicMappings({ replaceCurrent = false } = {}) {
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
      if (replaceCurrent) {
        this.#topicMappings = new Map();
        this.#contributionTopicMappings = new Map();
        this.#promotedTopicIds = new Set();
        this.#promotionCatalogVersion = 0;
      }
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
        ![1, TOPIC_MAPPING_VERSION].includes(value.version)
      ) {
        throw new TypeError("Global topic mappings are invalid.");
      }
      const mappings = normalizeTopicMappings(value.topic_ids);
      const contributionMappings = value.version === TOPIC_MAPPING_VERSION
        ? normalizeTopicMappings(value.contribution_topic_ids)
        : new Map();
      const promotedTopicIds = value.version === TOPIC_MAPPING_VERSION
        ? new Set(normalizeTopicIds(value.promoted_topic_ids))
        : new Set();
      const promotionCatalogVersion = value.version === TOPIC_MAPPING_VERSION
        ? normalizeStoredCatalogVersion(value.catalog_version ?? 0)
        : 0;
      this.#topicMappings = this.#allowedTopicIds
        ? new Map([...mappings].filter(([canonicalTopicId]) =>
          this.#allowedTopicIds.has(canonicalTopicId)
        ))
        : mappings;
      // Contributor mappings and first-promotion provenance are retained even
      // while a validated catalogue temporarily omits their canonical topic.
      // They are inert until that canonical definition is present again.
      this.#contributionTopicMappings = contributionMappings;
      this.#promotedTopicIds = promotedTopicIds;
      this.#promotionCatalogVersion = promotionCatalogVersion;
      this.#catalogVersion = Math.max(
        this.#catalogVersion,
        promotionCatalogVersion,
      );
      // Migration/filtering is persisted by the next lock-protected mapping
      // mutation. Constructors remain read-only so another WebView cannot be
      // overwritten between its own fresh read and write.
    } catch {
      if (replaceCurrent) {
        this.#topicMappings = new Map();
        this.#contributionTopicMappings = new Map();
        this.#promotedTopicIds = new Set();
        this.#promotionCatalogVersion = 0;
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
        enabled_topic_ids: [...this.#enabledTopicIds].sort(),
        disabled_topic_ids: [...this.#disabledTopicIds].sort(),
        hidden_bookmark_ids: this.hiddenBookmarkIds,
        seeded_catalog_version: this.#seededCatalogVersion,
        coverage: this.coverage,
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
      throw new TypeError("Global topic mappings are not persistent.");
    }
    this.#storage.setItem(this.#mappingKey, JSON.stringify({
      version: TOPIC_MAPPING_VERSION,
      catalog_version: this.#promotionCatalogVersion,
      topic_ids: Object.fromEntries(
        [...this.#topicMappings].sort(([left], [right]) =>
          left.localeCompare(right)
        ),
      ),
      contribution_topic_ids: this.contributionTopicMappings,
      promoted_topic_ids: this.promotedTopicIds,
    }));
  }

  #withFreshMappingLock(operation) {
    const run = async () => {
      if (this.#storage && this.#mappingKey) {
        await this.#storage.refreshItem?.(STORAGE_KEY);
        await this.#storage.refreshItem?.(this.#mappingKey);
        await this.#requireDurableStorageItem(STORAGE_KEY);
        await this.#requireDurableStorageItem(this.#mappingKey);
        this.#readPreferences({ replaceCurrent: true });
        this.#readTopicMappings({ replaceCurrent: true });
        if (!this.#mappingPersistent) {
          throw new TypeError(
            "Global topic mappings require a newer application.",
          );
        }
      }
      if (this.#mappingKey && !this.#mappingPersistent) {
        throw new TypeError("Global topic mappings are not persistent.");
      }
      return operation();
    };
    return this.#lockManager && this.#mappingLockName
      ? this.#lockManager.request(
        this.#mappingLockName,
        { mode: "exclusive" },
        run,
      )
      : run();
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

  async #ensureContributionMappingDurable({ requirePreferences = false } = {}) {
    if (!this.#storage || !this.#mappingKey || !this.#mappingPersistent) {
      throw new TypeError("Global topic mappings are not persistent.");
    }
    if (typeof this.#storage.flush !== "function") {
      return;
    }
    const keys = [
      this.#mappingKey,
      ...(requirePreferences ? [STORAGE_KEY] : []),
    ];
    await this.#storage.flush();
    if (typeof this.#storage.isItemDurable !== "function") {
      return;
    }
    let missingKeys = keys.filter((key) => !this.#storage.isItemDurable(key));
    for (const key of missingKeys) {
      this.#storage.retryItem?.(key);
    }
    if (missingKeys.length > 0) {
      await this.#storage.flush();
      missingKeys = keys.filter((key) => !this.#storage.isItemDurable(key));
    }
    if (missingKeys.length > 0) {
      for (const key of missingKeys) {
        this.#storage.restoreDurableItem?.(key);
      }
      throw new Error("Global bookmark preferences could not be persisted.");
    }
  }

  async #requireDurableStorageItem(key) {
    if (
      typeof this.#storage?.isItemDurable !== "function" ||
      this.#storage.getItem(key) === null ||
      this.#storage.isItemDurable(key)
    ) {
      return;
    }
    this.#storage.retryItem?.(key);
    await this.#storage.flush?.();
    if (!this.#storage.isItemDurable(key)) {
      this.#storage.restoreDurableItem?.(key);
      throw new Error("Global bookmark preferences could not be persisted.");
    }
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

function normalizeStoredCatalogVersion(value) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError("The global bookmark catalogue version is invalid.");
  }
  return value;
}

function normalizeStoredSeededVersion(value) {
  return Number.isSafeInteger(value) && value >= 1 ? value : null;
}

function normalizeRecordTimestamp(value) {
  if (!Number.isFinite(value) || value < 0 || value > MAX_RECORD_TIMESTAMP) {
    throw new TypeError("The coverage timestamp is invalid.");
  }
  return Math.floor(value);
}

function normalizeCoverageKey(value) {
  if (typeof value !== "string") {
    return null;
  }
  const match = COVERAGE_KEY_PATTERN.exec(value);
  if (
    !match ||
    Number.parseInt(match[1], 10) > 66 ||
    Number.parseInt(match[2], 10) > 150 ||
    Number.parseInt(match[3], 10) > 2_000
  ) {
    return null;
  }
  return value;
}

function normalizeCoverageKeys(value) {
  if (
    !value ||
    typeof value[Symbol.iterator] !== "function" ||
    typeof value === "string"
  ) {
    throw new TypeError("Global bookmark coverage keys are invalid.");
  }
  const keys = new Set();
  for (const candidate of value) {
    const key = normalizeCoverageKey(candidate);
    if (key === null) {
      throw new TypeError("A global bookmark coverage key is invalid.");
    }
    keys.add(key);
  }
  if (keys.size > MAX_COVERAGE_ENTRIES) {
    throw new RangeError("Too many global bookmark coverage entries.");
  }
  return keys;
}

/**
 * A stored coverage map is advisory: an entry this client cannot read is
 * dropped rather than costing the reader every other preference, and the
 * oldest entries win when a record somehow exceeds the bound.
 */
function normalizeStoredCoverage(value) {
  if (value === undefined || value === null) {
    return new Map();
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    return new Map();
  }
  const entries = [];
  for (const [candidate, firstSeenAt] of Object.entries(value)) {
    const key = normalizeCoverageKey(candidate);
    if (
      key === null ||
      !Number.isSafeInteger(firstSeenAt) ||
      firstSeenAt < 0 ||
      firstSeenAt > MAX_RECORD_TIMESTAMP
    ) {
      continue;
    }
    entries.push([key, firstSeenAt]);
  }
  entries.sort((left, right) => left[1] - right[1] || left[0].localeCompare(right[0]));
  return new Map(entries.slice(0, MAX_COVERAGE_ENTRIES));
}

function normalizeTopicIds(value) {
  if (!Array.isArray(value) || value.length > MAX_ENABLED_TOPICS) {
    throw new TypeError("Global bookmark topic preferences are invalid.");
  }
  return [...new Set(value.map(normalizeTopicId))].sort();
}

function normalizeMappingPatchTopicIds(value) {
  if (!Array.isArray(value) || value.length > MAX_MAPPING_PATCH_TOPIC_IDS) {
    throw new TypeError("Global topic mapping patch ids are invalid.");
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

function lockManagerLike(value) {
  return Boolean(value && typeof value.request === "function");
}

function browserLockManager() {
  try {
    return lockManagerLike(globalThis.navigator?.locks)
      ? globalThis.navigator.locks
      : null;
  } catch {
    return null;
  }
}

function mapsEqual(left, right) {
  return left.size === right.size && [...left].every(
    ([key, value]) => right.get(key) === value,
  );
}

function browserLocalStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
