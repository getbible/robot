import assert from "node:assert/strict";
import test from "node:test";

import {
  GLOBAL_BOOKMARK_DEVICE_ENVELOPE_VERSION,
  GLOBAL_BOOKMARK_DEVICE_KEY_PREFIX,
  GLOBAL_BOOKMARK_LOCAL_MIRROR_PREFIX,
  GlobalBookmarkDeviceStorage,
} from "../lib/global-bookmark-device-storage.js";
import {
  GLOBAL_BOOKMARK_PREFERENCES_KEY,
  GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX,
  GlobalBookmarkPreferences,
} from "../lib/global-bookmark-preferences.js";

const SCOPE = "a".repeat(64);
const OTHER_SCOPE = "b".repeat(64);
const MAPPING_KEY = `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${SCOPE}`;

class MemoryStorage {
  values = new Map();
  calls = [];
  errors = new Map();

  getItem(key) {
    this.calls.push(["getItem", key]);
    this.#fail("getItem");
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.calls.push(["setItem", key, String(value)]);
    this.#fail("setItem");
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.calls.push(["removeItem", key]);
    this.#fail("removeItem");
    this.values.delete(key);
  }

  #fail(method) {
    if (this.errors.has(method)) {
      throw this.errors.get(method);
    }
  }
}

class DeviceStorageMock {
  values = new Map();
  calls = [];
  errors = new Map();
  falseMethods = new Set();
  timeoutMethods = new Set();

  getItem(key, callback) {
    this.calls.push(["getItem", key]);
    this.#reply("getItem", callback, this.values.get(key) ?? "");
  }

  setItem(key, value, callback) {
    this.calls.push(["setItem", key, String(value)]);
    if (
      !this.timeoutMethods.has("setItem") &&
      !this.errors.has("setItem") &&
      !this.falseMethods.has("setItem")
    ) {
      this.values.set(key, String(value));
    }
    this.#reply(
      "setItem",
      callback,
      !this.falseMethods.has("setItem"),
    );
  }

  removeItem(key, callback) {
    this.calls.push(["removeItem", key]);
    this.#reply("removeItem", callback, true);
  }

  #reply(method, callback, result) {
    if (this.timeoutMethods.has(method)) {
      return;
    }
    queueMicrotask(() => callback(this.errors.get(method) ?? null, result));
  }
}

class ControlledDeviceStorage extends DeviceStorageMock {
  pending = [];

  setItem(key, value, callback) {
    this.calls.push(["setItem", key, String(value)]);
    this.pending.push({ key, value: String(value), callback });
  }

  completeNext() {
    const operation = this.pending.shift();
    assert.ok(operation, "A DeviceStorage write should be pending.");
    this.values.set(operation.key, operation.value);
    operation.callback(null, true);
  }
}

function webApp(device, version = "9.0", cloud = null) {
  return {
    version,
    DeviceStorage: device,
    CloudStorage: cloud,
    isVersionAtLeast(required) {
      return Number(version) >= Number(required);
    },
  };
}

function localMirrorKey(kind, scope = SCOPE) {
  return `${GLOBAL_BOOKMARK_LOCAL_MIRROR_PREFIX}:${scope}:${kind}`;
}

function deviceKey(kind, scope = SCOPE) {
  return `${GLOBAL_BOOKMARK_DEVICE_KEY_PREFIX}_${scope}_${kind}`;
}

function envelope(recordUpdatedAt, value) {
  return JSON.stringify({
    version: GLOBAL_BOOKMARK_DEVICE_ENVELOPE_VERSION,
    record_updated_at: recordUpdatedAt,
    deleted: false,
    value,
  });
}

function tombstone(recordUpdatedAt) {
  return JSON.stringify({
    version: GLOBAL_BOOKMARK_DEVICE_ENVELOPE_VERSION,
    record_updated_at: recordUpdatedAt,
    deleted: true,
  });
}

async function waitFor(predicate, message) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (predicate()) {
      return;
    }
    await Promise.resolve();
  }
  assert.fail(message);
}

