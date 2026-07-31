const TRANSLATION_PATTERN = /^[a-z0-9][a-z0-9_-]{0,29}$/;
const SHA1_PATTERN = /^[0-9a-f]{40}$/;
const LANGUAGE_TAG_PATTERN = /^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,7}$/;
const DIRECT_SELECTION_PATTERN = /^gbd_[a-z0-9][a-z0-9_-]{0,29}_[0-9]{3}_[0-9]{4}_[0-9]{4}$/;
const MAX_TRANSLATIONS = 1_000;
const MAX_BOOKS = 200;
const MAX_CHAPTERS = 500;
const MAX_VERSES = 500;
const MAX_TEXT_LENGTH = 20_000;

export function normalizeTranslationCode(value) {
  const code = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (!TRANSLATION_PATTERN.test(code)) {
    throw new TypeError("Translation code is invalid.");
  }
  return code;
}

export function normalizeTranslationsPayload(payload) {
  const items = [];
  if (Array.isArray(payload)) {
    for (const raw of payload) {
      const item = translationFromItem(raw);
      if (item) {
        items.push(item);
      }
    }
  } else if (isRecord(payload)) {
    for (const [rawCode, raw] of Object.entries(payload)) {
      const item = translationFromItem(raw, rawCode);
      if (item) {
        items.push(item);
      }
    }
  }
  const unique = uniqueBy(items, (item) => item.code);
  if (unique.length === 0 || unique.length > MAX_TRANSLATIONS) {
    throw new TypeError("Translation catalog is malformed.");
  }
  return unique.sort((left, right) =>
    left.language.localeCompare(right.language) ||
    left.name.localeCompare(right.name) ||
    left.code.localeCompare(right.code),
  );
}

export function normalizeBooksPayload(payload, expectedTranslation) {
  const translation = normalizeTranslationCode(expectedTranslation);
  const candidates = [];
  const source = Array.isArray(payload)
    ? payload
    : Array.isArray(payload?.books)
      ? payload.books
      : Array.isArray(payload?.items)
        ? payload.items
        : isRecord(payload)
          ? Object.entries(payload).map(([key, value]) => ({
            ...(isRecord(value) ? value : {}),
            __key: key,
          }))
          : [];

  for (const raw of source) {
    if (!isRecord(raw)) {
      continue;
    }
    const number = positiveInteger(
      raw.number ?? raw.nr ?? raw.book_number ?? raw.book_nr ?? raw.__key,
      MAX_BOOKS,
    );
    const name = boundedText(raw.name ?? raw.book_name ?? raw.title, 128);
    const abbreviation = raw.abbreviation ?? raw.translation_code;
    if (
      number === null ||
      name === null ||
      (typeof abbreviation === "string" &&
        abbreviation.toLowerCase() !== translation)
    ) {
      continue;
    }
    const rawSha = typeof raw.sha === "string" ? raw.sha.toLowerCase() : null;
    candidates.push({
      number,
      name,
      testament: normalizeTestament(raw.testament, number),
      sha: rawSha && SHA1_PATTERN.test(rawSha) ? rawSha : null,
    });
  }
  const items = uniqueBy(candidates, (item) => item.number)
    .sort((left, right) => left.number - right.number);
  if (items.length === 0 || items.length > MAX_BOOKS) {
    throw new TypeError("Book catalog is malformed.");
  }
  return { translation, items };
}

export function normalizeChaptersPayload(payload, {
  translation: expectedTranslation,
  book: expectedBook,
}) {
  const translation = normalizeTranslationCode(expectedTranslation);
  const book = positiveInteger(expectedBook, MAX_BOOKS);
  if (book === null) {
    throw new TypeError("Book number is invalid.");
  }
  const source = chapterCandidates(payload);
  const items = [];
  for (const [index, raw] of source.entries()) {
    const item = chapterFromItem(raw, index + 1);
    if (item) {
      items.push(item);
    }
  }
  const unique = uniqueBy(items, (item) => item.number)
    .sort((left, right) => left.number - right.number);
  if (unique.length === 0 || unique.length > MAX_CHAPTERS) {
    throw new TypeError("Chapter catalog is malformed.");
  }
  return { translation, book: { number: book }, items: unique };
}

