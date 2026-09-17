import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_DIACRITICS,
  graphemes,
  normalizedSearchValue,
  scriptFamily,
  termHighlights,
} from "../lib/search-highlight.js";

// The Search API returns the terms it matched in its own analysed form —
// folded and casefolded — while the verse arrives as written. Matching those
// terms literally against the raw verse finds nothing the moment folding does
// any work, which is every accented, pointed or unvowelled script.

function marked(text, terms, diacritics = DEFAULT_DIACRITICS) {
  return termHighlights(text, terms, diacritics)
    .map((span) => text.slice(span.start, span.end));
}

test("highlights folded matches in every writing system the engine reaches", () => {
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
  // The engine carries a word through an apostrophe only when a letter
  // follows, so it indexes `priests'` as the unit `priests`.
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
  assert.deepEqual(
    termHighlights("grace upon grace", ["grace", "grace upon"]),
    [{ start: 0, end: 10 }, { start: 11, end: 16 }],
  );
});

test("survives terms that normalize away or are absent", () => {
  assert.deepEqual(marked("God so loved", ["", "  "]), []);
  assert.deepEqual(marked("God so loved", ["absent"]), []);
  assert.deepEqual(marked("God so loved", []), []);
  assert.deepEqual(termHighlights("", ["god"]), []);
});

test("marks only the abjad stems the engine actually derives", () => {
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

test("classifies terms by the script that carries their letters", () => {
  assert.equal(scriptFamily("λόγος"), "alphabetic");
  assert.equal(scriptFamily("神爱世人"), "continuous");
  assert.equal(scriptFamily("בראשית"), "abjad");
  assert.equal(scriptFamily("यीशु"), "brahmic");
  assert.equal(scriptFamily("John 3:16"), "alphabetic");
});

test("normalized offsets map every folded character back into the raw text", () => {
  const { normalized, starts, ends } = normalizedSearchValue("Ðức Trời", true);

  assert.equal(normalized, "duc troi");
  assert.equal(starts.length, normalized.length);
  assert.equal(ends.length, normalized.length);
  assert.equal(starts[0], 0);
  assert.equal(ends[2], 3);
  assert.equal(starts[4], 4);
  assert.equal(ends.at(-1), "Ðức Trời".length);
});

test("keeps combining marks with their base when splitting graphemes", () => {
  assert.deepEqual(graphemes("बा"), ["बा"]);
  assert.deepEqual(graphemes("éa"), ["é", "a"]);
  assert.deepEqual(graphemes(""), []);
});
