import assert from "node:assert/strict";
import test from "node:test";

import { resolveLocale } from "../lib/i18n.js";
import {
  DEFAULT_FILTERS,
  abbreviateBookName,
  activeFilterCount,
  contributionReviewDetailsAvailable,
  entrypointIntent,
  moveItem,
  nearestChapterVerse,
  normalizeBasket,
  normalizeBooks,
  normalizeChapters,
  normalizeContributionStatus,
  normalizeFilters,
  normalizeReaderLocation,
  normalizeScripture,
  normalizeSearch,
  normalizeSession,
  normalizeTranslations,
  normalizeVerses,
  planTranslationChange,
  resolveBibleEntrypoint,
  routeName,
  uniqueBookLabels,
  uniqueVerses,
} from "../lib/model.js";

const verse = {
  selection_id: "Abcdefghijklmnop",
  translation: "kjv",
  reference: "John 3:16",
  book_number: 43,
  book_name: "John",
  chapter: 3,
  verse: 16,
  text: "For God so loved the world.",
  terms: ["God", "world"],
};

test("normalizes the backend session bootstrap without retaining identity", () => {
  const session = normalizeSession({
    user: { id: 42 },
    preferences: {
      translation: "kjv",
      search_defaults: {
        words: "phrase",
        match: "whole_word",
        scope: "bible",
        case_sensitive: false,
        diacritics: "exact",
        sort: "canonical",
      },
      reader_location: {
        translation: "kjv",
        book: 43,
        chapter: 3,
        verse: 16,
      },
    },
    contributions: {
      enabled: true,
      state: "approved",
      can_contribute: true,
      disclosure_required: false,
      topics: [{
        local_topic_id: "my-grace-topic",
        state: "pending",
        published: true,
        canonical_topic_id: "grace",
        canonical_topic: {
          id: "grace",
          name: "Grace",
          color: "#bbf7d0",
          aliases: ["God's Grace"],
        },
      }],
      summary: {
        topics: {
          pending: 1,
          mapped: 0,
          published: 1,
          rejected: 0,
          deferred: 0,
        },
        events: {
          pending: 2,
          approved: 0,
          rejected: 0,
          deferred: 0,
          applied: 3,
        },
      },
    },
    entrypoint: { route: "search", query: "eternal life" },
    translations: [
      {
        code: "kjv",
        name: "King James Version",
        language: "English",
        lang: "en-GB",
        direction: "ltr",
      },
      {
        code: "aov",
        name: "Afrikaanse Ou Vertaling",
        language: "Afrikaans",
        lang: "af",
        direction: "ltr",
      },
    ],
    basket: { count: 1, maximum: 100, items: [verse] },
  });

  assert.equal(session.preferences.translation, "kjv");
  assert.equal(session.translations[0].lang, "en-GB");
  assert.equal(session.translations[0].direction, "ltr");
  assert.equal(session.preferences.search_defaults.words, "phrase");
  assert.deepEqual(session.preferences.reader_location, {
    translation: "kjv",
    book: 43,
    chapter: 3,
    verse: 16,
  });
  assert.equal(session.entrypoint.route, "search");
  assert.equal(session.entrypoint.query, "eternal life");
  assert.equal(session.basket.count, 1);
  assert.equal(session.contributions.can_contribute, true);
  assert.equal(session.contributions.topics[0].published, true);
  assert.equal(
    session.contributions.topics[0].canonical_topic_id,
    "grace",
  );
  assert.equal(session.contributions.summary.events.pending, 2);
  assert.equal(
    contributionReviewDetailsAvailable(session.contributions),
    true,
  );
  assert.equal(session.user, undefined);
});

test("normalizes legacy and current contributor status envelopes", () => {
  const legacy = normalizeContributionStatus({
    enabled: true,
    state: "pending",
    can_contribute: false,
    disclosure_required: false,
  });
  assert.deepEqual(legacy, {
    enabled: true,
    state: "pending",
    can_contribute: false,
    disclosure_required: false,
    topics: [],
    summary: {
      topics: {
        pending: 0,
        mapped: 0,
        published: 0,
        rejected: 0,
        deferred: 0,
      },
      events: {
        pending: 0,
        approved: 0,
        rejected: 0,
        deferred: 0,
        applied: 0,
      },
    },
  });
  assert.equal(contributionReviewDetailsAvailable(legacy), false);
  assert.equal(
    contributionReviewDetailsAvailable(normalizeContributionStatus(legacy)),
    false,
  );

  const detailed = normalizeContributionStatus({
    enabled: true,
    state: "approved",
    can_contribute: true,
    disclosure_required: false,
    topics: [],
    summary: legacy.summary,
  });
  assert.equal(contributionReviewDetailsAvailable(detailed), true);
  assert.equal(
    contributionReviewDetailsAvailable(normalizeContributionStatus(detailed)),
    true,
  );
  assert.deepEqual(Object.keys(detailed).sort(), [
    "can_contribute",
    "disclosure_required",
    "enabled",
    "state",
    "summary",
    "topics",
  ]);
  assert.equal(
    normalizeContributionStatus(undefined).state,
    "unavailable",
  );
});

