import { graphemes, termHighlights } from "./search-highlight.js";

export const DEFAULT_FILTERS = Object.freeze({
  translation: "kjv",
  words: "all",
  match: "whole_word",
  scope: "bible",
  case_sensitive: false,
  // The Search API folds accents, vowel pointing and precomposed letters by
  // default, which is what lets unaccented Greek and unpointed Hebrew reach
  // the text.
  diacritics: "fold",
  sort: "canonical",
  books: [],
  exclude: [],
  proximity: null,
});

const TRANSLATION_PATTERN = /^[a-z0-9][a-z0-9_-]{0,29}$/;
const SELECTION_ID_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const CONTRIBUTION_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;
const CONTRIBUTION_CANONICAL_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,79}$/;
const CONTRIBUTION_REVIEW_DETAILS = Symbol("contributionReviewDetails");
const CONTRIBUTION_COLOR_PATTERN = /^#[a-f0-9]{6}$/;
const CONTRIBUTION_APPLICATION_STATES = new Set([
  "not_applied",
  "pending",
  "approved",
  "deferred",
  "rejected",
  "revoked",
  "unavailable",
]);
const CONTRIBUTION_TOPIC_STATES = new Set([
  "pending",
  "mapped",
  "rejected",
  "deferred",
]);
const CONTRIBUTION_TOPIC_SUMMARY_KEYS = Object.freeze([
  "pending",
  "mapped",
  "published",
  "rejected",
  "deferred",
]);
const CONTRIBUTION_EVENT_SUMMARY_KEYS = Object.freeze([
  "pending",
  "approved",
  "rejected",
  "deferred",
  "applied",
  "live",
]);
// "live" counts applied events the server has since observed in the public
// Bookmarks API. A server from before that observation simply omits it.
const CONTRIBUTION_OPTIONAL_EVENT_SUMMARY_KEYS = new Set(["live"]);
const MAX_CONTRIBUTION_TOPICS = 1_000;
const ROUTES = new Set([
  "home",
  "search",
  "bible",
  "history",
  "selection",
  "bookmarks",
]);
const ENTRYPOINT_ROUTES = new Set([
  "home",
  "search",
  "bible",
  "selection",
  "bookmarks",
]);

export function normalizeSession(payload) {
  if (!isRecord(payload)) {
    throw new TypeError("Invalid session response.");
  }
  const translations = normalizeTranslations(payload.translations);
  const requested = translationCode(
    payload.preferences?.translation,
    translations[0]?.code ?? DEFAULT_FILTERS.translation,
  );
  const preferred = translations.some((item) => item.code === requested)
    ? requested
    : translations[0]?.code ?? DEFAULT_FILTERS.translation;
  const filters = normalizeFilters(
    {
      ...payload.preferences?.search_defaults,
      translation: preferred,
    },
    preferred,
  );
  const readerLocation = normalizeReaderLocation(
    payload.preferences?.reader_location,
    preferred,
  );
  return {
    translations,
    preferences: {
      translation: preferred,
      search_defaults: filters,
      reader_location:
        readerLocation?.translation === preferred ? readerLocation : null,
    },
    basket: normalizeBasket(payload.basket),
    contributions: normalizeContributionStatus(payload.contributions),
    entrypoint: normalizeEntrypoint(payload.entrypoint),
  };
}

/**
 * Normalizes both the legacy four-field contributor status and the richer
 * per-user review outcome returned by current servers. Keeping the legacy
 * shape readable lets a newly upgraded Mini App connect while an older Robot
 * process is completing its own atomic upgrade.
 */
export function normalizeContributionStatus(value) {
  try {
    return parseContributionStatus(value);
  } catch (error) {
    if (!(error instanceof TypeError) && !(error instanceof RangeError)) {
      throw error;
    }
    // Opening the reader never depends on the contributor pipeline. A status
    // this client cannot read (a newer server, a damaged row, an unknown
    // state) is the same as no status: the reader opens, and the contributor
    // panel simply stays unavailable until a readable status arrives.
    return markContributionReviewDetails(
      unavailableContributionStatus(),
      false,
    );
  }
}