test("migrates both legacy browser records into scoped local and DeviceStorage envelopes", async () => {
  const local = new MemoryStorage();
  const device = new DeviceStorageMock();
  const cloudCalls = [];
  const cloud = new Proxy({}, {
    get(_target, property) {
      cloudCalls.push(String(property));
      throw new Error("CloudStorage must not be accessed.");
    },
  });
  const preferences = JSON.stringify({
    version: 2,
    catalog_version: 1,
    enabled_topic_ids: ["grace"],
    hidden_bookmark_ids: [],
  });
  const mapping = JSON.stringify({
    version: 1,
    topic_ids: { grace: "grace" },
  });
  local.values.set(GLOBAL_BOOKMARK_PREFERENCES_KEY, preferences);
  local.values.set(MAPPING_KEY, mapping);

  const storage = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(device, "9.0", cloud),
    clock: () => 100,
  });
  await storage.flush();

  assert.equal(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), preferences);
  assert.equal(storage.getItem(MAPPING_KEY), mapping);
  assert.equal(local.values.has(GLOBAL_BOOKMARK_PREFERENCES_KEY), false);
  assert.equal(local.values.has(MAPPING_KEY), false);
  assert.deepEqual(JSON.parse(local.values.get(localMirrorKey("preferences"))), {
    version: 1,
    record_updated_at: 100,
    deleted: false,
    value: preferences,
  });
  assert.deepEqual(JSON.parse(local.values.get(localMirrorKey("mapping"))), {
    version: 1,
    record_updated_at: 101,
    deleted: false,
    value: mapping,
  });
  assert.equal(
    JSON.parse(device.values.get(deviceKey("preferences"))).value,
    preferences,
  );
  assert.equal(JSON.parse(device.values.get(deviceKey("mapping"))).value, mapping);
  assert.deepEqual(cloudCalls, []);
  assert.deepEqual(storage.status.sources, {
    preferences: "legacy",
    mapping: "legacy",
  });
});

test("restores device-local global state after a fresh WebView loses localStorage", async () => {
  const device = new DeviceStorageMock();
  const firstLocal = new MemoryStorage();
  const first = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: firstLocal,
    webApp: webApp(device),
    clock: () => 1_000,
  });
  const preferences = "opaque preference record";
  const mapping = "opaque mapping record";
  first.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, preferences);
  first.setItem(MAPPING_KEY, mapping);
  await first.flush();

  const freshDesktopLocalStorage = new MemoryStorage();
  const reopened = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: freshDesktopLocalStorage,
    webApp: webApp(device),
    clock: () => 2_000,
  });

  assert.equal(reopened.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), preferences);
  assert.equal(reopened.getItem(MAPPING_KEY), mapping);
  assert.equal(
    JSON.parse(
      freshDesktopLocalStorage.values.get(localMirrorKey("preferences")),
    ).value,
    preferences,
  );
  assert.equal(reopened.status.sources.preferences, "device");
  assert.equal(reopened.status.sources.mapping, "device");
});

test("reopens enabled global topics through the real preference facade", async () => {
  const device = new DeviceStorageMock();
  const firstStorage = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp(device),
    clock: () => 3_000,
  });
  const firstPreferences = new GlobalBookmarkPreferences({
    allowedTopicIds: ["grace", "hope"],
    scope: SCOPE,
    storage: firstStorage,
  });
  firstPreferences.enableTopics(["grace", "hope"], 1);
  await firstPreferences.flush();

  const reopenedStorage = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp(device),
    clock: () => 4_000,
  });
  const reopenedPreferences = new GlobalBookmarkPreferences({
    allowedTopicIds: ["grace", "hope"],
    scope: SCOPE,
    storage: reopenedStorage,
  });

  assert.equal(reopenedPreferences.enabled, true);
  assert.deepEqual(reopenedPreferences.enabledTopicIds, ["grace", "hope"]);
  assert.equal(reopenedPreferences.hasTopic("grace"), true);
  assert.equal(reopenedPreferences.hasTopic("hope"), true);
});

test("DeviceStorage wins timestamp ties and later writes remain monotonic", async () => {
  const local = new MemoryStorage();
  const device = new DeviceStorageMock();
  local.values.set(
    localMirrorKey("preferences"),
    envelope(50, "local value"),
  );
  device.values.set(
    deviceKey("preferences"),
    envelope(50, "device value"),
  );

  const storage = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(device),
    clock: () => 1,
  });

  assert.equal(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), "device value");
  assert.equal(storage.status.sources.preferences, "device");
  assert.equal(
    JSON.parse(local.values.get(localMirrorKey("preferences"))).value,
    "device value",
  );

  storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, "new value");
  await storage.flush();
  assert.deepEqual(JSON.parse(device.values.get(deviceKey("preferences"))), {
    version: 1,
    record_updated_at: 51,
    deleted: false,
    value: "new value",
  });
});

test("persists an explicit tombstone that defeats stale browser state after restart", async () => {
  const firstLocal = new MemoryStorage();
  const device = new DeviceStorageMock();
  const first = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: firstLocal,
    webApp: webApp(device),
    clock: () => 200,
  });
  first.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, "enabled");
  first.removeItem(GLOBAL_BOOKMARK_PREFERENCES_KEY);
  await first.flush();

  assert.equal(first.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), null);
  assert.deepEqual(JSON.parse(device.values.get(deviceKey("preferences"))), {
    version: 1,
    record_updated_at: 201,
    deleted: true,
  });
  assert.equal(
    device.calls.some(([method]) => method === "removeItem"),
    false,
  );

  const nextLocal = new MemoryStorage();
  nextLocal.values.set(GLOBAL_BOOKMARK_PREFERENCES_KEY, "stale enabled state");
  const reopened = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: nextLocal,
    webApp: webApp(device),
    clock: () => 300,
  });

  assert.equal(reopened.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), null);
  assert.equal(nextLocal.values.has(GLOBAL_BOOKMARK_PREFERENCES_KEY), false);
  assert.equal(
    nextLocal.values.get(localMirrorKey("preferences")),
    tombstone(201),
  );
});

