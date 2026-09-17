"""GetBibleBookmarksClient: verified reads of the public Bookmarks API.

The robot never trusts what comes back from ``bookmarks.getbible.net``.
These tests pin the requests the client sends (``index.json``,
``checksums.json`` and ``catalog.json``, in that order, bounded and without
following redirects) and every way an answer is refused: the wrong status, a
transport failure, an oversized or malformed body, a catalogue whose SHA-256
differs from the manifest, and every rule of the catalogue contract.
"""

import hashlib
import json
import math
import unittest
from collections.abc import Mapping
from http.client import IncompleteRead
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from modules.bible_canon import BOOK_CHAPTER_COUNTS
from modules.getbible_bookmarks import (
    MAX_LINKS,
    MAX_TOPICS,
    BookmarksCatalog,
    BookmarksHTTPError,
    BookmarksIndex,
    BookmarksResponseError,
    BookmarksTransportError,
    BookmarkTopic,
    GetBibleBookmarksClient,
    GetBibleBookmarksError,
    _RejectRedirectHandler,
    parse_catalog_document,
)

BASE_URL = "https://bookmarks.getbible.net/v1"


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def encoded_document(document: object) -> bytes:
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


def index_document(**changes: object) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "catalog_version": 7,
        "checksum": "a" * 64,
        "counts": {"topics": 2, "verses": 3, "locales": 1},
        "resources": {"catalog": "catalog.json", "all": "all.json"},
        "locales": ["en"],
    }
    document.update(changes)
    return document


def topic_document(**changes: object) -> dict[str, Any]:
    topic: dict[str, Any] = {
        "id": "grace",
        "name": "Grace",
        "color": "#bbf7d0",
        "aliases": ["Favour"],
        "default": True,
        "verses": [[43, 3, 16], [43, 3, 17]],
    }
    topic.update(changes)
    return topic


