import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, MiniAppApi } from "../lib/api.js";

const SESSION_TOKEN = "abcdefghijklmnop";
const CONTRIBUTION_TOKEN = `gbc_${"A".repeat(43)}`;
const SESSION = {
  session_token: SESSION_TOKEN,
  limits: { search_timeout_seconds: 30 },
  basket: { maximum: 100, items: [] },
};
const STATUS = {
  enabled: true,
  state: "approved",
  can_contribute: true,
  disclosure_required: false,
};
const ENVELOPE = {
  protocol_version: 1,
  sync_id: "sync_0123456789abcdef",
  client_id: "client_0123456789abcdef",
  snapshot: {
    topics: [{
      local_topic_id: "grace",
      name: "Grace",
      color: "#bbf7d0",
    }],
    assignments: [{
      local_topic_id: "grace",
      book: 43,
      chapter: 3,
      verse: 16,
    }],
  },
  operations: [],
  disclosure_acknowledged: false,
};
const SYNC_RESPONSE = {
  protocol_version: 1,
  receipt: {
    sync_id: ENVELOPE.sync_id,
    snapshot_digest: "a".repeat(64),
    outcome: "accepted",
    accepted: 2,
    replayed: 0,
    event_ids: {
      "snapshot:topic:grace": 1,
      "snapshot:verse:grace:43:3:16": 2,
    },
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

test("uses one capability-authenticated request for a contribution sync", async () => {
  const requests = [];
  const api = testApi(async (url, options) => {
    const path = new URL(url).pathname;
    requests.push({ path, options });
    if (path.endsWith("/session")) {
      return json(SESSION, 201, {
        "X-Contribution-Token": CONTRIBUTION_TOKEN,
      });
    }
    if (path.endsWith("/cleanup")) {
      return new Response(null, { status: 204 });
    }
    if (path.endsWith("/contributions/status")) return json(STATUS);
    if (path.endsWith("/contributions/sync")) return json(SYNC_RESPONSE);
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
  assert.deepEqual(await api.syncContributions(ENVELOPE), SYNC_RESPONSE);
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
  assert.equal(authenticated.length, 3);
  assert.ok(authenticated.every(({ options }) =>
    options.headers["X-Telegram-Init-Data"] === undefined
  ));
  const syncRequests = authenticated.filter(({ path }) =>
    path.endsWith("/contributions/sync")
  );
  assert.equal(syncRequests.length, 1);
  assert.equal(
    syncRequests[0].options.headers.Authorization,
    `Bearer ${CONTRIBUTION_TOKEN}`,
  );
  assert.deepEqual(JSON.parse(syncRequests[0].options.body), ENVELOPE);

  for (const { path, options } of authenticated) {
    if (!path.endsWith("/contributions/sync")) {
      assert.equal(options.headers.Authorization, `Bearer ${SESSION_TOKEN}`);
    }
  }
});

test("will not send a contribution snapshot without a server capability", async () => {
  const requests = [];
  const api = testApi(async (url) => {
    const path = new URL(url).pathname;
    requests.push(path);
    if (path.endsWith("/session")) return json(SESSION, 201);
    if (path.endsWith("/cleanup")) return new Response(null, { status: 204 });
    return json({ error: "not_found" }, 404);
  });

  await api.createSession("OwnerBoundLaunch1");
  assert.equal(api.contributionTransportReady, false);
  assert.throws(
    () => api.syncContributions(ENVELOPE),
    (error) => {
      assert.equal(error instanceof ApiError, true);
      assert.equal(error.code, "contribution_transport_not_ready");
      assert.equal(error.status, 403);
      assert.equal(error.retryable, false);
      return true;
    },
  );
  assert.equal(
    requests.filter((path) => path.endsWith("/contributions/sync")).length,
    0,
  );
  assert.throws(() => api.syncContributions(null), /sync envelope/i);
});

test("accepts a capability issued when an application becomes approved", async () => {
  let statusCalls = 0;
  const api = testApi(async (url) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/session")) return json(SESSION, 201);
    if (path.endsWith("/cleanup")) return new Response(null, { status: 204 });
    if (path.endsWith("/contributions/status")) {
      statusCalls += 1;
      return json(STATUS, 200, {
        "X-Contribution-Token": CONTRIBUTION_TOKEN,
      });
    }
    if (path.endsWith("/contributions/sync")) return json(SYNC_RESPONSE);
    return json({ error: "not_found" }, 404);
  });

  await api.createSession("OwnerBoundLaunch1");
  assert.equal(api.contributionTransportReady, false);
  await api.contributionStatus();
  assert.equal(statusCalls, 1);
  assert.equal(api.contributionTransportReady, true);
  await api.syncContributions(ENVELOPE);
  api.clearSession();
  assert.equal(api.contributionTransportReady, false);
});

test("rejects malformed contribution capabilities", async () => {
  const api = testApi(async (url) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/session")) {
      return json(SESSION, 201, {
        "X-Contribution-Token": "gbc_not-a-32-byte-token",
      });
    }
    return json({ error: "not_found" }, 404);
  });

  await assert.rejects(api.createSession("OwnerBoundLaunch1"), (error) => {
    assert.equal(error instanceof ApiError, true);
    assert.equal(error.code, "invalid_response");
    assert.equal(error.retryable, true);
    return true;
  });
  assert.equal(api.contributionTransportReady, false);
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