test("coalesces rapid writes while preserving an already in-flight commit", async () => {
  const local = new MemoryStorage();
  const device = new ControlledDeviceStorage();
  const storage = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(device),
    clock: (() => {
      let now = 10;
      return () => now++;
    })(),
  });

  storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, "first");
  await waitFor(() => device.pending.length === 1, "First write did not start.");
  storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, "second");
  storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, "third");
  device.completeNext();
  await waitFor(() => device.pending.length === 1, "Newest write did not start.");
  device.completeNext();
  await storage.flush();

  const writes = device.calls
    .filter(([method]) => method === "setItem")
    .map(([, , raw]) => JSON.parse(raw).value);
  assert.deepEqual(writes, ["first", "third"]);
  assert.equal(
    JSON.parse(device.values.get(deviceKey("preferences"))).value,
    "third",
  );
});

test("falls back safely to the browser mirror when DeviceStorage times out", async () => {
  const local = new MemoryStorage();
  const device = new DeviceStorageMock();
  device.timeoutMethods.add("getItem");
  device.timeoutMethods.add("setItem");
  local.values.set(GLOBAL_BOOKMARK_PREFERENCES_KEY, "legacy local value");

  const storage = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(device),
    timeoutMs: 2,
    clock: () => 500,
  });

  assert.equal(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), "legacy local value");
  assert.equal(storage.status.device, "error");
  assert.match(storage.status.lastError, /timed out/i);

  storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, "new local value");
  const status = await storage.flush();
  assert.equal(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), "new local value");
  assert.equal(
    JSON.parse(local.values.get(localMirrorKey("preferences"))).value,
    "new local value",
  );
  assert.equal(status.device, "error");
  assert.match(status.lastError, /timed out/i);
});

test("repairs invalid device data from the newest valid local envelope", async () => {
  const local = new MemoryStorage();
  const device = new DeviceStorageMock();
  local.values.set(
    localMirrorKey("preferences"),
    envelope(42, "valid local value"),
  );
  device.values.set(deviceKey("preferences"), "not an envelope");

  const storage = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(device),
    clock: () => 100,
  });
  await storage.flush();

  assert.equal(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), "valid local value");
  assert.equal(
    device.values.get(deviceKey("preferences")),
    envelope(42, "valid local value"),
  );
});

test("preserves future envelopes and rejects writes that could downgrade them", async () => {
  const local = new MemoryStorage();
  const future = JSON.stringify({
    version: 2,
    record_updated_at: 900,
    deleted: false,
    value: "newer application value",
  });
  local.values.set(localMirrorKey("preferences"), future);

  const storage = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(null, "8.0"),
  });

  assert.equal(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), null);
  assert.equal(local.values.get(localMirrorKey("preferences")), future);
  assert.equal(storage.status.sources.preferences, "future");
  assert.throws(
    () => storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, "older value"),
    /newer application/i,
  );
  assert.throws(
    () => storage.removeItem(GLOBAL_BOOKMARK_PREFERENCES_KEY),
    /newer application/i,
  );
});

test("accepts only this account's preference and mapping keys and keeps payloads opaque", async () => {
  const local = new MemoryStorage();
  const storage = await GlobalBookmarkDeviceStorage.open({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(null, "8.0"),
    clock: () => 700,
  });
  const opaque = '{"bookmarks_by_topic":{"grace":[[43,3,16]]},"verse":"opaque"}';

  storage.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, opaque);
  assert.equal(storage.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), opaque);
  assert.equal(
    JSON.parse(local.values.get(localMirrorKey("preferences"))).value,
    opaque,
  );
  assert.throws(() => storage.getItem("unrelated"), /outside this scope/i);
  assert.throws(
    () => storage.setItem(
      `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${OTHER_SCOPE}`,
      "wrong account",
    ),
    /outside this scope/i,
  );

  const other = await GlobalBookmarkDeviceStorage.open({
    scope: OTHER_SCOPE,
    localStorage: local,
    webApp: webApp(null, "8.0"),
  });
  assert.equal(other.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), null);
  assert.equal(local.values.has(localMirrorKey("preferences", OTHER_SCOPE)), false);
});
