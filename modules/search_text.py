"""Script-aware text helpers that keep search highlighting honest.

The search service analyses each query and verse under the rules of its own
writing system: it composes to NFC, casefolds, and folds accents only where a
mark is an accent rather than a vowel.  A highlight can only mark what that
analysis matched, so the robot has to normalise a verse the same way before it
looks for a term inside it.  This module is that shared vocabulary, written
against the standard library alone so it stays a leaf of the architecture.

The same rules live in the Mini App (``miniapp/lib/model.js``: ``scriptFamily``,
``casefoldText``, ``foldMarks`` and ``graphemes``).  Keep the two in step.
"""

from __future__ import annotations

import unicodedata
from enum import Enum
from functools import lru_cache

__all__ = [
    "ScriptFamily",
    "casefold_text",
    "classify_text",
    "fold_marks",
    "graphemes",
]


class ScriptFamily(str, Enum):
    """How a writing system delimits and inflects its searchable units."""

    #: Latin, Cyrillic, Greek, Armenian, Georgian, Coptic, Cherokee and peers.
    #: Spaces delimit words; combining marks are accents that readers omit.
    ALPHABETIC = "alphabetic"

    #: Han, kana, Hangul, Bopomofo, Thai, Lao, Khmer, Myanmar and Tibetan.
    #: Either nothing delimits the units, or the delimiter does not bound a
    #: searchable word, so there is no boundary to test.
    CONTINUOUS = "continuous"

    #: Hebrew, Arabic, Syriac, Thaana, Samaritan. Spaces delimit words, vowel
    #: pointing is optional, and closed-class particles attach to the word.
    ABJAD = "abjad"

    #: Devanagari, Bengali, Tamil, Sinhala and peers. Spaces delimit words, but
    #: combining marks carry vowels and must never be folded away.
    BRAHMIC = "brahmic"


# Membership is decided by the Unicode character name, which the standard
# library exposes for every assigned code point. A name prefix identifies the
# script closely enough for a majority vote: "CJK UNIFIED IDEOGRAPH-4E00",
# "HANGUL SYLLABLE GA", "ARABIC-INDIC DIGIT ONE", "DEVANAGARI LETTER A". The
# families mirror the search service and the Mini App exactly; a script listed
# nowhere is alphabetic, which is the family with the plainest rules.
_CONTINUOUS_PREFIXES = (
    "CJK",
    "IDEOGRAPHIC",
    "KANGXI RADICAL",
    "HANGZHOU NUMERAL",
    "HIRAGANA",
    "KATAKANA",
    "HALFWIDTH KATAKANA",
    "HANGUL",
    "HALFWIDTH HANGUL",
    "BOPOMOFO",
    "THAI",
    "LAO",
    "KHMER",
    "MYANMAR",
    "TIBETAN",
)
_ABJAD_PREFIXES = ("HEBREW", "ARABIC", "SYRIAC", "THAANA", "SAMARITAN")
_BRAHMIC_PREFIXES = (
    "DEVANAGARI",
    "BENGALI",
    "GURMUKHI",
    "GUJARATI",
    "ORIYA",
    "TAMIL",
    "TELUGU",
    "KANNADA",
    "MALAYALAM",
    "SINHALA",
)

# Precomposed letters that Unicode decomposition cannot reach. NFD turns "é"
# into "e" plus a combining mark, but "đ" and "ø" are atomic code points, so a
# reader typing "Duc Chua Troi" would still miss "Ðức Chúa Trời" without this.
_PRECOMPOSED_FOLD = str.maketrans(
    {
        "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
        "ø": "o", "Ø": "O", "œ": "oe", "Œ": "OE",
        "æ": "ae", "Æ": "AE", "ł": "l", "Ł": "L",
        "ħ": "h", "Ħ": "H", "ı": "i", "İ": "I",
        "ŧ": "t", "Ŧ": "T", "ŋ": "n", "Ŋ": "N",
        "ẞ": "SS", "þ": "th", "Þ": "TH",
    }
)

_ZERO_WIDTH_JOINER = chr(0x200D)
_ZERO_WIDTH_NON_JOINER = chr(0x200C)
#: Spacing vowel signs that Unicode attaches to the preceding cluster although
#: their general category is a letter (Thai and Lao SARA AM).
_SPACING_VOWEL_SIGNS = frozenset({chr(0x0E33), chr(0x0EB3)})
#: Emoji skin-tone modifiers extend the pictograph before them although their
#: general category is a modifier symbol rather than a mark.
_EMOJI_MODIFIERS = frozenset(chr(code) for code in range(0x1F3FB, 0x1F400))
#: Categories that never take a mark: controls, format characters and line or
#: paragraph separators each stand alone.
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
#: Categories whose members attach to the cluster before them: nonspacing,
#: spacing and enclosing marks, which cover variation selectors too.
_MARK_CATEGORIES = frozenset({"Mn", "Mc", "Me"})


