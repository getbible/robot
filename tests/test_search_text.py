import unicodedata
import unittest

from modules.search_text import (
    ScriptFamily,
    casefold_text,
    classify_text,
    fold_marks,
    graphemes,
)

# Invisible code points are spelled out so the source never hides one.
ZWJ = chr(0x200D)
ZWNJ = chr(0x200C)
ZWSP = chr(0x200B)
ACUTE = chr(0x0301)
CIRCUMFLEX = chr(0x0302)
VS16 = chr(0xFE0F)
KEYCAP = chr(0x20E3)
SHEVA = chr(0x05B0)
DAGESH = chr(0x05BC)
TSERE = chr(0x05B5)
VIRAMA = chr(0x094D)
THAI_MAI_THO = chr(0x0E49)
THAI_SARA_AM = chr(0x0E33)
SKIN_TONE = chr(0x1F3FD)
CHOSEONG_KIYEOK = chr(0x1100)
JUNGSEONG_A = chr(0x1161)
JONGSEONG_KIYEOK = chr(0x11A8)
OMEGA_WITH_PERISPOMENI_AND_YPOGEGRAMMENI = chr(0x1FF7)
PERISPOMENI = chr(0x0342)


class ClassifyTextTestCase(unittest.TestCase):
    def test_families_are_string_valued(self) -> None:
        self.assertEqual(ScriptFamily.ALPHABETIC, "alphabetic")
        self.assertEqual(ScriptFamily.CONTINUOUS, "continuous")
        self.assertEqual(ScriptFamily.ABJAD, "abjad")
        self.assertEqual(ScriptFamily.BRAHMIC, "brahmic")

    def test_alphabetic_scripts(self) -> None:
        for text in ("grace", "Ðức Chúa Trời", "λόγος", "благодать", "ქართული", "Հայերեն"):
            with self.subTest(text=text):
                self.assertIs(classify_text(text), ScriptFamily.ALPHABETIC)

    def test_continuous_scripts(self) -> None:
        for text in (
            "神爱世人",
            "イエス",
            "ひらがな",
            "하나님",
            unicodedata.normalize("NFD", "하나님"),
            "ㄅㄆㄇ",
            "พระเจ้า",
            "ພຣະເຈົ້າ",
            "ព្រះ",
            "ဘုရားသခင်",
            "བོད་ཡིག",
            "〇々ー",
        ):
            with self.subTest(text=text):
                self.assertIs(classify_text(text), ScriptFamily.CONTINUOUS)

    def test_abjad_scripts(self) -> None:
        for text in ("אלהים", "الْبَدْءِ", "ܐܠܗܐ", "ދިވެހި", "ࠀࠁࠂ", "٣"):
            with self.subTest(text=text):
                self.assertIs(classify_text(text), ScriptFamily.ABJAD)

    def test_brahmic_scripts(self) -> None:
        for text in (
            "यीशु",
            "ঈশ্বর",
            "ਪਰਮੇਸ਼ੁਰ",
            "ઈશ્વર",
            "ପରମେଶ୍ୱର",
            "தேவன்",
            "దేవుడు",
            "ದೇವರು",
            "ദൈവം",
            "දෙවියන්",
        ):
            with self.subTest(text=text):
                self.assertIs(classify_text(text), ScriptFamily.BRAHMIC)

    def test_mixed_text_takes_the_majority_of_letters(self) -> None:
        self.assertIs(classify_text("Jesus 耶穌基督是主"), ScriptFamily.CONTINUOUS)
        self.assertIs(classify_text("אלהים said"), ScriptFamily.ABJAD)
        self.assertIs(classify_text("Amen यीशु"), ScriptFamily.ALPHABETIC)

    def test_marks_punctuation_and_spaces_do_not_vote(self) -> None:
        # Two Hebrew points and one Hebrew letter against two Latin letters.
        self.assertIs(classify_text("ab ב" + SHEVA + DAGESH), ScriptFamily.ALPHABETIC)
        self.assertIs(classify_text("!!! 神 ---"), ScriptFamily.CONTINUOUS)

    def test_ties_and_empty_text_resolve_to_alphabetic(self) -> None:
        self.assertIs(classify_text(""), ScriptFamily.ALPHABETIC)
        self.assertIs(classify_text("   ...  "), ScriptFamily.ALPHABETIC)
        self.assertIs(classify_text("a神"), ScriptFamily.ALPHABETIC)
        self.assertIs(classify_text("神א"), ScriptFamily.CONTINUOUS)


