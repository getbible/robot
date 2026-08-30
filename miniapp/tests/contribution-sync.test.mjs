import assert from "node:assert/strict";
import test from "node:test";

import {
  CONTRIBUTION_BATCH_MAXIMUM,
  CONTRIBUTION_OUTBOX_MAXIMUM,
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
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

class QuotaStorage extends MemoryStorage {
  rejectWrites = false;
  setItem(key, value) {
    if (this.rejectWrites) {
      throw Object.assign(new Error("quota exceeded"), {
        name: "QuotaExceededError",
      });
    }
    super.setItem(key, value);
  }
}

class MemoryJournal {
  raw = null;
  rejectWrites = false;
  async read() { return this.raw; }
  async write(raw) {
    if (this.rejectWrites) {
      throw Object.assign(new Error("journal quota exceeded"), {
        name: "QuotaExceededError",
      });
    }
    this.raw = String(raw);
    return true;
  }
  async remove() {
    this.raw = null;
    return true;
  }
}

class SerialLockManager {
  tails = new Map();
  request(name, operation) {
    const previous = this.tails.get(name) ?? Promise.resolve();
    const result = previous.catch(() => undefined).then(operation);
    this.tails.set(name, result.catch(() => undefined));
    return result;
  }
}

class ContributionApiMock {
  batches = [];
  disclosureCalls = 0;
  failureAt = null;
  onBatch = null;
  statusCalls = 0;
  status = {
    enabled: true,
    state: "approved",
    can_contribute: true,
    disclosure_required: false,
  };

  async contributionStatus() {
    this.statusCalls += 1;
    return structuredClone(this.status);
  }
  async acknowledgeContributionDisclosure() {
    this.disclosureCalls += 1;
    this.status.disclosure_required = false;
    return { ...this.status };
  }
  async submitContributionEvents(events) {
    this.batches.push(structuredClone(events));
    this.onBatch?.(events);
    if (this.failureAt === this.batches.length) {
      throw Object.assign(new Error("temporary"), { code: "network_error" });
    }
    return {
      accepted: events.length,
      replayed: 0,
      event_ids: Object.fromEntries(
        events.map((event, index) => [event.client_event_id, index + 1]),
      ),
    };
  }
}

function topic(id = "grace", name = "Grace") {
  return { id, name, color: "#bbf7d0" };
}

function bookmark(index, topicIds = ["grace"], overrides = {}) {
  return {
    topic_ids: topicIds,
    topic_id: topicIds[0],
    book: 1,
    chapter: 1,
    verse: index,
    text: `Browser display text ${index}`,
    ...overrides,
  };
}

function snapshot({ topics = [topic()], bookmarks = [] } = {}) {
  return { active_topic_id: topics[0]?.id ?? null, topics, bookmarks };
}

function reviewStatus(overrides = {}) {
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

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test("seeds approved session status without a hidden network request", () => {
  const api = new ContributionApiMock();
  const initialStatus = reviewStatus({
    topics: [{
      local_topic_id: "my-topic",
      state: "pending",
      published: true,
      canonical_topic_id: "reviewed-topic",
      canonical_topic: {
        id: "reviewed-topic",
        name: "Reviewed Topic",
        color: "#123456",
        aliases: [],
      },
    }],
    summary: {
      topics: {
        pending: 1,
        mapped: 0,
        published: 1,
        rejected: 0,
        deferred: 0,
      },
      events: {
        pending: 2,
        approved: 0,
        rejected: 0,
        deferred: 0,
        applied: 4,
      },
    },
  });
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    initialStatus,
  });

  assert.equal(api.statusCalls, 0);
  assert.equal(sync.canContribute, true);
  assert.equal(sync.reviewDetailsAvailable, true);
  assert.equal(sync.contributorState, "approved");
  assert.equal(sync.baselineComplete, false);
  assert.equal(sync.reviewSummary.events.pending, 2);
  assert.equal(sync.topicOutcomes[0].canonical_topic_id, "reviewed-topic");
  const exposed = sync.status;
  exposed.topics[0].canonical_topic.aliases.push("mutated");
  assert.deepEqual(sync.status.topics[0].canonical_topic.aliases, []);
});

test("retains legacy review provenance through status clones and reports", async () => {
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    initialStatus: api.status,
  });

  assert.equal(sync.reviewDetailsAvailable, false);
  const cloned = sync.status;
  assert.equal(Object.hasOwn(cloned, "review_details_available"), false);
  await sync.seedStatus(cloned);
  assert.equal(sync.reviewDetailsAvailable, false);
  const legacyReport = await sync.synchronizeNow(snapshot());
  assert.equal(legacyReport.review_details_available, false);

  api.status = reviewStatus();
  await sync.refreshStatus();
  assert.equal(sync.reviewDetailsAvailable, true);
  api.status = {
    enabled: true,
    state: "approved",
    can_contribute: true,
    disclosure_required: false,
  };
  await sync.refreshStatus();
  assert.equal(sync.reviewDetailsAvailable, false);
});

test("manual sync refreshes stale approval and returns current review outcomes", async () => {
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    initialStatus: reviewStatus({
      state: "pending",
      can_contribute: false,
    }),
  });
  assert.equal(sync.canContribute, false);
  api.status = reviewStatus();
  api.onBatch = () => {
    api.status = reviewStatus({
      topics: [{
        local_topic_id: "my-topic",
        state: "mapped",
        published: false,
        canonical_topic_id: "reviewed-topic",
        canonical_topic: {
          id: "reviewed-topic",
          name: "Reviewed Topic",
          color: "#123456",
          aliases: [],
        },
      }],
      summary: {
        topics: {
          pending: 0,
          mapped: 1,
          published: 0,
          rejected: 0,
          deferred: 0,
        },
        events: {
          pending: 1,
          approved: 0,
          rejected: 0,
          deferred: 0,
          applied: 0,
        },
      },
    });
  };
  const current = snapshot({
    topics: [topic(), topic("my-topic", "My Topic")],
    bookmarks: [bookmark(16, ["my-topic"])],
  });

  const result = await sync.synchronizeNow(current);

  assert.equal(api.statusCalls, 2);
  assert.equal(result.sent, 2);
  assert.equal(result.pending, 0);
  assert.equal(result.review_details_available, true);
  assert.equal(result.status.summary.events.pending, 1);
  assert.equal(result.topic_outcomes[0].canonical_topic_id, "reviewed-topic");
  assert.equal(sync.canContribute, true);
});

