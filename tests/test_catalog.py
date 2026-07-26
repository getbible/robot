import hashlib
import json
import unittest
from unittest.mock import patch

from getbible import (
    CacheIntegrityError,
    RepositoryError,
    RepositoryResponseError,
    RepositoryResponseTooLarge,
)

from modules.catalog import BookOption, CatalogClient


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_length: str | None = None,
    ) -> None:
        self.body = body
        self.status_code = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        return [
            self.body[index : index + chunk_size]
            for index in range(0, len(self.body), chunk_size)
        ]


def _client(*, max_bytes: int = 4096, retries: int = 0) -> CatalogClient:
    return CatalogClient(
        base_url="https://api.getbible.net",
        timeout=(1.0, 1.0),
        request_retries=retries,
        max_response_bytes=max_bytes,
    )


class CatalogClientTestCase(unittest.TestCase):
    def test_translation_and_book_catalogs_are_validated(self) -> None:
        translations = json.dumps(
            {
                "kjv": {
                    "abbreviation": "kjv",
                    "translation": "King James Version",
                    "language": "English",
                }
            }
        ).encode()
        books = json.dumps(
            {
                "43": {
                    "abbreviation": "kjv",
                    "nr": 43,
                    "name": "John",
                    "sha": "a" * 40,
                }
            }
        ).encode()
        with patch(
            "modules.catalog.requests.get",
            side_effect=[_Response(translations), _Response(books)],
        ):
            client = _client()
            translation_options = client.translations()
            book_options = client.books("kjv")

        self.assertEqual(translation_options[0].code, "kjv")
        self.assertEqual(book_options[0].name, "John")

    def test_translation_catalog_tolerates_missing_display_language(self) -> None:
        translations = json.dumps(
            {
                "kjv": {
                    "abbreviation": "kjv",
                    "translation": "King James Version",
                    "lang": "en",
                    "language": "English",
                },
                "klv": {
                    "abbreviation": "klv",
                    "translation": "Klingon Language Version",
                    "lang": "tlh",
                    "language": "",
                },
                "mal1910": {
                    "abbreviation": "mal1910",
                    "translation": "Malayalam Bible",
                },
            }
        ).encode()
        with patch(
            "modules.catalog.requests.get",
            return_value=_Response(translations),
        ):
            options = _client().translations()

        by_code = {option.code: option for option in options}
        self.assertEqual(set(by_code), {"kjv", "klv", "mal1910"})
        self.assertEqual(by_code["kjv"].language, "English")
        self.assertEqual(by_code["klv"].language, "tlh")
        self.assertEqual(by_code["mal1910"].language, "Unspecified")

    def test_one_malformed_translation_does_not_disable_valid_picker_entries(
        self,
    ) -> None:
        translations = json.dumps(
            {
                "kjv": {
                    "abbreviation": "kjv",
                    "translation": "King James Version",
                    "language": "English",
                },
                "unsafe code": {
                    "abbreviation": "unsafe code",
                    "translation": "Rejected",
                    "language": "English",
                },
            }
        ).encode()
        with (
            patch(
                "modules.catalog.requests.get",
                return_value=_Response(translations),
            ),
            self.assertLogs("modules.catalog", level="WARNING") as captured,
        ):
            options = _client().translations()

        self.assertEqual([option.code for option in options], ["kjv"])
        self.assertIn("Ignored 1 malformed translation catalog entry", captured.output[0])

    def test_catalog_without_any_usable_translation_fails_closed(self) -> None:
        translations = json.dumps(
            {
                "unsafe code": {
                    "abbreviation": "unsafe code",
                    "translation": "",
                    "language": [],
                }
            }
        ).encode()
        with (
            patch(
                "modules.catalog.requests.get",
                return_value=_Response(translations),
            ),
            self.assertRaises(RepositoryResponseError),
        ):
            _client().translations()

    def test_book_navigation_requires_published_checksum(self) -> None:
        payload = json.dumps(
            {
                "abbreviation": "kjv",
                "nr": 43,
                "name": "John",
                "chapters": [
                    {
                        "chapter": 3,
                        "verses": [
                            {"verse": 1},
                            {"verse": 2},
                        ],
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()
        sha = hashlib.sha1(payload, usedforsecurity=False).hexdigest()
        with patch(
            "modules.catalog.requests.get",
            return_value=_Response(payload),
        ):
            chapters = _client().chapters(
                "kjv",
                BookOption(43, "John", sha),
            )
        self.assertEqual(chapters[0].number, 3)
        self.assertEqual(chapters[0].verses, (1, 2))

        with (
            patch(
                "modules.catalog.requests.get",
                return_value=_Response(payload),
            ),
            self.assertRaises(CacheIntegrityError),
        ):
            _client().chapters("kjv", BookOption(43, "John", "0" * 40))

    def test_redirects_and_oversized_responses_fail_closed(self) -> None:
        with (
            patch(
                "modules.catalog.requests.get",
                return_value=_Response(b"", status=302),
            ),
            self.assertRaises(RepositoryError),
        ):
            _client().translations()

        with (
            patch(
                "modules.catalog.requests.get",
                return_value=_Response(
                    b"{}",
                    content_length="9999",
                ),
            ),
            self.assertRaises(RepositoryResponseTooLarge),
        ):
            _client(max_bytes=100).translations()

    def test_transient_server_failure_is_retried_only_within_budget(self) -> None:
        translations = json.dumps(
            {
                "kjv": {
                    "abbreviation": "kjv",
                    "translation": "King James Version",
                    "language": "English",
                }
            }
        ).encode()
        with patch(
            "modules.catalog.requests.get",
            side_effect=[
                _Response(b"", status=503),
                _Response(translations),
            ],
        ) as request:
            result = _client(retries=1).translations()
        self.assertEqual(result[0].code, "kjv")
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