function parseContributionStatus(value) {
  if (value === undefined || value === null) {
    return markContributionReviewDetails(
      unavailableContributionStatus(),
      false,
    );
  }
  if (!isRecord(value)) {
    throw new TypeError("Invalid contributor status response.");
  }
  // The contributor token is an optional sibling the API layer harvests
  // before this shape check; the normalized status itself never carries it.
  if (Object.hasOwn(value, "contribution_token")) {
    if (
      typeof value.contribution_token !== "string" ||
      !/^gbc_[A-Za-z0-9_-]{43}$/.test(value.contribution_token)
    ) {
      throw new TypeError("Invalid contributor status response.");
    }
    const { contribution_token: _ignored, ...rest } = value;
    value = rest;
  }
  const keys = Object.keys(value).sort();
  const legacyKeys = [
    "can_contribute",
    "disclosure_required",
    "enabled",
    "state",
  ];
  const currentKeys = [...legacyKeys, "summary", "topics"].sort();
  const reviewDetailsAvailable = Object.hasOwn(
    value,
    CONTRIBUTION_REVIEW_DETAILS,
  )
    ? value[CONTRIBUTION_REVIEW_DETAILS] === true
    : sameStringArray(keys, currentKeys);
  if (
    !sameStringArray(keys, legacyKeys) &&
    !sameStringArray(keys, currentKeys)
  ) {
    throw new TypeError("Invalid contributor status response.");
  }
  if (
    typeof value.enabled !== "boolean" ||
    typeof value.can_contribute !== "boolean" ||
    typeof value.disclosure_required !== "boolean" ||
    !CONTRIBUTION_APPLICATION_STATES.has(value.state)
  ) {
    throw new TypeError("Invalid contributor status response.");
  }
  const topics = Object.hasOwn(value, "topics")
    ? normalizeContributionTopics(value.topics)
    : [];
  const summary = Object.hasOwn(value, "summary")
    ? normalizeContributionSummary(value.summary)
    : emptyContributionSummary();
  return markContributionReviewDetails({
    enabled: value.enabled,
    state: value.state,
    can_contribute: value.can_contribute,
    disclosure_required: value.disclosure_required,
    topics,
    summary,
  }, reviewDetailsAvailable);
}

/** Internal provenance for distinguishing rich empty review data from defaults. */
export function contributionReviewDetailsAvailable(value) {
  return Boolean(value?.[CONTRIBUTION_REVIEW_DETAILS]);
}

export function normalizeTranslations(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  const seen = new Set();
  const translations = [];
  for (const item of value) {
    if (!isRecord(item)) {
      continue;
    }
    const code = translationCode(item.code, null);
    const name = boundedText(item.name, 256);
    const language = boundedText(item.language, 128);
    if (!code || !name || seen.has(code)) {
      continue;
    }
    seen.add(code);
    translations.push({
      code,
      name,
      language: language || "Unknown language",
      lang: localeCode(item.lang, "en"),
      direction: item.direction === "rtl" ? "rtl" : "ltr",
    });
  }
  return translations;
}

export function normalizeBooks(payload, expectedTranslation = null) {
  const expected = translationCode(expectedTranslation, null);
  if (
    expected &&
    (
      !isRecord(payload) ||
      translationCode(payload.translation, null) !== expected
    )
  ) {
    throw new TypeError("Book response translation did not match the request.");
  }
  const value = Array.isArray(payload)
    ? payload
    : payload?.books ?? payload?.items;
  if (!Array.isArray(value)) {
    return [];
  }
  const books = [];
  const seen = new Set();
  for (const item of value) {
    if (!isRecord(item)) {
      continue;
    }
    const number = boundedInteger(item.number, 1, 200);
    const name = boundedText(item.name, 128);
    if (!number || !name || seen.has(number)) {
      continue;
    }
    seen.add(number);
    books.push({
      number,
      name,
      testament: ["old", "new", "other"].includes(item.testament)
        ? item.testament
        : null,
    });
  }
  return books.sort((left, right) => left.number - right.number);
}

export function abbreviateBookName(value) {
  const name = boundedText(value, 128);
  if (!name) {
    return "";
  }
  const parts = name
    .split(/[\s\-–—]+/u)
    .map(graphemes)
    .filter((part) => part.length > 0);
  if (parts.length === 0) {
    return "";
  }
  if (/^\d+$/u.test(parts[0].join("")) && parts.length > 1) {
    return [
      ...parts[0],
      ...parts[1].slice(0, 2),
    ].join("");
  }
  if (parts.length === 1) {
    return parts[0].slice(0, 3).join("");
  }
  return parts.slice(0, 3).map((part) => part[0]).join("");
}

