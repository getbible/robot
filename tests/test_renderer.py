import unittest

from getbible import RequestLimitError

from modules.errors import ScriptureUnavailable
from modules.renderer import render_scripture


def _telegram_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


class RendererTestCase(unittest.TestCase):
    def result(self, text: str = "For God so loved the world.") -> dict:
        return {
            "kjv_43_3": {
                "book_name": 'John <script>&"',
                "abbreviation": "kjv",
                "chapter": 3,
                "verses": [
                    {"verse": 16, "text": text},
                    {"verse": 17, "text": "Not <b>condemn</b> & save."},
                    {"verse": 19, "text": "Light came into the world."},
                ],
            }
        }

    def test_html_and_link_segments_are_escaped(self) -> None:
        chunks = render_scripture(self.result(), "https://getbible.life")
        rendered = "\n".join(chunks)
        self.assertIn(
            "https://getbible.life/kjv/John%20%3Cscript%3E%26%22/3/16-17%2C19",
            rendered,
        )
        self.assertIn("John &lt;script&gt;&amp;&quot;", rendered)
        self.assertIn("Not &lt;b&gt;condemn&lt;/b&gt; &amp; save.", rendered)
        self.assertNotIn("<script>", rendered)

    def test_consecutive_verses_use_one_newline_without_blank_paragraphs(self) -> None:
        chunks = render_scripture(
            {
                "kjv_62_3": {
                    "book_name": "1 John",
                    "abbreviation": "kjv",
                    "chapter": 3,
                    "verses": [
                        {"verse": 10, "text": "Verse ten."},
                        {"verse": 11, "text": "Verse eleven."},
                        {"verse": 12, "text": "Verse twelve."},
                    ],
                }
            },
            "https://getbible.life",
        )

        self.assertEqual(
            chunks,
            [
                '<b><a href="https://getbible.life/kjv/1%20John/3/10-12">'
                "1 John 3:10-12</a></b> <code>kjv</code>\n"
                "<b>10.</b> Verse ten.\n"
                "<b>11.</b> Verse eleven.\n"
                "<b>12.</b> Verse twelve."
            ],
        )
        self.assertNotIn("\n\n", chunks[0])

    def test_messages_are_split_below_telegram_limit_without_broken_entities(self) -> None:
        chunks = render_scripture(
            self.result("<&>" * 500),
            "https://getbible.life",
            chunk_limit=512,
            max_chunks=32,
        )
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(_telegram_length(chunk) <= 512 for chunk in chunks))
        for chunk in chunks:
            self.assertNotRegex(chunk, r"&(?:a|am|amp|l|lt|g|gt|q|quo|quot)?\Z")

    def test_astral_unicode_is_measured_in_telegram_utf16_units(self) -> None:
        chunks = render_scripture(
            self.result("😀" * 1000),
            "https://getbible.life",
            chunk_limit=256,
            max_chunks=32,
        )
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(_telegram_length(chunk) <= 256 for chunk in chunks))

    def test_response_message_count_is_bounded(self) -> None:
        with self.assertRaises(RequestLimitError):
            render_scripture(
                self.result("<&>" * 5000),
                "https://getbible.life",
                chunk_limit=512,
                max_chunks=1,
            )

    def test_empty_results_fail_closed(self) -> None:
        with self.assertRaises(ScriptureUnavailable):
            render_scripture({}, "https://getbible.life")


if __name__ == "__main__":
    unittest.main()