export function normalizeChapterPayload(payload, {
  translation: expectedTranslation,
  book: expectedBook,
  chapter: expectedChapter,
  targetVerse = 1,
  sha,
}) {
  const translation = normalizeTranslationCode(expectedTranslation);
  const book = positiveInteger(expectedBook, MAX_BOOKS);
  const chapter = positiveInteger(expectedChapter, 1_000);
  const normalizedSha = typeof sha === "string" ? sha.toLowerCase() : "";
  if (book === null || chapter === null || !SHA1_PATTERN.test(normalizedSha)) {
    throw new TypeError("Chapter request is invalid.");
  }
  if (!isRecord(payload)) {
    throw new TypeError("Chapter payload is malformed.");
  }
  const abbreviation = String(payload.abbreviation ?? payload.translation_code ?? "")
    .trim()
    .toLowerCase();
  const responseBook = positiveInteger(
    payload.book_nr ?? payload.book_number ?? payload.nr,
    MAX_BOOKS,
  );
  const responseChapter = positiveInteger(payload.chapter, 1_000);
  const bookName = boundedText(payload.book_name ?? payload.book ?? payload.title, 128);
  const chapterReference = boundedText(
    payload.name ?? payload.reference ?? `${bookName ?? ""} ${chapter}`,
    180,
  );
  if (
    abbreviation !== translation ||
    responseBook !== book ||
    responseChapter !== chapter ||
    bookName === null ||
    chapterReference === null ||
    !Array.isArray(payload.verses) ||
    payload.verses.length === 0 ||
    payload.verses.length > MAX_VERSES
  ) {
    throw new TypeError("Chapter payload does not match the request.");
  }

  const items = [];
  for (const raw of payload.verses) {
    if (!isRecord(raw)) {
      throw new TypeError("Chapter verse is malformed.");
    }
    const verse = positiveInteger(raw.verse ?? raw.number, 2_000);
    const text = boundedText(raw.text, MAX_TEXT_LENGTH);
    if (verse === null || text === null) {
      throw new TypeError("Chapter verse is malformed.");
    }
    items.push({
      selection_id: directSelectionId(translation, book, chapter, verse),
      translation,
      reference: `${bookName} ${chapter}:${verse}`,
      book_number: book,
      book_name: bookName,
      chapter,
      verse,
      text,
      terms: [],
    });
  }
  const verses = uniqueBy(items, (item) => item.verse)
    .sort((left, right) => left.verse - right.verse);
  if (verses.length !== items.length) {
    throw new TypeError("Chapter contains duplicate verses.");
  }
  return {
    translation,
    book: {
      number: book,
      name: bookName,
      testament: normalizeTestament(payload.testament, book),
    },
    chapter,
    reference: chapterReference,
    target_verse: nearestVerse(verses.map((item) => item.verse), targetVerse),
    sha: normalizedSha,
    navigation: { previous: null, next: null },
    items: verses,
  };
}

export function directSelectionId(translation, book, chapter, verse) {
  const code = normalizeTranslationCode(translation);
  const bookNumber = positiveInteger(book, MAX_BOOKS);
  const chapterNumber = positiveInteger(chapter, 1_000);
  const verseNumber = positiveInteger(verse, 2_000);
  if (bookNumber === null || chapterNumber === null || verseNumber === null) {
    throw new TypeError("Direct selection coordinates are invalid.");
  }
  return [
    "gbd",
    code,
    String(bookNumber).padStart(3, "0"),
    String(chapterNumber).padStart(4, "0"),
    String(verseNumber).padStart(4, "0"),
  ].join("_");
}

export function isDirectSelectionId(value) {
  return typeof value === "string" && DIRECT_SELECTION_PATTERN.test(value);
}

export function selectionIdentity(verse) {
  if (!isRecord(verse)) {
    return "";
  }
  try {
    return directSelectionId(
      verse.translation,
      verse.book_number,
      verse.chapter,
      verse.verse,
    );
  } catch {
    return typeof verse.selection_id === "string" ? verse.selection_id : "";
  }
}

