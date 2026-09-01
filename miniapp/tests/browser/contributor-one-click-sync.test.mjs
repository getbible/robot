import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { chromium, webkit } from "playwright";

const miniappRoot = resolve(fileURLToPath(new URL("../../", import.meta.url)));
const mainApiPattern = /^https:\/\/api\.getbible\.net\/v2\/.+/;
const queryApiPattern = /^https:\/\/query\.getbible\.net\/v2\/.+/;
const corsHeaders = { "access-control-allow-origin": "*" };
const sessionToken = "ContributorBrowserSession123";
const contributionToken = `gbc_${"A".repeat(43)}`;
const browserName = process.env.PLAYWRIGHT_BROWSER ?? "chromium";
const browserType = { chromium, webkit }[browserName];
if (!browserType) {
  throw new TypeError("PLAYWRIGHT_BROWSER must be chromium or webkit.");
}

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

function installTelegramMock() {
  const handlers = new Map();
  const emit = (name, payload) => {
    for (const handler of handlers.get(name) ?? []) handler(payload);
  };
  window.__telegramState = { alerts: [], notifications: [], readyCalls: 0 };
  window.Telegram = {
    WebApp: {
      initData: "query_id=contributor-browser-test&user=%7B%22id%22%3A42%7D",
      initDataUnsafe: { start_param: "contributor-test" },
      colorScheme: "light",
      version: "8.0",
      viewportStableHeight: 844,
      safeAreaInset: { top: 0, right: 0, bottom: 0, left: 0 },
      contentSafeAreaInset: { top: 0, right: 0, bottom: 0, left: 0 },
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
      HapticFeedback: {
        selectionChanged() {},
        notificationOccurred(type) {
          window.__telegramState.notifications.push(type);
        },
      },
      showAlert(message, callback) {
        window.__telegramState.alerts.push(message);
        callback();
      },
      showConfirm(_message, callback) { callback(true); },
      onEvent(name, handler) {
        const current = handlers.get(name) ?? new Set();
        current.add(handler);
        handlers.set(name, current);
      },
      offEvent(name, handler) { handlers.get(name)?.delete(handler); },
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

function jsonBody(payload) {
  return JSON.stringify(payload);
}

function fulfillJson(route, payload, status = 200, headers = {}) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers,
    body: jsonBody(payload),
  });
}

function fulfillPublicJson(route, payload, status = 200) {
  return fulfillJson(route, payload, status, corsHeaders);
}

function fulfillPublicText(route, body) {
  return route.fulfill({
    status: 200,
    contentType: "text/plain",
    headers: corsHeaders,
    body,
  });
}

function sha1(value) {
  return createHash("sha1").update(value, "utf8").digest("hex");
}

function chapterPayload() {
  return {
    abbreviation: "kjv",
    book_nr: 43,
    book_name: "John",
    chapter: 3,
    name: "John 3",
    testament: "new",
    verses: Array.from({ length: 40 }, (_, index) => ({
      verse: index + 1,
      text: `KJV John 3 test verse ${index + 1}`,
    })),
  };
}

function contributionSummary({ submitted = false } = {}) {
  return {
    topics: {
      pending: submitted ? 1 : 0,
      mapped: 0,
      published: 0,
      rejected: 0,
      deferred: 0,
    },
    events: {
      pending: submitted ? 2 : 0,
      approved: 0,
      rejected: 0,
      deferred: 0,
      applied: 0,
    },
  };
}

function contributionStatus({
  approved,
  localTopicId = null,
  submitted = false,
  disclosureRequired = false,
}) {
  return {
    enabled: true,
    state: approved ? "approved" : "pending",
    can_contribute: approved,
    disclosure_required: disclosureRequired,
    ...(approved ? { contribution_token: contributionToken } : {}),
    topics: localTopicId && submitted
      ? [{
          local_topic_id: localTopicId,
          state: "pending",
          published: false,
        }]
      : [],
    summary: contributionSummary({ submitted }),
  };
}