def catalog_document(topics: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if topics is None:
        topics = [
            topic_document(),
            topic_document(id="mercy", name="Mercy", aliases=[], default=False, verses=[[1, 1, 1]]),
        ]
    return {"schema_version": 1, "topics": topics}


def checksums_document(catalog: bytes | None, **extra: str) -> dict[str, Any]:
    files: dict[str, str] = {"all.json": "b" * 64, **extra}
    if catalog is not None:
        files["catalog.json"] = sha256(catalog)
    return {"schema_version": 1, "files": files}


class FakeResponse:
    def __init__(
        self,
        document: object | None = None,
        *,
        body: bytes | object | None = None,
        status: int | None = 200,
        include_length: bool = True,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if body is None:
            body = encoded_document(document)
        self.body = body
        self.status = status
        self.headers = dict(headers or {})
        if include_length and isinstance(body, bytes) and "Content-Length" not in self.headers:
            self.headers["content-length"] = str(len(body))
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


def http_error(status: int, path: str = "index.json") -> HTTPError:
    return HTTPError(
        f"{BASE_URL}/{path}",
        status,
        "Problem",
        {"Content-Type": "text/plain"},  # type: ignore[arg-type]
        None,
    )


def catalog_client(
    catalog: bytes,
    *,
    manifest: dict[str, Any] | None = None,
    **options: Any,
) -> tuple[GetBibleBookmarksClient, ScriptedOpener]:
    opener = ScriptedOpener(
        FakeResponse(checksums_document(catalog) if manifest is None else manifest),
        FakeResponse(body=catalog),
    )
    return GetBibleBookmarksClient(opener=opener, **options), opener


class GetBibleBookmarksClientTestCase(unittest.TestCase):
    def test_rejects_invalid_configuration(self) -> None:
        invalid: list[dict[str, object]] = [
            {"base_url": "http://bookmarks.getbible.net/v1"},
            {"base_url": "https://bookmarks.getbible.net/v1?x=1"},
            {"base_url": "https://bookmarks.getbible.net/v1#frag"},
            {"base_url": None},
            {"timeout_seconds": 0},
            {"timeout_seconds": -1.0},
            {"timeout_seconds": math.inf},
            {"timeout_seconds": math.nan},
            {"timeout_seconds": True},
            {"timeout_seconds": "3"},
            {"max_response_bytes": 64 * 1024 - 1},
            {"max_response_bytes": True},
            {"max_response_bytes": 65536.0},
        ]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(BookmarksResponseError):
                GetBibleBookmarksClient(**options)  # type: ignore[arg-type]

        client = GetBibleBookmarksClient(
            base_url="https://bookmarks.example.test/v1/",
            timeout_seconds=4,
            max_response_bytes=64 * 1024,
        )
        self.assertEqual(client.base_url, "https://bookmarks.example.test/v1")
        self.assertEqual(client.timeout_seconds, 4.0)
        self.assertEqual(client.max_response_bytes, 64 * 1024)

    def test_default_opener_refuses_to_follow_redirects(self) -> None:
        handler = _RejectRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                Request(f"{BASE_URL}/index.json"),
                None,
                301,
                "Moved Permanently",
                {},
                "https://bookmarks.getbible.net/v2/index.json",
            )
        )
        client = GetBibleBookmarksClient()
        self.assertEqual(client.base_url, BASE_URL)
        self.assertTrue(callable(client._opener))

    def test_index_returns_the_discovery_document(self) -> None:
        opener = ScriptedOpener(FakeResponse(index_document()))
        client = GetBibleBookmarksClient(opener=opener, timeout_seconds=7.5)

        index = client.index()

        self.assertEqual(
            index,
            BookmarksIndex(
                catalog_version=7,
                checksum="a" * 64,
                topics=2,
                verses=3,
                locales=("en",),
            ),
        )
        self.assertEqual(opener.timeouts, [7.5])
        request = opener.requests[0]
        self.assertEqual(request.full_url, f"{BASE_URL}/index.json")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("User-agent"), "getbible-robot/2.2")

    def test_index_validation_follows_the_contract(self) -> None:
        cases: list[dict[str, Any]] = [
            {"schema_version": 2},
            {"catalog_version": 0},
            {"catalog_version": True},
            {"catalog_version": "7"},
            {"checksum": "A" * 64},
            {"checksum": "a" * 63},
            {"checksum": 7},
            {"counts": None},
            {"counts": {"topics": 2, "verses": 3}},
            {"counts": {"topics": -1, "verses": 3, "locales": 1}},
            {"counts": {"topics": MAX_TOPICS + 1, "verses": 3, "locales": 1}},
            {"counts": {"topics": True, "verses": 3, "locales": 1}},
            {"counts": {"topics": 2, "verses": MAX_LINKS + 1, "locales": 1}},
            {"counts": {"topics": 2, "verses": 3, "locales": 501}},
            {"locales": "en"},
            {"locales": ["EN"]},
            {"locales": ["e"]},
            {"locales": ["a" * 17]},
            {"locales": [None]},
            {"locales": ["en"] * 501},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                client = GetBibleBookmarksClient(
                    opener=ScriptedOpener(FakeResponse(index_document(**changes)))
                )
                with self.assertRaises(BookmarksResponseError) as raised:
                    client.index()
                self.assertFalse(raised.exception.retryable)
        # A list document is not an index either.
        client = GetBibleBookmarksClient(opener=ScriptedOpener(FakeResponse([])))
        with self.assertRaises(BookmarksResponseError):
            client.index()

    def test_index_is_bounded_to_sixty_four_kib_regardless_of_the_client_limit(self) -> None:
        padded = index_document(resources={"padding": "x" * (64 * 1024)})
        client = GetBibleBookmarksClient(opener=ScriptedOpener(FakeResponse(padded)))
        with self.assertRaises(BookmarksResponseError) as raised:
            client.index()
        self.assertIn("too large", str(raised.exception))

        oversize = FakeResponse(body=b"x" * (64 * 1024 + 1), include_length=False)
        client = GetBibleBookmarksClient(opener=ScriptedOpener(oversize))
        with self.assertRaises(BookmarksResponseError):
            client.index()
        # Bodies are read with one byte of headroom so an oversize answer is
        # detected without buffering all of it.
        self.assertEqual(oversize.read_amounts, [64 * 1024 + 1])

    def test_checksums_returns_the_manifest(self) -> None:
        payload = encoded_document(catalog_document())
        manifest = checksums_document(payload, **{"topics.json": "c" * 64})
        opener = ScriptedOpener(FakeResponse(manifest))
        client = GetBibleBookmarksClient(opener=opener)

        manifest = client.checksums()

        self.assertEqual(
            manifest,
            {"all.json": "b" * 64, "catalog.json": sha256(payload), "topics.json": "c" * 64},
        )
        self.assertEqual(opener.requests[0].full_url, f"{BASE_URL}/checksums.json")

    def test_checksums_validation_follows_the_contract(self) -> None:
        cases: list[object] = [
            {"schema_version": 2, "files": {"catalog.json": "a" * 64}},
            {"schema_version": 1},
            {"schema_version": 1, "files": {}},
            {"schema_version": 1, "files": []},
            {"schema_version": 1, "files": {"": "a" * 64}},
            {"schema_version": 1, "files": {"x" * 257: "a" * 64}},
            {"schema_version": 1, "files": {"catalog.json": "A" * 64}},
            {"schema_version": 1, "files": {"catalog.json": 1}},
            [],
        ]
        for document in cases:
            with self.subTest(document=document):
                client = GetBibleBookmarksClient(opener=ScriptedOpener(FakeResponse(document)))
                with self.assertRaises(BookmarksResponseError):
                    client.checksums()

    def test_catalog_fetches_the_manifest_then_the_verified_document(self) -> None:
        payload = encoded_document(catalog_document())
        client, opener = catalog_client(payload, timeout_seconds=3)

        catalog = client.catalog()

        self.assertIsInstance(catalog, BookmarksCatalog)
        self.assertEqual(catalog.checksum, sha256(payload))
        self.assertEqual(catalog.document, payload)
        self.assertEqual([topic.id for topic in catalog.topics], ["grace", "mercy"])
        grace = catalog.topics[0]
        self.assertEqual(
            grace,
            BookmarkTopic(
                id="grace",
                name="Grace",
                color="#bbf7d0",
                aliases=("Favour",),
                default=True,
                verses=((43, 3, 16), (43, 3, 17)),
            ),
        )
        self.assertEqual(
            grace.as_dict(),
            {
                "id": "grace",
                "name": "Grace",
                "color": "#bbf7d0",
                "aliases": ["Favour"],
                "default": True,
                "verses": [[43, 3, 16], [43, 3, 17]],
            },
        )
        self.assertEqual(catalog.topic_ids, frozenset({"grace", "mercy"}))
        self.assertEqual(
            catalog.associations,
            frozenset({("grace", 43, 3, 16), ("grace", 43, 3, 17), ("mercy", 1, 1, 1)}),
        )
        self.assertEqual(catalog.link_count, 3)
        self.assertEqual(
            [request.full_url for request in opener.requests],
            [f"{BASE_URL}/checksums.json", f"{BASE_URL}/catalog.json"],
        )
        self.assertEqual(opener.timeouts, [3.0, 3.0])

        # The raw bytes are available for saving to disk, verified the same way.
        client, _ = catalog_client(payload)
        self.assertEqual(client.fetch_catalog_json(), payload)

    def test_catalog_checksum_mismatch_and_missing_manifest_entry_are_refused(self) -> None:
        payload = encoded_document(catalog_document())
        client, opener = catalog_client(
            payload,
            manifest={"schema_version": 1, "files": {"catalog.json": "f" * 64}},
        )
        with self.assertRaises(BookmarksResponseError) as raised:
            client.catalog()
        self.assertIn("did not match its checksum", str(raised.exception))
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(opener.requests), 2)

        client, opener = catalog_client(payload, manifest=checksums_document(None))
        with self.assertRaises(BookmarksResponseError) as raised:
            client.catalog()
        self.assertIn("omits catalog.json", str(raised.exception))
        # Nothing is downloaded when the manifest cannot vouch for it.
        self.assertEqual(len(opener.requests), 1)

    def test_catalog_body_and_size_are_checked_before_parsing(self) -> None:
        payload = encoded_document(catalog_document())
        limit = 64 * 1024
        cases: list[tuple[FakeResponse, str]] = [
            (
                FakeResponse(body=payload, headers={"Content-Length": "not-a-number"}),
                "Content-Length",
            ),
            (FakeResponse(body=payload, headers={"Content-Length": "-1"}), "too large"),
            (FakeResponse(body=payload, headers={"Content-Length": str(limit + 1)}), "too large"),
            (FakeResponse(body=b"x" * (limit + 1), include_length=False), "too large"),
            (FakeResponse(body={"not": "bytes"}), "non-binary"),
        ]
        for response, message in cases:
            with self.subTest(message=message, body=str(response.body)[:24]):
                opener = ScriptedOpener(FakeResponse(checksums_document(payload)), response)
                client = GetBibleBookmarksClient(opener=opener, max_response_bytes=limit)
                with self.assertRaises(BookmarksResponseError) as raised:
                    client.catalog()
                self.assertIn(message, str(raised.exception))
                self.assertFalse(raised.exception.retryable)

    def test_malformed_json_is_refused(self) -> None:
        bodies = [b"not-json", b"[NaN]", b'{"topics": Infinity}', b"\xff\xfe", b"", b"\xed\xa0\x80"]
        for body in bodies:
            with self.subTest(body=body):
                client = GetBibleBookmarksClient(opener=ScriptedOpener(FakeResponse(body=body)))
                with self.assertRaises(BookmarksResponseError):
                    client.index()

    def test_non_success_status_is_an_http_error(self) -> None:
        expectations = {
            301: False,
            400: False,
            404: False,
            408: True,
            425: True,
            429: True,
            500: True,
            503: True,
        }
        for status, retryable in expectations.items():
            with self.subTest(status=status, kind="status"):
                opener = ScriptedOpener(FakeResponse({}, status=status))
                client = GetBibleBookmarksClient(opener=opener)
                with self.assertRaises(BookmarksHTTPError) as raised:
                    client.index()
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(raised.exception.path, "index.json")
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertIn(f"HTTP {status} for index.json", str(raised.exception))
            with self.subTest(status=status, kind="HTTPError"):
                client = GetBibleBookmarksClient(opener=ScriptedOpener(http_error(status)))
                with self.assertRaises(BookmarksHTTPError) as raised:
                    client.index()
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertIsNone(raised.exception.__cause__)

        for status in (None, True):
            with self.subTest(status=status):
                opener = ScriptedOpener(FakeResponse({}, status=status))
                client = GetBibleBookmarksClient(opener=opener)
                with self.assertRaises(BookmarksResponseError):
                    client.index()

    def test_transport_failures_are_retryable_and_carry_no_detail(self) -> None:
        cases: list[BaseException] = [
            URLError("offline"),
            TimeoutError(),
            OSError("network"),
            ConnectionResetError(),
            IncompleteRead(b"partial", 100),
        ]
        for action in cases:
            with self.subTest(action=type(action).__name__):
                client = GetBibleBookmarksClient(opener=ScriptedOpener(action))
                with self.assertRaises(BookmarksTransportError) as raised:
                    client.index()
                self.assertTrue(raised.exception.retryable)
                self.assertIsNone(raised.exception.__cause__)
                self.assertNotIn("offline", str(raised.exception))

    def test_redirects_are_reported_as_http_errors_not_followed(self) -> None:
        # The strict opener refuses to follow a redirect, which surfaces as
        # the 3xx answer itself; the client never issues a second request.
        redirect = HTTPError(
            f"{BASE_URL}/checksums.json",
            302,
            "Found",
            {"Location": "https://elsewhere.example/checksums.json"},  # type: ignore[arg-type]
            None,
        )
        opener = ScriptedOpener(redirect)
        client = GetBibleBookmarksClient(opener=opener)

        with self.assertRaises(BookmarksHTTPError) as raised:
            client.catalog()

        self.assertEqual(raised.exception.status_code, 302)
        self.assertEqual(raised.exception.path, "checksums.json")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(opener.requests), 1)

    def test_error_classification(self) -> None:
        for error in (
            BookmarksResponseError("bad"),
            BookmarksTransportError(),
            BookmarksHTTPError(404, "index.json"),
            BookmarksHTTPError(503, "index.json"),
        ):
            self.assertIsInstance(error, GetBibleBookmarksError)
            self.assertIsInstance(error, RuntimeError)
        self.assertFalse(BookmarksResponseError("bad").retryable)
        self.assertTrue(BookmarksTransportError().retryable)
        self.assertFalse(BookmarksHTTPError(404, "index.json").retryable)
        self.assertTrue(BookmarksHTTPError(503, "index.json").retryable)


