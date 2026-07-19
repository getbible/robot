import random
import unittest

from getbible import GetBibleReference, ReferenceValidationError, RequestLimitError


class ParserSecurityRegressionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = GetBibleReference(max_verses=100, max_verse_number=1000)

    def test_large_range_and_verse_number_fail_before_materialization(self) -> None:
        for reference in ("John 1:1-999999999", "John 1:999999999"):
            with self.subTest(reference=reference), self.assertRaises(RequestLimitError):
                self.parser.ref(reference, "kjv")

    def test_reversed_or_malformed_references_never_become_verse_one(self) -> None:
        for reference in ("John 1:16!", "John 1:10-1", "John 1:1--2", "John 1:0"):
            with self.subTest(reference=reference), self.assertRaises(ReferenceValidationError):
                self.parser.ref(reference, "kjv")

    def test_deterministic_symbol_fuzz_fails_closed(self) -> None:
        generator = random.Random(20260719)
        symbols = "!@#$%^&*()[]{}<>?/\\|`~=+\x00\x01\x1f"
        for _ in range(1000):
            value = "John 1:16" + "".join(generator.choice(symbols) for _ in range(3))
            with self.subTest(value=repr(value)):
                self.assertFalse(self.parser.valid(value, "kjv"))


if __name__ == "__main__":
    unittest.main()