test("manual sync reports a disclosure gate without submitting data", async () => {
  const api = new ContributionApiMock();
  api.status = reviewStatus({ disclosure_required: true });
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    initialStatus: reviewStatus({
      state: "pending",
      can_contribute: false,
    }),
  });

  const result = await sync.synchronizeNow(snapshot());

  assert.equal(result.sent, 0);
  assert.equal(result.status.disclosure_required, true);
  assert.equal(api.statusCalls, 1);
  assert.equal(api.batches.length, 0);
});

test("serializes concurrent status refreshes so the later authority wins", async () => {
  const api = new ContributionApiMock();
  const firstResponse = deferred();
  const secondResponse = deferred();
  const firstStarted = deferred();
  const secondStarted = deferred();
  api.statusCalls = 0;
  api.contributionStatus = () => {
    api.statusCalls += 1;
    if (api.statusCalls === 1) {
      firstStarted.resolve();
      return firstResponse.promise;
    }
    secondStarted.resolve();
    return secondResponse.promise;
  };
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    initialStatus: reviewStatus({ state: "pending", can_contribute: false }),
  });

  const older = sync.refreshStatus();
  await firstStarted.promise;
  const newer = sync.refreshStatus();
  assert.equal(api.statusCalls, 1);

  firstResponse.resolve(reviewStatus());
  await secondStarted.promise;
  assert.equal(api.statusCalls, 2);
  secondResponse.resolve(reviewStatus({
    state: "revoked",
    can_contribute: false,
  }));
  await Promise.all([older, newer]);

  assert.equal(sync.contributorState, "revoked");
  assert.equal(sync.canContribute, false);
});

test("orders disclosure PATCH after an older status refresh", async () => {
  const api = new ContributionApiMock();
  const staleResponse = deferred();
  const refreshStarted = deferred();
  api.contributionStatus = () => {
    api.statusCalls += 1;
    refreshStarted.resolve();
    return staleResponse.promise;
  };
  api.acknowledgeContributionDisclosure = async () => {
    api.disclosureCalls += 1;
    return reviewStatus({ disclosure_required: false });
  };
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    initialStatus: reviewStatus({ disclosure_required: true }),
  });

  const refresh = sync.refreshStatus();
  await refreshStarted.promise;
  const acknowledgement = sync.acknowledgeDisclosure();
  assert.equal(api.disclosureCalls, 0);

  staleResponse.resolve(reviewStatus({ disclosure_required: true }));
  await refresh;
  assert.equal(await acknowledgement, true);
  assert.equal(api.disclosureCalls, 1);
  assert.equal(sync.disclosureRequired, false);
});

test("a submit denial supersedes an older in-flight approved status", async () => {
  const api = new ContributionApiMock();
  const staleResponse = deferred();
  const refreshStarted = deferred();
  const submitStarted = deferred();
  api.contributionStatus = () => {
    api.statusCalls += 1;
    refreshStarted.resolve();
    return staleResponse.promise;
  };
  api.submitContributionEvents = async () => {
    submitStarted.resolve();
    throw Object.assign(new Error("contribution revoked"), {
      code: "contribution_not_allowed",
    });
  };
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    initialStatus: reviewStatus(),
  });
  const current = snapshot();
  assert.equal(
    sync.captureGlobalRemoval(
      topic(),
      { book: 43, chapter: 3, verse: 16 },
      current,
    ),
    1,
  );

  const refresh = sync.refreshStatus();
  await refreshStarted.promise;
  const drain = sync.synchronize(current);
  await submitStarted.promise;
  staleResponse.resolve(reviewStatus());

  await refresh;
  await assert.rejects(
    drain,
    (error) => error.code === "contribution_not_allowed",
  );
  assert.equal(sync.canContribute, false);
  assert.equal(sync.pendingCount, 1);
});

test("a generic 403 preserves authority and retries the pending journal", async () => {
  const api = new ContributionApiMock();
  const successfulSubmit = api.submitContributionEvents.bind(api);
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    initialStatus: reviewStatus(),
  });
  const current = snapshot();
  assert.equal(
    sync.captureGlobalRemoval(
      topic(),
      { book: 43, chapter: 3, verse: 16 },
      current,
    ),
    1,
  );
  api.submitContributionEvents = async () => {
    throw Object.assign(new Error("origin policy rejected the request"), {
      status: 403,
      code: "forbidden",
    });
  };

  await assert.rejects(
    sync.synchronize(current),
    (error) => error.status === 403 && error.code === "forbidden",
  );
  assert.equal(sync.canContribute, true);
  assert.equal(sync.pendingCount, 1);

  api.submitContributionEvents = successfulSubmit;
  assert.deepEqual(await sync.synchronize(current), { sent: 1, pending: 0 });
  assert.equal(api.batches[0][0].type, "verse_remove");
});

test("serializes status authority across clients sharing one journal", async () => {
  const journal = new MemoryJournal();
  const locks = new SerialLockManager();
  const firstApi = new ContributionApiMock();
  const secondApi = new ContributionApiMock();
  const firstResponse = deferred();
  const secondResponse = deferred();
  const firstStarted = deferred();
  const secondStarted = deferred();
  firstApi.contributionStatus = () => {
    firstApi.statusCalls += 1;
    firstStarted.resolve();
    return firstResponse.promise;
  };
  secondApi.contributionStatus = () => {
    secondApi.statusCalls += 1;
    secondStarted.resolve();
    return secondResponse.promise;
  };
  const options = {
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    coreTopicIds: ["grace"],
    journal,
    lockManager: locks,
  };
  const first = await ContributionSync.open({ ...options, api: firstApi });
  const second = await ContributionSync.open({ ...options, api: secondApi });

  const older = first.refreshStatus();
  await firstStarted.promise;
  const newer = second.refreshStatus();
  assert.equal(secondApi.statusCalls, 0);

  firstResponse.resolve(reviewStatus());
  await secondStarted.promise;
  secondResponse.resolve(reviewStatus({
    state: "revoked",
    can_contribute: false,
  }));
  await Promise.all([older, newer]);
  const reopened = await ContributionSync.open({
    ...options,
    api: new ContributionApiMock(),
  });

  assert.equal(reopened.contributorState, "revoked");
  assert.equal(reopened.canContribute, false);
});

