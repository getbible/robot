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
  basket: { count: 0, maximum: 100, items: [] },
};
const PUBLIC_API = {
  async translations() {
    throw new Error("Session bootstrap must not call the public API.");
  },
  async chapter() {
    return {};
  },
};

function createApi(initData, options = {}) {
  return new MiniAppApi(initData, {
    publicApi: PUBLIC_API,
    ...options,
  });
}

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
      const api = createApi("signed-init-data");
      const payload = await api.createSession("owner-bound-launch");
      assert.equal(payload.session_token, SESSION_PAYLOAD.session_token);
      assert.equal("translations" in payload, false);
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
      const api = createApi("signed-init-data");
      return api.createSession("owner-bound-launch");
    },
  );

  assert.equal(payload.session_token, SESSION_PAYLOAD.session_token);
  assert.equal("translations" in payload, false);
  assert.equal(requests, 2);
});

test("a resumed session also sends exactly one authenticated cleanup signal", async () => {
  const requests = [];
  const api = createApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    fetchImplementation: async (url, options) => {
      requests.push({ url: String(url), options });
      return requests.length === 1
        ? jsonResponse(SESSION_PAYLOAD)
        : new Response(null, { status: 204 });
    },
  });

  const payload = await api.resumeSession(SESSION_PAYLOAD.session_token);

  assert.equal(payload.session_token, SESSION_PAYLOAD.session_token);
  assert.equal("translations" in payload, false);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].options.method, "GET");
  assert.equal(requests[1].url, "https://robot.example/getbible/api/v1/cleanup");
  assert.equal(requests[1].options.method, "POST");
  assert.equal(requests[1].options.keepalive, true);
});

test("browser platform callables retain the global receiver", async () => {
  const requests = [];
  const api = createApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    fetchImplementation: function (url, options) {
      assert.equal(this, globalThis);
      requests.push({ url: String(url), options });
      return Promise.resolve(
        requests.length === 1
          ? jsonResponse(SESSION_PAYLOAD, 201)
          : new Response(null, { status: 204 }),
      );
    },
    setTimeoutImplementation: function (callback, delay) {
      assert.equal(this, globalThis);
      return setTimeout(callback, delay);
    },
    clearTimeoutImplementation: function (handle) {
      assert.equal(this, globalThis);
      clearTimeout(handle);
    },
  });

  const payload = await api.createSession("OwnerBoundLaunch1");
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(payload.session_token, SESSION_PAYLOAD.session_token);
  assert.equal(requests.length, 2);
});

test("delayed basket transport remains data-only after local invalidation", async () => {
  let releaseBasket;
  const basketGate = new Promise((resolve) => {
    releaseBasket = resolve;
  });
  const requests = [];
  const api = createApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    fetchImplementation: async (url, options) => {
      const path = new URL(url).pathname;
      requests.push({ path, options });
      if (path.endsWith("/session")) {
        return jsonResponse(SESSION_PAYLOAD, 201);
      }
      if (path.endsWith("/cleanup")) {
        return new Response(null, { status: 204 });
      }
      await basketGate;
      return jsonResponse({
        items: [{
          selection_id: "SelectionToken123",
          translation: "kjv",
          reference: "John 3:16",
          book_number: 43,
          book_name: "John",
          chapter: 3,
          verse: 16,
          text: "For God so loved the world.",
        }],
        count: 1,
        maximum: 100,
      });
    },
  });
  await api.createSession("OwnerBoundLaunch1");

  const delayed = api.addBasketItem("SelectionToken123");
  api.clearSession();
  releaseBasket();

  const payload = await delayed;
  assert.equal(payload.count, 1);
  await assert.rejects(
    api.basket(),
    (error) => error?.code === "session_not_ready",
  );
  assert.equal(
    requests.filter((request) => request.path.endsWith("/basket/items")).length,
    1,
  );
});

test("explicit revocation calls DELETE session and clears local auth", async () => {
  const requests = [];
  const api = createApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    fetchImplementation: async (url, options) => {
      requests.push({ url: String(url), options });
      return requests.length === 1
        ? jsonResponse(SESSION_PAYLOAD, 201)
        : new Response(null, { status: 204 });
    },
  });
  await api.createSession("OwnerBoundLaunch1");
  await api.revokeSession();

  assert.ok(
    requests.some(
      (request) =>
        request.url.endsWith("/api/v1/session") &&
        request.options.method === "DELETE" &&
        request.options.keepalive === true,
    ),
  );
  await assert.rejects(
    api.basket(),
    (error) => error?.code === "session_not_ready",
  );
});

test("failed explicit revocation still clears local auth", async () => {
  const api = createApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    fetchImplementation: async (url, options) => {
      const path = new URL(url).pathname;
      if (path.endsWith("/session") && options.method === "POST") {
        return jsonResponse(SESSION_PAYLOAD, 201);
      }
      if (path.endsWith("/cleanup")) {
        return new Response(null, { status: 204 });
      }
      throw new TypeError("offline");
    },
  });
  await api.createSession("OwnerBoundLaunch1");

  await assert.rejects(
    api.revokeSession(),
    (error) => error?.code === "network_error",
  );
  await assert.rejects(
    api.basket(),
    (error) => error?.code === "session_not_ready",
  );
});