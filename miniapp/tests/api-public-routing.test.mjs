import assert from "node:assert/strict";
import test from "node:test";

import { MiniAppApi } from "../lib/api.js";

const SESSION = {
  session_token: "abcdefghijklmnop",
  expires_in: 10_800,
  user: { id: 42 },
  preferences: { translation: "kjv", search_defaults: {}, reader_location: null },
  entrypoint: { route: "bible", query: "" },
  basket: { items: [], count: 0, maximum: 100 },
};

const READER_VERSE = {
  selection_id: "gbd_kjv_043_0003_0016",
  translation: "kjv",
  reference: "John 3:16",
  book_number: 43,
  book_name: "John",
  chapter: 3,
  verse: 16,
  text: "For God so loved the world.",
  terms: [],
  highlights: [],
};

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function client({ publicApi, onRobotRequest } = {}) {
  return new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: publicApi ?? {
      async translations() {
        return [{ code: "kjv" }];
      },
      async chapter() {
        return {
          translation: "kjv",
          book: { number: 43 },
          chapter: 3,
          items: [READER_VERSE],
        };
      },
    },
    fetchImplementation: async (url, options) => {
      const request = { url: String(url), options };
      onRobotRequest?.(request);
      if (request.url.endsWith("/api/v1/session")) {
        return json(SESSION, 201);
      }
      if (request.url.endsWith("/api/v1/cleanup")) {
        return new Response(null, { status: 204 });
      }
      return json({ error: { code: "unexpected_robot_request" } }, 500);
    },
  });
}

test("Bible catalogs and chapters bypass authenticated Robot data routes", async () => {
  const robotRequests = [];
  const publicCalls = [];
  const api = client({
    onRobotRequest: (request) => robotRequests.push(request.url),
    publicApi: {
      async translations() {
        publicCalls.push(["translations"]);
        return [{ code: "kjv" }];
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
        return { translation, book: { number: book }, chapter, items: [READER_VERSE] };
      },
      async resolveReference(translation, reference) {
        publicCalls.push(["reference", translation, reference]);
        return { translation, book_number: 43, chapter: 3, verse: 16 };
      },
    },
  });

  const bootstrap = await api.createSession("LaunchToken123456");
  assert.equal("translations" in bootstrap, false);
  await api.translations();
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
  assert.ok(robotRequests.every((url) =>
    !/\/(translations|books|chapters|scripture)(?:\?|$)/.test(url),
  ));
});

test("reader selection and unselection stay in browser state", async () => {
  const robotRequests = [];
  const api = client({ onRobotRequest: (request) => robotRequests.push(request.url) });
  await api.createSession("LaunchToken123456");
  await api.scripture("kjv", 43, 3, 16);

  const selected = await api.addBasketItem(READER_VERSE.selection_id);
  assert.deepEqual(selected.items, [READER_VERSE]);
  assert.equal(selected.items[0].selection_id, READER_VERSE.selection_id);

  const removed = await api.removeBasketItem(READER_VERSE.selection_id);
  assert.deepEqual(removed.items, []);

  assert.deepEqual(
    robotRequests.filter((url) => /\/api\/v1\/(basket|scripture)/.test(url)),
    [],
  );
});

test("reader and Librarian verses use the same local basket contract", async () => {
  const searchVerse = {
    selection_id: "OpaqueSearchSelectionToken123",
    translation: "kjv",
    reference: "John 3:17",
    book_number: 43,
    book_name: "John",
    chapter: 3,
    verse: 17,
    text: "For God sent not his Son into the world to condemn the world.",
    terms: ["world"],
  };
  const robotRequests = [];
  const api = new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: {
      async translations() {
        return [{ code: "kjv" }];
      },
      async chapter() {
        return { items: [READER_VERSE] };
      },
    },
    fetchImplementation: async (url, options) => {
      const request = { url: String(url), options };
      robotRequests.push(request);
      if (request.url.endsWith("/api/v1/session")) {
        return json(SESSION, 201);
      }
      if (request.url.endsWith("/api/v1/cleanup")) {
        return new Response(null, { status: 204 });
      }
      if (request.url.endsWith("/api/v1/search")) {
        return json({ items: [searchVerse], total: 1, page: 0, has_more: false });
      }
      return json({ error: { code: "unexpected_robot_request" } }, 500);
    },
  });

  await api.createSession("LaunchToken123456");
  await api.scripture("kjv", 43, 3, 16);
  await api.search("world", { translation: "kjv" });
  await api.addBasketItem(READER_VERSE.selection_id);
  await api.addBasketItem(searchVerse.selection_id);

  const basket = await api.basket();
  assert.deepEqual(
    basket.items.map((item) => item.reference),
    ["John 3:16", "John 3:17"],
  );
  assert.equal(
    robotRequests.some((request) => request.url.endsWith("/api/v1/basket/items")),
    false,
  );
});
