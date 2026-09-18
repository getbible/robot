/**
 * A real-browser harness for the Search page: the Mini App served from its
 * files, a Telegram mock, the public Main and Bookmarks APIs stubbed, and a
 * Search API for the word "text" that serves any number of matches page by
 * page, can fail one offset with a problem document until told otherwise,
 * and can publish a new corpus `sha` between pages.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

import { installBookmarksApiRoute } from "./bookmarks-api.mjs";

const miniappRoot = resolve(fileURLToPath(new URL("../../../", import.meta.url)));
const corsHeaders = { "access-control-allow-origin": "*" };
const mainApiPattern = /^https:\/\/api\.getbible\.net\/v2\/.+/;
const queryApiPattern = /^https:\/\/query\.getbible\.net\/v2\/.+/;
const searchApiPattern = /^https:\/\/search\.getbible\.net\/v2\/.+/;
export const PAGE_SIZE = 50;
export const OFFSET_CEILING = 10_000;
const VERSES_PER_CHAPTER = 40;
export const FIRST_SHA = "5".repeat(40);
export const SECOND_SHA = "6".repeat(40);
const bookNames = [
  "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua",
  "Judges", "Ruth", "1 Samuel", "2 Samuel", "1 Kings", "2 Kings",
  "1 Chronicles", "2 Chronicles", "Ezra", "Nehemiah", "Esther", "Job",
  "Psalms", "Proverbs", "Ecclesiastes", "Song of Solomon", "Isaiah",
  "Jeremiah", "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel", "Amos",
  "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk", "Zephaniah", "Haggai",
  "Zechariah", "Malachi", "Matthew", "Mark", "Luke", "John", "Acts", "Romans",
  "1 Corinthians", "2 Corinthians", "Galatians", "Ephesians", "Philippians",
  "Colossians", "1 Thessalonians", "2 Thessalonians", "1 Timothy", "2 Timothy",
  "Titus", "Philemon", "Hebrews", "James", "1 Peter", "2 Peter", "1 John",
  "2 John", "3 John", "Jude", "Revelation",
];

function sha1(value) {
  return createHash("sha1").update(value, "utf8").digest("hex");
}

function fulfillJson(route, payload, status = 200, cors = false) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers: cors ? corsHeaders : undefined,
    body: JSON.stringify(payload),
  });
}

function fulfillPublicText(route, body, contentType = "text/plain") {
  return route.fulfill({
    status: 200,
    contentType,
    headers: corsHeaders,
    body,
  });
}

function chapterPayload(book, chapter) {
  const bookName = bookNames[book - 1];
  return {
    abbreviation: "kjv",
    book_nr: book,
    book_name: bookName,
    chapter,
    name: `${bookName} ${chapter}`,
    testament: book < 40 ? "old" : "new",
    verses: Array.from({ length: VERSES_PER_CHAPTER }, (_, index) => ({
      verse: index + 1,
      text: `KJV ${bookName} ${chapter} text ${index + 1}`,
    })),
  };
}

/**
 * Match number `index` (from zero) of a search for the word "text": verses
 * of John counted straight through from chapter 3, forty to a chapter, so
 * any number of matches has a coordinate, every page is cut from the same
 * sequence, and a result opened in the reader has a previous chapter in
 * the same stubbed book.
 */
const FIRST_MATCH_CHAPTER = 3;

function matchCoordinate(index) {
  return {
    chapter: FIRST_MATCH_CHAPTER + Math.floor(index / VERSES_PER_CHAPTER),
    verse: 1 + (index % VERSES_PER_CHAPTER),
  };
}

export function selectionId(index) {
  const { chapter, verse } = matchCoordinate(index);
  return `gbd_kjv_043_${String(chapter).padStart(4, "0")}_${String(verse).padStart(4, "0")}`;
}

/**
 * One Search API v2 page of a search with `total` matches: the matches from
 * `offset`, at most `limit` of them, in the documented envelope with the
 * exact total, the page's `returned`, `has_more`, and the corpus `sha`.
 */
