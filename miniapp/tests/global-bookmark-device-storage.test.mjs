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
const INSTANCE_SCOPE = "1".repeat(16);
const OTHER_INSTANCE_SCOPE = "2".repeat(16);
const MAPPING_KEY =
  `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${INSTANCE_SCOPE}:${SCOPE}`;
const LEGACY_UNSCOPED_MAPPING_KEY =
  `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${SCOPE}`;

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

function localMirrorKey(
  kind,
  scope = SCOPE,
  instanceScope = INSTANCE_SCOPE,
) {
  return kind === "preferences"
    ? `${GLOBAL_BOOKMARK_LOCAL_MIRROR_PREFIX}:${scope}:preferences`
    : `${GLOBAL_BOOKMARK_LOCAL_MIRROR_PREFIX}:${instanceScope}:${scope}:mapping`;
}

function deviceKey(kind, scope = SCOPE, instanceScope = INSTANCE_SCOPE) {
  return kind === "preferences"
    ? `${GLOBAL_BOOKMARK_DEVICE_KEY_PREFIX}_${scope}_preferences`
    : `${GLOBAL_BOOKMARK_DEVICE_KEY_PREFIX}_${instanceScope}_${scope}_mapping`;
}

function openStorage(options = {}) {
  return GlobalBookmarkDeviceStorage.open({
    instanceScope: INSTANCE_SCOPE,
    ...options,
  });
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

test("requires an explicit Mini App instance scope", async () => {
  await assert.rejects(
    GlobalBookmarkDeviceStorage.open({ scope: SCOPE }),
    /instance scope/i,
  );
  await assert.rejects(
    GlobalBookmarkDeviceStorage.open({
      instanceScope: "invalid",
      scope: SCOPE,
    }),
    /instance scope/i,
  );
});

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

  const storage = await openStorage({
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

test("does not migrate a legacy unscoped private mapping", async () => {
  const local = new MemoryStorage();
  const legacyMapping = JSON.stringify({
    version: 1,
    topic_ids: { grace: "42" },
  });
  local.values.set(LEGACY_UNSCOPED_MAPPING_KEY, legacyMapping);

  const storage = await openStorage({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(null, "8.0"),
  });

  assert.equal(storage.getItem(MAPPING_KEY), null);
  assert.equal(local.values.get(LEGACY_UNSCOPED_MAPPING_KEY), legacyMapping);
  assert.equal(local.values.has(localMirrorKey("mapping")), false);
  assert.equal(storage.status.sources.mapping, "fresh");
});

test("shares visibility but isolates mappings between Mini App instances", async () => {
  const local = new MemoryStorage();
  const device = new DeviceStorageMock();
  const first = await openStorage({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(device),
    clock: () => 1_000,
  });
  first.setItem(GLOBAL_BOOKMARK_PREFERENCES_KEY, "shared visibility");
  first.setItem(MAPPING_KEY, "first instance mapping");
  await first.flush();

  const otherMappingKey =
    `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${OTHER_INSTANCE_SCOPE}:${SCOPE}`;
  const second = await openStorage({
    instanceScope: OTHER_INSTANCE_SCOPE,
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(device),
    clock: () => 2_000,
  });

  assert.equal(
    second.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY),
    "shared visibility",
  );
  assert.equal(second.getItem(otherMappingKey), null);
  assert.equal(
    local.values.has(
      localMirrorKey("mapping", SCOPE, OTHER_INSTANCE_SCOPE),
    ),
    false,
  );
  assert.equal(
    device.values.has(deviceKey("mapping", SCOPE, OTHER_INSTANCE_SCOPE)),
    false,
  );
});

test("restores device-local global state after a fresh WebView loses localStorage", async () => {
  const device = new DeviceStorageMock();
  const firstLocal = new MemoryStorage();
  const first = await openStorage({
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
  const reopened = await openStorage({
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

test("refreshes only a newer shared-local mapping envelope", async () => {
  const local = new MemoryStorage();
  const first = await openStorage({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(null, "8.0"),
    clock: () => 100,
  });
  const second = await openStorage({
    scope: SCOPE,
    localStorage: local,
    webApp: webApp(null, "8.0"),
    clock: () => 200,
  });

  first.setItem(MAPPING_KEY, "first mapping");
  assert.equal(second.refreshItem(MAPPING_KEY), true);
  assert.equal(second.getItem(MAPPING_KEY), "first mapping");
  second.setItem(MAPPING_KEY, "second mapping");
  assert.equal(first.refreshItem(MAPPING_KEY), true);
  assert.equal(first.getItem(MAPPING_KEY), "second mapping");
  assert.equal(first.refreshItem(MAPPING_KEY), false);

  local.values.set(localMirrorKey("mapping"), JSON.stringify({
    version: GLOBAL_BOOKMARK_DEVICE_ENVELOPE_VERSION + 1,
    record_updated_at: 300,
    deleted: false,
    value: "future mapping",
  }));
  assert.equal(first.refreshItem(MAPPING_KEY), false);
  assert.equal(first.getItem(MAPPING_KEY), "second mapping");
  assert.throws(() => first.setItem(MAPPING_KEY, "downgrade"), /newer/i);
});

test("requeues the current scoped envelope after a failed device write", async () => {
  const device = new DeviceStorageMock();
  device.falseMethods.add("setItem");
  const storage = await openStorage({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp(device),
    clock: () => 500,
  });

  storage.setItem(MAPPING_KEY, "published mapping");
  assert.equal((await storage.flush()).lastError !== null, true);
  assert.equal(storage.isItemDurable(MAPPING_KEY), true);
  device.falseMethods.delete("setItem");
  assert.equal(storage.retryItem(MAPPING_KEY), true);
  assert.equal((await storage.flush()).lastError, null);
  assert.equal(
    JSON.parse(device.values.get(deviceKey("mapping"))).value,
    "published mapping",
  );
  assert.throws(() => storage.retryItem("outside-scope"), /outside this scope/i);
});

test("promotion accepts either local or device durability and rejects memory only", async () => {
  const failingDevice = new DeviceStorageMock();
  failingDevice.falseMethods.add("setItem");
  const locallyDurableStorage = await openStorage({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp(failingDevice),
    clock: () => 700,
  });
  const locallyDurablePreferences = new GlobalBookmarkPreferences({
    allowedTopicIds: ["grace"],
    instanceScope: INSTANCE_SCOPE,
    scope: SCOPE,
    storage: locallyDurableStorage,
  });
  const promotion = await locallyDurablePreferences
    .reconcileContributionTopicMappings({
      catalogVersion: 1,
      mappings: { grace: "42" },
      promotedTopicIds: ["grace"],
      replacedLocalTopicIds: ["42"],
    }, [{ id: "42", name: "Grace" }]);
  assert.equal(promotion.changed, true);
  assert.equal(locallyDurablePreferences.hasTopic("grace"), true);

  const memoryOnlyStorage = await openStorage({
    scope: OTHER_SCOPE,
    localStorage: null,
    webApp: webApp(null, "8.0"),
    clock: () => 800,
  });
  const memoryOnlyPreferences = new GlobalBookmarkPreferences({
    allowedTopicIds: ["grace"],
    instanceScope: INSTANCE_SCOPE,
    scope: OTHER_SCOPE,
    storage: memoryOnlyStorage,
  });
  await assert.rejects(
    () => memoryOnlyPreferences.reconcileContributionTopicMappings({
      catalogVersion: 1,
      mappings: { grace: "42" },
      promotedTopicIds: ["grace"],
      replacedLocalTopicIds: ["42"],
    }, [{ id: "42", name: "Grace" }]),
    /could not be persisted/i,
  );
  assert.deepEqual(memoryOnlyPreferences.contributionTopicMappings, {});
  assert.equal(memoryOnlyPreferences.hasTopic("grace"), false);
  await assert.rejects(
    () => memoryOnlyPreferences.reconcileContributionTopicMappings({
      catalogVersion: 1,
      mappings: { grace: "42" },
      promotedTopicIds: ["grace"],
      replacedLocalTopicIds: ["42"],
    }, [{ id: "42", name: "Grace" }]),
    /could not be persisted/i,
  );
  assert.deepEqual(memoryOnlyPreferences.contributionTopicMappings, {});
  assert.equal(memoryOnlyPreferences.hasTopic("grace"), false);
});

test("reopens enabled global topics through the real preference facade", async () => {
  const device = new DeviceStorageMock();
  const firstStorage = await openStorage({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp(device),
    clock: () => 3_000,
  });
  const firstPreferences = new GlobalBookmarkPreferences({
    allowedTopicIds: ["grace", "hope"],
    instanceScope: INSTANCE_SCOPE,
    scope: SCOPE,
    storage: firstStorage,
  });
  firstPreferences.enableTopics(["grace", "hope"], 1);
  await firstPreferences.flush();

  const reopenedStorage = await openStorage({
    scope: SCOPE,
    localStorage: new MemoryStorage(),
    webApp: webApp(device),
    clock: () => 4_000,
  });
  const reopenedPreferences = new GlobalBookmarkPreferences({
    allowedTopicIds: ["grace", "hope"],
    instanceScope: INSTANCE_SCOPE,
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

  const storage = await openStorage({
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
  const first = await openStorage({
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
  const reopened = await openStorage({
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
  const storage = await openStorage({
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

  const storage = await openStorage({
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

  const storage = await openStorage({
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

  const storage = await openStorage({
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
  const storage = await openStorage({
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
      `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${INSTANCE_SCOPE}:${OTHER_SCOPE}`,
      "wrong account",
    ),
    /outside this scope/i,
  );
  assert.throws(
    () => storage.setItem(
      `${GLOBAL_BOOKMARK_TOPIC_MAPPING_PREFIX}:${OTHER_INSTANCE_SCOPE}:${SCOPE}`,
      "wrong instance",
    ),
    /outside this scope/i,
  );

  const other = await openStorage({
    scope: OTHER_SCOPE,
    localStorage: local,
    webApp: webApp(null, "8.0"),
  });
  assert.equal(other.getItem(GLOBAL_BOOKMARK_PREFERENCES_KEY), null);
  assert.equal(local.values.has(localMirrorKey("preferences", OTHER_SCOPE)), false);
});