function emptyCatalog() {
  return {
    revision: 0,
    checksum: "0".repeat(64),
    catalog: {
      schema_version: 1,
      topics: [],
      associations: { add: [], remove: [] },
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

async function createBrowserFixture(
  context,
  { approved = true, disclosureRequired = false, failFirstSync = false } = {},
) {
  const executablePath = browserName === "chromium"
    ? process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
    : process.env.PLAYWRIGHT_WEBKIT_EXECUTABLE_PATH;
  const browser = await browserType.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  context.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(installTelegramMock);

  const pageErrors = [];
  const failedRequests = [];
  const sessionRequests = [];
  const syncRequests = [];
  const requestSequence = [];
  let statusRequests = 0;
  let catalogRequests = 0;
  let shouldFailSync = failFirstSync;
  let disclosurePending = disclosureRequired;
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    const errorText = request.failure()?.errorText ?? "failed";
    const pathname = new URL(request.url()).pathname;
    // Launch cleanup is deliberately fire-and-forget. Chromium reports an
    // intercepted keepalive 204 as ERR_ABORTED even though the server handled
    // it; keep every other URL and failure reason visible to the assertion.
    if (pathname.endsWith("/api/v1/cleanup") && errorText === "net::ERR_ABORTED") {
      return;
    }
    failedRequests.push(`${request.url()}: ${errorText}`);
  });

  const chapterBody = jsonBody(chapterPayload());
  const chapterSha = sha1(chapterBody);

  await page.route("https://telegram.org/**", (route) => route.fulfill({
    status: 200,
    contentType: "application/javascript",
    body: "",
  }));
  await page.route(mainApiPattern, (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/v2\//, "");
    if (path === "translations.json") {
      return fulfillPublicJson(route, [{
        abbreviation: "kjv",
        name: "King James Version (1769)",
        language: "English",
        lang: "en",
        direction: "ltr",
      }]);
    }
    if (path === "kjv.sha") return fulfillPublicText(route, "1".repeat(40));
    if (path === "kjv/books.json") {
      return fulfillPublicJson(route, bookNames.map((name, index) => ({
        nr: index + 1,
        name,
        testament: index < 39 ? "old" : "new",
      })));
    }
    if (path === "kjv/43.sha") return fulfillPublicText(route, "2".repeat(40));
    if (path === "kjv/43/chapters.json") {
      return fulfillPublicJson(route, Array.from({ length: 21 }, (_, index) => ({
        chapter: index + 1,
        verses: Array.from({ length: 40 }, (_, verse) => verse + 1),
      })));
    }
    if (path === "kjv/43/3.sha") return fulfillPublicText(route, chapterSha);
    if (path === "kjv/43/3.json") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: corsHeaders,
        body: chapterBody,
      });
    }
    return fulfillPublicJson(route, { error: "not found" }, 404);
  });
  await page.route(queryApiPattern, (route) =>
    fulfillPublicJson(route, { error: "unexpected query request" }, 400)
  );

  await page.route("https://app.local/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const apiPath = url.pathname.split("/api/v1/")[1];
    if (!apiPath) return serveStatic(route);
    if (apiPath === "session") {
      sessionRequests.push({
        body: request.postDataJSON(),
        headers: request.headers(),
        method: request.method(),
      });
      return fulfillJson(route, {
        session_token: "ContributorBrowserSession123",
        expires_in: 10_800,
        user: { id: 42 },
        preferences: {
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
        },
        entrypoint: { route: "bible", query: "" },
        basket: { items: [], count: 0, maximum: 100 },
        contributions: contributionStatus({
          approved,
          disclosureRequired: disclosurePending,
        }),
      }, 201);
    }
    if (apiPath === "cleanup") {
      return route.fulfill({ status: 204, body: "" });
    }
    if (apiPath === "preferences") {
      return fulfillJson(route, { preferences: request.postDataJSON() });
    }
    if (apiPath === "contributions/status") {
      statusRequests += 1;
      requestSequence.push("status");
      return fulfillJson(
        route,
        contributionStatus({ approved, disclosureRequired: disclosurePending }),
      );
    }
    if (apiPath === "contributions/events") {
      const body = request.postDataJSON();
      syncRequests.push({
        body,
        headers: request.headers(),
        method: request.method(),
      });
      requestSequence.push("sync");
      if (shouldFailSync) {
        shouldFailSync = false;
        return fulfillJson(route, {
          error: "contributions_unavailable",
          message: "Contribution sync is temporarily unavailable.",
          retryable: true,
          retry_after: 0,
        }, 503);
      }
      if (body.contribution_token !== contributionToken) {
        return fulfillJson(route, {
          error: "contribution_not_allowed",
          message: "This Telegram user is not an approved contributor.",
        }, 403);
      }
      if (body.disclosure_acknowledged === true) {
        disclosurePending = false;
      }
      const localTopicId = body.events.find((event) =>
        event.type === "topic_upsert" && event.topic.name === "Community Hope"
      )?.topic.local_topic_id ?? null;
      return fulfillJson(route, {
        accepted: body.events.length,
        replayed: 0,
        event_ids: Object.fromEntries(
          body.events.map((event, index) => [event.client_event_id, index + 1]),
        ),
        status: contributionStatus({
          approved,
          localTopicId,
          submitted: true,
          disclosureRequired: disclosurePending,
        }),
        catalog: { revision: 0, checksum: "0".repeat(64) },
      });
    }
    if (apiPath === "contributions/sync") {
      requestSequence.push("retired-sync");
      return fulfillJson(route, { error: "not_found" }, 404);
    }
    if (apiPath === "bookmarks/catalog") {
      catalogRequests += 1;
      requestSequence.push("catalog");
      return fulfillJson(route, emptyCatalog(), 200, { ETag: '"catalog-0"' });
    }
    return fulfillJson(route, { error: { code: "not_found" } }, 404);
  });

  return {
    catalogRequestCount: () => catalogRequests,
    failedRequests,
    page,
    pageErrors,
    requestSequence,
    sessionRequests,
    statusRequestCount: () => statusRequests,
    syncRequests,
  };
}