export function uniqueBookLabels(books) {
  if (!Array.isArray(books)) {
    return [];
  }
  const compactNames = books.map((book) =>
    graphemes(boundedText(book?.name, 128))
      .filter((character) => !/[\s\-–—]/u.test(character)),
  );
  const labels = books.map((book) => abbreviateBookName(book?.name));
  const lengths = labels.map((label) => graphemes(label).length);

  for (let pass = 0; pass < 128; pass += 1) {
    const groups = new Map();
    labels.forEach((label, index) => {
      const key = label.toLocaleLowerCase();
      groups.set(key, [...(groups.get(key) ?? []), index]);
    });
    const duplicates = [...groups.values()].filter((group) => group.length > 1);
    if (duplicates.length === 0) {
      return labels;
    }
    let expanded = false;
    for (const group of duplicates) {
      for (const index of group) {
        if (lengths[index] < compactNames[index].length) {
          lengths[index] += 1;
          labels[index] = compactNames[index].slice(0, lengths[index]).join("");
          expanded = true;
        }
      }
    }
    if (!expanded) {
      break;
    }
  }

  return labels.map((label, index) => {
    const duplicate = labels.some(
      (other, otherIndex) =>
        otherIndex !== index &&
        other.toLocaleLowerCase() === label.toLocaleLowerCase(),
    );
    return duplicate && Number.isInteger(books[index]?.number)
      ? `${label}${books[index].number}`
      : label;
  });
}

export function normalizeChapters(payload, expected = null) {
  if (isRecord(expected)) {
    const expectedTranslation = translationCode(expected.translation, null);
    const expectedBook = boundedInteger(expected.book, 1, 200);
    const actualTranslation = isRecord(payload)
      ? translationCode(payload.translation, null)
      : null;
    const actualBook = isRecord(payload?.book)
      ? boundedInteger(payload.book.number, 1, 200)
      : null;
    if (
      !expectedTranslation ||
      !expectedBook ||
      actualTranslation !== expectedTranslation ||
      actualBook !== expectedBook
    ) {
      throw new TypeError("Chapter response did not match the requested book.");
    }
  }
  const value = Array.isArray(payload)
    ? payload
    : payload?.chapters ?? payload?.items;
  if (!Array.isArray(value)) {
    return [];
  }
  const chapters = [];
  const seen = new Set();
  for (const item of value) {
    const number = boundedInteger(
      isRecord(item) ? item.number : item,
      1,
      1_000,
    );
    if (!number || seen.has(number)) {
      continue;
    }
    seen.add(number);
    const verses = isRecord(item) && Array.isArray(item.verses)
      ? [...new Set(item.verses
        .map((verse) => boundedInteger(verse, 1, 2000))
        .filter((verse) => verse !== null))]
        .sort((left, right) => left - right)
      : [];
    chapters.push({
      number,
      verse_count: isRecord(item)
        ? boundedInteger(item.verse_count, 1, 250) ??
          (verses.length > 0 ? verses.length : null)
        : null,
      verses,
    });
  }
  return chapters.sort((left, right) => left.number - right.number);
}

export function nearestChapterVerse(chapter, requested = 1) {
  const target = boundedInteger(requested, 1, 2000) ?? 1;
  const verses = isRecord(chapter) && Array.isArray(chapter.verses)
    ? chapter.verses
      .map((verse) => boundedInteger(verse, 1, 2000))
      .filter((verse) => verse !== null)
    : [];
  if (verses.length === 0 || verses.includes(target)) {
    return target;
  }
  return verses.reduce((nearest, verse) =>
    Math.abs(verse - target) < Math.abs(nearest - target) ? verse : nearest,
  );
}