test("preserves a denied global removal and drains it after reapproval", async () => {
  const api = new ContributionApiMock();
  const successfulSubmit = api.submitContributionEvents.bind(api);
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
  });
  const current = snapshot();
  const coordinate = { book: 43, chapter: 3, verse: 16 };
  await sync.refreshStatus();
  await sync.synchronize(current);
  assert.equal(
    sync.captureGlobalRemoval(topic(), coordinate, current),
    1,
  );
  assert.equal(sync.pendingCount, 1);

  let deniedEvent = null;
  let deniedCalls = 0;
  api.submitContributionEvents = async (events) => {
    deniedCalls += 1;
    deniedEvent = structuredClone(events[0]);
    throw Object.assign(new Error("contribution revoked"), {
      status: 403,
      code: "contribution_not_allowed",
    });
  };
  await assert.rejects(
    sync.synchronize(current),
    (error) => error.status === 403,
  );
  assert.equal(sync.canContribute, false);
  assert.equal(sync.pendingCount, 1);

  for (const state of ["rejected", "revoked"]) {
    api.status = reviewStatus({ state, can_contribute: false });
    await sync.refreshStatus();
    assert.equal(sync.canContribute, false);
    assert.equal(sync.pendingCount, 1);
    assert.deepEqual(await sync.synchronize(current), { sent: 0, pending: 1 });
    assert.equal(deniedCalls, 1);
    assert.equal(
      sync.captureGlobalAddition(topic(), coordinate, current),
      0,
    );
  }

  api.status = reviewStatus();
  await sync.refreshStatus();
  assert.equal(sync.canContribute, true);
  assert.equal(sync.baselineComplete, false);
  api.submitContributionEvents = successfulSubmit;
  const result = await sync.synchronize(current);

  assert.deepEqual(result, { sent: 1, pending: 0 });
  assert.equal(api.batches.flat()[0].type, "verse_remove");
  assert.equal(api.batches.flat()[0].client_event_id, deniedEvent.client_event_id);
  assert.equal(sync.pendingCount, 0);
});

test("stops compact recovery when authority changes during a drain", async () => {
  const api = new ContributionApiMock();
  let suspendAfterBatch = false;
  let sync;
  sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    maximumOutboxEvents: 1,
    batchPause: async () => {
      if (!suspendAfterBatch) return;
      suspendAfterBatch = false;
      await sync.seedStatus(reviewStatus({
        state: "revoked",
        can_contribute: false,
      }));
    },
  });
  await sync.refreshStatus();
  const empty = snapshot();
  const one = snapshot({ bookmarks: [bookmark(1)] });
  const two = snapshot({ bookmarks: [bookmark(1), bookmark(2)] });
  await sync.synchronize(empty);
  assert.equal(sync.captureMutation(empty, one), 1);
  assert.equal(sync.captureMutation(one, two), 0);
  assert.equal(sync.recovering, true);

  suspendAfterBatch = true;
  const blocked = await sync.synchronize(two);

  assert.deepEqual(blocked, { sent: 1, pending: 0 });
  assert.equal(sync.canContribute, false);
  assert.equal(sync.recovering, true);
  assert.equal(api.batches.length, 1);
  assert.equal(
    api.batches.flat().some((event) =>
      event.client_event_id.startsWith("recovery:")
    ),
    false,
  );

  await sync.seedStatus(reviewStatus());
  await sync.synchronize(two);
  assert.equal(sync.recovering, false);
});

test("accepts every server status and gates baseline behind disclosure", async () => {
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
  });
  for (const state of [
    "not_applied", "pending", "deferred", "rejected", "revoked", "unavailable",
  ]) {
    api.status = {
      enabled: state !== "unavailable",
      state,
      can_contribute: false,
      disclosure_required: false,
    };
    assert.equal((await sync.refreshStatus()).state, state);
    assert.equal(sync.canContribute, false);
  }

  api.status = {
    enabled: true,
    state: "approved",
    can_contribute: true,
    disclosure_required: true,
  };
  await sync.refreshStatus();
  const existing = snapshot({
    topics: [topic(), topic("my-topic", "My Topic")],
  });
  assert.deepEqual(await sync.synchronize(existing), { sent: 0, pending: 0 });
  assert.equal(api.batches.length, 0);
  assert.equal(await sync.acknowledgeDisclosure(), true);
  await sync.synchronize(existing);
  assert.equal(api.disclosureCalls, 1);
  assert.equal(api.batches.flat().length, 1);
  assert.equal(api.batches[0][0].topic.name, "My Topic");
});

test("baseline is reviewable, bounded, deterministic, and resumes by checkpoint", async () => {
  const storage = new MemoryStorage();
  const existing = snapshot({
    topics: [topic(), topic("study-notes", "Study Notes")],
    bookmarks: Array.from({ length: 60 }, (_, index) =>
      bookmark(index + 1, ["study-notes"])
    ),
  });
  const baseline = baselineContributionEvents(existing, new Set(["grace"]));
  assert.equal(baseline.length, 61);
  assert.equal(
    baseline.some((event) => event.topic.local_topic_id === "grace"),
    false,
  );
  assert.equal(
    baseline.every((event) => !Object.hasOwn(event.verse ?? {}, "text")),
    true,
  );
  assert.equal(new Set(baseline.map((event) => event.client_event_id)).size, 61);

  const firstApi = new ContributionApiMock();
  firstApi.failureAt = 2;
  const first = new ContributionSync({
    scope: SCOPE,
    api: firstApi,
    storage,
    coreTopicIds: ["grace"],
  });
  await first.refreshStatus();
  await assert.rejects(first.synchronize(existing), /temporary/);
  assert.equal(firstApi.batches[0].length, CONTRIBUTION_BATCH_MAXIMUM);

  const resumedApi = new ContributionApiMock();
  const resumed = new ContributionSync({
    scope: SCOPE,
    api: resumedApi,
    storage,
    coreTopicIds: ["grace"],
  });
  await resumed.refreshStatus();
  const result = await resumed.synchronize(existing);
  assert.equal(result.sent, 11);
  assert.equal(resumedApi.batches.length, 1);
  assert.equal(resumedApi.batches[0][0].client_event_id, baseline[50].client_event_id);
});

test("restarts a completed baseline once when a reopened snapshot changed", async () => {
  const journal = new MemoryJournal();
  const locks = new SerialLockManager();
  const initial = snapshot({ bookmarks: [bookmark(1)] });
  const changed = snapshot({ bookmarks: [bookmark(1), bookmark(2)] });
  const options = {
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    coreTopicIds: ["grace"],
    journal,
    lockManager: locks,
    initialStatus: reviewStatus(),
  };
  const first = await ContributionSync.open({
    ...options,
    api: new ContributionApiMock(),
  });
  assert.deepEqual(await first.synchronize(initial), { sent: 2, pending: 0 });

  const reopenedApi = new ContributionApiMock();
  const reopened = await ContributionSync.open({
    ...options,
    api: reopenedApi,
  });
  assert.equal(reopened.baselineComplete, true);
  const result = await reopened.synchronize(changed);

  assert.deepEqual(result, { sent: 3, pending: 0 });
  assert.deepEqual(
    reopenedApi.batches.flat().map((event) => event.type),
    ["topic_upsert", "verse_add", "verse_add"],
  );
  assert.equal(reopenedApi.batches.flat().at(-1).verse.verse, 2);
});

