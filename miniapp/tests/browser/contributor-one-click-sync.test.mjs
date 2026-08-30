import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const miniappRoot = resolve(fileURLToPath(new URL("../../", import.meta.url)));
const mainApiPattern = /^https:\/\/api\.getbible\.net\/v2\/.+/;
const queryApiPattern = /^https:\/\/query\.getbible\.net\/v2\/.+/;
const corsHeaders = { "access-control-allow-origin": "*" };
const acceptedTopic = Object.freeze({
  id: "steadfast-hope",
  name: "Steadfast Hope",
  color: "#fde68a",
  aliases: [],
});
const acceptedVerses = Object.freeze([2, 3, 4]);
const waitingVerse = 5;
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

async function waitForCondition(predicate, message, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) assert.fail(message);
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 10));
  }
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

function contributionSummary({ published = false, submitted = false } = {}) {
  return {
    topics: {
      pending: submitted && !published ? 1 : 0,
      mapped: published ? 1 : 0,
      published: published ? 1 : 0,
      rejected: 0,
      deferred: 0,
    },
    events: {
      pending: submitted && !published ? 5 : published ? 1 : 0,
      approved: published ? 4 : 0,
      rejected: 0,
      deferred: 0,
      applied: published ? 4 : 0,
    },
  };
}

function contributionStatus({ contributorState, localTopicId, published, submitted }) {
  const mappedTopic = localTopicId && published
    ? [{
        local_topic_id: localTopicId,
        // A later metadata edit may reopen review while the already-published
        // canonical topic and associations remain globally authoritative.
        state: "pending",
        published: true,
        canonical_topic_id: acceptedTopic.id,
        canonical_topic: acceptedTopic,
      }]
    : localTopicId && submitted
      ? [{
          local_topic_id: localTopicId,
          state: "pending",
          published: false,
        }]
      : [];
  return {
    enabled: contributorState !== "unavailable",
    state: contributorState,
    can_contribute: contributorState === "approved",
    disclosure_required: false,
    topics: mappedTopic,
    summary: contributionSummary({ published, submitted }),
  };
}

function catalogEnvelope(published) {
  return published
    ? {
        revision: 1,
        checksum: "a".repeat(64),
        catalog: {
          schema_version: 1,
          topics: [acceptedTopic],
          associations: {
            add: acceptedVerses.map((verse) => ({
              topic_id: acceptedTopic.id,
              book: 43,
              chapter: 3,
              verse,
            })),
            remove: [],
          },
        },
      }
    : {
        revision: 0,
        checksum: "0".repeat(64),
        catalog: {
          schema_version: 1,
          topics: [],
          associations: { add: [], remove: [] },
        },
      };
}