test("rejects unsafe contributor review outcomes", () => {
  const summary = {
    topics: {
      pending: 0,
      mapped: 0,
      published: 1,
      rejected: 0,
      deferred: 0,
    },
    events: {
      pending: 0,
      approved: 0,
      rejected: 0,
      deferred: 0,
      applied: 1,
    },
  };
  assert.throws(() => normalizeContributionStatus({
    enabled: true,
    state: "approved",
    can_contribute: true,
    disclosure_required: false,
    topics: [{
      local_topic_id: "personal-topic",
      state: "pending",
      published: true,
    }],
    summary,
  }), /topic outcome/i);
  assert.throws(() => normalizeContributionStatus({
    enabled: true,
    state: "approved",
    can_contribute: true,
    disclosure_required: false,
    topics: [],
    summary: {
      ...summary,
      events: { ...summary.events, pending: -1 },
    },
  }), /review summary/i);
});

test("normalizes current backend book and chapter item envelopes", () => {
  assert.deepEqual(
    normalizeBooks({
      translation: "kjv",
      items: [
        { number: 43, name: "John", testament: "new" },
        { number: 1, name: "Genesis", testament: "old" },
      ],
    }),
    [
      { number: 1, name: "Genesis", testament: "old" },
      { number: 43, name: "John", testament: "new" },
    ],
  );
  assert.deepEqual(
    normalizeChapters({
      items: [
        { number: 3, verses: [1, 2, 3] },
        { number: 1, verses: [1] },
      ],
    }),
    [
      { number: 1, verse_count: 1, verses: [1] },
      { number: 3, verse_count: 3, verses: [1, 2, 3] },
    ],
  );
  const longName = "L".repeat(128);
  assert.equal(
    normalizeBooks({
      translation: "kjv",
      items: [{ number: 67, name: longName, testament: "other" }],
    })[0].name,
    longName,
  );
  assert.deepEqual(
    normalizeBooks({
      translation: "kjv",
      items: [{ number: 67, name: "L".repeat(129), testament: "other" }],
    }),
    [],
  );
});

test("accepts the complete upstream catalog response bounds", () => {
  const translations = normalizeTranslations(
    Array.from({ length: 501 }, (_, index) => ({
      code: `t${index}`,
      name: `Translation ${index}`,
      language: "Test",
    })),
  );
  const chapters = normalizeChapters(
    {
      translation: "kjv",
      book: { number: 19 },
      items: Array.from({ length: 251 }, (_, index) => ({
        number: index + 1,
        verses: [1],
      })),
    },
    { translation: "kjv", book: 19 },
  );

  assert.equal(translations.length, 501);
  assert.equal(chapters.length, 251);
  assert.equal(chapters.at(-1).number, 251);
});

test("binds navigation envelopes and clamps unavailable target verses", () => {
  assert.throws(
    () => normalizeBooks(
      { translation: "kjv", items: [{ number: 43, name: "John" }] },
      "aov",
    ),
    /translation did not match/,
  );
  assert.throws(
    () => normalizeChapters(
      {
        translation: "kjv",
        book: { number: 43, name: "John" },
        items: [{ number: 3, verses: [1, 2, 3] }],
      },
      { translation: "kjv", book: 19 },
    ),
    /requested book/,
  );
  const chapter = {
    number: 3,
    verse_count: 3,
    verses: [1, 15, 30],
  };
  assert.equal(nearestChapterVerse(chapter, 31), 30);
  assert.equal(nearestChapterVerse(chapter, 16), 15);
  assert.equal(nearestChapterVerse(chapter, 15), 15);
});

test("derives compact book labels from API-provided localized names", () => {
  assert.equal(abbreviateBookName("Genesis"), "Gen");
  assert.equal(abbreviateBookName("1 John"), "1Jo");
  assert.equal(abbreviateBookName("Song of Solomon"), "SoS");
  assert.equal(abbreviateBookName("创世记"), "创世记");
  assert.equal(abbreviateBookName("A\u0301mos"), "A\u0301mo");
  assert.equal(abbreviateBookName(""), "");
  assert.deepEqual(
    uniqueBookLabels([
      { number: 7, name: "Judges" },
      { number: 50, name: "Philippians" },
      { number: 57, name: "Philemon" },
      { number: 65, name: "Jude" },
    ]),
    ["Judg", "Phili", "Phile", "Jude"],
  );
  assert.equal(
    new Set(uniqueBookLabels([
      { number: 7, name: "Judges" },
      { number: 65, name: "Jude" },
    ])).size,
    2,
  );
});

