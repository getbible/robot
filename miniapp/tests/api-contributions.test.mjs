import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, MiniAppApi } from "../lib/api.js";

const SESSION_TOKEN = "abcdefghijklmnop";
const CONTRIBUTION_TOKEN = `gbc_${"A".repeat(43)}`;
const STATUS = {
  enabled: true,
  state: "approved",
  can_contribute: true,
  disclosure_required: false,
};
// Approved contributors receive their second token only inside ordinary
// JSON payloads — the session bootstrap and the detailed status.
const SESSION = {
  session_token: SESSION_TOKEN,
  limits: { search_timeout_seconds: 30 },
  basket: { maximum: 100, items: [] },
  contributions: { ...STATUS, contribution_token: CONTRIBUTION_TOKEN },
};
const EVENTS = [
  {
    client_event_id: "baseline:topic_upsert:0011223344556677",
    type: "topic_upsert",
    topic: { local_topic_id: "grace", name: "Grace", color: "#bbf7d0" },
  },
  {
    client_event_id: "baseline:verse_add:8899aabbccddeeff",
    type: "verse_add",
    topic: { local_topic_id: "grace", name: "Grace", color: "#bbf7d0" },
    verse: { book: 43, chapter: 3, verse: 16 },
  },
];
const EVENTS_RESPONSE = {
  accepted: 2,
  replayed: 0,
  event_ids: {
    "baseline:topic_upsert:0011223344556677": 1,
    "baseline:verse_add:8899aabbccddeeff": 2,
  },
  status: STATUS,
  catalog: {
    revision: 4,
    checksum: "b".repeat(64),
  },
};

function json(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function testApi(fetchImplementation) {
  return new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: {
      async translations() { return []; },
      async chapter() { return {}; },
    },
    fetchImplementation,
  });
}

test("submits contribution batches with the plain session bearer like search", async () => {
  const requests = [];
  const api = testApi(async (url, options) => {
    const path = new URL(url).pathname;
    requests.push({ path, options });
    if (path.endsWith("/session")) return json(SESSION, 201);
    if (path.endsWith("/cleanup")) {
      return new Response(null, { status: 204 });
    }
    if (path.endsWith("/contributions/status")) return json(STATUS);
    if (path.endsWith("/contributions/events")) return json(EVENTS_RESPONSE);
    if (path.endsWith("/bookmarks/catalog")) {
      return json({
        revision: 4,
        checksum: "b".repeat(64),
        catalog: {
          schema_version: 1,
          topics: [],
          associations: { add: [], remove: [] },
        },
      }, 200, { ETag: '"catalog-4"' });
    }
    return json({ error: { code: "not_found" } }, 404);
  });

  await api.createSession("OwnerBoundLaunch1");
  assert.equal(api.contributionTransportReady, true);
  assert.deepEqual(await api.contributionStatus(), STATUS);
  assert.deepEqual(
    await api.submitContributionEvents(EVENTS, { disclosureAcknowledged: true }),
    EVENTS_RESPONSE,
  );
  assert.deepEqual(await api.submitContributionEvents(EVENTS), EVENTS_RESPONSE);
  await api.bookmarkCatalog();

  const sessionRequest = requests.find(({ path }) => path.endsWith("/session"));
  assert.deepEqual(JSON.parse(sessionRequest.options.body), {
    init_data: "signed-init-data",
    launch_token: "OwnerBoundLaunch1",
  });
  assert.equal(sessionRequest.options.headers.Authorization, undefined);

  const authenticated = requests.filter(({ path }) =>
    /\/(?:contributions|bookmarks\/catalog)/u.test(path)
  );
  assert.equal(authenticated.length, 4);
  assert.ok(authenticated.every(({ options }) =>
    options.headers["X-Telegram-Init-Data"] === undefined
  ));
  assert.ok(authenticated.every(({ options }) =>
    options.headers.Authorization === `Bearer ${SESSION_TOKEN}`
  ));

  const batchRequests = requests.filter(({ path }) =>
    path.endsWith("/contributions/events")
  );
  assert.equal(batchRequests.length, 2);
  assert.equal(batchRequests[0].options.method, "POST");
  // The first batch carries the inline disclosure consent; later batches
  // stay minimal so an already-acknowledged consent is never re-stated. The
  // contributor token rides in every batch body, never in a header.
  assert.deepEqual(JSON.parse(batchRequests[0].options.body), {
    events: EVENTS,
    contribution_token: CONTRIBUTION_TOKEN,
    disclosure_acknowledged: true,
  });
  assert.deepEqual(JSON.parse(batchRequests[1].options.body), {
    events: EVENTS,
    contribution_token: CONTRIBUTION_TOKEN,
  });
  assert.ok(batchRequests.every(({ options }) =>
    options.headers["X-Contribution-Token"] === undefined
  ));
});