@lru_cache(maxsize=8192)
def _family_of(character: str) -> ScriptFamily:
    name = unicodedata.name(character, "")
    if name.startswith(_CONTINUOUS_PREFIXES):
        return ScriptFamily.CONTINUOUS
    if name.startswith(_ABJAD_PREFIXES):
        return ScriptFamily.ABJAD
    if name.startswith(_BRAHMIC_PREFIXES):
        return ScriptFamily.BRAHMIC
    return ScriptFamily.ALPHABETIC


def classify_text(text: str) -> ScriptFamily:
    """Return the family that dominates one string.

    Letters and numbers vote; marks, punctuation and spaces abstain. A tie, or
    a string with nothing to count, resolves to alphabetic, the family whose
    rules assume the least about the text.
    """
    counts: dict[ScriptFamily, int] = dict.fromkeys(ScriptFamily, 0)
    for character in text:
        if unicodedata.category(character)[0] in "LN":
            counts[_family_of(character)] += 1
    return max(counts, key=counts.__getitem__)


def casefold_text(text: str) -> str:
    """Casefold for comparison; Greek final sigma folds to σ like any other."""
    return text.casefold()


def fold_marks(text: str) -> str:
    """Remove combining marks and fold precomposed letters to their base.

    Applied to alphabetic and abjad text, where marks are accents or optional
    vowel pointing. Never applied to Brahmic or continuous text, where marks
    carry vowels that change the word.
    """
    if text.isascii():
        return text
    decomposed = unicodedata.normalize("NFD", text.translate(_PRECOMPOSED_FOLD))
    stripped = "".join(
        character for character in decomposed if unicodedata.category(character) != "Mn"
    )
    return unicodedata.normalize("NFC", stripped)


def _hangul_type(character: str) -> str | None:
    """Return the conjoining-jamo class of one code point, if it has one."""
    code = ord(character)
    if 0x1100 <= code <= 0x115F or 0xA960 <= code <= 0xA97C:
        return "L"
    if 0x1160 <= code <= 0x11A7 or 0xD7B0 <= code <= 0xD7C6:
        return "V"
    if 0x11A8 <= code <= 0x11FF or 0xD7CB <= code <= 0xD7FB:
        return "T"
    if 0xAC00 <= code <= 0xD7A3:
        return "LV" if (code - 0xAC00) % 28 == 0 else "LVT"
    return None


def _hangul_joins(previous: str | None, following: str | None) -> bool:
    """Apply the Hangul syllable rules (UAX #29 GB6, GB7 and GB8)."""
    if previous is None or following is None:
        return False
    if previous == "L":
        return following in {"L", "V", "LV", "LVT"}
    if previous in {"LV", "V"}:
        return following in {"V", "T"}
    return following == "T"


def _extends(character: str) -> bool:
    """Report whether a code point attaches to the cluster before it."""
    if character in (_ZERO_WIDTH_JOINER, _ZERO_WIDTH_NON_JOINER):
        return True
    if character in _SPACING_VOWEL_SIGNS or character in _EMOJI_MODIFIERS:
        return True
    return unicodedata.category(character) in _MARK_CATEGORIES


def _is_control(character: str) -> bool:
    if character in (_ZERO_WIDTH_JOINER, _ZERO_WIDTH_NON_JOINER):
        return False
    return unicodedata.category(character) in _CONTROL_CATEGORIES


def _pictographic(character: str) -> bool:
    return unicodedata.category(character) == "So"


def graphemes(text: str) -> list[str]:
    """Split text into user-perceived characters.

    This approximates Unicode extended grapheme clusters (UAX #29) without a
    third-party segmenter, which is enough to keep a highlight span from ending
    between a letter and its own mark. One cluster is:

    * a base code point followed by any combining marks (general categories
      Mn, Mc and Me, which include the variation selectors), the zero-width
      joiner and non-joiner, Thai and Lao SARA AM, and emoji skin-tone
      modifiers;
    * a pictograph joined to the next pictograph through a zero-width joiner,
      so an emoji sequence stays whole;
    * a Hangul syllable written as conjoining jamo (leading, vowel, trailing);
    * CR LF, while every other control or format character stands alone.

    Regional-indicator pairs and prepended format characters are not joined,
    and spacing marks that Unicode exempts from attaching are still attached.
    Neither affects how a span maps back onto Scripture text.
    """
    clusters: list[str] = []
    length = len(text)
    index = 0
    while index < length:
        start = index
        base = text[index]
        index += 1
        if base == "\r" and index < length and text[index] == "\n":
            clusters.append("\r\n")
            index += 1
            continue
        if _is_control(base):
            clusters.append(base)
            continue
        previous_jamo = _hangul_type(base)
        pictograph = _pictographic(base)
        while index < length:
            following = text[index]
            if _hangul_joins(previous_jamo, _hangul_type(following)):
                previous_jamo = _hangul_type(following)
                index += 1
                continue
            if not _extends(following):
                break
            index += 1
            previous_jamo = None
            if (
                following == _ZERO_WIDTH_JOINER
                and pictograph
                and index < length
                and _pictographic(text[index])
            ):
                index += 1
        clusters.append(text[start:index])
    return clusters