test("normalizes zero-based search pages and derives pagination", () => {
  const result = normalizeSearch({
    search_id: "SearchTokenValue1",
    query: "God",
    translation: "kjv",
    total: 40,
    available: 25,
    truncated: true,
    page: 0,
    page_count: 3,
    items: [verse],
  });

  assert.equal(result.page, 0);
  assert.equal(result.has_more, true);
  assert.equal(result.results.length, 1);
  assert.deepEqual(result.results[0].highlights, [
    { start: 4, end: 7 },
    { start: 21, end: 26 },
  ]);
});

test("rejects search and Scripture responses from a stale translation", () => {
  assert.throws(
    () => normalizeSearch({
      search_id: "SearchTokenValue1",
      query: "God",
      translation: "kjv",
      total: 1,
      items: [verse],
    }, "aov"),
    /translation did not match/,
  );
  assert.throws(
    () => normalizeScripture({
      translation: "kjv",
      book: { number: 43, name: "John" },
      chapter: 3,
      reference: "John 3",
      target_verse: 16,
      navigation: {},
      items: [verse],
    }, { translation: "aov", book: 43, chapter: 3 }),
    /requested passage/,
  );
});

test("normalizes scripture and basket without accepting malformed selections", () => {
  const scripture = normalizeScripture({
    translation: "kjv",
    book: { number: 43, name: "John" },
    chapter: 3,
    reference: "John 3",
    target_verse: 16,
    sha: "01234567".repeat(5),
    navigation: {
      previous: { book: 43, book_name: "John", chapter: 2 },
      next: { book: 43, book_name: "John", chapter: 4 },
    },
    items: [
      verse,
      { ...verse, selection_id: "invalid token with spaces" },
    ],
  });
  const basket = normalizeBasket({ count: 1, maximum: 100, items: [verse] });

  assert.equal(scripture.verses.length, 1);
  assert.equal(scripture.reference, "John 3");
  assert.equal(scripture.target_verse, 16);
  assert.deepEqual(scripture.navigation.next, {
    book: 43,
    book_name: "John",
    chapter: 4,
  });
  assert.equal(basket.count, 1);
  assert.equal(basket.items[0].text, verse.text);
});

test("plans an immediate translation change without losing reader position", () => {
  const translations = [
    { code: "kjv" },
    { code: "aov" },
  ];
  assert.deepEqual(
    planTranslationChange(
      "AOV",
      translations,
      { translation: "kjv", book: 43, chapter: 3, verse: 16 },
      { route: "bible", hasSearchQuery: true },
    ),
    {
      translation: "aov",
      reader_location: {
        translation: "aov",
        book: 43,
        chapter: 3,
        verse: 16,
      },
      reload_reader: true,
      rerun_search: false,
    },
  );
  assert.equal(
    planTranslationChange("missing", translations, null),
    null,
  );
});

test("keeps long valid translation metadata available to the selector", () => {
  const longName = "S".repeat(135);
  const session = normalizeSession({
    preferences: { translation: "statenvertalinga" },
    translations: [
      {
        code: "statenvertalinga",
        name: longName,
        language: "Dutch",
        lang: "nl",
        direction: "ltr",
      },
    ],
    basket: { items: [] },
  });

  assert.equal(session.translations[0].name, longName);
  assert.equal(session.preferences.translation, "statenvertalinga");
});

test("falls back safely when a saved translation left the catalog", () => {
  const session = normalizeSession({
    preferences: {
      translation: "removed",
      reader_location: {
        translation: "removed",
        book: 43,
        chapter: 3,
        verse: 16,
      },
    },
    translations: [
      {
        code: "kjv",
        name: "King James Version",
        language: "English",
        lang: "en",
        direction: "ltr",
      },
    ],
    basket: { items: [] },
  });

  assert.equal(session.preferences.translation, "kjv");
  assert.equal(session.preferences.reader_location, null);
});

