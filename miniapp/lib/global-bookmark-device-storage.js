import {
  GLOBAL_BOOKMARK_PREFERENCES_KEY,
  GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX,
} from "./global-bookmark-preferences.js";

const SCOPE_PATTERN = /^[a-f0-9]{64}$/;
const DEVICE_API_VERSION = "9.0";
const DEVICE_KEY_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const ENVELOPE_VERSION = 1;
const DEFAULT_TIMEOUT_MS = 2_500;
const MAX_TIMEOUT_MS = 30_000;
const DEVICE_WRITE_ATTEMPTS = 3;

export const GLOBAL_BOOKMARK_DEVICE_ENVELOPE_VERSION = ENVELOPE_VERSION;
export const GLOBAL_BOOKMARK_DEVICE_KEY_PREFIX = "gb_global_local_v1";
export const GLOBAL_BOOKMARK_LOCAL_MIRROR_PREFIX =
  "getbible.miniapp.global-device.v1";

/**
 * Synchronous Storage-compatible facade over a hydrated local/device replica.
 *
 * GlobalBookmarkPreferences deliberately remains synchronous. This adapter is
 * opened asynchronously first, selects the newest opaque record for each
 * allowed key, and then exposes getItem/setItem/removeItem. Writes update the
 * browser mirror synchronously and replicate to Telegram DeviceStorage in a
 * coalesced background queue. Telegram CloudStorage is intentionally never
 * consulted: global bookmark visibility remains local to this device.
 *
 * Payloads are opaque strings. The adapter validates only its own timestamped
 * envelope and never parses bookmark, topic, catalogue, or verse data.
 */
export class GlobalBookmarkDeviceStorage {
  #allowedKeys;
  #blockedKeys = new Set();
  #clock;
  #completedSequences = new Map();
  #desired = new Map();
  #device;
  #deviceKeys;
  #deviceRaw = new Map();
  #lastTimestamp = 0;
  #listener;
  #local;
  #localKeys;
  #mappingKey;
  #records = new Map();
  #sequence = 0;
  #status;
  #syncPromise = null;
  #timeoutMs;
  #values = new Map();

  static async open({
    scope,
    webApp = globalThis.Telegram?.WebApp ?? null,
    localStorage = browserLocalStorage(),
    timeoutMs = DEFAULT_TIMEOUT_MS,
    clock = Date.now,
    onStatus = null,
  } = {}) {
    const storage = new GlobalBookmarkDeviceStorage({
      scope,
      webApp,
      localStorage,
      timeoutMs,
      clock,
      onStatus,
    });
    await storage.#hydrate();
    return storage;
  }

