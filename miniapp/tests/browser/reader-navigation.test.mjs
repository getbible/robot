import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const miniappRoot = resolve(
  fileURLToPath(new URL("../../", import.meta.url)),
);
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

function fulfillJson(route, payload, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: jsonBody(payload),
  });
}

function chapterPayload(chapter) {
  return {
    abbreviation: "kjv",
    book_nr: 43,
    book_name: "John",
    chapter,
    name: `John ${chapter}`,
    testament: "new",
    verses: Array.from({ length: 40 }, (_, index) => ({
      verse: index + 1,
      text: `KJV John ${chapter} text ${index + 1}`,
    })),
  };
}

function chaptersPayload() {
  return Array.from({ length: 21 }, (_, index) => ({
    chapter: index + 1,
    verses: Array.from({ length: 40 }, (_, verse) => verse + 1),
  }));
}

function translationsPayload() {
  return [
    {
      abbreviation: "kjv",
      name: "King James Version (1769)",
      language: "English",
      lang: "en",
      direction: "ltr",
    },
    {
      abbreviation: "aov",
      name: "Afrikaanse Ou Vertaling",
      language: "Afrikaans",
      lang: "af",
      direction: "ltr",
    },
  ];
}

function booksPayload() {
  return bookNames.map((name, index) => ({
    nr: index + 1,
    name,
    testament: index < 39 ? "old" : "new",
  }));
}

function installTelegramMock() {
  const telegramEvents = new Map();
  const emit = (name, payload) => {
    for (const handler of telegramEvents.get(name) ?? []) {
      handler(payload);
    }
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
      BackButton: {
        onClick(handler) {
          window.__telegramBack = handler;
        },
        offClick() {
          window.__telegramBack = null;
        },
        show() {},
        hide() {},
      },
      HapticFeedback: {
        selectionChanged() {},
        notificationOccurred() {},
      },
      showConfirm(_message, callback) {
        callback(true);
      },
      onEvent(name, handler) {
        const handlers = telegramEvents.get(name) ?? new Set();
        handlers.add(handler);
        telegramEvents.set(name, handlers);
      },
      offEvent(name, handler) {
        telegramEvents.get(name)?.delete(handler);
      },
      isVersionAtLeast(version) {
        return version === "8.0";
      },
      expand() {},
      requestFullscreen() {
        this.isFullscreen = true;
        emit("fullscreenChanged");
      },
      enableVerticalSwipes() {},
      setHeaderColor() {},
      setBackgroundColor() {},
      setBottomBarColor() {},
      ready() {
        window.__telegramState.readyCalls += 1;
      },
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
  return route.fulfill({
    status: 200,
    contentType: mime,
    body: await readFile(file),
  });
}

test("reader navigation uses direct GetBible API calls in a real browser", async (context) => {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  context.after(() => browser.close());
  const page = await browser.newPage({
    viewport: { width: 390, height: 844 },
  });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(installTelegramMock);

  const publicRequests = [];
  const robotRequests = [];
  const chapterBodies = new Map();
  for (const chapter of [3, 4]) {
    const body = jsonBody(chapterPayload(chapter));
    chapterBodies.set(chapter, { body, sha: sha1(body) });
  }

  await page.route("https://telegram.org/**", (route) => route.fulfill({
    status: 200,
    contentType: "application/javascript",
    body: "",
  }));

  await page.route("https://api.getbible.net/v2/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/v2\//, "");
    publicRequests.push(path);

    if (path === "translations.json") {
      return fulfillJson(route, translationsPayload());
    }
    if (path === "kjv.sha") {
      return route.fulfill({ status: 200, body: "1".repeat(40) });
    }
    if (path === "kjv/books.json") {
      return fulfillJson(route, booksPayload());
    }
    if (path === "kjv/43.sha") {
      return route.fulfill({ status: 200, body: "2".repeat(40) });
    }
    if (path === "kjv/43/chapters.json") {
      return fulfillJson(route, chaptersPayload());
    }
    const shaMatch = /^kjv\/43\/(3|4)\.sha$/.exec(path);
    if (shaMatch) {
      return route.fulfill({
        status: 200,
        body: chapterBodies.get(Number(shaMatch[1])).sha,
      });
    }
    const jsonMatch = /^kjv\/43\/(3|4)\.json$/.exec(path);
    if (jsonMatch) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: chapterBodies.get(Number(jsonMatch[1])).body,
      });
    }
    return fulfillJson(route, { error: "not found" }, 404);
  });

  await page.route("https://query.getbible.net/v2/**", (route) => {
    publicRequests.push(new URL(route.request().url()).pathname);
    return fulfillJson(route, { error: "unexpected query request" }, 400);
  });

  let preferences = {
    translation: "kjv",
    search_defaults: {
      words: "all",
      match: "whole_word",
      scope: "bible",
      case_sensitive: false,
      diacritics: "sensitive",
      sort: "canonical",
    },
    reader_location: {
      translation: "kjv",
      book: 43,
      chapter: 3,
      verse: 1,
    },
  };

  await page.route("https://app.local/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const apiPath = url.pathname.split("/api/v1/")[1];
    if (!apiPath) {
      return serveStatic(route);
    }
    robotRequests.push(apiPath);
    assert.ok(
      !["translations", "books", "chapters", "scripture"].includes(apiPath),
      `public Bible data must not be proxied through Robot: ${apiPath}`,
    );
    if (apiPath === "session") {
      return fulfillJson(route, {
        session_token: "BrowserTestSessionToken123",
        expires_in: 900,
        preferences,
        entrypoint: { route: "bible", query: "" },
        basket: { items: [], count: 0, maximum: 100 },
      }, 201);
    }
    if (apiPath === "cleanup") {
      return route.fulfill({ status: 204, body: "" });
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
    if (apiPath === "basket") {
      return fulfillJson(route, { items: [], count: 0, maximum: 100 });
    }
    return fulfillJson(route, { error: { code: "not_found" } }, 404);
  });

  await page.goto(
    "https://app.local/miniapp/index.html?launch=browser-test",
    { waitUntil: "domcontentloaded" },
  );
  await page.waitForSelector('#bible-verses [data-reader-verse="1"]');

  assert.equal(await page.locator("#access-denied").isHidden(), true);
  assert.equal(await page.locator("#bible-reference").innerText(), "John 3");
  assert.equal(await page.locator("#bible-verse-count").innerText(), "40 verses");
  assert.equal(await page.locator("#bible-verses [data-reader-verse]").count(), 40);
  assert.equal(await page.evaluate(() => window.__telegramState.readyCalls), 1);

  await page.locator("#bible-next").click();
  await page.waitForFunction(() => (
    document.querySelector("#bible-reference")?.textContent === "John 4"
  ));
  assert.equal(await page.locator("#bible-reference").innerText(), "John 4");
  assert.match(
    await page.locator('[data-reader-verse="1"]').innerText(),
    /KJV John 4 text 1/,
  );

  assert.ok(publicRequests.includes("translations.json"));
  assert.ok(publicRequests.includes("kjv/books.json"));
  assert.ok(publicRequests.includes("kjv/43/chapters.json"));
  assert.ok(publicRequests.includes("kjv/43/3.json"));
  assert.ok(publicRequests.includes("kjv/43/4.json"));
  assert.deepEqual(
    robotRequests.filter((path) =>
      ["translations", "books", "chapters", "scripture"].includes(path)
    ),
    [],
  );
});