test("reduces reader locations to compact identifiers only", () => {
  assert.deepEqual(
    normalizeReaderLocation({
      translation: "KJV",
      book: 43,
      chapter: 3,
      verse: 16,
    }),
    {
      translation: "kjv",
      book: 43,
      chapter: 3,
      verse: 16,
    },
  );
  assert.deepEqual(
    normalizeReaderLocation({
      translation: "kjv",
      book: 43,
      chapter: 3,
      verse: 16,
      text: "Scripture must not be persisted here.",
    }),
    {
      translation: "kjv",
      book: 43,
      chapter: 3,
      verse: 16,
    },
  );
});

test("bounds filters and counts only non-default search controls", () => {
  const filters = normalizeFilters({
    ...DEFAULT_FILTERS,
    translation: "KJV",
    words: "any",
    scope: "new_testament",
    books: [43, 43, -1],
    exclude: ["grace", "GRACE", "law"],
    proximity: 8,
  });

  assert.equal(filters.translation, "kjv");
  assert.deepEqual(filters.books, [43]);
  assert.deepEqual(filters.exclude, ["grace", "law"]);
  assert.equal(filters.proximity, null);
  assert.equal(activeFilterCount(filters), 4);
});

test("carries Librarian's diacritics vocabulary without translating it", () => {
  assert.equal(DEFAULT_FILTERS.diacritics, "fold");

  for (const value of ["fold", "exact"]) {
    const filters = normalizeFilters({ ...DEFAULT_FILTERS, diacritics: value });
    assert.equal(filters.diacritics, value);
  }

  // Nothing else is a diacritics policy, including the vocabulary Librarian 1.x
  // used. The client sends what the engine accepts rather than mapping onto it.
  for (const value of ["insensitive", "sensitive", "nonsense", "", null]) {
    const filters = normalizeFilters({ ...DEFAULT_FILTERS, diacritics: value });
    assert.equal(filters.diacritics, "fold");
  }
});

test("reorders immutable basket arrays and deduplicates appended pages", () => {
  const second = { ...verse, selection_id: "QrStuvwxyz123456", verse: 17 };
  const original = [verse, second];
  const moved = moveItem(original, 1, -1);

  assert.deepEqual(moved.map((item) => item.verse), [17, 16]);
  assert.deepEqual(original.map((item) => item.verse), [16, 17]);
  assert.equal(uniqueVerses([verse], [verse, second]).length, 2);
});

test("falls back unknown routes to the protected home screen", () => {
  assert.equal(routeName("bible"), "bible");
  assert.equal(routeName("history"), "history");
  assert.equal(routeName("bookmarks"), "bookmarks");
  assert.equal(routeName("https://example.com"), "home");
});

test("keeps Bible fragments separate from executable search entrypoints", () => {
  assert.deepEqual(entrypointIntent({ route: "history", query: "ignored" }), {
    route: "home",
    search_query: "",
    bible_reference: "",
  });
  assert.deepEqual(entrypointIntent({ route: "bible", query: "John 3" }), {
    route: "bible",
    search_query: "",
    bible_reference: "John 3",
  });
  assert.deepEqual(entrypointIntent({ route: "search", query: "grace" }), {
    route: "search",
    search_query: "grace",
    bible_reference: "",
  });
  assert.deepEqual(entrypointIntent({
    route: "bookmarks",
    bookmark_restore_available: true,
  }), {
    route: "bookmarks",
    search_query: "",
    bible_reference: "",
    bookmark_restore_available: true,
  });
});

test("resolves API book names through the complete supported length", () => {
  const name = "A".repeat(128);
  assert.deepEqual(
    resolveBibleEntrypoint(`${name} 3`, [{ number: 67, name }]),
    { book_number: 67, chapter: 3 },
  );
});

test("resolves incomplete Bible entrypoints without treating them as searches", () => {
  const books = [
    { number: 43, name: "John" },
    { number: 62, name: "1 John" },
  ];

  assert.deepEqual(resolveBibleEntrypoint("John", books, ["kjv"]), {
    book_number: 43,
    chapter: null,
  });
  assert.deepEqual(resolveBibleEntrypoint("John 3 kjv", books, ["kjv"]), {
    book_number: 43,
    chapter: 3,
  });
  assert.deepEqual(resolveBibleEntrypoint("1 John", books, ["kjv"]), {
    book_number: 62,
    chapter: null,
  });
  assert.equal(resolveBibleEntrypoint("John 3:16", books, ["kjv"]), null);
  assert.equal(resolveBibleEntrypoint("unknown", books, ["kjv"]), null);
});

test("resolves interface locales by exact locale, base language, then English", () => {
  const available = ["en", "pt", "pt-br"];

  assert.equal(resolveLocale("pt-BR", available), "pt-br");
  assert.equal(resolveLocale("pt-AO", available), "pt");
  assert.equal(resolveLocale("zu-ZA", available), "en");
  assert.equal(resolveLocale("not a locale", available), "en");
});

