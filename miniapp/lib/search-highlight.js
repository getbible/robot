// Search-term highlighting shared by the reader model and the Search API
// model. The engine returns the terms it matched in its own analysed form and
// the verse arrives as it is written, so both the robot-shaped basket rows and
// the direct Search API results need one implementation of the same reading.

//: The Search API folds accents, vowel pointing and precomposed letters by
//: default, which is what lets unaccented Greek and unpointed Hebrew reach the
//: text. Callers pass the policy the query actually ran under.
export const DEFAULT_DIACRITICS = "fold";
const GRAPHEME_SEGMENTER = typeof Intl.Segmenter === "function"
  ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
  : null;

// Librarian's analysis, mirrored. The engine returns the terms it matched in
// its own analysed form — folded, casefolded — while the verse arrives as it is
// written. Matching those terms literally against the raw verse therefore finds
// nothing the moment folding does any work, which is every accented, pointed or
// unvowelled script. Both sides have to be read the same way, by the same rules.

//: Letters Unicode decomposition cannot reach, folded by table exactly as
//: Librarian folds them, so `Duc` can mark `Ðức`.
const PRECOMPOSED_FOLD = new Map(
  Object.entries({
    đ: "d", Đ: "D", ð: "d", Ð: "D", ø: "o", Ø: "O", œ: "oe", Œ: "OE",
    æ: "ae", Æ: "AE", ł: "l", Ł: "L", ħ: "h", Ħ: "H", ı: "i", İ: "I",
    ŧ: "t", Ŧ: "T", ŋ: "n", Ŋ: "N", ẞ: "SS", þ: "th", Þ: "TH",
  }),
);

//: Where JavaScript's toLowerCase and Python's casefold disagree. Greek final
//: sigma is the one that matters most here: the engine casefolds `ς` to `σ`,
//: and a Greek verse ends most of its words with it.
const CASEFOLD_EXTRA = new Map(
  Object.entries({ ς: "σ", ß: "ss", ﬁ: "fi", ﬂ: "fl", ﬀ: "ff" }),
);

// Thai, Lao, Khmer and Myanmar reach Librarian's continuous family through
// Line_Break=Complex_Context, which JavaScript does not expose; they are named
// here instead. Everything else is the same Script_Extensions test.
const CONTINUOUS_RE =
  /[\p{Script_Extensions=Han}\p{Script_Extensions=Hiragana}\p{Script_Extensions=Katakana}\p{Script_Extensions=Hangul}\p{Script_Extensions=Tibetan}\p{Script_Extensions=Thai}\p{Script_Extensions=Lao}\p{Script_Extensions=Khmer}\p{Script_Extensions=Myanmar}]/u;
const ABJAD_RE =
  /[\p{Script_Extensions=Hebrew}\p{Script_Extensions=Arabic}\p{Script_Extensions=Syriac}\p{Script_Extensions=Thaana}\p{Script_Extensions=Samaritan}]/u;
const BRAHMIC_RE =
  /[\p{Script_Extensions=Devanagari}\p{Script_Extensions=Bengali}\p{Script_Extensions=Gurmukhi}\p{Script_Extensions=Gujarati}\p{Script_Extensions=Oriya}\p{Script_Extensions=Tamil}\p{Script_Extensions=Telugu}\p{Script_Extensions=Kannada}\p{Script_Extensions=Malayalam}\p{Script_Extensions=Sinhala}]/u;
// Closed-class particles that attach to the front of an abjad word. Librarian
// indexes the stem behind one of these and nothing else, so `אור` reaches
// `והאור` while `אמר` never reaches `ויאמר` — that word yields `יאמר`.
const ABJAD_PROCLITICS = new Set([
  "ו", "ה", "ב", "ל", "כ", "מ", "ש",
  "וה", "וב", "ול", "וכ", "ומ", "וש",
  "ال", "و", "ف", "ب", "ل", "ك",
  "وال", "فال", "بال", "لل", "كال",
]);
// Hebrew and Arabic roots are overwhelmingly triliteral; Librarian will not
// invent a stem shorter than this.
const MIN_ABJAD_STEM = 3;
const LETTER_RE = /[\p{L}\p{N}]/u;
const MARK_RE = /\p{M}/u;
const COMBINING_RE = /\p{Mn}/u;