async function createBrowserFixture(
  context,
  { initialContributorState = "pending" } = {},
) {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  context.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(installTelegramMock);

  const pageErrors = [];
  const failedRequests = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.url()}: ${request.failure()?.errorText ?? "failed"}`);
  });

  let contributorState = initialContributorState;
  let statusPublished = false;
  let catalogPublished = false;
  let submitted = false;
  let denyNextEventRoute = false;
  let failNextEventRoute = false;
  let failNextStatusRoute = false;
  let failNextCatalogRoute = false;
  let revokeAfterNextEventRoute = false;
  let localTopicId = null;
  let receiptSequence = 0;
  const eventAttempts = [];
  const acceptedEvents = [];
  const catalogRequests = [];
  const requestSequence = [];
  const statusRequests = [];

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
          contributorState,
          localTopicId,
          published: statusPublished,
          submitted,
        }),
      }, 201);
    }
    if (apiPath === "cleanup") return fulfillJson(route, {});
    if (apiPath === "preferences") {
      return fulfillJson(route, { preferences: request.postDataJSON() });
    }
    if (apiPath === "basket") {
      return fulfillJson(route, { items: [], count: 0, maximum: 100 });
    }
    if (apiPath === "contributions/status") {
      requestSequence.push("status");
      statusRequests.push({
        method: request.method(),
        contributorState,
        details: url.searchParams.get("details"),
        published: statusPublished,
      });
      if (failNextStatusRoute) {
        failNextStatusRoute = false;
        return fulfillJson(route, {
          error: "contributions_unavailable",
          message: "Contributor status is temporarily unavailable.",
          retryable: true,
          retry_after: 0.01,
        }, 503);
      }
      return fulfillJson(route, contributionStatus({
        contributorState,
        localTopicId,
        published: statusPublished,
        submitted,
      }));
    }
    if (apiPath === "contributions/events") {
      requestSequence.push("events");
      const events = request.postDataJSON().events;
      eventAttempts.push(events);
      if (denyNextEventRoute) {
        denyNextEventRoute = false;
        contributorState = "revoked";
        failNextStatusRoute = true;
        return fulfillJson(route, {
          error: "contribution_not_allowed",
          message: "This Telegram user is not an approved contributor.",
        }, 403);
      }
      if (failNextEventRoute) {
        failNextEventRoute = false;
        return fulfillJson(route, {
          error: "not_found",
          message: "The contribution route is temporarily unavailable.",
          retryable: false,
        }, 404);
      }
      const customTopicEvent = events.find((event) =>
        event.topic?.name === "Community Hope"
      );
      localTopicId ??= customTopicEvent?.topic?.local_topic_id ?? null;
      submitted = true;
      acceptedEvents.push(...events);
      const eventIds = Object.fromEntries(events.map((event) => [
        event.client_event_id,
        ++receiptSequence,
      ]));
      if (revokeAfterNextEventRoute) {
        revokeAfterNextEventRoute = false;
        contributorState = "revoked";
      }
      return fulfillJson(route, {
        accepted: events.length,
        replayed: 0,
        event_ids: eventIds,
      });
    }
    if (apiPath === "bookmarks/catalog") {
      requestSequence.push("catalog");
      const requestEtag = request.headers()["if-none-match"] ?? null;
      catalogRequests.push({ published: catalogPublished, requestEtag });
      if (failNextCatalogRoute) {
        failNextCatalogRoute = false;
        return fulfillJson(route, {
          error: "upstream_error",
          message: "The live catalog route is temporarily unavailable.",
          retryable: true,
        }, 503);
      }
      const currentEtag = catalogPublished ? '"catalog-1"' : '"catalog-0"';
      if (requestEtag === currentEtag) {
        return route.fulfill({ status: 304, headers: { ETag: currentEtag } });
      }
      return fulfillJson(route, catalogEnvelope(catalogPublished), 200, {
        ETag: currentEtag,
      });
    }
    return fulfillJson(route, { error: { code: "not_found" } }, 404);
  });

  return {
    acceptedEvents,
    catalogRequests,
    eventAttempts,
    failedRequests,
    page,
    pageErrors,
    requestSequence,
    statusRequests,
    approve() { contributorState = "approved"; },
    denyNextEventRouteAndFailRecoveryStatus() { denyNextEventRoute = true; },
    failNextCatalogRoute() { failNextCatalogRoute = true; },
    failNextEventRoute() { failNextEventRoute = true; },
    revokeAfterNextEventRoute() { revokeAfterNextEventRoute = true; },
    publishCatalog() {
      assert.ok(localTopicId, "the submitted topic must exist before publication");
      catalogPublished = true;
    },
    publishStatus() {
      assert.ok(localTopicId, "the submitted topic must exist before publication");
      statusPublished = true;
    },
    localTopicId() { return localTopicId; },
  };
}

async function applyActiveTopicToVerse(page, verse) {
  await page.locator(`[data-reader-verse="${verse}"]`).click();
  await page.waitForFunction((expectedVerse) => (
    document.querySelector(
      `[data-bookmark-trigger="gbd_kjv_043_0003_${String(expectedVerse).padStart(4, "0")}"]`,
    )?.textContent === "•••"
  ), verse);
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

async function createPersonalTopicWithVerses(page, name, verses) {
  await openBookmarksRoute(page);
  if (!await page.locator("#bookmark-topic-manager").evaluate((manager) => manager.open)) {
    await page.locator("#bookmark-topic-manager summary").click();
  }
  await page.locator("#bookmark-topic-name").fill(name);
  await page.locator("#bookmark-topic-form button[type=submit]").click();
  const localTopicId = await page.locator(
    "#bookmark-topic-editor [data-topic-name]",
  ).evaluateAll((inputs, expectedName) => inputs.find((input) =>
    input.value === expectedName
  )?.dataset.topicName ?? null, name);
  assert.ok(localTopicId);

  await page.locator('[data-route="bible"]').click();
  await page.waitForSelector('#bible-verses [data-reader-verse="1"]');
  for (const verse of verses) {
    await applyActiveTopicToVerse(page, verse);
  }
  return localTopicId;
}

async function dispatchContributionRecheckEvents(page) {
  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("online"));
  });
  await page.waitForTimeout(100);
}

async function readContributionPersistence(page) {
  return page.evaluate(async () => ({
    databases: typeof indexedDB.databases === "function"
      ? (await indexedDB.databases()).map((database) => database.name)
      : [],
    localKeys: Object.keys(localStorage).filter((key) =>
      key.startsWith("getbible.miniapp.contributions.v1")
    ),
  }));
}

test("terminal contributor states create no traffic or journal", async (context) => {
  for (const contributorState of [
    "not_applied", "rejected", "revoked", "unavailable",
  ]) {
    const fixture = await createBrowserFixture(context, {
      initialContributorState: contributorState,
    });
    const { eventAttempts, page, statusRequests } = fixture;
    await page.goto("https://app.local/miniapp/index.html?launch=contributor-test", {
      waitUntil: "domcontentloaded",
    });
    await page.waitForSelector('#bible-verses [data-reader-verse="1"]', {
      timeout: 15_000,
    });
    await applyActiveTopicToVerse(page, 1);
    // Exercise every automatic recheck path available without waiting for the
    // periodic timer. An ineligible session is terminal: it must not probe
    // contribution routes or allocate private retry storage.
    await dispatchContributionRecheckEvents(page);
    assert.deepEqual(statusRequests, [], `${contributorState} polled contributor status`);
    assert.deepEqual(eventAttempts, [], `${contributorState} posted contribution events`);
    const presentation = await page.locator("#contributor-sync").evaluate((section) => ({
      buttonHidden: section.querySelector("#contributor-sync-button")?.hidden,
      hidden: section.hidden,
      status: section.querySelector("#contributor-sync-status")?.textContent,
    }));
    if (["not_applied", "unavailable"].includes(contributorState)) {
      assert.equal(presentation.hidden, true);
    } else {
      assert.equal(presentation.hidden, false);
      assert.equal(presentation.buttonHidden, true);
      assert.match(
        presentation.status,
        contributorState === "rejected" ? /not accepted|reject/i : /revok|no longer/i,
      );
    }
    const contributionPersistence = await readContributionPersistence(page);
    assert.equal(
      contributionPersistence.databases.includes("getbible-miniapp-contributions"),
      false,
    );
    assert.deepEqual(contributionPersistence.localKeys, []);
  }
});

test("a deferred application checks status only when the user asks", async (context) => {
  const fixture = await createBrowserFixture(context, {
    initialContributorState: "deferred",
  });
  const { eventAttempts, page, statusRequests } = fixture;
  await page.goto("https://app.local/miniapp/index.html?launch=contributor-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector('#bible-verses [data-reader-verse="1"]', {
    timeout: 15_000,
  });
  await applyActiveTopicToVerse(page, 1);
  await openBookmarksRoute(page);
  if (!await page.locator("#bookmark-topic-manager").evaluate((manager) => manager.open)) {
    await page.locator("#bookmark-topic-manager summary").click();
  }
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.hidden === false
  ));
  assert.match(await page.locator("#contributor-sync-status").innerText(), /defer/i);
  assert.equal(
    await page.locator("#contributor-sync-button").evaluate((button) => button.hidden),
    false,
  );
  assert.match(
    await page.locator("#contributor-sync-button").innerText(),
    /check status/i,
  );

  // Focus and reconnect must stay free: an unapproved applicant reaches the
  // contribution status route only through this deliberate button action.
  await dispatchContributionRecheckEvents(page);
  assert.deepEqual(statusRequests, []);
  await page.locator("#contributor-sync-button").click();
  await waitForCondition(
    () => statusRequests.length === 1,
    "deferred Check status did not make its one explicit request",
  );
  await dispatchContributionRecheckEvents(page);
  assert.equal(statusRequests.length, 1);
  assert.deepEqual(eventAttempts, []);
  const contributionPersistence = await readContributionPersistence(page);
  assert.equal(
    contributionPersistence.databases.includes("getbible-miniapp-contributions"),
    false,
  );
  assert.deepEqual(contributionPersistence.localKeys, []);
});

test("a final revoked status cannot be presented as a successful sync", async (context) => {
  const fixture = await createBrowserFixture(context);
  const {
    acceptedEvents,
    eventAttempts,
    page,
    requestSequence,
    statusRequests,
  } = fixture;
  await page.goto("https://app.local/miniapp/index.html?launch=contributor-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector('#bible-verses [data-reader-verse="1"]', {
    timeout: 15_000,
  });
  const localTopicId = await createPersonalTopicWithVerses(
    page,
    "Community Hope",
    [2],
  );
  assert.equal(eventAttempts.length, 0);

  await openBookmarksRoute(page);
  if (!await page.locator("#bookmark-topic-manager").evaluate((manager) => manager.open)) {
    await page.locator("#bookmark-topic-manager summary").click();
  }
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.hidden === false
  ));
  const requestStart = requestSequence.length;
  fixture.approve();
  fixture.revokeAfterNextEventRoute();
  await page.locator("#contributor-sync-button").click();
  await waitForCondition(
    () => eventAttempts.length === 1,
    "manual sync never uploaded the approved contribution",
  );
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.hidden === false &&
    document.querySelector("#contributor-sync")?.dataset.state === "error" &&
    document.querySelector("#contributor-sync-button")?.hidden === true
  ));

  assert.equal(fixture.localTopicId(), localTopicId);
  assert.equal(acceptedEvents.some((event) => (
    event.type === "topic_upsert" && event.topic.local_topic_id === localTopicId
  )), true);
  assert.equal(acceptedEvents.some((event) => (
    event.type === "verse_add" &&
    event.topic.local_topic_id === localTopicId &&
    event.verse.verse === 2
  )), true);
  assert.equal(statusRequests.at(-1)?.contributorState, "revoked");
  assert.equal(await page.locator("#contributor-sync").getAttribute("data-state"), "error");
  assert.match(
    await page.locator("#contributor-sync-status").innerText(),
    /revok|no longer/i,
  );
  assert.equal(
    await page.evaluate(() => window.__telegramState.notifications.includes("success")),
    false,
  );
  const manualOrder = requestSequence.slice(requestStart);
  const uploadIndex = manualOrder.indexOf("events");
  assert.ok(uploadIndex > 0, `unexpected manual request order: ${manualOrder}`);
  assert.equal(manualOrder.slice(0, uploadIndex).includes("status"), true);
  assert.equal(manualOrder.slice(uploadIndex + 1).includes("status"), true);

  const statusCountAfterRevocation = statusRequests.length;
  await dispatchContributionRecheckEvents(page);
  assert.equal(statusRequests.length, statusCountAfterRevocation);
  assert.equal(eventAttempts.length, 1);
});

test("a denied contribution POST automatically resumes after authority recovers", async (context) => {
  const fixture = await createBrowserFixture(context);
  const {
    acceptedEvents,
    eventAttempts,
    page,
    requestSequence,
    statusRequests,
  } = fixture;
  await page.goto("https://app.local/miniapp/index.html?launch=contributor-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector('#bible-verses [data-reader-verse="1"]', {
    timeout: 15_000,
  });
  await createPersonalTopicWithVerses(page, "Community Hope", [2]);
  await openBookmarksRoute(page);
  if (!await page.locator("#bookmark-topic-manager").evaluate((manager) => manager.open)) {
    await page.locator("#bookmark-topic-manager summary").click();
  }

  const firstRequest = requestSequence.length;
  fixture.approve();
  fixture.denyNextEventRouteAndFailRecoveryStatus();
  await page.locator("#contributor-sync-button").click();
  await waitForCondition(
    () => eventAttempts.length === 1,
    "manual sync never reached the denied contribution route",
  );
  await page.waitForFunction(() => {
    const section = document.querySelector("#contributor-sync");
    const button = document.querySelector("#contributor-sync-button");
    return section && !section.hidden && section.dataset.state === "error" &&
      button && !button.hidden && !button.disabled &&
      /check status/i.test(button.textContent ?? "");
  });

  assert.deepEqual(
    requestSequence.slice(firstRequest),
    ["status", "status", "events", "status"],
  );
  const deniedIds = eventAttempts[0].map((event) => event.client_event_id);
  assert.equal(statusRequests.at(-1)?.details, "1");
  const statusCountAfterRecoveryFailure = statusRequests.length;
  await dispatchContributionRecheckEvents(page);
  assert.equal(statusRequests.length, statusCountAfterRecoveryFailure);
  assert.equal(eventAttempts.length, 1);

  fixture.approve();
  await waitForCondition(
    () => eventAttempts.length === 2,
    "authority recovery did not automatically resume the preserved journal",
  );
  assert.deepEqual(
    eventAttempts[1].map((event) => event.client_event_id),
    deniedIds,
  );
  assert.equal(acceptedEvents.length, eventAttempts[1].length);
});

test("a normal user's personal core-topic bookmark remains personal with globals off", async (context) => {
  const fixture = await createBrowserFixture(context, {
    initialContributorState: "not_applied",
  });
  const { eventAttempts, page, statusRequests } = fixture;

  await page.goto("https://app.local/miniapp/index.html?launch=contributor-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector('#bible-verses [data-reader-verse="3"]', {
    timeout: 15_000,
  });

  // John 3:3 already exists in the bundled global catalog for Spiritual
  // Rebirth. The user has not enabled global topics, so their own assignment
  // at the same coordinate must retain personal provenance and remain visible.
  await applyActiveTopicToVerse(page, 3);
  await page.locator(
    '[data-bookmark-trigger="gbd_kjv_043_0003_0003"]',
  ).click();
  await page.waitForFunction(() => (
    !document.querySelector("#bookmark-popover")?.hidden
  ));
  await page.locator("#bookmark-topic-picker").selectOption("spiritual-rebirth");
  await page.waitForFunction(() => (
    document.querySelector(
      '#bookmark-assigned-topics [data-bookmark-source="personal"]' +
        '[data-bookmark-topic="spiritual-rebirth"]',
    )
  ));
  await page.locator("#close-bookmark-popover").click();

  await openBookmarksRoute(page);
  assert.match(await page.locator("#bookmarks-summary").innerText(), /^One personal verse$/);
  assert.equal(await page.locator("#global-bookmark-status").innerText(), "");

  await page.locator(
    '.bookmark-group-card[data-bookmark-topic="spiritual-rebirth"]',
  ).click();
  await page.waitForFunction(() => (
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === 1
  ));
  const displayedBookmark = await page.locator(
    "#bookmark-list .bookmark-list__item",
  ).evaluate((row) => ({
    contributionMarker: row.querySelector(".bookmark-contribution-badge")?.textContent ?? null,
    global: Boolean(row.querySelector(".bookmark-list__global-badge")),
    openAriaLabel: row.querySelector("[data-bookmark-open]")?.getAttribute("aria-label"),
    openId: row.querySelector("[data-bookmark-open]")?.dataset.bookmarkOpen,
  }));
  assert.equal(displayedBookmark.openId, "gbd_kjv_043_0003_0003");
  assert.equal(displayedBookmark.global, false);
  assert.equal(displayedBookmark.contributionMarker, null);
  assert.doesNotMatch(displayedBookmark.openAriaLabel, /global/i);

  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("online"));
  });
  await page.waitForTimeout(100);
  assert.equal(await page.locator("#contributor-sync").isHidden(), true);
  assert.deepEqual(statusRequests, []);
  assert.deepEqual(eventAttempts, []);
});

test("a contributor can retry one-click sync and receive published G mappings without reloading", async (context) => {
  const fixture = await createBrowserFixture(context);
  const {
    acceptedEvents,
    catalogRequests,
    eventAttempts,
    failedRequests,
    page,
    pageErrors,
    requestSequence,
    statusRequests,
  } = fixture;

  await page.goto("https://app.local/miniapp/index.html?launch=contributor-test", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForSelector('#bible-verses [data-reader-verse="1"]', {
    timeout: 15_000,
  });
  assert.equal(await page.evaluate(() => window.__telegramState.readyCalls), 1);

  // The contributor builds the local topic while their application is still
  // pending. No reload or special approval-time action is used below.
  await openBookmarksRoute(page);
  await page.locator("#bookmark-topic-manager summary").click();
  await page.locator("#bookmark-topic-name").fill("Community Hope");
  await page.locator("#bookmark-topic-form button[type=submit]").click();
  const localTopicId = await page.locator(
    "#bookmark-topic-editor [data-topic-name]",
  ).evaluateAll((inputs) => inputs.find((input) =>
    input.value === "Community Hope"
  )?.dataset.topicName ?? null);
  assert.ok(localTopicId);

  await page.locator('[data-route="bible"]').click();
  await page.waitForSelector('#bible-verses [data-reader-verse="2"]');
  for (const verse of [...acceptedVerses, waitingVerse]) {
    await applyActiveTopicToVerse(page, verse);
  }
  assert.equal(eventAttempts.length, 0, "a pending applicant must never POST events");
  await dispatchContributionRecheckEvents(page);
  assert.deepEqual(
    statusRequests,
    [],
    "a pending applicant must not be checked by focus or reconnect",
  );
  assert.equal(eventAttempts.length, 0);

  await openBookmarksRoute(page);
  if (!await page.locator("#bookmark-topic-manager").evaluate((manager) => manager.open)) {
    await page.locator("#bookmark-topic-manager summary").click();
  }
  await page.waitForFunction(() => {
    const section = document.querySelector("#contributor-sync");
    return section && !section.hidden && section.dataset.state === "pending";
  });
  assert.match(await page.locator("#contributor-sync-button").innerText(), /check status/i);
  assert.notEqual((await page.locator("#contributor-sync-status").innerText()).trim(), "");

  // Approval changes on the server while this exact Mini App instance remains
  // open. A missing upgraded route is recoverable from the same explicit action.
  fixture.approve();
  fixture.failNextEventRoute();
  await page.locator("#contributor-sync-button").click();
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "error"
  ));
  assert.equal(eventAttempts.length, 1);
  assert.match(
    await page.locator("#contributor-sync-status").innerText(),
    /failed|retry|try again|could not|unavailable/i,
  );
  assert.equal(await page.locator("#contributor-sync-button").isEnabled(), true);

  // The next click retries the durable baseline. The events route accepts the
  // complete upload before the strict live-catalog pull fails with a 503, so
  // the UI must distinguish reconciliation failure from upload failure.
  const catalogFailureOrderStart = requestSequence.length;
  fixture.failNextCatalogRoute();
  await page.locator("#contributor-sync-button").click();
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "error"
  ));
  assert.equal(eventAttempts.length, 2);
  assert.deepEqual(
    requestSequence.slice(catalogFailureOrderStart),
    ["status", "events", "status", "catalog"],
  );
  assert.match(
    await page.locator("#contributor-sync-status").innerText(),
    /catalog|refresh|reconciliation/i,
  );

  // A later click retries only status/catalog reconciliation. The accepted
  // durable baseline must never be resent because its catalog pull failed.
  const acceptedCountAfterCatalogFailure = acceptedEvents.length;
  await page.locator("#contributor-sync-button").click();
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "success"
  ));
  assert.equal(eventAttempts.length, 2);
  assert.equal(acceptedEvents.length, acceptedCountAfterCatalogFailure);
  assert.equal(fixture.localTopicId(), localTopicId);
  const submittedTopicCreates = acceptedEvents.filter((event) => (
    event.type === "topic_upsert" &&
    event.topic.local_topic_id === localTopicId &&
    event.topic.name === "Community Hope"
  ));
  const submittedVerseAdds = acceptedEvents.filter((event) => (
    event.type === "verse_add" && event.topic.local_topic_id === localTopicId
  ));
  assert.equal(submittedTopicCreates.length, 1);
  assert.deepEqual(
    submittedVerseAdds.map((event) => event.verse.verse).sort((left, right) => left - right),
    [...acceptedVerses, waitingVerse],
  );
  assert.equal(
    submittedVerseAdds.every((event) => !Object.hasOwn(event.verse, "text")),
    true,
  );
  assert.match(
    await page.locator("#contributor-sync-status").innerText(),
    /pending|review|waiting|sent|submitted/i,
  );
  assert.equal(
    await page.locator(
      `.bookmark-group-card[data-bookmark-topic="${localTopicId}"] ` +
        ".bookmark-contribution-badge",
    ).innerText(),
    "P",
  );
  assert.equal(
    await page.locator("#bookmark-topic-editor .bookmark-topic-editor__name--core")
      .filter({ hasText: "Steadfast Hope" }).count(),
    0,
  );

  // Review status can become visible before the independently cached catalog
  // revision arrives. A fresh but older catalog must not promote or suppress
  // personal links merely because status already says "published".
  fixture.publishStatus();
  const staleCatalogOrderStart = requestSequence.length;
  await page.locator("#contributor-sync-button").click();
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "success"
  ));
  assert.deepEqual(
    requestSequence.slice(staleCatalogOrderStart),
    ["status", "status", "catalog"],
  );
  assert.equal(catalogRequests.at(-1)?.published, false);
  assert.equal(
    await page.locator(
      `.bookmark-group-card[data-bookmark-topic="${localTopicId}"] ` +
        ".bookmark-contribution-badge",
    ).innerText(),
    "P",
  );
  assert.equal(
    await page.locator("#bookmark-topic-editor .bookmark-topic-editor__name--core")
      .filter({ hasText: "Steadfast Hope" }).count(),
    0,
  );

  // The following click receives the newer catalog and completes the pull
  // half of sync. Three reviewed links become G; the link still absent from
  // the catalog remains personal.
  fixture.publishCatalog();
  await page.locator("#contributor-sync-button").click();
  await page.waitForFunction((topicId) => {
    const row = document.querySelector(`[data-topic-editor="${topicId}"]`);
    return document.querySelector("#contributor-sync")?.dataset.state === "success" &&
      row?.querySelector(".bookmark-topic-editor__name--core")?.textContent ===
        "Steadfast Hope";
  }, localTopicId);
  assert.equal(
    await page.locator(`[data-topic-editor="${localTopicId}"] [data-topic-name]`).count(),
    0,
  );
  assert.match(
    await page.locator("#contributor-sync-status").innerText(),
    /accepted|global|published|synced|up to date/i,
  );

  const publishedTopicCard = page.locator(
    `.bookmark-group-card[data-bookmark-topic="${localTopicId}"]`,
  );
  assert.equal(
    await publishedTopicCard.locator(".bookmark-contribution-badge").innerText(),
    "G",
  );
  await publishedTopicCard.click();
  await page.waitForFunction(() => (
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === 4
  ));
  const displayedLinks = await page.locator("#bookmark-list .bookmark-list__item")
    .evaluateAll((rows) => rows.map((row) => ({
      global: Boolean(row.querySelector(".bookmark-list__global-badge")),
      marker: row.querySelector(".bookmark-contribution-badge")?.textContent,
      openId: row.querySelector("[data-bookmark-open]")?.dataset.bookmarkOpen,
      reference: row.querySelector(".bookmark-list__reference")?.textContent,
    })));
  for (const verse of acceptedVerses) {
    const link = displayedLinks.find((item) => item.openId ===
      `global_${acceptedTopic.id}_43_3_${verse}`
    );
    assert.equal(link?.global, true, `John 3:${verse} did not become global`);
  }
  const stillPersonal = displayedLinks.find((item) =>
    item.reference?.includes(`John 3:${waitingVerse}`)
  );
  assert.equal(stillPersonal?.global, false);
  assert.equal(stillPersonal?.marker, "P");
  assert.equal(displayedLinks.filter((item) => item.global).length, 3);

  // A user's explicit exclusion remains authoritative. Re-syncing a mapped
  // topic must neither re-enable the hidden global row nor reveal the latent
  // personal copy of that same coordinate.
  const hiddenGlobalId = `global_${acceptedTopic.id}_43_3_${acceptedVerses[0]}`;
  await page.locator(
    `#bookmark-list [data-bookmark-remove="${hiddenGlobalId}"]`,
  ).click();
  await page.waitForFunction((bookmarkId) => (
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === 3 &&
    !document.querySelector(`[data-bookmark-open="${bookmarkId}"]`)
  ), hiddenGlobalId);
  await page.locator("#bookmark-all-topics").click();
  const statusCountBeforeRepeatedSync = statusRequests.length;
  await page.locator("#contributor-sync-button").click();
  await waitForCondition(
    () => statusRequests.length >= statusCountBeforeRepeatedSync + 2,
    "repeated manual sync did not refresh its status",
  );
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "success"
  ));
  await page.locator(
    `.bookmark-group-card[data-bookmark-topic="${localTopicId}"]`,
  ).click();
  await page.waitForFunction((bookmarkId) => (
    document.querySelectorAll("#bookmark-list .bookmark-list__item").length === 3 &&
    document.querySelectorAll("#bookmark-list .bookmark-list__global-badge").length === 2 &&
    !document.querySelector(`[data-bookmark-open="${bookmarkId}"]`)
  ), hiddenGlobalId);

  // A status refresh alone is not enough to claim success. The explicit action
  // must remain visibly failed when its authoritative catalog pull fails.
  await page.locator("#bookmark-all-topics").click();
  fixture.failNextCatalogRoute();
  await page.locator("#contributor-sync-button").click();
  await page.waitForFunction(() => (
    document.querySelector("#contributor-sync")?.dataset.state === "error"
  ));
  assert.match(
    await page.locator("#contributor-sync-status").innerText(),
    /catalog|failed|refresh|try again|could not|unavailable/i,
  );

  assert.ok(statusRequests.length >= 4);
  assert.equal(
    statusRequests.filter((request) => request.method === "GET")
      .every((request) => request.details === "1"),
    true,
  );
  assert.equal(catalogRequests.some((request) => request.published), true);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(failedRequests, []);
});
