import io
import json
import math
import unittest
from collections.abc import Mapping
from http.client import IncompleteRead
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from modules.getbible_query import (
    GetBibleQueryClient,
    GetBibleQueryError,
    MissingVerseError,
    QueryHTTPError,
    QueryInputError,
    QueryLimitError,
    QueryResponseError,
    QueryTransportError,
    VerseReference,
)


def chapter_payload(
    book: int,
    book_name: str,
    chapter: int,
    verses: list[tuple[int, str]],
    *,
    abbreviation: str = "kjv",
    translation: str = "King James Version",
) -> dict[str, Any]:
    return {
        "translation": translation,
        "abbreviation": abbreviation,
        "lang": "en",
        "language": "English",
        "direction": "LTR",
        "encoding": "UTF-8",
        "book_nr": book,
        "book_name": book_name,
        "chapter": chapter,
        "name": f"{book_name} {chapter}",
        "ref": [f"{book} {chapter}:{verse}" for verse, _ in verses],
        "verses": [
            {
                "chapter": chapter,
                "verse": verse,
                "name": f"{book_name} {chapter}:{verse}",
                "text": text,
            }
            for verse, text in verses
        ],
    }


def encoded_document(document: object) -> bytes:
    return json.dumps(document).encode("utf-8")


class FakeResponse:
    def __init__(
        self,
        document: object | None = None,
        *,
        body: bytes | object | None = None,
        status: int | None = 200,
        content_type: str | None = "application/json; charset=utf-8",
        include_length: bool = True,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if body is None:
            body = encoded_document(document)
        self.body = body
        self.status = status
        self.headers = dict(headers or {})
        if content_type is not None:
            self.headers["content-type"] = content_type
        if include_length and isinstance(body, bytes):
            self.headers["CONTENT-LENGTH"] = str(len(body))
        self.read_amounts: list[int] = []
        self.closed = False

    def getcode(self) -> int | None:
        return self.status

    def read(self, amount: int = -1) -> bytes | object:
        self.read_amounts.append(amount)
        if isinstance(self.body, bytes) and amount >= 0:
            return self.body[:amount]
        return self.body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.closed = True


class ScriptedOpener:
    def __init__(self, *actions: FakeResponse | BaseException) -> None:
        self.actions = list(actions)
        self.requests: list[Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self.actions:
            raise AssertionError("No scripted HTTP response remains.")
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class GetBibleQueryClientTestCase(unittest.TestCase):
    def test_fetches_numeric_references_and_returns_normalized_records(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(
                {
                    "kjv_43_3": chapter_payload(
                        43,
                        "John",
                        3,
                        [(16, "  For God so loved\n the world.  ")],
                    ),
                    "kjv_62_3": chapter_payload(
                        62,
                        "1 John",
                        3,
                        [(16, "Hereby perceive we the love of God.")],
                    ),
                }
            )
        )
        client = GetBibleQueryClient(opener=opener, timeout_seconds=3.5)

        result = client.fetch_verses([(43, 3, 16), VerseReference(62, 3, 16)])

        john = result[VerseReference(43, 3, 16)]
        self.assertEqual(list(result), [VerseReference(43, 3, 16), VerseReference(62, 3, 16)])
        self.assertEqual(john.reference.canonical, "43 3:16")
        self.assertEqual(john.display_reference, "John 3:16")
        self.assertEqual(john.text, "For God so loved the world.")
        self.assertEqual(john.translation, "kjv")
        self.assertEqual(john.translation_name, "King James Version")
        self.assertEqual(john.book_name, "John")
        self.assertEqual(opener.timeouts, [3.5])
        request = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "https://query.getbible.net/v2/kjv/43%203:16;%2062%203:16",
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("User-agent"), "getbible-robot/2.2")

    def test_uses_configured_translation_without_falling_back(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(
                {
                    "web_1_1": chapter_payload(
                        1,
                        "Genesis",
                        1,
                        [(1, "In the beginning.")],
                        abbreviation="web",
                        translation="World English Bible",
                    )
                }
            )
        )

        verse = GetBibleQueryClient(
            translation=" WEB ",
            opener=opener,
        ).fetch_verses([(1, 1, 1)])[VerseReference(1, 1, 1)]

        self.assertIn("/web/1%201:1", opener.requests[0].full_url)
        self.assertEqual(verse.translation, "web")
        self.assertEqual(verse.translation_name, "World English Bible")

    def test_deduplicates_within_each_fetch_and_does_not_retain_scripture(self) -> None:
        first = FakeResponse(
            {"kjv_1_1": chapter_payload(1, "Genesis", 1, [(1, "Beginning")])}
        )
        second = FakeResponse(
            {"kjv_43_3": chapter_payload(43, "John", 3, [(16, "Loved")])}
        )
        third = FakeResponse(
            {"kjv_1_1": chapter_payload(1, "Genesis", 1, [(1, "Beginning")])}
        )
        opener = ScriptedOpener(first, second, third)
        client = GetBibleQueryClient(
            opener=opener,
            batch_size=1,
        )

        result = client.fetch_verses([(1, 1, 1), (1, 1, 1), (43, 3, 16)])

        self.assertEqual(len(result), 2)
        self.assertEqual(len(opener.requests), 2)
        repeated = client.fetch_verses([(1, 1, 1)])
        self.assertEqual(repeated[VerseReference(1, 1, 1)].text, "Beginning")
        self.assertEqual(len(opener.requests), 3)

    def test_empty_request_does_not_open_a_connection(self) -> None:
        opener = ScriptedOpener()
        self.assertEqual(GetBibleQueryClient(opener=opener).fetch_verses([]), {})
        self.assertEqual(opener.requests, [])

    def test_rejects_invalid_configuration_and_coordinates(self) -> None:
        invalid_clients: list[dict[str, object]] = [
            {"translation": "../kjv"},
            {"translation": ""},
            {"translation": None},
            {"timeout_seconds": 0},
            {"timeout_seconds": math.inf},
            {"max_response_bytes": 0},
            {"batch_size": 0},
            {"max_url_length": 127},
            {"max_references": 0},
        ]
        for settings in invalid_clients:
            with self.subTest(settings=settings), self.assertRaises(QueryInputError):
                GetBibleQueryClient(**settings)  # type: ignore[arg-type]

        dotted = GetBibleQueryClient(translation="custom.v2", opener=ScriptedOpener())
        self.assertEqual(dotted.translation, "custom.v2")

        for coordinates in ((0, 1, 1), (1, 0, 1), (1, 1, 0), (True, 1, 1), (1000, 1, 1)):
            with self.subTest(coordinates=coordinates), self.assertRaises(QueryInputError):
                VerseReference(*coordinates)

        client = GetBibleQueryClient(opener=ScriptedOpener(), max_references=1)
        invalid_inputs: list[object] = [
            "43 3:16",
            [(43, 3)],
            [[43, 3, 16]],
            [(43, "3", 16)],
            [(43, 3, 16), (62, 3, 16)],
        ]
        for references in invalid_inputs:
            with self.subTest(references=references), self.assertRaises(QueryInputError):
                client.fetch_verses(references)  # type: ignore[arg-type]

    def test_splits_batches_before_the_configured_count_limit(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(
                {"kjv_1_1": chapter_payload(1, "Genesis", 1, [(1, "One"), (2, "Two")])}
            ),
            FakeResponse(
                {"kjv_1_1": chapter_payload(1, "Genesis", 1, [(3, "Three")])}
            ),
        )
        client = GetBibleQueryClient(opener=opener, batch_size=2)

        result = client.fetch_verses([(1, 1, 1), (1, 1, 2), (1, 1, 3)])

        self.assertEqual([verse.text for verse in result.values()], ["One", "Two", "Three"])
        self.assertEqual(len(opener.requests), 2)

    def test_splits_batches_before_the_encoded_url_limit(self) -> None:
        first_seven = [(verse, f"Text {verse}") for verse in range(1, 8)]
        opener = ScriptedOpener(
            FakeResponse(
                {"kjv_66_22": chapter_payload(66, "Revelation", 22, first_seven)}
            ),
            FakeResponse(
                {
                    "kjv_66_22": chapter_payload(
                        66,
                        "Revelation",
                        22,
                        [(8, "Text 8")],
                    )
                }
            ),
        )
        client = GetBibleQueryClient(opener=opener, max_url_length=128)

        result = client.fetch_verses([(66, 22, verse) for verse in range(1, 9)])

        self.assertEqual(len(result), 8)
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(all(len(request.full_url) <= 128 for request in opener.requests))

    def test_http_and_transport_failures_are_typed_for_deferral(self) -> None:
        http_error = HTTPError(
            "https://query.getbible.net/",
            503,
            "Service Unavailable",
            {},
            None,
        )
        cases: list[tuple[BaseException | FakeResponse, type[GetBibleQueryError], bool]] = [
            (http_error, QueryHTTPError, True),
            (FakeResponse({}, status=302), QueryHTTPError, False),
            (FakeResponse({}, status=400), QueryHTTPError, False),
            (URLError("offline"), QueryTransportError, True),
            (TimeoutError(), QueryTransportError, True),
            (OSError("network"), QueryTransportError, True),
            (IncompleteRead(b"partial", 100), QueryTransportError, True),
        ]
        for action, error_type, retryable in cases:
            with self.subTest(error_type=error_type, retryable=retryable):
                client = GetBibleQueryClient(opener=ScriptedOpener(action))
                with self.assertRaises(error_type) as raised:
                    client.fetch_verses([(43, 3, 16)])
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertIn("defer", str(raised.exception).lower())

    def test_enforces_content_type_and_size_before_parsing(self) -> None:
        valid = {"kjv_1_1": chapter_payload(1, "Genesis", 1, [(1, "Beginning")])}
        cases = [
            FakeResponse(valid, content_type=None),
            FakeResponse(valid, content_type="text/html"),
            FakeResponse(valid, headers={"Content-Length": "not-a-number"}),
            FakeResponse(valid, headers={"Content-Length": "-1"}),
            FakeResponse(valid, headers={"Content-Length": "500"}),
            FakeResponse(body=b"x" * 65, include_length=False),
            FakeResponse(valid, body={"not": "bytes"}),
        ]
        for index, response in enumerate(cases):
            with self.subTest(index=index):
                maximum = 64 if index >= 4 else 1024 * 1024
                client = GetBibleQueryClient(
                    opener=ScriptedOpener(response),
                    max_response_bytes=maximum,
                )
                with self.assertRaises(QueryResponseError):
                    client.fetch_verses([(1, 1, 1)])

    def test_rejects_malformed_incomplete_and_inconsistent_documents(self) -> None:
        wrong_translation = chapter_payload(
            43,
            "John",
            3,
            [(16, "Loved")],
            abbreviation="web",
        )
        inconsistent_chapter = chapter_payload(43, "John", 3, [(16, "Loved")])
        inconsistent_chapter["verses"][0]["chapter"] = 4
        inconsistent_name = chapter_payload(43, "John", 3, [(16, "Loved")])
        inconsistent_name["verses"][0]["name"] = "Genesis 1:1"
        unexpected_verse = chapter_payload(43, "John", 3, [(17, "Sent")])
        duplicate_verse = chapter_payload(
            43,
            "John",
            3,
            [(16, "Loved"), (16, "Loved again")],
        )
        unsafe_text = chapter_payload(43, "John", 3, [(16, "Loved\x1b[31m")])
        empty_text = chapter_payload(43, "John", 3, [(16, "   ")])
        invalid_cases: list[bytes] = [
            b"not-json",
            b"[NaN]",
            encoded_document([]),
            encoded_document({}),
            encoded_document({"error": "Invalid reference"}),
            encoded_document({"kjv_43_3": []}),
            encoded_document({"kjv_43_3": wrong_translation}),
            encoded_document({"kjv_43_3": inconsistent_chapter}),
            encoded_document({"kjv_43_3": inconsistent_name}),
            encoded_document({"kjv_43_3": unexpected_verse}),
            encoded_document({"kjv_43_3": duplicate_verse}),
            encoded_document({"kjv_43_3": unsafe_text}),
            encoded_document({"kjv_43_3": empty_text}),
        ]
        for index, body in enumerate(invalid_cases):
            with self.subTest(index=index):
                client = GetBibleQueryClient(opener=ScriptedOpener(FakeResponse(body=body)))
                with self.assertRaises(QueryResponseError):
                    client.fetch_verses([(43, 3, 16)])

        client = GetBibleQueryClient(
            opener=ScriptedOpener(
                FakeResponse(
                    {
                        "kjv_43_3": chapter_payload(
                            43,
                            "John",
                            3,
                            [(16, "Loved")],
                        )
                    }
                )
            )
        )
        with self.assertRaises(MissingVerseError) as raised:
            client.fetch_verses([(43, 3, 16), (62, 3, 16)])
        self.assertEqual(raised.exception.missing, (VerseReference(62, 3, 16),))
        self.assertFalse(raised.exception.retryable)

    def test_rejects_invalid_response_status_metadata(self) -> None:
        response = FakeResponse({}, status=None)
        with self.assertRaises(QueryResponseError):
            GetBibleQueryClient(opener=ScriptedOpener(response)).fetch_verses(
                [(1, 1, 1)]
            )

    def test_accepts_structured_json_media_type_without_content_length(self) -> None:
        response = FakeResponse(
            {"kjv_1_1": chapter_payload(1, "Genesis", 1, [(1, "Beginning")])},
            content_type="application/problem+json",
            include_length=False,
        )
        result = GetBibleQueryClient(
            opener=ScriptedOpener(response)
        ).fetch_verses([(1, 1, 1)])
        self.assertEqual(result[VerseReference(1, 1, 1)].text, "Beginning")


class _FailingBody:
    """A problem body whose read fails after the headers arrived."""

    def read(self, amount: int = -1) -> bytes:
        raise OSError("connection reset")

    def close(self) -> None:
        return None


def problem(
    status: int,
    document: object,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    fp: object | None = None,
) -> HTTPError:
    if fp is None:
        fp = io.BytesIO(encoded_document(document) if body is None else body)
    return HTTPError(
        "https://query.getbible.net/v2/kjv/John%203:16",
        status,
        "Problem",
        dict(headers or {"Content-Type": "application/problem+json"}),
        fp,  # type: ignore[arg-type]
    )


class FetchScriptureTestCase(unittest.TestCase):
    """The free-form reference route the robot's ``/bible`` command relies on."""

    def test_returns_the_chapter_grouped_document_with_normalised_names(self) -> None:
        john = chapter_payload(43, "John", 3, [(16, "  For God so loved\n the world.  ")])
        del john["name"]
        del john["verses"][0]["name"]
        first_john = chapter_payload(62, "1 John", 3, [(16, "Hereby perceive we the love.")])
        first_john["verses"][0]["chapter"] = 3
        opener = ScriptedOpener(FakeResponse({"kjv_43_3": john, "kjv_62_3": first_john}))
        client = GetBibleQueryClient(opener=opener, timeout_seconds=2.5)

        document = client.fetch_scripture("John 3:16;1 John 3:16", max_verses=100)

        self.assertEqual(list(document), ["kjv_43_3", "kjv_62_3"])
        self.assertEqual(
            document["kjv_43_3"],
            {
                "translation": "King James Version",
                "abbreviation": "kjv",
                "book_nr": 43,
                "book_name": "John",
                "chapter": 3,
                "name": "John 3",
                "verses": [
                    {
                        "chapter": 3,
                        "verse": 16,
                        "name": "John 3:16",
                        "text": "For God so loved the world.",
                    }
                ],
            },
        )
        self.assertEqual(document["kjv_62_3"]["name"], "1 John 3")
        self.assertEqual(document["kjv_62_3"]["verses"][0]["name"], "1 John 3:16")
        # Fields the renderer never reads are dropped rather than trusted.
        self.assertNotIn("ref", document["kjv_43_3"])
        self.assertNotIn("lang", document["kjv_62_3"])
        self.assertEqual(opener.timeouts, [2.5])
        request = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "https://query.getbible.net/v2/kjv/John%203:16;1%20John%203:16",
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("User-agent"), "getbible-robot/2.2")

    def test_reference_url_keeps_query_punctuation_and_encodes_the_rest(self) -> None:
        opener = ScriptedOpener(
            FakeResponse({"kjv_1_1": chapter_payload(1, "Genesis", 1, [(1, "Beginning")])}),
        )
        client = GetBibleQueryClient(opener=opener)

        client.fetch_scripture("  Genesis   1:1-3,5;  Génesis 1:1–2  ")

        self.assertEqual(
            opener.requests[0].full_url,
            "https://query.getbible.net/v2/kjv/Genesis%201:1-3,5;%20G%C3%A9nesis%201:1%E2%80%932",
        )
        # A slash is not reference syntax: it is refused before it could ever
        # be encoded into, or escape from, the reference path segment.
        with self.assertRaises(QueryInputError):
            client.fetch_scripture("Gen 1:1 / 2")
        with self.assertRaises(QueryInputError):
            client.fetch_scripture("Gen 1:1/../../aov/Gen 1:1")
        self.assertEqual(len(opener.requests), 1)

    def test_translation_override_and_base_url_are_honoured(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(
                {
                    "aov_43_3": chapter_payload(
                        43,
                        "Johannes",
                        3,
                        [(16, "Want so lief het God die wêreld gehad.")],
                        abbreviation="AOV",
                        translation="Afrikaanse Ou Vertaling",
                    )
                }
            )
        )
        client = GetBibleQueryClient(
            base_url="https://query.example.test/custom/",
            opener=opener,
        )

        document = client.fetch_scripture("Johannes 3:16", translation=" AOV ")

        self.assertEqual(
            opener.requests[0].full_url,
            "https://query.example.test/custom/aov/Johannes%203:16",
        )
        self.assertEqual(document["aov_43_3"]["abbreviation"], "aov")
        self.assertEqual(document["aov_43_3"]["translation"], "Afrikaanse Ou Vertaling")

        for translation in ("../kjv", "", "k" * 65, "kjv/x", 5):
            with self.subTest(translation=translation), self.assertRaises(QueryInputError):
                client.fetch_scripture("John 3:16", translation=translation)  # type: ignore[arg-type]
        self.assertEqual(len(opener.requests), 1)

    def test_reference_syntax_is_refused_before_any_request(self) -> None:
        opener = ScriptedOpener()
        client = GetBibleQueryClient(opener=opener, max_url_length=128)
        invalid: list[object] = [
            "",
            "   ",
            "John <3",
            "John 3:16!",
            "!!!",
            "---",
            "\x00John 3:16",
            "John\x1b[31m 3:16",
            "x" * 513,
            "Genesis 1:1-3;" * 10,
            5,
            None,
        ]
        for reference in invalid:
            with self.subTest(reference=reference), self.assertRaises(QueryInputError) as raised:
                client.fetch_scripture(reference)  # type: ignore[arg-type]
            self.assertFalse(raised.exception.retryable)
        for max_verses in (0, -1, True, "5", 2.0):
            with self.subTest(max_verses=max_verses), self.assertRaises(QueryInputError):
                client.fetch_scripture("John 3:16", max_verses=max_verses)  # type: ignore[arg-type]
        self.assertEqual(opener.requests, [])

    def test_max_verses_bounds_the_whole_selection(self) -> None:
        def document() -> dict[str, Any]:
            return {
                "kjv_43_3": chapter_payload(43, "John", 3, [(16, "Loved"), (17, "Sent")]),
                "kjv_62_3": chapter_payload(62, "1 John", 3, [(16, "Perceive")]),
            }

        client = GetBibleQueryClient(opener=ScriptedOpener(FakeResponse(document())))
        with self.assertRaises(QueryLimitError) as raised:
            client.fetch_scripture("John 3:16-17;1 John 3:16", max_verses=2)
        self.assertIsInstance(raised.exception, QueryInputError)
        self.assertFalse(raised.exception.retryable)
        self.assertIn("2-verse limit", str(raised.exception))

        exact = GetBibleQueryClient(opener=ScriptedOpener(FakeResponse(document())))
        self.assertEqual(
            len(exact.fetch_scripture("John 3:16-17;1 John 3:16", max_verses=3)),
            2,
        )
        unbounded = GetBibleQueryClient(opener=ScriptedOpener(FakeResponse(document())))
        self.assertEqual(len(unbounded.fetch_scripture("John 3:16-17;1 John 3:16")), 2)

    def test_rejects_documents_that_do_not_describe_the_request(self) -> None:
        wrong_translation = chapter_payload(43, "John", 3, [(16, "Loved")], abbreviation="web")
        duplicate = chapter_payload(43, "John", 3, [(16, "Loved"), (16, "Loved again")])
        inconsistent_chapter = chapter_payload(43, "John", 3, [(16, "Loved")])
        inconsistent_chapter["verses"][0]["chapter"] = 4
        zero_verse = chapter_payload(43, "John", 3, [(0, "Nothing")])
        huge_verse = chapter_payload(43, "John", 3, [(1000, "Nothing")])
        empty_verses = chapter_payload(43, "John", 3, [])
        verse_not_object = chapter_payload(43, "John", 3, [(16, "Loved")])
        verse_not_object["verses"] = ["16"]
        unsafe_text = chapter_payload(43, "John", 3, [(16, "Loved\x1b[31m")])
        blank_text = chapter_payload(43, "John", 3, [(16, "   ")])
        missing_text = chapter_payload(43, "John", 3, [(16, "Loved")])
        del missing_text["verses"][0]["text"]
        bad_book_number = chapter_payload(43, "John", 3, [(16, "Loved")])
        bad_book_number["book_nr"] = "43"
        bad_book_name = chapter_payload(43, "John", 3, [(16, "Loved")])
        bad_book_name["book_name"] = ""
        bad_verse_name = chapter_payload(43, "John", 3, [(16, "Loved")])
        bad_verse_name["verses"][0]["name"] = "x" * 257
        too_many_chapters = {
            f"kjv_19_{chapter}": chapter_payload(19, "Psalms", chapter, [(1, "Praise")])
            for chapter in range(1, 66)
        }
        invalid_cases: list[bytes] = [
            b"not-json",
            b"[NaN]",
            encoded_document([]),
            encoded_document({}),
            encoded_document({"error": "Invalid reference"}),
            encoded_document({"kjv_43_3": []}),
            encoded_document({"kjv_43_3": wrong_translation}),
            encoded_document({"kjv_43_3": duplicate}),
            encoded_document({"kjv_43_3": inconsistent_chapter}),
            encoded_document({"kjv_43_3": zero_verse}),
            encoded_document({"kjv_43_3": huge_verse}),
            encoded_document({"kjv_43_3": empty_verses}),
            encoded_document({"kjv_43_3": verse_not_object}),
            encoded_document({"kjv_43_3": unsafe_text}),
            encoded_document({"kjv_43_3": blank_text}),
            encoded_document({"kjv_43_3": missing_text}),
            encoded_document({"kjv_43_3": bad_book_number}),
            encoded_document({"kjv_43_3": bad_book_name}),
            encoded_document({"kjv_43_3": bad_verse_name}),
            encoded_document(too_many_chapters),
        ]
        for index, body in enumerate(invalid_cases):
            with self.subTest(index=index):
                client = GetBibleQueryClient(opener=ScriptedOpener(FakeResponse(body=body)))
                with self.assertRaises(QueryResponseError) as raised:
                    client.fetch_scripture("John 3:16")
                self.assertFalse(raised.exception.retryable)

        # A verse without its own chapter field is still the chapter's verse.
        no_verse_chapter = chapter_payload(43, "John", 3, [(16, "Loved")])
        del no_verse_chapter["verses"][0]["chapter"]
        no_verse_chapter["name"] = 3
        no_verse_chapter["verses"][0]["name"] = None
        client = GetBibleQueryClient(
            opener=ScriptedOpener(FakeResponse({"kjv_43_3": no_verse_chapter}))
        )
        document = client.fetch_scripture("John 3:16")
        self.assertEqual(document["kjv_43_3"]["name"], "John 3")
        self.assertEqual(document["kjv_43_3"]["verses"][0]["name"], "John 3:16")
        self.assertEqual(document["kjv_43_3"]["verses"][0]["chapter"], 3)

    def test_problem_documents_populate_the_http_error(self) -> None:
        cases: list[tuple[int, dict[str, object], dict[str, object]]] = [
            (
                404,
                {"code": "invalid_reference", "detail": "  Unknown book\n name. "},
                {
                    "code": "invalid_reference",
                    "detail": "Unknown book name.",
                    "invalid_reference": True,
                    "translation_not_found": False,
                    "request_limit": False,
                    "retryable": False,
                },
            ),
            (
                404,
                {"code": "translation_not_found"},
                {
                    "code": "translation_not_found",
                    "invalid_reference": False,
                    "translation_not_found": True,
                    "request_limit": False,
                    "retryable": False,
                },
            ),
            (
                404,
                {"code": "unknown_version"},
                {
                    "invalid_reference": False,
                    "translation_not_found": False,
                    "request_limit": False,
                    "retryable": False,
                },
            ),
            (
                404,
                {"code": "not_found"},
                {"invalid_reference": True, "request_limit": False},
            ),
            (
                400,
                {"code": "request_limit", "detail": "Too many verses."},
                {
                    "code": "request_limit",
                    "detail": "Too many verses.",
                    "invalid_reference": False,
                    "request_limit": True,
                    "retryable": False,
                },
            ),
            (
                400,
                {"code": "parameters_not_accepted"},
                {"invalid_reference": True, "request_limit": False},
            ),
            (
                429,
                {"code": "rate_limited", "retry_after": 9},
                {"retry_after": 9, "retryable": True, "invalid_reference": False},
            ),
            (
                503,
                {"code": "repository_unavailable"},
                {"retry_after": None, "retryable": True, "invalid_reference": False},
            ),
        ]
        for status, document, expected in cases:
            with self.subTest(status=status, code=document.get("code")):
                client = GetBibleQueryClient(opener=ScriptedOpener(problem(status, document)))
                with self.assertRaises(QueryHTTPError) as raised:
                    client.fetch_scripture("John 3:16")
                error = raised.exception
                self.assertEqual(error.status_code, status)
                for attribute, value in expected.items():
                    self.assertEqual(getattr(error, attribute), value, attribute)
                self.assertIn(f"HTTP {status} ({document['code']})", str(error))
                self.assertIn("defer", str(error))

    def test_retry_after_header_takes_precedence_and_is_capped(self) -> None:
        cases: list[tuple[Mapping[str, str] | None, object, int | None]] = [
            ({"retry-after": "7"}, 30, 7),
            ({"Retry-After": "99999"}, None, 3600),
            ({"Retry-After": "later"}, 12, 12),
            ({}, 99999, 3600),
            ({}, -1, None),
            ({}, True, None),
            (None, 4, 4),
        ]
        for headers, body_retry, expected in cases:
            with self.subTest(headers=headers, body_retry=body_retry):
                document: dict[str, object] = {"code": "rate_limited"}
                if body_retry is not None:
                    document["retry_after"] = body_retry
                error = problem(429, document, headers=headers)
                if headers is None:
                    error.hdrs = None  # type: ignore[assignment]
                client = GetBibleQueryClient(opener=ScriptedOpener(error))
                with self.assertRaises(QueryHTTPError) as raised:
                    client.fetch_scripture("John 3:16")
                self.assertEqual(raised.exception.retry_after, expected)

    def test_problem_document_faults_are_ignored(self) -> None:
        cases: list[HTTPError] = [
            problem(503, ["not", "an", "object"]),
            problem(503, None, body=b"{"),
            problem(503, None, body=b"\xff\xfe"),
            problem(503, None, body=b"{" + b" " * (16 * 1024) + b"}"),
            problem(503, None, fp=_FailingBody()),
            problem(503, {"code": "busy\x1b[31m", "detail": "x\x00y"}),
            problem(503, {"code": "", "detail": "d" * 513}),
            problem(503, {"code": 503, "detail": ["busy"]}),
        ]
        for index, error in enumerate(cases):
            with self.subTest(index=index):
                client = GetBibleQueryClient(opener=ScriptedOpener(error))
                with self.assertRaises(QueryHTTPError) as raised:
                    client.fetch_scripture("John 3:16")
                self.assertEqual(raised.exception.status_code, 503)
                self.assertEqual(raised.exception.code, "")
                self.assertEqual(raised.exception.detail, "")
                self.assertIsNone(raised.exception.retry_after)
                self.assertTrue(raised.exception.retryable)
                self.assertEqual(str(raised.exception), (
                    "The GetBible Query API returned HTTP 503; defer this review."
                ))

    def test_http_error_properties_do_not_depend_on_a_problem_body(self) -> None:
        self.assertTrue(QueryHTTPError(400).invalid_reference)
        self.assertFalse(QueryHTTPError(400).request_limit)
        self.assertTrue(QueryHTTPError(404).invalid_reference)
        self.assertFalse(QueryHTTPError(404, code="translation_not_found").invalid_reference)
        self.assertFalse(QueryHTTPError(404, code="unknown_version").invalid_reference)
        self.assertFalse(QueryHTTPError(405).invalid_reference)
        self.assertFalse(QueryHTTPError(429).invalid_reference)
        self.assertFalse(QueryHTTPError(503).translation_not_found)
        self.assertIsNone(QueryHTTPError(503).retry_after)
        self.assertEqual(QueryHTTPError(503).detail, "")
        for status in (408, 425, 429, 500, 599):
            self.assertTrue(QueryHTTPError(status).retryable, status)
        for status in (301, 400, 401, 404, 405, 415):
            self.assertFalse(QueryHTTPError(status).retryable, status)


if __name__ == "__main__":
    unittest.main()
