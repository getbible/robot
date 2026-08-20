const DEFAULT_MAXIMUM_CONCURRENCY = 4;
const TRANSLATION_PATTERN = /^[a-z0-9][a-z0-9_-]{0,29}$/;
const TARGET_ID_PATTERN = /^[A-Za-z0-9_-]{1,192}$/;

/**
 * Resolves display-only verse text from the public chapter repository.
 *
 * Reading history and global bookmarks intentionally persist coordinates, not
 * copied Scripture. This resolver lets their views reuse the reader's existing
 * browser chapter repository while bounding concurrent cold-cache requests.
 * One failed chapter never prevents the remaining cards from rendering.
 */
export class ScriptureExcerptResolver {
  #activeLoads = 0;
  #inFlight = new Map();
  #loadChapter;
  #maximumConcurrency;
  #waiters = [];

  constructor({
    loadChapter,
    maximumConcurrency = DEFAULT_MAXIMUM_CONCURRENCY,
  } = {}) {
    if (typeof loadChapter !== "function") {
      throw new TypeError("A public Scripture chapter loader is required.");
    }
    if (
      !Number.isInteger(maximumConcurrency) ||
      maximumConcurrency < 1 ||
      maximumConcurrency > 12
    ) {
      throw new RangeError("Scripture excerpt concurrency is invalid.");
    }
    this.#loadChapter = loadChapter;
    this.#maximumConcurrency = maximumConcurrency;
  }

  async resolve(targets, { signal = null } = {}) {
    if (!Array.isArray(targets)) {
      throw new TypeError("Scripture excerpt targets must be an array.");
    }
    if (
      signal !== null &&
      (typeof signal !== "object" || typeof signal.aborted !== "boolean")
    ) {
      throw new TypeError("Scripture excerpt cancellation signal is invalid.");
    }
    const normalized = targets.map(normalizeTarget);
    return Promise.all(normalized.map(async (target) => {
      if (!target || signal?.aborted) {
        return null;
      }
      try {
        const verses = await this.#chapter(target, signal);
        if (signal?.aborted) {
          return null;
        }
        const verse = verses.find((candidate) =>
          candidate?.translation === target.translation &&
          candidate?.book_number === target.book &&
          candidate?.chapter === target.chapter &&
          candidate?.verse === target.verse &&
          typeof candidate?.text === "string" &&
          candidate.text.trim().length > 0
        );
        return verse
          ? {
            id: target.id,
            status: "ready",
            translation: target.translation,
            reference: verse.reference,
            book_name: verse.book_name,
            text: verse.text,
          }
          : {
            id: target.id,
            status: "unavailable",
          };
      } catch {
        return signal?.aborted
          ? null
          : {
            id: target.id,
            status: "error",
          };
      }
    }));
  }

  #chapter(target, signal) {
    const key = chapterKey(target);
    const active = this.#inFlight.get(key);
    if (active) {
      active.consumers.add(signal);
      return active.request;
    }
    const entry = {
      consumers: new Set([signal]),
      request: null,
    };
    // The public loader never inherits one view's AbortSignal. Cancellation is
    // checked while the request is still queued, however, so abandoned work is
    // skipped unless a fresh view has joined the same chapter request.
    const request = this.#withSlot(async () => {
      if (![...entry.consumers].some((consumer) => !consumer?.aborted)) {
        // Remove the doomed entry before rejecting. A fresh view arriving in
        // the rejection/finally microtask window must create a new request,
        // not join one that has already committed to cancellation.
        if (this.#inFlight.get(key) === entry) {
          this.#inFlight.delete(key);
        }
        throw cancellationError();
      }
      const verses = await this.#loadChapter({
        translation: target.translation,
        book: target.book,
        chapter: target.chapter,
        verse: target.verse,
      });
      if (!Array.isArray(verses)) {
        throw new TypeError("Public Scripture chapter verses are invalid.");
      }
      // The loader is the existing GetBibleApi chapter repository, which owns
      // the one bounded browser cache. Keep only in-flight coalescing here so
      // display hydration cannot create a competing Scripture cache.
      return verses.map((verse) => ({ ...verse }));
    }).finally(() => {
      if (this.#inFlight.get(key) === entry) {
        this.#inFlight.delete(key);
      }
    });
    entry.request = request;
    this.#inFlight.set(key, entry);
    return request;
  }

  async #withSlot(operation) {
    if (this.#activeLoads >= this.#maximumConcurrency) {
      await new Promise((resolve) => this.#waiters.push(resolve));
    }
    this.#activeLoads += 1;
    try {
      return await operation();
    } finally {
      this.#activeLoads -= 1;
      this.#waiters.shift()?.();
    }
  }
}

function normalizeTarget(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const id = typeof value.id === "string" ? value.id : "";
  const translation = typeof value.translation === "string"
    ? value.translation.toLowerCase()
    : "";
  const book = boundedInteger(value.book, 1, 200);
  const chapter = boundedInteger(value.chapter, 1, 1_000);
  const verse = boundedInteger(value.verse, 1, 2_000);
  if (
    !TARGET_ID_PATTERN.test(id) ||
    !TRANSLATION_PATTERN.test(translation) ||
    book === null ||
    chapter === null ||
    verse === null
  ) {
    return null;
  }
  return { id, translation, book, chapter, verse };
}

function boundedInteger(value, minimum, maximum) {
  const number = Number(value);
  return Number.isInteger(number) && number >= minimum && number <= maximum
    ? number
    : null;
}

function chapterKey(target) {
  return `${target.translation}:${target.book}:${target.chapter}`;
}

function cancellationError() {
  const error = new Error("Scripture excerpt resolution was cancelled.");
  error.name = "AbortError";
  return error;
}

export const SCRIPTURE_EXCERPT_MAXIMUM_CONCURRENCY =
  DEFAULT_MAXIMUM_CONCURRENCY;
