import {
  BOOKMARK_TEXT_MAX_CHARS,
  BOOKMARK_STORAGE_PREFIX,
  MAX_BOOKMARKS,
  MAX_BOOKMARK_TOPICS,
  MAX_RECENT_BOOKMARK_TOPICS,
} from "./bookmark-store.js";

const SCOPE_PATTERN = /^[a-f0-9]{64}$/;
const CLOUD_KEY_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const TRANSLATION_PATTERN = /^[a-z0-9][a-z0-9_-]{0,29}$/;
const COLOR_PATTERN = /^#[a-f0-9]{6}$/;
const FORBIDDEN_CONTROL_PATTERN = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/;
const CLOUD_API_VERSION = "6.9";
const DEVICE_API_VERSION = "9.0";
const CLOUD_VALUE_LIMIT = 4_096;
const CLOUD_KEY_LIMIT = 1_024;
const CLOUD_WRITE_CONCURRENCY = 8;
const CLOUD_READ_BATCH_SIZE = 100;
const BOOKMARK_SYNC_MAX_ATTEMPTS = 3;
const DEFAULT_TIMEOUT_MS = 2_500;
const LAST_READ_STORAGE_PREFIX = "getbible.miniapp.last-read.v1";
const STORAGE_UNKNOWN = Symbol("telegram-storage-unknown");
const BOOKMARK_AGGREGATE_VERSION = 3;

export const TELEGRAM_BOOKMARK_CLOUD_MAX_KEYS =
  1 + MAX_BOOKMARK_TOPICS + MAX_BOOKMARKS;
export const TELEGRAM_CLOUD_VALUE_MAX_CHARS = CLOUD_VALUE_LIMIT;
export const TELEGRAM_LAST_READ_STORAGE_PREFIX = LAST_READ_STORAGE_PREFIX;

/**
 * Hydrates BookmarkStore's synchronous aggregate from local and Telegram
 * storage, then mirrors later synchronous writes back to Telegram.
 */
export class TelegramBookmarkStorage {
  #aggregateKey;
  #cloud;
  #cloudRoot;
  #cloudSnapshot = new Map();
  #completedSequence = 0;
  #desired = null;
  #device;
  #deviceKey;
  #deviceRaw = null;
  #lastRead = null;
  #lastReadHydrated = false;
  #lastReadRecordUpdatedAt = 0;
  #lastReadCloudRaw = STORAGE_UNKNOWN;
  #lastReadCompletedSequence = 0;
  #lastReadDesired = null;
  #lastReadDeviceRaw = STORAGE_UNKNOWN;
  #lastReadKey;
  #lastReadSequence = 0;
  #lastReadSyncPromise = null;
  #lastReadTask = null;
  #lastReadWaiters = [];
  #listener;
  #local;
  #memoryRaw = null;
  #sequence = 0;
  #status;
  #syncPromise = null;
  #timeoutMs;

  static async open({
    scope,
    webApp = globalThis.Telegram?.WebApp ?? null,
    localStorage = browserLocalStorage(),
    timeoutMs = DEFAULT_TIMEOUT_MS,
    onStatus = null,
    hydrateLastRead = false,
  } = {}) {
    const storage = new TelegramBookmarkStorage({
      scope,
      webApp,
      localStorage,
      timeoutMs,
      onStatus,
    });
    await Promise.all([
      storage.#hydrate(),
      hydrateLastRead ? storage.readLastRead() : Promise.resolve(null),
    ]);
    return storage;
  }

  constructor({ scope, webApp, localStorage, timeoutMs, onStatus }) {
    if (typeof scope !== "string" || !SCOPE_PATTERN.test(scope)) {
      throw new TypeError("An authenticated bookmark storage scope is required.");
    }
    if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 30_000) {
      throw new TypeError("A valid Telegram storage timeout is required.");
    }
    if (onStatus !== null && typeof onStatus !== "function") {
      throw new TypeError("The Telegram storage status listener is invalid.");
    }