test("validates an unchanged reopen once then uses the normal outbox", async () => {
  const journal = new MemoryJournal();
  const locks = new SerialLockManager();
  const initial = snapshot({ bookmarks: [bookmark(1)] });
  const changed = snapshot({ bookmarks: [bookmark(1), bookmark(2)] });
  const options = {
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    coreTopicIds: ["grace"],
    journal,
    lockManager: locks,
    initialStatus: reviewStatus(),
  };
  const first = await ContributionSync.open({
    ...options,
    api: new ContributionApiMock(),
  });
  await first.synchronize(initial);

  const reopenedApi = new ContributionApiMock();
  const reopened = await ContributionSync.open({
    ...options,
    api: reopenedApi,
  });
  assert.deepEqual(await reopened.synchronize(initial), { sent: 0, pending: 0 });
  assert.equal(reopenedApi.batches.length, 0);

  assert.equal(await reopened.captureMutation(initial, changed), 1);
  assert.deepEqual(await reopened.synchronize(changed), { sent: 1, pending: 0 });
  assert.equal(reopenedApi.batches.length, 1);
  assert.equal(reopenedApi.batches[0].length, 1);
  assert.equal(reopenedApi.batches[0][0].type, "verse_add");
  assert.equal(reopenedApi.batches[0][0].verse.verse, 2);
});

test("drops stale personal outbox intent from a closed contribution gap", async () => {
  const cases = [
    {
      name: "addition removed while closed",
      baseline: snapshot(),
      queued: snapshot({ bookmarks: [bookmark(1)] }),
      current: snapshot(),
    },
    {
      name: "removal restored while closed",
      baseline: snapshot({ bookmarks: [bookmark(1)] }),
      queued: snapshot(),
      current: snapshot({ bookmarks: [bookmark(1)] }),
    },
  ];
  for (const scenario of cases) {
    const journal = new MemoryJournal();
    const locks = new SerialLockManager();
    const options = {
      scope: SCOPE,
      instanceScope: INSTANCE_SCOPE,
      coreTopicIds: ["grace"],
      journal,
      lockManager: locks,
      initialStatus: reviewStatus(),
    };
    const first = await ContributionSync.open({
      ...options,
      api: new ContributionApiMock(),
    });
    await first.synchronize(scenario.baseline);
    assert.equal(
      await first.captureMutation(scenario.baseline, scenario.queued),
      1,
      scenario.name,
    );
    assert.equal(first.pendingCount, 1, scenario.name);

    const reopenedApi = new ContributionApiMock();
    const reopened = await ContributionSync.open({
      ...options,
      api: reopenedApi,
    });
    assert.deepEqual(
      await reopened.synchronize(scenario.current),
      { sent: 0, pending: 0 },
      scenario.name,
    );
    assert.equal(reopenedApi.batches.length, 0, scenario.name);
    assert.equal(reopened.pendingCount, 0, scenario.name);
  }
});

test("reopen validation preserves explicit global inverse intent", async () => {
  const journal = new MemoryJournal();
  const locks = new SerialLockManager();
  const api = new ContributionApiMock();
  const current = snapshot();
  const options = {
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    coreTopicIds: ["grace"],
    journal,
    lockManager: locks,
    initialStatus: reviewStatus(),
  };
  const first = await ContributionSync.open({ ...options, api });
  await first.synchronize(current);
  assert.equal(
    await first.captureGlobalRemoval(
      topic(),
      { book: 43, chapter: 3, verse: 16 },
      current,
    ),
    1,
  );

  const reopenedApi = new ContributionApiMock();
  const reopened = await ContributionSync.open({
    ...options,
    api: reopenedApi,
  });
  assert.deepEqual(await reopened.synchronize(current), { sent: 1, pending: 0 });
  assert.equal(reopenedApi.batches[0][0].type, "verse_remove");
});

test("paces every bounded batch in a large baseline", async () => {
  const api = new ContributionApiMock();
  let pauses = 0;
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    batchPause: async () => { pauses += 1; },
  });
  await sync.refreshStatus();
  const large = snapshot({
    topics: [topic(), topic("study-topic", "Study Topic")],
    bookmarks: Array.from({ length: 101 }, (_, index) => bookmark(
      index + 1,
      ["study-topic"],
      { book: 19, chapter: 119 },
    )),
  });
  await sync.synchronize(large);
  assert.deepEqual(api.batches.map((batch) => batch.length), [50, 50, 2]);
  assert.equal(pauses, 3);
});

test("coalesces offline semantic mutations and keeps stable retry ids", async () => {
  const storage = new MemoryStorage();
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage,
    coreTopicIds: ["grace"],
    clock: () => 1234,
    idFactory: () => "fixed-token",
  });
  await sync.refreshStatus();
  await sync.synchronize(snapshot());

  const empty = snapshot();
  const added = snapshot({ bookmarks: [bookmark(16)] });
  assert.equal(sync.captureMutation(empty, added), 1);
  assert.equal(sync.captureMutation(added, empty), 1);
  assert.equal(sync.pendingCount, 1);

  api.failureAt = api.batches.length + 1;
  await assert.rejects(sync.synchronize(empty), /temporary/);
  const failedId = api.batches.at(-1)[0].client_event_id;
  const retryApi = new ContributionApiMock();
  const reopened = new ContributionSync({
    scope: SCOPE,
    api: retryApi,
    storage,
    coreTopicIds: ["grace"],
  });
  await reopened.refreshStatus();
  await reopened.synchronize(empty);
  assert.equal(retryApi.batches[0][0].client_event_id, failedId);
  assert.equal(retryApi.batches[0][0].type, "verse_remove");
  assert.deepEqual(retryApi.batches[0][0].topic, {
    local_topic_id: "grace",
    name: "Grace",
    color: "#bbf7d0",
  });
  assert.equal(reopened.pendingCount, 0);
});

test("coalesces a newly created topic into an explicit inverse delete", async () => {
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
  });
  await sync.refreshStatus();
  await sync.synchronize(snapshot());
  const created = snapshot({ topics: [topic(), topic("new-topic", "New Topic")] });
  assert.equal(sync.captureMutation(snapshot(), created), 1);
  assert.equal(sync.captureMutation(created, snapshot()), 1);
  assert.equal(sync.pendingCount, 1);
  await sync.synchronize(snapshot());
  assert.equal(api.batches.flat().at(-1).type, "topic_delete");
});

