import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";
import { CORE_BOOKMARK_TOPIC_DEFINITIONS } from "../../lib/bookmark-topic-definitions.js";
import { GLOBAL_BOOKMARK_DATA } from "../../lib/global-bookmark-data.js";

const miniappRoot = resolve(fileURLToPath(new URL("../../", import.meta.url)));
const corsHeaders = { "access-control-allow-origin": "*" };
const mainApiPattern = /^https:\/\/api\.getbible\.net\/v2\/.+/;
const queryApiPattern = /^https:\/\/query\.getbible\.net\/v2\/.+/;
const bookmarkScope = createHash("sha256")
  .update("getbible.miniapp.bookmarks.v1\u000042", "utf8")
  .digest("hex");
const globalBookmarkPreferencesMirrorKey =
  `getbible.miniapp.global-device.v1:${bookmarkScope}:preferences`;
const readingHistoryStorageKey =
  `getbible.miniapp.reading-history.v1:${bookmarkScope}`;
const globalTopicCount = CORE_BOOKMARK_TOPIC_DEFINITIONS.length;
const globalAssignmentCount = Object.values(GLOBAL_BOOKMARK_DATA.bookmarks_by_topic)
  .reduce((total, coordinates) => total + coordinates.length, 0);
const graceGlobalCount = GLOBAL_BOOKMARK_DATA.bookmarks_by_topic.grace.length;
const spiritualRebirthGlobalCount =
  GLOBAL_BOOKMARK_DATA.bookmarks_by_topic["spiritual-rebirth"].length;
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

function chapterPayload(translation, book, chapter) {
  const label = translation.toUpperCase();
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
      text: `${label} ${bookName} ${chapter} text ${index + 1}`,
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

async function assertBottomNavigationIconsMatch(page) {
  await ensureBottomNavigationExpanded(page);
  const icons = await page.locator("#bottom-nav .bottom-nav__icon")
    .evaluateAll((elements) => elements.map((element) => {
      const bounds = element.getBoundingClientRect();
      return { height: bounds.height, width: bounds.width };
    }));
  assert.equal(icons.length, 5);
  for (const icon of icons) {
    assert.ok(Math.abs(icon.width - icon.height) < 0.5);
    assert.ok(Math.abs(icon.width - icons[0].width) < 0.5);
    assert.ok(Math.abs(icon.height - icons[0].height) < 0.5);
  }
}

async function assertBookmarkPopoverControlsDoNotOverlap(page) {
  await page.evaluate(() => new Promise((resolvePromise) => {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(resolvePromise);
    });
  }));
  const layout = await page.evaluate(() => {
    const popover = document.querySelector("#bookmark-popover");
    const close = document.querySelector("#close-bookmark-popover");
    const removals = [
      ...document.querySelectorAll("[data-bookmark-assignment-remove]"),
    ];
    const rect = (element) => {
      const bounds = element.getBoundingClientRect();
      return {
        bottom: bounds.bottom,
        left: bounds.left,
        right: bounds.right,
        top: bounds.top,
      };
    };
    const overlaps = (first, second) => (
      first.left < second.right &&
      first.right > second.left &&
      first.top < second.bottom &&
      first.bottom > second.top
    );
    const popoverRect = rect(popover);
    const closeRect = rect(close);
    return {
      closeInsidePopover:
        closeRect.left >= popoverRect.left &&
        closeRect.right <= popoverRect.right &&
        closeRect.top >= popoverRect.top &&
        closeRect.bottom <= popoverRect.bottom,
      overlapCount: removals
        .map(rect)
        .filter((removeRect) => overlaps(closeRect, removeRect))
        .length,
      popoverHidden: popover.hidden,
      removalCount: removals.length,
    };
  });

  assert.equal(layout.popoverHidden, false);
  assert.ok(layout.removalCount > 0);
  assert.equal(layout.closeInsidePopover, true);
  assert.equal(layout.overlapCount, 0);
}

