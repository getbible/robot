import {
  BrowserPublicCache,
  publicCacheKey,
} from "./public-cache.js";
import {
  normalizeBooksPayload,
  normalizeChapterPayload,
  normalizeChaptersPayload,
  normalizeQueryTarget,
  normalizeTranslationCode,
  normalizeTranslationsPayload,
  withChapterNavigation,
} from "./getbible-model.js";
import {
  GetBibleTransport,
  PublicApiError,
} from "./getbible-transport.js";

const WEEK_MS = 7 * 24 * 60 * 60 * 1_000;

/**
 * Browser-side repository for public GetBible data.
 *
 * All normal reading/navigation requests terminate at the public API. The
 * robot remains responsible only for authenticated control-plane operations,
 * Librarian-backed search, and final authoritative posting.
 */
export class GetBibleApi {
  #cache;
  #inFlight = new Map();
  #now;
  #revalidateAfterMs;
  #transport;

  constructor({
    transport = new GetBibleTransport(),
    cache = new BrowserPublicCache(),
    now = Date.now,
    revalidateAfterMs = WEEK_MS,
  } = {}) {
    if (!transport || typeof transport.json !== "function") {
      throw new TypeError("A GetBible transport is required.");
    }
    if (!cache || typeof cache.get !== "function") {
      throw new TypeError("A public data cache is required.");
    }
    if (typeof now !== "function") {
      throw new TypeError("A public data clock is required.");
    }
    if (
      !Number.isInteger(revalidateAfterMs) ||
      revalidateAfterMs < 60_000 ||
      revalidateAfterMs > WEEK_MS
    ) {
      throw new RangeError("Public cache revalidation interval is invalid.");
    }
    this.#transport = transport;
    this.#cache = cache;
    this.#now = now;
    this.#revalidateAfterMs = revalidateAfterMs;
  }

