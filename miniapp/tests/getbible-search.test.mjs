import assert from "node:assert/strict";
import test from "node:test";

import { GetBibleApi } from "../lib/getbible-api.js";
import { normalizeSearchPayload } from "../lib/getbible-model.js";
import {
  GetBibleTransport,
  PublicApiError,
} from "../lib/getbible-transport.js";
import {
  BrowserPublicCache,
  MemoryPublicStore,
} from "../lib/public-cache.js";

const SHA = "0123456789abcdef0123456789abcdef01234567";
const SEARCH_ORIGIN = "https://search.getbible.net/v2/";

const JOHN_3 = {
  translation: "King James Version",
  abbreviation: "kjv",
  lang: "en",
  language: "English",
  direction: "LTR",
  encoding: "UTF-8",
  book_nr: 43,
  book_name: "John",
  chapter: 3,
  name: "John 3",
  ref: ["John 3:16", "John 3:17"],
  verses: [
    // Deliberately not in verse order: the match list decides the order.
    {
      chapter: 3,
      verse: 17,
      name: "John 3:17",
      text: "For God sent not his Son into the world to condemn the world.",
    },
    {
      chapter: 3,
      verse: 16,
      name: "John 3:16",
      text: "For God so loved the world.",
    },
  ],
};

/** A full-text envelope exactly as Search API v2 documents it. */
function envelope({
  kind = "search",
  translation = "kjv",
  text = "God world",
  total = 40,
  offset = 0,
  limit = 25,
  has_more = true,
  sha = SHA,
  results = { kjv_43_3: JOHN_3 },
  matches = [
    { reference: "John 3:16", book_nr: 43, chapter: 3, verse: 16, score: 2.1, occurrences: 2, terms: ["god", "world"] },
    { reference: "John 3:17", book_nr: 43, chapter: 3, verse: 17, score: 1.4, occurrences: 3, terms: ["god", "world"] },
  ],
  query = {},
} = {}) {
  const base = {
    text,
    kind,
    translation: {
      translation: "King James Version",
      abbreviation: translation,
      lang: "en",
      language: "English",
      direction: "LTR",
      encoding: "UTF-8",
    },
    engine_version: 5,
    total,
    returned: matches.length,
  };
  const paging = kind === "search"
    ? { criteria: {}, sha, offset, limit, has_more, cache: { checked_at: null, stale: false } }
    : {};
  return { query: { ...base, ...paging, ...query }, results, matches };
}

function problem(status, code, detail, extra = {}) {
  return {
    type: "about:blank",
    title: "Refused",
    status,
    code,
    detail,
    instance: "/v2/kjv",
    ...extra,
  };
}

