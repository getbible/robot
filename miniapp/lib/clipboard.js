import { UI_CATALOGS } from "./i18n.js";

const DEFAULT_WEB_BASE_URL = "https://getbible.life";

let latestBasket = [];
let basketReady = false;
let copyButton = null;
let labelTimer = null;
let browserWindow = null;
let browserDocument = null;

export function setClipboardBasket(candidate) {
  if (!candidate || !Array.isArray(candidate.items)) {
    return false;
  }
  latestBasket = candidate.items;
  basketReady = true;
  updateButton();
  return true;
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

function installClipboardEnhancements(windowObject, documentObject) {
  if (!windowObject || !documentObject) {
    return;
  }
  browserWindow = windowObject;
  browserDocument = documentObject;

  const installButton = () => {
    const postButton = documentObject.getElementById("post-selection");
    if (!postButton || documentObject.getElementById("copy-selection")) {
      return;
    }
    copyButton = documentObject.createElement("button");
    copyButton.id = "copy-selection";
    copyButton.type = "button";
    copyButton.className = "button button--secondary post-button";
    copyButton.dataset.i18n = "selection.copy";
    copyButton.textContent = localizedMessage("selection.copy");
    copyButton.disabled = true;
    copyButton.addEventListener("click", () => void copySelection());
    postButton.insertAdjacentElement("beforebegin", copyButton);
    updateButton();

    const selectionList = documentObject.getElementById("selection-list");
    const Observer = windowObject.MutationObserver;
    if (selectionList && typeof Observer === "function") {
      const observer = new Observer(updateButton);
      observer.observe(selectionList, { childList: true, subtree: true });
    }
  };

  if (documentObject.readyState === "loading") {
    documentObject.addEventListener("DOMContentLoaded", installButton, { once: true });
  } else {
    installButton();
  }
}

async function copySelection() {
  if (!copyButton || !browserWindow || !browserDocument) {
    return;
  }
  const payload = formatBasketForClipboard(latestBasket);
  const copied = await writeClipboardPayload(payload, {
    navigatorObject: browserWindow.navigator,
    documentObject: browserDocument,
    ClipboardItemCtor: browserWindow.ClipboardItem,
    BlobCtor: browserWindow.Blob,
  });
  if (!copied) {
    showToast(localizedMessage("selection.copy_failed"));
    browserWindow.Telegram?.WebApp?.HapticFeedback?.notificationOccurred?.("error");
    return;
  }
  browserWindow.Telegram?.WebApp?.HapticFeedback?.notificationOccurred?.("success");
  showToast(localizedMessage("selection.copy_success"));
  copyButton.textContent = localizedMessage("selection.copied");
  if (labelTimer !== null) {
    browserWindow.clearTimeout(labelTimer);
  }
  labelTimer = browserWindow.setTimeout(() => {
    if (copyButton) {
      copyButton.textContent = localizedMessage("selection.copy");
    }
    labelTimer = null;
  }, 1200);
}

function updateButton() {
  if (!copyButton || !browserDocument) {
    return;
  }
  const postButton = browserDocument.getElementById("post-selection");
  const hasSelection = basketReady && latestBasket.length > 0;
  copyButton.hidden = !hasSelection || Boolean(postButton?.hidden);
  copyButton.disabled = !hasSelection || Boolean(postButton?.disabled);
  copyButton.setAttribute("aria-label", localizedMessage("selection.copy"));
}

function showToast(message) {
  if (!browserWindow || !browserDocument) {
    return;
  }
  const region = browserDocument.getElementById("toast-region");
  if (!region) {
    return;
  }
  const item = browserDocument.createElement("div");
  item.className = "toast";
  item.textContent = message;
  region.replaceChildren(item);
  browserWindow.setTimeout(() => {
    if (region.contains(item)) {
      region.replaceChildren();
    }
  }, 3600);
}

function localizedMessage(key) {
  const requested = String(
    browserDocument?.documentElement.lang || "en",
  ).toLowerCase();
  const base = requested.split("-")[0];
  return UI_CATALOGS[requested]?.[key] ??
    UI_CATALOGS[base]?.[key] ??
    UI_CATALOGS.en?.[key] ??
    key;
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

if (typeof window !== "undefined" && typeof document !== "undefined") {
  installClipboardEnhancements(window, document);
}