  constructor({ scope, webApp, localStorage, timeoutMs, clock, onStatus }) {
    if (typeof scope !== "string" || !SCOPE_PATTERN.test(scope)) {
      throw new TypeError(
        "An authenticated global bookmark storage scope is required.",
      );
    }
    if (
      !Number.isFinite(timeoutMs) ||
      timeoutMs < 1 ||
      timeoutMs > MAX_TIMEOUT_MS
    ) {
      throw new TypeError("A valid Telegram storage timeout is required.");
    }
    if (typeof clock !== "function") {
      throw new TypeError("A valid global bookmark storage clock is required.");
    }
    if (onStatus !== null && typeof onStatus !== "function") {
      throw new TypeError(
        "The global bookmark storage status listener is invalid.",
      );
    }

    this.#mappingKey = `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${scope}`;
    this.#allowedKeys = new Set([
      GLOBAL_BOOKMARK_PREFERENCES_KEY,
      this.#mappingKey,
    ]);
    this.#localKeys = new Map([
      [
        GLOBAL_BOOKMARK_PREFERENCES_KEY,
        `${GLOBAL_BOOKMARK_LOCAL_MIRROR_PREFIX}:${scope}:preferences`,
      ],
      [
        this.#mappingKey,
        `${GLOBAL_BOOKMARK_LOCAL_MIRROR_PREFIX}:${scope}:mapping`,
      ],
    ]);
    this.#deviceKeys = new Map([
      [
        GLOBAL_BOOKMARK_PREFERENCES_KEY,
        `${GLOBAL_BOOKMARK_DEVICE_KEY_PREFIX}_${scope}_preferences`,
      ],
      [
        this.#mappingKey,
        `${GLOBAL_BOOKMARK_DEVICE_KEY_PREFIX}_${scope}_mapping`,
      ],
    ]);
    if ([...this.#deviceKeys.values()].some((key) =>
      !DEVICE_KEY_PATTERN.test(key)
    )) {
      throw new TypeError("A global bookmark device storage key is invalid.");
    }

    this.#local = storageLike(localStorage) ? localStorage : null;
    this.#device = supportedDeviceStorage(webApp);
    this.#timeoutMs = timeoutMs;
    this.#clock = clock;
    this.#listener = onStatus;
    this.#status = Object.freeze({
      phase: "opening",
      pending: false,
      local: this.#local ? "reading" : "unavailable",
      device: this.#device ? "reading" : "unavailable",
      sources: Object.freeze({
        preferences: "fresh",
        mapping: "fresh",
      }),
      lastError: null,
    });
    this.#notify();
  }

  get status() {
    return {
      ...this.#status,
      sources: { ...this.#status.sources },
    };
  }

  getItem(key) {
    this.#requireKey(key);
    return this.#values.get(key) ?? null;
  }

  setItem(key, value) {
    this.#requireWritableKey(key);
    const raw = String(value);
    const current = this.#records.get(key);
    if (current && !current.deleted && current.value === raw) {
      return;
    }
    const record = valueEnvelope(this.#nextTimestamp(), raw);
    this.#installRecord(key, record);
    this.#writeLocalRecord(key, record);
    this.#queueDeviceWrite(key, record);
  }

  removeItem(key) {
    this.#requireWritableKey(key);
    const current = this.#records.get(key);
    if (current?.deleted) {
      return;
    }
    const record = tombstoneEnvelope(this.#nextTimestamp());
    this.#installRecord(key, record);
    this.#writeLocalRecord(key, record);
    this.#queueDeviceWrite(key, record);
  }

  async flush() {
    while (this.#syncPromise) {
      await this.#syncPromise;
    }
    return this.status;
  }

  async #hydrate() {
    const keys = [...this.#allowedKeys];
    const localCandidates = new Map();
    const legacyCandidates = new Map();
    for (const key of keys) {
      localCandidates.set(key, this.#readLocalRecord(key));
      legacyCandidates.set(key, this.#readLegacyRecord(key));
    }

    const deviceResults = this.#device
      ? await Promise.allSettled(keys.map((key) => this.#readDeviceRecord(key)))
      : keys.map(() => ({ status: "fulfilled", value: null }));
    let deviceReadFailed = false;
    let lastError = this.#status.lastError;
    const sources = { preferences: "fresh", mapping: "fresh" };

    for (let index = 0; index < keys.length; index += 1) {
      const key = keys[index];
      const deviceResult = deviceResults[index];
      let deviceCandidate = null;
      if (deviceResult.status === "fulfilled") {
        deviceCandidate = deviceResult.value;
      } else {
        deviceReadFailed = true;
        lastError = errorMessage(deviceResult.reason);
      }

      const localCandidate = localCandidates.get(key);
      const legacyCandidate = legacyCandidates.get(key);
      if (localCandidate?.future || deviceCandidate?.future) {
        this.#blockedKeys.add(key);
        sources[sourceName(key, this.#mappingKey)] = "future";
        continue;
      }

      for (const candidate of [localCandidate, deviceCandidate]) {
        if (candidate?.record) {
          this.#lastTimestamp = Math.max(
            this.#lastTimestamp,
            candidate.record.record_updated_at,
          );
        }
      }

      let winner = newestCandidate([
        candidate(localCandidate, "local", 2),
        candidate(deviceCandidate, "device", 3),
        candidate(legacyCandidate, "legacy", 1),
      ]);
      if (winner?.source === "legacy") {
        winner = {
          ...winner,
          record: valueEnvelope(this.#nextTimestamp(), winner.record.value),
        };
      }
      if (!winner) {
        continue;
      }

      this.#installRecord(key, winner.record);
      sources[sourceName(key, this.#mappingKey)] = winner.source;
      this.#writeLocalRecord(key, winner.record);

      if (
        this.#device &&
        deviceResult.status === "fulfilled" &&
        this.#deviceRaw.get(key) !== serializeEnvelope(winner.record)
      ) {
        this.#queueDeviceWrite(key, winner.record);
      }
    }

    this.#setStatus({
      phase: lastError ? "degraded" : "ready",
      local: this.#local ? "ready" : this.#status.local,
      device: this.#device
        ? deviceReadFailed ? "error" : "ready"
        : "unavailable",
      sources: Object.freeze(sources),
      lastError,
    });
  }

  #readLocalRecord(key) {
    if (!this.#local) {
      return null;
    }
    const localKey = this.#localKeys.get(key);
    let raw;
    try {
      raw = this.#local.getItem(localKey);
    } catch (error) {
      this.#disableLocal(error);
      return null;
    }
    const parsed = parseEnvelope(raw);
    if (raw && !parsed) {
      try {
        this.#local.removeItem(localKey);
      } catch (error) {
        this.#disableLocal(error);
      }
    }
    return parsed;
  }

  #readLegacyRecord(key) {
    if (!this.#local) {
      return null;
    }
    try {
      const raw = this.#local.getItem(key);
      return typeof raw === "string" && raw.length > 0
        ? { record: valueEnvelope(0, raw), raw, future: false }
        : null;
    } catch (error) {
      this.#disableLocal(error);
      return null;
    }
  }

  async #readDeviceRecord(key) {
    const raw = await callbackCall(
      this.#device,
      "getItem",
      [this.#deviceKeys.get(key)],
      this.#timeoutMs,
    );
    const normalizedRaw = typeof raw === "string" && raw.length > 0
      ? raw
      : null;
    this.#deviceRaw.set(key, normalizedRaw);
    return parseEnvelope(normalizedRaw);
  }

  #installRecord(key, record) {
    this.#records.set(key, record);
    this.#lastTimestamp = Math.max(
      this.#lastTimestamp,
      record.record_updated_at,
    );
    if (record.deleted) {
      this.#values.delete(key);
    } else {
      this.#values.set(key, record.value);
    }
  }

  #writeLocalRecord(key, record) {
    if (!this.#local) {
      return;
    }
    try {
      this.#local.setItem(
        this.#localKeys.get(key),
        serializeEnvelope(record),
      );
      // Remove the old unwrapped record only after its scoped mirror exists.
      this.#local.removeItem(key);
      this.#setStatus({ local: "ready" });
    } catch (error) {
      this.#disableLocal(error);
    }
  }

  #disableLocal(error) {
    this.#local = null;
    this.#setStatus({
      local: "error",
      lastError: errorMessage(error),
    });
  }

  #queueDeviceWrite(key, record) {
    if (!this.#device || this.#blockedKeys.has(key)) {
      return;
    }
    const envelopeRaw = serializeEnvelope(record);
    if (this.#deviceRaw.get(key) === envelopeRaw) {
      return;
    }
    this.#desired.set(key, {
      sequence: ++this.#sequence,
      envelopeRaw,
    });
    this.#setStatus({ pending: true });
    this.#startSync();
  }

  #startSync() {
    if (this.#syncPromise || !this.#nextDesiredWrite()) {
      return;
    }
    this.#syncPromise = Promise.resolve()
      .then(() => this.#drainSync())
      .finally(() => {
        this.#syncPromise = null;
        const pending = Boolean(this.#nextDesiredWrite());
        this.#setStatus({
          phase: pending
            ? "syncing"
            : this.#status.lastError ? "degraded" : "ready",
          pending,
        });
        this.#startSync();
      });
  }

  async #drainSync() {
    let target;
    while ((target = this.#nextDesiredWrite())) {
      let failure = null;
      this.#setStatus({ phase: "syncing", device: "syncing" });
      for (let attempt = 1; attempt <= DEVICE_WRITE_ATTEMPTS; attempt += 1) {
        try {
          const stored = await callbackCall(
            this.#device,
            "setItem",
            [this.#deviceKeys.get(target.key), target.envelopeRaw],
            this.#timeoutMs,
          );
          if (stored === false) {
            throw new Error("Telegram DeviceStorage did not store the value.");
          }
          failure = null;
          break;
        } catch (error) {
          failure = error;
        }
      }

      this.#completedSequences.set(target.key, target.sequence);
      if (this.#desired.get(target.key)?.sequence === target.sequence) {
        this.#desired.delete(target.key);
      }
      if (failure) {
        this.#setStatus({
          device: "error",
          lastError: errorMessage(failure),
        });
      } else {
        this.#deviceRaw.set(target.key, target.envelopeRaw);
        this.#setStatus({
          device: "synced",
          lastError: null,
        });
      }
    }
  }

  #nextDesiredWrite() {
    return [...this.#desired.entries()]
      .filter(([key, desired]) =>
        desired.sequence > (this.#completedSequences.get(key) ?? 0)
      )
      .map(([key, desired]) => ({ key, ...desired }))
      .sort((left, right) => left.sequence - right.sequence)[0] ?? null;
  }

  #nextTimestamp() {
    const clockValue = Number(this.#clock());
    if (!Number.isSafeInteger(clockValue) || clockValue < 0) {
      throw new TypeError("The global bookmark storage clock is invalid.");
    }
    if (this.#lastTimestamp >= Number.MAX_SAFE_INTEGER) {
      throw new RangeError("The global bookmark storage timestamp is exhausted.");
    }
    const timestamp = Math.max(clockValue, this.#lastTimestamp + 1, 1);
    if (!Number.isSafeInteger(timestamp)) {
      throw new RangeError("The global bookmark storage timestamp is exhausted.");
    }
    this.#lastTimestamp = timestamp;
    return timestamp;
  }

  #requireKey(key) {
    if (typeof key !== "string" || !this.#allowedKeys.has(key)) {
      throw new TypeError(
        "The global bookmark storage key is outside this scope.",
      );
    }
  }

  #requireWritableKey(key) {
    this.#requireKey(key);
    if (this.#blockedKeys.has(key)) {
      throw new TypeError(
        "The global bookmark storage record requires a newer application.",
      );
    }
  }

  #setStatus(changes) {
    this.#status = Object.freeze({ ...this.#status, ...changes });
    this.#notify();
  }

  #notify() {
    if (!this.#listener) {
      return;
    }
    try {
      this.#listener(this.status);
    } catch {
      // A UI status listener cannot break persistence.
    }
  }
}