export function normalizeScripture(payload, expected = null) {
  if (!isRecord(payload)) {
    throw new TypeError("Invalid scripture response.");
  }
  const translation = translationCode(payload.translation, null);
  const book = boundedInteger(
    isRecord(payload.book) ? payload.book.number : payload.book,
    1,
    200,
  );
  const chapter = boundedInteger(payload.chapter, 1, 1_000);
  const verses = normalizeVerses(payload.verses ?? payload.items);
  const expectedTranslation = isRecord(expected)
    ? translationCode(expected.translation, null)
    : null;
  const expectedBook = isRecord(expected)
    ? boundedInteger(expected.book, 1, 200)
    : null;
  const expectedChapter = isRecord(expected)
    ? boundedInteger(expected.chapter, 1, 1_000)
    : null;
  if (
    !translation ||
    !book ||
    !chapter ||
    (expectedTranslation && translation !== expectedTranslation) ||
    (expectedBook && book !== expectedBook) ||
    (expectedChapter && chapter !== expectedChapter) ||
    verses.some(
      (verse) =>
        verse.translation !== translation ||
        verse.book_number !== book ||
        verse.chapter !== chapter,
    )
  ) {
    throw new TypeError("Scripture response did not match the requested passage.");
  }
  return {
    translation,
    book,
    chapter,
    reference: boundedText(payload.reference, 180),
    target_verse: boundedInteger(payload.target_verse, 1, 2_000) ?? 1,
    navigation: {
      previous: normalizeChapterLocation(payload.navigation?.previous),
      next: normalizeChapterLocation(payload.navigation?.next),
    },
    verses,
  };
}

export function normalizeReaderLocation(value, fallbackTranslation = "kjv") {
  if (!isRecord(value)) {
    return null;
  }
  const translation = translationCode(value.translation, fallbackTranslation);
  const book = boundedInteger(value.book, 1, 200);
  const chapter = boundedInteger(value.chapter, 1, 1_000);
  const verse = boundedInteger(value.verse, 1, 2_000) ?? 1;
  if (!translation || !book || !chapter) {
    return null;
  }
  return { translation, book, chapter, verse };
}

export function planTranslationChange(
  value,
  translations,
  readerLocation,
  { route = "home", hasSearchQuery = false } = {},
) {
  const translation = translationCode(value, null);
  if (
    !translation ||
    !Array.isArray(translations) ||
    !translations.some((item) => item?.code === translation)
  ) {
    return null;
  }
  const location = normalizeReaderLocation(readerLocation, translation);
  return {
    translation,
    reader_location: location
      ? { ...location, translation }
      : null,
    reload_reader: route === "bible",
    rerun_search: route === "search" && Boolean(hasSearchQuery),
  };
}

export function normalizeVerses(value, diacritics = DEFAULT_FILTERS.diacritics) {
  if (!Array.isArray(value)) {
    return [];
  }
  const verses = [];
  const seen = new Set();
  for (const item of value) {
    const verse = normalizeVerse(item, diacritics);
    if (!verse || seen.has(verse.selection_id)) {
      continue;
    }
    seen.add(verse.selection_id);
    verses.push(verse);
  }
  return verses;
}

export function normalizeVerse(value, diacritics = DEFAULT_FILTERS.diacritics) {
  if (!isRecord(value)) {
    return null;
  }
  const selectionId =
    typeof value.selection_id === "string" &&
    SELECTION_ID_PATTERN.test(value.selection_id)
      ? value.selection_id
      : null;
  const translation = translationCode(value.translation, null);
  const reference = boundedText(value.reference, 180);
  const text = boundedText(value.text, 4_096);
  const bookNumber = boundedInteger(value.book_number, 1, 200);
  const chapter = boundedInteger(value.chapter, 1, 1_000);
  const verseNumber = boundedInteger(value.verse, 1, 2_000);
  if (
    !selectionId ||
    !translation ||
    !reference ||
    !text ||
    !bookNumber ||
    !chapter ||
    !verseNumber
  ) {
    return null;
  }
  const highlights = normalizeHighlights(value.highlights, text.length);
  return {
    selection_id: selectionId,
    translation,
    reference,
    book_number: bookNumber,
    book_name: boundedText(value.book_name, 128) || reference.split(/\s+\d/)[0],
    chapter,
    verse: verseNumber,
    text,
    highlights:
      highlights.length > 0
        ? highlights
        : termHighlights(text, normalizeWords(value.terms), diacritics),
  };
}

export function normalizeBasket(payload) {
  const items = normalizeVerses(Array.isArray(payload) ? payload : payload?.items);
  return {
    items,
    count: items.length,
  };
}