    this.#aggregateKey = `${BOOKMARK_STORAGE_PREFIX}:${scope}`;
    this.#lastReadKey = `${LAST_READ_STORAGE_PREFIX}:${scope}`;
    this.#cloudRoot = `gb_bm_v1_${scope}`;
    this.#deviceKey = `${this.#cloudRoot}_cache`;
    this.#local = storageLike(localStorage) ? localStorage : null;
    this.#timeoutMs = timeoutMs;
    this.#listener = onStatus;
    this.#cloud = supportedCloudStorage(webApp);
    this.#device = supportedDeviceStorage(webApp);
    this.#status = Object.freeze({
      phase: "opening",
      source: "fresh",
      pending: false,
      recordUpdatedAt: 0,
      local: this.#local ? "ready" : "unavailable",
      device: this.#device ? "reading" : "unavailable",
      cloud: this.#cloud ? "reading" : "unavailable",
      lastRead: "idle",
      lastReadRecordUpdatedAt: 0,
      lastReadCleared: false,
      lastError: null,
    });
    this.#notify();
  }

  get status() {
    return { ...this.#status };
  }

  getItem(key) {
    this.#requireAggregateKey(key);
    return this.#memoryRaw;
  }

  setItem(key, value) {
    this.#requireAggregateKey(key);
    const inputRaw = String(value);
    const parsed = parseAggregate(inputRaw);
    const raw = parsed && !parsed.future ? parsed.raw : inputRaw;
    this.#memoryRaw = raw;
    this.#writeLocal(this.#aggregateKey, raw);
    this.#setStatus({
      recordUpdatedAt: parsed?.recordUpdatedAt ?? this.#status.recordUpdatedAt,
    });
    if (parsed && !parsed.future) {
      this.#queueSync(raw);
    }
  }

  removeItem(key) {
    this.#requireAggregateKey(key);
    this.#memoryRaw = null;
    this.#removeLocal(this.#aggregateKey);
    this.#setStatus({ recordUpdatedAt: 0 });
    this.#queueSync(null);
  }

  async flush() {
    while (this.#syncPromise) {
      await this.#syncPromise;
    }
    return this.status;
  }

  /**
   * Reads the compact, cross-device Bible position. History and chapter data
   * deliberately never pass through this adapter.
   */
  async readLastRead({ force = false } = {}) {
    if (
      this.#lastReadDesired &&
      this.#lastReadDesired.sequence > this.#lastReadCompletedSequence
    ) {
      return cloneLastRead(this.#lastRead);
    }
    if (this.#lastReadHydrated && !force) {
      return cloneLastRead(this.#lastRead);
    }
    if (this.#lastReadTask) {
      return this.#lastReadTask;
    }
    this.#lastReadTask = this.#readLastRead().finally(() => {
      this.#lastReadTask = null;
    });
    return this.#lastReadTask;
  }

  /**
   * Saves only a compact location record:
   * {version, record_updated_at, translation, book, chapter, verse}.
   */
  writeLastRead(value) {
    let record;
    try {
      record = normalizeLastRead(value);
    } catch (error) {
      return Promise.reject(error);
    }
    if (
      this.#lastReadRecordUpdatedAt > record.record_updated_at ||
      (
        this.#lastRead === null &&
        this.#lastReadRecordUpdatedAt > 0 &&
        this.#lastReadRecordUpdatedAt === record.record_updated_at
      )
    ) {
      return Promise.resolve(cloneLastRead(this.#lastRead));
    }
    this.#lastRead = record;
    this.#lastReadHydrated = true;
    this.#lastReadRecordUpdatedAt = record.record_updated_at;
    this.#setStatus({
      lastReadRecordUpdatedAt: record.record_updated_at,
      lastReadCleared: false,
    });
    const raw = JSON.stringify(record);
    this.#writeLocal(this.#lastReadKey, raw);
    return this.#queueLastReadSync(raw).then(() => cloneLastRead(record));
  }

  clearLastRead(recordUpdatedAt = Date.now()) {
    const requestedTimestamp = safeTimestamp(recordUpdatedAt);
    if (requestedTimestamp === null) {
      return Promise.reject(
        new TypeError("The last-read Bible location is invalid."),
      );
    }
    const previousTimestamp = this.#lastReadRecordUpdatedAt;
    const timestamp = Math.min(
      Number.MAX_SAFE_INTEGER,
      Math.max(requestedTimestamp, previousTimestamp + 1),
    );
    this.#lastRead = null;
    this.#lastReadHydrated = true;
    this.#lastReadRecordUpdatedAt = timestamp;
    this.#setStatus({
      lastReadRecordUpdatedAt: timestamp,
      lastReadCleared: true,
    });
    const raw = JSON.stringify({
      version: 1,
      record_updated_at: timestamp,
      cleared: true,
    });
    this.#writeLocal(this.#lastReadKey, raw);
    return this.#queueLastReadSync(raw);
  }

  async #hydrate() {
    const localCandidate = this.#readLocalAggregate();
    const [deviceResult, cloudResult] = await Promise.allSettled([
      this.#readDeviceAggregate(),
      this.#readCloudAggregate(),
    ]);
    const deviceCandidate = deviceResult.status === "fulfilled"
      ? deviceResult.value
      : null;
    const cloudCandidate = cloudResult.status === "fulfilled"
      ? cloudResult.value
      : null;
    const winner = newestCandidate([
      candidate(localCandidate, "local", 1),
      candidate(deviceCandidate, "device", 2),
      candidate(cloudCandidate, "cloud", 3),
    ]);

    this.#memoryRaw = winner?.raw ?? null;
    if (winner) {
      this.#writeLocal(this.#aggregateKey, winner.raw);
    }
    this.#setStatus({
      phase: "ready",
      source: winner?.source ?? "fresh",
      recordUpdatedAt: winner?.recordUpdatedAt ?? 0,
      lastError:
        deviceResult.status === "rejected"
          ? errorMessage(deviceResult.reason)
          : cloudResult.status === "rejected"
            ? errorMessage(cloudResult.reason)
            : this.#status.lastError,
    });

    if (
      winner &&
      !winner.future &&
      ((this.#device && this.#deviceRaw !== winner.raw) ||
        // Re-run the cloud diff even when CloudStorage won. This removes
        // timestamped remnants from an earlier interrupted commit.
        this.#cloud)
    ) {
      this.#queueSync(winner.raw);
    }
  }

  #readLocalAggregate() {
    if (!this.#local) {
      return null;
    }
    try {
      const raw = this.#local.getItem(this.#aggregateKey);
      const parsed = parseAggregate(raw);
      if (!parsed && raw) {
        this.#local.removeItem(this.#aggregateKey);
      }
      return parsed;
    } catch (error) {
      this.#local = null;
      this.#setStatus({ local: "error", lastError: errorMessage(error) });
      return null;
    }
  }

  async #readDeviceAggregate() {
    if (!this.#device) {
      return null;
    }
    try {
      const raw = await callbackCall(
        this.#device,
        "getItem",
        [this.#deviceKey],
        this.#timeoutMs,
      );
      const parsed = parseAggregate(raw);
      this.#deviceRaw = parsed?.raw ?? null;
      this.#setStatus({ device: "ready" });
      return parsed;
    } catch (error) {
      this.#setStatus({ device: "error", lastError: errorMessage(error) });
      throw error;
    }
  }

  async #readCloudAggregate() {
    if (!this.#cloud) {
      return null;
    }
    try {
      const keys = await callbackCall(
        this.#cloud,
        "getKeys",
        [],
        this.#timeoutMs,
      );
      if (!Array.isArray(keys)) {
        throw new TypeError("Telegram CloudStorage returned invalid keys.");
      }
      const ownedKeys = keys.filter((key) => this.#isBookmarkCloudKey(key));
      const values = await this.#getCloudItems(ownedKeys);
      this.#cloudSnapshot = values;
      const parsed = cloudAggregate(
        values,
        this.#metaCloudKey(),
        this.#topicCloudPrefix(),
        this.#verseCloudPrefix(),
      );
      this.#setStatus({ cloud: "ready" });
      return parsed;
    } catch (error) {
      this.#setStatus({ cloud: "error", lastError: errorMessage(error) });
      throw error;
    }
  }

  async #getCloudItems(keys) {
    const values = new Map();
    if (keys.length === 0) {
      return values;
    }
    if (typeof this.#cloud.getItems === "function") {
      for (const batch of chunks(keys, CLOUD_READ_BATCH_SIZE)) {
        const result = await callbackCall(
          this.#cloud,
          "getItems",
          [batch],
          this.#timeoutMs,
        );
        if (!isRecord(result)) {
          throw new TypeError("Telegram CloudStorage returned invalid values.");
        }
        for (const key of batch) {
          if (typeof result[key] === "string") {
            values.set(key, result[key]);
          }
        }
      }
      return values;
    }

    await runLimited(keys, CLOUD_WRITE_CONCURRENCY, async (key) => {
      const value = await callbackCall(
        this.#cloud,
        "getItem",
        [key],
        this.#timeoutMs,
      );
      if (typeof value === "string") {
        values.set(key, value);
      }
    });
    return values;
  }

  #queueSync(raw) {
    if (!this.#device && !this.#cloud) {
      return;
    }
    this.#desired = { sequence: ++this.#sequence, raw };
    this.#setStatus({ pending: true });
    this.#startSync();
  }

  #startSync() {
    if (
      this.#syncPromise ||
      !this.#desired ||
      this.#desired.sequence <= this.#completedSequence
    ) {
      return;
    }
    this.#syncPromise = Promise.resolve()
      .then(() => this.#drainSync())
      .finally(() => {
        this.#syncPromise = null;
        const pending = Boolean(
          this.#desired &&
          this.#desired.sequence > this.#completedSequence
        );
        this.#setStatus({
          phase: pending
            ? "syncing"
            : this.#status.lastError ? "degraded" : "ready",
          pending,
        });
        // A synchronous BookmarkStore mutation can land after the drain loop
        // exits but before this promise settles. Do not strand that write.
        this.#startSync();
      });
  }

  async #drainSync() {
    while (
      this.#desired &&
      this.#desired.sequence > this.#completedSequence
    ) {
      const target = this.#desired;
      let errors = [];
      // Cloud snapshots are updated per successful item, so a bounded retry
      // replays only failed work and can finish an interrupted commit without
      // waiting for another user mutation.
      for (
        let attempt = 1;
        attempt <= BOOKMARK_SYNC_MAX_ATTEMPTS;
        attempt += 1
      ) {
        errors = [];
        this.#setStatus({ phase: "syncing", lastError: null });
        const tasks = [];
        if (this.#device) {
          this.#setStatus({ device: "syncing" });
          tasks.push(
            this.#syncDevice(target.raw)
              .then(() => this.#setStatus({ device: "synced" }))
              .catch((error) => {
                errors.push(error);
                this.#setStatus({ device: "error" });
              }),
          );
        }
        if (this.#cloud) {
          this.#setStatus({ cloud: "syncing" });
          tasks.push(
            this.#syncCloud(target.raw)
              .then(() => this.#setStatus({ cloud: "synced" }))
              .catch((error) => {
                errors.push(error);
                this.#setStatus({ cloud: "error" });
              }),
          );
        }
        await Promise.all(tasks);
        if (
          errors.length === 0 ||
          this.#desired?.sequence > target.sequence
        ) {
          break;
        }
      }
      this.#completedSequence = target.sequence;
      this.#setStatus({
        lastError: errors.length > 0 ? errorMessage(errors[0]) : null,
      });
    }
  }

  async #syncDevice(raw) {
    if (raw === this.#deviceRaw) {
      return;
    }
    if (raw === null) {
      await this.#removeTelegramItem(
        this.#device,
        "removeItem",
        this.#deviceKey,
      );
    } else {
      await this.#setTelegramItem(this.#device, this.#deviceKey, raw);
    }
    this.#deviceRaw = raw;
  }

  async #syncCloud(raw) {
    if (raw === null) {
      const keys = [...this.#cloudSnapshot.keys()]
        .filter((key) => this.#isBookmarkCloudKey(key));
      await this.#removeCloudItems(keys);
      return;
    }

    const parsed = parseAggregate(raw);
    if (!parsed || parsed.future) {
      throw new TypeError("The bookmark aggregate is invalid.");
    }
    const target = this.#buildCloudTarget(parsed.value);
    const metaKey = this.#metaCloudKey();
    const sets = [...target.entries()].filter(
      ([key, value]) => key !== metaKey && this.#cloudSnapshot.get(key) !== value,
    );
    const removals = [...this.#cloudSnapshot.keys()].filter(
      (key) => this.#isBookmarkCloudKey(key) && !target.has(key),
    );

    // Remove obsolete verse keys first so a large coordinate replacement never
    // exceeds Telegram's key quota transiently. Meta remains the old commit
    // marker until every item mutation succeeds.
    await this.#removeCloudItems(removals);
    await runLimited(sets, CLOUD_WRITE_CONCURRENCY, async ([key, value]) => {
      await this.#setTelegramItem(this.#cloud, key, value);
      this.#cloudSnapshot.set(key, value);
    });

    const meta = target.get(metaKey);
    if (this.#cloudSnapshot.get(metaKey) !== meta) {
      await this.#setTelegramItem(this.#cloud, metaKey, meta);
      this.#cloudSnapshot.set(metaKey, meta);
    }
  }

  #buildCloudTarget(record) {
    const updatedAt = record.record_updated_at ?? 0;
    const target = new Map();
    const topicIndexes = new Map();
    record.topics.forEach((topic, index) => {
      topicIndexes.set(topic.id, index);
      target.set(
        `${this.#topicCloudPrefix()}${String(index).padStart(3, "0")}`,
        cloudValue({ topic }),
      );
    });
    for (const bookmark of record.bookmarks) {
      const key = `${this.#verseCloudPrefix()}${pad(bookmark.book, 3)}_${
        pad(bookmark.chapter, 4)
      }_${pad(bookmark.verse, 4)}`;
      const topicIndexValues = bookmark.topic_ids.map((topicId) =>
        topicIndexes.get(topicId)
      );
      if (topicIndexValues.some((index) => !Number.isInteger(index))) {
        throw new TypeError("The bookmark aggregate is invalid.");
      }
      const compactBookmark = {
        ...bookmark,
        topic_indexes: topicIndexValues,
      };
      delete compactBookmark.topic_ids;
      delete compactBookmark.topic_id;
      target.set(
        key,
        cloudValue({ bookmark: compactBookmark }),
      );
    }
    // Item wrappers deliberately exclude the aggregate timestamp. This keeps
    // unchanged values byte-stable, while the fingerprint lets the metadata
    // remain the last, atomic commit marker for the complete payload.
    const payloadFingerprint = cloudPayloadFingerprint(target);
    target.set(this.#metaCloudKey(), cloudValue({
      version: record.version,
      record_updated_at: updatedAt,
      active_topic_id: record.active_topic_id,
      recent_topic_indexes: record.recent_topic_ids.map((topicId) =>
        topicIndexes.get(topicId)
      ),
      topic_count: record.topics.length,
      bookmark_count: record.bookmarks.length,
      payload_fingerprint: payloadFingerprint,
    }));
    if (target.size > CLOUD_KEY_LIMIT) {
      throw new RangeError("The Telegram CloudStorage key limit was exceeded.");
    }
    return target;
  }

  async #removeCloudItems(keys) {
    if (keys.length === 0) {
      return;
    }
    if (typeof this.#cloud.removeItems === "function") {
      for (const batch of chunks(keys, CLOUD_READ_BATCH_SIZE)) {
        await this.#removeTelegramItem(
          this.#cloud,
          "removeItems",
          batch,
        );
        for (const key of batch) {
          this.#cloudSnapshot.delete(key);
        }
      }
      return;
    }
    await runLimited(keys, CLOUD_WRITE_CONCURRENCY, async (key) => {
      await this.#removeTelegramItem(
        this.#cloud,
        "removeItem",
        key,
      );
      this.#cloudSnapshot.delete(key);
    });
  }

  async #removeTelegramItem(api, method, keyOrKeys) {
    const removed = await callbackCall(
      api,
      method,
      [keyOrKeys],
      this.#timeoutMs,
    );
    if (removed === false) {
      throw new Error("Telegram storage did not remove the value.");
    }
  }

  async #setTelegramItem(api, key, value) {
    if (!CLOUD_KEY_PATTERN.test(key)) {
      throw new TypeError("A Telegram storage key is invalid.");
    }
    if (value.length > CLOUD_VALUE_LIMIT && api === this.#cloud) {
      throw new RangeError("A Telegram CloudStorage value is too large.");
    }
    const stored = await callbackCall(
      api,
      "setItem",
      [key, value],
      this.#timeoutMs,
    );
    if (stored === false) {
      throw new Error("Telegram storage did not store the value.");
    }
  }

  #queueLastReadSync(raw) {
    const sequence = ++this.#lastReadSequence;
    this.#lastReadDesired = { sequence, raw };
    this.#setStatus({ lastRead: "syncing", lastError: null });
    const result = new Promise((resolve) => {
      this.#lastReadWaiters.push({ sequence, resolve });
    });

    if (!this.#device && !this.#cloud) {
      this.#lastReadCompletedSequence = sequence;
      this.#settleLastReadWaiters(sequence, true);
      this.#setStatus({ lastRead: raw === null ? "ready" : "synced" });
      return result;
    }
    this.#startLastReadSync();
    return result;
  }

  #startLastReadSync() {
    if (
      this.#lastReadSyncPromise ||
      !this.#lastReadDesired ||
      this.#lastReadDesired.sequence <= this.#lastReadCompletedSequence
    ) {
      return;
    }
    this.#lastReadSyncPromise = Promise.resolve()
      .then(() => this.#drainLastReadSync())
      .finally(() => {
        this.#lastReadSyncPromise = null;
        this.#startLastReadSync();
      });
  }

  async #drainLastReadSync() {
    while (
      this.#lastReadDesired &&
      this.#lastReadDesired.sequence > this.#lastReadCompletedSequence
    ) {
      const target = this.#lastReadDesired;
      const errors = [];
      const tasks = [];
      if (this.#device) {
        tasks.push(
          this.#syncLastReadBackend(
            this.#device,
            this.#lastReadDeviceKey(),
            target.raw,
            this.#lastReadDeviceRaw,
          ).then(() => {
            this.#lastReadDeviceRaw = target.raw;
          }).catch((error) => errors.push(error)),
        );
      }
      if (this.#cloud) {
        tasks.push(
          this.#syncLastReadBackend(
            this.#cloud,
            this.#lastReadCloudKey(),
            target.raw,
            this.#lastReadCloudRaw,
          ).then(() => {
            this.#lastReadCloudRaw = target.raw;
          }).catch((error) => errors.push(error)),
        );
      }
      await Promise.all(tasks);
      this.#lastReadCompletedSequence = target.sequence;
      const success = errors.length === 0;
      this.#settleLastReadWaiters(target.sequence, success);
      if (this.#lastReadDesired.sequence === target.sequence) {
        this.#setStatus({
          lastRead: success
            ? target.raw === null ? "ready" : "synced"
            : "degraded",
          lastError: success ? null : errorMessage(errors[0]),
        });
      }
    }
  }

  async #syncLastReadBackend(api, key, raw, currentRaw) {
    if (raw === currentRaw) {
      return;
    }
    if (raw !== null) {
      await this.#setTelegramItem(api, key, raw);
      return;
    }
    const method = api === this.#cloud &&
      typeof api.removeItem !== "function"
      ? "removeItems"
      : "removeItem";
    await this.#removeTelegramItem(
      api,
      method,
      method === "removeItem" ? key : [key],
    );
  }

  #settleLastReadWaiters(sequence, success) {
    const pending = [];
    for (const waiter of this.#lastReadWaiters) {
      if (waiter.sequence <= sequence) {
        waiter.resolve(success);
      } else {
        pending.push(waiter);
      }
    }
    this.#lastReadWaiters = pending;
  }

  async #readLastRead() {
    const mutationAtStart = this.#lastReadSequence;
    this.#setStatus({ lastRead: "reading", lastError: null });
    const local = this.#readLocalLastRead();
    const reads = await Promise.allSettled([
      this.#readTelegramLastRead(this.#device, this.#lastReadDeviceKey()),
      this.#readTelegramLastRead(this.#cloud, this.#lastReadCloudKey()),
    ]);
    const readErrors = reads
      .filter((result) => result.status === "rejected")
      .map((result) => result.reason);
    if (this.#lastReadSequence !== mutationAtStart) {
      return cloneLastRead(this.#lastRead);
    }
    if (this.#device && reads[0].status === "fulfilled") {
      this.#lastReadDeviceRaw = reads[0].value?.raw ?? null;
    }
    if (this.#cloud && reads[1].status === "fulfilled") {
      this.#lastReadCloudRaw = reads[1].value?.raw ?? null;
    }
    const winner = newestCandidate([
      candidate(local, "local", 1),
      candidate(reads[0].status === "fulfilled" ? reads[0].value : null, "device", 2),
      candidate(reads[1].status === "fulfilled" ? reads[1].value : null, "cloud", 3),
    ]);
    if (!winner) {
      this.#lastReadHydrated = true;
      this.#lastReadRecordUpdatedAt = 0;
      this.#setStatus({
        lastRead: readErrors.length > 0 ? "degraded" : "ready",
        lastReadRecordUpdatedAt: 0,
        lastReadCleared: false,
        lastError: readErrors.length > 0
          ? errorMessage(readErrors[0])
          : null,
      });
      return null;
    }
    this.#lastRead = winner.value;
    this.#lastReadHydrated = true;
    this.#lastReadRecordUpdatedAt = winner.recordUpdatedAt;
    this.#setStatus({
      lastReadRecordUpdatedAt: winner.recordUpdatedAt,
      lastReadCleared: Boolean(winner.cleared),
    });
    const raw = winner.raw;
    this.#writeLocal(this.#lastReadKey, raw);
    await this.#queueLastReadSync(raw);
    return cloneLastRead(this.#lastRead);
  }

  #readLocalLastRead() {
    if (!this.#local) {
      return null;
    }
    try {
      return parseLastRead(this.#local.getItem(this.#lastReadKey));
    } catch (error) {
      this.#setStatus({ local: "error", lastError: errorMessage(error) });
      return null;
    }
  }

  async #readTelegramLastRead(api, key) {
    if (!api) {
      return null;
    }
    const raw = await callbackCall(
      api,
      "getItem",
      [key],
      this.#timeoutMs,
    );
    return parseLastRead(raw);
  }

  #writeLocal(key, value) {
    if (!this.#local) {
      return;
    }
    try {
      this.#local.setItem(key, value);
      this.#setStatus({ local: "ready" });
    } catch (error) {
      this.#local = null;
      this.#setStatus({ local: "error", lastError: errorMessage(error) });
    }
  }

  #removeLocal(key) {
    if (!this.#local) {
      return;
    }
    try {
      this.#local.removeItem(key);
      this.#setStatus({ local: "ready" });
    } catch (error) {
      this.#local = null;
      this.#setStatus({ local: "error", lastError: errorMessage(error) });
    }
  }

  #requireAggregateKey(key) {
    if (key !== this.#aggregateKey) {
      throw new TypeError("The bookmark storage key is outside this scope.");
    }
  }

  #metaCloudKey() {
    return `${this.#cloudRoot}_meta`;
  }

  #topicCloudPrefix() {
    return `${this.#cloudRoot}_topic_`;
  }

  #verseCloudPrefix() {
    return `${this.#cloudRoot}_verse_`;
  }

  #lastReadCloudKey() {
    return `${this.#cloudRoot}_last_read`;
  }

  #lastReadDeviceKey() {
    return `${this.#cloudRoot}_last_read_cache`;
  }

  #isBookmarkCloudKey(key) {
    return typeof key === "string" && (
      key === this.#metaCloudKey() ||
      key.startsWith(this.#topicCloudPrefix()) ||
      key.startsWith(this.#verseCloudPrefix())
    );
  }

  #setStatus(changes) {
    this.#status = Object.freeze({ ...this.#status, ...changes });
    this.#notify();
  }

  #notify() {
    try {
      this.#listener?.(this.status);
    } catch {
      // A presentation listener must never disable persistence.
    }
  }
}

