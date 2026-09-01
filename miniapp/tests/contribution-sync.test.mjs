import assert from "node:assert/strict";
import test from "node:test";

import {
  CONTRIBUTION_STORAGE_PREFIX,
  ContributionSync,
  baselineContributionEvents,
  contributionEventsForDiff,
  isEnglishContributionTopicName,
} from "../lib/contribution-sync.js";

const SCOPE = "c".repeat(64);
const INSTANCE_SCOPE = "4".repeat(16);

class MemoryStorage {
  values = new Map();
  rejectWrites = false;
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) {
    if (this.rejectWrites) throw new Error("storage write failed");
    this.values.set(key, String(value));
  }
  removeItem(key) { this.values.delete(key); }
}

class MemoryJournal {
  raw = null;
  rejectWrites = false;
  async read() { return this.raw; }
  async write(raw) {
    if (this.rejectWrites) {
      throw new Error("journal write failed");
    }
    this.raw = String(raw);
    return true;
  }
  async remove() {
    this.raw = null;
    return true;
  }
}

class SyncApiMock {
  calls = [];
  failures = 0;
  status = approvedStatus();

  async syncContributions(envelope) {
    this.calls.push(structuredClone(envelope));
    if (this.failures > 0) {
      this.failures -= 1;
      throw Object.assign(new Error("response lost"), { code: "network_error" });
    }
    return syncResponse(envelope, this.status);
  }

  async contributionStatus() {
    return structuredClone(this.status);
  }
}

function approvedStatus(overrides = {}) {
  return {
    enabled: true,
    state: "approved",
    can_contribute: true,
    disclosure_required: false,
    topics: [],
    summary: {
      topics: {
        pending: 0,
        mapped: 0,
        published: 0,
        rejected: 0,
        deferred: 0,
      },
      events: {
        pending: 0,
        approved: 0,
        rejected: 0,
        deferred: 0,
        applied: 0,
      },
    },
    ...overrides,
  };
}

function syncResponse(envelope, status = approvedStatus()) {
  return {
    protocol_version: 1,
    receipt: {
      sync_id: envelope.sync_id,
      snapshot_digest: "a".repeat(64),
      outcome: "accepted",
      accepted: envelope.operations.length + envelope.snapshot.assignments.length,
      replayed: 0,
      event_ids: Object.fromEntries(
        envelope.operations.map((operation, index) => [
          operation.client_event_id,
          index + 1,
        ]),
      ),
    },
    status: structuredClone(status),
    catalog: {
      revision: 7,
      checksum: "b".repeat(64),
    },
  };
}

function personalTopic(overrides = {}) {
  return {
    id: "my-grace",
    name: "My Grace",
    color: "#bbf7d0",
    ...overrides,
  };
}

function coreTopic(overrides = {}) {
  return {
    id: "authority",
    name: "Authority of the Bible",
    color: "#93c5fd",
    ...overrides,
  };
}

function bookmark(topicIds, overrides = {}) {
  return {
    topic_ids: topicIds,
    topic_id: topicIds[0],
    book: 43,
    chapter: 3,
    verse: 16,
    text: "Browser display text is not transported.",
    ...overrides,
  };
}

function snapshot({
  topics = [personalTopic()],
  bookmarks = [bookmark(["my-grace"])],
} = {}) {
  return {
    active_topic_id: topics[0]?.id ?? null,
    topics,
    bookmarks,
  };
}

function createSync(api, options = {}) {
  return new ContributionSync({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    api,
    storage: new MemoryStorage(),
    idFactory: () => "stable-client",
    initialStatus: approvedStatus(),
    ...options,
  });
}

test("first snapshot uses one request and returns status, receipt, and catalog", async () => {
  const api = new SyncApiMock();
  const sync = createSync(api);

  const report = await sync.synchronizeNow(snapshot(), {
    disclosureAcknowledged: true,
  });

  assert.equal(api.calls.length, 1);
  assert.deepEqual(Object.keys(api.calls[0]).sort(), [
    "client_id",
    "disclosure_acknowledged",
    "operations",
    "protocol_version",
    "snapshot",
    "sync_id",
  ]);
  assert.equal(api.calls[0].protocol_version, 1);
  assert.equal(api.calls[0].disclosure_acknowledged, true);
  assert.deepEqual(api.calls[0].snapshot, {
    topics: [personalTopic()],
    assignments: [{
      topic_id: "my-grace",
      book: 43,
      chapter: 3,
      verse: 16,
    }],
  });
  assert.equal(report.sent, 1);
  assert.equal(report.pending, 0);
  assert.equal(report.status.can_contribute, true);
  assert.equal(report.review_details_available, true);
  assert.deepEqual(report.topic_outcomes, []);
  assert.equal(report.catalog.revision, 7);
  assert.equal(sync.pendingCount, 0);
  assert.equal(sync.recovering, false);
});

