import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, MiniAppApi } from "../lib/api.js";

const SESSION = {
  session_token: "abcdefghijklmnop",
  limits: { search_timeout_seconds: 30 },
  basket: { maximum: 100, items: [] },
};
const STATUS = {
  enabled: true,
  state: "approved",
  can_contribute: true,
  disclosure_required: false,
};

function json(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

test("uses authenticated contribution routes and revalidates the live catalog", async () => {
  const requests = [];
  let catalogCalls = 0;
  const api = new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: {
      async translations() { return []; },
      async chapter() { return {}; },
    },
    fetchImplementation: async (url, options) => {
      const parsed = new URL(url);
      const path = parsed.pathname;
      requests.push({ path, search: parsed.search, options });
      if (path.endsWith("/session")) return json(SESSION, 201);
      if (path.endsWith("/cleanup")) return new Response(null, { status: 204 });
      if (path.endsWith("/contributions/status")) return json(STATUS);
      if (path.endsWith("/contributions/events")) {
        const events = JSON.parse(options.body).events;
        return json({
          accepted: events.length,
          replayed: 0,
          event_ids: Object.fromEntries(
            events.map((event, index) => [event.client_event_id, index + 1]),
          ),
        });
      }
      if (path.endsWith("/bookmarks/catalog")) {
        catalogCalls += 1;
        if (catalogCalls === 2) {
          return new Response(null, {
            status: 304,
            headers: { ETag: '"catalog-4"' },
          });
        }
        return json({
          revision: 4,
          checksum: "a".repeat(64),
          catalog: {
            schema_version: 1,
            topics: [],
            associations: { add: [], remove: [] },
          },
        }, 200, { ETag: '"catalog-4"' });
      }
      return json({ error: { code: "not_found" } }, 404);
    },
  });

  await api.createSession("OwnerBoundLaunch1");
  assert.deepEqual(await api.contributionStatus(), STATUS);
  assert.deepEqual(await api.acknowledgeContributionDisclosure(), STATUS);
  const event = {
    client_event_id: "event:one",
    type: "verse_add",
    topic: {
      local_topic_id: "grace",
      name: "Grace",
      color: "#bbf7d0",
    },
    verse: { book: 43, chapter: 3, verse: 16 },
  };
  assert.equal((await api.submitContributionEvents([event])).accepted, 1);
  const catalog = await api.bookmarkCatalog();
  assert.equal(catalog.etag, '"catalog-4"');
  assert.deepEqual(await api.bookmarkCatalog(catalog.etag), {
    not_modified: true,
    etag: '"catalog-4"',
  });

  const authenticated = requests.filter(({ path }) =>
    /\/(?:contributions|bookmarks\/catalog)/u.test(path)
  );
  assert.equal(authenticated.length, 5);
  for (const { options } of authenticated) {
    assert.equal(options.headers.Authorization, `Bearer ${SESSION.session_token}`);
    assert.equal(options.headers["X-Telegram-Init-Data"], "signed-init-data");
  }
  const statusRequests = authenticated.filter(({ path }) =>
    path.endsWith("/contributions/status")
  );
  assert.equal(statusRequests.length, 2);
  assert.ok(statusRequests.every(({ search }) => search === "?details=1"));
  const patch = authenticated.find(({ path, options }) =>
    path.endsWith("/contributions/status") && options.method === "PATCH"
  );
  assert.deepEqual(JSON.parse(patch.options.body), {
    disclosure_acknowledged: true,
  });
  const submitted = authenticated.find(({ path }) =>
    path.endsWith("/contributions/events")
  );
  assert.deepEqual(JSON.parse(submitted.options.body), { events: [event] });
  assert.equal(Object.hasOwn(event.verse, "text"), false);
  assert.equal(
    authenticated.at(-1).options.headers["If-None-Match"],
    '"catalog-4"',
  );
});

test("refuses unbounded contribution batches and unsafe ETags before transport", () => {
  const api = new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: {
      async translations() { return []; },
      async chapter() { return {}; },
    },
    fetchImplementation: async () => {
      throw new Error("transport must not run");
    },
  });
  assert.throws(() => api.submitContributionEvents([]), /bounded/i);
  assert.throws(
    () => api.submitContributionEvents(Array.from({ length: 51 }, () => ({}))),
    /bounded/i,
  );
  assert.throws(() => api.bookmarkCatalog("bad\nvalue"), /ETag/i);
});

test("rejects a malformed success receipt so the durable caller can retry", async () => {
  const api = new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: {
      async translations() { return []; },
      async chapter() { return {}; },
    },
    fetchImplementation: async (url) => {
      const path = new URL(url).pathname;
      if (path.endsWith("/session")) return json(SESSION, 201);
      if (path.endsWith("/cleanup")) return new Response(null, { status: 204 });
      if (path.endsWith("/contributions/events")) {
        return json({ accepted: 0, replayed: 0, event_ids: {} });
      }
      return json({ error: "not_found" }, 404);
    },
  });
  await api.createSession("OwnerBoundLaunch1");
  await assert.rejects(api.submitContributionEvents([{
    client_event_id: "event:retry",
    type: "topic_delete",
    topic: { local_topic_id: "grace" },
  }]), /unexpected contribution receipt/i);
});

test("preserves bounded Retry-After guidance from error bodies and headers", async () => {
  let statusCalls = 0;
  const api = new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: {
      async translations() { return []; },
      async chapter() { return {}; },
    },
    fetchImplementation: async (url) => {
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
    },
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