export async function openTelegramBookmarkStorage(options) {
  return TelegramBookmarkStorage.open(options);
}

function parseAggregate(raw) {
  if (typeof raw !== "string" || raw.length === 0) {
    return null;
  }
  try {
    const value = JSON.parse(raw);
    if (
      isRecord(value) &&
      Number.isInteger(value.version) &&
      value.version > BOOKMARK_AGGREGATE_VERSION
    ) {
      return {
        raw,
        value: null,
        recordUpdatedAt: safeTimestamp(value.record_updated_at) ?? 0,
        future: true,
      };
    }
    if (
      !isRecord(value) ||
      ![1, 2, BOOKMARK_AGGREGATE_VERSION].includes(value.version) ||
      !Array.isArray(value.topics) ||
      value.topics.length < 1 ||
      value.topics.length > MAX_BOOKMARK_TOPICS ||
      !Array.isArray(value.bookmarks) ||
      value.bookmarks.length > MAX_BOOKMARKS ||
      typeof value.active_topic_id !== "string"
    ) {
      return null;
    }
    const updatedAt = Object.hasOwn(value, "record_updated_at")
      ? safeTimestamp(value.record_updated_at)
      : 0;
    if (updatedAt === null) {
      return null;
    }
    const normalized = normalizeAggregate(value, updatedAt);
    if (!normalized) {
      return null;
    }
    return {
      raw: JSON.stringify(normalized),
      value: normalized,
      recordUpdatedAt: updatedAt,
      future: false,
    };
  } catch {
    return null;
  }
}