function searchPage({ total, offset, limit, sha }) {
  const translation = {
    translation: "KJV",
    abbreviation: "kjv",
    lang: "en",
    language: "English",
    direction: "LTR",
    encoding: "UTF-8",
  };
  const results = {};
  const matches = [];
  const end = Math.min(offset + limit, total);
  for (let index = offset; index < end; index += 1) {
    const { chapter, verse } = matchCoordinate(index);
    const key = `kjv_43_${chapter}`;
    results[key] ??= {
      ...translation,
      book_nr: 43,
      book_name: "John",
      chapter,
      name: `John ${chapter}`,
      ref: [],
      verses: [],
    };
    const name = `John ${chapter}:${verse}`;
    results[key].ref.push(name);
    results[key].verses.push({ chapter, verse, name, text: `KJV John ${chapter} text ${verse}` });
    matches.push({
      reference: name,
      book_nr: 43,
      chapter,
      verse,
      score: 1,
      occurrences: 1,
      terms: ["text"],
    });
  }
  return {
    query: {
      text: "text",
      kind: "search",
      translation,
      engine_version: 5,
      total,
      returned: matches.length,
      sha,
      offset,
      limit,
      has_more: end < total,
    },
    results,
    matches,
  };
}

/**
 * Serves the Search API for the word "text". `state.failure` makes one
 * offset answer with a problem document until it is cleared; `state.sha`
 * can be changed between pages to publish a new corpus, and `state.shaFor`
 * can name the corpus per offset to mimic a cache that still holds a page
 * of the old one.
 */