  translations() {
    const key = publicCacheKey("translations");
    return this.#coalesce(key, async () => {
      const cached = await this.#cache.get(key);
      if (this.#fresh(cached)) {
        return cached.value;
      }
      const payload = await this.#transport.json("translations.json", {
        maximumBytes: 512 * 1024,
      });
      const translations = normalizeTranslationsPayload(payload);
      await this.#cache.put(key, translations, { checkedAt: this.#now() });
      return translations;
    });
  }

  books(translation) {
    const code = normalizeTranslationCode(translation);
    const key = publicCacheKey("books", code);
    return this.#coalesce(key, async () => {
      const cached = await this.#cache.get(key);
      if (this.#fresh(cached)) {
        return cached.value;
      }
      const validatorPath = `${code}.sha`;
      const validator = await this.#transport.sha(validatorPath);
      if (cached && cached.validator === validator) {
        await this.#cache.markChecked(key, validator, this.#now());
        return cached.value;
      }
      const { payload, stableValidator } = await this.#mappedScope(
        `${code}/books.json`,
        validatorPath,
        512 * 1024,
        validator,
      );
      const books = normalizeBooksPayload(payload, code);
      if (cached && cached.validator !== stableValidator) {
        await Promise.all([
          this.#cache.invalidatePrefix(publicCacheKey("chapters", code)),
          this.#cache.invalidatePrefix(publicCacheKey("chapter", code)),
        ]);
      }
      await this.#cache.put(key, books, {
        validator: stableValidator,
        checkedAt: this.#now(),
      });
      return books;
    });
  }

  chapters(translation, book) {
    const code = normalizeTranslationCode(translation);
    const number = boundedInteger(book, 1, 200, "Book number");
    const key = publicCacheKey("chapters", code, number);
    return this.#coalesce(key, async () => {
      const cached = await this.#cache.get(key);
      if (this.#fresh(cached)) {
        return cached.value;
      }
      const validatorPath = `${code}/${number}.sha`;
      const validator = await this.#transport.sha(validatorPath);
      if (cached && cached.validator === validator) {
        await this.#cache.markChecked(key, validator, this.#now());
        return cached.value;
      }

      let chapters;
      let stableValidator = validator;
      try {
        const mapped = await this.#mappedScope(
          `${code}/${number}/chapters.json`,
          validatorPath,
          512 * 1024,
          validator,
        );
        chapters = normalizeChaptersPayload(mapped.payload, {
          translation: code,
          book: number,
        });
        stableValidator = mapped.stableValidator;
      } catch (error) {
        if (!(error instanceof TypeError) && !(error instanceof PublicApiError)) {
          throw error;
        }
        const fallback = await this.#transport.consistentJson(
          `${code}/${number}.json`,
          validatorPath,
        );
        chapters = normalizeChaptersPayload(fallback.payload, {
          translation: code,
          book: number,
        });
        stableValidator = fallback.sha;
      }
      if (cached && cached.validator !== stableValidator) {
        await this.#cache.invalidatePrefix(
          publicCacheKey("chapter", code, number),
        );
      }
      await this.#cache.put(key, chapters, {
        validator: stableValidator,
        checkedAt: this.#now(),
      });
      return chapters;
    });
  }

  async chapter(translation, book, chapter, targetVerse = 1) {
    const code = normalizeTranslationCode(translation);
    const bookNumber = boundedInteger(book, 1, 200, "Book number");
    const chapterNumber = boundedInteger(chapter, 1, 1_000, "Chapter number");
    const target = boundedInteger(targetVerse, 1, 2_000, "Verse number");
    const key = publicCacheKey("chapter", code, bookNumber, chapterNumber);
    const scripture = await this.#coalesce(key, async () => {
      const cached = await this.#cache.get(key);
      if (this.#fresh(cached)) {
        return cached.value;
      }
      const base = `${code}/${bookNumber}/${chapterNumber}`;
      if (cached) {
        const validator = await this.#transport.sha(`${base}.sha`);
        if (cached.validator === validator) {
          await this.#cache.markChecked(key, validator, this.#now());
          return cached.value;
        }
      }
      const result = await this.#transport.consistentJson(
        `${base}.json`,
        `${base}.sha`,
      );
      const normalized = normalizeChapterPayload(result.payload, {
        translation: code,
        book: bookNumber,
        chapter: chapterNumber,
        targetVerse: 1,
        sha: result.sha,
      });
      await this.#cache.put(key, normalized, {
        validator: result.sha,
        checkedAt: this.#now(),
      });
      return normalized;
    });
    return this.#presentChapter(scripture, target);
  }

  async resolveReference(translation, references) {
    const code = normalizeTranslationCode(translation);
    const payload = await this.#transport.query(code, references, {
      maximumBytes: 1024 * 1024,
    });
    return normalizeQueryTarget(payload, code);
  }

  #fresh(cached) {
    return Boolean(
      cached &&
      this.#now() - cached.checkedAt < this.#revalidateAfterMs,
    );
  }

  #coalesce(key, operation) {
    const active = this.#inFlight.get(key);
    if (active) {
      return active;
    }
    const request = Promise.resolve()
      .then(operation)
      .finally(() => {
        if (this.#inFlight.get(key) === request) {
          this.#inFlight.delete(key);
        }
      });
    this.#inFlight.set(key, request);
    return request;
  }

  async #mappedScope(path, validatorPath, maximumBytes, firstValidator = null) {
    let before = firstValidator;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      before ??= await this.#transport.sha(validatorPath);
      const payload = await this.#transport.json(path, { maximumBytes });
      const after = await this.#transport.sha(validatorPath);
      if (before === after) {
        return { payload, stableValidator: after };
      }
      before = null;
    }
    throw new PublicApiError("GetBible mapping changed repeatedly during retrieval.", {
      code: "content_changed",
      retryable: true,
    });
  }

  async #presentChapter(scripture, targetVerse) {
    const translation = scripture.translation;
    const bookNumber = scripture.book.number;
    const chapterNumber = scripture.chapter;
    // A cold chapter costs a chain of public round trips, and these two owe
    // each other nothing. Running them in sequence made every reader wait one
    // catalogue read longer than the data required.
    const [books, chapters] = await Promise.all([
      this.books(translation),
      this.chapters(translation, bookNumber),
    ]);
    const navigation = await this.#navigation(
      books.items,
      chapters.items,
      translation,
      bookNumber,
      chapterNumber,
    );
    return withChapterNavigation(
      {
        ...scripture,
        target_verse: nearestVerse(
          scripture.items.map((item) => item.verse),
          targetVerse,
        ),
      },
      navigation,
    );
  }

  async #navigation(books, chapters, translation, bookNumber, chapterNumber) {
    const bookIndex = books.findIndex((item) => item.number === bookNumber);
    const chapterIndex = chapters.findIndex((item) => item.number === chapterNumber);
    if (bookIndex < 0 || chapterIndex < 0) {
      return { previous: null, next: null };
    }
    const currentBook = books[bookIndex];
    let previous = null;
    let next = null;
    if (chapterIndex > 0) {
      previous = location(currentBook, chapters[chapterIndex - 1]);
    } else if (bookIndex > 0) {
      const previousBook = books[bookIndex - 1];
      const previousChapters = await this.chapters(translation, previousBook.number);
      previous = location(previousBook, previousChapters.items.at(-1));
    }
    if (chapterIndex < chapters.length - 1) {
      next = location(currentBook, chapters[chapterIndex + 1]);
    } else if (bookIndex < books.length - 1) {
      const nextBook = books[bookIndex + 1];
      const nextChapters = await this.chapters(translation, nextBook.number);
      next = location(nextBook, nextChapters.items[0]);
    }
    return { previous, next };
  }
}

function location(book, chapter) {
  if (!book || !chapter) {
    return null;
  }
  return {
    book: book.number,
    book_name: book.name,
    chapter: chapter.number,
  };
}

function nearestVerse(available, requested) {
  return available.reduce((nearest, current) =>
    Math.abs(current - requested) < Math.abs(nearest - requested)
      ? current
      : nearest,
  available[0]);
}

function boundedInteger(value, minimum, maximum, label) {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isInteger(number) || number < minimum || number > maximum) {
    throw new TypeError(`${label} is invalid.`);
  }
  return number;
}

export { PublicApiError } from "./getbible-transport.js";
