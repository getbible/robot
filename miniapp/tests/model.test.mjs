import assert from "node:assert/strict";
import test from "node:test";

import { resolveLocale } from "../lib/i18n.js";
import {
  DEFAULT_FILTERS,
  activeFilterCount,
  moveItem,
  normalizeBasket,
  normalizeBooks,
  normalizeChapters,
  normalizeFilters,
  normalizeScripture,
  normalizeSearch,
  normalizeSession,
  routeName,
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
        diacritics: "sensitive",
        sort: "canonical",
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
    ],
    basket: { count: 1, maximum: 100, items: [verse] },
  });

  assert.equal(session.preferences.translation, "kjv");
  assert.equal(session.translations[0].lang, "en-GB");
  assert.equal(session.translations[0].direction, "ltr");
  assert.equal(session.preferences.search_defaults.words, "phrase");
  assert.equal(session.entrypoint.route, "search");
  assert.equal(session.entrypoint.query, "eternal life");
  assert.equal(session.basket.count, 1);
  assert.equal(session.user, undefined);
});

test("normalizes current backend book and chapter item envelopes", () => {
  assert.deepEqual(
    normalizeBooks({
      translation: "kjv",
      items: [
        { number: 43, name: "John" },
        { number: 1, name: "Genesis" },
      ],
    }),
    [
      { number: 1, name: "Genesis", testament: null },
      { number: 43, name: "John", testament: null },
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
      { number: 1, verse_count: 1 },
      { number: 3, verse_count: 3 },
    ],
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

test("normalizes scripture and basket without accepting malformed selections", () => {
  const scripture = normalizeScripture({
    translation: "kjv",
    book: { number: 43, name: "John" },
    chapter: 3,
    items: [
      verse,
      { ...verse, selection_id: "invalid token with spaces" },
    ],
  });
  const basket = normalizeBasket({ count: 1, maximum: 100, items: [verse] });

  assert.equal(scripture.verses.length, 1);
  assert.equal(basket.count, 1);
  assert.equal(basket.items[0].text, verse.text);
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
  assert.equal(routeName("https://example.com"), "home");
});

test("resolves interface locales by exact locale, base language, then English", () => {
  const available = ["en", "pt", "pt-br"];

  assert.equal(resolveLocale("pt-BR", available), "pt-br");
  assert.equal(resolveLocale("pt-AO", available), "pt");
  assert.equal(resolveLocale("zu-ZA", available), "en");
  assert.equal(resolveLocale("not a locale", available), "en");
});