test("prunes queued adds represented by the completed baseline but retains removes", async () => {
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
  });
  await sync.refreshStatus();
  const empty = snapshot();
  const added = snapshot({ bookmarks: [bookmark(1)] });
  assert.equal(sync.captureMutation(empty, added), 1);
  await sync.synchronize(added);
  const additions = api.batches.flat().filter((event) =>
    event.type === "verse_add" && event.verse.verse === 1
  );
  assert.equal(additions.length, 1);
  assert.equal(sync.pendingCount, 0);

  const removed = snapshot();
  sync.captureMutation(added, removed);
  await sync.synchronize(removed);
  assert.equal(
    api.batches.flat().some((event) =>
      event.type === "verse_remove" && event.verse.verse === 1
    ),
    true,
  );
});

test("treats reviewed live topics as core until they receive an assignment", () => {
  const live = topic("reviewed-live", "Reviewed Live");
  const unassigned = snapshot({ topics: [topic(), live] });
  assert.deepEqual(
    baselineContributionEvents(unassigned, new Set(["grace", live.id])),
    [],
  );
  const assigned = snapshot({
    topics: [topic(), live],
    bookmarks: [bookmark(1, [live.id])],
  });
  const events = baselineContributionEvents(
    assigned,
    new Set(["grace", live.id]),
  );
  assert.deepEqual(events.map((event) => event.type), ["topic_upsert", "verse_add"]);
});

test("reconciles a bounded journal overflow instead of dropping the mutation", async () => {
  const storage = new MemoryStorage();
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage,
    coreTopicIds: ["grace"],
    maximumOutboxEvents: 1,
    idFactory: () => "bounded",
  });
  await sync.refreshStatus();
  await sync.synchronize(snapshot());
  const changed = snapshot({ bookmarks: [bookmark(1)] });
  const overflowed = snapshot({ bookmarks: [bookmark(1), bookmark(2)] });

  assert.equal(sync.captureMutation(snapshot(), changed), 1);
  assert.equal(sync.pendingCount, 1);
  assert.equal(sync.captureMutation(changed, overflowed), 0);
  assert.equal(sync.overflowed, true);
  await sync.synchronize(overflowed);
  assert.equal(sync.pendingCount, 0);
  assert.equal(sync.overflowed, false);
  assert.equal(sync.recovering, false);
  assert.equal(
    api.batches.flat().some((event) =>
      event.client_event_id.startsWith("recovery:") &&
      event.type === "verse_add" &&
      event.verse.verse === 2
    ),
    true,
  );

  const reopened = new ContributionSync({
    scope: SCOPE,
    api: new ContributionApiMock(),
    storage,
    coreTopicIds: ["grace"],
    maximumOutboxEvents: 1,
  });
  assert.equal(reopened.overflowed, false);
});

test("overflow recovery preserves lost removals and a topic deletion", async () => {
  const storage = new MemoryStorage();
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage,
    coreTopicIds: ["grace"],
    maximumOutboxEvents: 1,
  });
  const study = topic("study-topic", "Study Topic");
  const initial = snapshot({
    topics: [topic(), study],
    bookmarks: [bookmark(1, [study.id]), bookmark(2, [study.id])],
  });
  await sync.refreshStatus();
  await sync.synchronize(initial);
  api.batches.length = 0;

  const beforeOverflow = snapshot({
    topics: [topic(), study],
    bookmarks: [
      bookmark(1, [study.id]),
      bookmark(2, [study.id]),
      bookmark(3, [study.id]),
    ],
  });
  const deleted = snapshot({ topics: [topic()], bookmarks: [] });
  assert.equal(sync.captureMutation(initial, beforeOverflow), 1);
  assert.equal(sync.captureMutation(beforeOverflow, deleted), 0);
  await sync.synchronize(deleted);

  const recovery = api.batches.flat().filter((event) =>
    event.client_event_id.startsWith("recovery:")
  );
  assert.deepEqual(
    recovery.filter((event) => event.type === "verse_remove")
      .map((event) => event.verse.verse)
      .sort((left, right) => left - right),
    [1, 2, 3],
  );
  assert.equal(
    recovery.some((event) =>
      event.type === "topic_delete" &&
      event.topic.local_topic_id === study.id
    ),
    true,
  );
  assert.equal(sync.recovering, false);
});

test("captures a global assignment removal with canonical topic context", async () => {
  const storage = new MemoryStorage();
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage,
    coreTopicIds: ["grace"],
    maximumOutboxEvents: 1,
  });
  await sync.refreshStatus();
  await sync.synchronize(snapshot());
  api.batches.length = 0;
  const coordinate = { book: 43, chapter: 3, verse: 16 };
  assert.equal(sync.captureGlobalRemoval(topic(), coordinate, snapshot()), 1);
  await sync.synchronize(snapshot());
  assert.deepEqual(api.batches.flat()[0], {
    client_event_id: api.batches.flat()[0].client_event_id,
    type: "verse_remove",
    topic: {
      local_topic_id: "grace",
      name: "Grace",
      color: "#bbf7d0",
    },
    verse: coordinate,
  });

  const personal = snapshot({ bookmarks: [bookmark(1)] });
  sync.captureMutation(snapshot(), personal);
  assert.equal(
    sync.captureGlobalRemoval(topic(), { book: 43, chapter: 3, verse: 17 }, personal),
    1,
  );
  assert.equal(sync.recovering, true);
  await sync.synchronize(personal);
  assert.equal(
    api.batches.flat().some((event) =>
      event.client_event_id.startsWith("recovery:") &&
      event.type === "verse_remove" &&
      event.verse.verse === 17
    ),
    true,
  );
  assert.equal(sync.recovering, false);
});

test("a restored global assignment supersedes an unsent removal", async () => {
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
  });
  const current = snapshot();
  const coordinate = { book: 43, chapter: 3, verse: 16 };
  await sync.refreshStatus();
  await sync.synchronize(current);
  api.batches.length = 0;

  assert.equal(sync.captureGlobalRemoval(topic(), coordinate, current), 1);
  assert.equal(sync.captureGlobalAddition(topic(), coordinate, current), 1);
  assert.equal(sync.pendingCount, 1);
  await sync.synchronize(current);

  assert.deepEqual(
    api.batches.flat().filter((event) =>
      event.verse?.book === 43 &&
      event.verse?.chapter === 3 &&
      event.verse?.verse === 16
    ).map((event) => event.type),
    ["verse_add"],
  );
});