export function normalizeFilters(value, fallbackTranslation = "kjv") {
  const filters = isRecord(value) ? value : {};
  const words = ["all", "any", "phrase"].includes(filters.words)
    ? filters.words
    : DEFAULT_FILTERS.words;
  return {
    translation: translationCode(filters.translation, fallbackTranslation),
    words,
    match: ["whole_word", "substring"].includes(filters.match)
      ? filters.match
      : DEFAULT_FILTERS.match,
    scope: [
      "bible",
      "old_testament",
      "new_testament",
      "deuterocanon",
    ].includes(filters.scope)
      ? filters.scope
      : DEFAULT_FILTERS.scope,
    case_sensitive: Boolean(filters.case_sensitive),
    diacritics: normalizeDiacritics(filters.diacritics),
    sort: ["canonical", "relevance"].includes(filters.sort)
      ? filters.sort
      : DEFAULT_FILTERS.sort,
    books: normalizeNumberList(filters.books, 200),
    exclude: normalizeWords(filters.exclude),
    proximity:
      words === "all"
        ? boundedInteger(filters.proximity, 0, 100)
        : null,
  };
}

export function activeFilterCount(filters) {
  let count = 0;
  for (const key of [
    "words",
    "match",
    "scope",
    "case_sensitive",
    "diacritics",
  ]) {
    if (filters[key] !== DEFAULT_FILTERS[key]) {
      count += 1;
    }
  }
  if (filters.books.length > 0) {
    count += 1;
  }
  if (filters.exclude.length > 0) {
    count += 1;
  }
  if (filters.proximity !== null) {
    count += 1;
  }
  return count;
}

export function moveItem(items, index, offset) {
  const target = index + offset;
  if (index < 0 || index >= items.length || target < 0 || target >= items.length) {
    return [...items];
  }
  const result = [...items];
  const [item] = result.splice(index, 1);
  result.splice(target, 0, item);
  return result;
}

export function uniqueVerses(existing, incoming) {
  const result = [...existing];
  const seen = new Set(existing.map((item) => item.selection_id));
  for (const verse of incoming) {
    if (!seen.has(verse.selection_id)) {
      seen.add(verse.selection_id);
      result.push(verse);
    }
  }
  return result;
}

export function routeName(value, fallback = "home") {
  return ROUTES.has(value) ? value : fallback;
}

export function entrypointIntent(value) {
  const entrypoint = normalizeEntrypoint(value);
  const intent = {
    route: entrypoint.route,
    search_query: entrypoint.route === "search" ? entrypoint.query : "",
    bible_reference: entrypoint.route === "bible" ? entrypoint.query : "",
  };
  if (
    entrypoint.route === "bookmarks" &&
    entrypoint.bookmark_restore_available
  ) {
    intent.bookmark_restore_available = true;
  }
  return intent;
}

export function resolveBibleEntrypoint(value, books, translationCodes = []) {
  if (typeof value !== "string" || !Array.isArray(books)) {
    return null;
  }
  let source = value.normalize("NFKC").trim().replace(/\s+/g, " ");
  if (!source || source.length > 512) {
    return null;
  }
  const translations = new Set(
    translationCodes
      .filter((code) => typeof code === "string")
      .map((code) => code.toLocaleLowerCase()),
  );
  const finalSpace = source.lastIndexOf(" ");
  if (finalSpace > 0) {
    const suffix = source.slice(finalSpace + 1).toLocaleLowerCase();
    if (translations.has(suffix)) {
      source = source.slice(0, finalSpace).trim();
    }
  }

  const normalizedSource = source.toLocaleLowerCase();
  const candidates = books
    .filter(
      (book) =>
        isRecord(book) &&
        boundedInteger(book.number, 1, 200) !== null &&
        boundedText(book.name, 128),
    )
    .map((book) => ({
      number: book.number,
      name: book.name.normalize("NFKC").trim().replace(/\s+/g, " "),
    }))
    .sort((left, right) => right.name.length - left.name.length);
  for (const book of candidates) {
    const normalizedName = book.name.toLocaleLowerCase();
    if (
      normalizedSource !== normalizedName &&
      !normalizedSource.startsWith(`${normalizedName} `)
    ) {
      continue;
    }
    const remainder = source.slice(book.name.length).trim();
    if (!remainder) {
      return { book_number: book.number, chapter: null };
    }
    if (/^(?:[1-9][0-9]{0,2}|1000)$/.test(remainder)) {
      return {
        book_number: book.number,
        chapter: Number(remainder),
      };
    }
  }
  return null;
}