function normalizeAggregate(value, updatedAt) {
  const topicIds = new Set();
  for (const topic of value.topics) {
    if (
      !isRecord(topic) ||
      typeof topic.id !== "string" ||
      !ID_PATTERN.test(topic.id) ||
      typeof topic.name !== "string" ||
      topic.name.trim().length < 1 ||
      topic.name.trim().length > 80 ||
      typeof topic.color !== "string" ||
      !COLOR_PATTERN.test(topic.color.toLowerCase()) ||
      topicIds.has(topic.id)
    ) {
      return null;
    }
    topicIds.add(topic.id);
  }
  if (!topicIds.has(value.active_topic_id)) {
    return null;
  }
  const recentTopicIds = value.version >= 3
    ? value.recent_topic_ids
    : [];
  if (
    !Array.isArray(recentTopicIds) ||
    recentTopicIds.length > MAX_BOOKMARK_TOPICS ||
    new Set(recentTopicIds).size !== recentTopicIds.length ||
    recentTopicIds.some((topicId) =>
      typeof topicId !== "string" ||
      !ID_PATTERN.test(topicId) ||
      !topicIds.has(topicId)
    )
  ) {
    return null;
  }
  const verses = new Set();
  const bookmarkIds = new Set();
  const bookmarks = [];
  for (const bookmark of value.bookmarks) {
    const createdAt = safeTimestamp(bookmark?.created_at);
    const bookmarkUpdatedAt = safeTimestamp(
      bookmark?.updated_at ?? bookmark?.created_at,
    );
    const bookmarkTopicIds = value.version === 1
      ? [bookmark?.topic_id]
      : bookmark?.topic_ids;
    if (
      !isRecord(bookmark) ||
      typeof bookmark.id !== "string" ||
      !ID_PATTERN.test(bookmark.id) ||
      bookmarkIds.has(bookmark.id) ||
      !validTopicIds(bookmarkTopicIds, topicIds) ||
      (
        value.version >= 2 &&
        bookmark.topic_id !== bookmarkTopicIds[0]
      ) ||
      typeof bookmark.translation !== "string" ||
      !TRANSLATION_PATTERN.test(bookmark.translation.toLowerCase()) ||
      !boundedString(bookmark.reference, 180) ||
      !boundedString(bookmark.book_name, 128) ||
      !boundedString(bookmark.text, BOOKMARK_TEXT_MAX_CHARS) ||
      !boundedInteger(bookmark.book, 1, 200) ||
      !boundedInteger(bookmark.chapter, 1, 1_000) ||
      !boundedInteger(bookmark.verse, 1, 2_000) ||
      createdAt === null ||
      bookmarkUpdatedAt === null ||
      bookmarkUpdatedAt < createdAt
    ) {
      return null;
    }
    const key = `${bookmark.book}/${bookmark.chapter}/${bookmark.verse}`;
    if (verses.has(key)) {
      return null;
    }
    verses.add(key);
    bookmarkIds.add(bookmark.id);
    bookmarks.push({
      ...bookmark,
      topic_ids: [...bookmarkTopicIds],
      topic_id: bookmarkTopicIds[0],
    });
  }
  return {
    version: BOOKMARK_AGGREGATE_VERSION,
    active_topic_id: value.active_topic_id,
    recent_topic_ids: recentTopicIds.slice(0, MAX_RECENT_BOOKMARK_TOPICS),
    topics: value.topics.map((topic) => ({ ...topic })),
    bookmarks,
    record_updated_at: updatedAt,
  };
}