test("a committed sync accepts only the documented catalog fallback", async () => {
  const api = new SyncApiMock();
  api.syncContributions = async function (envelope) {
    this.calls.push(structuredClone(envelope));
    return {
      ...syncResponse(envelope),
      catalog: { revision: null, checksum: null, available: false },
    };
  };
  const sync = createSync(api);

  const report = await sync.synchronizeNow(snapshot());

  assert.deepEqual(report.catalog, {
    revision: null,
    checksum: null,
    available: false,
  });
});

test("an invalid receipt cannot clear the durable retry envelope", async () => {
  const api = new SyncApiMock();
  api.syncContributions = async function (envelope) {
    this.calls.push(structuredClone(envelope));
    const response = syncResponse(envelope);
    response.receipt.event_ids = { invalid: 0 };
    return response;
  };
  const sync = createSync(api);

  await assert.rejects(
    sync.synchronizeNow(snapshot()),
    /invalid contribution sync response/i,
  );

  assert.equal(sync.pendingCount, 1);
});

test("a lost response retries the exact same persisted ID and body", async () => {
  const api = new SyncApiMock();
  api.failures = 1;
  const sync = createSync(api);
  const current = snapshot();

  await assert.rejects(sync.synchronizeNow(current), /response lost/);
  const report = await sync.synchronizeNow(current);

  assert.equal(api.calls.length, 2);
  assert.deepEqual(api.calls[1], api.calls[0]);
  assert.equal(report.pending, 0);
});

test("a changed snapshot after failure receives a new sync ID", async () => {
  const api = new SyncApiMock();
  api.failures = 1;
  const sync = createSync(api);

  await assert.rejects(sync.synchronizeNow(snapshot()), /response lost/);
  const changed = snapshot({
    bookmarks: [
      bookmark(["my-grace"]),
      bookmark(["my-grace"], { verse: 17 }),
    ],
  });
  await sync.captureMutation(snapshot(), changed);
  await sync.synchronizeNow(changed);

  assert.equal(api.calls.length, 2);
  assert.notEqual(api.calls[0].sync_id, api.calls[1].sync_id);
  assert.equal(api.calls[1].snapshot.assignments.length, 2);
});

test("an artificial fingerprint collision cannot replay a stale body", async () => {
  const firstJournal = new MemoryJournal();
  const firstApi = new SyncApiMock();
  firstApi.failures = 1;
  const options = {
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    initialStatus: approvedStatus(),
    idFactory: () => "stable-client",
  };
  const first = await ContributionSync.open({
    ...options,
    journal: firstJournal,
    api: firstApi,
  });
  await assert.rejects(first.synchronizeNow(snapshot()), /response lost/);

  const changed = snapshot({
    bookmarks: [
      bookmark(["my-grace"]),
      bookmark(["my-grace"], { verse: 17 }),
    ],
  });
  const probeJournal = new MemoryJournal();
  const probeApi = new SyncApiMock();
  probeApi.failures = 1;
  const probe = await ContributionSync.open({
    ...options,
    journal: probeJournal,
    api: probeApi,
  });
  await assert.rejects(probe.synchronizeNow(changed), /response lost/);
  const changedFingerprint = probeApi.calls[0].sync_id.split(":").at(-1);

  // Simulate the theoretically possible case where the compact hashes match
  // even though the stored canonical body does not.
  const stored = JSON.parse(firstJournal.raw);
  stored.inflight.fingerprint = changedFingerprint;
  firstJournal.raw = JSON.stringify(stored);

  const retryApi = new SyncApiMock();
  const reopened = await ContributionSync.open({
    ...options,
    initialStatus: undefined,
    journal: firstJournal,
    api: retryApi,
  });
  await reopened.synchronizeNow(changed);

  assert.notEqual(retryApi.calls[0].sync_id, firstApi.calls[0].sync_id);
  assert.deepEqual(retryApi.calls[0].snapshot.assignments, [
    {
      topic_id: "my-grace",
      book: 43,
      chapter: 3,
      verse: 16,
    },
    {
      topic_id: "my-grace",
      book: 43,
      chapter: 3,
      verse: 17,
    },
  ]);
});