function normalizeEntrypoint(value) {
  if (!isRecord(value)) {
    return {
      route: "home",
      query: "",
      reference: "",
      bookmark_restore_available: false,
    };
  }
  const route = ENTRYPOINT_ROUTES.has(value.route) ? value.route : "home";
  return {
    route,
    query: boundedText(value.query, 240),
    reference: boundedText(value.reference, 180),
    bookmark_restore_available:
      route === "bookmarks" && value.bookmark_restore_available === true,
  };
}

function normalizeChapterLocation(value) {
  if (!isRecord(value)) {
    return null;
  }
  const book = boundedInteger(value.book, 1, 200);
  const chapter = boundedInteger(value.chapter, 1, 1_000);
  const bookName = boundedText(value.book_name, 128);
  if (!book || !chapter || !bookName) {
    return null;
  }
  return { book, book_name: bookName, chapter };
}

function normalizeHighlights(value, textLength) {
  if (!Array.isArray(value)) {
    return [];
  }
  const spans = [];
  for (const item of value) {
    if (!isRecord(item)) {
      continue;
    }
    const start = boundedInteger(item.start, 0, textLength);
    const end = boundedInteger(item.end, 0, textLength);
    if (start === null || end === null || end <= start) {
      continue;
    }
    spans.push({ start, end });
  }
  return spans
    .sort((left, right) => left.start - right.start || left.end - right.end)
    .filter((span, index, all) => index === 0 || span.start >= all[index - 1].end);
}

function normalizeDiacritics(value) {
  return ["fold", "exact"].includes(value)
    ? value
    : DEFAULT_FILTERS.diacritics;
}

function normalizeNumberList(value, maximum) {
  if (!Array.isArray(value)) {
    return [];
  }
  return [...new Set(
    value
      .map((item) => boundedInteger(item, 1, maximum))
      .filter((item) => item !== null),
  )].sort((left, right) => left - right);
}

function normalizeWords(value) {
  const source = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.trim().split(/\s+/)
      : [];
  const words = [];
  const seen = new Set();
  for (const item of source) {
    const word = boundedText(item, 80);
    const key = word.toLocaleLowerCase();
    if (word && !seen.has(key) && words.length < 20) {
      seen.add(key);
      words.push(word);
    }
  }
  return words;
}

function normalizeContributionTopics(value) {
  if (!Array.isArray(value) || value.length > MAX_CONTRIBUTION_TOPICS) {
    throw new TypeError("Invalid contributor topic outcomes.");
  }
  const topics = [];
  const localIds = new Set();
  for (const item of value) {
    if (!isRecord(item)) {
      throw new TypeError("Invalid contributor topic outcome.");
    }
    const allowedKeys = new Set([
      "local_topic_id",
      "state",
      "published",
      "canonical_topic_id",
      "canonical_topic",
    ]);
    const keys = Object.keys(item);
    if (
      !["local_topic_id", "state", "published"].every((key) =>
        Object.hasOwn(item, key)
      ) ||
      keys.some((key) => !allowedKeys.has(key)) ||
      !CONTRIBUTION_ID_PATTERN.test(item.local_topic_id) ||
      !CONTRIBUTION_TOPIC_STATES.has(item.state) ||
      typeof item.published !== "boolean" ||
      localIds.has(item.local_topic_id)
    ) {
      throw new TypeError("Invalid contributor topic outcome.");
    }
    localIds.add(item.local_topic_id);
    const canonicalTopicId = Object.hasOwn(item, "canonical_topic_id")
      ? item.canonical_topic_id
      : null;
    if (
      canonicalTopicId !== null &&
      !CONTRIBUTION_CANONICAL_ID_PATTERN.test(canonicalTopicId)
    ) {
      throw new TypeError("Invalid contributor topic outcome.");
    }
    const canonicalTopic = Object.hasOwn(item, "canonical_topic")
      ? normalizeContributionCanonicalTopic(item.canonical_topic)
      : null;
    if (
      (canonicalTopic && canonicalTopic.id !== canonicalTopicId) ||
      (item.published && canonicalTopicId === null)
    ) {
      throw new TypeError("Invalid contributor topic outcome.");
    }
    topics.push({
      local_topic_id: item.local_topic_id,
      state: item.state,
      published: item.published,
      ...(canonicalTopicId === null
        ? {}
        : { canonical_topic_id: canonicalTopicId }),
      ...(canonicalTopic === null ? {} : { canonical_topic: canonicalTopic }),
    });
  }
  return topics;
}