async function assertReaderStartsBelowToolbar(page) {
  await page.evaluate(() => new Promise((resolvePromise) => {
    window.requestAnimationFrame(() => window.requestAnimationFrame(resolvePromise));
  }));
  const layout = await page.evaluate(() => {
    const chip = document.querySelector("#translation-shortcut")
      .getBoundingClientRect();
    const toolbarElement = document.querySelector("#bible-heading");
    const toolbar = toolbarElement.getBoundingClientRect();
    const firstVerse = document.querySelector('[data-reader-verse="1"]')
      .getBoundingClientRect();
    const background = getComputedStyle(toolbarElement).backgroundColor;
    return {
      chipBottom: chip.bottom,
      firstVerseTop: firstVerse.top,
      toolbarBackground: background,
      toolbarBottom: toolbar.bottom,
      toolbarTop: toolbar.top,
    };
  });
  assert.ok(Math.abs(layout.toolbarTop - layout.chipBottom) <= 1);
  assert.ok(layout.firstVerseTop >= layout.toolbarBottom + 8);
  assert.doesNotMatch(layout.toolbarBackground, /rgba\([^)]*,\s*0\s*\)$/u);
  assert.notEqual(layout.toolbarBackground, "transparent");
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
  const publicChapter = (translation, book, chapter) => {
    const key = `${translation}:${book}:${chapter}`;
    if (!chapterBodies.has(key)) {
      const body = jsonBody(chapterPayload(translation, book, chapter));
      chapterBodies.set(key, { body, sha: sha1(body) });
    }
    return chapterBodies.get(key);
  };

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
    const shaMatch = /^(kjv|aov)\/(\d{1,3})\/(\d{1,3})\.sha$/.exec(path);
    if (shaMatch) {
      const book = Number(shaMatch[2]);
      const chapter = Number(shaMatch[3]);
      if (!bookNames[book - 1] || chapter < 1) {
        return fulfillJson(route, { error: "not found" }, 404, true);
      }
      return fulfillPublicText(
        route,
        publicChapter(shaMatch[1], book, chapter).sha,
      );
    }
    const jsonMatch = /^(kjv|aov)\/(\d{1,3})\/(\d{1,3})\.json$/.exec(path);
    if (jsonMatch) {
      const book = Number(jsonMatch[2]);
      const chapter = Number(jsonMatch[3]);
      if (!bookNames[book - 1] || chapter < 1) {
        return fulfillJson(route, { error: "not found" }, 404, true);
      }
      return fulfillPublicText(
        route,
        publicChapter(jsonMatch[1], book, chapter).body,
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
  const contributionBatches = [];
  let contributionAttempts = 0;
  const contributionStatus = {
    enabled: true,
    state: "approved",
    can_contribute: true,
    disclosure_required: false,
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
        user: { id: 42 },
        preferences,
        entrypoint: { route: "bible", query: "" },
        basket: { items: [], count: 0, maximum: 100 },
        contributions: contributionStatus,
      }, 201);
    }
    if (apiPath === "cleanup") return fulfillJson(route, {});
    if (apiPath === "contributions/status") {
      return fulfillJson(route, contributionStatus);
    }
    if (apiPath === "contributions/events") {
      const events = request.postDataJSON().events;
      contributionAttempts += 1;
      if (contributionAttempts === 1) {
        return fulfillJson(route, {
          error: "rate_limited",
          message: "Please wait.",
          retryable: true,
          retry_after: 0.01,
        }, 429);
      }
      contributionBatches.push(events);
      return fulfillJson(route, {
        accepted: events.length,
        replayed: 0,
        event_ids: Object.fromEntries(
          events.map((event, index) => [event.client_event_id, index + 1]),
        ),
      });
    }
    if (apiPath === "bookmarks/catalog") {
      return fulfillJson(route, {
        revision: 0,
        checksum: "0".repeat(64),
        catalog: {
          schema_version: 1,
          topics: [],
          associations: { add: [], remove: [] },
        },
      });
    }
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
  await assertReaderStartsBelowToolbar(page);
  await page.locator("#bible-view").evaluate((view) => {
    view.scrollTop = 420;
    view.dispatchEvent(new Event("scroll"));
  });
  await page.waitForFunction(() => (
    document.querySelector("#bible-heading")?.classList.contains("is-hidden")
  ));
  await page.locator("#bible-view").evaluate((view) => {
    view.scrollTop = 0;
    view.dispatchEvent(new Event("scroll"));
  });
  await page.waitForFunction(() => (
    !document.querySelector("#bible-heading")?.classList.contains("is-hidden")
  ));
  await assertReaderStartsBelowToolbar(page);
  await assertBottomNavigationIconsMatch(page);

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
  await assertBookmarkPopoverControlsDoNotOverlap(page);
  await page.setViewportSize({ width: 320, height: 844 });
  await assertBookmarkPopoverControlsDoNotOverlap(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await assertBookmarkPopoverControlsDoNotOverlap(page);
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
  await waitForCondition(
    () => contributionBatches.flat().filter((event) => (
      event.type === "verse_add" && event.verse.verse === 2
    )).length === 2,
    "contribution queue did not resume after Retry-After",
    15_000,
  );
  assert.ok(contributionAttempts >= 2);

  await page.locator("#close-bookmark-popover").click();
  await page.locator('[data-reader-verse="3"]').click();
  await page.locator(
    '[data-bookmark-trigger="gbd_kjv_043_0003_0003"]',
  ).click();
  const pickerGroups = await page.locator("#bookmark-topic-picker").evaluate(
    (select) => [...select.querySelectorAll("optgroup")].map((group) => ({
      label: group.label,
      values: [...group.querySelectorAll("option")].map((option) => option.value),
      labels: [...group.querySelectorAll("option")].map((option) => option.textContent),
    })),
  );
  assert.deepEqual(pickerGroups.map((group) => group.label), [
    "Recently used",
    "All topics",
  ]);
  assert.deepEqual(pickerGroups[0].values.slice(0, 2), [
    "biblical-love",
    "grace",
  ]);
  assert.equal(pickerGroups[1].values.includes("biblical-love"), true);
  assert.equal(pickerGroups[1].values.includes("grace"), true);
  assert.deepEqual(
    pickerGroups[1].labels,
    [...pickerGroups[1].labels].sort((left, right) =>
      left.localeCompare(right, "en", { sensitivity: "base" })
    ),
  );
  assert.equal(await page.locator("#clear-recent-bookmark-topics").isVisible(), true);
  await page.locator("#clear-recent-bookmark-topics").click();
  assert.deepEqual(
    await page.locator("#bookmark-topic-picker optgroup").evaluateAll(
      (groups) => groups.map((group) => group.label),
    ),
    ["All topics"],
  );
  await page.locator("#close-bookmark-popover").click();
  await page.locator('[data-reader-verse="3"]').click();
  await page.locator(
    '[data-bookmark-trigger="gbd_kjv_043_0003_0002"]',
  ).click();

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
  const expectedHomeHistoryCount = await page.evaluate((storageKey) => (
    JSON.parse(window.localStorage.getItem(storageKey)).items.length
  ), readingHistoryStorageKey);
  assert.ok(expectedHomeHistoryCount > 0);
  await page.locator('[data-route="home"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "home"
  ));
  assert.equal(await page.locator(".home-actions .home-action").count(), 2);
  assert.equal(await page.locator("#home-history").isVisible(), true);
  assert.equal(
    await page.locator("#home-history-meta").innerText(),
    `${expectedHomeHistoryCount} places`,
  );
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
  const narrowHomeActions = await page.locator(".home-action")
    .evaluateAll((actions) => actions.map((action) => {
      const bounds = action.getBoundingClientRect();
      return { bottom: bounds.bottom, left: bounds.left, top: bounds.top };
    }));
  assert.ok(Math.abs(narrowHomeActions[0].left - narrowHomeActions[1].left) < 1);
  assert.ok(narrowHomeActions[0].bottom <= narrowHomeActions[1].top);
  await page.setViewportSize({ width: 780, height: 844 });
  await assertBottomNavigationIconsMatch(page);
  const wideHomeActions = await page.locator(".home-action")
    .evaluateAll((actions) => actions.map((action) => {
      const bounds = action.getBoundingClientRect();
      return {
        left: bounds.left,
        right: bounds.right,
        top: bounds.top,
        width: bounds.width,
      };
    }));
  assert.ok(Math.abs(wideHomeActions[0].top - wideHomeActions[1].top) < 1);
  assert.ok(wideHomeActions[0].right <= wideHomeActions[1].left);
  assert.ok(Math.abs(wideHomeActions[0].width - wideHomeActions[1].width) < 1);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#home-bookmarks [data-home-route="bookmarks"]').click();
  await page.waitForFunction((expectedTopicCount) => (
    document.querySelector("#app")?.dataset.activeRoute === "bookmarks" &&
    document.querySelectorAll(".bookmark-group-card").length === expectedTopicCount
  ), globalTopicCount);
  assert.equal(await page.locator("#bottom-nav").isVisible(), true);
  assert.equal(
    await page.locator("#translation-shortcut").evaluate(
      (button) => getComputedStyle(button).display,
    ),
    "none",
  );
  assert.equal(await page.locator(".bookmark-group-card").count(), globalTopicCount);
  const topicGroupNames = await page.locator(".bookmark-group-card strong")
    .allInnerTexts();
  assert.deepEqual(
    topicGroupNames,
    [...topicGroupNames].sort((left, right) =>
      left.localeCompare(right, "en", { sensitivity: "base" })
    ),
  );
  await page.locator("#bookmark-topic-manager summary").click();
  const managedTopicNames = await page.locator(
    "#bookmark-topic-editor .bookmark-topic-editor__name",
  ).evaluateAll((items) => items.map((item) => item.value || item.textContent));
  assert.deepEqual(
    managedTopicNames,
    [...managedTopicNames].sort((left, right) =>
      left.localeCompare(right, "en", { sensitivity: "base" })
    ),
  );
  await page.locator("#bookmark-topic-manager summary").click();
  const globalControlsLayout = await page.evaluate(() => {
    const section = document.querySelector(".bookmark-global").getBoundingClientRect();
    const search = document.querySelector(".bookmark-search").getBoundingClientRect();
    const targets = [
      document.querySelector("#load-global-bookmarks"),
      document.querySelector("#clear-global-bookmarks"),
      document.querySelector(".bookmark-global__info > summary"),
    ].map((element) => {
      const bounds = element.getBoundingClientRect();
      return { height: bounds.height, width: bounds.width };
    });
    return { sectionBottom: section.bottom, searchTop: search.top, targets };
  });
  assert.ok(globalControlsLayout.sectionBottom <= globalControlsLayout.searchTop);
  for (const target of globalControlsLayout.targets) {
    assert.ok(target.height >= 44);
    assert.ok(target.width >= 44);
  }
  await page.locator(".bookmark-global__info > summary").click();
  assert.equal(await page.locator(".bookmark-global__info > p").isVisible(), true);
  assert.match(
    await page.locator(".bookmark-global__info > p").innerText(),
    /sets of verses that match each topic/i,
  );
  await page.locator(".bookmark-global__info > summary").click();
  await page.locator(
    '.bookmark-group-card[data-bookmark-topic="grace"]',
  ).click();
  await page.waitForFunction(() => (
    !document.querySelector("#bookmark-detail")?.hidden &&
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === 1
  ));
  assert.match(await page.locator("#bookmark-list").innerText(), /John 3:2/);
  assert.equal(await page.locator("#load-topic-global-bookmarks").isVisible(), true);
  const catalogsBeforeExplicitTopicLoad = robotRequests.filter(
    (path) => path === "bookmarks/catalog",
  ).length;
  await page.locator("#load-topic-global-bookmarks").click();
  await page.waitForFunction(({ globalCount }) => (
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length ===
      globalCount + 1 &&
    document.querySelectorAll("#bookmark-list .bookmark-list__global-badge").length ===
      globalCount
  ), { globalCount: graceGlobalCount });
  assert.ok(
    robotRequests.filter((path) => path === "bookmarks/catalog").length >
      catalogsBeforeExplicitTopicLoad,
  );
  assert.equal(
    await page.locator("#bookmark-list [data-bookmark-remove]").count(),
    graceGlobalCount + 1,
  );
  assert.equal(
    await page.locator('#bookmark-list [data-bookmark-open^="global_grace_"]').count(),
    graceGlobalCount,
  );
  const hiddenGlobalId = await page.locator(
    '#bookmark-list [data-bookmark-open^="global_grace_"]',
  ).first().getAttribute("data-bookmark-open");
  await page.locator(
    `#bookmark-list [data-bookmark-open="${hiddenGlobalId}"] + [data-bookmark-remove]`,
  ).click();
  await page.waitForFunction(({ bookmarkId, storageKey, expectedCount }) => {
    const envelope = JSON.parse(window.localStorage.getItem(storageKey));
    const record = JSON.parse(envelope.value);
    return document.querySelectorAll("#bookmark-list .bookmark-list__global-badge")
      .length === expectedCount && record.hidden_bookmark_ids.includes(bookmarkId);
  }, {
    bookmarkId: hiddenGlobalId,
    storageKey: globalBookmarkPreferencesMirrorKey,
    expectedCount: graceGlobalCount - 1,
  });
  const hiddenCoordinate = hiddenGlobalId.split("_").slice(-3).map(Number);
  await waitForCondition(
    () => contributionBatches.flat().some((event) => (
      event.type === "verse_remove" &&
      event.topic.local_topic_id === "grace" &&
      event.verse.book === hiddenCoordinate[0] &&
      event.verse.chapter === hiddenCoordinate[1] &&
      event.verse.verse === hiddenCoordinate[2]
    )),
    "global list removal was not mirrored for review",
    15_000,
  );
  assert.match(
    await page.locator("#load-topic-global-bookmarks-label").innerText(),
    /Reload global verses/,
  );
  const matchingGlobalAddsBeforeRestore = contributionBatches.flat().filter(
    (event) => (
      event.type === "verse_add" &&
      event.topic.local_topic_id === "grace" &&
      event.verse.book === hiddenCoordinate[0] &&
      event.verse.chapter === hiddenCoordinate[1] &&
      event.verse.verse === hiddenCoordinate[2]
    ),
  ).length;
  await page.locator("#load-topic-global-bookmarks").click();
  await page.waitForFunction(({ bookmarkId, storageKey, expectedCount }) => {
    const envelope = JSON.parse(window.localStorage.getItem(storageKey));
    const record = JSON.parse(envelope.value);
    return document.querySelectorAll("#bookmark-list .bookmark-list__global-badge")
      .length === expectedCount && !record.hidden_bookmark_ids.includes(bookmarkId);
  }, {
    bookmarkId: hiddenGlobalId,
    storageKey: globalBookmarkPreferencesMirrorKey,
    expectedCount: graceGlobalCount,
  });
  await waitForCondition(
    () => contributionBatches.flat().filter((event) => (
      event.type === "verse_add" &&
      event.topic.local_topic_id === "grace" &&
      event.verse.book === hiddenCoordinate[0] &&
      event.verse.chapter === hiddenCoordinate[1] &&
      event.verse.verse === hiddenCoordinate[2]
    )).length > matchingGlobalAddsBeforeRestore,
    "restored global list assignment was not mirrored for review",
    15_000,
  );
  assert.equal(await page.locator("#clear-topic-global-bookmarks").isVisible(), true);
  await page.locator("#clear-topic-global-bookmarks").click();
  await page.waitForFunction(() => (
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === 1 &&
    document.querySelectorAll("#bookmark-list .bookmark-list__global-badge").length === 0 &&
    document.querySelector("#bookmark-topic-global-status")?.textContent
      ?.includes("Personal bookmarks were kept")
  ));
  assert.equal(
    await page.evaluate((storageKey) => {
      const envelope = JSON.parse(window.localStorage.getItem(storageKey));
      const record = JSON.parse(envelope.value);
      return record.enabled_topic_ids.includes("grace");
    }, globalBookmarkPreferencesMirrorKey),
    false,
  );
  await page.locator("#bookmark-all-topics").click();
  await page.locator("#load-global-bookmarks").click();
  await page.waitForFunction((expected) => (
    document.querySelector("#global-bookmark-status")?.textContent
      ?.includes(`${expected.assignments} global verse links`) &&
    document.querySelector("#global-bookmark-status")?.textContent
      ?.includes(`${expected.topics} topics`)
  ), { assignments: globalAssignmentCount, topics: globalTopicCount });
  assert.equal(await page.locator("#clear-global-bookmarks").isEnabled(), true);
  await page.locator("#clear-global-bookmarks").click();
  await page.waitForFunction((storageKey) => {
    const envelope = JSON.parse(window.localStorage.getItem(storageKey));
    const record = JSON.parse(envelope.value);
    return record.enabled_topic_ids.length === 0 &&
      document.querySelector("#global-bookmark-status")?.textContent
        ?.includes("Personal bookmarks and topics were kept");
  }, globalBookmarkPreferencesMirrorKey);
  assert.equal(await page.locator("#clear-global-bookmarks").isDisabled(), true);
  await page.locator(
    '.bookmark-group-card[data-bookmark-topic="grace"]',
  ).click();
  await page.waitForFunction(() => (
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === 1 &&
    document.querySelectorAll("#bookmark-list .bookmark-list__global-badge").length === 0
  ));
  await page.locator("#bookmark-all-topics").click();
  await page.locator("#load-global-bookmarks").click();
  await page.waitForFunction((expectedAssignments) => (
    document.querySelector("#global-bookmark-status")?.textContent
      ?.includes(`${expectedAssignments} global verse links`) &&
    !document.querySelector("#clear-global-bookmarks")?.disabled
  ), globalAssignmentCount);
  await page.locator(
    '.bookmark-group-card[data-bookmark-topic="grace"]',
  ).click();
  await page.waitForFunction((globalCount) => (
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length ===
      globalCount + 1 &&
    document.querySelectorAll("#bookmark-list .bookmark-list__global-badge").length ===
      globalCount
  ), graceGlobalCount);
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
  await page.waitForFunction((globalCount) => (
    document.querySelectorAll("#bookmark-list .bookmark-list__global-badge").length ===
      globalCount
  ), spiritualRebirthGlobalCount);
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
  await page.locator(
    '[data-bookmark-trigger="gbd_kjv_043_0003_0003"]',
  ).click();
  const globalPopoverRemove = page.locator(
    '[data-bookmark-source="global"][data-bookmark-assignment-remove]',
  ).first();
  assert.equal(await globalPopoverRemove.isVisible(), true);
  await globalPopoverRemove.click();
  await waitForCondition(
    () => contributionBatches.flat().some((event) => (
      event.type === "verse_remove" &&
      event.topic.local_topic_id === "spiritual-rebirth" &&
      event.verse.book === 43 &&
      event.verse.chapter === 3 &&
      event.verse.verse === 3
    )),
    "global popover removal was not mirrored for review",
    15_000,
  );
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
  await assertReaderStartsBelowToolbar(page);
  const historyCountAfterNextChapter = historyCountBeforeNextChapter;
  await page.waitForFunction((expectedCount) => (
    document.querySelector("#bible-history-count")?.textContent ===
      String(expectedCount)
  ), historyCountAfterNextChapter);
  await waitForCondition(
    () => preferences.reader_location.chapter === 4,
    "reader preference did not reach John 4",
  );
  const johnFourHistory = await page.evaluate((storageKey) => {
    const record = JSON.parse(window.localStorage.getItem(storageKey));
    return record.items.filter((item) => (
      item.kind === "chapter" && item.book === 43 && item.chapter === 4
    ));
  }, readingHistoryStorageKey);
  assert.equal(johnFourHistory.length, 1);
  assert.equal(johnFourHistory[0].translation, "kjv");
  assert.equal(johnFourHistory[0].verse, 1);

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
  await assertReaderStartsBelowToolbar(page);

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

  // Changing the active Bible translation localizes the bookmark surface and
  // keeps History tied to the reader's current choice rather than each visit's
  // recorded translation metadata.
  await page.locator("#translation-shortcut").click();
  await page.locator("#translation-select").selectOption("aov");
  await page.waitForFunction(() => (
    !document.querySelector("#translation-dialog")?.open &&
    document.documentElement.lang === "af" &&
    document.querySelector("#translation-short-label")?.textContent === "AOV" &&
    document.querySelector('#bible-verses [data-reader-verse="1"]')
  ));
  await waitForCondition(
    () => preferences.translation === "aov",
    "translation preference did not reach AOV",
  );
  await ensureBottomNavigationExpanded(page);
  await page.locator('[data-route="home"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "home"
  ));
  assert.equal(
    await page.locator("#home-hero [data-i18n=\"home.tagline\"]").innerText(),
    "Die woorde van die ewige lewe",
  );
  await page.locator('#home-bookmarks [data-home-route="bookmarks"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bookmarks"
  ));
  assert.equal(await page.locator("#bookmarks-title").innerText(), "Boekmerke");
  assert.equal(await page.locator("#bookmark-backup-status").innerText(), "");
  const localizedSpiritualRebirth = page.locator(
    '.bookmark-group-card[data-bookmark-topic="spiritual-rebirth"]',
  );
  assert.equal(
    await localizedSpiritualRebirth.locator("strong").innerText(),
    "Geestelike wedergeboorte",
  );
  await localizedSpiritualRebirth.click();
  await page.waitForFunction(() => (
    !document.querySelector("#bookmark-detail")?.hidden &&
    document.querySelector("#bookmark-detail-title")?.textContent ===
      "Geestelike wedergeboorte"
  ));
  assert.equal(
    await page.locator("#bookmark-detail-title").innerText(),
    "Geestelike wedergeboorte",
  );
  const globalExcerpt = page.locator(
    '[data-bookmark-preview-text="global_spiritual-rebirth_43_3_3"]',
  );
  await globalExcerpt.scrollIntoViewIfNeeded();
  await page.waitForFunction(() => {
    const excerpt = document.querySelector(
      '[data-bookmark-preview-text="global_spiritual-rebirth_43_3_3"]',
    );
    return excerpt && !excerpt.hidden &&
      excerpt.textContent === "AOV John 3 text 3";
  });
  assert.equal(
    await page.locator(
      '[data-bookmark-open="global_spiritual-rebirth_43_3_3"]',
    ).getAttribute("aria-describedby"),
    await globalExcerpt.getAttribute("id"),
  );
  await page.locator("#bookmark-all-topics").click();
  await page.waitForFunction(() => (
    !document.querySelector("#bookmark-groups-panel")?.hidden
  ));
  assert.equal(
    await page.locator(
      '.bookmark-group-card[data-bookmark-topic="grace"] strong',
    ).innerText(),
    "Genade",
  );
  await page.locator("#bookmark-topic-manager summary").click();
  const coreGraceEditor = page.locator('[data-topic-editor="grace"]');
  assert.equal(
    await coreGraceEditor.locator(".bookmark-topic-editor__name--core").innerText(),
    "Genade",
  );
  assert.equal(await coreGraceEditor.locator('[data-topic-name="grace"]').count(), 0);
  assert.equal(await coreGraceEditor.locator('[data-topic-color="grace"]').count(), 1);
  assert.equal(await coreGraceEditor.locator('[data-topic-delete="grace"]').count(), 1);
  await ensureBottomNavigationExpanded(page);

  await page.setViewportSize({ width: 320, height: 844 });
  const historyCountBeforeHistoryUi = Number(
    await page.locator("#bible-history-count").innerText(),
  );
  assert.equal(Number.isSafeInteger(historyCountBeforeHistoryUi), true);
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
    historyCountBeforeHistoryUi,
  );
  assert.equal(await page.locator("#reading-history-title").innerText(), "Geskiedenis");
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
    return record.items.map((item) => item.kind === "chapter"
      ? `chapter:${item.book}:${item.chapter}`
      : `verse:${item.book}:${item.chapter}:${item.verse}`
    );
  }, readingHistoryStorageKey);
  assert.equal(new Set(storedLocations).size, storedLocations.length);
  const selectedHistory = page.locator(".history-item")
    .filter({ has: page.getByText("John 4:16", { exact: true }) })
    .filter({
      has: page.getByText("Afrikaanse Ou Vertaling (AOV)", {
        exact: true,
      }),
  });
  assert.equal(await selectedHistory.count(), 1);
  await selectedHistory.scrollIntoViewIfNeeded();
  await page.waitForFunction(() => [...document.querySelectorAll(
    ".history-item__text",
  )].some((excerpt) => excerpt.textContent === "AOV John 4 text 16"));
  assert.match(await selectedHistory.innerText(), /Afrikaanse Ou Vertaling.*AOV/);
  assert.match(await selectedHistory.innerText(), /Vers gekies/);
  assert.match(await selectedHistory.innerText(), /AOV John 4 text 16/);
  assert.equal(
    await selectedHistory.locator("[data-history-open]")
      .getAttribute("aria-describedby"),
    await selectedHistory.locator(".history-item__text").getAttribute("id"),
  );

  await selectedHistory.locator("[data-history-remove]").click();
  const historyCountAfterRemoval = historyCountBeforeHistoryUi - 1;
  assert.equal(
    await page.locator(".history-item").count(),
    historyCountAfterRemoval,
  );
  assert.equal(
    await page.locator("#bible-history-count").innerText(),
    String(historyCountAfterRemoval),
  );
  assert.equal(robotRequests.length, robotRequestsBeforeHistoryUi);

  const currentTranslationHistory = page.locator(".history-item")
    .filter({ has: page.getByText("John 3:8", { exact: true }) })
    .filter({
      has: page.getByText("Afrikaanse Ou Vertaling (AOV)", { exact: true }),
    });
  assert.equal(await currentTranslationHistory.count(), 1);
  await currentTranslationHistory.scrollIntoViewIfNeeded();
  await page.waitForFunction(() => [...document.querySelectorAll(
    ".history-item__text",
  )].some((excerpt) => excerpt.textContent === "AOV John 3 text 8"));
  assert.match(
    await currentTranslationHistory.innerText(),
    /AOV John 3 text 8/,
  );
  await currentTranslationHistory
    .locator("[data-history-open]")
    .click();
  await page.waitForFunction(() => (
    document.querySelector("#bible-reference")?.textContent === "John 3" &&
    document.querySelector("#bible-translation-label")?.textContent
      ?.includes("AOV") &&
    document.activeElement?.getAttribute("data-reader-verse") === "8"
  ));
  assert.match(
    await page.locator('[data-reader-verse="8"]').innerText(),
    /AOV John 3 text 8/,
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
        item.book === 43 &&
        item.chapter === 3 &&
        item.verse === 8
      )).length,
    };
  }, readingHistoryStorageKey);
  assert.equal(promotedHistory.count, historyCountAfterRemoval);
  assert.equal(promotedHistory.matching, 1);
  assert.equal(promotedHistory.first.kind, "selection");
  assert.equal(promotedHistory.first.translation, "aov");
  assert.equal(promotedHistory.first.chapter, 3);
  assert.equal(promotedHistory.first.verse, 8);
  await waitForCondition(
    () => (
      preferences.translation === "aov" &&
      preferences.reader_location.translation === "aov" &&
      preferences.reader_location.chapter === 3 &&
      preferences.reader_location.verse === 8
    ),
    "reader preference did not open history in the current AOV translation",
  );

  await ensureBottomNavigationExpanded(page);
  await page.locator("#bible-history").click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "history" &&
    document.querySelector("#history-view")?.scrollTop === 0
  ));
  const firstPromotedHistory = page.locator(".history-item").first();
  assert.equal(await firstPromotedHistory.isVisible(), true);
  await page.waitForFunction(() => (
    document.querySelector(".history-item:first-child .history-item__text")
      ?.textContent === "AOV John 3 text 8"
  ));
  assert.match(await firstPromotedHistory.innerText(), /John 3:8/);
  assert.match(await firstPromotedHistory.innerText(), /AOV/);
  assert.match(await firstPromotedHistory.innerText(), /AOV John 3 text 8/);
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
    if (route === "home") {
      assert.equal(await page.locator("#home-history").isHidden(), true);
    }
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
    "aov/43/3.json",
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
