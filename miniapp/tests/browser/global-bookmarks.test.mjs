import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

import {
  bookmarksApiAssignmentCount,
  bookmarksApiTopicCount,
  bookmarksApiTopicVerseCount,
} from "../fixtures/bookmarks-api.mjs";
import { installBookmarksApiRoute } from "./support/bookmarks-api.mjs";

/**
 * The global catalogue's life in a real browser: it is read from the public
 * Bookmarks API and verified against the API's own checksum, kept in the
 * public cache between launches, stands in as "unavailable" until a
 * connection exists, and merges a personal verse link away only after its
 * global twin has stood beside it for a day on a network-verified pull.
 */
const miniappRoot = resolve(fileURLToPath(new URL("../../", import.meta.url)));
const corsHeaders = { "access-control-allow-origin": "*" };
const mainApiPattern = /^https:\/\/api\.getbible\.net\/v2\/.+/;
const queryApiPattern = /^https:\/\/query\.getbible\.net\/v2\/.+/;
const bookmarkScope = createHash("sha256")
  .update("getbible.miniapp.bookmarks.v1\u000042", "utf8")
  .digest("hex");
const globalBookmarkPreferencesMirrorKey =
  `getbible.miniapp.global-device.v1:${bookmarkScope}:preferences`;
const personalBookmarkKey = `getbible.miniapp.bookmarks.v1:${bookmarkScope}`;
const globalTopicCount = bookmarksApiTopicCount();
const globalAssignmentCount = bookmarksApiAssignmentCount();
const graceGlobalCount = bookmarksApiTopicVerseCount("grace");
const DAY_MS = 24 * 60 * 60 * 1_000;
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
  return route.fulfill({ status: 200, contentType, headers: corsHeaders, body });
}

function chapterPayload(translation, book, chapter) {
  const bookName = bookNames[book - 1];
  return {
    abbreviation: translation,
    book_nr: book,
    book_name: bookName,
    chapter,
    name: `${bookName} ${chapter}`,
    testament: book < 40 ? "old" : "new",
    verses: Array.from({ length: 40 }, (_, index) => ({
      verse: index + 1,
      text: `${bookName} ${chapter} text ${index + 1}`,
    })),
  };
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
      colorScheme: "light",
      version: "8.0",
      viewportStableHeight: 844,
      safeAreaInset: { top: 0, right: 0, bottom: 0, left: 0 },
      contentSafeAreaInset: { top: 0, right: 0, bottom: 0, left: 0 },
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

/**
 * A wall clock the test can move forward: the page's Date.now() reports
 * real time plus an offset the test sets, so "a day later" is one call. The
 * offset lives in localStorage so it survives the relaunches under test.
 */
function installAdjustableClock() {
  const realNow = Date.now;
  Date.now = () => realNow() + Number(
    window.localStorage.getItem("__testClockOffsetMs") ?? 0,
  );
}

function setClockOffset(page, offsetMs) {
  return page.evaluate((offset) => {
    window.localStorage.setItem("__testClockOffsetMs", String(offset));
  }, offsetMs);
}

async function launch(context) {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  context.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(installTelegramMock);
  await page.addInitScript(installAdjustableClock);
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
        verses: Array.from({ length: 40 }, (_, verse) => verse + 1),
      })), 200, true);
    }
    const shaMatch = /^kjv\/(\d{1,3})\/(\d{1,3})\.sha$/.exec(path);
    if (shaMatch) {
      const body = JSON.stringify(chapterPayload("kjv", Number(shaMatch[1]), Number(shaMatch[2])));
      return fulfillPublicText(route, sha1(body));
    }
    const jsonMatch = /^kjv\/(\d{1,3})\/(\d{1,3})\.json$/.exec(path);
    if (jsonMatch) {
      const body = JSON.stringify(chapterPayload("kjv", Number(jsonMatch[1]), Number(jsonMatch[2])));
      return fulfillPublicText(route, body, "application/json");
    }
    return fulfillJson(route, { error: "not found" }, 404, true);
  });
  await page.route(queryApiPattern, (route) =>
    fulfillJson(route, { error: "unexpected query request" }, 400, true));
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
          entrypoint: { route: "bible", query: "" },
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
  return { page, observed };
}

async function openReader(page) {
  await page.goto("https://app.local/miniapp/index.html?launch=browser-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector('#bible-verses [data-reader-verse="1"]', {
    timeout: 15_000,
  });
}

async function openBookmarksRoute(page) {
  await page.locator('[data-route="home"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "home"
  ));
  await page.locator('[data-home-route="bookmarks"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bookmarks"
  ));
}

