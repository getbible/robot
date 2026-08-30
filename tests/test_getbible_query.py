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
        self.assertIn("Contribution-Review", request.get_header("User-agent"))

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


if __name__ == "__main__":
    unittest.main()
