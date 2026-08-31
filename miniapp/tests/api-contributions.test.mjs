import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, MiniAppApi } from "../lib/api.js";

const SESSION_TOKEN = "abcdefghijklmnop";
const LEGACY_CONTRIBUTION_TOKEN = `gbc_${"A".repeat(43)}`;
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
const SYNC_ID = "sync:push.0123456789abcdef";
const RECEIPT_RESPONSE = {
  found: true,
  receipt: {
    sync_id: SYNC_ID,
    accepted: 2,
    replayed: 0,
    snapshot_digest: "a".repeat(64),
    event_ids: {
      "snapshot:topic:grace": 1,
      "snapshot:verse:grace:43:3:16": 2,
    },
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

test("reads contribution status over GET with the session bearer only", async () => {
  const requests = [];
  const api = testApi(async (url, options) => {
    const parsed = new URL(url);
    requests.push({ path: parsed.pathname, search: parsed.search, options });
    if (parsed.pathname.endsWith("/session")) return json(SESSION, 201);
    if (parsed.pathname.endsWith("/cleanup")) {
      return new Response(null, { status: 204 });
    }
    if (parsed.pathname.endsWith("/contributions/status")) return json(STATUS);
    return json({ error: { code: "not_found" } }, 404);
  });

  await api.createSession("OwnerBoundLaunch1");
  assert.deepEqual(await api.contributionStatus(), STATUS);

  const statusRequests = requests.filter(({ path }) =>
    path.endsWith("/contributions/status")
  );
  assert.equal(statusRequests.length, 1);
  const [{ search, options }] = statusRequests;
  assert.equal(search, "?details=1");
  assert.equal(options.method, "GET");
  assert.equal(options.body, undefined);
  assert.equal(options.headers.Authorization, `Bearer ${SESSION_TOKEN}`);
  assert.equal(options.headers["X-Telegram-Init-Data"], undefined);
});

test("fetches a push receipt by encoded sync identity with the session bearer", async () => {
  const requests = [];
  const api = testApi(async (url, options) => {
    const parsed = new URL(url);
    requests.push({ url: parsed, options });
    if (parsed.pathname.endsWith("/session")) return json(SESSION, 201);
    if (parsed.pathname.endsWith("/cleanup")) {
      return new Response(null, { status: 204 });
    }
    if (parsed.pathname.endsWith("/contributions/receipt")) {
      return json(RECEIPT_RESPONSE);
    }
    return json({ error: { code: "not_found" } }, 404);
  });

  await api.createSession("OwnerBoundLaunch1");
  assert.deepEqual(await api.contributionReceipt(SYNC_ID), RECEIPT_RESPONSE);

  const receiptRequests = requests.filter(({ url }) =>
    url.pathname.endsWith("/contributions/receipt")
  );
  assert.equal(receiptRequests.length, 1);
  const [{ url, options }] = receiptRequests;
  assert.equal(options.method, "GET");
  assert.equal(options.body, undefined);
  assert.equal(options.headers.Authorization, `Bearer ${SESSION_TOKEN}`);
  assert.equal(options.headers["X-Telegram-Init-Data"], undefined);
  // The colon must ride percent-encoded on the wire yet round-trip intact.
  assert.equal(url.search, "?sync_id=sync%3Apush.0123456789abcdef");
  assert.equal(url.searchParams.get("sync_id"), SYNC_ID);
});

test("rejects invalid sync identities before any network request", async () => {
  let fetchCalls = 0;
  const api = testApi(async () => {
    fetchCalls += 1;
    return json({ error: { code: "not_found" } }, 404);
  });

  const invalidSyncIds = [
    "",
    "sync|0123456789abcdef",
    "a".repeat(129),
  ];
  for (const syncId of invalidSyncIds) {
    assert.throws(
      () => api.contributionReceipt(syncId),
      (error) => {
        assert.equal(error instanceof TypeError, true);
        assert.match(error.message, /sync identity/iu);
        return true;
      },
    );
  }
  assert.equal(fetchCalls, 0);
});

test("surfaces receipt outages with bounded Retry-After preference", async () => {
  let receiptCalls = 0;
  const api = testApi(async (url) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/session")) return json(SESSION, 201);
    if (path.endsWith("/cleanup")) return new Response(null, { status: 204 });
    if (path.endsWith("/contributions/receipt")) {
      receiptCalls += 1;
      if (receiptCalls === 1) {
        return json({
          error: "contributions_unavailable",
          message: "Contributions are paused.",
          retry_after: 7,
        }, 503, { "Retry-After": "20" });
      }
      if (receiptCalls === 2) {
        return json({ error: "contributions_unavailable" }, 503, {
          "Retry-After": "11",
        });
      }
      return json({
        error: "contributions_unavailable",
        retry_after: 999_999,
      }, 503);
    }
    return json({ error: "not_found" }, 404);
  });

  await api.createSession("OwnerBoundLaunch1");
  await assert.rejects(api.contributionReceipt(SYNC_ID), (error) => {
    assert.equal(error instanceof ApiError, true);
    assert.equal(error.code, "contributions_unavailable");
    assert.equal(error.status, 503);
    assert.equal(error.retryable, true);
    assert.equal(error.retryAfter, 7);
    return true;
  });
  await assert.rejects(api.contributionReceipt(SYNC_ID), (error) => {
    assert.equal(error.code, "contributions_unavailable");
    assert.equal(error.retryAfter, 11);
    return true;
  });
  await assert.rejects(api.contributionReceipt(SYNC_ID), (error) => {
    assert.equal(error.retryAfter, 3_600);
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

test("ignores legacy contribution capability headers harmlessly", async () => {
  const requests = [];
  const api = testApi(async (url, options) => {
    const path = new URL(url).pathname;
    requests.push({ path, options });
    if (path.endsWith("/session")) {
      return json(SESSION, 201, {
        "X-Contribution-Token": LEGACY_CONTRIBUTION_TOKEN,
      });
    }
    if (path.endsWith("/cleanup")) return new Response(null, { status: 204 });
    if (path.endsWith("/contributions/status")) {
      // A malformed legacy token would have failed the old client; the new
      // client must not even look at it.
      return json(STATUS, 200, {
        "X-Contribution-Token": "gbc_not-a-32-byte-token",
      });
    }
    return json({ error: "not_found" }, 404);
  });

  await api.createSession("OwnerBoundLaunch1");
  assert.deepEqual(await api.contributionStatus(), STATUS);
  // The capability transport is gone entirely, not merely dormant.
  assert.equal(api.contributionTransportReady, undefined);
  assert.equal(api.syncContributions, undefined);
  assert.equal(api.acknowledgeContributionDisclosure, undefined);

  // Every authenticated request keeps the plain session bearer even after
  // responses dangled capability tokens.
  assert.deepEqual(await api.contributionStatus(), STATUS);
  const authenticated = requests.filter(({ path }) =>
    path.endsWith("/contributions/status")
  );
  assert.equal(authenticated.length, 2);
  for (const { options } of authenticated) {
    assert.equal(options.headers.Authorization, `Bearer ${SESSION_TOKEN}`);
  }
});