test("a restore captured during an in-flight global removal converges to add", async () => {
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
  });
  const current = snapshot();
  const coordinate = { book: 43, chapter: 3, verse: 16 };
  await sync.refreshStatus();
  await sync.synchronize(current);
  api.batches.length = 0;
  sync.captureGlobalRemoval(topic(), coordinate, current);

  let release;
  let started;
  const startedPromise = new Promise((resolve) => { started = resolve; });
  const releasePromise = new Promise((resolve) => { release = resolve; });
  let blocked = false;
  api.submitContributionEvents = async function (events) {
    this.batches.push(structuredClone(events));
    if (!blocked && events[0]?.type === "verse_remove") {
      blocked = true;
      started();
      await releasePromise;
    }
    return {
      accepted: events.length,
      replayed: 0,
      event_ids: Object.fromEntries(
        events.map((event, index) => [event.client_event_id, index + 1]),
      ),
    };
  };

  const draining = sync.synchronize(current);
  await startedPromise;
  assert.equal(sync.captureGlobalAddition(topic(), coordinate, current), 1);
  release();
  await draining;

  assert.deepEqual(
    api.batches.flat().filter((event) =>
      event.verse?.book === 43 &&
      event.verse?.chapter === 3 &&
      event.verse?.verse === 16
    ).map((event) => event.type),
    ["verse_remove", "verse_add"],
  );
  assert.equal(sync.pendingCount, 0);
});

test("bulk global restoration keeps the latest intent during compact recovery", async () => {
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    maximumOutboxEvents: 1,
  });
  const current = snapshot({ bookmarks: [bookmark(2)] });
  const coordinate = { book: 43, chapter: 3, verse: 16 };
  await sync.refreshStatus();
  await sync.synchronize(snapshot());
  api.batches.length = 0;

  sync.captureMutation(snapshot(), current);
  assert.equal(sync.captureGlobalRemoval(topic(), coordinate, current), 1);
  assert.equal(sync.recovering, true);
  assert.equal(
    sync.captureGlobalAdditions([
      { topic: topic(), verse: coordinate },
      { topic: topic(), verse: coordinate },
    ], current),
    1,
  );
  await sync.synchronize(current);

  const recovery = api.batches.flat().filter((event) =>
    event.client_event_id.startsWith("recovery:") &&
    event.verse?.book === 43 &&
    event.verse?.chapter === 3 &&
    event.verse?.verse === 16
  );
  assert.deepEqual(recovery.map((event) => event.type), ["verse_add"]);
  assert.equal(sync.recovering, false);
});

test("deduplicates an overlapping personal and global recovery removal", async () => {
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage: new MemoryStorage(),
    coreTopicIds: ["grace"],
    maximumOutboxEvents: 1,
  });
  const initial = snapshot({ bookmarks: [bookmark(1)] });
  const beforeOverflow = snapshot({ bookmarks: [bookmark(1), bookmark(2)] });
  const empty = snapshot();
  await sync.refreshStatus();
  await sync.synchronize(initial);
  api.batches.length = 0;
  sync.captureMutation(initial, beforeOverflow);
  sync.captureMutation(beforeOverflow, empty);
  sync.captureGlobalRemoval(topic(), { book: 1, chapter: 1, verse: 1 }, empty);
  await sync.synchronize(empty);

  const overlapping = api.batches.flat().filter((event) =>
    event.client_event_id.startsWith("recovery:") &&
    event.type === "verse_remove" &&
    event.topic.local_topic_id === "grace" &&
    event.verse.book === 1 &&
    event.verse.chapter === 1 &&
    event.verse.verse === 1
  );
  assert.equal(overlapping.length, 1);
  assert.equal(
    new Set(api.batches.flat().map((event) => event.client_event_id)).size,
    api.batches.flat().length,
  );
});

test("a mutation during recovery is sent in a following compact pass", async () => {
  const storage = new MemoryStorage();
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage,
    coreTopicIds: ["grace"],
    maximumOutboxEvents: 1,
  });
  const initial = snapshot({
    bookmarks: Array.from({ length: 60 }, (_, index) => bookmark(index + 1)),
  });
  const beforeOverflow = snapshot({
    bookmarks: [...initial.bookmarks, bookmark(61)],
  });
  const empty = snapshot();
  const latest = snapshot({ bookmarks: [bookmark(62)] });
  await sync.refreshStatus();
  await sync.synchronize(initial);
  api.batches.length = 0;
  sync.captureMutation(initial, beforeOverflow);
  sync.captureMutation(beforeOverflow, empty);

  let captured = false;
  api.onBatch = (events) => {
    if (!captured && events[0]?.client_event_id.startsWith("recovery:")) {
      captured = true;
      sync.captureMutation(empty, latest);
    }
  };
  await sync.synchronize(empty);

  assert.equal(captured, true);
  assert.equal(
    api.batches.flat().some((event) =>
      event.client_event_id.startsWith("recovery:") &&
      event.type === "verse_add" &&
      event.verse.verse === 62
    ),
    true,
  );
  assert.equal(sync.recovering, false);
});

test("overflow recovery resumes after a crash at the last successful batch", async () => {
  const storage = new MemoryStorage();
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage,
    coreTopicIds: ["grace"],
    maximumOutboxEvents: 1,
  });
  const initial = snapshot({
    bookmarks: Array.from({ length: 60 }, (_, index) => bookmark(index + 1)),
  });
  const beforeOverflow = snapshot({
    bookmarks: [...initial.bookmarks, bookmark(61)],
  });
  const empty = snapshot();
  await sync.refreshStatus();
  await sync.synchronize(initial);
  api.batches.length = 0;
  sync.captureMutation(initial, beforeOverflow);
  sync.captureMutation(beforeOverflow, empty);
  api.failureAt = 3;
  await assert.rejects(sync.synchronize(empty), /temporary/);
  const failedBatch = api.batches[2];
  assert.equal(failedBatch.length, 11);

  const retryApi = new ContributionApiMock();
  const reopened = new ContributionSync({
    scope: SCOPE,
    api: retryApi,
    storage,
    coreTopicIds: ["grace"],
    maximumOutboxEvents: 1,
  });
  await reopened.refreshStatus();
  await reopened.synchronize(empty);
  assert.equal(retryApi.batches.length, 1);
  assert.equal(retryApi.batches[0].length, 11);
  assert.equal(
    retryApi.batches[0][0].client_event_id,
    failedBatch[0].client_event_id,
  );
  assert.equal(reopened.recovering, false);
});

