import unittest

from modules.renderer import render_scripture


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
        self.assertIn("https://getbible.life/kjv/John%20%3Cscript%3E%26%22/3/16-17%2C19", rendered)
        self.assertIn("John &lt;script&gt;&amp;&quot;", rendered)
        self.assertIn("Not &lt;b&gt;condemn&lt;/b&gt; &amp; save.", rendered)
        self.assertNotIn("<script>", rendered)

    def test_messages_are_split_below_telegram_limit_without_broken_entities(self) -> None:
        chunks = render_scripture(
            self.result("<&>" * 5000),
            "https://getbible.life",
            chunk_limit=512,
        )
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 512 for chunk in chunks))
        for chunk in chunks:
            self.assertNotRegex(chunk, r"&(?:a|am|amp|l|lt|g|gt|q|quo|quot)?\Z")

    def test_empty_results_fail_closed(self) -> None:
        with self.assertRaises(Exception):
            render_scripture({}, "https://getbible.life")


if __name__ == "__main__":
    unittest.main()
