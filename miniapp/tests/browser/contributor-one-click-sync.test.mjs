import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { inflateSync } from "node:zlib";

import { chromium, webkit } from "playwright";
import {
  CORE_BOOKMARK_TOPIC_DEFINITIONS,
} from "../../lib/bookmark-topic-definitions.js";
import { GLOBAL_BOOKMARK_DATA } from "../../lib/global-bookmark-data.js";

const miniappRoot = resolve(fileURLToPath(new URL("../../", import.meta.url)));
const mainApiPattern = /^https:\/\/api\.getbible\.net\/v2\/.+/;
const queryApiPattern = /^https:\/\/query\.getbible\.net\/v2\/.+/;
const corsHeaders = { "access-control-allow-origin": "*" };
const globalTopicCount = CORE_BOOKMARK_TOPIC_DEFINITIONS.length;
const globalAssignmentCount = Object.values(GLOBAL_BOOKMARK_DATA.bookmarks_by_topic)
  .reduce((total, coordinates) => total + coordinates.length, 0);
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
  window.__telegramState = {
    alerts: [],
    notifications: [],
    readyCalls: 0,
    sentData: [],
  };
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
      // Real Telegram closes the Mini App after sendData. The mock only
      // records the payload so the test can keep inspecting the page.
      sendData(data) {
        window.__telegramState.sentData.push(String(data));
      },
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

function contributionSummary() {
  return {
    topics: { pending: 0, mapped: 0, published: 0, rejected: 0, deferred: 0 },
    events: { pending: 0, approved: 0, rejected: 0, deferred: 0, applied: 0 },
  };
}

function contributionStatus({ approved }) {
  return {
    enabled: true,
    state: approved ? "approved" : "pending",
    can_contribute: approved,
    disclosure_required: false,
    topics: [],
    summary: contributionSummary(),
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

async function createBrowserFixture(context, { approved = true } = {}) {
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
  const statusRequests = [];
  const receiptRequests = [];
  const contributionWrites = [];
  const requestSequence = [];
  let catalogRequests = 0;
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
    if (apiPath.startsWith("contributions/") && request.method() !== "GET") {
      // The HTTPS contribution write surface no longer exists; a push travels
      // only through Telegram.WebApp.sendData. Record every attempt so the
      // tests can prove the client never falls back to a removed route.
      contributionWrites.push(`${request.method()} ${apiPath}`);
      return fulfillJson(route, { error: { code: "not_found" } }, 404);
    }
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
        contributions: contributionStatus({ approved }),
      }, 201);
    }
    if (apiPath === "cleanup") {
      return route.fulfill({ status: 204, body: "" });
    }
    if (apiPath === "preferences") {
      return fulfillJson(route, { preferences: request.postDataJSON() });
    }
    if (apiPath === "contributions/status") {
      statusRequests.push({
        headers: request.headers(),
        query: url.search,
      });
      requestSequence.push("status");
      return fulfillJson(route, contributionStatus({ approved }));
    }
    if (apiPath === "contributions/receipt") {
      receiptRequests.push({
        headers: request.headers(),
        query: url.search,
      });
      requestSequence.push("receipt");
      return fulfillJson(route, { found: false, receipt: null });
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
    contributionWrites,
    failedRequests,
    page,
    pageErrors,
    receiptRequests,
    requestSequence,
    sessionRequests,
    statusRequests,
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

function decodePushMessage(message) {
  const parts = message.split("|");
  assert.equal(parts.length, 7);
  const [prefix, syncId, index, count, encoding, digest, payload] = parts;
  assert.equal(prefix, "GBC1");
  assert.ok(["d", "j"].includes(encoding));
  assert.match(digest, /^[0-9a-f]{64}$/);
  const body = Buffer.from(payload, "base64url");
  const plaintext = encoding === "d" ? inflateSync(body) : body;
  assert.equal(
    createHash("sha256").update(plaintext).digest("hex"),
    digest,
    "the GBC1 digest must cover the serialized plaintext envelope",
  );
  return {
    count: Number(count),
    encoding,
    envelope: JSON.parse(plaintext.toString("utf8")),
    index: Number(index),
    plaintext: plaintext.toString("utf8"),
    sync_id: syncId,
  };
}

test("Push hands Telegram one GBC1 envelope instead of any contribution POST", async (context) => {
  const fixture = await createBrowserFixture(context);
  const {
    contributionWrites,
    failedRequests,
    page,
    pageErrors,
    sessionRequests,
  } = fixture;
  await page.goto(
    "https://app.local/miniapp/index.html?launch=contributor-test&context=push",
    { waitUntil: "domcontentloaded" },
  );
  // The reply-keyboard launch lands directly on the contribution panel.
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bookmarks" &&
    document.querySelector("#contributor-manager")?.open === true
  ), undefined, { timeout: 15_000 });
  const localTopicId = await createPersonalTopicWithVerse(page);
  await openContributorManager(page);

  // Local edits are deliberately quiet. They mark the desired snapshot dirty
  // but cannot race the contributor's explicit transport request.
  await page.waitForTimeout(300);
  assert.equal(await page.evaluate(() => window.__telegramState.sentData.length), 0);

  const notificationStart = await page.evaluate(
    () => window.__telegramState.notifications.length,
  );
  await page.locator("#contributor-push-button").click();
  await page.waitForFunction(() => window.__telegramState.sentData.length > 0);
  // A single-chunk transfer must not fan out into further sendData calls.
  await page.waitForTimeout(300);
  const sentData = await page.evaluate(() => window.__telegramState.sentData);
  assert.equal(sentData.length, 1);
  const message = sentData[0];
  assert.match(message, /^GBC1\|/);
  assert.ok(new TextEncoder().encode(message).byteLength <= 4096);
  assert.equal(message.includes("KJV John 3 test verse"), false);

  const decoded = decodePushMessage(message);
  assert.equal(decoded.index, 1);
  assert.equal(decoded.count, 1);
  const envelope = decoded.envelope;
  assert.equal(envelope.protocol_version, 1);
  assert.equal(envelope.sync_id, decoded.sync_id);
  assert.match(envelope.sync_id, /^[A-Za-z0-9._:-]+$/);
  assert.match(envelope.client_id, /^[A-Za-z0-9._:-]+$/);
  assert.equal(envelope.disclosure_acknowledged, false);
  assert.deepEqual(envelope.operations, []);
  assert.deepEqual(envelope.snapshot.topics, [{
    id: localTopicId,
    name: "Community Hope",
    color: "#fde68a",
  }]);
  assert.deepEqual(envelope.snapshot.assignments, [{
    topic_id: localTopicId,
    book: 43,
    chapter: 3,
    verse: 2,
  }]);
  // The envelope carries topic and coordinate data only, never Scripture.
  assert.equal(decoded.plaintext.includes("KJV John 3 test verse"), false);

  assert.deepEqual(
    contributionWrites,
    [],
    "a push must never POST to the contributions HTTPS surface",
  );
  const notifications = await page.evaluate(
    () => window.__telegramState.notifications,
  );
  assert.equal(notifications.slice(notificationStart).includes("success"), true);

  assert.equal(sessionRequests.length, 1);
  assert.equal(sessionRequests[0].body.init_data, windowInitData());
  assert.equal(sessionRequests[0].headers.authorization, undefined);
  assert.equal(sessionRequests[0].headers["x-telegram-init-data"], undefined);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(failedRequests, []);
});

