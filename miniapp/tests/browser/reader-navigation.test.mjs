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
const bookmarkScope = createHash("sha256")
  .update("getbible.miniapp.bookmarks.v1\u000042", "utf8")
  .digest("hex");
const readingHistoryStorageKey =
  `getbible.miniapp.reading-history.v1:${bookmarkScope}`;
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
      safeAreaInset: { top: 0, right: 0, bottom: 18, left: 0 },
      contentSafeAreaInset: { top: 56, right: 0, bottom: 34, left: 0 },
      isFullscreen: false,
      BackButton: {
        onClick(handler) { window.__telegramState.backHandler = handler; },
        offClick(handler) {
          if (window.__telegramState.backHandler === handler) {
            window.__telegramState.backHandler = null;
          }
        },
        show() {},
        hide() {},
      },
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

async function ensureBottomNavigationExpanded(page) {
  // Reader jumps schedule their scroll and resulting collapse across animation
  // frames. Drain that work before inspecting the footer so the test cannot
  // race a visible button that is about to become intentionally hidden.
  await page.evaluate(() => new Promise((resolvePromise) => {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(resolvePromise);
    });
  }));
  const navigation = page.locator("#bottom-nav");
  if (await navigation.evaluate((element) => (
    element.classList.contains("is-collapsed")
  ))) {
    await page.locator("#bottom-nav-handle").click();
  }
  await page.waitForFunction(() => {
    const navigationElement = document.querySelector("#bottom-nav");
    const handle = document.querySelector("#bottom-nav-handle");
    const items = document.querySelector(".bottom-nav__items");
    return !navigationElement?.classList.contains("is-collapsed") &&
      handle?.getAttribute("aria-expanded") === "true" &&
      getComputedStyle(items).visibility === "visible" &&
      getComputedStyle(items).pointerEvents !== "none";
  });
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
  await page.addInitScript((storageKey) => {
    window.localStorage.setItem(
      storageKey,
      JSON.stringify({
        version: 1,
        items: [
          {
            id: "visit_seeded_aov",
            kind: "chapter",
            translation: "aov",
            reference: "John 4:16",
            book: 43,
            book_name: "John",
            chapter: 4,
            verse: 16,
            visited_at: 1,
          },
          ...Array.from({ length: 7 }, (_, index) => ({
            id: `visit_seeded_kjv_${index + 1}`,
            kind: "selection",
            translation: "kjv",
            reference: `John 3:${index + 2}`,
            book: 43,
            book_name: "John",
            chapter: 3,
            verse: index + 2,
            visited_at: index + 2,
          })),
          {
            id: "visit_seeded_aov_legacy_duplicate",
            kind: "selection",
            translation: "aov",
            reference: "John 4:16",
            book: 43,
            book_name: "John",
            chapter: 4,
            verse: 16,
            visited_at: 0,
          },
        ],
      }),
    );
  }, readingHistoryStorageKey);

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
  let bookmarkBackupRequest = null;

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
        user: { id: 42 },
        preferences,
        entrypoint: { route: "bible", query: "" },
        basket: { items: [], count: 0, maximum: 100 },
      }, 201);
    }
    if (apiPath === "cleanup") return fulfillJson(route, {});
    if (apiPath === "preferences") {
      const update = request.postDataJSON();
      preferences = {
        ...preferences,
        ...update,
        reader_location: update.reader_location ?? preferences.reader_location,
      };
      return fulfillJson(route, { preferences });
    }
    if (apiPath === "bookmarks/backup") {
      bookmarkBackupRequest = request.postDataJSON();
      return fulfillJson(route, {
        status: "backed_up",
        message_id: 77,
        idempotent_replay: false,
      });
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
  assert.equal(await page.locator("#bible-history-count").innerText(), "9");
  assert.equal(await page.locator("#bible-heading #bible-history").count(), 0);
  assert.equal(
    await page.locator("#bottom-nav #bible-history").isVisible(),
    true,
  );

  // Selecting in the reader reveals a compact menu trigger without opening it.
  // Multiple topic assignments remain browser-local first and outside the
  // five-item footer while the explicit chat backup alone calls Robot.
  await page.locator('[data-reader-verse="2"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#bookmark-popover")?.hidden &&
    document.querySelector('[data-bookmark-trigger="gbd_kjv_043_0003_0002"]')
      ?.textContent === "•••"
  ));
  await page.locator(
    '[data-bookmark-trigger="gbd_kjv_043_0003_0002"]',
  ).click();
  await page.waitForFunction(() => (
    !document.querySelector("#bookmark-popover")?.hidden
  ));
  assert.match(
    await page.locator('#bookmark-topic-picker option[value="biblical-love"]')
      .innerText(),
    /^● Biblical Love$/,
  );
  assert.equal(
    await page.locator('#bookmark-topic-picker option[value="biblical-love"]')
      .evaluate((option) => option.style.color),
    "rgb(161, 98, 7)",
  );
  await page.locator("#bookmark-topic-picker").selectOption("grace");
  await page.waitForFunction(() => (
    document.querySelector('#bible-verses [data-reader-verse="2"]')
      ?.closest(".reader-verse-row")?.dataset.bookmarkColor === "bbf7d0" &&
    !document.querySelector("#bookmark-popover")?.hidden &&
    document.querySelector('[data-bookmark-topic-open="grace"]')
  ));
  await page.locator("#bookmark-topic-picker").selectOption("biblical-love");
  await page.waitForFunction(() => (
    document.querySelectorAll("#bookmark-assigned-topics .bookmark-assigned-topic")
      .length === 2 &&
    !document.querySelector('#bookmark-topic-picker option[value="grace"]') &&
    !document.querySelector('#bookmark-topic-picker option[value="biblical-love"]')
  ));
  const storedBookmark = await page.evaluate(() => {
    const key = Object.keys(window.localStorage).find((candidate) =>
      candidate.startsWith("getbible.miniapp.bookmarks.v1:")
    );
    const record = key ? JSON.parse(window.localStorage.getItem(key)) : null;
    return record?.bookmarks?.[0] ?? null;
  });
  assert.deepEqual(storedBookmark?.topic_ids, ["grace", "biblical-love"]);
  assert.equal(storedBookmark?.chapter, 3);
  assert.equal(storedBookmark?.verse, 2);

  await page.locator('[data-bookmark-topic-open="grace"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bookmarks" &&
    !document.querySelector("#bookmark-detail")?.hidden &&
    !document.querySelector("#bookmark-back-to-verse")?.hidden
  ));
  await page.locator("#bookmark-back-to-verse").click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bible" &&
    document.querySelector('[data-reader-verse="2"]')
      ?.closest(".reader-verse-row")?.dataset.bookmarkColor === "bbf7d0" &&
    document.querySelector("#bookmark-popover")?.hidden &&
    document.activeElement?.matches('[data-reader-verse="2"]')
  ));

  await ensureBottomNavigationExpanded(page);
  await page.locator('[data-route="home"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "home"
  ));
  assert.equal(await page.locator(".home-actions .home-action").count(), 3);
  assert.equal(await page.locator("#home-bookmarks").isVisible(), true);
  assert.match(
    await page.locator("#home-bookmarks-meta").innerText(),
    /One personal verse/,
  );
  assert.equal(
    await page.locator("#translation-shortcut").evaluate(
      (button) => getComputedStyle(button).display,
    ),
    "none",
  );
  await page.locator('#home-bookmarks [data-home-route="bookmarks"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bookmarks" &&
    document.querySelectorAll(".bookmark-group-card").length === 61
  ));
  assert.equal(await page.locator("#bottom-nav").isVisible(), true);
  assert.equal(
    await page.locator("#translation-shortcut").evaluate(
      (button) => getComputedStyle(button).display,
    ),
    "none",
  );
  assert.equal(await page.locator(".bookmark-group-card").count(), 61);
  await page.locator(
    '.bookmark-group-card[data-bookmark-topic="grace"]',
  ).click();
  await page.waitForFunction(() => (
    !document.querySelector("#bookmark-detail")?.hidden &&
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === 1
  ));
  assert.match(await page.locator("#bookmark-list").innerText(), /John 3:2/);
  assert.equal(await page.locator("#load-topic-global-bookmarks").isVisible(), true);
  await page.locator("#load-topic-global-bookmarks").click();
  await page.waitForFunction(() => (
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === 53 &&
    document.querySelectorAll("#bookmark-list .bookmark-list__global-badge").length === 52
  ));
  assert.equal(
    await page.locator("#bookmark-list [data-bookmark-remove]").count(),
    53,
  );
  assert.equal(
    await page.locator('#bookmark-list [data-bookmark-open^="global_grace_"]').count(),
    52,
  );
  const hiddenGlobalId = await page.locator(
    '#bookmark-list [data-bookmark-open^="global_grace_"]',
  ).first().getAttribute("data-bookmark-open");
  await page.locator(
    `#bookmark-list [data-bookmark-open="${hiddenGlobalId}"] + [data-bookmark-remove]`,
  ).click();
  await page.waitForFunction((bookmarkId) => {
    const record = JSON.parse(
      window.localStorage.getItem("getbible.miniapp.global-bookmarks.v2"),
    );
    return document.querySelectorAll("#bookmark-list .bookmark-list__global-badge")
      .length === 51 && record.hidden_bookmark_ids.includes(bookmarkId);
  }, hiddenGlobalId);
  assert.match(
    await page.locator("#load-topic-global-bookmarks-label").innerText(),
    /Reload global verses/,
  );
  await page.locator("#load-topic-global-bookmarks").click();
  await page.waitForFunction((bookmarkId) => {
    const record = JSON.parse(
      window.localStorage.getItem("getbible.miniapp.global-bookmarks.v2"),
    );
    return document.querySelectorAll("#bookmark-list .bookmark-list__global-badge")
      .length === 52 && !record.hidden_bookmark_ids.includes(bookmarkId);
  }, hiddenGlobalId);
  assert.equal(await page.locator("#clear-topic-global-bookmarks").isVisible(), true);
  await page.locator("#clear-topic-global-bookmarks").click();
  await page.waitForFunction(() => (
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === 1 &&
    document.querySelectorAll("#bookmark-list .bookmark-list__global-badge").length === 0 &&
    document.querySelector("#bookmark-topic-global-status")?.textContent
      ?.includes("Personal bookmarks were kept")
  ));
  assert.equal(
    await page.evaluate(() => {
      const record = JSON.parse(
        window.localStorage.getItem("getbible.miniapp.global-bookmarks.v2"),
      );
      return record.enabled_topic_ids.includes("grace");
    }),
    false,
  );
  await page.locator("#bookmark-all-topics").click();
  await page.locator("#load-global-bookmarks").click();
  await page.waitForFunction(() => (
    document.querySelector("#global-bookmark-status")?.textContent
      ?.includes("2155 global verse links") &&
    document.querySelector("#global-bookmark-status")?.textContent
      ?.includes("61 topics")
  ));
  await page.locator(
    '.bookmark-group-card[data-bookmark-topic="grace"]',
  ).click();
  await page.waitForFunction(() => (
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === 53 &&
    document.querySelectorAll("#bookmark-list .bookmark-list__global-badge").length === 52
  ));
  await page.locator("#bookmark-all-topics").click();
  await page.locator("#backup-bookmarks").click();
  await page.waitForFunction(() => (
    document.querySelector("#bookmark-backup-status")?.textContent ===
      "Backup saved in this private bot chat."
  ));
  assert.equal(bookmarkBackupRequest?.backup?.version, 4);
  assert.equal(bookmarkBackupRequest?.backup?.markings?.length, 1);
  assert.deepEqual(
    bookmarkBackupRequest?.backup?.markings?.[0]?.colorIndexes,
    ["grace", "biblical-love"].map((topicId) =>
      bookmarkBackupRequest.backup.colors.findIndex((topic) => topic.id === topicId)
    ),
  );
  await page.locator(
    '.bookmark-group-card[data-bookmark-topic="spiritual-rebirth"]',
  ).click();
  await page.waitForFunction(() => (
    document.querySelectorAll("#bookmark-list .bookmark-list__global-badge").length === 44
  ));
  assert.equal(
    await page.locator(
      '[data-bookmark-open="global_spiritual-rebirth_43_3_3"]',
    ).getAttribute("aria-label"),
    "Open John 3:3 in King James Version (1769) (KJV). Global bookmark.",
  );
  await page.locator(
    '[data-bookmark-open="global_spiritual-rebirth_43_3_3"]',
  ).click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bible" &&
    document.querySelector("#bible-reference")?.textContent === "John 3"
  ));
  await page.waitForFunction((scope) => {
    const raw = window.localStorage.getItem(
      `getbible.miniapp.last-read.v1:${scope}`,
    );
    return raw && JSON.parse(raw).verse === 3;
  }, bookmarkScope);
  await page.evaluate(() => window.__telegramState.backHandler?.());
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bookmarks" &&
    document.querySelector("#bookmark-detail-title")?.textContent ===
      "Spiritual Rebirth" &&
    document.activeElement?.getAttribute("data-bookmark-open") ===
      "global_spiritual-rebirth_43_3_3"
  ));
  await page.locator('[data-route="bible"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bible" &&
    document.querySelector('#bible-verses [data-reader-verse="2"]')
  ));
  assert.notEqual(
    await page.locator("#translation-shortcut").evaluate(
      (button) => getComputedStyle(button).display,
    ),
    "none",
  );
  await page.locator(
    '[data-bookmark-trigger="gbd_kjv_043_0003_0002"]',
  ).click();
  await page.locator(
    '[data-bookmark-topic-open="biblical-love"] + [data-bookmark-assignment-remove]',
  ).click();
  await page.waitForFunction(() => {
    const key = Object.keys(window.localStorage).find((candidate) =>
      candidate.startsWith("getbible.miniapp.bookmarks.v1:")
    );
    const record = key ? JSON.parse(window.localStorage.getItem(key)) : null;
    return record?.bookmarks?.length === 1 &&
      record.bookmarks[0].topic_ids.length === 1 &&
      record.bookmarks[0].topic_ids[0] === "grace" &&
      document.querySelector('[data-bookmark-topic-open="grace"]');
  });
  await page.locator("#close-bookmark-popover").click();

  const historyCountBeforeNextChapter = Number(
    await page.locator("#bible-history-count").innerText(),
  );
  assert.equal(Number.isSafeInteger(historyCountBeforeNextChapter), true);
  await page.locator("#bible-next").click();
  await page.waitForFunction(() => (
    document.querySelector("#bible-reference")?.textContent === "John 4"
  ));
  assert.match(
    await page.locator('[data-reader-verse="1"]').innerText(),
    /KJV John 4 text 1/,
  );
  const historyCountAfterNextChapter = historyCountBeforeNextChapter + 1;
  await page.waitForFunction((expectedCount) => (
    document.querySelector("#bible-history-count")?.textContent ===
      String(expectedCount)
  ), historyCountAfterNextChapter);
  await waitForCondition(
    () => preferences.reader_location.chapter === 4,
    "reader preference did not reach John 4",
  );

  // Dispatch both actions in one browser task. The chapter navigation clears
  // the visible verse list before the queued selection finishes, so history
  // must use the canonical item returned by the selection store.
  const historyCountBeforeConcurrentActions = Number(
    await page.locator("#bible-history-count").innerText(),
  );
  await page.evaluate(() => {
    document.querySelector('[data-reader-verse="16"]')?.click();
    document.querySelector("#bible-previous")?.click();
  });
  const historyCountAfterConcurrentActions =
    historyCountBeforeConcurrentActions + 1;
  await page.waitForFunction((expectedCount) => (
    document.querySelector("#bible-reference")?.textContent === "John 3" &&
    document.querySelector("#bible-history-count")?.textContent ===
      String(expectedCount)
  ), historyCountAfterConcurrentActions);
  await waitForCondition(
    () => preferences.reader_location.chapter === 3,
    "reader preference did not return to John 3",
  );

  const bottomNavigation = page.locator("#bottom-nav");
  if (!await bottomNavigation.evaluate((nav) => nav.classList.contains("is-collapsed"))) {
    await page.locator("#bottom-nav-handle").click();
  }
  await page.waitForFunction(() => (
    document.querySelector("#bottom-nav")?.classList.contains("is-collapsed") &&
    document.querySelector("#bottom-nav-handle")?.getAttribute("aria-expanded") ===
      "false" &&
    getComputedStyle(document.querySelector(".bottom-nav__items")).pointerEvents ===
      "none"
  ));
  await page.locator("#bottom-nav-handle").click();
  await page.waitForFunction(() => (
    !document.querySelector("#bottom-nav")?.classList.contains("is-collapsed") &&
    document.querySelector("#bottom-nav-handle")?.getAttribute("aria-expanded") ===
      "true" &&
    getComputedStyle(document.querySelector("#bible-history")).visibility ===
      "visible"
  ));
  assert.equal(await page.locator("#bible-history").isVisible(), true);

  await page.setViewportSize({ width: 320, height: 844 });
  const robotRequestsBeforeHistoryUi = robotRequests.length;
  await page.locator("#bible-history").click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "history"
  ));
  assert.equal(await page.locator("#history-view").isVisible(), true);
  assert.equal(await bottomNavigation.isVisible(), true);
  assert.equal(await page.locator("#bible-history").isVisible(), true);
  assert.equal(
    await page.locator("#bible-history").getAttribute("aria-current"),
    "page",
  );
  assert.equal(await page.locator("#reading-history-dialog").count(), 0);
  assert.equal(await page.locator("#close-reading-history").count(), 0);
  assert.equal(
    await page.locator(".history-item").count(),
    historyCountAfterConcurrentActions,
  );
  const historyLayout = await page.evaluate(() => {
    const view = document.querySelector("#history-view");
    const heading = document.querySelector(".history-heading");
    const copy = heading.firstElementChild.getBoundingClientRect();
    const clear = document.querySelector("#clear-reading-history")
      .getBoundingClientRect();
    const topbar = document.querySelector(".topbar").getBoundingClientRect();
    const brand = document.querySelector(".brand").getBoundingClientRect();
    const navigation = document.querySelector("#bottom-nav")
      .getBoundingClientRect();
    return {
      usesSelectionHeading: heading.classList.contains("selection-heading"),
      contentSafeTop: window.Telegram.WebApp.contentSafeAreaInset.top,
      copyLeft: copy.left,
      copyRight: copy.right,
      clearLeft: clear.left,
      clearRight: clear.right,
      topbarCenter: topbar.left + topbar.width / 2,
      brandCenter: brand.left + brand.width / 2,
      brandVisible: getComputedStyle(document.querySelector(".brand__icon")).display !==
        "none",
      translationDisplay: getComputedStyle(
        document.querySelector("#translation-shortcut"),
      ).display,
      viewportWidth: window.innerWidth,
      viewTop: view.getBoundingClientRect().top,
      viewOverflow: getComputedStyle(view).overflowY,
      viewClientHeight: view.clientHeight,
      viewScrollHeight: view.scrollHeight,
      navigationTop: navigation.top,
      navigationBottom: navigation.bottom,
    };
  });
  assert.equal(historyLayout.usesSelectionHeading, true);
  assert.ok(historyLayout.copyLeft < historyLayout.clearLeft);
  assert.ok(historyLayout.copyRight <= historyLayout.clearLeft);
  assert.ok(historyLayout.clearRight <= historyLayout.viewportWidth);
  assert.ok(historyLayout.viewTop >= historyLayout.contentSafeTop);
  assert.ok(Math.abs(historyLayout.brandCenter - historyLayout.topbarCenter) < 1);
  assert.equal(historyLayout.brandVisible, true);
  assert.equal(historyLayout.translationDisplay, "none");
  assert.equal(historyLayout.viewOverflow, "auto");
  assert.ok(historyLayout.viewScrollHeight > historyLayout.viewClientHeight);
  assert.ok(historyLayout.navigationTop < historyLayout.navigationBottom);
  await page.locator("#bottom-nav-handle").click();
  await page.waitForFunction(() => (
    document.querySelector("#bottom-nav")?.classList.contains("is-collapsed")
  ));
  await page.locator("#history-view").evaluate((view) => new Promise((resolve) => {
    view.addEventListener("scroll", () => {
      window.requestAnimationFrame(resolve);
    }, { once: true });
    view.scrollTop = view.scrollHeight;
  }));
  await page.waitForFunction(() => {
    const navigation = document.querySelector("#bottom-nav");
    return !navigation?.classList.contains("is-collapsed");
  });
  await page.waitForFunction(() => {
    const navigation = document.querySelector("#bottom-nav");
    const view = document.querySelector("#history-view");
    return view?.scrollTop > 0 &&
      !navigation?.classList.contains("is-collapsed") &&
      getComputedStyle(document.querySelector(".bottom-nav__items")).visibility ===
        "visible" &&
      getComputedStyle(document.querySelector("#bible-history")).visibility ===
        "visible";
  });
  const scrolledHistoryLayout = await page.locator("#history-view")
    .evaluate((view) => {
      const navigation = document.querySelector("#bottom-nav")
        .getBoundingClientRect();
      return {
        maximumScrollTop: view.scrollHeight - view.clientHeight,
        navigationTop: navigation.top,
        scrollTop: view.scrollTop,
      };
    });
  assert.ok(scrolledHistoryLayout.scrollTop > 0);
  assert.ok(
    Math.abs(
      scrolledHistoryLayout.scrollTop - scrolledHistoryLayout.maximumScrollTop,
    ) < 1,
  );
  assert.equal(scrolledHistoryLayout.navigationTop, historyLayout.navigationTop);
  assert.equal(await page.locator(".brand__icon").isVisible(), true);
  const storedLocations = await page.evaluate((storageKey) => {
    const record = JSON.parse(
      window.localStorage.getItem(storageKey),
    );
    return record.items.map((item) => (
      `${item.translation}:${item.book}:${item.chapter}:${item.verse}`
    ));
  }, readingHistoryStorageKey);
  assert.equal(new Set(storedLocations).size, storedLocations.length);
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
  const historyCountAfterRemoval = historyCountAfterConcurrentActions - 1;
  assert.equal(
    await page.locator(".history-item").count(),
    historyCountAfterRemoval,
  );
  assert.equal(
    await page.locator("#bible-history-count").innerText(),
    String(historyCountAfterRemoval),
  );
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
  await page.waitForFunction((expectedCount) => (
    document.querySelector("#bible-history-count")?.textContent ===
      String(expectedCount)
  ), historyCountAfterRemoval);
  const promotedHistory = await page.evaluate((storageKey) => {
    const record = JSON.parse(
      window.localStorage.getItem(storageKey),
    );
    return {
      count: record.items.length,
      first: record.items[0],
      matching: record.items.filter((item) => (
        item.translation === "aov" &&
        item.book === 43 &&
        item.chapter === 4 &&
        item.verse === 16
      )).length,
    };
  }, readingHistoryStorageKey);
  assert.equal(promotedHistory.count, historyCountAfterRemoval);
  assert.equal(promotedHistory.matching, 1);
  assert.equal(promotedHistory.first.translation, "aov");
  assert.equal(promotedHistory.first.chapter, 4);
  assert.equal(promotedHistory.first.verse, 16);
  await waitForCondition(
    () => (
      preferences.translation === "aov" &&
      preferences.reader_location.translation === "aov" &&
      preferences.reader_location.chapter === 4 &&
      preferences.reader_location.verse === 16
    ),
    "reader preference did not restore the stored AOV location",
  );

  await ensureBottomNavigationExpanded(page);
  await page.locator("#bible-history").click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "history" &&
    document.querySelector("#history-view")?.scrollTop === 0
  ));
  const firstPromotedHistory = page.locator(".history-item").first();
  assert.equal(await firstPromotedHistory.isVisible(), true);
  assert.match(await firstPromotedHistory.innerText(), /John 4:16/);
  assert.match(await firstPromotedHistory.innerText(), /AOV/);
  const robotRequestsBeforeClear = robotRequests.length;
  await page.locator("#clear-reading-history").click();
  await page.waitForFunction(() => (
    document.querySelectorAll(".history-item").length === 0 &&
    !document.querySelector("#reading-history-empty")?.hidden
  ));
  assert.equal(
    await page.evaluate((storageKey) => (
      window.localStorage.getItem(storageKey)
    ), readingHistoryStorageKey),
    null,
  );
  assert.equal(robotRequests.length, robotRequestsBeforeClear);
  assert.equal(
    await page.evaluate(() => document.activeElement?.id),
    "empty-history-browse",
  );
  assert.equal(await page.locator("#clear-reading-history").isHidden(), true);
  assert.equal(await page.locator("#reading-history-empty").isVisible(), true);
  await page.locator("#empty-history-browse").click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bible" &&
    document.activeElement?.dataset.route === "bible"
  ));

  for (const route of ["home", "search", "selection"]) {
    await page.locator(`[data-route="${route}"]`).click();
    assert.equal(await page.locator("#bottom-nav").isVisible(), true);
    assert.equal(await page.locator("#bible-history").isVisible(), true);
    const translationDisplay = await page.locator("#translation-shortcut")
      .evaluate((button) => getComputedStyle(button).display);
    assert.equal(translationDisplay === "none", route !== "search");
  }

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