class CasefoldTextTestCase(unittest.TestCase):
    def test_casefold_reaches_final_sigma_and_sharp_s(self) -> None:
        self.assertEqual(casefold_text("ΛΌΓΟΣ λόγος"), "λόγοσ λόγοσ")
        self.assertEqual(casefold_text("Straße"), "strasse")
        # Full case folding expands the iota subscript into a full iota
        # (U+1FF7 -> U+03C9 U+0342 U+03B9), which is why the highlighter
        # casefolds before it folds marks.
        self.assertEqual(
            casefold_text("τ" + OMEGA_WITH_PERISPOMENI_AND_YPOGEGRAMMENI),
            "τω" + PERISPOMENI + "ι",
        )


class FoldMarksTestCase(unittest.TestCase):
    def test_combining_marks_are_removed_and_text_recomposed(self) -> None:
        self.assertEqual(fold_marks("Grâce"), "Grace")
        self.assertEqual(fold_marks(unicodedata.normalize("NFD", "Chúa Trời")), "Chua Troi")
        self.assertEqual(fold_marks("בְּרֵאשִׁית"), "בראשית")
        self.assertEqual(fold_marks("الْبَدْءِ"), "البدء")

    def test_precomposed_letters_fold_by_table(self) -> None:
        cases = {
            "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "ø": "o", "Ø": "O",
            "œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "ł": "l", "Ł": "L",
            "ħ": "h", "Ħ": "H", "ı": "i", "İ": "I", "ŧ": "t", "Ŧ": "T",
            "ŋ": "n", "Ŋ": "N", "ẞ": "SS", "þ": "th", "Þ": "TH",
        }
        for letter, folded in cases.items():
            with self.subTest(letter=letter):
                self.assertEqual(fold_marks(letter), folded)
        self.assertEqual(fold_marks("Ðức"), "Duc")

    def test_ascii_and_precomposed_remainder_are_untouched(self) -> None:
        self.assertEqual(fold_marks("plain ascii"), "plain ascii")
        self.assertEqual(fold_marks("ß"), "ß")
        # Spacing marks are not nonspacing marks, so a Brahmic vowel sign stays
        # even when the caller folds text it should not have.
        self.assertEqual(fold_marks("का"), "का")