export function scriptFamily(value) {
  const counts = { continuous: 0, abjad: 0, brahmic: 0, alphabetic: 0 };
  for (const character of value) {
    if (!LETTER_RE.test(character)) continue;
    if (CONTINUOUS_RE.test(character)) counts.continuous += 1;
    else if (ABJAD_RE.test(character)) counts.abjad += 1;
    else if (BRAHMIC_RE.test(character)) counts.brahmic += 1;
    else counts.alphabetic += 1;
  }
  let family = "alphabetic";
  let best = 0;
  for (const [name, count] of Object.entries(counts)) {
    if (count > best) {
      best = count;
      family = name;
    }
  }
  return family;
}

export function casefoldText(value) {
  let folded = "";
  for (const character of value.toLowerCase()) {
    folded += CASEFOLD_EXTRA.get(character) ?? character;
  }
  return folded;
}

export function foldMarks(value) {
  let translated = "";
  for (const character of value) {
    translated += PRECOMPOSED_FOLD.get(character) ?? character;
  }
  let stripped = "";
  for (const character of translated.normalize("NFD")) {
    if (!COMBINING_RE.test(character)) stripped += character;
  }
  return stripped.normalize("NFC");
}

// Compose, then case, then marks — Librarian's order. Casefolding a Greek iota
// subscript expands it into a full iota, so the sequence decides whether `τῷ`
// becomes the `τωι` the engine indexed or a bare `τω` that matches nothing.
export function normalizedSearchValue(value, fold) {
  let normalized = "";
  const starts = [];
  const ends = [];
  let offset = 0;
  for (const grapheme of graphemes(value)) {
    const end = offset + grapheme.length;
    let piece = casefoldText(grapheme.normalize("NFC"));
    if (fold) piece = foldMarks(piece);
    for (const character of piece) {
      normalized += character;
      starts.push(offset);
      ends.push(end);
    }
    offset = end;
  }
  return { normalized, starts, ends };
}

// An apostrophe carries a word onward only when a letter follows, which is why
// the engine indexes `priests'` as `priests`.
function continuesWord(value, index) {
  if (index < 0 || index >= value.length) return false;
  const character = value[index];
  if (character === "'" || character === "’") {
    const following = value[index + 1] ?? "";
    return following !== "" && LETTER_RE.test(following);
  }
  return LETTER_RE.test(character) || MARK_RE.test(character);
}

export function termHighlights(text, terms, diacritics = DEFAULT_DIACRITICS) {
  const candidates = [];
  const prepared = new Map();
  for (const term of terms) {
    const stripped = term.trim();
    if (!stripped) continue;
    const family = scriptFamily(stripped);
    // Brahmic and continuous marks carry vowels, so Librarian never folds them.
    const fold =
      diacritics === "fold" && (family === "alphabetic" || family === "abjad");
    if (!prepared.has(fold)) {
      prepared.set(fold, normalizedSearchValue(text, fold));
    }
    const { normalized, starts, ends } = prepared.get(fold);
    if (!normalized) continue;
    const needle = normalizedSearchValue(stripped, fold).normalized;
    if (!needle) continue;
    // A continuous script has no word boundary to test; an abjad stem sits
    // behind an attached particle, so only its trailing edge is one.
    const delimited = family !== "continuous";
    let from = 0;
    for (;;) {
      const at = normalized.indexOf(needle, from);
      if (at < 0) break;
      const stop = at + needle.length;
      from = Math.max(stop, at + 1);
      if (delimited) {
        let leading = continuesWord(normalized, at - 1);
        if (leading && family === "abjad") {
          // Only a closed-class particle may sit in front, and only ahead of a
          // stem long enough for Librarian to have derived one.
          let wordStart = at;
          while (continuesWord(normalized, wordStart - 1)) wordStart -= 1;
          leading = !(
            needle.length >= MIN_ABJAD_STEM &&
            ABJAD_PROCLITICS.has(normalized.slice(wordStart, at))
          );
        }
        if (leading || continuesWord(normalized, stop)) continue;
      }
      candidates.push({ start: starts[at], end: ends[stop - 1] });
    }
  }
  const result = [];
  for (const span of candidates.sort(
    (left, right) => left.start - right.start || right.end - left.end,
  )) {
    if (result.length === 0 || span.start >= result[result.length - 1].end) {
      result.push(span);
    }
  }
  return result;
}

export function graphemes(value) {
  if (GRAPHEME_SEGMENTER) {
    return [...GRAPHEME_SEGMENTER.segment(value)].map((item) => item.segment);
  }
  // Without a segmenter, keep each combining sequence with its base. Splitting
  // a letter from its own mark would end a highlight span between the two.
  const parts = [];
  for (const character of value) {
    if (parts.length > 0 && MARK_RE.test(character)) {
      parts[parts.length - 1] += character;
    } else {
      parts.push(character);
    }
  }
  return parts;
}
