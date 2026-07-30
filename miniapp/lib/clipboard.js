import { UI_CATALOGS } from "./i18n.js";

const DEFAULT_WEB_BASE_URL = "https://getbible.life";
const COPY_MESSAGES = Object.freeze({
  "selection.copy": "Copy selected verses",
  "selection.copied": "Copied",
  "selection.copy_success": "Selected verses copied.",
  "selection.copy_failed": "Unable to copy the selected verses.",
});

export class ClipboardController {
  #button;
  #getItems;
  #format;
  #write;
  #message;
  #toast;
  #notifySuccess;
  #notifyError;
  #navigator;
  #document;
  #ClipboardItem;
  #Blob;
  #setTimeout;
  #clearTimeout;
  #labelTimer = null;

  constructor({
    button,
    getItems,
    format = formatBasketForClipboard,
    write = writeClipboardPayload,
    message = (key) => clipboardMessage(
      key,
      globalThis.document?.documentElement?.lang,
    ),
    toast = () => undefined,
    notifySuccess = () => undefined,
    notifyError = () => undefined,
    navigatorObject = globalThis.navigator,
    documentObject = globalThis.document,
    ClipboardItemCtor = globalThis.ClipboardItem,
    BlobCtor = globalThis.Blob,
    setTimeoutImplementation = globalThis.setTimeout,
    clearTimeoutImplementation = globalThis.clearTimeout,
  }) {
    if (!button || typeof getItems !== "function") {
      throw new TypeError("Clipboard controls and basket access are required.");
    }
    if (
      typeof format !== "function" ||
      typeof write !== "function" ||
      typeof message !== "function" ||
      typeof toast !== "function" ||
      typeof notifySuccess !== "function" ||
      typeof notifyError !== "function" ||
      typeof setTimeoutImplementation !== "function" ||
      typeof clearTimeoutImplementation !== "function"
    ) {
      throw new TypeError("Clipboard controller dependencies are invalid.");
    }
    this.#button = button;
    this.#getItems = getItems;
    this.#format = format;
    this.#write = write;
    this.#message = message;
    this.#toast = toast;
    this.#notifySuccess = notifySuccess;
    this.#notifyError = notifyError;
    this.#navigator = navigatorObject;
    this.#document = documentObject;
    this.#ClipboardItem = ClipboardItemCtor;
    this.#Blob = BlobCtor;
    this.#setTimeout = setTimeoutImplementation;
    this.#clearTimeout = clearTimeoutImplementation;
  }

  sync({ visible, disabled }) {
    this.#button.hidden = !visible;
    this.#button.disabled = !visible || Boolean(disabled);
    this.#button.setAttribute(
      "aria-label",
      this.#message("selection.copy"),
    );
    if (this.#labelTimer === null) {
      this.#button.textContent = this.#message("selection.copy");
    }
  }

  async copy() {
    if (this.#button.disabled) {
      return false;
    }
    const payload = this.#format(this.#getItems());
    const copied = await this.#write(payload, {
      navigatorObject: this.#navigator,
      documentObject: this.#document,
      ClipboardItemCtor: this.#ClipboardItem,
      BlobCtor: this.#Blob,
    });
    if (!copied) {
      this.#toast(this.#message("selection.copy_failed"));
      this.#notifyError();
      return false;
    }
    this.#notifySuccess();
    this.#toast(this.#message("selection.copy_success"));
    this.#button.textContent = this.#message("selection.copied");
    if (this.#labelTimer !== null) {
      this.#clearTimeout(this.#labelTimer);
    }
    this.#labelTimer = this.#setTimeout(() => {
      this.#button.textContent = this.#message("selection.copy");
      this.#labelTimer = null;
    }, 1200);
    return true;
  }

  destroy() {
    if (this.#labelTimer !== null) {
      this.#clearTimeout(this.#labelTimer);
      this.#labelTimer = null;
    }
  }
}

export function clipboardMessage(
  key,
  requestedLocale = "en",
  catalogs = UI_CATALOGS,
) {
  const requested = String(requestedLocale || "en").toLowerCase();
  const base = requested.split("-")[0];
  return catalogs[requested]?.[key] ??
    catalogs[base]?.[key] ??
    COPY_MESSAGES[key] ??
    key;
}

