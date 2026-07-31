import assert from "node:assert/strict";
import test from "node:test";

import {
  directSelectionId,
  normalizeBooksPayload,
  normalizeChapterPayload,
  normalizeChaptersPayload,
  normalizeQueryTarget,
  normalizeTranslationCode,
  normalizeTranslationsPayload,
  selectionIdentity,
} from "../lib/getbible-model.js";

test("public catalog payloads are normalized into the Mini App model", () => {
  const translations = normalizeTranslationsPayload({
    kjv: {
      abbreviation: "kjv",
      translation: "King James Version",
      language: "English",
      lang: "en",
      direction: "LTR",
    },
  });
  assert.deepEqual(translations, [{
    code: "kjv",
    name: "King James Version",
    language: "English",
    lang: "en",
    direction: "ltr",
  }]);

  const books = normalizeBooksPayload({
    43: {
      nr: 43,
      abbreviation: "kjv",
      name: "John",
      sha: "a".repeat(40),
    },
  }, "kjv");
  assert.deepEqual(books.items, [{
    number: 43,
    name: "John",
    testament: "new",
    sha: "a".repeat(40),
  }]);

  const chapters = normalizeChaptersPayload({
    3: { chapter: 3, verses: 36 },
  }, { translation: "kjv", book: 43 });
  assert.equal(chapters.items[0].number, 3);
  assert.equal(chapters.items[0].verse_count, 36);
  assert.deepEqual(chapters.items[0].verses.slice(-3), [34, 35, 36]);
});

test("chapter payloads receive deterministic coordinate identities", () => {
  const scripture = normalizeChapterPayload({
    abbreviation: "kjv",
    translation: "King James Version",
    book_nr: 43,
    book_name: "John",
    chapter: 3,
    name: "John 3",
    verses: [
      { verse: 1, text: "There was a man." },
      { verse: 16, text: "For God so loved the world." },
    ],
  }, {
    translation: "kjv",
    book: 43,
    chapter: 3,
    targetVerse: 15,
    sha: "b".repeat(40),
  });

  assert.equal(scripture.target_verse, 16);
  assert.equal(scripture.items[1].selection_id, "gbd_kjv_043_0003_0016");
  assert.equal(selectionIdentity(scripture.items[1]), scripture.items[1].selection_id);
  assert.equal(directSelectionId("KJV", 43, 3, 16), scripture.items[1].selection_id);
});

test("query responses resolve nested reference targets without trusting text", () => {
  const target = normalizeQueryTarget({
    abbreviation: "kjv",
    books: [{
      book_nr: 43,
      book_name: "John",
      chapters: [{
        chapter: 3,
        name: "John 3",
        verses: [{
          verse: 16,
          name: "John 3:16",
          text: "For God so loved the world.",
        }],
      }],
    }],
  }, "kjv");

  assert.deepEqual(target, {
    translation: "kjv",
    book_number: 43,
    book_name: "John",
    chapter: 3,
    verse: 16,
    reference: "John 3:16",
  });
});

test("translation identifiers follow the complete API V2 contract", () => {
  assert.equal(
    normalizeTranslationCode("Example.Translation-2026_1"),
    "example.translation-2026_1",
  );
  assert.throws(
    () => normalizeTranslationCode(`x${"a".repeat(64)}`),
    /invalid/,
  );
});