test("requires English topic names and omits coordinates outside the core canon", () => {
  assert.equal(isEnglishContributionTopicName("Faith & Patience"), true);
  assert.equal(isEnglishContributionTopicName("Надежда"), false);
  assert.equal(isEnglishContributionTopicName("Oración"), false);
  assert.equal(isEnglishContributionTopicName("A"), false);
  assert.equal(isEnglishContributionTopicName("12"), false);
  assert.equal(isEnglishContributionTopicName("Grace  Hope"), false);
  const before = snapshot();
  const after = snapshot({
    bookmarks: [
      bookmark(1, ["grace"], { book: 67 }),
      bookmark(1, ["grace"], { book: 1, chapter: 51 }),
    ],
  });
  assert.deepEqual(contributionEventsForDiff(before, after), []);
});

test("does not claim a durable capture after quota failure and retries in memory", async () => {
  const storage = new QuotaStorage();
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage,
    coreTopicIds: ["grace"],
    idFactory: () => "quota-retry",
  });
  await sync.refreshStatus();
  await sync.synchronize(snapshot());

  storage.rejectWrites = true;
  const changed = snapshot({ bookmarks: [bookmark(16)] });
  assert.equal(sync.captureMutation(snapshot(), changed), 0);
  assert.equal(sync.pendingCount, 1);
  assert.equal(sync.persistenceFailed, true);

  const reopened = new ContributionSync({
    scope: SCOPE,
    api: new ContributionApiMock(),
    storage,
    coreTopicIds: ["grace"],
  });
  await reopened.refreshStatus();
  assert.equal(reopened.pendingCount, 0);
  assert.equal(reopened.persistenceFailed, true);

  storage.rejectWrites = false;
  await sync.synchronize(changed);
  assert.equal(sync.pendingCount, 0);
  assert.equal(sync.persistenceFailed, false);
  const verseEvent = api.batches.flat().find((event) =>
    event.type === "verse_add"
  );
  assert.deepEqual(verseEvent.topic, {
    local_topic_id: "grace",
    name: "Grace",
    color: "#bbf7d0",
  });
});

test("keeps a near-maximum persistent journal below two MiB", async () => {
  const storage = new MemoryStorage();
  const api = new ContributionApiMock();
  const sync = new ContributionSync({
    scope: SCOPE,
    api,
    storage,
    coreTopicIds: ["grace"],
    clock: () => 1,
    idFactory: () => "x".repeat(64),
  });
  await sync.refreshStatus();
  await sync.synchronize(snapshot());
  const customTopics = Array.from({ length: 3 }, (_, index) => topic(
    `t${index}${"x".repeat(126)}`,
    `${String.fromCharCode(65 + index)}${"z".repeat(79)}`,
  ));
  const bookmarks = Array.from({ length: 85 }, (_, index) =>
    bookmark(
      index + 1,
      index === 84
        ? customTopics.slice(0, 1).map((item) => item.id)
        : customTopics.map((item) => item.id),
    )
  );
  const changed = snapshot({
    topics: [topic(), ...customTopics],
    bookmarks,
  });

  assert.equal(
    sync.captureMutation(snapshot(), changed),
    CONTRIBUTION_OUTBOX_MAXIMUM,
  );
  assert.equal(sync.pendingCount, CONTRIBUTION_OUTBOX_MAXIMUM);
  assert.equal(sync.overflowed, false);
  const overLimit = snapshot({
    topics: [topic(), ...customTopics],
    bookmarks: [...bookmarks, bookmark(86, [customTopics[0].id])],
  });
  assert.equal(sync.captureMutation(changed, overLimit), 0);
  assert.equal(sync.overflowed, true);
  const [raw] = storage.values.values();
  assert.ok(new TextEncoder().encode(raw).byteLength < 2 * 1024 * 1024);
});

test("migrates a valid v1 localStorage journal only after IndexedDB commits", async () => {
  const legacy = new MemoryStorage();
  const api = new ContributionApiMock();
  const old = new ContributionSync({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    api,
    storage: legacy,
    coreTopicIds: ["grace"],
  });
  await old.refreshStatus();
  await old.synchronize(snapshot());
  old.captureMutation(snapshot(), snapshot({ bookmarks: [bookmark(7)] }));
  const [currentKey, raw] = [...legacy.values.entries()][0];
  const legacyKey = `${CONTRIBUTION_STORAGE_PREFIX}:${SCOPE}`;
  legacy.values.delete(currentKey);
  legacy.values.set(legacyKey, raw);

  const journal = new MemoryJournal();
  const migrated = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    api: new ContributionApiMock(),
    coreTopicIds: ["grace"],
    journal,
    lockManager: new SerialLockManager(),
    legacyStorage: legacy,
  });
  assert.equal(migrated.pendingCount, 1);
  assert.equal(legacy.getItem(legacyKey), null);
  assert.equal(typeof journal.raw, "string");
});

test("does not hold the cross-tab lock across a slow network request", async () => {
  const journal = new MemoryJournal();
  const locks = new SerialLockManager();
  const setupApi = new ContributionApiMock();
  const first = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    api: setupApi,
    coreTopicIds: ["grace"],
    journal,
    lockManager: locks,
  });
  await first.refreshStatus();
  await first.synchronize(snapshot());
  const second = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    api: setupApi,
    coreTopicIds: ["grace"],
    journal,
    lockManager: locks,
  });
  const one = snapshot({ bookmarks: [bookmark(1)] });
  const two = snapshot({ bookmarks: [bookmark(1), bookmark(2)] });
  await first.captureMutation(snapshot(), one);

  let release;
  let started;
  const startedPromise = new Promise((resolve) => { started = resolve; });
  const releasePromise = new Promise((resolve) => { release = resolve; });
  setupApi.onBatch = async (events) => {
    if (events.some((event) => event.type === "verse_add" && event.verse.verse === 1)) {
      started();
      await releasePromise;
    }
  };
  // Ensure the mock actually awaits the blocking callback for this test.
  setupApi.submitContributionEvents = async function (events) {
    this.batches.push(structuredClone(events));
    await this.onBatch?.(events);
    return {
      accepted: events.length,
      replayed: 0,
      event_ids: Object.fromEntries(
        events.map((event, index) => [event.client_event_id, index + 1]),
      ),
    };
  };
  const draining = first.synchronize(one);
  await startedPromise;
  const captured = await Promise.race([
    second.captureMutation(one, two),
    new Promise((_, reject) => setTimeout(
      () => reject(new Error("capture waited behind the network request")),
      100,
    )),
  ]);
  assert.equal(captured, 1);
  assert.equal(JSON.parse(journal.raw).outbox.length, 2);
  release();
  await draining;
});

