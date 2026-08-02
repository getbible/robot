import assert from "node:assert/strict";
import test from "node:test";

import { MiniAppApi } from "../lib/api.js";

const SESSION = {
  session_token: "abcdefghijklmnop",
  expires_in: 900,
  user: { id: 42 },
  preferences: { translation: "kjv", search_defaults: {}, reader_location: null },
  entrypoint: { route: "bible", query: "" },
  basket: { items: [], count: 0, maximum: 100 },
};

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("Bible catalogs and chapters bypass the authenticated robot data routes", async () => {
  const serverRequests = [];
  const publicCalls = [];
  const publicTranslations = [{ code: "kjv" }];
  const publicApi = {
    async translations() {
      publicCalls.push(["translations"]);
      return publicTranslations;
    },
    async books(translation) {
      publicCalls.push(["books", translation]);
      return { translation, items: [{ number: 43, name: "John" }] };
    },
    async chapters(translation, book) {
      publicCalls.push(["chapters", translation, book]);
      return { translation, book: { number: book }, items: [{ number: 3, verses: [16] }] };
    },
    async chapter(translation, book, chapter, verse) {
      publicCalls.push(["chapter", translation, book, chapter, verse]);
      return { translation, book: { number: book }, chapter, items: [] };
    },
    async resolveReference(translation, reference) {
      publicCalls.push(["reference", translation, reference]);
      return { translation, book_number: 43, chapter: 3, verse: 16 };
    },
  };
  const api = new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi,
    fetchImplementation: async (url) => {
      serverRequests.push(String(url));
      return serverRequests.length === 1
        ? json(SESSION, 201)
        : new Response(null, { status: 204 });
    },
  });

  const bootstrap = await api.createSession("LaunchToken123456");
  assert.equal("translations" in bootstrap, false);
  assert.deepEqual(await api.translations(), publicTranslations);
  await api.books("kjv");
  await api.chapters("kjv", 43);
  await api.scripture("kjv", 43, 3, 16);
  await api.resolveReference("kjv", "John 3:16");

  assert.deepEqual(publicCalls, [
    ["translations"],
    ["books", "kjv"],
    ["chapters", "kjv", 43],
    ["chapter", "kjv", 43, 3, 16],
    ["reference", "kjv", "John 3:16"],
  ]);
  assert.ok(serverRequests.every((url) =>
    !/\/(translations|books|chapters|scripture)(?:\?|$)/.test(url),
  ));
});

test("direct reader selection is registered authoritatively before basket mutation", async () => {
  const requests = [];
  const authoritativeSelection = {
    selection_id: "AuthoritativeSelectionToken123",
    translation: "kjv",
    reference: "John 3:16",
    book_number: 43,
    book_name: "John",
    chapter: 3,
    verse: 16,
    text: "For God so loved the world.",
  };
  const api = new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: {
      async translations() {
        return [{ code: "kjv" }];
      },
      async chapter() {
        return {};
      },
    },
    fetchImplementation: async (url, options) => {
      const request = { url: String(url), options };
      requests.push(request);
      if (request.url.endsWith("/api/v1/session")) {
        return json(SESSION, 201);
      }
      if (request.url.endsWith("/api/v1/cleanup")) {
        return new Response(null, { status: 204 });
      }
      if (request.url.endsWith("/api/v1/scripture")) {
        return json({ items: [authoritativeSelection] });
      }
      if (request.url.endsWith("/api/v1/basket/items")) {
        return json({ items: [authoritativeSelection], count: 1, maximum: 100 });
      }
      return json({ error: { code: "not_found" } }, 404);
    },
  });
  await api.createSession("LaunchToken123456");

  await api.addBasketItem("gbd_kjv_043_0003_0016");

  const scriptureRequest = requests.find((request) =>
    request.url.endsWith("/api/v1/scripture"),
  );
  assert.ok(scriptureRequest);
  assert.deepEqual(JSON.parse(scriptureRequest.options.body), {
    translation: "kjv",
    book: 43,
    chapter: 3,
    verse: 16,
  });

  const basketRequest = requests.find((request) =>
    request.url.endsWith("/api/v1/basket/items"),
  );
  assert.ok(basketRequest);
  assert.deepEqual(JSON.parse(basketRequest.options.body), {
    selection_id: authoritativeSelection.selection_id,
  });
  assert.equal(basketRequest.options.headers.Authorization, "Bearer abcdefghijklmnop");
});

test("opaque search selections still go directly to the basket endpoint", async () => {
  const requests = [];
  const api = new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: {
      async translations() {
        return [{ code: "kjv" }];
      },
      async chapter() {
        return {};
      },
    },
    fetchImplementation: async (url, options) => {
      const request = { url: String(url), options };
      requests.push(request);
      if (request.url.endsWith("/api/v1/session")) {
        return json(SESSION, 201);
      }
      if (request.url.endsWith("/api/v1/cleanup")) {
        return new Response(null, { status: 204 });
      }
      return json({ items: [], count: 0, maximum: 100 });
    },
  });
  await api.createSession("LaunchToken123456");

  await api.addBasketItem("OpaqueSearchSelectionToken123");

  assert.equal(
    requests.some((request) => request.url.endsWith("/api/v1/scripture")),
    false,
  );
  const basketRequest = requests.find((request) =>
    request.url.endsWith("/api/v1/basket/items"),
  );
  assert.ok(basketRequest);
  assert.deepEqual(JSON.parse(basketRequest.options.body), {
    selection_id: "OpaqueSearchSelectionToken123",
  });
});
