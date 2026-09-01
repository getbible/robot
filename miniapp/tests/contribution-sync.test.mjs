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

  async submitContributionEvents(events, options = {}) {
    this.calls.push({
      events: structuredClone(events),
      options: structuredClone(options),
    });
    if (this.failures > 0) {
      this.failures -= 1;
      throw Object.assign(new Error("response lost"), { code: "network_error" });
    }
    return eventsResponse(events, this.status);
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

function eventsResponse(events, status = approvedStatus()) {
  return {
    accepted: events.length,
    replayed: 0,
    event_ids: Object.fromEntries(
      events.map((event, index) => [event.client_event_id, index + 1]),
    ),
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
    waitImplementation: () => Promise.resolve(),
    ...options,
  });
}

test("one Sync drips the snapshot as idempotent events and returns the result set", async () => {
  const api = new SyncApiMock();
  const sync = createSync(api);

  const report = await sync.synchronizeNow(snapshot(), {
    disclosureAcknowledged: true,
  });

  assert.equal(api.calls.length, 1);
  const { events, options } = api.calls[0];
  assert.deepEqual(options, { disclosureAcknowledged: true });
  assert.deepEqual(
    events.map((event) => event.type),
    ["topic_upsert", "verse_add"],
  );
  assert.ok(events.every((event) =>
    /^baseline:(?:topic_upsert|verse_add):[a-f0-9]{16}$/.test(event.client_event_id)
  ));
  assert.deepEqual(events[0].topic, {
    local_topic_id: "my-grace",
    name: "My Grace",
    color: "#bbf7d0",
  });
  assert.deepEqual(events[1].verse, { book: 43, chapter: 3, verse: 16 });
  assert.equal(report.sent, 2);
  assert.equal(report.replayed, 0);
  assert.equal(report.pending, 0);
  assert.equal(report.status.can_contribute, true);
  assert.equal(report.review_details_available, true);
  assert.deepEqual(report.topic_outcomes, []);
  assert.equal(report.catalog.revision, 7);
  assert.equal(sync.pendingCount, 0);
  assert.equal(sync.recovering, false);
});

test("a large contribution is dripped in search-sized ordered batches", async () => {
  const api = new SyncApiMock();
  const sync = createSync(api);
  const topics = Array.from({ length: 60 }, (_, index) =>
    personalTopic({ id: `topic-${index}`, name: `Topic ${index + 1}` })
  );
  const bookmarks = Array.from({ length: 30 }, (_, index) =>
    bookmark([`topic-${index}`], { verse: index + 1 })
  );

  const report = await sync.synchronizeNow(
    snapshot({ topics, bookmarks }),
    { disclosureAcknowledged: true },
  );

  // Every request stays in the size class the network path has proven with
  // search: a couple of kilobytes, never anywhere near a large upload.
  assert.ok(api.calls.length > 1);
  for (const { events } of api.calls) {
    assert.ok(events.length <= 50);
    assert.ok(new TextEncoder().encode(JSON.stringify(events)).byteLength <= 2_400);
  }
  assert.deepEqual(
    api.calls.map(({ options }) => options.disclosureAcknowledged),
    [true, ...api.calls.slice(1).map(() => false)],
  );
  const identifiers = api.calls.flatMap(({ events }) =>
    events.map((event) => event.client_event_id)
  );
  assert.equal(new Set(identifiers).size, identifiers.length);
  assert.equal(identifiers.length, 90);
  const types = api.calls.flatMap(({ events }) =>
    events.map((event) => event.type)
  );
  assert.equal(types.lastIndexOf("topic_upsert") < types.indexOf("verse_add"), true);
  assert.equal(report.sent, 90);
  assert.equal(report.pending, 0);
});

test("an upload-hostile path is crossed by halving down to single events", async () => {
  const api = new SyncApiMock();
  const submit = api.submitContributionEvents.bind(api);
  let rejectedLarge = 0;
  api.submitContributionEvents = async (events, options) => {
    if (events.length > 1) {
      rejectedLarge += 1;
      throw Object.assign(new Error("connection reset"), {
        code: "network_error",
        retryable: true,
      });
    }
    return submit(events, options);
  };
  const sync = createSync(api);
  const topics = Array.from({ length: 12 }, (_, index) =>
    personalTopic({ id: `topic-${index}`, name: `Topic ${index + 1}` })
  );

  const report = await sync.synchronizeNow(snapshot({ topics, bookmarks: [] }));

  assert.ok(rejectedLarge > 0);
  assert.equal(report.sent, 12);
  assert.equal(report.pending, 0);
  assert.equal(api.calls.length, 12);
  assert.ok(api.calls.every(({ events }) => events.length === 1));
});