async function installSearchApi(page, { total }) {
  const state = { total, sha: FIRST_SHA, shaFor: null, failure: null, requests: [] };
  await page.route(searchApiPattern, (route) => {
    const url = new URL(route.request().url());
    state.requests.push(url);
    assert.equal(route.request().headers().authorization, undefined);
    const offset = Number(url.searchParams.get("offset"));
    const limit = Number(url.searchParams.get("limit"));
    const failure = state.failure;
    if (failure && failure.offset === offset) {
      return route.fulfill({
        status: failure.status,
        contentType: "application/problem+json",
        headers: {
          ...corsHeaders,
          "access-control-expose-headers": "Retry-After",
          ...(failure.retryAfter ? { "retry-after": String(failure.retryAfter) } : {}),
        },
        body: JSON.stringify({
          type: "about:blank",
          title: failure.title,
          status: failure.status,
          code: failure.code,
          detail: failure.detail,
          instance: url.pathname,
          ...(failure.retryAfter ? { retry_after: failure.retryAfter } : {}),
        }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: corsHeaders,
      body: JSON.stringify(searchPage({
        total: state.total,
        offset,
        limit,
        sha: state.shaFor ? state.shaFor(offset) : state.sha,
      })),
    });
  });
  return state;
}

async function serveStatic(route) {
  const url = new URL(route.request().url());
  const relative = url.pathname.replace(/^\/miniapp\/?/, "") || "index.html";
  const file = resolve(miniappRoot, relative);
  assert.ok(file.startsWith(`${miniappRoot}/`));
  const mime = {
    ".css": "text/css",
    ".html": "text/html",
    ".js": "application/javascript",
    ".png": "image/png",
    ".webp": "image/webp",
  }[extname(file)] ?? "application/octet-stream";
  return route.fulfill({ status: 200, contentType: mime, body: await readFile(file) });
}

function installTelegramMock() {
  const events = new Map();
  window.Telegram = {
    WebApp: {
      initData: "query_id=browser-test&user=%7B%22id%22%3A42%7D",
      initDataUnsafe: { start_param: "browser-test" },
      colorScheme: "dark",
      version: "8.0",
      viewportStableHeight: window.innerHeight,
      safeAreaInset: { top: 0, right: 0, bottom: 18, left: 0 },
      contentSafeAreaInset: { top: 56, right: 0, bottom: 34, left: 0 },
      isFullscreen: false,
      BackButton: { onClick() {}, offClick() {}, show() {}, hide() {} },
      HapticFeedback: { selectionChanged() {}, notificationOccurred() {} },
      showConfirm(_message, callback) { callback(true); },
      onEvent(name, handler) {
        const handlers = events.get(name) ?? new Set();
        handlers.add(handler);
        events.set(name, handlers);
      },
      offEvent(name, handler) { events.get(name)?.delete(handler); },
      isVersionAtLeast(version) { return version === "8.0"; },
      expand() {},
      requestFullscreen() {},
      enableVerticalSwipes() {},
      setHeaderColor() {},
      setBackgroundColor() {},
      setBottomBarColor() {},
      ready() {},
      enableClosingConfirmation() {},
      disableClosingConfirmation() {},
      close() {},
    },
  };
}

function removeIntersectionObserver() {
  Object.defineProperty(window, "IntersectionObserver", {
    configurable: true,
    writable: true,
    value: undefined,
  });
}

export async function launch(context, {
  total,
  viewport = { width: 390, height: 844 },
  intersectionObserver = true,
} = {}) {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  context.after(() => browser.close());
  const page = await browser.newPage({ viewport });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(installTelegramMock);
  if (!intersectionObserver) {
    await page.addInitScript(removeIntersectionObserver);
  }
  const observed = { consoleMessages: [], failedRequests: [], pageErrors: [] };
  page.on("console", (message) => {
    if (["error", "warning"].includes(message.type())) {
      observed.consoleMessages.push(`${message.type()}: ${message.text()}`);
    }
  });
  page.on("pageerror", (error) => observed.pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    observed.failedRequests.push(
      `${request.url()}: ${request.failure()?.errorText ?? "failed"}`,
    );
  });
  await page.route("https://telegram.org/**", (route) => route.fulfill({
    status: 200,
    contentType: "application/javascript",
    body: "",
  }));
  await page.route(mainApiPattern, (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/v2\//, "");
    if (path === "translations.json") {
      return fulfillJson(route, [
        { abbreviation: "kjv", name: "King James Version (1769)", language: "English", lang: "en", direction: "ltr" },
      ], 200, true);
    }
    if (path === "kjv.sha") return fulfillPublicText(route, "1".repeat(40));
    if (path === "kjv/books.json") {
      return fulfillJson(route, bookNames.map((name, index) => ({
        nr: index + 1,
        name,
        testament: index < 39 ? "old" : "new",
      })), 200, true);
    }
    if (path === "kjv/43.sha") return fulfillPublicText(route, "2".repeat(40));
    if (path === "kjv/43/chapters.json") {
      return fulfillJson(route, Array.from({ length: 21 }, (_, index) => ({
        chapter: index + 1,
        verses: Array.from({ length: VERSES_PER_CHAPTER }, (_, verse) => verse + 1),
      })), 200, true);
    }
    const shaMatch = /^kjv\/(\d{1,3})\/(\d{1,3})\.sha$/.exec(path);
    if (shaMatch) {
      // The reader holds a chapter against its published SHA-1, so the
      // digest has to be the one of the body served below.
      const body = JSON.stringify(chapterPayload(Number(shaMatch[1]), Number(shaMatch[2])));
      return fulfillPublicText(route, sha1(body));
    }
    const jsonMatch = /^kjv\/(\d{1,3})\/(\d{1,3})\.json$/.exec(path);
    if (jsonMatch) {
      const body = JSON.stringify(chapterPayload(Number(jsonMatch[1]), Number(jsonMatch[2])));
      return fulfillPublicText(route, body, "application/json");
    }
    return fulfillJson(route, { error: "not found" }, 404, true);
  });
  await page.route(queryApiPattern, (route) =>
    fulfillJson(route, { error: "unexpected query request" }, 400, true));
  await installBookmarksApiRoute(page);
  const search = await installSearchApi(page, { total });
  await page.route("https://app.local/**", async (route) => {
    const url = new URL(route.request().url());
    const apiPath = url.pathname.split("/api/v1/")[1];
    if (!apiPath) return serveStatic(route);
    if (apiPath === "session") {
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          session_token: "BrowserTestSessionToken123",
          expires_in: 10_800,
          user: { id: 42 },
          preferences: {
            translation: "kjv",
            search_defaults: {},
            reader_location: { translation: "kjv", book: 43, chapter: 3, verse: 1 },
          },
          entrypoint: { route: "search", query: "" },
          basket: { items: [], count: 0, maximum: 100 },
          contributions: {
            enabled: true,
            state: "not_applied",
            can_contribute: false,
            disclosure_required: false,
          },
          translations: [
            { code: "kjv", name: "King James Version (1769)", language: "English", lang: "en", direction: "ltr" },
          ],
        }),
      });
    }
    if (apiPath === "cleanup") return fulfillJson(route, {});
    if (apiPath === "preferences") {
      return fulfillJson(route, { preferences: route.request().postDataJSON() });
    }
    return fulfillJson(route, { error: { code: "not_found" } }, 404);
  });
  return { page, observed, search };
}