function sourceName(key, mappingKey) {
  return key === mappingKey ? "mapping" : "preferences";
}

function candidate(parsed, source, priority) {
  return parsed?.record
    ? { record: parsed.record, source, priority }
    : null;
}

function newestCandidate(values) {
  return values.filter(Boolean).sort((left, right) =>
    right.record.record_updated_at - left.record.record_updated_at ||
    right.priority - left.priority
  )[0] ?? null;
}

function valueEnvelope(recordUpdatedAt, value) {
  return Object.freeze({
    version: ENVELOPE_VERSION,
    record_updated_at: recordUpdatedAt,
    deleted: false,
    value,
  });
}

function tombstoneEnvelope(recordUpdatedAt) {
  return Object.freeze({
    version: ENVELOPE_VERSION,
    record_updated_at: recordUpdatedAt,
    deleted: true,
  });
}

function serializeEnvelope(record) {
  return JSON.stringify(record);
}

function parseEnvelope(raw) {
  if (typeof raw !== "string" || raw.length === 0) {
    return null;
  }
  try {
    const value = JSON.parse(raw);
    if (!isRecord(value)) {
      return null;
    }
    if (
      Number.isInteger(value.version) &&
      value.version > ENVELOPE_VERSION
    ) {
      return { future: true, raw };
    }
    if (
      value.version !== ENVELOPE_VERSION ||
      !Number.isSafeInteger(value.record_updated_at) ||
      value.record_updated_at < 1 ||
      typeof value.deleted !== "boolean"
    ) {
      return null;
    }
    const record = value.deleted
      ? tombstoneEnvelope(value.record_updated_at)
      : typeof value.value === "string"
        ? valueEnvelope(value.record_updated_at, value.value)
        : null;
    return record
      ? { future: false, raw: serializeEnvelope(record), record }
      : null;
  } catch {
    return null;
  }
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

function callbackCall(api, method, args, timeoutMs) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error(`Telegram DeviceStorage ${method} timed out.`));
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

function isRecord(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function errorMessage(error) {
  return error instanceof Error && error.message
    ? error.message
    : "Telegram DeviceStorage is temporarily unavailable.";
}