export function formatBasketForClipboard(
  rawItems,
  { webBaseUrl = DEFAULT_WEB_BASE_URL } = {},
) {
  const items = normalizeClipboardItems(rawItems);
  if (items.length === 0) {
    return { text: "", html: "" };
  }

  const translationRuns = [];
  for (const item of items) {
    const current = translationRuns.at(-1);
    if (!current || current.translation !== item.translation) {
      translationRuns.push({ translation: item.translation, items: [item] });
    } else {
      current.items.push(item);
    }
  }

  const textBlocks = [];
  const htmlBlocks = [];
  for (const run of translationRuns) {
    const seen = new Set();
    const chapters = [];
    const chaptersByKey = new Map();
    for (const item of run.items) {
      const identity = clipboardIdentity(item);
      if (seen.has(identity)) {
        continue;
      }
      seen.add(identity);
      const key = `${item.book_number}:${item.chapter}`;
      let chapter = chaptersByKey.get(key);
      if (!chapter) {
        chapter = {
          key,
          book_name: item.book_name,
          chapter: item.chapter,
          translation: item.translation,
          verses: [],
        };
        chaptersByKey.set(key, chapter);
        chapters.push(chapter);
      }
      chapter.verses.push(item);
    }

    for (const chapter of chapters) {
      chapter.verses.sort((left, right) => left.verse - right.verse);
      const range = verseRanges(chapter.verses.map((item) => item.verse));
      const label = `${chapter.book_name} ${chapter.chapter}:${range}`;
      const code = chapter.translation.toLowerCase();
      const lines = chapter.verses.map(
        (item) => `${item.verse}. ${item.text}`,
      );
      textBlocks.push(`${label} ${code}\n${lines.join("\n")}`);

      const href = [
        webBaseUrl.replace(/\/+$/, ""),
        pathSegment(code),
        pathSegment(chapter.book_name),
        pathSegment(chapter.chapter),
        pathSegment(range),
      ].join("/");
      const htmlLines = chapter.verses.map(
        (item) => `<strong>${item.verse}.</strong> ${escapeHtml(item.text)}`,
      );
      htmlBlocks.push(
        `<p><strong><a href="${escapeHtmlAttribute(href)}">` +
        `${escapeHtml(label)}</a></strong> <code>${escapeHtml(code)}</code>` +
        `<br>${htmlLines.join("<br>")}</p>`,
      );
    }
  }

  return {
    text: textBlocks.join("\n\n"),
    html: htmlBlocks.join("\n"),
  };
}

export async function writeClipboardPayload(
  payload,
  {
    navigatorObject = globalThis.navigator,
    documentObject = globalThis.document,
    ClipboardItemCtor = globalThis.ClipboardItem,
    BlobCtor = globalThis.Blob,
  } = {},
) {
  if (!payload?.text) {
    return false;
  }

  if (
    typeof navigatorObject?.clipboard?.write === "function" &&
    typeof ClipboardItemCtor === "function" &&
    typeof BlobCtor === "function" &&
    payload.html
  ) {
    try {
      const item = new ClipboardItemCtor({
        "text/plain": new BlobCtor([payload.text], { type: "text/plain" }),
        "text/html": new BlobCtor([payload.html], { type: "text/html" }),
      });
      await navigatorObject.clipboard.write([item]);
      return true;
    } catch {
      // Some Telegram WebViews expose rich writes but permit only plain text.
    }
  }
  if (typeof navigatorObject?.clipboard?.writeText === "function") {
    try {
      await navigatorObject.clipboard.writeText(payload.text);
      return true;
    } catch {
      // Continue to the synchronous WebView fallback below.
    }
  }

  return copyWithTextarea(payload.text, documentObject);
}

function normalizeClipboardItems(rawItems) {
  if (!Array.isArray(rawItems)) {
    return [];
  }
  return rawItems.flatMap((item) => {
    if (
      !item ||
      typeof item.translation !== "string" ||
      typeof item.book_name !== "string" ||
      !Number.isInteger(item.book_number) ||
      !Number.isInteger(item.chapter) ||
      !Number.isInteger(item.verse) ||
      typeof item.text !== "string" ||
      !item.translation.trim() ||
      !item.book_name.trim() ||
      !item.text.trim()
    ) {
      return [];
    }
    return [{
      translation: item.translation.trim().toLowerCase(),
      book_number: item.book_number,
      book_name: item.book_name.trim(),
      chapter: item.chapter,
      verse: item.verse,
      text: item.text.trim(),
    }];
  });
}

function clipboardIdentity(item) {
  return `${item.translation}:${item.book_number}:${item.chapter}:${item.verse}`;
}

function verseRanges(values) {
  const numbers = [...new Set(values)].sort((left, right) => left - right);
  const ranges = [];
  let start = numbers[0];
  let previous = numbers[0];
  for (const number of numbers.slice(1)) {
    if (number === previous + 1) {
      previous = number;
      continue;
    }
    ranges.push(start === previous ? String(start) : `${start}-${previous}`);
    start = number;
    previous = number;
  }
  ranges.push(start === previous ? String(start) : `${start}-${previous}`);
  return ranges.join(",");
}

function pathSegment(value) {
  return encodeURIComponent(String(value)).replace(
    /[!'()*]/g,
    (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeHtmlAttribute(value) {
  return escapeHtml(value);
}

function copyWithTextarea(text, documentObject) {
  if (
    !documentObject?.body ||
    typeof documentObject.createElement !== "function" ||
    typeof documentObject.execCommand !== "function"
  ) {
    return false;
  }
  const textarea = documentObject.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  textarea.style.pointerEvents = "none";
  documentObject.body.append(textarea);
  textarea.focus();
  textarea.select();
  let copied = false;
  try {
    copied = documentObject.execCommand("copy") === true;
  } catch {
    copied = false;
  }
  textarea.remove();
  return copied;
}