async function bookmarkVerseUnderGrace(page, verse) {
  await page.locator('[data-route="bible"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bible"
  ));
  await page.locator(`[data-reader-verse="${verse}"]`).click();
  const trigger = `gbd_kjv_043_0003_${String(verse).padStart(4, "0")}`;
  await page.waitForFunction((id) => (
    document.querySelector(`[data-bookmark-trigger="${id}"]`)?.textContent === "•••"
  ), trigger);
  await page.locator(`[data-bookmark-trigger="${trigger}"]`).click();
  await page.waitForFunction(() => !document.querySelector("#bookmark-popover")?.hidden);
  await page.locator("#bookmark-topic-picker").selectOption("grace");
  await page.locator('#bookmark-assigned-topics [data-bookmark-topic="grace"]').waitFor();
  await page.locator("#close-bookmark-popover").click();
}

function personalBookmarks(page) {
  return page.evaluate((key) => {
    const record = JSON.parse(window.localStorage.getItem(key) ?? "null");
    return (record?.bookmarks ?? []).map((bookmark) => ({
      chapter: bookmark.chapter,
      verse: bookmark.verse,
      topic_ids: bookmark.topic_ids,
    }));
  }, personalBookmarkKey);
}

function preferenceRecord(page) {
  return page.evaluate((key) => {
    const envelope = JSON.parse(window.localStorage.getItem(key) ?? "null");
    return envelope ? JSON.parse(envelope.value) : null;
  }, globalBookmarkPreferencesMirrorKey);
}

async function clickAddAll(page) {
  const before = await page.locator("#global-bookmark-status").innerText();
  await page.locator("#load-global-bookmarks").click();
  await page.waitForFunction((previous) => {
    const status = document.querySelector("#global-bookmark-status")?.textContent ?? "";
    return status !== previous && /global verse links/.test(status) &&
      document.querySelector("#load-global-bookmarks")?.getAttribute("aria-busy") === "false";
  }, before);
  return page.locator("#global-bookmark-status").innerText();
}

test("the catalogue is verified from the Bookmarks API, seeds the default topics, and is cached for a day", async (context) => {
  const { page, observed } = await launch(context);
  const bookmarksApi = await installBookmarksApiRoute(page);

  await openReader(page);
  await openBookmarksRoute(page);
  await page.waitForFunction((expected) => (
    document.querySelectorAll(".bookmark-group-card").length === expected
  ), globalTopicCount);
  assert.deepEqual(bookmarksApi.requests, ["index.json", "all.json"]);
  assert.equal(await page.locator("#global-bookmark-status").innerText(), "");
  const seeded = await preferenceRecord(page);
  assert.equal(seeded.version, 4);
  assert.equal(seeded.seeded_catalog_version, 3);
  assert.deepEqual(seeded.coverage, {});
  assert.equal(
    await page.locator('.bookmark-group-card[data-bookmark-topic="grace"] strong').innerText(),
    "Grace",
  );

  // A relaunch within the day serves the verified copy without a request; a
  // relaunch a day later asks the index whether anything changed, and only
  // the index, because the checksum still matches.
  await openReader(page);
  await openBookmarksRoute(page);
  await page.waitForFunction((expected) => (
    document.querySelectorAll(".bookmark-group-card").length === expected
  ), globalTopicCount);
  assert.deepEqual(bookmarksApi.requests, ["index.json", "all.json"]);
  await setClockOffset(page, DAY_MS + 1);
  await openReader(page);
  await page.waitForFunction(() => (
    window.__getbibleBoot?.settled === true
  ));
  assert.deepEqual(bookmarksApi.requests, ["index.json", "all.json", "index.json"]);
  assert.deepEqual(observed.failedRequests, []);
  assert.deepEqual(observed.pageErrors, []);
  assert.deepEqual(observed.consoleMessages, []);
});

