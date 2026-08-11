import assert from "node:assert/strict";
import test from "node:test";

import { MiniAppApi } from "../lib/api.js";

const PUBLIC_API = {
  async translations() {
    return [{ code: "kjv" }];
  },
  async chapter() {
    return {};
  },
};

function session(limits) {
  return {
    session_token: "abcdefghijklmnop",
    expires_in: 900,
    user: { id: 42 },
    preferences: {
      translation: "kjv",
      search_defaults: {},
      reader_location: null,
    },
    entrypoint: { route: "search", query: "" },
    translations: [{ code: "kjv", name: "King James Version" }],
    basket: { items: [], count: 0, maximum: 100 },
    ...(limits === undefined ? {} : { limits }),
  };
}

/**
 * Records the deadline armed for each request without letting any of them fire,
 * so the budget a call is given can be read directly.
 */
function budgetRecorder({ limits, resume = false } = {}) {
  const armed = [];
  const api = new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: PUBLIC_API,
    fetchImplementation: async (url) => {
      const path = new URL(url).pathname;
      const body = path.endsWith("/session")
        ? session(limits)
        : { search_id: "s".repeat(16), items: [], total: 0, page: 0 };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
    setTimeoutImplementation: (callback, delay) => {
      armed.push(delay);
      return setTimeout(callback, delay);
    },
    clearTimeoutImplementation: (handle) => clearTimeout(handle),
  });
  const ready = resume
    ? api.resumeSession("abcdefghijklmnop")
    : api.createSession();
  return { api, armed, ready };
}

test("a search waits for the budget the robot declares", async () => {
  const { api, armed, ready } = budgetRecorder({
    limits: { search_timeout_seconds: 150 },
  });
  await ready;
  armed.length = 0;

  await api.search("λόγος", { translation: "kjv" });

  // 150 s of server budget plus the grace that covers the queue wait and the
  // round trip, so the robot's own answer arrives before the page gives up.
  assert.deepEqual(armed, [160_000]);
});

test("a resumed session adopts the declared budget too", async () => {
  const { api, armed, ready } = budgetRecorder({
    limits: { search_timeout_seconds: 300 },
    resume: true,
  });
  await ready;
  armed.length = 0;

  await api.search("λόγος", { translation: "kjv" });

  assert.deepEqual(armed, [310_000]);
});

test("a robot that declares no budget still gets a search-shaped wait", async () => {
  // An older robot says nothing about its budget. The floor must still exceed
  // an index build, because the failure being prevented is the page reporting a
  // timeout for work the robot was still doing.
  for (const limits of [undefined, {}, { search_timeout_seconds: "soon" }]) {
    const { api, armed, ready } = budgetRecorder({ limits });
    await ready;
    armed.length = 0;

    await api.search("λόγος", { translation: "kjv" });

    assert.deepEqual(armed, [150_000], JSON.stringify(limits ?? null));
  }
});

test("a declared budget cannot be inflated without bound", async () => {
  const { api, armed, ready } = budgetRecorder({
    limits: { search_timeout_seconds: 10_000_000 },
  });
  await ready;
  armed.length = 0;

  await api.search("λόγος", { translation: "kjv" });

  assert.deepEqual(armed, [900_000]);
});

test("ordinary requests keep the ordinary deadline", async () => {
  const { api, armed, ready } = budgetRecorder({
    limits: { search_timeout_seconds: 150 },
  });
  await ready;
  armed.length = 0;

  await api.preferences({ translation: "kjv" });

  assert.deepEqual(armed, [15_000]);
});