test("a single-event upload failure is retried once and then surfaced", async () => {
  const api = new SyncApiMock();
  let attempts = 0;
  api.submitContributionEvents = async () => {
    attempts += 1;
    throw Object.assign(new Error("connection reset"), {
      code: "network_error",
      retryable: true,
    });
  };
  const waits = [];
  const sync = createSync(api, {
    waitImplementation: (milliseconds) => {
      waits.push(milliseconds);
      return Promise.resolve();
    },
  });

  await assert.rejects(
    sync.synchronizeNow(snapshot({
      topics: [personalTopic()],
      bookmarks: [],
    })),
    /connection reset/,
  );
  assert.equal(attempts, 2);
  assert.deepEqual(waits, [1_500]);
  assert.equal(sync.pendingCount, 1);
});

test("rate limiting paces the drip and retries the same batch", async () => {
  const waits = [];
  const api = new SyncApiMock();
  let limited = true;
  const submit = api.submitContributionEvents.bind(api);
  api.submitContributionEvents = async (events, options) => {
    if (limited) {
      limited = false;
      throw Object.assign(new Error("Please wait."), {
        code: "rate_limited",
        status: 429,
        retryAfter: 7,
      });
    }
    return submit(events, options);
  };
  const sync = createSync(api, {
    waitImplementation: (milliseconds) => {
      waits.push(milliseconds);
      return Promise.resolve();
    },
  });

  const report = await sync.synchronizeNow(snapshot());

  assert.deepEqual(waits, [7_000]);
  assert.equal(api.calls.length, 1);
  assert.equal(report.sent, 2);
});

test("a persistent rate limit surfaces after bounded pacing attempts", async () => {
  const waits = [];
  const api = new SyncApiMock();
  let attempts = 0;
  api.submitContributionEvents = async () => {
    attempts += 1;
    throw Object.assign(new Error("Please wait."), {
      code: "rate_limited",
      status: 429,
    });
  };
  const sync = createSync(api, {
    waitImplementation: (milliseconds) => {
      waits.push(milliseconds);
      return Promise.resolve();
    },
  });

  await assert.rejects(sync.synchronizeNow(snapshot()), /wait/i);
  assert.equal(attempts, 6);
  assert.deepEqual(waits, [2_000, 2_000, 2_000, 2_000, 2_000]);
  assert.equal(sync.pendingCount, 1);
});

test("a missing contributor token is recovered with one status request", async () => {
  const api = new SyncApiMock();
  let statusCalls = 0;
  let tokenReady = false;
  const submit = api.submitContributionEvents.bind(api);
  api.submitContributionEvents = async (events, options) => {
    if (!tokenReady) {
      throw Object.assign(new Error("token missing"), {
        code: "contribution_transport_not_ready",
        status: 403,
      });
    }
    return submit(events, options);
  };
  api.contributionStatus = async () => {
    statusCalls += 1;
    tokenReady = true;
    return structuredClone(api.status);
  };
  const sync = createSync(api);

  const report = await sync.synchronizeNow(snapshot());

  assert.equal(statusCalls, 1);
  assert.equal(report.sent, 2);
  assert.equal(api.calls.length, 1);
});

test("a server-refused stale token is recovered instead of read as revocation", async () => {
  const api = new SyncApiMock();
  let statusCalls = 0;
  let tokenFresh = false;
  const submit = api.submitContributionEvents.bind(api);
  api.submitContributionEvents = async (events, options) => {
    if (!tokenFresh) {
      throw Object.assign(new Error("capability expired"), {
        code: "contribution_not_allowed",
        status: 403,
      });
    }
    return submit(events, options);
  };
  api.contributionStatus = async () => {
    statusCalls += 1;
    tokenFresh = true;
    return structuredClone(api.status);
  };
  const sync = createSync(api);

  const report = await sync.synchronizeNow(snapshot());

  assert.equal(statusCalls, 1);
  assert.equal(report.sent, 2);
  assert.equal(sync.canContribute, true);
});