function validTopicIds(value, allowedTopicIds) {
  return Array.isArray(value) &&
    value.length >= 1 &&
    value.length <= MAX_BOOKMARK_TOPICS &&
    new Set(value).size === value.length &&
    value.every((topicId) =>
      typeof topicId === "string" &&
      ID_PATTERN.test(topicId) &&
      allowedTopicIds.has(topicId)
    );
}

function expandCloudBookmark(value, topics) {
  if (
    !isRecord(value) ||
    !Array.isArray(value.topic_indexes) ||
    value.topic_indexes.length < 1 ||
    value.topic_indexes.length > MAX_BOOKMARK_TOPICS ||
    new Set(value.topic_indexes).size !== value.topic_indexes.length
  ) {
    return null;
  }
  const topicIds = value.topic_indexes.map((index) =>
    Number.isInteger(index) && index >= 0 && index < topics.length
      ? topics[index]?.id
      : null
  );
  if (topicIds.some((topicId) => typeof topicId !== "string")) {
    return null;
  }
  const bookmark = { ...value };
  delete bookmark.topic_indexes;
  return {
    ...bookmark,
    topic_ids: topicIds,
    topic_id: topicIds[0],
  };
}

function cloudAggregate(values, metaKey, topicPrefix, versePrefix) {
  const meta = parseJsonRecord(values.get(metaKey));
  const updatedAt = safeTimestamp(meta?.record_updated_at);
  if (
    meta &&
    Number.isInteger(meta.version) &&
    meta.version > BOOKMARK_AGGREGATE_VERSION &&
    updatedAt !== null
  ) {
    return {
      raw: JSON.stringify({
        version: meta.version,
        record_updated_at: updatedAt,
      }),
      value: null,
      recordUpdatedAt: updatedAt,
      future: true,
    };
  }
  if (
    !meta ||
    ![1, 2, BOOKMARK_AGGREGATE_VERSION].includes(meta.version) ||
    updatedAt === null ||
    !boundedInteger(meta.topic_count, 1, MAX_BOOKMARK_TOPICS) ||
    !boundedInteger(meta.bookmark_count, 0, MAX_BOOKMARKS) ||
    typeof meta.active_topic_id !== "string"
  ) {
    return null;
  }
  const recentTopicIndexes = meta.version >= 3
    ? meta.recent_topic_indexes
    : [];
  if (
    !Array.isArray(recentTopicIndexes) ||
    recentTopicIndexes.length > MAX_BOOKMARK_TOPICS ||
    new Set(recentTopicIndexes).size !== recentTopicIndexes.length ||
    recentTopicIndexes.some((index) =>
      !Number.isInteger(index) || index < 0 || index >= meta.topic_count
    )
  ) {
    return null;
  }

  const fingerprinted = Object.hasOwn(meta, "payload_fingerprint");
  if (
    fingerprinted &&
    (
      typeof meta.payload_fingerprint !== "string" ||
      !/^fnv2x32-[0-9a-f]{16}$/.test(meta.payload_fingerprint)
    )
  ) {
    return null;
  }
  if (fingerprinted) {
    const payload = new Map(
      [...values].filter(([key]) =>
        key.startsWith(topicPrefix) || key.startsWith(versePrefix)
      ),
    );
    if (cloudPayloadFingerprint(payload) !== meta.payload_fingerprint) {
      return null;
    }
  }

  const topics = [];
  const encodedBookmarks = [];
  for (const [key, raw] of values) {
    if (key.startsWith(topicPrefix)) {
      const wrapper = parseJsonRecord(raw);
      if (
        isRecord(wrapper?.topic) &&
        (fingerprinted || wrapper.record_updated_at === updatedAt)
      ) {
        const index = Number(key.slice(topicPrefix.length));
        if (Number.isInteger(index) && index >= 0) {
          topics[index] = wrapper.topic;
        }
      }
    } else if (key.startsWith(versePrefix)) {
      const wrapper = parseJsonRecord(raw);
      if (
        isRecord(wrapper?.bookmark) &&
        (fingerprinted || wrapper.record_updated_at === updatedAt)
      ) {
        encodedBookmarks.push(wrapper.bookmark);
      }
    }
  }
  const bookmarks = meta.version === 1
    ? encodedBookmarks
    : encodedBookmarks.map((bookmark) =>
      expandCloudBookmark(bookmark, topics)
    );
  if (
    topics.length !== meta.topic_count ||
    topics.some((topic) => !topic) ||
    bookmarks.length !== meta.bookmark_count ||
    bookmarks.some((bookmark) => !bookmark)
  ) {
    return null;
  }
  return parseAggregate(JSON.stringify({
    version: meta.version,
    active_topic_id: meta.active_topic_id,
    recent_topic_ids: recentTopicIndexes
      .slice(0, MAX_RECENT_BOOKMARK_TOPICS)
      .map((index) => topics[index].id),
    topics,
    bookmarks,
    record_updated_at: updatedAt,
  }));
}