async function openBookmarksRoute(page) {
  if (await page.locator("#app").getAttribute("data-active-route") === "bookmarks") {
    return;
  }
  await page.locator('[data-route="home"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "home"
  ));
  await page.locator('[data-home-route="bookmarks"]').click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bookmarks"
  ));
}

async function createPersonalTopicWithVerse(page) {
  await openBookmarksRoute(page);
  const manager = page.locator("#bookmark-topic-manager");
  if (!await manager.evaluate((element) => element.open)) {
    await page.locator("#bookmark-topic-manager > summary").click();
  }
  await page.locator("#bookmark-topic-name").fill("Community Hope");
  await page.locator("#bookmark-topic-form button[type=submit]").click();
  await page.waitForFunction(() => (
    [...document.querySelectorAll("#bookmark-group-list .bookmark-group-card")]
      .some((card) => card.querySelector("strong")?.textContent?.trim() ===
        "Community Hope")
  ));
  const localTopicId = await page.locator("#bookmark-group-list .bookmark-group-card")
    .evaluateAll((cards) => cards.find((card) => (
      card.querySelector("strong")?.textContent?.trim() === "Community Hope"
    ))?.dataset.bookmarkTopic ?? null);
  assert.ok(localTopicId);

  await page.locator('[data-route="bible"]').click();
  await page.waitForSelector('[data-reader-verse="2"]');
  await page.locator('[data-reader-verse="2"]').click();
  const bookmarkId = "gbd_kjv_043_0003_0002";
  await page.waitForFunction((id) => (
    document.querySelector(`[data-bookmark-trigger="${id}"]`)?.textContent === "•••"
  ), bookmarkId);
  await page.locator(`[data-bookmark-trigger="${bookmarkId}"]`).click();
  await page.waitForFunction(() => !document.querySelector("#bookmark-popover")?.hidden);
  await page.locator("#bookmark-topic-picker").selectOption(localTopicId);
  await page.locator(
    `#bookmark-assigned-topics [data-bookmark-topic="${localTopicId}"]`,
  ).waitFor();
  await page.locator("#close-bookmark-popover").click();
  return localTopicId;
}

async function openContributorManager(page) {
  await openBookmarksRoute(page);
  await page.waitForFunction(() => !document.querySelector("#contributor-manager")?.hidden);
  const manager = page.locator("#contributor-manager");
  if (!await manager.evaluate((element) => element.open)) {
    await page.locator("#contributor-manager > summary").click();
  }
}

