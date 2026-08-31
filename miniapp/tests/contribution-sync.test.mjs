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
  writes = 0;
  rejectWrites = false;
  async read() { return this.raw; }
  async write(raw) {
    if (this.rejectWrites) {
      throw new Error("journal write failed");
    }
    this.writes += 1;
    this.raw = String(raw);
    return true;
  }
  async remove() {
    this.raw = null;
    return true;
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

/** Recording GBC1 encoder stub with a configurable chunk count. */
function stubEncoder(total = 1) {
  const calls = [];
  const encode = async (envelope) => {
    calls.push(structuredClone(envelope));
    const messages = Array.from({ length: total }, (_, index) =>
      "GBC1|" + envelope.sync_id + "|" + (index + 1) + "|" + total + "|j|" +
        "a".repeat(64) + "|AAAA"
    );
    return {
      sync_id: envelope.sync_id,
      digest: "a".repeat(64),
      encoding: "j",
      messages,
    };
  };
  return { calls, encode };
}

function createSync(options = {}) {
  return new ContributionSync({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    storage: new MemoryStorage(),
    idFactory: () => "stable-client",
    initialStatus: approvedStatus(),
    ...options,
  });
}

function openOptions(journal, overrides = {}) {
  return {
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    journal,
    idFactory: () => "stable-client",
    initialStatus: approvedStatus(),
    ...overrides,
  };
}

test("preparePush persists an outbox whose envelope has the exact shape", async () => {
  const journal = new MemoryJournal();
  const { calls, encode } = stubEncoder();
  const sync = await ContributionSync.open(openOptions(journal));

  const outbox = await sync.preparePush(snapshot(), {
    disclosureAcknowledged: true,
    encode,
  });

  assert.equal(calls.length, 1);
  assert.deepEqual(Object.keys(calls[0]).sort(), [
    "client_id",
    "disclosure_acknowledged",
    "operations",
    "protocol_version",
    "snapshot",
    "sync_id",
  ]);
  assert.equal(calls[0].protocol_version, 1);
  assert.equal(calls[0].client_id, "stable-client");
  assert.equal(calls[0].disclosure_acknowledged, true);
  assert.deepEqual(calls[0].operations, []);
  assert.deepEqual(calls[0].snapshot, {
    topics: [personalTopic()],
    assignments: [{
      topic_id: "my-grace",
      book: 43,
      chapter: 3,
      verse: 16,
    }],
  });
  assert.match(calls[0].sync_id, /^stable-client:s:[0-9a-z]+:[0-9a-f]{16}$/);

  assert.deepEqual(outbox, {
    sync_id: calls[0].sync_id,
    fingerprint: calls[0].sync_id.split(":").at(-1),
    total: 1,
    attempt_index: 0,
    sent_all: false,
    disclosure_acknowledged: true,
  });

  // The exact transfer is durable before any sendData attempt.
  const stored = JSON.parse(journal.raw);
  assert.equal(stored.version, 3);
  assert.deepEqual(stored.outbox.envelope, calls[0]);
  assert.deepEqual(stored.outbox.messages, [
    "GBC1|" + calls[0].sync_id + "|1|1|j|" + "a".repeat(64) + "|AAAA",
  ]);
});

test("identical content resumes the same outbox without re-encoding", async () => {
  const { calls, encode } = stubEncoder();
  const sync = createSync();

  const first = await sync.preparePush(snapshot(), { encode });
  const second = await sync.preparePush(snapshot(), { encode });

  assert.equal(calls.length, 1);
  assert.equal(second.sync_id, first.sync_id);
  assert.deepEqual(second, first);
});

test("changed content mints a new sync identity", async () => {
  const { calls, encode } = stubEncoder();
  const sync = createSync();

  const first = await sync.preparePush(snapshot(), { encode });
  const changed = snapshot({
    bookmarks: [
      bookmark(["my-grace"]),
      bookmark(["my-grace"], { verse: 17 }),
    ],
  });
  await sync.captureMutation(snapshot(), changed);
  const second = await sync.preparePush(changed, { encode });

  assert.equal(calls.length, 2);
  assert.notEqual(second.sync_id, first.sync_id);
  assert.equal(calls[1].snapshot.assignments.length, 2);
});

test("explicit global operations coalesce to the newest stable intent", async () => {
  const definition = coreTopic();
  const sync = createSync({ coreTopics: [definition] });
  const verse = { book: 43, chapter: 3, verse: 16 };

  assert.equal(sync.captureGlobalRemoval(definition, verse, snapshot()), 1);
  assert.equal(sync.captureGlobalRemoval(definition, verse, snapshot()), 0);
  assert.equal(sync.pendingCount, 2);
  await sync.captureMutation(snapshot(), snapshot());
  assert.equal(sync.captureGlobalAddition(definition, verse, snapshot()), 1);
  assert.equal(sync.pendingCount, 2);

  const { calls, encode } = stubEncoder();
  await sync.preparePush(snapshot(), { encode });
  assert.equal(calls[0].operations.length, 1);
  assert.equal(calls[0].operations[0].type, "verse_add");
  assert.match(calls[0].operations[0].client_event_id, /:e:/);
});

test("captures are refused without contribution authority", () => {
  const sync = createSync({
    coreTopics: [coreTopic()],
    initialStatus: approvedStatus({ can_contribute: false }),
  });
  const verse = { book: 43, chapter: 3, verse: 16 };

  assert.equal(sync.canContribute, false);
  assert.equal(sync.captureGlobalRemoval(coreTopic(), verse, snapshot()), 0);
  assert.equal(sync.captureGlobalAddition(coreTopic(), verse, snapshot()), 0);
  assert.equal(sync.pendingCount, 1);
});

test("explicit operations stop at the durable outbox maximum", () => {
  const sync = createSync({ coreTopics: [coreTopic()] });
  const assignments = Array.from({ length: 2_000 }, (_, index) => ({
    topic: coreTopic(),
    verse: { book: 1, chapter: 1, verse: index + 1 },
  }));

  assert.equal(sync.captureGlobalAdditions(assignments, snapshot()), 2_000);
  assert.throws(
    () => sync.captureGlobalAddition(
      coreTopic(),
      { book: 2, chapter: 1, verse: 1 },
      snapshot(),
    ),
    RangeError,
  );
  assert.equal(sync.pendingCount, 2_001);
});

test("core metadata is authoritative and noncanonical data is filtered", async () => {
  const authoritative = coreTopic();
  const sync = createSync({ coreTopics: [authoritative] });
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

  const { calls, encode } = stubEncoder();
  await sync.preparePush(current, { encode });

  assert.deepEqual(calls[0].snapshot.topics, [authoritative]);
  assert.deepEqual(calls[0].snapshot.assignments, [{
    topic_id: "authority",
    book: 43,
    chapter: 3,
    verse: 16,
  }]);
});

test("fallback core IDs cannot leak local metadata or orphan assignments", async () => {
  const sync = createSync({ coreTopicIds: ["authority"] });
  const current = snapshot({
    topics: [coreTopic({ color: "#ff0000" })],
    bookmarks: [bookmark(["authority"])],
  });

  const { calls, encode } = stubEncoder();
  await sync.preparePush(current, { encode });

  assert.deepEqual(calls[0].snapshot, {
    topics: [],
    assignments: [],
  });
});

test("invalid snapshot data fails before any encoding", async () => {
  const sync = createSync();
  const { calls, encode } = stubEncoder();

  await assert.rejects(
    sync.preparePush(snapshot({
      topics: [personalTopic({ color: "red" })],
    }), { encode }),
    /bookmark topic is invalid/,
  );
  assert.equal(calls.length, 0);
  assert.equal(sync.outbox, null);
});

test("preparePush rejects a missing encoder or invalid encoded messages", async () => {
  const sync = createSync();

  await assert.rejects(
    sync.preparePush(snapshot()),
    /push message encoder is required/i,
  );
  await assert.rejects(
    sync.preparePush(snapshot(), {
      encode: async () => ({ messages: ["NOT-GBC1|payload"] }),
    }),
    /encoded push messages are invalid/i,
  );
  assert.equal(sync.outbox, null);
});

test("takeNextPushMessage durably advances the pointer before returning", async () => {
  const journal = new MemoryJournal();
  const { encode } = stubEncoder(2);
  const sync = await ContributionSync.open(openOptions(journal));
  await sync.preparePush(snapshot(), { encode });
  const stored = JSON.parse(journal.raw).outbox.messages;

  const writesBefore = journal.writes;
  const first = await sync.takeNextPushMessage();

  assert.equal(first, stored[0]);
  assert.equal(journal.writes, writesBefore + 1);
  assert.equal(JSON.parse(journal.raw).outbox.attempt_index, 1);
  assert.equal(sync.outbox.attempt_index, 1);
  assert.equal(sync.outbox.sent_all, false);

  const second = await sync.takeNextPushMessage();
  assert.equal(second, stored[1]);
  assert.equal(sync.outbox.sent_all, true);
  assert.equal(await sync.takeNextPushMessage(), null);
});

test("a persist failure keeps the unsent message at the pointer", async () => {
  const storage = new MemoryStorage();
  const { encode } = stubEncoder(2);
  const sync = createSync({ storage });
  const outbox = await sync.preparePush(snapshot(), { encode });
  const expected = "GBC1|" + outbox.sync_id + "|1|2|j|" + "a".repeat(64) + "|AAAA";

  storage.rejectWrites = true;
  await assert.rejects(
    sync.takeNextPushMessage(),
    /retry storage is unavailable/i,
  );
  assert.equal(sync.outbox.attempt_index, 0);
  assert.equal(sync.persistenceFailed, true);

  storage.rejectWrites = false;
  assert.equal(await sync.takeNextPushMessage(), expected);
  assert.equal(sync.outbox.attempt_index, 1);
});

test("rewindPushMessage undoes one advance and restartPush replays the transfer", async () => {
  const { encode } = stubEncoder(3);
  const sync = createSync();

  assert.equal(await sync.rewindPushMessage(), false);
  assert.equal(await sync.restartPush(), false);

  await sync.preparePush(snapshot(), { encode });
  assert.equal(await sync.rewindPushMessage(), false);

  const first = await sync.takeNextPushMessage();
  assert.equal(await sync.rewindPushMessage(), true);
  assert.equal(sync.outbox.attempt_index, 0);
  assert.equal(await sync.takeNextPushMessage(), first);

  await sync.takeNextPushMessage();
  await sync.takeNextPushMessage();
  assert.equal(sync.outbox.sent_all, true);
  assert.equal(await sync.restartPush(), true);
  assert.equal(sync.outbox.attempt_index, 0);
  assert.equal(await sync.takeNextPushMessage(), first);
});

test("a matching receipt clears exactly the sent operations and the outbox", async () => {
  const sync = createSync({ coreTopics: [coreTopic()] });
  sync.captureGlobalRemoval(
    coreTopic(),
    { book: 43, chapter: 3, verse: 16 },
    snapshot(),
  );
  const { calls, encode } = stubEncoder();
  const outbox = await sync.preparePush(snapshot(), { encode });
  const sentEventId = calls[0].operations[0].client_event_id;

  // A capture after the outbox was built survives the receipt.
  sync.captureGlobalAddition(
    coreTopic(),
    { book: 1, chapter: 1, verse: 1 },
    snapshot(),
  );

  assert.equal(await sync.confirmReceipt({ sync_id: outbox.sync_id }), true);
  assert.equal(sync.outbox, null);
  assert.equal(sync.baselineComplete, false);
  assert.equal(sync.pendingCount, 2);

  const followUp = stubEncoder();
  await sync.preparePush(snapshot(), { encode: followUp.encode });
  assert.equal(followUp.calls[0].operations.length, 1);
  assert.notEqual(followUp.calls[0].operations[0].client_event_id, sentEventId);
  assert.deepEqual(followUp.calls[0].operations[0].verse, {
    book: 1,
    chapter: 1,
    verse: 1,
  });
});

test("an undisturbed receipt resets the dirty baseline", async () => {
  const sync = createSync();
  const { encode } = stubEncoder();
  const outbox = await sync.preparePush(snapshot(), { encode });

  assert.equal(sync.baselineComplete, false);
  assert.equal(await sync.confirmReceipt({ sync_id: outbox.sync_id }), true);
  assert.equal(sync.baselineComplete, true);
  assert.equal(sync.pendingCount, 0);
});

test("a foreign or absent receipt is a no-op", async () => {
  const sync = createSync({ coreTopics: [coreTopic()] });
  assert.equal(await sync.confirmReceipt({ sync_id: "unknown:s:0:0" }), false);

  sync.captureGlobalRemoval(
    coreTopic(),
    { book: 43, chapter: 3, verse: 16 },
    snapshot(),
  );
  const { encode } = stubEncoder();
  const outbox = await sync.preparePush(snapshot(), { encode });

  assert.equal(await sync.confirmReceipt({ sync_id: "other:s:0:0" }), false);
  assert.equal(await sync.confirmReceipt(null), false);
  assert.deepEqual(sync.outbox, outbox);
  assert.equal(sync.pendingCount, 2);

  assert.equal(await sync.discardPush(), true);
  assert.equal(sync.outbox, null);
  assert.equal(sync.pendingCount, 2);
});

test("a persistence failure in preparePush throws and leaves no outbox", async () => {
  const storage = new MemoryStorage();
  const sync = createSync({ storage });
  const { encode } = stubEncoder();
  storage.rejectWrites = true;

  await assert.rejects(
    sync.preparePush(snapshot(), { encode }),
    /push was not prepared/i,
  );
  assert.equal(sync.outbox, null);
  assert.equal(sync.persistenceFailed, true);
});

test("a journal failure in preparePush commits no durable outbox", async () => {
  const journal = new MemoryJournal();
  const { encode } = stubEncoder();
  const sync = await ContributionSync.open(openOptions(journal));
  journal.rejectWrites = true;

  await assert.rejects(
    sync.preparePush(snapshot(), { encode }),
    /journal write failed/,
  );
  assert.equal(JSON.parse(journal.raw).outbox, null);
  assert.equal(sync.persistenceFailed, true);
});

test("reopening preserves the outbox byte-for-byte across launches", async () => {
  const journal = new MemoryJournal();
  const options = openOptions(journal, { coreTopics: [coreTopic()] });
  const first = await ContributionSync.open(options);
  await first.captureGlobalRemoval(
    coreTopic(),
    { book: 1, chapter: 1, verse: 1 },
    snapshot(),
  );
  const { encode } = stubEncoder(3);
  const prepared = await first.preparePush(snapshot(), { encode });
  const sentFirst = await first.takeNextPushMessage();
  const storedMessages = [...JSON.parse(journal.raw).outbox.messages];

  const reopened = await ContributionSync.open({
    ...options,
    initialStatus: undefined,
  });

  assert.deepEqual(reopened.outbox, {
    ...prepared,
    attempt_index: 1,
  });
  assert.deepEqual(JSON.parse(journal.raw).outbox.messages, storedMessages);

  // Preparing the identical content resumes without re-encoding a byte.
  const resume = stubEncoder(3);
  const resumed = await reopened.preparePush(snapshot(), {
    encode: resume.encode,
  });
  assert.equal(resume.calls.length, 0);
  assert.equal(resumed.sync_id, prepared.sync_id);
  assert.equal(resumed.attempt_index, 1);

  assert.equal(sentFirst, storedMessages[0]);
  assert.equal(await reopened.takeNextPushMessage(), storedMessages[1]);
  assert.equal(await reopened.takeNextPushMessage(), storedMessages[2]);
  assert.equal(reopened.outbox.sent_all, true);
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
  const sync = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    journal,
    coreTopics: [coreTopic()],
    idFactory: () => "migrated-client",
  });

  assert.equal(sync.pendingCount, 2);
  const { calls, encode } = stubEncoder();
  await sync.preparePush(snapshot(), { encode });
  assert.equal(calls[0].operations.length, 1);
  assert.equal(
    calls[0].operations[0].client_event_id,
    "event:g:remove-one",
  );
  assert.equal(JSON.parse(journal.raw).version, 3);
});