test("explicit global operations coalesce to the newest stable intent", async () => {
  const api = new SyncApiMock();
  const definition = coreTopic();
  const sync = createSync(api, { coreTopics: [definition] });
  const verse = { book: 43, chapter: 3, verse: 16 };

  assert.equal(sync.captureGlobalRemoval(definition, verse, snapshot()), 1);
  assert.equal(sync.captureGlobalRemoval(definition, verse, snapshot()), 0);
  assert.equal(sync.pendingCount, 2);
  await sync.captureMutation(snapshot(), snapshot());
  assert.equal(sync.captureGlobalAddition(definition, verse, snapshot()), 1);
  assert.equal(sync.pendingCount, 2);

  await sync.synchronizeNow(snapshot());
  assert.equal(api.calls[0].operations.length, 1);
  assert.equal(api.calls[0].operations[0].type, "verse_add");
  assert.match(api.calls[0].operations[0].client_event_id, /:e:/);
  assert.equal(sync.pendingCount, 0);
});

test("reopening retains explicit operations and an exact in-flight envelope", async () => {
  const journal = new MemoryJournal();
  const firstApi = new SyncApiMock();
  firstApi.failures = 1;
  const options = {
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    journal,
    coreTopics: [coreTopic()],
    idFactory: () => "stable-client",
    initialStatus: approvedStatus(),
  };
  const first = await ContributionSync.open({ ...options, api: firstApi });
  await first.captureGlobalRemoval(
    coreTopic(),
    { book: 1, chapter: 1, verse: 1 },
    snapshot(),
  );
  await assert.rejects(first.synchronizeNow(snapshot()), /response lost/);
  const sent = structuredClone(firstApi.calls[0]);

  const secondApi = new SyncApiMock();
  const reopened = await ContributionSync.open({
    ...options,
    api: secondApi,
    initialStatus: undefined,
  });
  await reopened.synchronizeNow(snapshot());

  assert.deepEqual(secondApi.calls[0], sent);
  assert.equal(reopened.pendingCount, 0);
});

test("status clones are isolated and a denied transport suspends local authority", async () => {
  const api = new SyncApiMock();
  const sync = createSync(api);
  const exposed = sync.status;
  exposed.topics.push({ broken: true });
  assert.deepEqual(sync.topicOutcomes, []);

  api.syncContributions = async () => {
    throw Object.assign(new Error("revoked"), {
      code: "contribution_not_allowed",
    });
  };
  await assert.rejects(sync.synchronizeNow(snapshot()), /revoked/);
  assert.equal(sync.canContribute, false);
  assert.equal(sync.disclosureRequired, false);
});

test("server status from the one sync response updates review outcomes", async () => {
  const api = new SyncApiMock();
  api.status = approvedStatus({
    can_contribute: false,
    state: "revoked",
    topics: [{
      local_topic_id: "my-grace",
      state: "rejected",
      published: false,
    }],
    summary: {
      topics: {
        pending: 0,
        mapped: 0,
        published: 0,
        rejected: 1,
        deferred: 0,
      },
      events: {
        pending: 0,
        approved: 0,
        rejected: 1,
        deferred: 0,
        applied: 0,
      },
    },
  });
  const sync = createSync(api);

  const report = await sync.synchronizeNow(snapshot());

  assert.equal(api.calls.length, 1);
  assert.equal(sync.canContribute, false);
  assert.equal(report.status.state, "revoked");
  assert.equal(report.topic_outcomes[0].state, "rejected");
});

test("core metadata is authoritative and noncanonical data is filtered", async () => {
  const api = new SyncApiMock();
  const authoritative = coreTopic();
  const sync = createSync(api, { coreTopics: [authoritative] });
  const current = snapshot({
    topics: [
      personalTopic({ name: "Привет" }),
      coreTopic({ name: "Locally Changed", color: "#ff0000" }),
    ],
    bookmarks: [
      bookmark(["authority"]),
      bookmark(["authority"], { book: 99, chapter: 1, verse: 1 }),
      bookmark(["my-grace"], { verse: 17 }),
    ],
  });

  await sync.synchronizeNow(current);

  assert.deepEqual(api.calls[0].snapshot.topics, [authoritative]);
  assert.deepEqual(api.calls[0].snapshot.assignments, [{
    topic_id: "authority",
    book: 43,
    chapter: 3,
    verse: 16,
  }]);
});

test("fallback core IDs cannot leak local metadata or orphan assignments", async () => {
  const api = new SyncApiMock();
  const sync = createSync(api, { coreTopicIds: ["authority"] });
  const current = snapshot({
    topics: [coreTopic({ color: "#ff0000" })],
    bookmarks: [bookmark(["authority"])],
  });

  await sync.synchronizeNow(current);

  assert.deepEqual(api.calls[0].snapshot, {
    topics: [],
    assignments: [],
  });
});