class ParseCatalogDocumentTestCase(unittest.TestCase):
    def assert_refused(self, document: object, message: str) -> None:
        payload = document if isinstance(document, bytes) else encoded_document(document)
        with self.assertRaises(BookmarksResponseError) as raised:
            parse_catalog_document(payload)  # type: ignore[arg-type]
        self.assertIn(message, str(raised.exception))
        self.assertFalse(raised.exception.retryable)

    def test_accepts_a_contract_conforming_document(self) -> None:
        payload = encoded_document(catalog_document())
        catalog = parse_catalog_document(payload)
        self.assertEqual(catalog.checksum, sha256(payload))
        self.assertEqual(catalog.document, payload)
        self.assertEqual(len(catalog.topics), 2)
        self.assertEqual(catalog.link_count, 3)

        # An empty catalogue is valid; so are names that use the full
        # allowed alphabet and ids with several hyphenated segments.
        self.assertEqual(parse_catalog_document(encoded_document(catalog_document([]))).topics, ())
        fancy = catalog_document(
            [
                topic_document(
                    id="gods-grace-and-mercy-2",
                    name="God's grace & mercy (part 2): why (not)",
                    aliases=["Grace (2)", "Mercy: part 2"],
                    verses=[[1, 1, 1], [66, 22, 2000]],
                )
            ]
        )
        parsed = parse_catalog_document(encoded_document(fancy))
        self.assertEqual(parsed.topics[0].verses, ((1, 1, 1), (66, 22, 2000)))

    def test_rejects_non_bytes_and_malformed_text(self) -> None:
        with self.assertRaises(BookmarksResponseError):
            parse_catalog_document("{}")  # type: ignore[arg-type]
        self.assert_refused(b"\xff", "invalid UTF-8")
        self.assert_refused(b"{", "malformed JSON")
        self.assert_refused(b"[NaN]", "malformed JSON")

    def test_rejects_the_wrong_envelope(self) -> None:
        self.assert_refused([], "unsupported schema")
        self.assert_refused({"schema_version": 2, "topics": []}, "unsupported schema")
        self.assert_refused({"topics": []}, "unsupported schema")
        self.assert_refused(
            {"schema_version": 1, "topics": [], "locales": {}}, "unexpected members"
        )
        self.assert_refused({"schema_version": 1}, "unexpected members")
        self.assert_refused({"schema_version": 1, "topics": {}}, "topic list is invalid")
        self.assert_refused(
            catalog_document([topic_document(id=f"t{index}") for index in range(MAX_TOPICS + 1)]),
            "topic list is invalid",
        )

    def test_rejects_malformed_topics(self) -> None:
        self.assert_refused(catalog_document([[]]), "topics[0] is not an object")
        without_verses = topic_document()
        del without_verses["verses"]
        self.assert_refused(catalog_document([without_verses]), "topics[0] has unexpected members")
        self.assert_refused(
            catalog_document([topic_document(names={"en": "Grace"})]),
            "topics[0] has unexpected members",
        )
        self.assert_refused(
            catalog_document([topic_document(), []]),
            "topics[1] is not an object",
        )

    def test_rejects_invalid_ids(self) -> None:
        for topic_id in ("Grace", "", "a" * 81, "-grace", "grace-", "gods--grace", "gr_ace", 7):
            with self.subTest(topic_id=topic_id):
                self.assert_refused(
                    catalog_document([topic_document(id=topic_id)]), "id is invalid"
                )
        parse_catalog_document(encoded_document(catalog_document([topic_document(id="a" * 80)])))

    def test_rejects_invalid_names_and_aliases(self) -> None:
        invalid_names = [
            "G", "a" * 81, " Grace", "Grace ", "Gr  ace", "Grâce", "Grace!", "-Grace", "Why?", 3
        ]
        for name in invalid_names:
            with self.subTest(name=name):
                self.assert_refused(
                    catalog_document([topic_document(name=name)]), "name is invalid"
                )
        for aliases in ("Favour", [["Favour"]], ["G"], ["a" * 81], ["Gr  ace"], ["x"] * 21):
            with self.subTest(aliases=aliases):
                self.assert_refused(
                    catalog_document([topic_document(aliases=aliases)]),
                    "aliases are invalid",
                )
        self.assert_refused(
            catalog_document([topic_document(aliases=["Favour", "favour"])]),
            "aliases are invalid",
        )
        # Twenty distinct aliases are the documented maximum.
        twenty = [f"Alias {index}" for index in range(20)]
        parse_catalog_document(encoded_document(catalog_document([topic_document(aliases=twenty)])))

    def test_rejects_invalid_colours_and_default_flags(self) -> None:
        for color in ("#BBF7D0", "#bbf7d", "bbf7d0", "#bbf7d0ff", None):
            with self.subTest(color=color):
                self.assert_refused(
                    catalog_document([topic_document(color=color)]), "colour is invalid"
                )
        for default in (1, 0, "true", None):
            with self.subTest(default=default):
                self.assert_refused(
                    catalog_document([topic_document(default=default)]),
                    "default flag is invalid",
                )

    def test_rejects_invalid_verse_lists_and_coordinates(self) -> None:
        for verses in ({}, "43:3:16", None):
            with self.subTest(verses=verses):
                self.assert_refused(
                    catalog_document([topic_document(verses=verses)]),
                    "verse list is invalid",
                )
        invalid_coordinates: list[object] = [
            [43, 3],
            [43, 3, 16, 1],
            [0, 1, 1],
            [67, 1, 1],
            [43, 22, 1],
            [43, 0, 1],
            [43, 3, 0],
            [43, 3, 2001],
            [True, 1, 1],
            [43, 3, 16.0],
            ["43", 3, 16],
            "43:3:16",
            {"book": 43, "chapter": 3, "verse": 16},
        ]
        for coordinate in invalid_coordinates:
            with self.subTest(coordinate=coordinate):
                self.assert_refused(
                    catalog_document([topic_document(verses=[coordinate])]),
                    "invalid coordinate",
                )
        # Every book's last chapter is inside the canon; one more is not.
        for book, chapters in enumerate(BOOK_CHAPTER_COUNTS, start=1):
            parse_catalog_document(
                encoded_document(catalog_document([topic_document(verses=[[book, chapters, 1]])]))
            )
            self.assert_refused(
                catalog_document([topic_document(verses=[[book, chapters + 1, 1]])]),
                "invalid coordinate",
            )

    def test_rejects_unsorted_or_duplicate_verses(self) -> None:
        unsorted = (
            [[43, 3, 16], [43, 3, 16]],
            [[43, 3, 17], [43, 3, 16]],
            [[44, 1, 1], [43, 3, 16]],
        )
        for verses in unsorted:
            with self.subTest(verses=verses):
                self.assert_refused(
                    catalog_document([topic_document(verses=verses)]),
                    "not strictly ascending",
                )

    def test_rejects_repeated_ids_and_reused_names(self) -> None:
        self.assert_refused(
            catalog_document([topic_document(), topic_document(name="Mercy", aliases=[])]),
            "repeats topic 'grace'",
        )
        self.assert_refused(
            catalog_document(
                [topic_document(), topic_document(id="mercy", name="grace", aliases=[])]
            ),
            "reuses the name 'grace'",
        )
        self.assert_refused(
            catalog_document(
                [topic_document(), topic_document(id="mercy", name="Mercy", aliases=["FAVOUR"])]
            ),
            "reuses the name 'FAVOUR'",
        )
        # A topic repeating its own name as an alias is not a cross-topic
        # reuse; the client leaves that to the builder's validation.
        parse_catalog_document(encoded_document(catalog_document([topic_document(aliases=["grace"])])))

    def test_rejects_more_links_than_the_contract_allows(self) -> None:
        triples: list[list[int]] = []
        for book, chapters in enumerate(BOOK_CHAPTER_COUNTS, start=1):
            for chapter in range(1, chapters + 1):
                for verse in range(1, 2001):
                    triples.append([book, chapter, verse])
                    if len(triples) > MAX_LINKS:
                        break
                if len(triples) > MAX_LINKS:
                    break
            if len(triples) > MAX_LINKS:
                break
        first, second = triples[:60_000], triples[60_000:]
        self.assertEqual(len(first) + len(second), MAX_LINKS + 1)
        document = catalog_document(
            [
                topic_document(verses=first),
                topic_document(id="mercy", name="Mercy", aliases=[], verses=second),
            ]
        )
        self.assert_refused(document, "exceeds the link limit")
        # One topic alone is refused earlier by the per-list bound.
        self.assert_refused(
            catalog_document([topic_document(verses=triples)]),
            "verse list is invalid",
        )
        # Exactly the limit is accepted.
        document["topics"][1]["verses"] = second[:-1]
        self.assertEqual(parse_catalog_document(encoded_document(document)).link_count, MAX_LINKS)


if __name__ == "__main__":
    unittest.main()