test("v2 migration drops the HTTP inflight envelope but keeps every intent", async () => {
  const operation = {
    client_event_id: "legacy-client:e:2",
    type: "verse_remove",
    topic: {
      local_topic_id: "authority",
      name: "Authority of the Bible",
      color: "#93c5fd",
    },
    verse: { book: 43, chapter: 3, verse: 16 },
  };
  const journal = new MemoryJournal();
  journal.raw = JSON.stringify({
    version: 2,
    client_id: "legacy-client",
    next_sequence: 9,
    dirty: false,
    revision: 4,
    operations: [operation],
    inflight: {
      fingerprint: "0123456789abcdef",
      envelope: {
        protocol_version: 1,
        sync_id: "legacy-client:s:3:0123456789abcdef",
        client_id: "legacy-client",
        snapshot: { topics: [], assignments: [] },
        operations: [],
        disclosure_acknowledged: true,
      },
    },
    status: approvedStatus(),
  });
  const sync = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    journal,
    idFactory: () => "unused-fresh-client",
  });

  // The v2 inflight cannot travel sendData: no outbox, forced dirty.
  assert.equal(sync.outbox, null);
  assert.equal(sync.baselineComplete, false);
  assert.equal(sync.pendingCount, 2);

  const stored = JSON.parse(journal.raw);
  assert.equal(stored.version, 3);
  assert.equal(stored.outbox, null);
  assert.equal(stored.dirty, true);
  assert.equal(stored.client_id, "legacy-client");
  assert.equal(stored.next_sequence, 9);
  assert.deepEqual(stored.operations, [operation]);

  const { calls, encode } = stubEncoder();
  await sync.preparePush(snapshot(), { encode });
  assert.equal(calls[0].client_id, "legacy-client");
  assert.match(calls[0].sync_id, /^legacy-client:s:9:[0-9a-f]{16}$/);
  assert.deepEqual(calls[0].operations, [operation]);
});

test("status clones are isolated and seedStatus replaces review outcomes", async () => {
  const sync = createSync();
  const exposed = sync.status;
  exposed.topics.push({ broken: true });
  assert.deepEqual(sync.topicOutcomes, []);
  assert.equal(sync.reviewDetailsAvailable, true);

  await sync.seedStatus(approvedStatus({
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
  }));

  assert.equal(sync.canContribute, false);
  assert.equal(sync.disclosureRequired, false);
  assert.equal(sync.contributorState, "revoked");
  assert.equal(sync.topicOutcomes[0].state, "rejected");
  assert.equal(sync.reviewSummary.topics.rejected, 1);
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
    idFactory: () => "migrated-client",
  });

  assert.equal(sync.canContribute, true);
  assert.equal(storage.getItem(key), null);
  assert.equal(JSON.parse(journal.raw).version, 3);
});