test("Add all merges a personal verse link only after its global twin has stood beside it for a day", async (context) => {
  const { page, observed } = await launch(context);
  const bookmarksApi = await installBookmarksApiRoute(page);

  await openReader(page);
  await bookmarkVerseUnderGrace(page, 16);
  await bookmarkVerseUnderGrace(page, 2);
  assert.deepEqual(await personalBookmarks(page), [
    { chapter: 3, verse: 16, topic_ids: ["grace"] },
    { chapter: 3, verse: 2, topic_ids: ["grace"] },
  ]);
  await openBookmarksRoute(page);
  await page.waitForFunction((expected) => (
    document.querySelectorAll(".bookmark-group-card").length === expected
  ), globalTopicCount);

  // The first pull enables every topic and starts the clock on John 3:16,
  // which the grace topic also carries; John 3:2 is the reader's alone.
  const first = await clickAddAll(page);
  assert.match(
    first,
    new RegExp(`Loaded ${globalAssignmentCount} global verse links across ${globalTopicCount} topics`),
  );
  assert.doesNotMatch(first, /covered by the global catalogue/);
  assert.deepEqual(bookmarksApi.requests, ["index.json", "all.json", "index.json"]);
  const afterFirst = await preferenceRecord(page);
  assert.deepEqual(Object.keys(afterFirst.coverage), ["43:3:16"]);
  assert.equal(afterFirst.enabled_topic_ids.length, globalTopicCount);
  assert.equal((await personalBookmarks(page)).length, 2);

  // Pulling again the same day changes nothing.
  const sameDay = await clickAddAll(page);
  assert.match(sameDay, /global verse links are active/);
  assert.doesNotMatch(sameDay, /covered by the global catalogue/);
  assert.equal((await personalBookmarks(page)).length, 2);

  // A day later the personal copy of John 3:16 is merged into the global row.
  await setClockOffset(page, DAY_MS + 60_000);
  const merged = await clickAddAll(page);
  assert.match(merged, /One personal bookmark is now covered by the global catalogue\./);
  assert.deepEqual(await personalBookmarks(page), [
    { chapter: 3, verse: 2, topic_ids: ["grace"] },
  ]);
  const afterMerge = await preferenceRecord(page);
  assert.deepEqual(afterMerge.coverage, {});
  assert.deepEqual(
    bookmarksApi.requests,
    ["index.json", "all.json", "index.json", "index.json", "index.json"],
  );

  await page.locator('.bookmark-group-card[data-bookmark-topic="grace"]').click();
  await page.waitForFunction((expected) => (
    !document.querySelector("#bookmark-detail")?.hidden &&
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === expected
  ), graceGlobalCount + 1);
  assert.equal(
    await page.locator(
      '#bookmark-list [data-bookmark-open="global_grace_43_3_16"] .bookmark-list__global-badge',
    ).count(),
    1,
  );
  assert.equal(
    await page.locator("#bookmark-list .bookmark-list__global-badge").count(),
    graceGlobalCount,
  );
  assert.deepEqual(observed.failedRequests, []);
  assert.deepEqual(observed.pageErrors, []);
  assert.deepEqual(observed.consoleMessages, []);
});

test("without the Bookmarks API the surface says global topics wait for a connection, and coming online loads them", async (context) => {
  const { page, observed } = await launch(context);
  const bookmarksApi = await installBookmarksApiRoute(page, { unavailable: true });

  await openReader(page);
  await openBookmarksRoute(page);
  await page.waitForFunction(() => (
    /unavailable until you are online/.test(
      document.querySelector("#global-bookmark-status")?.textContent ?? "",
    )
  ));
  assert.equal(await page.locator(".bookmark-group-card").count(), 0);
  assert.equal(await preferenceRecord(page), null);
  assert.ok(bookmarksApi.requests.length >= 1);
  assert.ok(bookmarksApi.requests.every((path) => path === "index.json"));
  const degraded = await page.evaluate(() => window.__getbibleBoot.degraded ?? []);
  assert.deepEqual(degraded, ["global catalogue"]);

  // An explicit pull while still offline reports the same state.
  await page.locator("#load-global-bookmarks").click();
  await page.waitForFunction(() => (
    document.querySelector("#load-global-bookmarks")?.getAttribute("aria-busy") === "false" &&
    /unavailable until you are online/.test(
      document.querySelector("#global-bookmark-status")?.textContent ?? "",
    )
  ));
  assert.equal(await page.locator(".bookmark-group-card").count(), 0);

  // The connection returns: the catalogue loads, the defaults are seeded and
  // the notice clears without a reload.
  bookmarksApi.unavailable = false;
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await page.waitForFunction((expected) => (
    document.querySelectorAll(".bookmark-group-card").length === expected &&
    document.querySelector("#global-bookmark-status")?.textContent === ""
  ), globalTopicCount);
  assert.equal(
    bookmarksApi.requests.filter((path) => path === "all.json").length,
    1,
  );
  assert.equal((await preferenceRecord(page)).seeded_catalog_version, 3);
  assert.deepEqual(observed.failedRequests, []);
  assert.deepEqual(observed.pageErrors, []);
});