// --- search-term highlighting ------------------------------------------------
// Librarian returns the terms it matched in its own analysed form — folded and
// casefolded — while the verse arrives as written. Matching those terms
// literally against the raw verse finds nothing the moment folding does any
// work, which is every accented, pointed or unvowelled script.

function marked(text, terms, diacritics = "fold") {
  const payload = {
    selection_id: "AbCdEfGhIjKlMnOp",
    translation: "kjv",
    reference: "John 1:1",
    book_number: 43,
    chapter: 1,
    verse: 1,
    text,
    terms,
  };
  const [normalized] = normalizeVerses([payload], diacritics);
  return normalized.highlights.map((span) => text.slice(span.start, span.end));
}

test("highlights folded matches in every writing system Librarian reaches", () => {
  const cases = [
    ["λόγος ἦν πρὸς τὸν θεόν", ["λογος"], ["λόγος"], "Greek, unaccented query"],
    ["Ðức Chúa Trời yêu", ["duc", "troi"], ["Ðức", "Trời"], "precomposed Latin"],
    ["בְּרֵאשִׁית בָּרָא", ["בראשית"], ["בְּרֵאשִׁית"], "Hebrew, unpointed query"],
    ["فِي الْبَدْءِ كَانَ", ["بدء"], ["بَدْءِ"], "Arabic stem behind a particle"],
    ["神爱世人，甚至将他的独生子", ["神爱"], ["神爱"], "Han"],
    ["하나님이 세상을 이처럼 사랑하사", ["사랑"], ["사랑"], "Hangul"],
    ["พระเจ้าทรงรักโลก", ["พระเจ้า"], ["พระเจ้า"], "Thai"],
    ["यीशु ने कहा", ["यीशु"], ["यीशु"], "Devanagari, marks kept"],
    ["God so loved the world", ["loved"], ["loved"], "Latin control"],
  ];
  for (const [text, terms, expected, label] of cases) {
    assert.deepEqual(marked(text, terms), expected, label);
  }
});

test("ends a highlighted word at a trailing apostrophe", () => {
  // Librarian carries a word through an apostrophe only when a letter follows,
  // so it indexes `priests'` as the unit `priests` and returns the verse.
  assert.deepEqual(
    marked("minister in the priests' office", ["priests"]),
    ["priests"],
  );
  assert.deepEqual(marked("the sons of d'Israel", ["israel"]), []);
});

test("keeps a folded trailing mark inside the span it belongs to", () => {
  // The kasra after the hamza is part of the matched stem; closing the span
  // before it would split a letter from its own vowel.
  const [span] = marked("فِي الْبَدْءِ كَانَ", ["بدء"]);
  assert.equal(span, "بَدْءِ");
});

test("does not fold when the reader asked for exact diacritics", () => {
  // Under `exact` the engine distinguishes pointed from unpointed, so an
  // unaccented term is not a match to mark.
  assert.deepEqual(marked("λόγος ἦν", ["λογος"], "exact"), []);
  assert.deepEqual(marked("λόγος ἦν", ["λόγος"], "exact"), ["λόγος"]);
});

test("marks whole words only, and never overlapping spans", () => {
  assert.deepEqual(marked("grace and greatness", ["great"]), []);
  assert.deepEqual(
    marked("grace upon grace", ["grace"]),
    ["grace", "grace"],
  );
});

test("survives terms that normalize away or are absent", () => {
  assert.deepEqual(marked("God so loved", ["", "  "]), []);
  assert.deepEqual(marked("God so loved", ["absent"]), []);
  assert.deepEqual(marked("God so loved", []), []);
  // A verse carrying no text is not a verse; the model drops it outright.
  assert.deepEqual(normalizeVerses([{ ...verse, text: "" }]), []);
});

test("marks only the abjad stems Librarian actually derives", () => {
  // `ולאמר` analyses to ['ולאמר','אמר'] because `ול` is a closed-class
  // particle; `ויאמר` analyses to ['ויאמר','יאמר'] and never yields `אמר`.
  assert.deepEqual(
    marked("ויאמר אלהים ולאמר הנביא אמר יהוה", ["אמר"]),
    ["אמר", "אמר"],
  );
  assert.deepEqual(marked("יהי אור והאור טוב", ["אור"]), ["אור", "אור"]);
  // Three letters is the floor below which no stem is derived, so `ובן` keeps
  // its particle and only the bare word matches.
  assert.deepEqual(marked("ובן האיש בן", ["בן"]), ["בן"]);
});