test("the drip reports batch progress without letting display break transfer", async () => {
  const api = new SyncApiMock();
  const sync = createSync(api);
  const topics = Array.from({ length: 60 }, (_, index) =>
    personalTopic({ id: `topic-${index}`, name: `Topic ${index + 1}` })
  );
  const progress = [];

  await sync.synchronizeNow(snapshot({ topics, bookmarks: [] }), {
    onProgress(update) {
      progress.push({ ...update });
      throw new Error("display failure must not stop the drip");
    },
  });

  assert.equal(progress.length, api.calls.length);
  assert.ok(progress.length > 1);
  assert.deepEqual(
    progress,
    progress.map((_, index) => ({ batch: index + 1, total: progress.length })),
  );
});

test("a committed sync accepts only the documented catalog fallback", async () => {
  const api = new SyncApiMock();
  api.submitContributionEvents = async function (events, options = {}) {
    this.calls.push({ events: structuredClone(events), options });
    return {
      ...eventsResponse(events),
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

test("an invalid response cannot mark local work as synchronized", async () => {
  const api = new SyncApiMock();
  api.submitContributionEvents = async function (events, options = {}) {
    this.calls.push({ events: structuredClone(events), options });
    const response = eventsResponse(events);
    response.event_ids = { invalid: 0 };
    return response;
  };
  const sync = createSync(api);

  await assert.rejects(
    sync.synchronizeNow(snapshot()),
    /invalid contribution sync response/i,
  );

  assert.equal(sync.pendingCount, 1);
});

test("a lost response is recovered inside the same drip with the same identities", async () => {
  const api = new SyncApiMock();
  api.failures = 1;
  const sync = createSync(api);
  const current = snapshot();

  // The first request dies on the wire; the drip halves and delivers the
  // exact same deterministic events without failing the synchronization.
  const report = await sync.synchronizeNow(current);
  assert.equal(report.sent, 2);
  assert.equal(report.pending, 0);
  const delivered = [...new Set(api.calls
    .flatMap(({ events }) => events.map((event) => event.client_event_id)))].sort();

  const replayApi = new SyncApiMock();
  const replaySync = createSync(replayApi);
  await replaySync.synchronizeNow(snapshot());
  const replayed = [...new Set(replayApi.calls
    .flatMap(({ events }) => events.map((event) => event.client_event_id)))].sort();
  assert.deepEqual(replayed, delivered);
});

test("changed content mints new identities while unchanged events keep theirs", async () => {
  const api = new SyncApiMock();
  const sync = createSync(api);
  const current = snapshot();
  await sync.synchronizeNow(current);

  const changed = snapshot({
    bookmarks: [
      bookmark(["my-grace"]),
      bookmark(["my-grace"], { verse: 17 }),
    ],
  });
  await sync.captureMutation(current, changed);
  await sync.synchronizeNow(changed);

  const firstIds = api.calls[0].events.map((event) => event.client_event_id);
  const secondIds = api.calls[1].events.map((event) => event.client_event_id);
  assert.equal(secondIds.length, 3);
  // The untouched topic and verse keep their content-derived identities, so
  // the server replays them; only the new verse creates a new event.
  for (const id of firstIds) {
    assert.ok(secondIds.includes(id));
  }
  assert.equal(
    secondIds.filter((id) => !firstIds.includes(id)).length,
    1,
  );
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
  const explicit = api.calls[0].events.filter((event) =>
    /:e:/.test(event.client_event_id)
  );
  assert.equal(explicit.length, 1);
  assert.equal(explicit[0].type, "verse_add");
  assert.equal(sync.pendingCount, 0);
});

test("reopening retains explicit operations with their exact identities", async () => {
  const journal = new MemoryJournal();
  const firstApi = new SyncApiMock();
  firstApi.submitContributionEvents = async () => {
    throw Object.assign(new Error("response lost"), {
      code: "network_error",
      retryable: true,
    });
  };
  const options = {
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    journal,
    coreTopics: [coreTopic()],
    idFactory: () => "stable-client",
    initialStatus: approvedStatus(),
    waitImplementation: () => Promise.resolve(),
  };
  const first = await ContributionSync.open({ ...options, api: firstApi });
  await first.captureGlobalRemoval(
    coreTopic(),
    { book: 1, chapter: 1, verse: 1 },
    snapshot(),
  );
  const explicitId = JSON.parse(journal.raw).operations[0].client_event_id;
  await assert.rejects(first.synchronizeNow(snapshot()), /response lost/);

  const secondApi = new SyncApiMock();
  const reopened = await ContributionSync.open({
    ...options,
    api: secondApi,
    initialStatus: undefined,
  });
  await reopened.synchronizeNow(snapshot());

  const delivered = secondApi.calls.flatMap(({ events }) =>
    events.map((event) => event.client_event_id)
  );
  assert.ok(delivered.includes(explicitId));
  assert.equal(reopened.pendingCount, 0);
});

test("status clones are isolated and a denied transport suspends local authority", async () => {
  const api = new SyncApiMock();
  const sync = createSync(api);
  const exposed = sync.status;
  exposed.topics.push({ broken: true });
  assert.deepEqual(sync.topicOutcomes, []);

  api.submitContributionEvents = async () => {
    throw Object.assign(new Error("revoked"), {
      code: "contribution_not_allowed",
    });
  };
  await assert.rejects(sync.synchronizeNow(snapshot()), /revoked/);
  assert.equal(sync.canContribute, false);
  assert.equal(sync.disclosureRequired, false);
});

test("server status from the drip responses updates review outcomes", async () => {
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

  const { events } = api.calls[0];
  assert.deepEqual(
    events.map((event) => event.type),
    ["topic_upsert", "verse_add"],
  );
  assert.deepEqual(events[0].topic, {
    local_topic_id: "authority",
    name: "Authority of the Bible",
    color: "#93c5fd",
  });
  assert.deepEqual(events[1].verse, { book: 43, chapter: 3, verse: 16 });
});

test("an empty contribution refreshes standing without sending events", async () => {
  const api = new SyncApiMock();
  const sync = createSync(api, { coreTopicIds: ["authority"] });
  const current = snapshot({
    topics: [coreTopic({ color: "#ff0000" })],
    bookmarks: [bookmark(["authority"])],
  });

  const report = await sync.synchronizeNow(current);

  assert.equal(api.calls.length, 0);
  assert.equal(report.sent, 0);
  assert.equal(report.pending, 0);
  assert.equal(report.status.state, "approved");
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
    waitImplementation: () => Promise.resolve(),
  });

  assert.equal(sync.pendingCount, 2);
  await sync.synchronizeNow(snapshot());
  const explicit = api.calls[0].events.filter((event) =>
    !event.client_event_id.startsWith("baseline:")
  );
  assert.equal(explicit.length, 1);
  assert.equal(explicit[0].client_event_id, "event:g:remove-one");
  assert.equal(JSON.parse(journal.raw).version, 2);
});

test("capturing an explicit intent fails loudly when the journal cannot persist", async () => {
  const journal = new MemoryJournal();
  const api = new SyncApiMock();
  const sync = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    journal,
    api,
    coreTopics: [coreTopic()],
    idFactory: () => "stable-client",
    initialStatus: approvedStatus(),
    waitImplementation: () => Promise.resolve(),
  });
  journal.rejectWrites = true;

  await assert.rejects(
    Promise.resolve(sync.captureGlobalRemoval(
      coreTopic(),
      { book: 1, chapter: 1, verse: 1 },
      snapshot(),
    )),
    /journal write failed/,
  );
  assert.equal(sync.persistenceFailed, true);
});

test("a persistence failure cannot relabel a committed drip", async () => {
  const journal = new MemoryJournal();
  const api = new SyncApiMock();
  const submit = api.submitContributionEvents.bind(api);
  api.submitContributionEvents = async (events, options) => {
    journal.rejectWrites = true;
    return submit(events, options);
  };
  const sync = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    journal,
    api,
    idFactory: () => "stable-client",
    initialStatus: approvedStatus(),
    waitImplementation: () => Promise.resolve(),
  });

  const report = await sync.synchronizeNow(snapshot());

  assert.equal(report.sent, 2);
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
    waitImplementation: () => Promise.resolve(),
  });

  assert.equal(sync.canContribute, true);
  assert.equal(storage.getItem(key), null);
  assert.equal(JSON.parse(journal.raw).version, 2);
});
