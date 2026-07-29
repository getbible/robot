import assert from "node:assert/strict";
import test from "node:test";

import { MiniAppApi } from "../lib/api.js";

const SESSION_PAYLOAD = {
  session_token: "abcdefghijklmnop",
  expires_in: 900,
  user: { id: 42 },
  preferences: {
    translation: "kjv",
    search_defaults: {},
    reader_location: null,
  },
  entrypoint: { route: "bible", query: "" },
  translations: [],
  basket: { count: 0, maximum: 100, items: [] },
};

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function withBrowser(fetchImplementation, operation) {
  const originalWindow = globalThis.window;
  const originalDocument = globalThis.document;
  const originalFetch = globalThis.fetch;
  globalThis.window = {
    setTimeout,
    clearTimeout,
  };
  globalThis.document = {
    baseURI: "https://robot.example/getbible/",
  };
  globalThis.fetch = fetchImplementation;
  try {
    return await operation();
  } finally {
    globalThis.window = originalWindow;
    globalThis.document = originalDocument;
    globalThis.fetch = originalFetch;
  }
}

test("a created session sends one authenticated launch-cleanup signal", async () => {
  const requests = [];
  await withBrowser(
    async (url, options) => {
      requests.push({ url: String(url), options });
      return requests.length === 1
        ? jsonResponse(SESSION_PAYLOAD, 201)
        : new Response(null, { status: 204 });
    },
    async () => {
      const api = new MiniAppApi("signed-init-data");
      const payload = await api.createSession("owner-bound-launch");
      assert.equal(payload.session_token, SESSION_PAYLOAD.session_token);
    },
  );

  assert.equal(requests.length, 2);
  assert.equal(requests[0].url, "https://robot.example/getbible/api/v1/session");
  assert.equal(requests[1].url, "https://robot.example/getbible/api/v1/cleanup");
  assert.equal(requests[1].options.method, "POST");
  assert.equal(
    requests[1].options.headers.Authorization,
    `Bearer ${SESSION_PAYLOAD.session_token}`,
  );
  assert.equal(
    requests[1].options.headers["X-Telegram-Init-Data"],
    "signed-init-data",
  );
  assert.equal(requests[1].options.keepalive, true);
});

test("cleanup transport failure cannot invalidate successful session creation", async () => {
  let requests = 0;
  const payload = await withBrowser(
    async () => {
      requests += 1;
      if (requests === 1) {
        return jsonResponse(SESSION_PAYLOAD, 201);
      }
      throw new TypeError("cleanup transport unavailable");
    },
    async () => {
      const api = new MiniAppApi("signed-init-data");
      return api.createSession("owner-bound-launch");
    },
  );

  assert.equal(payload.session_token, SESSION_PAYLOAD.session_token);
  assert.equal(requests, 2);
});