test("withholds contribution batches until a contributor token arrives", async () => {
  const requests = [];
  const api = testApi(async (url) => {
    const path = new URL(url).pathname;
    requests.push(path);
    if (path.endsWith("/session")) {
      return json({ ...SESSION, contributions: STATUS }, 201);
    }
    if (path.endsWith("/cleanup")) return new Response(null, { status: 204 });
    if (path.endsWith("/contributions/status")) {
      return json({ ...STATUS, contribution_token: CONTRIBUTION_TOKEN });
    }
    return json({ error: "not_found" }, 404);
  });

  await api.createSession("OwnerBoundLaunch1");
  assert.equal(api.contributionTransportReady, false);
  await assert.rejects(
    Promise.resolve().then(() => api.submitContributionEvents(EVENTS)),
    (error) => {
      assert.equal(error instanceof ApiError, true);
      assert.equal(error.code, "contribution_transport_not_ready");
      assert.equal(error.status, 403);
      return true;
    },
  );
  assert.equal(
    requests.filter((path) => path.endsWith("/contributions/events")).length,
    0,
  );

  // One ordinary status request recovers the token.
  await api.contributionStatus();
  assert.equal(api.contributionTransportReady, true);
  api.clearSession();
  assert.equal(api.contributionTransportReady, false);
});

test("bounds contribution batches locally before any transport", async () => {
  const requests = [];
  const api = testApi(async (url) => {
    const path = new URL(url).pathname;
    requests.push(path);
    if (path.endsWith("/session")) return json(SESSION, 201);
    if (path.endsWith("/cleanup")) return new Response(null, { status: 204 });
    return json({ error: "not_found" }, 404);
  });

  await api.createSession("OwnerBoundLaunch1");
  assert.throws(() => api.submitContributionEvents([]), TypeError);
  assert.throws(() => api.submitContributionEvents(null), TypeError);
  assert.throws(
    () => api.submitContributionEvents(Array.from({ length: 51 }, () => EVENTS[0])),
    RangeError,
  );
  assert.throws(
    () => api.submitContributionEvents(EVENTS, { disclosureAcknowledged: "yes" }),
    TypeError,
  );
  assert.equal(
    requests.filter((path) => path.endsWith("/contributions/events")).length,
    0,
  );
});

test("requires a live session and token before a contribution batch is sent", async () => {
  const requests = [];
  const api = testApi(async (url) => {
    const path = new URL(url).pathname;
    requests.push(path);
    return json({ error: "not_found" }, 404);
  });

  await assert.rejects(
    Promise.resolve().then(() => api.submitContributionEvents(EVENTS)),
    (error) => {
      assert.equal(error instanceof ApiError, true);
      assert.equal(error.code, "contribution_transport_not_ready");
      return true;
    },
  );
  assert.equal(requests.length, 0);
});

test("surfaces contribution batch failures with their declared error codes", async () => {
  let batchCalls = 0;
  const api = testApi(async (url) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/session")) return json(SESSION, 201);
    if (path.endsWith("/cleanup")) return new Response(null, { status: 204 });
    if (path.endsWith("/contributions/events")) {
      batchCalls += 1;
      if (batchCalls === 1) {
        return json({
          error: "contribution_not_allowed",
          message: "This Telegram user is not an approved contributor.",
        }, 403);
      }
      return json({
        error: "idempotency_conflict",
        message: "A contribution event ID was reused with different data.",
      }, 409);
    }
    return json({ error: "not_found" }, 404);
  });

  await api.createSession("OwnerBoundLaunch1");
  await assert.rejects(api.submitContributionEvents(EVENTS), (error) => {
    assert.equal(error instanceof ApiError, true);
    assert.equal(error.code, "contribution_not_allowed");
    assert.equal(error.status, 403);
    return true;
  });
  await assert.rejects(api.submitContributionEvents(EVENTS), (error) => {
    assert.equal(error.code, "idempotency_conflict");
    assert.equal(error.status, 409);
    return true;
  });
});

test("preserves bounded Retry-After guidance from error bodies and headers", async () => {
  let statusCalls = 0;
  const api = testApi(async (url) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/session")) return json(SESSION, 201);
    if (path.endsWith("/cleanup")) return new Response(null, { status: 204 });
    if (path.endsWith("/contributions/status")) {
      statusCalls += 1;
      return statusCalls === 1
        ? json({
          error: "rate_limited",
          message: "Please wait.",
          retryable: true,
          retry_after: 7,
        }, 429, { "Retry-After": "20" })
        : json({ error: "rate_limited", message: "Please wait." }, 429, {
          "Retry-After": "11",
        });
    }
    return json({ error: "not_found" }, 404);
  });

  await api.createSession("OwnerBoundLaunch1");
  await assert.rejects(api.contributionStatus(), (error) => {
    assert.equal(error instanceof ApiError, true);
    assert.equal(error.retryable, true);
    assert.equal(error.retryAfter, 7);
    return true;
  });
  await assert.rejects(api.contributionStatus(), (error) => {
    assert.equal(error.retryAfter, 11);
    return true;
  });
});
