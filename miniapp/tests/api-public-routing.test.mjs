import assert from "node:assert/strict";
import test from "node:test";

import { MiniAppApi } from "../lib/api.js";
import { GetBibleApi } from "../lib/getbible-api.js";
import { GetBibleTransport } from "../lib/getbible-transport.js";
import {
  BrowserPublicCache,
  MemoryPublicStore,
} from "../lib/public-cache.js";

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

test("display-only Scripture previews never become selectable authority", async () => {
  const robotRequests = [];
  const publicCalls = [];
  const api = client({
    onRobotRequest: (request) => robotRequests.push(request.url),
    publicApi: {
      async translations() {
        return [{ code: "kjv" }];
      },
      async chapter(translation, book, chapter, verse, options) {
        publicCalls.push([translation, book, chapter, verse, options ?? null]);
        return {
          translation,
          book: { number: book },
          chapter,
          items: [READER_VERSE],
        };
      },
    },
  });
  await api.createSession("LaunchToken123456");

  const preview = await api.scripturePreview("kjv", 43, 3, 16);
  assert.equal(preview.items[0].text, READER_VERSE.text);
  assert.throws(
    () => api.addBasketItem(READER_VERSE.selection_id),
    (error) => error?.code === "invalid_selection",
  );

  await api.scripture("kjv", 43, 3, 16);
  const selected = await api.addBasketItem(READER_VERSE.selection_id);
  assert.equal(selected.items[0].selection_id, READER_VERSE.selection_id);
  assert.deepEqual(publicCalls, [
    ["kjv", 43, 3, 16, { includeNavigation: false }],
    ["kjv", 43, 3, 16, null],
  ]);
  assert.equal(
    robotRequests.some((url) => /\/(?:scripture|history)(?:\?|$)/.test(url)),
    false,
  );
});

test("reader and search verses use the same local basket contract", async () => {
  const searchVerse = {
    selection_id: "gbd_kjv_043_0003_0017",
    translation: "kjv",
    reference: "John 3:17",
    book_number: 43,
    book_name: "John",
    chapter: 3,
    verse: 17,
    text: "For God sent not his Son into the world to condemn the world.",
    terms: ["world"],
    highlights: [{ start: 34, end: 39 }, { start: 55, end: 60 }],
  };
  const robotRequests = [];
  const searches = [];
  const api = new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: {
      async translations() {
        return [{ code: "kjv" }];
      },
      async chapter() {
        return { items: [READER_VERSE] };
      },
      async search(translation, query, filters, page) {
        searches.push([translation, query, filters, page]);
        return {
          kind: "search",
          translation,
          query_text: query,
          total: 1,
          returned: 1,
          offset: 0,
          limit: 25,
          has_more: false,
          sha: null,
          engine_version: 5,
          items: [searchVerse],
        };
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
      return json({ error: { code: "unexpected_robot_request" } }, 500);
    },
  });

  await api.createSession("LaunchToken123456");
  await api.scripture("kjv", 43, 3, 16);
  const page = await api.search("kjv", "world", { words: "all" }, { offset: 0, limit: 25 });
  await api.addBasketItem(READER_VERSE.selection_id);
  await api.addBasketItem(searchVerse.selection_id);

  assert.deepEqual(searches, [["kjv", "world", { words: "all" }, { offset: 0, limit: 25 }]]);
  assert.deepEqual(page.items[0].highlights, searchVerse.highlights);
  const basket = await api.basket();
  assert.deepEqual(
    basket.items.map((item) => item.reference),
    ["John 3:16", "John 3:17"],
  );
  assert.deepEqual(basket.items[1].highlights, searchVerse.highlights);
  assert.equal(
    robotRequests.some((request) => /\/api\/v1\/(?:basket|search)/.test(request.url)),
    false,
  );
});

test("searches go straight to the search origin and never through Robot", async () => {
  const robotRequests = [];
  const publicRequests = [];
  const envelope = {
    query: {
      text: "world",
      kind: "search",
      translation: { abbreviation: "kjv" },
      engine_version: 5,
      total: 1,
      returned: 1,
      offset: 0,
      limit: 25,
      has_more: false,
      sha: null,
    },
    results: {
      kjv_43_3: {
        abbreviation: "kjv",
        book_nr: 43,
        book_name: "John",
        chapter: 3,
        name: "John 3",
        verses: [{ chapter: 3, verse: 16, name: "John 3:16", text: READER_VERSE.text }],
      },
    },
    matches: [{ reference: "John 3:16", book_nr: 43, chapter: 3, verse: 16, terms: ["world"] }],
  };
  const transport = new GetBibleTransport({
    fetchImplementation: async (url, options) => {
      publicRequests.push({ url: String(url), options });
      return json(envelope);
    },
  });
  const api = client({
    onRobotRequest: (request) => {
      robotRequests.push(request.url);
      assert.doesNotMatch(request.url, /\/api\/v1\/search/);
    },
    publicApi: new GetBibleApi({
      transport,
      cache: new BrowserPublicCache({ store: new MemoryPublicStore(), now: () => 1 }),
      now: () => 1,
    }),
  });
  await api.createSession("LaunchToken123456");

  const page = await api.search("kjv", "world", {});

  assert.equal(publicRequests.length, 1);
  assert.equal(
    new URL(publicRequests[0].url).origin,
    "https://search.getbible.net",
  );
  assert.match(publicRequests[0].url, /^https:\/\/search\.getbible\.net\/v2\/kjv\?q=world&/);
  assert.equal(publicRequests[0].options.headers.Authorization, undefined);
  assert.equal(publicRequests[0].options.credentials, "omit");
  assert.equal(page.items[0].selection_id, "gbd_kjv_043_0003_0016");
  assert.deepEqual(page.items[0].highlights, [{ start: 21, end: 26 }]);
  assert.ok(robotRequests.every((url) => !/\/search(?:\?|\/|$)/.test(url)));
  const selected = await api.addBasketItem("gbd_kjv_043_0003_0016");
  assert.equal(selected.items[0].reference, "John 3:16");
});

test("only the three public GetBible origins are accepted as fixed roots", () => {
  const options = { fetchImplementation: async () => json({}) };
  assert.doesNotThrow(() => new GetBibleTransport({
    ...options,
    apiRoot: "https://api.getbible.net/v2/",
    queryRoot: "https://query.getbible.net/v2/",
    searchRoot: "https://search.getbible.net/v2/",
  }));
  for (const searchRoot of [
    "http://search.getbible.net/v2/",
    "https://search.getbible.net/v2",
    "https://search.getbible.net/v2/?q=x",
    "https://user:pass@search.getbible.net/v2/",
    "https://search.getbible.net/v2/#top",
    "not a url",
  ]) {
    assert.throws(
      () => new GetBibleTransport({ ...options, searchRoot }),
      TypeError,
      searchRoot,
    );
  }
  if (process.env.NODE_ENV !== "test") {
    // The host pin is lifted only for a test runtime that names itself.
    assert.throws(
      () => new GetBibleTransport({ ...options, searchRoot: "https://search.example.net/v2/" }),
      /allowlisted/,
    );
  }
});
