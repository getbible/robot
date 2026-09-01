import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import readline from "node:readline";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

/**
 * The real client against the real server. Every other browser test answers
 * the API with mocks, and every server test sends hand-written batches, so a
 * genuine Mini App had never synchronized against a genuine MiniAppApi with a
 * genuine SQLite contribution store. This test closes that gap: Playwright
 * only intercepts the public origin (the server insists on an HTTPS origin)
 * and forwards each request byte-for-byte to the Python process started by
 * tests/support/real_miniapp_server.py.
 */

const repoRoot = resolve(fileURLToPath(new URL("../../../", import.meta.url)));
const serverScript = resolve(repoRoot, "tests/support/real_miniapp_server.py");
const publicOrigin = "https://app.local";
const publicPath = "/getbible";
const mainApiPattern = /^https:\/\/api\.getbible\.net\/v2\/.+/;
const queryApiPattern = /^https:\/\/query\.getbible\.net\/v2\/.+/;
const corsHeaders = { "access-control-allow-origin": "*" };

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

function installTelegramMock(initData) {
  const handlers = new Map();
  const emit = (name, payload) => {
    for (const handler of handlers.get(name) ?? []) handler(payload);
  };
  window.__telegramState = { alerts: [], notifications: [], readyCalls: 0 };
  window.Telegram = {
    WebApp: {
      initData,
      initDataUnsafe: {},
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
      // Mirror the real SDK: showAlert/showConfirm route through showPopup,
      // which throws synchronously for a message outside 1-256 characters
      // instead of ever calling back.
      showAlert(message, callback) {
        if (typeof message !== "string" || message.trim().length > 256) {
          throw new Error("WebAppPopupParamInvalid");
        }
        window.__telegramState.alerts.push(message);
        callback();
      },
      showConfirm(message, callback) {
        if (typeof message !== "string" || message.trim().length > 256) {
          throw new Error("WebAppPopupParamInvalid");
        }
        callback(true);
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

function fulfillJson(route, payload, status = 200, headers = {}) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers,
    body: JSON.stringify(payload),
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

function pythonExecutable() {
  if (process.env.GETBIBLE_PYTHON) return process.env.GETBIBLE_PYTHON;
  const venv = resolve(repoRoot, "venv/bin/python");
  return existsSync(venv) ? venv : "python3";
}

async function startRealServer(context, args = []) {
  const child = spawn(pythonExecutable(), [serverScript, ...args], {
    cwd: repoRoot,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const exchanges = [];
  const stderrLines = [];
  readline.createInterface({ input: child.stderr }).on("line", (line) => {
    try {
      exchanges.push(JSON.parse(line));
    } catch {
      stderrLines.push(line);
    }
  });
  const stdout = readline.createInterface({ input: child.stdout });
  const [firstLine] = await Promise.race([
    once(stdout, "line"),
    once(child, "exit").then(() => {
      throw new Error(`real server exited early:\n${stderrLines.join("\n")}`);
    }),
  ]);
  const descriptor = JSON.parse(firstLine);
  context.after(() => {
    child.kill("SIGTERM");
  });
  return { ...descriptor, exchanges, stderrLines };
}

async function proxyToRealServer(route, port) {
  const request = route.request();
  const url = new URL(request.url());
  const target = `http://127.0.0.1:${port}${url.pathname}${url.search}`;
  const headers = {};
  for (const [name, value] of Object.entries(request.headers())) {
    if (["host", "content-length", "connection", "accept-encoding"].includes(name)) {
      continue;
    }
    headers[name] = value;
  }
  const body = request.postDataBuffer();
  const response = await fetch(target, {
    method: request.method(),
    headers,
    body: body ?? undefined,
    redirect: "manual",
  });
  const responseHeaders = {};
  response.headers.forEach((value, name) => {
    if (!["content-encoding", "content-length", "transfer-encoding", "connection"].includes(name)) {
      responseHeaders[name] = value;
    }
  });
  return route.fulfill({
    status: response.status,
    headers: responseHeaders,
    body: Buffer.from(await response.arrayBuffer()),
  });
}

async function createBrowserFixture(context, serverArgs = []) {
  const server = await startRealServer(context, serverArgs);
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  context.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(installTelegramMock, server.init_data);

  const pageErrors = [];
  const consoleMessages = [];
  const failedRequests = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (["error", "warning"].includes(message.type())) {
      consoleMessages.push(`${message.type()}: ${message.text()}`);
    }
  });
  page.on("requestfailed", (request) => {
    const errorText = request.failure()?.errorText ?? "failed";
    const pathname = new URL(request.url()).pathname;
    // Chromium reports a route-fulfilled body-less response (the keepalive
    // cleanup 204 and the catalogue's conditional 304) as ERR_ABORTED even
    // though the server answered; the exchange log shows the real status.
    if (
      errorText === "net::ERR_ABORTED" &&
      (pathname.endsWith("/api/v1/cleanup") || pathname.endsWith("/api/v1/bookmarks/catalog"))
    ) {
      return;
    }
    failedRequests.push(`${request.url()}: ${errorText}`);
  });

  const chapterBody = JSON.stringify(chapterPayload());
  const chapterSha = createHash("sha1").update(chapterBody, "utf8").digest("hex");
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
  await page.route(`${publicOrigin}/**`, (route) => proxyToRealServer(route, server.port));

  return {
    consoleMessages,
    failedRequests,
    page,
    pageErrors,
    server,
    async storeState() {
      const response = await fetch(`http://127.0.0.1:${server.port}/__harness/state`);
      return response.json();
    },
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

/** Agree to the in-app disclosure when a first sync presents it. */
async function acceptDisclosureIfShown(page) {
  const accept = page.locator("#contributor-disclosure[open] #contributor-disclosure-accept");
  try {
    await accept.waitFor({ state: "visible", timeout: 3_000 });
  } catch {
    return false;
  }
  await accept.click();
  return true;
}

async function panelReport(page) {
  return page.evaluate(() => {
    const panel = document.querySelector("#contributor-sync");
    return {
      state: panel?.dataset.state ?? null,
      hidden: panel?.hidden ?? null,
      status: document.querySelector("#contributor-sync-status")?.textContent?.trim() ?? null,
      details: document.querySelector("#contributor-sync-details")?.textContent?.trim() ?? null,
      alerts: window.__telegramState.alerts,
      notifications: window.__telegramState.notifications,
    };
  });
}

function describeExchanges(exchanges) {
  return exchanges
    .map((entry) => `${entry.method} ${entry.target} -> ${entry.status}\n  req: ${entry.request_body.slice(0, 300)}\n  res: ${entry.response_body.slice(0, 300)}`)
    .join("\n");
}

test("a store that refuses every token write is reported as a server fault, not a generic failure", async (context) => {
  const fixture = await createBrowserFixture(context, ["--acknowledged", "--fail-token-issuance"]);
  const { page, server } = fixture;
  await page.goto(`${publicOrigin}${publicPath}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#app[data-active-route]", { timeout: 30_000 });
  await page.locator('[data-route="bible"]').click();
  await page.waitForSelector('[data-reader-verse="2"]', { timeout: 30_000 });
  await createPersonalTopicWithVerse(page);
  await openContributorManager(page);
  const before = await panelReport(page);
  // The read path says approved, so the panel is visible and actionable.
  assert.equal(before.hidden, false);

  await page.locator("#contributor-sync-button").click();
  await page.waitForFunction(() => (
    ["success", "error", "pending"].includes(
      document.querySelector("#contributor-sync")?.dataset.state ?? "",
    )
  ), undefined, { timeout: 60_000 });
  const after = await panelReport(page);
  const stored = await fixture.storeState();
  const report = [
    `panel after: ${JSON.stringify(after)}`,
    "exchanges:",
    describeExchanges(server.exchanges),
  ].join("\n");

  assert.equal(after.state, "error", report);
  assert.match(after.status, /could not issue a contributor token/, report);
  assert.equal(stored.event_count, 0, report);
  // The client tried exactly one token recovery (a status request) before
  // reporting; it never posted a batch without a token.
  const statusRequests = server.exchanges.filter((entry) =>
    entry.target.includes("contributions/status")
  );
  const eventPosts = server.exchanges.filter((entry) =>
    entry.target.includes("contributions/events")
  );
  assert.ok(statusRequests.length >= 1, report);
  assert.equal(eventPosts.length, 0, report);
  assert.deepEqual(fixture.pageErrors, []);
});

for (const scenario of [
  { name: "first ever sync (disclosure not yet acknowledged)", args: [] },
  { name: "returning contributor (disclosure acknowledged)", args: ["--acknowledged"] },
  {
    name: "store that lost its capabilities table at the current version, reopened",
    args: ["--acknowledged", "--damaged-then-reopened"],
  },
]) {
  test(`a real approved contributor's Sync tap reaches the real store: ${scenario.name}`, async (context) => {
    const fixture = await createBrowserFixture(context, scenario.args);
    const { page, server } = fixture;
    const diagnostics = async (extra = {}) => [
      `scenario: ${scenario.name}`,
      ...Object.entries(extra).map(([key, value]) => `${key}: ${JSON.stringify(value)}`),
      `gate: ${JSON.stringify(await page.evaluate(() => ({
        route: document.querySelector("#app")?.dataset.activeRoute ?? null,
        boot: document.querySelector("#boot-message")?.textContent?.trim() ?? null,
        bootHidden: document.querySelector("#boot-screen")?.hidden ?? null,
        denied: document.querySelector("#access-denied")?.hidden ?? null,
        deniedText: document.querySelector("#access-denied")?.textContent?.replace(/\s+/g, " ").trim().slice(0, 300) ?? null,
      })).catch(() => null))}`,
      `page errors: ${JSON.stringify(fixture.pageErrors)}`,
      `console: ${JSON.stringify(fixture.consoleMessages)}`,
      `failed requests: ${JSON.stringify(fixture.failedRequests)}`,
      `server stderr: ${JSON.stringify(server.stderrLines)}`,
      "exchanges:",
      describeExchanges(server.exchanges),
    ].join("\n");

    try {
      await page.goto(`${publicOrigin}${publicPath}/`, { waitUntil: "domcontentloaded" });
      // A generic menu-button launch lands on Home, exactly like production.
      await page.waitForSelector("#app[data-active-route]", { timeout: 30_000 });
      await page.locator('[data-route="bible"]').click();
      await page.waitForSelector('[data-reader-verse="2"]', { timeout: 30_000 });
      const localTopicId = await createPersonalTopicWithVerse(page);
      await openContributorManager(page);
      const before = await panelReport(page);

      await page.locator("#contributor-sync-button").click();
      const disclosureShown = await acceptDisclosureIfShown(page);
      await page.waitForFunction(() => (
        ["success", "error", "pending"].includes(
          document.querySelector("#contributor-sync")?.dataset.state ?? "",
        )
      ), undefined, { timeout: 60_000 });
      const after = await panelReport(page);
      const stored = await fixture.storeState();
      const report = await diagnostics({
        panelBefore: before,
        panelAfter: after,
        disclosureShown,
        store: { event_count: stored.event_count, event_types: stored.event_types },
      });
      console.log(report);

      // The in-app disclosure appears exactly when the server still requires
      // it, and never as a Telegram popup.
      assert.equal(disclosureShown, !scenario.args.includes("--acknowledged"), report);
      assert.deepEqual(after.alerts, [], report);
      assert.equal(after.state, "success", report);
      assert.deepEqual(stored.event_types, ["topic_upsert", "verse_add"], report);
      assert.ok(localTopicId);
      assert.deepEqual(fixture.pageErrors, []);
      assert.deepEqual(fixture.failedRequests, []);
    } catch (error) {
      console.log(await diagnostics({ failure: String(error?.message ?? error) }));
      throw error;
    }
  });
}