test("Sync Now drips session-authenticated event batches without fanout", async (context) => {
  const fixture = await createBrowserFixture(context);
  const {
    failedRequests,
    page,
    pageErrors,
    requestSequence,
    sessionRequests,
    syncRequests,
  } = fixture;
  await page.goto("https://app.local/miniapp/index.html?launch=contributor-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector('[data-reader-verse="2"]', { timeout: 15_000 });
  const localTopicId = await createPersonalTopicWithVerse(page);

  // Local edits are deliberately quiet. They mark the desired snapshot dirty
  // but cannot race the user's explicit transport request.
  await page.waitForTimeout(300);
  assert.equal(syncRequests.length, 0);

  await openContributorManager(page);
  // Let independent bootstrap catalogue work settle before measuring the
  // click. The action itself must have exactly one network dependency.
  await page.waitForTimeout(200);
  const sequenceStart = requestSequence.length;
  const statusStart = fixture.statusRequestCount();
  const catalogStart = fixture.catalogRequestCount();
  await page.locator("#contributor-sync-button").click();
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "success"
  ));

  assert.equal(syncRequests.length, 1);
  assert.deepEqual(requestSequence.slice(sequenceStart), ["sync"]);
  assert.equal(fixture.statusRequestCount(), statusStart);
  assert.equal(fixture.catalogRequestCount(), catalogStart);
  const sync = syncRequests[0];
  assert.equal(sync.method, "POST");
  // The drip uses the exact same plain session bearer the search flow uses.
  assert.equal(sync.headers.authorization, `Bearer ${sessionToken}`);
  assert.equal(sync.headers["x-telegram-init-data"], undefined);
  assert.deepEqual(
    Object.keys(sync.body).sort(),
    ["contribution_token", "events"],
  );
  assert.equal(sync.body.contribution_token, contributionToken);
  assert.equal(sync.headers["x-contribution-token"], undefined);
  assert.deepEqual(
    sync.body.events.map((event) => event.type),
    ["topic_upsert", "verse_add"],
  );
  assert.ok(sync.body.events.every((event) =>
    /^baseline:(?:topic_upsert|verse_add):[a-f0-9]{16}$/.test(event.client_event_id)
  ));
  assert.deepEqual(sync.body.events[0].topic, {
    local_topic_id: localTopicId,
    name: "Community Hope",
    color: "#fde68a",
  });
  assert.deepEqual(sync.body.events[1].verse, {
    book: 43,
    chapter: 3,
    verse: 2,
  });
  assert.equal(
    JSON.stringify(sync.body).includes("KJV John 3 test verse"),
    false,
  );
  assert.equal(
    await page.evaluate(() => window.__telegramState.notifications.includes("success")),
    true,
  );

  assert.equal(sessionRequests.length, 1);
  assert.equal(sessionRequests[0].body.init_data, windowInitData());
  assert.equal(sessionRequests[0].headers.authorization, undefined);
  assert.equal(sessionRequests[0].headers["x-telegram-init-data"], undefined);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(failedRequests, []);
});

test("a lost batch retries the identical deterministic events", async (context) => {
  const fixture = await createBrowserFixture(context, { failFirstSync: true });
  const { page, syncRequests } = fixture;
  await page.goto("https://app.local/miniapp/index.html?launch=contributor-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector('[data-reader-verse="2"]', { timeout: 15_000 });
  const localTopicId = await createPersonalTopicWithVerse(page);
  await openContributorManager(page);

  await page.locator("#contributor-sync-button").click();
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "error"
  ));
  assert.equal(syncRequests.length, 1);
  const firstBody = syncRequests[0].body;

  // The inviolable law: a failed synchronization never touches the personal
  // topic or its bookmarks. Everything stays exactly as the user made it
  // until the server confirms the topic is part of the published core.
  await assertPersonalTopicIntact(page, localTopicId);

  // The client honors the server's retry guard while keeping the button and
  // exact idempotency identity actionable for the next explicit click.
  await page.waitForTimeout(300);
  await page.locator("#contributor-sync-button").click();
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "success"
  ));
  assert.equal(syncRequests.length, 2);
  assert.deepEqual(syncRequests[1].body, firstBody);
  assert.equal(syncRequests[1].headers.authorization, `Bearer ${sessionToken}`);

  // A merely submitted (still pending) contribution is equally untouchable.
  await assertPersonalTopicIntact(page, localTopicId);
});

test("disclosure consent rides inside the first synchronized batch", async (context) => {
  const fixture = await createBrowserFixture(context, { disclosureRequired: true });
  const { page, syncRequests } = fixture;
  await page.goto("https://app.local/miniapp/index.html?launch=contributor-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector('[data-reader-verse="2"]', { timeout: 15_000 });
  await createPersonalTopicWithVerse(page);
  await openContributorManager(page);

  await page.locator("#contributor-sync-button").click();
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "success"
  ));

  assert.equal(
    await page.evaluate(() => window.__telegramState.alerts.length),
    1,
  );
  assert.equal(syncRequests.length, 1);
  assert.equal(syncRequests[0].body.disclosure_acknowledged, true);
  assert.deepEqual(
    Object.keys(syncRequests[0].body).sort(),
    ["contribution_token", "disclosure_acknowledged", "events"],
  );
});

async function assertPersonalTopicIntact(page, localTopicId) {
  await openBookmarksRoute(page);
  const card = await page.evaluate((topicId) => {
    const found = [...document.querySelectorAll(
      "#bookmark-group-list .bookmark-group-card",
    )].find((element) => element.dataset.bookmarkTopic === topicId);
    return found
      ? { text: found.textContent.replace(/\s+/g, " ").trim() }
      : null;
  }, localTopicId);
  assert.ok(card, "the personal topic card must still exist");
  assert.match(card.text, /Community Hope/);
  assert.match(card.text, /(?:One|1) bookmark/);
}

function windowInitData() {
  return "query_id=contributor-browser-test&user=%7B%22id%22%3A42%7D";
}