function normalizeContributionCanonicalTopic(value) {
  const expectedKeys = ["aliases", "color", "id", "name"];
  if (
    !isRecord(value) ||
    !sameStringArray(Object.keys(value).sort(), expectedKeys) ||
    !CONTRIBUTION_CANONICAL_ID_PATTERN.test(value.id) ||
    typeof value.name !== "string" ||
    value.name !== value.name.trim() ||
    value.name.length < 1 ||
    value.name.length > 80 ||
    typeof value.color !== "string" ||
    !CONTRIBUTION_COLOR_PATTERN.test(value.color) ||
    !Array.isArray(value.aliases) ||
    value.aliases.length > 20
  ) {
    throw new TypeError("Invalid canonical contributor topic.");
  }
  const aliases = [];
  const seen = new Set();
  for (const alias of value.aliases) {
    if (
      typeof alias !== "string" ||
      alias !== alias.trim() ||
      alias.length < 1 ||
      alias.length > 80 ||
      seen.has(alias.toLocaleLowerCase("en"))
    ) {
      throw new TypeError("Invalid canonical contributor topic.");
    }
    seen.add(alias.toLocaleLowerCase("en"));
    aliases.push(alias);
  }
  return {
    id: value.id,
    name: value.name,
    color: value.color,
    aliases,
  };
}

function normalizeContributionSummary(value) {
  if (
    !isRecord(value) ||
    !sameStringArray(Object.keys(value).sort(), ["events", "topics"]) ||
    !isRecord(value.topics) ||
    !sameStringArray(
      Object.keys(value.topics).sort(),
      [...CONTRIBUTION_TOPIC_SUMMARY_KEYS].sort(),
    ) ||
    !isRecord(value.events) ||
    Object.keys(value.events).some((key) =>
      !CONTRIBUTION_EVENT_SUMMARY_KEYS.includes(key)
    ) ||
    CONTRIBUTION_EVENT_SUMMARY_KEYS.some((key) =>
      !Object.hasOwn(value.events, key) &&
      !CONTRIBUTION_OPTIONAL_EVENT_SUMMARY_KEYS.has(key)
    ) ||
    CONTRIBUTION_TOPIC_SUMMARY_KEYS.some((key) =>
      !Number.isSafeInteger(value.topics[key]) || value.topics[key] < 0
    ) ||
    Object.values(value.events).some((count) =>
      !Number.isSafeInteger(count) || count < 0
    )
  ) {
    throw new TypeError("Invalid contributor review summary.");
  }
  return {
    topics: Object.fromEntries(
      CONTRIBUTION_TOPIC_SUMMARY_KEYS.map((key) => [key, value.topics[key]]),
    ),
    events: Object.fromEntries(
      CONTRIBUTION_EVENT_SUMMARY_KEYS.map((key) => [key, value.events[key] ?? 0]),
    ),
  };
}

function emptyContributionSummary() {
  return {
    topics: Object.fromEntries(
      CONTRIBUTION_TOPIC_SUMMARY_KEYS.map((key) => [key, 0]),
    ),
    events: Object.fromEntries(
      CONTRIBUTION_EVENT_SUMMARY_KEYS.map((key) => [key, 0]),
    ),
  };
}

function markContributionReviewDetails(status, available) {
  Object.defineProperty(status, CONTRIBUTION_REVIEW_DETAILS, {
    value: available === true,
    enumerable: false,
  });
  return status;
}

function unavailableContributionStatus() {
  return {
    enabled: false,
    state: "unavailable",
    can_contribute: false,
    disclosure_required: false,
    topics: [],
    summary: emptyContributionSummary(),
  };
}

function sameStringArray(left, right) {
  return left.length === right.length &&
    left.every((item, index) => item === right[index]);
}

function translationCode(value, fallback) {
  if (typeof value !== "string") {
    return fallback;
  }
  const code = value.toLocaleLowerCase();
  return TRANSLATION_PATTERN.test(code) ? code : fallback;
}

function localeCode(value, fallback) {
  if (
    typeof value !== "string" ||
    value.length > 35 ||
    !/^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(value)
  ) {
    return fallback;
  }
  return value;
}

function boundedInteger(value, minimum, maximum) {
  if (
    typeof value !== "number" ||
    !Number.isInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    return null;
  }
  return value;
}

function boundedText(value, maximum) {
  return typeof value === "string" && value.trim().length <= maximum
    ? value.trim()
    : "";
}

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
