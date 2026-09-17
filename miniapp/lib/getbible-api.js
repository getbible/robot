import {
  BrowserPublicCache,
  publicCacheKey,
} from "./public-cache.js";
import {
  normalizeBooksPayload,
  normalizeChapterPayload,
  normalizeChaptersPayload,
  normalizeQueryTarget,
  normalizeSearchPayload,
  normalizeTranslationCode,
  normalizeTranslationsPayload,
  withChapterNavigation,
} from "./getbible-model.js";
import {
  GetBibleTransport,
  PublicApiError,
} from "./getbible-transport.js";

const WEEK_MS = 7 * 24 * 60 * 60 * 1_000;
// The Search API's own request contract. The page asks for a quarter of the
// largest page the API allows, so a phone renders a result set in steps
// rather than waiting on one hundred verses at once.
const SEARCH_QUERY_MAX_LENGTH = 240;
const SEARCH_LIMIT_MAX = 100;
const SEARCH_OFFSET_MAX = 10_000;
const SEARCH_BOOKS_MAX = 83;
const SEARCH_EXCLUDE_MAX = 32;
const SEARCH_EXCLUDE_LENGTH_MAX = 100;
const SEARCH_RESPONSE_MAX_BYTES = 1024 * 1024;
const SEARCH_WORDS = ["all", "any", "phrase"];
const SEARCH_MATCH = ["whole_word", "substring"];
const SEARCH_SCOPE = ["bible", "old_testament", "new_testament", "deuterocanon"];
const SEARCH_DIACRITICS = ["fold", "exact"];
const SEARCH_SORT = ["canonical", "relevance"];

/**
 * Browser-side repository for public GetBible data.
 *
 * All normal reading, navigation and search requests terminate at the public
 * APIs. The robot remains responsible only for authenticated control-plane
 * operations and final authoritative posting.
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

  async chapter(
    translation,
    book,
    chapter,
    targetVerse = 1,
    { includeNavigation = true } = {},
  ) {
    if (typeof includeNavigation !== "boolean") {
      throw new TypeError("Chapter navigation option is invalid.");
    }
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
    if (!includeNavigation) {
      return {
        ...scripture,
        target_verse: nearestVerse(
          scripture.items.map((item) => item.verse),
          target,
        ),
      };
    }
    return this.#presentChapter(scripture, target);
  }

  async resolveReference(translation, references) {
    const code = normalizeTranslationCode(translation);
    const payload = await this.#transport.query(code, references, {
      maximumBytes: 1024 * 1024,
    });
    return normalizeQueryTarget(payload, code);
  }

  /**
   * One page of a full-text search, straight from the Search API.
   *
   * Nothing here is written to the persistent cache: a result set is only
   * meaningful for one query under one set of criteria, and the browser's own
   * HTTP cache already honours the API's Cache-Control for repeats.
   */
  async search(translation, query, filters, { offset = 0, limit = 25 } = {}) {
    const code = normalizeTranslationCode(translation);
    const text = normalizeSearchQuery(query);
    const criteria = normalizeSearchCriteria(filters);
    const first = boundedInteger(offset, 0, SEARCH_OFFSET_MAX, "Search offset");
    const size = boundedInteger(limit, 1, SEARCH_LIMIT_MAX, "Search limit");
    const parameters = new URLSearchParams();
    parameters.set("q", text);
    parameters.set("words", criteria.words);
    parameters.set("match", criteria.match);
    parameters.set("case_sensitive", String(criteria.case_sensitive));
    parameters.set("scope", criteria.scope);
    parameters.set("diacritics", criteria.diacritics);
    parameters.set("sort", criteria.sort);
    parameters.set("limit", String(size));
    parameters.set("offset", String(first));
    for (const book of criteria.books) {
      parameters.append("book", String(book));
    }
    for (const term of criteria.exclude) {
      parameters.append("exclude", term);
    }
    if (criteria.proximity !== null) {
      parameters.set("proximity", String(criteria.proximity));
    }
    const payload = await this.#transport.search(code, parameters, {
      maximumBytes: SEARCH_RESPONSE_MAX_BYTES,
    });
    return normalizeSearchPayload(payload, {
      translation: code,
      query: text,
      diacritics: criteria.diacritics,
    });
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

function normalizeSearchQuery(value) {
  if (typeof value !== "string") {
    throw new TypeError("Search query is invalid.");
  }
  const text = value.replace(/\s+/gu, " ").trim();
  if (text.length === 0 || text.length > SEARCH_QUERY_MAX_LENGTH) {
    throw new TypeError("Search query is invalid.");
  }
  return text;
}

/**
 * Validate search criteria against the Search API's documented vocabulary.
 *
 * A missing control takes the API default; a present control that says
 * something the API does not accept is refused here, before any request, so
 * the page never learns about its own mistake from a 400.
 */
function normalizeSearchCriteria(filters) {
  const value = filters && typeof filters === "object" && !Array.isArray(filters)
    ? filters
    : {};
  const words = oneOf(value.words, SEARCH_WORDS, "Search words");
  if (
    value.case_sensitive !== undefined &&
    value.case_sensitive !== null &&
    typeof value.case_sensitive !== "boolean"
  ) {
    throw new TypeError("Search case sensitivity is invalid.");
  }
  const books = [];
  if (value.books !== undefined && value.books !== null) {
    if (!Array.isArray(value.books)) {
      throw new TypeError("Search books are invalid.");
    }
    for (const book of value.books) {
      const number = boundedInteger(book, 1, 200, "Search book");
      if (!books.includes(number)) {
        books.push(number);
      }
    }
    if (books.length > SEARCH_BOOKS_MAX) {
      throw new RangeError("Search book selection is too large.");
    }
  }
  const exclude = [];
  if (value.exclude !== undefined && value.exclude !== null) {
    if (!Array.isArray(value.exclude)) {
      throw new TypeError("Search exclusions are invalid.");
    }
    const seen = new Set();
    for (const item of value.exclude) {
      const term = typeof item === "string" ? item.trim() : "";
      if (
        term.length === 0 ||
        term.length > SEARCH_EXCLUDE_LENGTH_MAX ||
        /\s/u.test(term)
      ) {
        throw new TypeError("Search exclusion is invalid.");
      }
      const key = term.toLocaleLowerCase();
      if (!seen.has(key)) {
        seen.add(key);
        exclude.push(term);
      }
    }
    if (exclude.length > SEARCH_EXCLUDE_MAX) {
      throw new RangeError("Search exclusion list is too large.");
    }
  }
  let proximity = null;
  if (value.proximity !== undefined && value.proximity !== null) {
    // Proximity only means anything when every unit must be present, which
    // is also the only combination the API accepts.
    proximity = boundedInteger(value.proximity, 0, 100, "Search proximity");
    if (words !== "all") {
      proximity = null;
    }
  }
  return {
    words,
    match: oneOf(value.match, SEARCH_MATCH, "Search match"),
    case_sensitive: value.case_sensitive === true,
    scope: oneOf(value.scope, SEARCH_SCOPE, "Search scope"),
    diacritics: oneOf(value.diacritics, SEARCH_DIACRITICS, "Search diacritics"),
    sort: oneOf(value.sort, SEARCH_SORT, "Search sort"),
    books: books.sort((left, right) => left - right),
    exclude,
    proximity,
  };
}

function oneOf(value, allowed, label) {
  if (value === undefined || value === null) {
    return allowed[0];
  }
  if (!allowed.includes(value)) {
    throw new TypeError(`${label} is invalid.`);
  }
  return value;
}

export { PublicApiError } from "./getbible-transport.js";