export function normalizeQueryTarget(payload, expectedTranslation) {
  const translation = normalizeTranslationCode(expectedTranslation);
  const candidates = [];
  visitQueryValue(payload, {}, candidates, translation, "");
  const first = candidates.find((candidate) =>
    candidate.translation === translation,
  ) ?? candidates[0];
  if (!first) {
    throw new TypeError("Reference query did not return a resolvable verse.");
  }
  return {
    translation,
    book_number: first.book_number,
    book_name: first.book_name,
    chapter: first.chapter,
    verse: first.verse,
    reference: first.reference,
  };
}

export function withChapterNavigation(scripture, {
  previous = null,
  next = null,
} = {}) {
  if (!isRecord(scripture)) {
    throw new TypeError("Scripture payload is invalid.");
  }
  return {
    ...scripture,
    navigation: {
      previous: normalizeNavigation(previous),
      next: normalizeNavigation(next),
    },
  };
}

function translationFromItem(raw, key = null) {
  if (!isRecord(raw)) {
    return null;
  }
  let code;
  try {
    code = normalizeTranslationCode(
      raw.code ?? raw.abbreviation ?? raw.translation_code ?? key,
    );
  } catch {
    return null;
  }
  const name = boundedText(raw.name ?? raw.translation ?? raw.title, 256);
  const language = boundedText(raw.language ?? raw.language_name ?? raw.lang, 128);
  if (name === null || language === null) {
    return null;
  }
  return {
    code,
    name,
    language,
    lang: normalizeLanguageTag(raw.lang ?? raw.language_code),
    direction: String(raw.direction ?? "ltr").toLowerCase() === "rtl" ? "rtl" : "ltr",
  };
}

function chapterCandidates(payload) {
  if (Array.isArray(payload)) {
    return payload;
  }
  if (!isRecord(payload)) {
    return [];
  }
  if (Array.isArray(payload.chapters)) {
    return payload.chapters;
  }
  if (Array.isArray(payload.items)) {
    return payload.items;
  }
  const numericEntries = Object.entries(payload)
    .filter(([key]) => /^\d+$/.test(key))
    .map(([key, value]) => {
      if (isRecord(value)) {
        return { ...value, __key: key };
      }
      return { chapter: Number(key), verses: value };
    });
  return numericEntries;
}

function chapterFromItem(raw, fallbackNumber) {
  if (typeof raw === "number") {
    const count = positiveInteger(raw, MAX_VERSES);
    return count === null
      ? null
      : { number: fallbackNumber, verse_count: count, verses: sequence(count) };
  }
  if (!isRecord(raw)) {
    return null;
  }
  const number = positiveInteger(
    raw.number ?? raw.chapter ?? raw.nr ?? raw.__key ?? fallbackNumber,
    1_000,
  );
  if (number === null) {
    return null;
  }
  const rawVerses = raw.verses ?? raw.verse_count ?? raw.count;
  let verses;
  if (Array.isArray(rawVerses)) {
    verses = uniqueNumbers(
      rawVerses.map((item) =>
        positiveInteger(
          isRecord(item) ? item.verse ?? item.number : item,
          2_000,
        ),
      ),
    );
  } else {
    const count = positiveInteger(rawVerses, MAX_VERSES);
    verses = count === null ? [] : sequence(count);
  }
  if (verses.length === 0 || verses.length > MAX_VERSES) {
    return null;
  }
  return { number, verse_count: verses.length, verses };
}

