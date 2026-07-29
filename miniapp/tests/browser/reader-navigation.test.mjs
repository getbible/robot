import assert from "node:assert/strict";
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

function fulfillJson(route, payload, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

test("reader navigation remains coherent in a real browser", async (context) => {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  const browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
  });
  context.after(() => browser.close());
  const page = await browser.newPage({
    viewport: { width: 390, height: 844 },
  });
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error));

  await page.addInitScript(() => {
    const telegramEvents = new Map();
    const emitTelegramEvent = (name, payload) => {
      for (const handler of telegramEvents.get(name) ?? []) {
        handler(payload);
      }
    };
    window.__telegramState = {
      expandCalls: 0,
      fullscreenCalls: 0,
      readyCalls: 0,
      emit: emitTelegramEvent,
    };
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
          const handlers = telegramEvents.get(name);
          handlers?.delete(handler);
          if (handlers?.size === 0) {
            telegramEvents.delete(name);
          }
        },
        isVersionAtLeast(version) {
          return version === "8.0";
        },
        expand() {
          window.__telegramState.expandCalls += 1;
        },
        requestFullscreen() {
          window.__telegramState.fullscreenCalls += 1;
          this.isFullscreen = true;
          emitTelegramEvent("fullscreenChanged");
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
  let pendingChapterRequest = null;
  let failNextScripture = 0;
  const preferenceWrites = [];
  const selectionsById = new Map();
  let serverBasket = [];
  let basketMutationCount = 0;
  let basketMutationGate = null;
  const basketPayload = () => ({
    items: [...serverBasket],
    count: serverBasket.length,
    maximum: 100,
  });
  const beforeBasketMutation = async () => {
    basketMutationCount += 1;
    const gate = basketMutationGate;
    if (gate?.requestNumber === basketMutationCount) {
      gate.started();
      await gate.promise;
      gate.completed();
    }
  };

  await page.route("https://telegram.org/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: "",
    }),
  );
  await page.route("https://app.local/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const apiPath = url.pathname.split("/api/v1/")[1];
    if (apiPath) {
      if (apiPath === "session") {
        return fulfillJson(route, {
          session_token: "BrowserTestSessionToken123",
          expires_in: 900,
          user: { id: 42 },
          preferences,
          entrypoint: { route: "bible", query: "" },
          translations: [
            {
              code: "kjv",
              name: "King James Version",
              language: "English",
              lang: "en",
              direction: "ltr",
            },
            {
              code: "aov",
              name: "Afrikaanse Ou Vertaling",
              language: "Afrikaans",
              lang: "af",
              direction: "ltr",
            },
            {
              code: "arb",
              name: "Arabic Bible",
              language: "Arabic",
              lang: "ar",
              direction: "rtl",
            },
          ],
          basket: { items: [], count: 0, maximum: 100 },
        }, 201);
      }
      if (apiPath === "preferences") {
        const update = request.postDataJSON();
        preferenceWrites.push(update);
        preferences = {
          translation: update.translation ?? preferences.translation,
          search_defaults:
            update.search_defaults ?? preferences.search_defaults,
          reader_location:
            update.reader_location === undefined
              ? preferences.reader_location
              : update.reader_location,
        };
        return fulfillJson(route, { preferences });
      }
      if (apiPath === "books") {
        const translation = url.searchParams.get("translation");
        return fulfillJson(route, {
          translation,
          items: bookNames.map((name, index) => ({
            number: index + 1,
            name:
              translation === "aov" && index === 18
                ? "Psalms Afrikaans"
                : translation === "arb" && index === 0
                  ? "التكوين"
                  : name,
            testament: index < 39 ? "old" : "new",
          })),
        });
      }
      if (apiPath === "chapters") {
        const translation = url.searchParams.get("translation");
        const book = Number(url.searchParams.get("book"));
        const pending =
          pendingChapterRequest?.book === book
            ? pendingChapterRequest
            : null;
        if (pending) {
          await pending.promise;
        }
        const count = book === 19 ? 150 : book === 43 ? 21 : 5;
        await fulfillJson(route, {
          translation,
          book: {
            number: book,
            name: bookNames[book - 1],
            testament: book <= 39 ? "old" : "new",
          },
          items: Array.from({ length: count }, (_, index) => ({
            number: index + 1,
            verses: Array.from({ length: 40 }, (_, verse) => verse + 1),
          })),
        });
        pending?.completed();
        return undefined;
      }
      if (apiPath === "scripture") {
        if (failNextScripture) {
          const status = failNextScripture;
          failNextScripture = 0;
          return fulfillJson(
            route,
            {
              error: {
                code: status === 401 ? "invalid_session_token" : "upstream",
                message: "Scripture temporarily unavailable.",
                retryable: status !== 401,
              },
            },
            status,
          );
        }
        const body = request.postDataJSON();
        const name =
          body.translation === "aov" && body.book === 19
            ? "Psalms Afrikaans"
            : bookNames[body.book - 1];
        const items = Array.from(
          { length: 40 },
          (_, index) => index + 1,
        ).map((verse) => ({
          selection_id:
            `Selection${body.book}${body.chapter}${verse}`.padEnd(18, "x"),
          translation: body.translation,
          reference: `${name} ${body.chapter}:${verse}`,
          book_number: body.book,
          book_name: name,
          chapter: body.chapter,
          verse,
          text: `${body.translation.toUpperCase()} ${name} text ${verse}`,
          terms: [],
        }));
        for (const item of items) {
          selectionsById.set(item.selection_id, item);
        }
        return fulfillJson(route, {
          translation: body.translation,
          book: {
            number: body.book,
            name,
            testament: body.book <= 39 ? "old" : "new",
          },
          chapter: body.chapter,
          reference: `${name} ${body.chapter}`,
          target_verse: body.verse,
          sha: "a".repeat(40),
          navigation: { previous: null, next: null },
          items,
        });
      }
      if (apiPath === "basket/items" && request.method() === "POST") {
        await beforeBasketMutation();
        const selection = selectionsById.get(
          request.postDataJSON().selection_id,
        );
        assert.ok(selection);
        if (
          !serverBasket.some(
            (item) => item.selection_id === selection.selection_id,
          )
        ) {
          serverBasket.push(selection);
        }
        return fulfillJson(route, basketPayload());
      }
      if (
        apiPath.startsWith("basket/items/") &&
        request.method() === "DELETE"
      ) {
        await beforeBasketMutation();
        const selectionId = decodeURIComponent(
          apiPath.slice("basket/items/".length),
        );
        serverBasket = serverBasket.filter(
          (item) => item.selection_id !== selectionId,
        );
        return fulfillJson(route, basketPayload());
      }
      if (apiPath === "basket/order" && request.method() === "PATCH") {
        await beforeBasketMutation();
        const order = request.postDataJSON().selection_ids;
        const byId = new Map(
          serverBasket.map((item) => [item.selection_id, item]),
        );
        serverBasket = order.map((selectionId) => byId.get(selectionId));
        assert.ok(serverBasket.every(Boolean));
        return fulfillJson(route, basketPayload());
      }
      if (apiPath === "basket" && request.method() === "DELETE") {
        await beforeBasketMutation();
        serverBasket = [];
        return route.fulfill({ status: 204, body: "" });
      }
      if (apiPath === "basket" && request.method() === "GET") {
        return fulfillJson(route, basketPayload());
      }
      return fulfillJson(route, { error: { code: "not_found" } }, 404);
    }

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
  });

  await page.goto(
    "https://app.local/miniapp/index.html?launch=browser-test",
    { waitUntil: "domcontentloaded" },
  );
  await page.locator("#bible-verses [data-reader-verse]").first().waitFor();
  assert.equal(await page.locator("#bible-reference").innerText(), "John 3");
  assert.deepEqual(
    await page.evaluate(() => ({
      expandCalls: window.__telegramState.expandCalls,
      fullscreenCalls: window.__telegramState.fullscreenCalls,
      readyCalls: window.__telegramState.readyCalls,
      safeTop: getComputedStyle(document.documentElement)
        .getPropertyValue("--bridge-content-safe-area-inset-top")
        .trim(),
      safeBottom: getComputedStyle(document.documentElement)
        .getPropertyValue("--bridge-content-safe-area-inset-bottom")
        .trim(),
    })),
    {
      expandCalls: 1,
      fullscreenCalls: 1,
      readyCalls: 1,
      safeTop: "48px",
      safeBottom: "34px",
    },
  );

  await page.locator("#bible-view").evaluate((node) => {
    node.scrollTop = 500;
    node.dispatchEvent(new Event("scroll"));
  });
  const bottomNav = page.locator("#bottom-nav");
  await bottomNav.evaluate((node) => {
    if (!node.classList.contains("is-collapsed")) {
      throw new Error("Bible navigation did not collapse while reading");
    }
  });
  assert.equal(
    await page.locator(".bottom-nav__items").evaluate(
      (node) => getComputedStyle(node).visibility,
    ),
    "hidden",
  );
  await page.locator("#bible-view").evaluate((node) => {
    node.scrollTop = 420;
    node.dispatchEvent(new Event("scroll"));
  });
  await page.mouse.move(389, 843);
  assert.equal(await bottomNav.getAttribute("class"), "bottom-nav is-collapsed");

  await page.locator('[data-reader-verse="30"]').click();
  assert.equal(await bottomNav.getAttribute("class"), "bottom-nav");
  await page.waitForFunction(
    () => document.querySelector('[data-reader-verse="30"]')
      ?.classList.contains("is-selected"),
  );
  await page.locator('[data-reader-verse="30"]').click();
  await page.waitForFunction(
    () => !document.querySelector('[data-reader-verse="30"]')
      ?.classList.contains("is-selected"),
  );
  await page.locator("#bible-view").evaluate((node) => {
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
    node.scrollTop = node.scrollHeight;
    node.dispatchEvent(new Event("scroll"));
  });
  assert.equal(await bottomNav.getAttribute("class"), "bottom-nav is-collapsed");
  await page.locator("#bottom-nav-handle").click();
  assert.equal(await bottomNav.getAttribute("class"), "bottom-nav");

  await page.locator("#bible-passage").click();
  const dialog = page.locator("#bible-navigation-dialog");
  assert.equal(await dialog.evaluate((node) => node.open), true);
  assert.ok(
    await page.locator(".sheet__surface--passage").evaluate(
      (node) => {
        const bounds = node.getBoundingClientRect();
        return bounds.width < window.innerWidth && bounds.left > 0;
      },
    ),
  );
  assert.equal(await page.locator("#bible-chapter-grid button").count(), 21);

  await page.locator("#bible-picker-back").click();
  assert.equal(await page.locator("#bible-book-grid button").count(), 66);
  await page.evaluate(() => new Promise(requestAnimationFrame));
  await page.locator('[data-bible-book="39"]').focus();
  await page.keyboard.press("ArrowDown");
  assert.equal(
    await page.evaluate(() => document.activeElement?.dataset?.bibleBook),
    "39",
  );
  await page.keyboard.press("Tab");
  assert.equal(
    await page.evaluate(() => document.activeElement?.dataset?.bibleBook),
    "43",
  );

  await page.locator('[data-bible-book="19"]').click();
  await page.locator('[data-bible-chapter="150"]').waitFor();
  assert.equal(
    await page.locator("#bible-navigation-title").innerText(),
    "Psalms · Choose a chapter",
  );
  await page.locator('[data-bible-chapter="150"]').click();
  await page.locator("#bible-reference").getByText("Psalms 150").waitFor();
  await page.locator("#bible-verses [data-reader-verse]").first().waitFor();
  assert.match(await page.locator("#bible-verses").innerText(), /KJV Psalms/);

  await page.locator("#bible-passage").click();
  await page.evaluate(() => window.__telegramBack());
  assert.equal(await page.locator("#bible-book-grid button").count(), 66);
  await page.evaluate(() => window.__telegramBack());
  assert.equal(await dialog.evaluate((node) => node.open), false);
  assert.equal(await page.evaluate(() => document.activeElement?.id), "bible-passage");

  await page.locator("#bible-passage").click();
  await page.keyboard.press("Escape");
  assert.equal(await dialog.evaluate((node) => node.open), false);
  assert.equal(await page.evaluate(() => document.activeElement?.id), "bible-passage");

  await page.locator("#bible-passage").click();
  await dialog.click({ position: { x: 3, y: 3 } });
  assert.equal(await dialog.evaluate((node) => node.open), false);

  let releaseChapter;
  let chapterRequestCompleted;
  const chapterCompletion = new Promise((resolvePromise) => {
    chapterRequestCompleted = resolvePromise;
  });
  pendingChapterRequest = {
    book: 20,
    promise: new Promise((resolvePromise) => {
      releaseChapter = resolvePromise;
    }),
    completed: chapterRequestCompleted,
  };
  await page.locator("#bible-passage").click();
  await page.evaluate(() => window.__telegramBack());
  await page.locator('[data-bible-book="20"]').click();
  await page.locator("#close-bible-navigation").click();
  releaseChapter();
  await chapterCompletion;
  assert.equal(await dialog.evaluate((node) => node.open), false);
  pendingChapterRequest = null;

  await page.locator("#translation-shortcut").click();
  assert.match(
    await page.locator("#close-translation").getAttribute("aria-label"),
    /^Close /,
  );
  assert.notEqual(
    await page.locator("#close-translation").getAttribute("aria-label"),
    await page.locator("#translation-shortcut").getAttribute("aria-label"),
  );
  await page.locator("#translation-select").selectOption("aov");
  await page.locator("#bible-reference").getByText("Psalms Afrikaans 150").waitFor();
  await page.locator("#bible-verses [data-reader-verse]").first().waitFor();
  assert.match(
    await page.locator("#bible-verses").innerText(),
    /AOV Psalms Afrikaans/,
  );
  assert.ok(
    preferenceWrites.some(
      (write) =>
        write.translation === "aov" &&
        write.reader_location?.book === 19 &&
        write.reader_location?.chapter === 150,
    ),
  );

  await page.locator("#translation-shortcut").click();
  await page.locator("#translation-select").selectOption("arb");
  await page.locator("#bible-reference").getByText("Psalms 150").waitFor();
  await page.locator("#bible-verses [data-reader-verse]").first().waitFor();
  assert.equal(await page.locator("html").getAttribute("dir"), "rtl");
  await page.locator("#bible-passage").click();
  assert.ok(
    await page.locator(".sheet__surface--passage").evaluate(
      (node) => node.getBoundingClientRect().left < 1,
    ),
  );
  await page.evaluate(() => window.__telegramBack());
  await page.evaluate(() => new Promise(requestAnimationFrame));
  assert.equal(
    await page.locator('[data-bible-book="1"]').getAttribute("aria-label"),
    "التكوين",
  );
  await page.locator('[data-bible-book="1"]').focus();
  await page.keyboard.press("ArrowLeft");
  assert.equal(
    await page.evaluate(() => document.activeElement?.dataset?.bibleBook),
    "2",
  );
  await page.evaluate(() => window.__telegramBack());

  for (const width of [320, 367, 368]) {
    await page.setViewportSize({ width, height: 844 });
    await page.locator("#bible-passage").click();
    const target = page.locator("#bible-chapter-grid button").first();
    assert.ok(
      await target.evaluate(
        (node) => node.getBoundingClientRect().height >= 44,
      ),
      `chapter target was too small at ${width}px`,
    );
    await page.locator("#close-bible-navigation").click();
  }
  await page.setViewportSize({ width: 390, height: 844 });

  let releaseMutation;
  let resolveMutationStarted;
  let resolveMutationCompleted;
  const mutationStarted = new Promise((resolvePromise) => {
    resolveMutationStarted = resolvePromise;
  });
  const mutationCompleted = new Promise((resolvePromise) => {
    resolveMutationCompleted = resolvePromise;
  });
  const firstMutationNumber = basketMutationCount + 1;
  basketMutationGate = {
    requestNumber: firstMutationNumber,
    promise: new Promise((resolvePromise) => {
      releaseMutation = resolvePromise;
    }),
    started: resolveMutationStarted,
    completed: resolveMutationCompleted,
  };
  await page.locator('[data-reader-verse="1"]').click();
  await page.locator('[data-reader-verse="2"]').click();
  await mutationStarted;
  await page.waitForTimeout(30);
  assert.equal(basketMutationCount, firstMutationNumber);
  releaseMutation();
  await mutationCompleted;
  await page.waitForFunction(
    () => document.querySelectorAll(
      "#bible-verses [data-reader-verse].is-selected",
    ).length === 2,
  );
  assert.equal(basketMutationCount, firstMutationNumber + 1);

  let releaseReorder;
  let resolveReorderStarted;
  const reorderStarted = new Promise((resolvePromise) => {
    resolveReorderStarted = resolvePromise;
  });
  const addBeforeReorder = basketMutationCount + 1;
  basketMutationGate = {
    requestNumber: addBeforeReorder,
    promise: new Promise((resolvePromise) => {
      releaseReorder = resolvePromise;
    }),
    started: resolveReorderStarted,
    completed() {},
  };
  await page.locator('[data-reader-verse="3"]').click();
  await reorderStarted;
  await page.locator('[data-route="selection"]').click();
  await page
    .locator("#selection-list [data-selection-action='up']")
    .last()
    .click();
  await page.waitForTimeout(30);
  assert.equal(basketMutationCount, addBeforeReorder);
  releaseReorder();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (basketMutationCount >= addBeforeReorder + 1) {
      break;
    }
    await page.waitForTimeout(10);
  }
  assert.equal(basketMutationCount, addBeforeReorder + 1);
  assert.equal(serverBasket.length, 3);

  let releaseClear;
  let resolveClearStarted;
  const clearStarted = new Promise((resolvePromise) => {
    resolveClearStarted = resolvePromise;
  });
  const clearMutationNumber = basketMutationCount + 1;
  basketMutationGate = {
    requestNumber: clearMutationNumber,
    promise: new Promise((resolvePromise) => {
      releaseClear = resolvePromise;
    }),
    started: resolveClearStarted,
    completed() {},
  };
  await page.locator("#clear-selection").click();
  await clearStarted;
  await page.locator('[data-route="bible"]').click();
  await page.locator('[data-reader-verse="4"]').click();
  await page.waitForTimeout(30);
  assert.equal(basketMutationCount, clearMutationNumber);
  releaseClear();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (basketMutationCount >= clearMutationNumber + 1) {
      break;
    }
    await page.waitForTimeout(10);
  }
  assert.equal(basketMutationCount, clearMutationNumber + 1);
  assert.equal(serverBasket.length, 1);
  assert.equal(serverBasket[0].verse, 4);
  await page.waitForFunction(
    () => document.querySelector(
      '#bible-verses [data-reader-verse="4"]',
    )?.classList.contains("is-selected"),
  );
  basketMutationGate = null;

  await page.locator("#bible-view").evaluate((node) => {
    const verse = node.querySelector('[data-reader-verse="30"]');
    node.scrollTop = verse.offsetTop;
    node.dispatchEvent(new Event("scroll"));
    window.dispatchEvent(new PageTransitionEvent("pagehide"));
  });
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (
      preferenceWrites.some(
        (write) =>
          write.reader_location?.translation === "arb" &&
          write.reader_location?.verse >= 25,
      )
    ) {
      break;
    }
    await page.waitForTimeout(25);
  }
  assert.ok(
    preferenceWrites.some(
      (write) =>
        write.reader_location?.translation === "arb" &&
        write.reader_location?.verse >= 25,
    ),
    JSON.stringify(preferenceWrites),
  );

  failNextScripture = 503;
  await page.locator("#bible-passage").click();
  await page.locator('[data-bible-chapter="149"]').click();
  await page.locator("#bible-state button").waitFor();
  await page.waitForFunction(
    () => document.activeElement?.closest("#bible-state")?.id === "bible-state",
  );
  assert.equal(
    await page.evaluate(() => document.activeElement?.closest("#bible-state")?.id),
    "bible-state",
  );

  await page.locator("#bible-passage").click();
  failNextScripture = 401;
  await page.locator('[data-bible-chapter="148"]').click();
  await page.locator("#access-denied").waitFor();
  await page.waitForFunction(
    () => document.activeElement?.id === "access-retry",
  );
  assert.equal(await page.evaluate(() => document.activeElement?.id), "access-retry");
  assert.equal(await dialog.evaluate((node) => node.open), false);
  assert.deepEqual(pageErrors, []);
});
