"""Local input hardening for Scripture references.

The robot keeps no reference parser. The Query API decides what a reference
means — whether a book exists, whether a verse number is real — so absurd but
well-formed coordinates are its business and travel to it unchanged. What
stays local is the bound a request must satisfy before a round trip is spent
on it: control characters, markup, oversized input, and empty or excessive
reference groups are refused here and never reach the network.
"""

import os
import random
import unittest
from unittest.mock import patch

from config import Settings
from modules.catalog import TranslationOption
from modules.errors import (
    ReferenceValidationError,
    RequestLimitError,
    RobotInputError,
)
from modules.getbible_query import QueryHTTPError
from modules.service import ScriptureQuery, ScriptureService


class _NetworkForbidden:
    """A Query API client whose every call fails the test."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch_scripture(
        self,
        reference: str,
        *,
        translation: str,
        max_verses: int | None,
    ) -> dict:
        self.calls += 1
        raise AssertionError("Malformed input must never reach the Query API.")


class _SearchForbidden:
    """A Search API client that reference validation must never touch."""

    def __init__(self) -> None:
        self.calls = 0

    def search(self, query: str, translation: str, criteria: object) -> dict:
        self.calls += 1
        raise AssertionError("Reference validation must never reach the Search API.")


class _Catalog:
    def __init__(self) -> None:
        self.calls = 0

    def translations(self) -> tuple[TranslationOption, ...]:
        self.calls += 1
        return (TranslationOption("kjv", "King James Version", "English", "en"),)


class _RefusingQueryClient:
    """The Query API answering that a well-formed reference does not exist."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_scripture(
        self,
        reference: str,
        *,
        translation: str,
        max_verses: int | None,
    ) -> dict:
        self.calls.append(reference)
        raise QueryHTTPError(
            404,
            code="invalid_reference",
            detail="The verse does not exist.",
        )


def _settings(**environment: str) -> Settings:
    values = {
        "TELEGRAM_API_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        "HEALTH_PORT": "0",
        "MAX_REFERENCES": "16",
    }
    values.update(environment)
    with patch.dict(os.environ, values, clear=True):
        return Settings.from_env(load_environment_file=False)


class ReferenceInputHardeningTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.query = _NetworkForbidden()
        self.search = _SearchForbidden()
        self.catalog = _Catalog()
        self.service = ScriptureService(
            _settings(),
            query_client=self.query,
            search_client=self.search,
            catalog=self.catalog,
        )
        self.addAsyncCleanup(self.service.close)

    async def asyncTearDown(self) -> None:
        self.assertEqual(self.query.calls, 0)
        self.assertEqual(self.search.calls, 0)

    async def test_control_characters_are_refused_locally(self) -> None:
        for reference in (
            "John 3:16\x00",
            "John\x01 3:16",
            "\x07John 3:16",
            "John 3:1\x086",
            "\x1b[31mJohn 3:16",
            "John 3:16\x7f",
            "John 3:16\ud800",
        ):
            with self.subTest(reference=repr(reference)):
                with self.assertRaises(ReferenceValidationError):
                    await self.service.resolve_query([reference])
                with self.assertRaises(ReferenceValidationError):
                    self.service._validate_reference_set(reference)

    async def test_markup_and_shell_punctuation_are_refused_locally(self) -> None:
        for reference in (
            "<b>John</b> 3:16",
            "John 3:16<script>alert(1)</script>",
            "John 3:16 & Gen 1:1",
            "John 3:16 | cat /etc/passwd",
            "John 3:16 $(id)",
            "John 3:16 `id`",
            "John 3:16?ref=1",
            "John 3:16#fragment",
            "John 3:16/../../kjv",
            "John 3:16%20",
            "John 3:16\\n",
        ):
            with self.subTest(reference=reference), self.assertRaises(ReferenceValidationError):
                await self.service.resolve_query([reference])

    async def test_oversized_input_is_refused_before_any_group_is_examined(self) -> None:
        with self.assertRaises(RequestLimitError):
            await self.service.resolve_query(["J" * 1000])
        with self.assertRaises(RequestLimitError):
            await self.service.resolve_query(["John 3:16"] * 40)
        # A single group is capped at a hundred characters even when the whole
        # request would fit the input bound.
        with self.assertRaises(ReferenceValidationError):
            await self.service.resolve_query(["J" * 101])
        self.assertEqual(self.catalog.calls, 0)

    async def test_too_many_reference_groups_are_refused(self) -> None:
        sixteen = ";".join(["John 3:16"] * 16)
        seventeen = ";".join(["John 3:16"] * 17)

        query = await self.service.resolve_query([sixteen])
        self.assertEqual(query, ScriptureQuery(sixteen, "kjv"))
        with self.assertRaises(RequestLimitError):
            await self.service.resolve_query([seventeen])
        with self.assertRaises(RequestLimitError):
            self.service._validate_reference_set(seventeen)

    async def test_empty_groups_never_become_a_default_reference(self) -> None:
        for reference in (
            "John 3:16;;John 3:17",
            ";John 3:16",
            "John 3:16;",
            ";",
            ";;;",
            "John 3:16; ;John 3:17",
        ):
            with self.subTest(reference=reference), self.assertRaises(ReferenceValidationError):
                await self.service.resolve_query([reference])
        for arguments in ([], [""], ["  "], ["", ""]):
            with self.subTest(arguments=arguments), self.assertRaises(RobotInputError):
                await self.service.resolve_query(arguments)

    async def test_deterministic_symbol_fuzz_fails_closed(self) -> None:
        generator = random.Random(20260719)
        symbols = "!@#$%^&*()[]{}<>?/\\|`~=+\x00\x01\x07\x08\x1b\x7f"
        for _ in range(1000):
            value = "John 1:16" + "".join(generator.choice(symbols) for _ in range(3))
            with self.subTest(value=repr(value)), self.assertRaises(ReferenceValidationError):
                self.service._validate_reference_set(value)

    async def test_well_formed_references_pass_the_local_bound_untouched(self) -> None:
        for arguments, expected in (
            (["John", "3:16"], "John 3:16"),
            (["1", "John", "3:16-18,20"], "1 John 3:16-18,20"),
            (["Gen", "1:1-3;", "John", "3:16"], "Gen 1:1-3; John 3:16"),
            (["Génesis", "1:1–3"], "Génesis 1:1–3"),
            (["Song", "of", "Solomon", "1"], "Song of Solomon 1"),
            (["John", "1:1-999999999"], "John 1:1-999999999"),
            (["John", "1:0"], "John 1:0"),
            (["John", "1:10-1"], "John 1:10-1"),
            (["Nowhere", "9:9"], "Nowhere 9:9"),
        ):
            with self.subTest(arguments=arguments):
                query = await self.service.resolve_query(arguments)
                self.assertEqual(query, ScriptureQuery(expected, "kjv"))


class AbsurdCoordinatesBelongToTheQueryApiTestCase(unittest.IsolatedAsyncioTestCase):
    """Verse arithmetic moved upstream: the robot forwards and classifies."""

    async def test_query_api_refusal_of_a_well_formed_reference_is_an_input_error(
        self,
    ) -> None:
        query = _RefusingQueryClient()
        service = ScriptureService(
            _settings(),
            query_client=query,
            search_client=_SearchForbidden(),
            catalog=_Catalog(),
        )
        self.addAsyncCleanup(service.close)

        for reference in ("John 1:1-999999999", "John 1:999999999", "John 1:0", "John 1:10-1"):
            with self.subTest(reference=reference):
                resolved = await service.resolve_query([reference])
                with self.assertRaises(ReferenceValidationError):
                    await service.select(resolved)

        self.assertEqual(
            query.calls,
            ["John 1:1-999999999", "John 1:999999999", "John 1:0", "John 1:10-1"],
        )
        snapshot = await service.snapshot()
        self.assertEqual(snapshot["circuit"]["failures"], 0)
        self.assertEqual(snapshot["circuit"]["state"], "closed")
        self.assertNotIn("repository_failures", snapshot["metrics"])


if __name__ == "__main__":
    unittest.main()
