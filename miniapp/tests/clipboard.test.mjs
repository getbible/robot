import assert from "node:assert/strict";
import test from "node:test";

import {
  ClipboardController,
  clipboardMessage,
  formatBasketForClipboard,
  writeClipboardPayload,
} from "../lib/clipboard.js";

function verse({
  translation = "kjv",
  bookNumber = 43,
  bookName = "John",
  chapter = 3,
  number = 16,
  text = "For God so loved the world.",
} = {}) {
  return {
    selection_id: `${translation}-${bookNumber}-${chapter}-${number}`,
    reference: `${bookName} ${chapter}:${number}`,
    translation,
    book_number: bookNumber,
    book_name: bookName,
    chapter,
    verse: number,
    text,
  };
}

test("formats one verse like the visible Telegram Scripture result", () => {
  const payload = formatBasketForClipboard([verse()]);

  assert.equal(
    payload.text,
    "John 3:16 kjv\n16. For God so loved the world.",
  );
  assert.match(
    payload.html,
    /href="https:\/\/getbible\.life\/kjv\/John\/3\/16"/,
  );
  assert.match(payload.html, /<strong>16\.<\/strong> For God so loved the world\./);
});

test("canonicalizes ranges, removes duplicates, and preserves translation runs", () => {
  const payload = formatBasketForClipboard([
    verse({ number: 17, text: "Verse seventeen." }),
    verse({ number: 16, text: "Verse sixteen." }),
    verse({ number: 17, text: "Duplicate seventeen." }),
    verse({
      translation: "aov",
      bookNumber: 19,
      bookName: "Psalms",
      chapter: 1,
      number: 1,
      text: "Welgeluksalig is die man.",
    }),
  ]);

  assert.equal(
    payload.text,
    "John 3:16-17 kjv\n" +
      "16. Verse sixteen.\n" +
      "17. Verse seventeen.\n\n" +
      "Psalms 1:1 aov\n" +
      "1. Welgeluksalig is die man.",
  );
});

test("retains Unicode and escapes rich clipboard markup", () => {
  const payload = formatBasketForClipboard([
    verse({
      translation: "cns",
      bookName: "约翰福音",
      text: "神爱世人 <恩典> & “真理”。",
    }),
  ]);

  assert.match(payload.text, /神爱世人 <恩典> & “真理”。/);
  assert.match(payload.html, /%E7%BA%A6%E7%BF%B0%E7%A6%8F%E9%9F%B3/);
  assert.match(payload.html, /&lt;恩典&gt; &amp; “真理”。/);
  assert.doesNotMatch(payload.html, /<恩典>/);
});

test("ignores malformed basket rows rather than copying browser-controlled data", () => {
  const payload = formatBasketForClipboard([
    verse(),
    { translation: "kjv", text: "Missing authoritative identifiers." },
    null,
  ]);

  assert.equal(
    payload.text,
    "John 3:16 kjv\n16. For God so loved the world.",
  );
});

test("uses the plain-text clipboard API when rich clipboard support is absent", async () => {
  let copied = "";
  const result = await writeClipboardPayload(
    { text: "John 3:16 kjv\n16. Text.", html: "<p>Text.</p>" },
    {
      navigatorObject: {
        clipboard: {
          writeText: async (value) => {
            copied = value;
          },
        },
      },
      documentObject: null,
      ClipboardItemCtor: undefined,
      BlobCtor: undefined,
    },
  );

  assert.equal(result, true);
  assert.equal(copied, "John 3:16 kjv\n16. Text.");
});

test("falls back to plain text when a WebView rejects rich clipboard writes", async () => {
  let richWrites = 0;
  let copied = "";
  const result = await writeClipboardPayload(
    { text: "John 3:16 kjv\n16. Text.", html: "<p>Text.</p>" },
    {
      navigatorObject: {
        clipboard: {
          write: async () => {
            richWrites += 1;
            throw new Error("rich clipboard unavailable");
          },
          writeText: async (value) => {
            copied = value;
          },
        },
      },
      documentObject: null,
      ClipboardItemCtor: class ClipboardItem {},
      BlobCtor: class Blob {},
    },
  );

  assert.equal(result, true);
  assert.equal(richWrites, 1);
  assert.equal(copied, "John 3:16 kjv\n16. Text.");
});

test("empty selections never overwrite the clipboard", async () => {
  let called = false;
  const result = await writeClipboardPayload(
    formatBasketForClipboard([]),
    {
      navigatorObject: {
        clipboard: {
          writeText: async () => {
            called = true;
          },
        },
      },
      documentObject: null,
    },
  );

  assert.equal(result, false);
  assert.equal(called, false);
});

test("the explicit clipboard controller reads only the current basket authority", async () => {
  const attributes = new Map();
  const button = {
    disabled: true,
    hidden: true,
    textContent: "",
    setAttribute(name, value) {
      attributes.set(name, value);
    },
  };
  const state = { basket: [verse()] };
  const writes = [];
  const notices = [];
  const toasts = [];
  let resetLabel = null;
  const controller = new ClipboardController({
    button,
    getItems: () => state.basket,
    write: async (payload) => {
      writes.push(payload);
      return Boolean(payload.text);
    },
    message: (key) => clipboardMessage(key, "en"),
    toast: (message) => toasts.push(message),
    notifySuccess: () => notices.push("success"),
    notifyError: () => notices.push("error"),
    setTimeoutImplementation: (callback) => {
      resetLabel = callback;
      return 1;
    },
    clearTimeoutImplementation: () => undefined,
  });

  controller.sync({ visible: true, disabled: false });
  assert.equal(button.hidden, false);
  assert.equal(button.disabled, false);
  assert.equal(attributes.get("aria-label"), "Copy selected verses");
  assert.equal(await controller.copy(), true);
  assert.equal(writes.length, 1);
  assert.match(writes[0].text, /^John 3:16 kjv/);
  assert.deepEqual(notices, ["success"]);
  assert.deepEqual(toasts, ["Selected verses copied."]);
  assert.equal(button.textContent, "Copied");

  resetLabel();
  assert.equal(button.textContent, "Copy selected verses");

  state.basket = [];
  controller.sync({ visible: false, disabled: true });
  assert.equal(button.hidden, true);
  assert.equal(button.disabled, true);
  assert.equal(await controller.copy(), false);
  assert.equal(writes.length, 1);
});