function jsonResponse(body, { status = 200, headers = {} } = {}) {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/**
 * A public API client whose transport answers from a scripted list of
 * responses and records every request it made.
 */
function harness(responses, { attempts = 1, ...options } = {}) {
  const requests = [];
  const queue = [...responses];
  const transport = new GetBibleTransport({
    attempts,
    retryBackoffMs: 0,
    fetchImplementation: async (url, fetchOptions) => {
      requests.push({ url: String(url), options: fetchOptions });
      const next = queue.length > 1 ? queue.shift() : queue[0];
      return typeof next === "function" ? next() : next;
    },
    ...options,
  });
  const api = new GetBibleApi({
    transport,
    cache: new BrowserPublicCache({ store: new MemoryPublicStore(), now: () => 1 }),
    now: () => 1,
  });
  return { api, transport, requests };
}

test("search parameters are built in the documented order on the search origin", async () => {
  const { api, requests } = harness([() => jsonResponse(envelope())]);

  await api.search("KJV", "  God   world ", {
    words: "all",
    match: "substring",
    case_sensitive: true,
    scope: "new_testament",
    diacritics: "exact",
    sort: "relevance",
    books: [62, 43, 43],
    exclude: ["darkness", "Darkness", "night"],
    proximity: 5,
  }, { offset: 25, limit: 25 });

  assert.equal(requests.length, 1);
  assert.equal(
    requests[0].url,
    `${SEARCH_ORIGIN}kjv?q=God+world&words=all&match=substring&case_sensitive=true` +
      "&scope=new_testament&diacritics=exact&sort=relevance&limit=25&offset=25" +
      "&book=43&book=62&exclude=darkness&exclude=night&proximity=5",
  );
  const { options } = requests[0];
  assert.equal(options.method, "GET");
  assert.equal(options.headers.Accept, "application/json");
  assert.equal(options.headers.Authorization, undefined);
  assert.equal(options.credentials, "omit");
  assert.equal(options.cache, "default");
  assert.equal(options.redirect, "error");
  assert.equal(options.referrerPolicy, "no-referrer");
});

test("missing controls take the API defaults and proximity needs words=all", async () => {
  const { api, requests } = harness([() => jsonResponse(envelope())]);

  await api.search("kjv", "God world", {});
  await api.search("kjv", "God world", { words: "any", proximity: 5 });
  await api.search("kjv", "God world", { words: "phrase", proximity: 0 });
  await api.search("kjv", "God world", { words: "all", proximity: 0 });

  assert.equal(
    requests[0].url,
    `${SEARCH_ORIGIN}kjv?q=God+world&words=all&match=whole_word&case_sensitive=false` +
      "&scope=bible&diacritics=fold&sort=canonical&limit=25&offset=0",
  );
  assert.doesNotMatch(requests[1].url, /proximity/);
  assert.doesNotMatch(requests[2].url, /proximity/);
  assert.match(requests[3].url, /&words=all&.*&proximity=0$/);
});

test("search text and exclusions are percent-encoded for every script", async () => {
  const { api, requests } = harness([() => jsonResponse(envelope())]);

  await api.search("kjv", "神爱世人", {});
  await api.search("kjv", "בראשית ברא", { exclude: ["אלהים"] });
  await api.search("kjv", "priests' office", { exclude: ["d'Israel"] });

  assert.match(requests[0].url, /\?q=%E7%A5%9E%E7%88%B1%E4%B8%96%E4%BA%BA&/);
  assert.match(
    requests[1].url,
    /\?q=%D7%91%D7%A8%D7%90%D7%A9%D7%99%D7%AA\+%D7%91%D7%A8%D7%90&/,
  );
  assert.match(requests[1].url, /&exclude=%D7%90%D7%9C%D7%94%D7%99%D7%9D$/);
  assert.match(requests[2].url, /\?q=priests%27\+office&/);
  assert.match(requests[2].url, /&exclude=d%27Israel$/);
  for (const request of requests) {
    assert.doesNotMatch(request.url, /[\s'"<>]/);
  }
});

test("search inputs outside the API contract are refused before any request", async () => {
  const { api, requests } = harness([() => jsonResponse(envelope())]);
  const cases = [
    ["", {}, {}],
    ["   ", {}, {}],
    ["x".repeat(241), {}, {}],
    [42, {}, {}],
    ["God", {}, { limit: 0 }],
    ["God", {}, { limit: 101 }],
    ["God", {}, { offset: -1 }],
    ["God", {}, { offset: 10_001 }],
    ["God", {}, { offset: 1.5 }],
    ["God", { words: "some" }, {}],
    ["God", { match: "fuzzy" }, {}],
    ["God", { scope: "apocrypha" }, {}],
    ["God", { diacritics: "insensitive" }, {}],
    ["God", { sort: "newest" }, {}],
    ["God", { case_sensitive: "yes" }, {}],
    ["God", { books: Array.from({ length: 84 }, (_, index) => index + 1) }, {}],
    ["God", { books: [0] }, {}],
    ["God", { books: "43" }, {}],
    ["God", { exclude: Array.from({ length: 33 }, (_, index) => `w${index}`) }, {}],
    ["God", { exclude: ["x".repeat(101)] }, {}],
    ["God", { exclude: ["two words"] }, {}],
    ["God", { exclude: [""] }, {}],
    ["God", { proximity: 101 }, {}],
  ];
  for (const [query, filters, page] of cases) {
    await assert.rejects(
      api.search("kjv", query, filters, page),
      (error) => error instanceof TypeError || error instanceof RangeError,
      JSON.stringify([query.length, filters, page]),
    );
  }
  await assert.rejects(api.search("not a code", "God", {}), TypeError);
  assert.equal(requests.length, 0);
});

test("a full-text envelope is normalized in match order with highlights", async () => {
  const { api } = harness([() => jsonResponse(envelope())]);

  const page = await api.search("kjv", "God world", {}, { offset: 0, limit: 25 });

  assert.equal(page.kind, "search");
  assert.equal(page.translation, "kjv");
  assert.equal(page.query_text, "God world");
  assert.equal(page.total, 40);
  assert.equal(page.returned, 2);
  assert.equal(page.offset, 0);
  assert.equal(page.limit, 25);
  assert.equal(page.has_more, true);
  assert.equal(page.sha, SHA);
  assert.equal(page.engine_version, 5);
  assert.deepEqual(page.items, [
    {
      selection_id: "gbd_kjv_043_0003_0016",
      translation: "kjv",
      reference: "John 3:16",
      book_number: 43,
      book_name: "John",
      chapter: 3,
      verse: 16,
      text: "For God so loved the world.",
      terms: ["god", "world"],
      highlights: [{ start: 4, end: 7 }, { start: 21, end: 26 }],
    },
    {
      selection_id: "gbd_kjv_043_0003_0017",
      translation: "kjv",
      reference: "John 3:17",
      book_number: 43,
      book_name: "John",
      chapter: 3,
      verse: 17,
      text: "For God sent not his Son into the world to condemn the world.",
      terms: ["god", "world"],
      highlights: [
        { start: 4, end: 7 },
        { start: 34, end: 39 },
        { start: 55, end: 60 },
      ],
    },
  ]);
});

test("highlights follow the diacritics policy the search ran under", async () => {
  const greek = {
    ...JOHN_3,
    book_nr: 43,
    chapter: 1,
    name: "John 1",
    verses: [{ chapter: 1, verse: 1, name: "John 1:1", text: "λόγος ἦν πρὸς τὸν θεόν" }],
  };
  const answer = () => jsonResponse(envelope({
    results: { kjv_43_1: greek },
    matches: [{ reference: "John 1:1", book_nr: 43, chapter: 1, verse: 1, terms: ["λογος"] }],
  }));
  const { api } = harness([answer]);

  const folded = await api.search("kjv", "λογος", { diacritics: "fold" });
  const exact = await api.search("kjv", "λογος", { diacritics: "exact" });

  assert.deepEqual(folded.items[0].highlights, [{ start: 0, end: 5 }]);
  assert.deepEqual(exact.items[0].highlights, []);
});

test("a reference envelope is a complete answer with no pagination", async () => {
  const { api } = harness([() => jsonResponse(envelope({
    kind: "reference",
    text: "John 3:16-17",
    total: 2,
    matches: [
      { reference: "John 3:16", book_nr: 43, chapter: 3, verse: 16 },
      { reference: "John 3:17", book_nr: 43, chapter: 3, verse: 17 },
    ],
  }))]);

  const page = await api.search("kjv", "John 3:16-17", {});

  assert.equal(page.kind, "reference");
  assert.equal(page.total, 2);
  assert.equal(page.returned, 2);
  assert.equal(page.offset, null);
  assert.equal(page.limit, null);
  assert.equal(page.has_more, false);
  assert.equal(page.sha, null);
  assert.deepEqual(
    page.items.map((item) => [item.selection_id, item.terms, item.highlights]),
    [
      ["gbd_kjv_043_0003_0016", [], []],
      ["gbd_kjv_043_0003_0017", [], []],
    ],
  );
});

test("an empty result set is a successful search with total 0", async () => {
  const { api } = harness([() => jsonResponse(envelope({
    total: 0,
    has_more: false,
    results: {},
    matches: [],
  }))]);

  const page = await api.search("kjv", "nothingness", {});

  assert.equal(page.total, 0);
  assert.equal(page.returned, 0);
  assert.equal(page.has_more, false);
  assert.deepEqual(page.items, []);
});

test("has_more and offset let the caller advance by the verses received", async () => {
  const { api, requests } = harness([
    () => jsonResponse(envelope({ offset: 0, limit: 2, total: 3, has_more: true })),
    () => jsonResponse(envelope({
      offset: 2,
      limit: 2,
      total: 3,
      has_more: false,
      matches: [
        { reference: "John 3:17", book_nr: 43, chapter: 3, verse: 17, terms: ["god"] },
      ],
    })),
  ]);

  const first = await api.search("kjv", "God", {}, { offset: 0, limit: 2 });
  const next = first.offset + first.returned;
  const second = await api.search("kjv", "God", {}, { offset: next, limit: 2 });

  assert.equal(first.has_more, true);
  assert.equal(next, 2);
  assert.match(requests[1].url, /&limit=2&offset=2$/);
  assert.equal(second.offset, 2);
  assert.equal(second.returned, 1);
  assert.equal(second.has_more, false);
  assert.equal(second.offset + second.returned, second.total);
  assert.equal(second.sha, first.sha);
});

test("problem documents map to codes the page can act on", async () => {
  const cases = [
    {
      response: () => jsonResponse(problem(400, "invalid_search", "limit must be at most 100"), {
        status: 400,
        headers: { "Content-Type": "application/problem+json" },
      }),
      code: "search_invalid",
      status: 400,
      retryable: false,
      retryAfter: null,
      attempts: 1,
    },
    {
      response: () => jsonResponse(problem(404, "translation_not_found", "unknown translation"), {
        status: 404,
        headers: { "Content-Type": "application/problem+json" },
      }),
      code: "translation_not_found",
      status: 404,
      retryable: false,
      retryAfter: null,
      attempts: 1,
    },
    {
      response: () => jsonResponse(problem(404, "unknown_version", "no such version"), {
        status: 404,
        headers: { "Content-Type": "application/problem+json" },
      }),
      code: "search_unavailable",
      status: 404,
      retryable: false,
      retryAfter: null,
      attempts: 1,
    },
    {
      response: () => jsonResponse(problem(429, "rate_limited", "slow down", { retry_after: 30 }), {
        status: 429,
        headers: { "Content-Type": "application/problem+json", "Retry-After": "30" },
      }),
      code: "search_rate_limited",
      status: 429,
      retryable: true,
      retryAfter: 30,
      // A thirty second wait is advice for the reader, not for the loop.
      attempts: 1,
    },
    {
      response: () => jsonResponse(problem(503, "busy", "capacity"), {
        status: 503,
        headers: { "Content-Type": "application/problem+json", "Retry-After": "0" },
      }),
      code: "search_unavailable",
      status: 503,
      retryable: true,
      retryAfter: 0,
      attempts: 3,
    },
    {
      response: () => jsonResponse(problem(503, "search_timeout", "deadline"), {
        status: 503,
        headers: { "Content-Type": "application/problem+json" },
      }),
      code: "search_unavailable",
      status: 503,
      retryable: true,
      retryAfter: null,
      attempts: 3,
    },
    {
      response: () => new Response("<html>bad gateway</html>", {
        status: 502,
        headers: { "Content-Type": "text/html" },
      }),
      code: "search_unavailable",
      status: 502,
      retryable: true,
      retryAfter: null,
      attempts: 3,
    },
  ];
  for (const expected of cases) {
    const { api, requests } = harness([expected.response], { attempts: 3 });
    await assert.rejects(
      api.search("kjv", "God", {}),
      (error) =>
        error instanceof PublicApiError &&
        error.code === expected.code &&
        error.status === expected.status &&
        error.retryable === expected.retryable &&
        error.retryAfter === expected.retryAfter,
      `${expected.status} ${expected.code}`,
    );
    assert.equal(requests.length, expected.attempts, `${expected.status} attempts`);
  }
});

test("a problem document's detail is carried but never trusted for the code", async () => {
  const { api } = harness([() => jsonResponse(
    problem(400, "rate_limited", "the body lies about being rate limited"),
    { status: 400, headers: { "Content-Type": "application/problem+json" } },
  )]);

  await assert.rejects(
    api.search("kjv", "God", {}),
    (error) =>
      error.code === "search_invalid" &&
      error.retryable === false &&
      error.message === "the body lies about being rate limited",
  );
});

test("redirects are refused rather than followed", async () => {
  // A browser honouring redirect: "error" rejects the fetch outright.
  const rejected = harness([() => {
    throw new TypeError("Failed to fetch");
  }]);
  await assert.rejects(
    rejected.api.search("kjv", "God", {}),
    (error) =>
      error instanceof PublicApiError &&
      error.code === "public_api_network_error",
  );
  assert.equal(rejected.requests[0].options.redirect, "error");

  // A redirect that somehow arrives as a response is a refusal, not a page.
  const answered = harness([() => new Response(null, {
    status: 301,
    headers: { Location: "https://search.getbible.net/v2/kjv/God" },
  })], { attempts: 3 });
  await assert.rejects(
    answered.api.search("kjv", "God", {}),
    (error) =>
      error instanceof PublicApiError &&
      error.code === "search_unavailable" &&
      error.status === 301 &&
      error.retryable === false,
  );
  assert.equal(answered.requests.length, 1);
});

test("an oversized search body is refused", async () => {
  const announced = harness([() => new Response("{}", {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Content-Length": String(1024 * 1024 + 1),
    },
  })], { attempts: 3 });
  await assert.rejects(
    announced.api.search("kjv", "God", {}),
    (error) =>
      error instanceof PublicApiError &&
      error.code === "public_api_response_too_large",
  );
  assert.equal(announced.requests.length, 1);

  const streamed = harness([() => new Response(
    new ReadableStream({
      start(controller) {
        const chunk = new Uint8Array(256 * 1024).fill(0x20);
        for (let index = 0; index < 5; index += 1) controller.enqueue(chunk);
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  )], { attempts: 3 });
  await assert.rejects(
    streamed.api.search("kjv", "God", {}),
    (error) =>
      error instanceof PublicApiError &&
      error.code === "public_api_response_too_large",
  );
  assert.equal(streamed.requests.length, 1);
});

test("malformed JSON is reported as an invalid public response", async () => {
  const { api } = harness([() => jsonResponse('{"query": {"kind": "search"')]);

  await assert.rejects(
    api.search("kjv", "God", {}),
    (error) =>
      error instanceof PublicApiError &&
      error.code === "invalid_public_response",
  );
});

test("an envelope for another translation is refused", async () => {
  const { api } = harness([() => jsonResponse(envelope({ translation: "aov" }))]);

  await assert.rejects(
    api.search("kjv", "God", {}),
    (error) => error instanceof TypeError && /translation did not match/.test(error.message),
  );
});

test("the abbreviation is matched without regard to case in either form", () => {
  const upper = normalizeSearchPayload(
    envelope({ translation: "KJV" }),
    { translation: "kjv", query: "God", diacritics: "fold" },
  );
  const string = normalizeSearchPayload(
    envelope({ query: { translation: "KJV" } }),
    { translation: "kjv", query: "God", diacritics: "fold" },
  );

  assert.equal(upper.translation, "kjv");
  assert.equal(string.translation, "kjv");
  assert.equal(upper.items[0].selection_id, "gbd_kjv_043_0003_0016");
});

test("duplicate matches are refused", async () => {
  const { api } = harness([() => jsonResponse(envelope({
    matches: [
      { reference: "John 3:16", book_nr: 43, chapter: 3, verse: 16, terms: ["god"] },
      { reference: "John 3:16", book_nr: 43, chapter: 3, verse: 16, terms: ["god"] },
    ],
  }))]);

  await assert.rejects(
    api.search("kjv", "God", {}),
    (error) => error instanceof TypeError && /duplicate/.test(error.message),
  );
});

test("malformed envelopes are refused rather than repaired", () => {
  const options = { translation: "kjv", query: "God", diacritics: "fold" };
  const cases = [
    [null, /malformed/],
    [[], /malformed/],
    [{ query: {}, results: {}, matches: {} }, /malformed/],
    [envelope({ kind: "other" }), /kind/],
    [envelope({ query: { returned: 1 } }), /counts/],
    [envelope({ total: 1 }), /counts/],
    [envelope({ query: { has_more: "yes" } }), /pagination/],
    [envelope({ query: { offset: -1 } }), /pagination/],
    [envelope({ query: { limit: 101 } }), /pagination/],
    [envelope({ matches: [
      { reference: "John 3:18", book_nr: 43, chapter: 3, verse: 18, terms: [] },
    ] }), /did not resolve/],
    [envelope({ matches: [
      { reference: "John 3:16", book_nr: "43", chapter: 3, verse: 0 },
    ] }), /coordinates/],
    [envelope({ results: { aov_43_3: JOHN_3 } }), /malformed/],
    [envelope({ results: { kjv_43_4: JOHN_3 } }), /malformed/],
    [envelope({ results: { kjv_43_3: { ...JOHN_3, abbreviation: "aov" } } }), /another translation/],
    [envelope({ results: { kjv_43_3: { ...JOHN_3, verses: [
      { chapter: 3, verse: 16, name: "John 3:16", text: "" },
      { chapter: 3, verse: 17, name: "John 3:17", text: "x" },
    ] } } }), /verse is malformed/],
    [envelope({ results: { kjv_43_3: { ...JOHN_3, book_name: "" } } }), /malformed/],
  ];
  for (const [payload, pattern] of cases) {
    assert.throws(
      () => normalizeSearchPayload(payload, options),
      (error) => error instanceof TypeError && pattern.test(error.message),
      JSON.stringify(payload)?.slice(0, 120),
    );
  }
});

test("optional metadata is bounded without failing the search", () => {
  const options = { translation: "kjv", query: "God", diacritics: "fold" };

  const badSha = normalizeSearchPayload(
    envelope({ sha: "not-a-sha", query: { engine_version: "five" } }),
    options,
  );
  assert.equal(badSha.sha, null);
  assert.equal(badSha.engine_version, null);

  const upperSha = normalizeSearchPayload(envelope({ sha: SHA.toUpperCase() }), options);
  assert.equal(upperSha.sha, SHA);

  const noText = normalizeSearchPayload(envelope({ query: { text: 7 } }), options);
  assert.equal(noText.query_text, "God");

  const fallbackReference = normalizeSearchPayload(envelope({
    matches: [{ book_nr: 43, chapter: 3, verse: 16, terms: ["x".repeat(101), "god", "God", ""] }],
  }), options);
  assert.equal(fallbackReference.items[0].reference, "John 3:16");
  assert.deepEqual(fallbackReference.items[0].terms, ["god"]);
});