test("serializes divergent first baselines across tabs without checkpoint livelock", async () => {
  const journal = new MemoryJournal();
  const locks = new SerialLockManager();
  const api = new ContributionApiMock();
  const options = {
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    api,
    coreTopicIds: ["grace"],
    journal,
    lockManager: locks,
  };
  const first = await ContributionSync.open(options);
  const second = await ContributionSync.open(options);
  await first.refreshStatus();
  await second.refreshStatus();

  const results = await Promise.all([
    first.synchronize(snapshot({ bookmarks: [bookmark(1)] })),
    second.synchronize(snapshot({ bookmarks: [bookmark(2)] })),
  ]);

  assert.deepEqual(results, [
    { sent: 2, pending: 0 },
    { sent: 0, pending: 0 },
  ]);
  assert.equal(api.batches.length, 1);
  assert.deepEqual(
    api.batches[0].map((event) => event.type),
    ["topic_upsert", "verse_add"],
  );
  assert.equal(api.batches[0][1].verse.verse, 1);
});

test("a blocked add followed by remove converges after the add succeeds", async () => {
  const journal = new MemoryJournal();
  const locks = new SerialLockManager();
  const api = new ContributionApiMock();
  const sync = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    api,
    coreTopicIds: ["grace"],
    journal,
    lockManager: locks,
  });
  await sync.refreshStatus();
  await sync.synchronize(snapshot());
  const added = snapshot({ bookmarks: [bookmark(9)] });
  await sync.captureMutation(snapshot(), added);

  let release;
  let started;
  const startedPromise = new Promise((resolve) => { started = resolve; });
  const releasePromise = new Promise((resolve) => { release = resolve; });
  let blocked = false;
  api.submitContributionEvents = async function (events) {
    this.batches.push(structuredClone(events));
    if (!blocked && events[0]?.type === "verse_add") {
      blocked = true;
      started();
      await releasePromise;
    }
    return {
      accepted: events.length,
      replayed: 0,
      event_ids: Object.fromEntries(
        events.map((event, index) => [event.client_event_id, index + 1]),
      ),
    };
  };
  const draining = sync.synchronize(added);
  await startedPromise;
  assert.equal(await sync.captureMutation(added, snapshot()), 1);
  release();
  await draining;
  assert.deepEqual(
    api.batches.flat().filter((event) => event.verse?.verse === 9)
      .map((event) => event.type),
    ["verse_add", "verse_remove"],
  );
  assert.equal(sync.pendingCount, 0);
});

test("a blocked topic create followed by delete converges explicitly", async () => {
  const journal = new MemoryJournal();
  const locks = new SerialLockManager();
  const api = new ContributionApiMock();
  const sync = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    api,
    coreTopicIds: ["grace"],
    journal,
    lockManager: locks,
  });
  await sync.refreshStatus();
  await sync.synchronize(snapshot());
  const created = snapshot({ topics: [topic(), topic("quick-note", "Quick Note")] });
  await sync.captureMutation(snapshot(), created);

  let release;
  let started;
  const startedPromise = new Promise((resolve) => { started = resolve; });
  const releasePromise = new Promise((resolve) => { release = resolve; });
  let blocked = false;
  api.submitContributionEvents = async function (events) {
    this.batches.push(structuredClone(events));
    if (!blocked && events[0]?.type === "topic_upsert") {
      blocked = true;
      started();
      await releasePromise;
    }
    return {
      accepted: events.length,
      replayed: 0,
      event_ids: Object.fromEntries(
        events.map((event, index) => [event.client_event_id, index + 1]),
      ),
    };
  };
  const draining = sync.synchronize(created);
  await startedPromise;
  assert.equal(await sync.captureMutation(created, snapshot()), 1);
  release();
  await draining;
  assert.deepEqual(
    api.batches.flat().filter((event) =>
      event.topic.local_topic_id === "quick-note"
    ).map((event) => event.type),
    ["topic_upsert", "topic_delete"],
  );
});

test("retains an IDB-rejected mutation in memory without claiming durability", async () => {
  const journal = new MemoryJournal();
  const locks = new SerialLockManager();
  const api = new ContributionApiMock();
  const sync = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    api,
    coreTopicIds: ["grace"],
    journal,
    lockManager: locks,
  });
  await sync.refreshStatus();
  await sync.synchronize(snapshot());
  journal.rejectWrites = true;
  const changed = snapshot({ bookmarks: [bookmark(8)] });
  assert.equal(await sync.captureMutation(snapshot(), changed), 0);
  assert.equal(sync.persistenceFailed, true);
  assert.equal(sync.pendingCount, 1);

  const reopened = await ContributionSync.open({
    scope: SCOPE,
    instanceScope: INSTANCE_SCOPE,
    api: new ContributionApiMock(),
    coreTopicIds: ["grace"],
    journal,
    lockManager: locks,
    legacyStorage: new MemoryStorage(),
  });
  assert.equal(reopened.pendingCount, 0);
  assert.equal(reopened.recovering, false);

  journal.rejectWrites = false;
  await sync.synchronize(changed);
  assert.equal(sync.pendingCount, 0);
  assert.equal(sync.persistenceFailed, false);
  assert.equal(
    api.batches.flat().some((event) =>
      event.type === "verse_add" && event.verse.verse === 8
    ),
    true,
  );
});

test("namespaces contribution journals by server instance", async () => {
  const storage = new MemoryStorage();
  const first = new ContributionSync({
    scope: SCOPE,
    instanceScope: "5".repeat(16),
    api: new ContributionApiMock(),
    storage,
    coreTopicIds: ["grace"],
  });
  const second = new ContributionSync({
    scope: SCOPE,
    instanceScope: "6".repeat(16),
    api: new ContributionApiMock(),
    storage,
    coreTopicIds: ["grace"],
  });
  await first.refreshStatus();
  await second.refreshStatus();
  first.captureMutation(snapshot(), snapshot({ bookmarks: [bookmark(1)] }));
  second.captureMutation(snapshot(), snapshot({ bookmarks: [bookmark(2)] }));
  assert.equal(storage.values.size, 2);
  assert.equal(
    [...storage.values.keys()].every((key) =>
      key.includes(`:${SCOPE}`) &&
      (key.includes(`:${"5".repeat(16)}:`) || key.includes(`:${"6".repeat(16)}:`))
    ),
    true,
  );
});
