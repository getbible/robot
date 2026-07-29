const DEFAULT_WEB_BASE_URL = "https://getbible.life";
const SESSION_PATH = /\/api\/v1\/session\/?$/;
const BASKET_PATH = /\/api\/v1\/basket\/?$/;

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
    const canonical = [...run.items]
      .sort((left, right) =>
        left.book_number - right.book_number ||
        left.chapter - right.chapter ||
        left.verse - right.verse
      )
      .filter((item, index, values) =>
        index === 0 || clipboardIdentity(item) !== clipboardIdentity(values[index - 1])
      );
    const chapters = [];
    for (const item of canonical) {
      const key = `${item.book_number}:${item.chapter}`;
      const chapter = chapters.at(-1);
      if (!chapter || chapter.key !== key) {
        chapters.push({
          key,
          book_name: item.book_name,
          chapter: item.chapter,
          translation: item.translation,
          verses: [item],
        });
      } else {
        chapter.verses.push(item);
      }
    }

    for (const chapter of chapters) {
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

  try {
    if (
      typeof navigatorObject?.clipboard?.write === "function" &&
      typeof ClipboardItemCtor === "function" &&
      typeof BlobCtor === "function" &&
      payload.html
    ) {
      const item = new ClipboardItemCtor({
        "text/plain": new BlobCtor([payload.text], { type: "text/plain" }),
        "text/html": new BlobCtor([payload.html], { type: "text/html" }),
      });
      await navigatorObject.clipboard.write([item]);
      return true;
    }
    if (typeof navigatorObject?.clipboard?.writeText === "function") {
      await navigatorObject.clipboard.writeText(payload.text);
      return true;
    }
  } catch {
    // Continue to the synchronous WebView fallback below.
  }

  return copyWithTextarea(payload.text, documentObject);
}

function installClipboardEnhancements(windowObject, documentObject) {
  if (
    !windowObject ||
    !documentObject ||
    typeof windowObject.fetch !== "function"
  ) {
    return;
  }

  const originalFetch = windowObject.fetch.bind(windowObject);
  let latestBasket = [];
  let basketReady = false;
  let sessionToken = null;
  let cleanupAttempted = false;
  let copyButton = null;
  let labelTimer = null;

  const updateButton = () => {
    if (!copyButton) {
      return;
    }
    const postButton = documentObject.getElementById("post-selection");
    const hasSelection = basketReady && latestBasket.length > 0;
    copyButton.hidden = postButton?.hidden ?? !hasSelection;
    copyButton.disabled = !hasSelection || Boolean(postButton?.disabled);
    copyButton.setAttribute(
      "aria-label",
      hasSelection
        ? `Copy ${latestBasket.length} selected verse${latestBasket.length === 1 ? "" : "s"}`
        : "Copy selected verses",
    );
  };

  const setBasket = (candidate) => {
    if (!candidate || !Array.isArray(candidate.items)) {
      return;
    }
    latestBasket = candidate.items;
    basketReady = true;
    updateButton();
  };

  const attemptLaunchCleanup = (token) => {
    if (cleanupAttempted || typeof token !== "string" || token.length < 16) {
      return;
    }
    cleanupAttempted = true;
    const initData = windowObject.Telegram?.WebApp?.initData;
    if (typeof initData !== "string" || initData.length === 0) {
      return;
    }
    void originalFetch(new URL("api/v1/cleanup", documentObject.baseURI), {
      method: "POST",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${token}`,
        "X-Telegram-Init-Data": initData,
      },
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      keepalive: true,
    }).catch(() => undefined);
  };

  const inspectResponse = async (input, init, response) => {
    let url;
    try {
      const source = input instanceof Request ? input.url : String(input);
      url = new URL(source, documentObject.baseURI);
    } catch {
      return;
    }
    const method = String(
      init?.method ?? (input instanceof Request ? input.method : "GET"),
    ).toUpperCase();
    if (!response.ok) {
      return;
    }
    if (BASKET_PATH.test(url.pathname) && method === "DELETE" && response.status === 204) {
      latestBasket = [];
      basketReady = true;
      updateButton();
      return;
    }
    if (
      !SESSION_PATH.test(url.pathname) &&
      !url.pathname.includes("/api/v1/basket")
    ) {
      return;
    }
    const payload = await response.clone().json().catch(() => null);
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (SESSION_PATH.test(url.pathname)) {
      if (typeof payload.session_token === "string") {
        sessionToken = payload.session_token;
        attemptLaunchCleanup(sessionToken);
      }
      setBasket(payload.basket);
      return;
    }
    setBasket(payload.basket ?? payload);
  };

  windowObject.fetch = async (input, init) => {
    const response = await originalFetch(input, init);
    void inspectResponse(input, init, response);
    return response;
  };

  const showToast = (message) => {
    const region = documentObject.getElementById("toast-region");
    if (!region) {
      return;
    }
    const item = documentObject.createElement("div");
    item.className = "toast";
    item.textContent = message;
    region.replaceChildren(item);
    windowObject.setTimeout(() => {
      if (region.contains(item)) {
        region.replaceChildren();
      }
    }, 3600);
  };

  const installButton = () => {
    const postButton = documentObject.getElementById("post-selection");
    if (!postButton || documentObject.getElementById("copy-selection")) {
      return;
    }
    copyButton = documentObject.createElement("button");
    copyButton.id = "copy-selection";
    copyButton.type = "button";
    copyButton.className = "button button--secondary post-button";
    copyButton.textContent = "Copy selected verses";
    copyButton.disabled = true;
    copyButton.addEventListener("click", async () => {
      const payload = formatBasketForClipboard(latestBasket);
      const copied = await writeClipboardPayload(payload);
      if (!copied) {
        showToast("Unable to copy the selected verses.");
        windowObject.Telegram?.WebApp?.HapticFeedback?.notificationOccurred?.("error");
        return;
      }
      windowObject.Telegram?.WebApp?.HapticFeedback?.notificationOccurred?.("success");
      showToast("Selected verses copied.");
      copyButton.textContent = "Copied";
      if (labelTimer !== null) {
        windowObject.clearTimeout(labelTimer);
      }
      labelTimer = windowObject.setTimeout(() => {
        copyButton.textContent = "Copy selected verses";
        labelTimer = null;
      }, 1200);
      attemptLaunchCleanup(sessionToken);
    });
    postButton.insertAdjacentElement("beforebegin", copyButton);
    updateButton();

    const selectionList = documentObject.getElementById("selection-list");
    if (selectionList && typeof MutationObserver === "function") {
      const observer = new MutationObserver(updateButton);
      observer.observe(selectionList, { childList: true, subtree: true });
    }
  };

  if (documentObject.readyState === "loading") {
    documentObject.addEventListener("DOMContentLoaded", installButton, { once: true });
  } else {
    installButton();
  }
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