class GraphemesTestCase(unittest.TestCase):
    def test_plain_text_splits_per_code_point(self) -> None:
        self.assertEqual(graphemes(""), [])
        self.assertEqual(graphemes("abc 神"), ["a", "b", "c", " ", "神"])

    def test_combining_marks_stay_with_their_base(self) -> None:
        self.assertEqual(graphemes("e" + ACUTE + "x"), ["e" + ACUTE, "x"])
        self.assertEqual(graphemes("e" + ACUTE + CIRCUMFLEX), ["e" + ACUTE + CIRCUMFLEX])
        # Hebrew points (Mn) attach to their consonants.
        self.assertEqual(
            graphemes("ב" + SHEVA + DAGESH + "ר" + TSERE),
            ["ב" + SHEVA + DAGESH, "ר" + TSERE],
        )
        # A Devanagari vowel sign (Mc) attaches too.
        self.assertEqual(graphemes("का"), ["का"])
        # Thai: SARA E (Lo) stands alone, MAI THO (Mn) attaches, SARA AA (Lo)
        # stands alone, but SARA AM attaches like the spacing mark it behaves as.
        self.assertEqual(
            graphemes("เจ" + THAI_MAI_THO + "า"),
            ["เ", "จ" + THAI_MAI_THO, "า"],
        )
        self.assertEqual(
            graphemes("น" + THAI_MAI_THO + THAI_SARA_AM),
            ["น" + THAI_MAI_THO + THAI_SARA_AM],
        )
        # An enclosing mark (Me) attaches.
        self.assertEqual(graphemes("1" + KEYCAP), ["1" + KEYCAP])
        # A leading mark with nothing before it is its own cluster.
        self.assertEqual(graphemes(ACUTE + "x"), [ACUTE, "x"])

    def test_variation_selectors_and_joiners_extend(self) -> None:
        self.assertEqual(graphemes("❤" + VS16 + "x"), ["❤" + VS16, "x"])
        # A joiner in Indic text attaches to the cluster before it and no more.
        self.assertEqual(
            graphemes("क" + VIRAMA + ZWJ + "ष"),
            ["क" + VIRAMA + ZWJ, "ष"],
        )
        self.assertEqual(graphemes("ه" + ZWNJ + "ا"), ["ه" + ZWNJ, "ا"])

    def test_emoji_zwj_sequences_stay_whole(self) -> None:
        family = "👨" + ZWJ + "👩" + ZWJ + "👧"
        thumbs = "👍" + SKIN_TONE
        self.assertEqual(graphemes(family + " " + thumbs), [family, " ", thumbs])

    def test_cr_lf_is_one_cluster_and_controls_stand_alone(self) -> None:
        self.assertEqual(graphemes("a\r\nb\rc\nd"), ["a", "\r\n", "b", "\r", "c", "\n", "d"])
        self.assertEqual(graphemes("\n" + ACUTE), ["\n", ACUTE])
        self.assertEqual(graphemes("a" + ZWSP + "b"), ["a", ZWSP, "b"])

    def test_hangul_jamo_sequences_stay_together(self) -> None:
        decomposed = unicodedata.normalize("NFD", "하나님이 사랑")
        clusters = graphemes(decomposed)
        self.assertEqual(
            [unicodedata.normalize("NFC", cluster) for cluster in clusters],
            ["하", "나", "님", "이", " ", "사", "랑"],
        )
        syllable = CHOSEONG_KIYEOK + JUNGSEONG_A + JONGSEONG_KIYEOK
        self.assertEqual(graphemes(syllable), [syllable])
        double_lead = CHOSEONG_KIYEOK + CHOSEONG_KIYEOK + JUNGSEONG_A
        self.assertEqual(graphemes(double_lead), [double_lead])
        # A trailing consonant cannot start a syllable, so it stands alone.
        self.assertEqual(
            graphemes(JONGSEONG_KIYEOK + CHOSEONG_KIYEOK),
            [JONGSEONG_KIYEOK, CHOSEONG_KIYEOK],
        )
        # A precomposed LV syllable still takes a conjoining tail; an LVT
        # syllable takes no further vowel.
        self.assertEqual(graphemes("가" + JONGSEONG_KIYEOK), ["가" + JONGSEONG_KIYEOK])
        self.assertEqual(graphemes("각" + JUNGSEONG_A), ["각", JUNGSEONG_A])

    def test_clusters_reassemble_the_original_text(self) -> None:
        for text in (
            "Ðức Chúa Trời yêu thương thế gian",
            "ἐν τῷ κόσμῳ ἦν",
            "فِي الْبَدْءِ كَانَ",
            "พระเจ้าทรงรักโลก",
            "यीशु ने कहा",
            unicodedata.normalize("NFD", "하나님이 세상을 이처럼 사랑하사"),
            "a\r\n" + ZWJ + "👨" + ZWJ + "👩",
        ):
            with self.subTest(text=text):
                self.assertEqual("".join(graphemes(text)), text)


if __name__ == "__main__":
    unittest.main()
