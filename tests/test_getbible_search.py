"""GetBibleSearchClient: one bounded request to the public Search API.

The client never trusts what comes back. These tests pin the request it is
allowed to send — the documented parameters in their documented order and
bounds — and every way an answer can be refused before the service layer sees
it: the wrong status, a redirect, a body that is too large or not JSON, and an
envelope that does not carry the published shape.
"""

import json
import math
import unittest
from collections.abc import Mapping
from http.client import IncompleteRead
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from modules.getbible_search import (
    MAX_BOOKS,
    MAX_EXCLUSIONS,
    MAX_LIMIT,
    MAX_OFFSET,
    MAX_QUERY_LENGTH,
    GetBibleSearchClient,
    GetBibleSearchError,
    SearchCriteria,
    SearchHTTPError,
    SearchInputError,
    SearchResponseError,
    SearchTransportError,
    _RejectRedirectHandler,
)

SEARCH_URL = "https://search.getbible.net/v2/kjv?q=loved"


def search_envelope(
    *,
    kind: str = "search",
    total: int = 1,
    returned: int = 1,
) -> dict[str, Any]:
    return {
        "query": {
            "text": "loved",
            "kind": kind,
            "translation": {
                "translation": "King James Version",
                "abbreviation": "kjv",
                "lang": "en",
                "language": "English",
                "direction": "LTR",
                "encoding": "UTF-8",
            },
            "engine_version": 4,
            "total": total,
            "returned": returned,
        },
        "results": {
            "kjv_43_3": {
                "translation": "King James Version",
                "abbreviation": "kjv",
                "book_nr": 43,
                "book_name": "John",
                "chapter": 3,
                "name": "John 3",
                "ref": ["43 3:16"],
                "verses": [
                    {
                        "chapter": 3,
                        "verse": 16,
                        "name": "John 3:16",
                        "text": "For God so loved the world.",
                    }
                ],
            }
        },
        "matches": [
            {"reference": "John 3:16", "book_nr": 43, "chapter": 3, "verse": 16, "terms": ["loved"]}
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
        import io

        fp = io.BytesIO(encoded_document(document) if body is None else body)
    return HTTPError(
        SEARCH_URL,
        status,
        "Problem",
        dict(headers or {"Content-Type": "application/problem+json"}),
        fp,  # type: ignore[arg-type]
    )


class SearchCriteriaTestCase(unittest.TestCase):
    def test_defaults_match_the_published_contract(self) -> None:
        criteria = SearchCriteria()

        self.assertEqual(criteria.words, "all")
        self.assertEqual(criteria.match, "whole_word")
        self.assertFalse(criteria.case_sensitive)
        self.assertEqual(criteria.scope, "bible")
        self.assertEqual(criteria.books, ())
        self.assertEqual(criteria.diacritics, "fold")
        self.assertEqual(criteria.exclude, ())
        self.assertIsNone(criteria.proximity)
        self.assertEqual(criteria.sort, "canonical")
        self.assertEqual(criteria.limit, MAX_LIMIT)
        self.assertEqual(criteria.offset, 0)

    def test_parameters_follow_the_documented_order(self) -> None:
        self.assertEqual(
            SearchCriteria().parameters("For God"),
            [
                ("q", "For God"),
                ("words", "all"),
                ("match", "whole_word"),
                ("case_sensitive", "false"),
                ("scope", "bible"),
                ("diacritics", "fold"),
                ("sort", "canonical"),
                ("limit", "100"),
                ("offset", "0"),
            ],
        )
        detailed = SearchCriteria(
            words="all",
            match="substring",
            case_sensitive=True,
            scope="new_testament",
            books=(43, 62),
            diacritics="exact",
            exclude=("hate", "world"),
            proximity=5,
            sort="relevance",
            limit=25,
            offset=50,
        )
        self.assertEqual(
            detailed.parameters("loved"),
            [
                ("q", "loved"),
                ("words", "all"),
                ("match", "substring"),
                ("case_sensitive", "true"),
                ("scope", "new_testament"),
                ("diacritics", "exact"),
                ("sort", "relevance"),
                ("limit", "25"),
                ("offset", "50"),
                ("book", "43"),
                ("book", "62"),
                ("exclude", "hate"),
                ("exclude", "world"),
                ("proximity", "5"),
            ],
        )

    def test_proximity_requires_the_all_words_mode(self) -> None:
        self.assertEqual(SearchCriteria(words="all", proximity=0).proximity, 0)
        self.assertEqual(SearchCriteria(words="all", proximity=100).proximity, 100)
        self.assertIsNone(SearchCriteria(words="any").proximity)
        self.assertIsNone(SearchCriteria(words="phrase").proximity)

        for words in ("any", "phrase"):
            with self.subTest(words=words), self.assertRaises(SearchInputError):
                SearchCriteria(words=words, proximity=3)
        for proximity in (-1, 101, True, "3", 2.5):
            with self.subTest(proximity=proximity), self.assertRaises(SearchInputError):
                SearchCriteria(proximity=proximity)  # type: ignore[arg-type]

    def test_enumerations_are_closed(self) -> None:
        invalid: list[dict[str, object]] = [
            {"words": "some"},
            {"words": "ALL"},
            {"match": "regex"},
            {"scope": "apocrypha"},
            {"diacritics": "insensitive"},
            {"sort": "newest"},
            {"case_sensitive": "yes"},
            {"case_sensitive": 1},
        ]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(SearchInputError):
                SearchCriteria(**options)  # type: ignore[arg-type]

    def test_limit_and_offset_bounds(self) -> None:
        self.assertEqual(SearchCriteria(limit=1).limit, 1)
        self.assertEqual(SearchCriteria(limit=MAX_LIMIT).limit, 100)
        self.assertEqual(SearchCriteria(offset=MAX_OFFSET).offset, 10_000)

        for limit in (0, 101, True, "5", 1.0):
            with self.subTest(limit=limit), self.assertRaises(SearchInputError):
                SearchCriteria(limit=limit)  # type: ignore[arg-type]
        for offset in (-1, 10_001, True, "0"):
            with self.subTest(offset=offset), self.assertRaises(SearchInputError):
                SearchCriteria(offset=offset)  # type: ignore[arg-type]

    def test_book_selection_bounds(self) -> None:
        every_book = tuple(range(1, MAX_BOOKS + 1))
        self.assertEqual(len(SearchCriteria(books=every_book).books), 83)
        self.assertEqual(SearchCriteria(books=(1000,)).books, (1000,))

        invalid: list[object] = [
            tuple(range(1, MAX_BOOKS + 2)),
            (43, 43),
            (0,),
            (1001,),
            (True,),
            ("43",),
            [43],
        ]
        for books in invalid:
            with self.subTest(books=books), self.assertRaises(SearchInputError):
                SearchCriteria(books=books)  # type: ignore[arg-type]

    def test_exclusion_bounds(self) -> None:
        many = tuple(f"term{index}" for index in range(MAX_EXCLUSIONS))
        self.assertEqual(len(SearchCriteria(exclude=many).exclude), 32)
        self.assertEqual(SearchCriteria(exclude=("x" * 100,)).exclude, ("x" * 100,))

        invalid: list[object] = [
            tuple(f"term{index}" for index in range(MAX_EXCLUSIONS + 1)),
            ("",),
            ("x" * 101,),
            (" padded",),
            ("a\x00b",),
            (5,),
            ["hate"],
        ]
        for exclude in invalid:
            with self.subTest(exclude=exclude), self.assertRaises(SearchInputError):
                SearchCriteria(exclude=exclude)  # type: ignore[arg-type]


class GetBibleSearchClientTestCase(unittest.TestCase):
    def test_rejects_invalid_configuration(self) -> None:
        invalid: list[dict[str, object]] = [
            {"base_url": "http://search.getbible.net/v2"},
            {"base_url": "https://search.getbible.net/v2?x=1"},
            {"base_url": "https://search.getbible.net/v2#frag"},
            {"base_url": None},
            {"timeout_seconds": 0},
            {"timeout_seconds": -1.0},
            {"timeout_seconds": math.inf},
            {"timeout_seconds": math.nan},
            {"timeout_seconds": True},
            {"timeout_seconds": "3"},
            {"max_response_bytes": 1023},
            {"max_response_bytes": True},
            {"max_response_bytes": 4096.0},
            {"max_query_length": 0},
            {"max_query_length": MAX_QUERY_LENGTH + 1},
            {"max_query_length": True},
        ]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(SearchInputError):
                GetBibleSearchClient(**options)  # type: ignore[arg-type]

        client = GetBibleSearchClient(
            base_url="https://search.example.test/v2/",
            timeout_seconds=4,
            max_response_bytes=1024,
            max_query_length=1,
        )
        self.assertEqual(client.base_url, "https://search.example.test/v2")
        self.assertEqual(client.timeout_seconds, 4.0)
        self.assertEqual(client.max_response_bytes, 1024)
        self.assertEqual(client.max_query_length, 1)

    def test_default_opener_refuses_to_follow_redirects(self) -> None:
        handler = _RejectRedirectHandler()
        request = Request(SEARCH_URL)

        self.assertIsNone(
            handler.redirect_request(
                request,
                None,
                301,
                "Moved Permanently",
                {},
                "https://search.getbible.net/v2/kjv?q=loved",
            )
        )
        client = GetBibleSearchClient()
        self.assertEqual(client.base_url, "https://search.getbible.net/v2")
        self.assertTrue(callable(client._opener))

    def test_builds_the_documented_get_url(self) -> None:
        client = GetBibleSearchClient(opener=ScriptedOpener())
        criteria = SearchCriteria(
            books=(43, 62),
            exclude=("hate", "the world"),
            proximity=5,
            limit=25,
            offset=50,
        )

        url = client.build_url("For  God\n so", " KJV ", criteria)

        self.assertEqual(
            url,
            "https://search.getbible.net/v2/kjv"
            "?q=For+God+so&words=all&match=whole_word&case_sensitive=false"
            "&scope=bible&diacritics=fold&sort=canonical&limit=25&offset=50"
            "&book=43&book=62&exclude=hate&exclude=the+world&proximity=5",
        )
        self.assertEqual(
            client.build_url("θεός", "custom.v2", SearchCriteria()),
            "https://search.getbible.net/v2/custom.v2"
            "?q=%CE%B8%CE%B5%CF%8C%CF%82&words=all&match=whole_word"
            "&case_sensitive=false&scope=bible&diacritics=fold&sort=canonical"
            "&limit=100&offset=0",
        )
        custom = GetBibleSearchClient(
            base_url="https://search.example.test/custom/",
            opener=ScriptedOpener(),
        )
        self.assertTrue(
            custom.build_url("loved", "kjv", SearchCriteria()).startswith(
                "https://search.example.test/custom/kjv?q=loved&"
            )
        )

    def test_refuses_unsendable_queries_and_translations_locally(self) -> None:
        opener = ScriptedOpener()
        client = GetBibleSearchClient(opener=opener)

        for query in ("", "   ", "\t\n", "x" * (MAX_QUERY_LENGTH + 1), "a\x00b", "a\x7fb", 5):
            with self.subTest(query=query), self.assertRaises(SearchInputError):
                client.search(query, "kjv", SearchCriteria())  # type: ignore[arg-type]
        for translation in ("../kjv", "", None, "k" * 65, "kjv/v2", "-kjv", "kjv?x"):
            with self.subTest(translation=translation), self.assertRaises(SearchInputError):
                client.search("loved", translation, SearchCriteria())  # type: ignore[arg-type]

        short = GetBibleSearchClient(opener=opener, max_query_length=10)
        with self.assertRaises(SearchInputError):
            short.search("x" * 11, "kjv", SearchCriteria())
        self.assertIn("q=xxxxxxxxxx&", short.build_url("x" * 10, "kjv", SearchCriteria()))
        # The bound applies after whitespace is collapsed, so padding is free.
        self.assertIn("q=x+x&", short.build_url("  x   \n  x  ", "kjv", SearchCriteria()))
        self.assertEqual(opener.requests, [])

    def test_returns_the_validated_envelope(self) -> None:
        opener = ScriptedOpener(FakeResponse(search_envelope()))
        client = GetBibleSearchClient(opener=opener, timeout_seconds=7.5)

        result = client.search("loved", "KJV", SearchCriteria(limit=50))

        self.assertEqual(set(result), {"query", "results", "matches"})
        self.assertEqual(result["query"]["kind"], "search")
        self.assertEqual(result["query"]["total"], 1)
        self.assertIn("kjv_43_3", result["results"])
        self.assertEqual(result["matches"][0]["reference"], "John 3:16")
        self.assertEqual(opener.timeouts, [7.5])
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("User-agent"), "getbible-robot/2.2")
        self.assertTrue(request.full_url.startswith("https://search.getbible.net/v2/kjv?q=loved&"))
        self.assertIn("&limit=50&offset=0", request.full_url)

    def test_accepts_reference_answers_and_structured_json_media_types(self) -> None:
        reference = search_envelope(kind="reference", total=1, returned=1)
        del reference["matches"][0]["terms"]
        response = FakeResponse(
            reference,
            content_type="application/problem+json",
            include_length=False,
        )

        result = GetBibleSearchClient(opener=ScriptedOpener(response)).search(
            "John 3:16", "kjv", SearchCriteria()
        )

        self.assertEqual(result["query"]["kind"], "reference")
        self.assertNotIn("terms", result["matches"][0])

    def test_rejects_unknown_result_kinds_and_metadata(self) -> None:
        def with_query(**fields: object) -> dict[str, Any]:
            envelope = search_envelope()
            envelope["query"].update(fields)
            return envelope

        without_kind = search_envelope()
        del without_kind["query"]["kind"]
        without_returned = search_envelope()
        del without_returned["query"]["returned"]
        cases: list[object] = [
            ["not", "an", "object"],
            {},
            {"query": {}, "results": {}},
            {"query": "search", "results": {}, "matches": []},
            {"query": {"kind": "search"}, "results": [], "matches": []},
            {"query": {"kind": "search"}, "results": {}, "matches": {}},
            without_kind,
            without_returned,
            with_query(kind="fuzzy"),
            with_query(kind=None),
            with_query(total=-1),
            with_query(total=True),
            with_query(total="1"),
            with_query(returned=-1),
            with_query(returned=1.0),
        ]
        for index, document in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(SearchResponseError):
                GetBibleSearchClient(opener=ScriptedOpener(FakeResponse(document))).search(
                    "loved", "kjv", SearchCriteria()
                )

        # Zero results is a valid answer, not a malformed one.
        empty = {
            "query": {"kind": "search", "total": 0, "returned": 0},
            "results": {},
            "matches": [],
        }
        result = GetBibleSearchClient(opener=ScriptedOpener(FakeResponse(empty))).search(
            "zzz", "kjv", SearchCriteria()
        )
        self.assertEqual(result["matches"], [])

    def test_enforces_content_type_and_size_before_parsing(self) -> None:
        valid = search_envelope()
        cases = [
            FakeResponse(valid, content_type=None),
            FakeResponse(valid, content_type="text/html"),
            FakeResponse(valid, content_type="text/json"),
            FakeResponse(valid, headers={"Content-Length": "not-a-number"}),
            FakeResponse(valid, headers={"Content-Length": "-1"}),
            FakeResponse(valid, headers={"Content-Length": "5000"}),
            FakeResponse(body=b"x" * 1025, include_length=False),
            FakeResponse(valid, body={"not": "bytes"}),
        ]
        for index, response in enumerate(cases):
            with self.subTest(index=index):
                client = GetBibleSearchClient(
                    opener=ScriptedOpener(response),
                    max_response_bytes=1024,
                )
                with self.assertRaises(SearchResponseError) as raised:
                    client.search("loved", "kjv", SearchCriteria())
                self.assertFalse(raised.exception.retryable)
        # Bodies are read with one byte of headroom so an oversize answer is
        # detected without buffering all of it.
        oversize = cases[6]
        self.assertEqual(oversize.read_amounts, [1025])

    def test_rejects_malformed_json(self) -> None:
        for body in (b"not-json", b"[NaN]", b"{\"query\": Infinity}", b"\xff\xfe", b""):
            with self.subTest(body=body):
                client = GetBibleSearchClient(opener=ScriptedOpener(FakeResponse(body=body)))
                with self.assertRaises(SearchResponseError):
                    client.search("loved", "kjv", SearchCriteria())

    def test_non_success_status_without_an_http_error(self) -> None:
        for status, retryable in ((301, False), (400, False), (404, False), (503, True)):
            with self.subTest(status=status):
                response = FakeResponse({}, status=status)
                client = GetBibleSearchClient(opener=ScriptedOpener(response))
                with self.assertRaises(SearchHTTPError) as raised:
                    client.search("loved", "kjv", SearchCriteria())
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(raised.exception.code, "")
                self.assertEqual(raised.exception.detail, "")
                self.assertIsNone(raised.exception.retry_after)
                self.assertEqual(raised.exception.retryable, retryable)

        for status in (None, True):
            with self.subTest(status=status):
                response = FakeResponse({}, status=status)
                client = GetBibleSearchClient(opener=ScriptedOpener(response))
                with self.assertRaises(SearchResponseError):
                    client.search("loved", "kjv", SearchCriteria())

    def test_problem_documents_populate_the_error(self) -> None:
        opener = ScriptedOpener(
            problem(
                400,
                {
                    "type": "https://search.getbible.net/problems/invalid_search",
                    "title": "Invalid search",
                    "status": 400,
                    "code": "invalid_search",
                    "detail": "  Search words\n are required. ",
                    "retry_after": 3,
                },
            )
        )
        client = GetBibleSearchClient(opener=opener)

        with self.assertRaises(SearchHTTPError) as raised:
            client.search("loved", "kjv", SearchCriteria())

        error = raised.exception
        self.assertEqual(error.status_code, 400)
        self.assertEqual(error.code, "invalid_search")
        self.assertEqual(error.detail, "Search words are required.")
        self.assertEqual(error.retry_after, 3)
        self.assertTrue(error.invalid_request)
        self.assertFalse(error.translation_not_found)
        self.assertFalse(error.retryable)
        self.assertIn("HTTP 400 (invalid_search)", str(error))
        self.assertIsInstance(error, GetBibleSearchError)

    def test_retry_after_header_takes_precedence_and_is_capped(self) -> None:
        cases: list[tuple[Mapping[str, str] | None, object, int | None]] = [
            ({"retry-after": "7"}, 30, 7),
            ({"Retry-After": " 12 "}, None, 12),
            ({"Retry-After": "99999"}, None, 3600),
            ({"Retry-After": "later"}, 12, 12),
            ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 99999, 3600),
            ({}, -1, None),
            ({}, True, None),
            ({}, "5", None),
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
                client = GetBibleSearchClient(opener=ScriptedOpener(error))
                with self.assertRaises(SearchHTTPError) as raised:
                    client.search("loved", "kjv", SearchCriteria())
                self.assertEqual(raised.exception.retry_after, expected)
                self.assertEqual(raised.exception.code, "rate_limited")
                self.assertTrue(raised.exception.retryable)

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
                client = GetBibleSearchClient(opener=ScriptedOpener(error))
                with self.assertRaises(SearchHTTPError) as raised:
                    client.search("loved", "kjv", SearchCriteria())
                self.assertEqual(raised.exception.status_code, 503)
                self.assertEqual(raised.exception.code, "")
                self.assertEqual(raised.exception.detail, "")
                self.assertIsNone(raised.exception.retry_after)
                self.assertTrue(raised.exception.retryable)

    def test_error_classification_properties(self) -> None:
        cases: list[tuple[SearchHTTPError, bool, bool, bool]] = [
            # error, translation_not_found, invalid_request, retryable
            (SearchHTTPError(404, code="translation_not_found"), True, False, False),
            (SearchHTTPError(404, code="unknown_version"), False, False, False),
            (SearchHTTPError(404, code="not_found"), False, True, False),
            (SearchHTTPError(404), False, True, False),
            (SearchHTTPError(400, code="invalid_search"), False, True, False),
            (SearchHTTPError(400), False, True, False),
            (SearchHTTPError(401, code="unauthorized"), False, False, False),
            (SearchHTTPError(405), False, False, False),
            (SearchHTTPError(408), False, False, True),
            (SearchHTTPError(415), False, False, False),
            (SearchHTTPError(425), False, False, True),
            (SearchHTTPError(429, code="rate_limited"), False, False, True),
            (SearchHTTPError(500), False, False, True),
            (SearchHTTPError(503, code="search_timeout"), False, False, True),
            (SearchHTTPError(599), False, False, True),
            (SearchHTTPError(301), False, False, False),
        ]
        for error, translation_not_found, invalid_request, retryable in cases:
            with self.subTest(status=error.status_code, code=error.code):
                self.assertEqual(error.translation_not_found, translation_not_found)
                self.assertEqual(error.invalid_request, invalid_request)
                self.assertEqual(error.retryable, retryable)
        self.assertEqual(str(SearchHTTPError(503)), "The GetBible Search API returned HTTP 503.")

    def test_transport_failures_are_retryable(self) -> None:
        cases: list[BaseException] = [
            URLError("offline"),
            TimeoutError(),
            OSError("network"),
            ConnectionResetError(),
            IncompleteRead(b"partial", 100),
        ]
        for action in cases:
            with self.subTest(action=type(action).__name__):
                client = GetBibleSearchClient(opener=ScriptedOpener(action))
                with self.assertRaises(SearchTransportError) as raised:
                    client.search("loved", "kjv", SearchCriteria())
                self.assertTrue(raised.exception.retryable)
                self.assertIsInstance(raised.exception, GetBibleSearchError)
                self.assertIsNone(raised.exception.__cause__)

    def test_redirects_are_reported_as_http_errors_not_followed(self) -> None:
        # The strict opener refuses to follow a redirect, which surfaces as the
        # 3xx answer itself. A redirect means the translation segment was not a
        # catalogue code, so it is an error and never a second request.
        opener = ScriptedOpener(
            HTTPError(
                "https://search.getbible.net/v2/KJV?q=loved",
                301,
                "Moved Permanently",
                {"Location": "https://search.getbible.net/v2/kjv?q=loved"},  # type: ignore[arg-type]
                None,
            )
        )
        client = GetBibleSearchClient(opener=opener)

        with self.assertRaises(SearchHTTPError) as raised:
            client.search("loved", "kjv", SearchCriteria())

        error = raised.exception
        self.assertEqual(error.status_code, 301)
        self.assertEqual(error.code, "")
        self.assertFalse(error.retryable)
        self.assertFalse(error.invalid_request)
        self.assertFalse(error.translation_not_found)
        self.assertEqual(len(opener.requests), 1)

    def test_input_and_response_errors_are_never_retryable(self) -> None:
        self.assertFalse(SearchInputError("bad").retryable)
        self.assertFalse(SearchResponseError("bad").retryable)
        self.assertTrue(SearchTransportError().retryable)
        for error in (SearchInputError("bad"), SearchResponseError("bad"), SearchTransportError()):
            self.assertIsInstance(error, GetBibleSearchError)
            self.assertIsInstance(error, RuntimeError)


if __name__ == "__main__":
    unittest.main()