export async function openSearchView(page) {
  await page.goto("https://app.local/miniapp/index.html?launch=browser-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "search" &&
    document.querySelector("#app")?.hidden === false
  ));
}

export async function openSearch(page, query = "text") {
  await openSearchView(page);
  await page.locator("#search-query").fill(query);
  await page.locator("#search-query").press("Enter");
}

/** Wait on this side for the Search API stub to have seen `count` requests. */
export async function waitForRequests(search, count, timeoutMs = 10_000) {
  const started = Date.now();
  while (search.requests.length < count) {
    if (Date.now() - started > timeoutMs) {
      throw new Error(`waited for ${count} search requests, saw ${search.requests.length}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

export function cardCount(page) {
  return page.evaluate(() =>
    document.querySelectorAll("#search-results .verse-result").length);
}

export function waitForCards(page, count) {
  return page.waitForFunction((expected) => (
    document.querySelectorAll("#search-results .verse-result").length >= expected
  ), count);
}

export function waitForReach(page, reach) {
  return page.waitForFunction((expected) => (
    document.querySelector("#search-more")?.dataset.reach === expected
  ), reach);
}

export function scrollToFoot(page) {
  return page.evaluate(() => {
    const view = document.querySelector("#search-view");
    view.scrollTop = view.scrollHeight;
  });
}

/** Two frames and a beat, enough for any pending request to have been sent. */
export async function settle(page) {
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(resolve, 120)));
  }));
}

export function footText(page) {
  return page.locator("#search-more").innerText();
}

export function offsets(search) {
  return search.requests.map((url) => Number(url.searchParams.get("offset")));
}

export function expectedOffsets(total) {
  const list = [];
  for (let offset = 0; offset < total && offset <= OFFSET_CEILING; offset += PAGE_SIZE) {
    list.push(offset);
  }
  return list;
}

export function selectionIds(page) {
  return page.evaluate(() => [...document.querySelectorAll(
    "#search-results [data-selection-id]",
  )].map((card) => card.dataset.selectionId));
}

/**
 * Scroll to the foot of the list again and again, as a reader would, until
 * the foot stops asking for more. Each visit must bring the next page before
 * the next visit; the whole walk runs inside the page so a long walk costs
 * only what the browser itself spends. Returns the final `reach`, the number
 * of verses on screen, and the count after each visit.
 */
export function scrollUntilSettled(page, { total, maxPages = 400 }) {
  return page.evaluate(async ({ pageSize, expectedTotal, limit }) => {
    const view = document.querySelector("#search-view");
    const foot = document.querySelector("#search-more");
    const count = () =>
      document.querySelectorAll("#search-results .verse-result").length;
    const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
    const ended = () => ["complete", "ceiling", "failed", "stalled"]
      .includes(foot.dataset.reach);
    const trace = [];
    for (let visits = 0; visits < limit; visits += 1) {
      if (ended()) {
        return { reach: foot.dataset.reach, loaded: count(), trace };
      }
      const before = count();
      const started = performance.now();
      view.scrollTop = view.scrollHeight;
      const wanted = Math.min(expectedTotal, before + pageSize);
      while (count() < wanted && !ended()) {
        if (performance.now() - started > 30_000) {
          return { reach: "timeout", loaded: count(), trace };
        }
        await frame();
      }
      trace.push({ loaded: count(), ms: Math.round(performance.now() - started) });
    }
    return { reach: "exhausted", loaded: count(), trace };
  }, { pageSize: PAGE_SIZE, expectedTotal: total, limit: maxPages });
}

