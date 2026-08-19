import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const miniappRoot = resolve(fileURLToPath(new URL("../../", import.meta.url)));
const corsHeaders = { "access-control-allow-origin": "*" };
const mainApiPattern = /^https:\/\/api\.getbible\.net\/v2\/.+/;
const queryApiPattern = /^https:\/\/query\.getbible\.net\/v2\/.+/;
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

function jsonBody(payload) {
  return JSON.stringify(payload);
}

function sha1(value) {
  return createHash("sha1").update(value, "utf8").digest("hex");
}

function fulfillJson(route, payload, status = 200, cors = false) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers: cors ? corsHeaders : undefined,
    body: jsonBody(payload),
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

function chapterPayload(translation, chapter) {
  const label = translation.toUpperCase();
  return {
    abbreviation: translation,
    book_nr: 43,
    book_name: "John",
    chapter,
    name: `John ${chapter}`,
    testament: "new",
    verses: Array.from({ length: 40 }, (_, index) => ({
      verse: index + 1,
      text: `${label} John ${chapter} text ${index + 1}`,
    })),
  };
}

async function waitForCondition(predicate, message, timeoutMs = 5_000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) {
      assert.fail(message);
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 10));
  }
}

function installTelegramMock() {
  const events = new Map();
  const emit = (name, payload) => {
    for (const handler of events.get(name) ?? []) handler(payload);
  };
  window.__telegramState = { readyCalls: 0 };
  window.Telegram = {
    WebApp: {
      initData: "query_id=browser-test&user=%7B%22id%22%3A42%7D",
      initDataUnsafe: { start_param: "browser-test" },
      colorScheme: "dark",
      version: "8.0",
      viewportStableHeight: 844,
      safeAreaInset: { top: 24, right: 0, bottom: 18, left: 0 },
      contentSafeAreaInset: { top: 48, right: 0, bottom: 34, left: 0 },
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
      requestFullscreen() {
        this.isFullscreen = true;
        emit("fullscreenChanged");
      },
      enableVerticalSwipes() {},
      setHeaderColor() {},
      setBackgroundColor() {},
      setBottomBarColor() {},
      ready() { window.__telegramState.readyCalls += 1; },
      enableClosingConfirmation() {},
      disableClosingConfirmation() {},
      close() {},
    },
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

test("reader navigation uses direct GetBible API calls in a real browser", async (context) => {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  context.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(installTelegramMock);
  await page.addInitScript(() => {
    window.sessionStorage.setItem(
      "getbible.miniapp.reading-history",
      JSON.stringify({
        version: 1,
        items: [{
          id: "visit_seeded_aov",
          kind: "chapter",
          translation: "aov",
          reference: "John 4:16",
          book: 43,
          book_name: "John",
          chapter: 4,
          verse: 16,
          visited_at: 1,
        }],
      }),
    );
  });

  const consoleMessages = [];
  const pageErrors = [];
  const failedRequests = [];
  page.on("console", (message) => {
    if (["error", "warning"].includes(message.type())) {
      consoleMessages.push(`${message.type()}: ${message.text()}`);
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.url()}: ${request.failure()?.errorText ?? "failed"}`);
  });

  const publicRequests = [];
  const robotRequests = [];
  const chapterBodies = new Map();
  for (const translation of ["kjv", "aov"]) {
    for (const chapter of [3, 4]) {
      const body = jsonBody(chapterPayload(translation, chapter));
      chapterBodies.set(
        `${translation}:${chapter}`,
        { body, sha: sha1(body) },
      );
    }
  }

  await page.route("https://telegram.org/**", (route) => route.fulfill({
    status: 200,
    contentType: "application/javascript",
    body: "",
  }));

  await page.route(mainApiPattern, (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/v2\//, "");
    publicRequests.push(path);
    if (path === "translations.json") {
      return fulfillJson(route, [
        { abbreviation: "kjv", name: "King James Version (1769)", language: "English", lang: "en", direction: "ltr" },
        { abbreviation: "aov", name: "Afrikaanse Ou Vertaling", language: "Afrikaans", lang: "af", direction: "ltr" },
      ], 200, true);
    }
    if (/^(?:kjv|aov)\.sha$/.test(path)) {
      return fulfillPublicText(route, "1".repeat(40));
    }
    if (/^(?:kjv|aov)\/books\.json$/.test(path)) {
      return fulfillJson(route, bookNames.map((name, index) => ({
        nr: index + 1,
        name,
        testament: index < 39 ? "old" : "new",
      })), 200, true);
    }
    if (/^(?:kjv|aov)\/43\.sha$/.test(path)) {
      return fulfillPublicText(route, "2".repeat(40));
    }
    if (/^(?:kjv|aov)\/43\/chapters\.json$/.test(path)) {
      return fulfillJson(route, Array.from({ length: 21 }, (_, index) => ({
        chapter: index + 1,
        verses: Array.from({ length: 40 }, (_, verse) => verse + 1),
      })), 200, true);
    }
    const shaMatch = /^(kjv|aov)\/43\/(3|4)\.sha$/.exec(path);
    if (shaMatch) {
      return fulfillPublicText(
        route,
        chapterBodies.get(`${shaMatch[1]}:${shaMatch[2]}`).sha,
      );
    }
    const jsonMatch = /^(kjv|aov)\/43\/(3|4)\.json$/.exec(path);
    if (jsonMatch) {
      return fulfillPublicText(
        route,
        chapterBodies.get(`${jsonMatch[1]}:${jsonMatch[2]}`).body,
        "application/json",
      );
    }
    return fulfillJson(route, { error: "not found" }, 404, true);
  });

  await page.route(queryApiPattern, (route) => {
    publicRequests.push(new URL(route.request().url()).pathname);
    return fulfillJson(route, { error: "unexpected query request" }, 400, true);
  });

  let preferences = {
    translation: "kjv",
    search_defaults: {
      words: "all",
      match: "whole_word",
      scope: "bible",
      case_sensitive: false,
      diacritics: "exact",
      sort: "canonical",
    },
    reader_location: { translation: "kjv", book: 43, chapter: 3, verse: 1 },
  };

  await page.route("https://app.local/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const apiPath = url.pathname.split("/api/v1/")[1];
    if (!apiPath) return serveStatic(route);
    robotRequests.push(apiPath);
    assert.ok(
      !["translations", "books", "chapters", "scripture"].includes(apiPath),
      `public Bible data must not be proxied through Robot: ${apiPath}`,
    );
    if (apiPath === "session") {
      return fulfillJson(route, {
        session_token: "BrowserTestSessionToken123",
        expires_in: 10_800,
        preferences,
        entrypoint: { route: "bible", query: "" },
        basket: { items: [], count: 0, maximum: 100 },
      }, 201);
    }
    if (apiPath === "cleanup") return route.fulfill({ status: 204, body: "" });
    if (apiPath === "preferences") {
      const update = request.postDataJSON();
      preferences = {
        ...preferences,
        ...update,
        reader_location: update.reader_location ?? preferences.reader_location,
      };
      return fulfillJson(route, { preferences });
    }
    if (apiPath === "basket") {
      return fulfillJson(route, { items: [], count: 0, maximum: 100 });
    }
    return fulfillJson(route, { error: { code: "not_found" } }, 404);
  });

  await page.goto("https://app.local/miniapp/index.html?launch=browser-test", {
    waitUntil: "domcontentloaded",
  });
  try {
    await page.waitForSelector('#bible-verses [data-reader-verse="1"]', {
      timeout: 15_000,
    });
  } catch (error) {
    const accessMessage = await page.locator("#access-message").textContent().catch(() => null);
    throw new Error([
      error.message,
      `access: ${accessMessage ?? "none"}`,
      `public requests: ${publicRequests.join(", ")}`,
      `failed requests: ${failedRequests.join(" | ") || "none"}`,
      `console: ${consoleMessages.join(" | ") || "none"}`,
      `page errors: ${pageErrors.join(" | ") || "none"}`,
    ].join("\n"));
  }

  assert.equal(await page.locator("#access-denied").isHidden(), true);
  assert.equal(await page.locator("#bible-reference").innerText(), "John 3");
  assert.equal(await page.locator("#bible-verses [data-reader-verse]").count(), 40);
  assert.equal(await page.evaluate(() => window.__telegramState.readyCalls), 1);
  assert.equal(await page.locator("#bible-history-count").innerText(), "2");

  await page.locator("#bible-next").click();
  await page.waitForFunction(() => (
    document.querySelector("#bible-reference")?.textContent === "John 4"
  ));
  assert.match(
    await page.locator('[data-reader-verse="1"]').innerText(),
    /KJV John 4 text 1/,
  );
  await page.waitForFunction(() => (
    document.querySelector("#bible-history-count")?.textContent === "3"
  ));
  await waitForCondition(
    () => preferences.reader_location.chapter === 4,
    "reader preference did not reach John 4",
  );

  // Dispatch both actions in one browser task. The chapter navigation clears
  // the visible verse list before the queued selection finishes, so history
  // must use the canonical item returned by the selection store.
  await page.evaluate(() => {
    document.querySelector('[data-reader-verse="16"]')?.click();
    document.querySelector("#bible-previous")?.click();
  });
  await page.waitForFunction(() => (
    document.querySelector("#bible-reference")?.textContent === "John 3" &&
    document.querySelector("#bible-history-count")?.textContent === "5"
  ));
  await waitForCondition(
    () => preferences.reader_location.chapter === 3,
    "reader preference did not return to John 3",
  );

  const robotRequestsBeforeHistoryUi = robotRequests.length;
  await page.locator("#bible-history").click();
  assert.equal(
    await page.locator("#reading-history-dialog").getAttribute("open"),
    "",
  );
  assert.equal(
    await page.locator("#bible-history").getAttribute("aria-expanded"),
    "true",
  );
  assert.equal(await page.locator(".history-item").count(), 5);
  const selectedHistory = page.locator(".history-item")
    .filter({ has: page.getByText("John 4:16", { exact: true }) })
    .filter({
      has: page.getByText("King James Version (1769) (KJV)", {
        exact: true,
      }),
    });
  assert.equal(await selectedHistory.count(), 1);
  assert.match(await selectedHistory.innerText(), /King James Version \(1769\).*KJV/);
  assert.match(await selectedHistory.innerText(), /Verse selected/);

  await selectedHistory.locator("[data-history-remove]").click();
  assert.equal(await page.locator(".history-item").count(), 4);
  assert.equal(await page.locator("#bible-history-count").innerText(), "4");
  assert.equal(robotRequests.length, robotRequestsBeforeHistoryUi);

  const storedTranslationHistory = page.locator(".history-item")
    .filter({ has: page.getByText("John 4:16", { exact: true }) })
    .filter({
      has: page.getByText("Afrikaanse Ou Vertaling (AOV)", { exact: true }),
    });
  assert.equal(await storedTranslationHistory.count(), 1);
  await storedTranslationHistory
    .locator("[data-history-open]")
    .click();
  await page.waitForFunction(() => (
    document.querySelector("#bible-reference")?.textContent === "John 4" &&
    document.querySelector("#bible-translation-label")?.textContent
      ?.includes("AOV") &&
    document.activeElement?.getAttribute("data-reader-verse") === "16"
  ));
  assert.match(
    await page.locator('[data-reader-verse="16"]').innerText(),
    /AOV John 4 text 16/,
  );
  await page.waitForFunction(() => (
    document.querySelector("#bible-history-count")?.textContent === "5"
  ));
  await waitForCondition(
    () => (
      preferences.translation === "aov" &&
      preferences.reader_location.translation === "aov" &&
      preferences.reader_location.chapter === 4 &&
      preferences.reader_location.verse === 16
    ),
    "reader preference did not restore the stored AOV location",
  );

  await page.locator("#bible-history").click();
  const robotRequestsBeforeClear = robotRequests.length;
  await page.locator("#clear-reading-history").click();
  await page.waitForFunction(() => (
    document.querySelectorAll(".history-item").length === 0 &&
    !document.querySelector("#reading-history-empty")?.hidden
  ));
  assert.equal(
    await page.evaluate(() => (
      window.sessionStorage.getItem("getbible.miniapp.reading-history")
    )),
    null,
  );
  assert.equal(robotRequests.length, robotRequestsBeforeClear);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => (
    !document.querySelector("#reading-history-dialog")?.open
  ));
  assert.equal(
    await page.locator("#bible-history").getAttribute("aria-expanded"),
    "false",
  );
  assert.equal(
    await page.evaluate(() => document.activeElement?.id),
    "bible-history",
  );

  await page.locator('[data-route="home"]').click();
  assert.equal(await page.locator("#bible-history").isHidden(), true);

  for (const expected of [
    "translations.json",
    "kjv/books.json",
    "kjv/43/chapters.json",
    "kjv/43/3.json",
    "kjv/43/4.json",
    "aov/books.json",
    "aov/43/chapters.json",
    "aov/43/4.json",
  ]) {
    assert.ok(publicRequests.includes(expected), `missing public request: ${expected}`);
  }
  assert.deepEqual(
    robotRequests.filter((path) =>
      ["translations", "books", "chapters", "scripture"].includes(path)
    ),
    [],
  );
  assert.equal(robotRequests.some((path) => path.includes("history")), false);
  assert.deepEqual(consoleMessages, []);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(failedRequests, []);
});