function visitQueryValue(value, context, results, expectedTranslation, pathKey) {
  if (Array.isArray(value)) {
    for (const item of value) {
      visitQueryValue(item, context, results, expectedTranslation, pathKey);
    }
    return;
  }
  if (!isRecord(value)) {
    return;
  }
  const keyed = /^([a-z0-9._-]+)_(\d+)_(\d+)$/i.exec(pathKey);
  const next = {
    translation: normalizePossibleTranslation(
      value.abbreviation ?? value.translation_code ?? context.translation ?? keyed?.[1],
      expectedTranslation,
    ),
    book_number: positiveInteger(
      value.book_nr ?? value.book_number ?? context.book_number ?? keyed?.[2],
      MAX_BOOKS,
    ),
    book_name:
      boundedText(value.book_name ?? context.book_name, 128) ?? context.book_name ?? null,
    chapter: positiveInteger(
      value.chapter ?? context.chapter ?? keyed?.[3],
      1_000,
    ),
    reference:
      boundedText(value.reference ?? value.name ?? context.reference, 180) ??
      context.reference ??
      null,
  };
  const verse = positiveInteger(value.verse ?? value.verse_number, 2_000);
  const text = boundedText(value.text, MAX_TEXT_LENGTH);
  if (
    verse !== null &&
    text !== null &&
    next.book_number !== null &&
    next.book_name &&
    next.chapter !== null
  ) {
    results.push({
      translation: next.translation,
      book_number: next.book_number,
      book_name: next.book_name,
      chapter: next.chapter,
      verse,
      reference:
        next.reference && /:\s*\d/.test(next.reference)
          ? next.reference
          : `${next.book_name} ${next.chapter}:${verse}`,
    });
  }
  for (const [key, child] of Object.entries(value)) {
    if (isRecord(child) || Array.isArray(child)) {
      visitQueryValue(child, next, results, expectedTranslation, key);
    }
  }
}

function normalizePossibleTranslation(value, fallback) {
  try {
    return normalizeTranslationCode(value ?? fallback);
  } catch {
    return fallback;
  }
}

function normalizeNavigation(value) {
  if (!isRecord(value)) {
    return null;
  }
  const book = positiveInteger(value.book ?? value.book_number, MAX_BOOKS);
  const chapter = positiveInteger(value.chapter, 1_000);
  const bookName = boundedText(value.book_name ?? value.name, 128);
  if (book === null || chapter === null || bookName === null) {
    return null;
  }
  return { book, book_name: bookName, chapter };
}

function normalizeTestament(value, book) {
  const normalized = typeof value === "string" ? value.toLowerCase() : "";
  if (["old", "new", "other"].includes(normalized)) {
    return normalized;
  }
  return book <= 39 ? "old" : book <= 66 ? "new" : "other";
}

function normalizeLanguageTag(value) {
  if (typeof value !== "string") {
    return "und";
  }
  const candidate = value.trim().replaceAll("_", "-");
  if (!LANGUAGE_TAG_PATTERN.test(candidate)) {
    return "und";
  }
  const parts = candidate.split("-");
  return parts.map((part, index) => {
    if (index === 0) {
      return part.toLowerCase();
    }
    if (part.length === 4 && /^[A-Za-z]+$/.test(part)) {
      return part[0].toUpperCase() + part.slice(1).toLowerCase();
    }
    if ((part.length === 2 && /^[A-Za-z]+$/.test(part)) || /^\d{3}$/.test(part)) {
      return part.toUpperCase();
    }
    return part.toLowerCase();
  }).join("-");
}

function nearestVerse(available, requested) {
  const target = positiveInteger(requested, 2_000) ?? available[0];
  return available.reduce((nearest, current) =>
    Math.abs(current - target) < Math.abs(nearest - target)
      ? current
      : nearest,
  available[0]);
}

function positiveInteger(value, maximum) {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isInteger(number) && number >= 1 && number <= maximum
    ? number
    : null;
}

function boundedText(value, maximum) {
  if (typeof value !== "string") {
    return null;
  }
  const normalized = value.trim();
  return normalized.length >= 1 && normalized.length <= maximum
    ? normalized
    : null;
}

function sequence(count) {
  return Array.from({ length: count }, (_, index) => index + 1);
}

function uniqueNumbers(values) {
  return [...new Set(values.filter((value) => value !== null))]
    .sort((left, right) => left - right);
}

function uniqueBy(values, key) {
  const seen = new Set();
  return values.filter((value) => {
    const identity = key(value);
    if (seen.has(identity)) {
      return false;
    }
    seen.add(identity);
    return true;
  });
}

function isRecord(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