function normalizeLastRead(value) {
  if (!isRecord(value) || value.version !== 1) {
    throw new TypeError("The last-read Bible location is invalid.");
  }
  const recordUpdatedAt = safeTimestamp(value.record_updated_at);
  const translation = typeof value.translation === "string"
    ? value.translation.trim().toLowerCase()
    : "";
  const book = boundedInteger(value.book, 1, 200);
  const chapter = boundedInteger(value.chapter, 1, 1_000);
  const verse = boundedInteger(value.verse, 1, 2_000);
  if (
    recordUpdatedAt === null ||
    !TRANSLATION_PATTERN.test(translation) ||
    !book ||
    !chapter ||
    !verse
  ) {
    throw new TypeError("The last-read Bible location is invalid.");
  }
  return {
    version: 1,
    record_updated_at: recordUpdatedAt,
    translation,
    book,
    chapter,
    verse,
  };
}

function parseLastRead(raw) {
  if (typeof raw !== "string" || raw.length === 0) {
    return null;
  }
  try {
    const decoded = JSON.parse(raw);
    if (
      isRecord(decoded) &&
      decoded.version === 1 &&
      decoded.cleared === true
    ) {
      const timestamp = safeTimestamp(decoded.record_updated_at);
      if (timestamp === null) {
        return null;
      }
      const tombstone = {
        version: 1,
        record_updated_at: timestamp,
        cleared: true,
      };
      return {
        raw: JSON.stringify(tombstone),
        value: null,
        recordUpdatedAt: timestamp,
        cleared: true,
      };
    }
    const value = normalizeLastRead(decoded);
    return {
      raw: JSON.stringify(value),
      value,
      recordUpdatedAt: value.record_updated_at,
      cleared: false,
    };
  } catch {
    return null;
  }
}