test("Pull refreshes status and the reviewed catalogue over session HTTPS", async (context) => {
  const fixture = await createBrowserFixture(context);
  const {
    contributionWrites,
    failedRequests,
    page,
    pageErrors,
    requestSequence,
    statusRequests,
  } = fixture;
  await page.goto("https://app.local/miniapp/index.html?launch=contributor-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector('[data-reader-verse="2"]', { timeout: 15_000 });
  await openContributorManager(page);
  // Let independent bootstrap catalogue work settle before measuring the
  // click, exactly one status read plus the strict catalogue refresh and the
  // add-all revalidation may follow it.
  await page.waitForTimeout(300);
  const sequenceStart = requestSequence.length;

  await page.locator("#contributor-pull-button").click();
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "success"
  ));

  const pullSequence = requestSequence.slice(sequenceStart);
  assert.deepEqual(
    pullSequence.filter((entry) => entry === "status"),
    ["status"],
  );
  assert.equal(statusRequests.length, 1);
  assert.equal(statusRequests[0].query, "?details=1");
  assert.equal(
    statusRequests[0].headers.authorization,
    "Bearer ContributorBrowserSession123",
  );
  assert.equal(statusRequests[0].headers["x-contribution-token"], undefined);
  // The strict pull refresh must reach the network; the follow-up add-all
  // load revalidates the same ETag with its own conditional request.
  assert.deepEqual(pullSequence, ["status", "catalog", "catalog"]);

  await page.waitForFunction(({ assignments, topics }) => (
    document.querySelector("#global-bookmark-status")?.textContent
      ?.includes(`${assignments} global verse links`) &&
    document.querySelector("#global-bookmark-status")?.textContent
      ?.includes(`${topics} topics`)
  ), { assignments: globalAssignmentCount, topics: globalTopicCount });
  assert.equal(
    await page.locator(".bookmark-group-card").count(),
    globalTopicCount,
  );
  assert.equal(
    await page.evaluate(() => window.__telegramState.notifications.includes("success")),
    true,
  );
  assert.equal(
    await page.evaluate(() => window.__telegramState.sentData.length),
    0,
  );
  assert.deepEqual(contributionWrites, []);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(failedRequests, []);
});

test("approved contributors keep the manager and a chat launch push points at the keyboard", async (context) => {
  const fixture = await createBrowserFixture(context);
  const {
    contributionWrites,
    failedRequests,
    page,
    pageErrors,
    requestSequence,
  } = fixture;
  await page.goto("https://app.local/miniapp/index.html?launch=contributor-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector('[data-reader-verse="2"]', { timeout: 15_000 });
  // Approval alone shows the contributor manager; no capability token exists
  // in the redesigned protocol, so nothing else gates the controls.
  await openContributorManager(page);
  assert.equal(await page.locator("#contributor-manager").isHidden(), false);
  assert.equal(await page.locator("#contributor-push-button").isVisible(), true);
  assert.equal(await page.locator("#contributor-pull-button").isVisible(), true);

  const sequenceStart = requestSequence.length;
  await page.locator("#contributor-push-button").click();
  // Outside the reply-keyboard launch sendData cannot reach the bot, so the
  // push button explains the keyboard instead of transferring anything.
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "pending" &&
    document.querySelector("#contributor-sync-status")?.textContent
      ?.includes("Push contribution")
  ));
  await page.waitForTimeout(200);
  assert.equal(
    await page.evaluate(() => window.__telegramState.sentData.length),
    0,
  );
  assert.deepEqual(requestSequence.slice(sequenceStart), []);
  assert.deepEqual(contributionWrites, []);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(failedRequests, []);
});

function windowInitData() {
  return "query_id=contributor-browser-test&user=%7B%22id%22%3A42%7D";
}