test("invalid snapshot data fails before transport", async () => {
  const api = new SyncApiMock();
  const sync = createSync(api);

  await assert.rejects(
    sync.synchronizeNow(snapshot({
      topics: [personalTopic({ color: "red" })],
    })),
    /bookmark topic is invalid/,
  );
  assert.equal(api.calls.length, 0);
});

test("v1 migration keeps global intent and discards reconstructible personal events", async () => {
  const journal = new MemoryJournal();
  journal.raw = JSON.stringify({
    version: 1,
    approved: true,
    contributor_state: "approved",
    disclosure_required: false,
    next_sequence: 4,
    outbox: [
      {
        client_event_id: "event:g:remove-one",
        type: "verse_remove",
        topic: {
          local_topic_id: "authority",
          name: "Authority of the Bible",
          color: "#93c5fd",
        },
        verse: { book: 43, chapter: 3, verse: 16 },
      },
      {
        client_event_id: "event:p:personal-one",
        type: "verse_remove",
        topic: {
          local_topic_id: "my-grace",
          name: "My Grace",
          color: "#bbf7d0",
        },
        verse: { book: 43, chapter: 3, verse: 17 },
      },
    ],
    recovery_external: null,
    recovery_external_latest: null,
  });
  const api = new SyncApiMock();
  const sync = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    journal,
    api,
    coreTopics: [coreTopic()],
    idFactory: () => "migrated-client",
  });

  assert.equal(sync.pendingCount, 2);
  await sync.synchronizeNow(snapshot());
  assert.equal(api.calls[0].operations.length, 1);
  assert.equal(
    api.calls[0].operations[0].client_event_id,
    "event:g:remove-one",
  );
  assert.equal(JSON.parse(journal.raw).version, 2);
});

test("the retry envelope must persist before network I/O", async () => {
  const journal = new MemoryJournal();
  const api = new SyncApiMock();
  const sync = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    journal,
    api,
    idFactory: () => "stable-client",
    initialStatus: approvedStatus(),
  });
  journal.rejectWrites = true;

  await assert.rejects(sync.synchronizeNow(snapshot()), /journal write failed/);
  assert.equal(api.calls.length, 0);
  assert.equal(sync.persistenceFailed, true);
});

test("fallback storage failure also prevents ambiguous network I/O", async () => {
  const storage = new MemoryStorage();
  const api = new SyncApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage,
    api,
    idFactory: () => "stable-client",
    initialStatus: approvedStatus(),
  });
  storage.rejectWrites = true;

  await assert.rejects(
    sync.synchronizeNow(snapshot()),
    /retry storage is unavailable/i,
  );
  assert.equal(api.calls.length, 0);
  assert.equal(sync.persistenceFailed, true);
});

test("cleanup storage failure cannot relabel a confirmed commit", async () => {
  const journal = new MemoryJournal();
  const api = new SyncApiMock();
  api.syncContributions = async function (envelope) {
    this.calls.push(structuredClone(envelope));
    journal.rejectWrites = true;
    return syncResponse(envelope);
  };
  const sync = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    journal,
    api,
    idFactory: () => "stable-client",
    initialStatus: approvedStatus(),
  });

  const report = await sync.synchronizeNow(snapshot());

  assert.equal(report.sent, 1);
  assert.equal(api.calls.length, 1);
  assert.equal(sync.persistenceFailed, true);
});

test("compatibility diff helpers remain deterministic", () => {
  const empty = snapshot({ bookmarks: [] });
  const current = snapshot();
  assert.deepEqual(
    contributionEventsForDiff(empty, current).map((event) => event.type),
    ["verse_add"],
  );
  const baseline = baselineContributionEvents(current, new Set());
  assert.deepEqual(
    baseline.map((event) => event.type),
    ["topic_upsert", "verse_add"],
  );
  assert.equal(
    baseline.every((event) => event.client_event_id.startsWith("baseline:")),
    true,
  );
  assert.equal(isEnglishContributionTopicName("Authority of the Bible"), true);
  assert.equal(isEnglishContributionTopicName("Библия"), false);
});

test("legacy localStorage values migrate into the transactional journal", async () => {
  const storage = new MemoryStorage();
  const key = CONTRIBUTION_STORAGE_PREFIX + ":" + SCOPE;
  storage.setItem(key, JSON.stringify({
    version: 1,
    approved: true,
    contributor_state: "approved",
    disclosure_required: false,
    outbox: [],
    recovery_external: null,
    recovery_external_latest: null,
  }));
  const journal = new MemoryJournal();
  const sync = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    journal,
    legacyStorage: storage,
    api: new SyncApiMock(),
    idFactory: () => "migrated-client",
  });

  assert.equal(sync.canContribute, true);
  assert.equal(storage.getItem(key), null);
  assert.equal(JSON.parse(journal.raw).version, 2);
});