function candidate(parsed, source, priority) {
  return parsed ? { ...parsed, source, priority } : null;
}

function newestCandidate(candidates) {
  return candidates.filter(Boolean).sort((left, right) =>
    Number(right.future) - Number(left.future) ||
    right.recordUpdatedAt - left.recordUpdatedAt ||
    Number(Boolean(right.cleared)) - Number(Boolean(left.cleared)) ||
    right.priority - left.priority
  )[0] ?? null;
}

function cloudValue(value) {
  const raw = JSON.stringify(value);
  if (raw.length > CLOUD_VALUE_LIMIT) {
    throw new RangeError("A Telegram CloudStorage value is too large.");
  }
  return raw;
}

/**
 * Compact commit fingerprint for exact CloudStorage key/value payload bytes.
 * This is an atomicity checksum, not an authentication primitive: the metadata
 * is written only after all fingerprinted item mutations have succeeded.
 */
function cloudPayloadFingerprint(values) {
  const entries = [...values.entries()].sort(([left], [right]) =>
    left < right ? -1 : left > right ? 1 : 0
  );
  let left = 0x811c9dc5;
  let right = 0x9e3779b9;
  for (const [key, value] of entries) {
    const framed = `${key.length}:${key}${value.length}:${value}`;
    for (let index = 0; index < framed.length; index += 1) {
      const unit = framed.charCodeAt(index);
      left = Math.imul(left ^ unit, 0x01000193) >>> 0;
      right = Math.imul(right ^ unit, 0x85ebca6b) >>> 0;
    }
  }
  return `fnv2x32-${left.toString(16).padStart(8, "0")}${
    right.toString(16).padStart(8, "0")
  }`;
}

function parseJsonRecord(raw) {
  if (typeof raw !== "string" || raw.length === 0) {
    return null;
  }
  try {
    const value = JSON.parse(raw);
    return isRecord(value) ? value : null;
  } catch {
    return null;
  }
}

function safeTimestamp(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function boundedInteger(value, minimum, maximum) {
  return Number.isInteger(value) && value >= minimum && value <= maximum
    ? value
    : null;
}

function boundedString(value, maximum) {
  return typeof value === "string" &&
    value.trim().length > 0 &&
    value.trim().length <= maximum &&
    !FORBIDDEN_CONTROL_PATTERN.test(value) &&
    !hasUnpairedSurrogate(value);
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

function pad(value, length) {
  return String(value).padStart(length, "0");
}

function cloneLastRead(value) {
  return value ? { ...value } : null;
}

function isRecord(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
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

function supportedCloudStorage(webApp) {
  const storage = webApp?.CloudStorage;
  return supportsVersion(webApp, CLOUD_API_VERSION) &&
    storage &&
    typeof storage.getKeys === "function" &&
    typeof storage.getItem === "function" &&
    typeof storage.setItem === "function" &&
    (typeof storage.removeItems === "function" ||
      typeof storage.removeItem === "function")
    ? storage
    : null;
}

function supportedDeviceStorage(webApp) {
  const storage = webApp?.DeviceStorage;
  return supportsVersion(webApp, DEVICE_API_VERSION) && storageLike(storage)
    ? storage
    : null;
}

function supportsVersion(webApp, required) {
  if (typeof webApp?.isVersionAtLeast === "function") {
    try {
      return webApp.isVersionAtLeast(required) === true;
    } catch {
      return versionAtLeast(webApp?.version, required);
    }
  }
  return versionAtLeast(webApp?.version, required);
}

function versionAtLeast(actual, required) {
  const left = versionParts(actual);
  const right = versionParts(required);
  if (!left || !right) {
    return false;
  }
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0);
    if (difference !== 0) {
      return difference > 0;
    }
  }
  return true;
}

function versionParts(value) {
  return typeof value === "string" && /^\d+(?:\.\d+)*$/.test(value)
    ? value.split(".").map(Number)
    : null;
}

function callbackCall(api, method, args, timeoutMs) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error(`Telegram storage ${method} timed out.`));
      }
    }, timeoutMs);
    const callback = (error, result) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timeout);
      if (error) {
        reject(error instanceof Error ? error : new Error(String(error)));
      } else {
        resolve(result);
      }
    };
    try {
      api[method](...args, callback);
    } catch (error) {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(error);
      }
    }
  });
}

async function runLimited(values, concurrency, operation) {
  let index = 0;
  const errors = [];
  const workers = Array.from(
    { length: Math.min(concurrency, values.length) },
    async () => {
      while (index < values.length) {
        const value = values[index++];
        try {
          await operation(value);
        } catch (error) {
          errors.push(error);
        }
      }
    },
  );
  await Promise.all(workers);
  if (errors.length > 0) {
    throw errors[0];
  }
}

function chunks(values, size) {
  const result = [];
  for (let index = 0; index < values.length; index += size) {
    result.push(values.slice(index, index + size));
  }
  return result;
}

function errorMessage(error) {
  return error instanceof Error && error.message
    ? error.message
    : "Telegram storage is temporarily unavailable.";
}
