import asyncio
import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from modules import miniapp_sessions
from modules.catalog import (
    BookOption,
    ChapterContent,
    ChapterOption,
    ChapterVerse,
    TranslationOption,
)
from modules.errors import RobotRateLimited, ScriptureUnavailable
from modules.interactions import SearchOptions, SearchResult
from modules.miniapp_api import MiniAppApi, MiniAppHttpRequest
from modules.miniapp_auth import TelegramInitDataValidator
from modules.miniapp_sessions import MiniAppLaunchStore, MiniAppSessionStore
from modules.preferences import ReaderLocation, SearchDefaults, UserPreferences
from modules.service import ScriptureQuery, SearchPage

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
PUBLIC_URL = "https://robot.example/getbible"
ORIGIN = "https://robot.example"


def _init_data(
    user_id: int = 42,
    *,
    start_param: str | None = None,
    query_id: str = "query-id",
) -> str:
    fields = {
        "auth_date": "1700000000",
        "query_id": query_id,
        "user": json.dumps({"id": user_id, "first_name": "Grace"}, separators=(",", ":")),
    }
    if start_param is not None:
        fields["start_param"] = start_param
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class _Preferences:
    def __init__(self) -> None:
        self.values: dict[int, str] = {}
        self.reader_locations: dict[int, ReaderLocation] = {}
        self.fail_preferences_once = False

    def translation_for(self, user_id: int) -> str:
        return self.values.get(user_id, "kjv")

    def set_translation(self, user_id: int, translation: str) -> None:
        self.values[user_id] = translation

    def preferences_for(self, user_id: int) -> UserPreferences:
        if self.fail_preferences_once:
            self.fail_preferences_once = False
            raise RuntimeError("temporary preference read failure")
        return UserPreferences(
            self.translation_for(user_id),
            SearchDefaults(),
            self.reader_locations.get(user_id),
        )

    def set_search_defaults(
        self,
        user_id: int,
        defaults: SearchDefaults,
    ) -> None:
        return None

    def set_reader_location(
        self,
        user_id: int,
        location: ReaderLocation,
    ) -> None:
        self.reader_locations[user_id] = location

    def update_preferences(
        self,
        user_id: int,
        *,
        translation: str | None = None,
        search_defaults: SearchDefaults | None = None,
        reader_location: ReaderLocation | None | object = ...,
    ) -> UserPreferences:
        current = self.preferences_for(user_id)
        code = translation or current.translation
        if reader_location is ...:
            location = current.reader_location
            if location is not None and location.translation != code:
                location = None
        else:
            location = reader_location
        if location is None:
            self.reader_locations.pop(user_id, None)
        elif isinstance(location, ReaderLocation):
            if location.translation != code:
                raise ValueError(
                    "Reader location translation must match the preferred translation."
                )
            self.reader_locations[user_id] = location
        self.values[user_id] = code
        return UserPreferences(
            code,
            search_defaults or current.search_defaults,
            self.reader_locations.get(user_id),
        )


class _Limiter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.details: list[tuple[int, int, float, str | None]] = []
        self.rejection: RobotRateLimited | None = None

    async def acquire(
        self,
        *,
        user_id: int,
        chat_id: int,
        cost: float = 1.0,
        client_key: str | None = None,
    ) -> None:
        self.calls.append((user_id, chat_id))
        self.details.append((user_id, chat_id, cost, client_key))
        if self.rejection is not None:
            raise self.rejection


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _Service:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            max_input_length=250,
            max_references=10,
            max_total_verses=50,
            max_verses_per_reference=50,
            mini_app_max_selections=50,
        )
        self.selected: list[ScriptureQuery] = []
        self.search_requests: list[tuple[str, SearchOptions]] = []
        self.chapter_requests: list[tuple[str, int, int]] = []
        self.long_chapter = False
        self.fail_translations_once = False
        self.chapter_verses: dict[str, tuple[int, ...]] = {}

    async def translations(self) -> tuple[TranslationOption, ...]:
        if self.fail_translations_once:
            self.fail_translations_once = False
            raise ScriptureUnavailable("temporary translation failure")
        return (
            TranslationOption("kjv", "King James Version", "English"),
            TranslationOption("aov", "Afrikaanse Ou Vertaling", "Afrikaans", "af"),
        )

    async def books(self, translation: str) -> tuple[BookOption, ...]:
        if self.long_chapter:
            return (BookOption(19, "Psalms", "b" * 40),)
        return (BookOption(43, "John", "a" * 40),)

    async def chapters(
        self,
        translation: str,
        book: BookOption,
    ) -> tuple[ChapterOption, ...]:
        if self.long_chapter:
            return (ChapterOption(119, tuple(range(1, 177))),)
        return (
            ChapterOption(
                3,
                self.chapter_verses.get(translation, (1, 2, 16)),
            ),
        )

    async def resolve_query(
        self,
        arguments: list[str],
        *,
        default_translation: str | None = None,
    ) -> ScriptureQuery:
        return ScriptureQuery(" ".join(arguments), default_translation or "kjv")

    async def select(self, query: ScriptureQuery) -> dict:
        self.selected.append(query)
        if self.long_chapter:
            raw_numbers = query.references.rsplit(":", 1)[1]
            numbers: list[int] = []
            for part in raw_numbers.split(","):
                start, separator, end = part.partition("-")
                if separator:
                    numbers.extend(range(int(start), int(end) + 1))
                else:
                    numbers.append(int(start))
            return {
                "kjv_19_119": {
                    "book_name": "Psalms",
                    "abbreviation": query.translation,
                    "chapter": 119,
                    "verses": [
                        {"verse": number, "text": f"Psalm verse {number}."} for number in numbers
                    ],
                }
            }
        return {
            "kjv_43_3": {
                "book_name": "John",
                "abbreviation": query.translation,
                "chapter": 3,
                "verses": [{"verse": 16, "text": "For God so loved the world."}],
            }
        }

    async def chapter(
        self,
        translation: str,
        book: BookOption,
        chapter: ChapterOption,
    ) -> ChapterContent:
        self.chapter_requests.append((translation, book.number, chapter.number))
        verses = (
            tuple(
                ChapterVerse(number, f"Psalm verse {number}.")
                for number in chapter.verses
            )
            if self.long_chapter
            else (
                ChapterVerse(1, "There was a man of the Pharisees."),
                ChapterVerse(2, "The same came to Jesus by night."),
                ChapterVerse(16, "For God so loved the world."),
            )
        )
        return ChapterContent(
            translation=translation,
            translation_name="King James Version",
            book_number=book.number,
            book_name=book.name,
            chapter=chapter.number,
            reference=f"{book.name} {chapter.number}",
            verses=verses,
            sha="c" * 40,
        )

    async def search(self, query: str, options: SearchOptions) -> SearchPage:
        self.search_requests.append((query, options))
        return SearchPage(
            query=query,
            translation=options.translation,
            total=1,
            items=(
                SearchResult(
                    reference="John 3:16",
                    book_number=43,
                    book_name="John",
                    chapter=3,
                    verse=16,
                    text="For God so loved the world.",
                    terms=("loved",),
                ),
            ),
        )

    async def translation_exists(self, translation: str) -> bool:
        return translation in {"kjv", "aov"}


class MiniAppApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = _Service()
        self.preferences = _Preferences()
        self.limiter = _Limiter()
        self.clock = _Clock()
        self.sessions = MiniAppSessionStore(
            max_sessions=10,
            ttl_seconds=600,
            clock=self.clock,
        )
        self.launches = MiniAppLaunchStore(
            max_launches=10,
            ttl_seconds=300,
            clock=self.clock,
        )
        self.posted: list[tuple[object, tuple[ScriptureQuery, ...]]] = []
        self.cleaned_launches: list[object] = []
        self.warnings: list[tuple[int, int, str]] = []
        self.post_error: Exception | None = None
        self.post_started: asyncio.Event | None = None
        self.post_release: asyncio.Event | None = None

        async def post_scripture(
            launch: object,
            queries: tuple[ScriptureQuery, ...],
        ) -> tuple[int, ...]:
            self.posted.append((launch, queries))
            if self.post_started is not None:
                self.post_started.set()
            if self.post_release is not None:
                await self.post_release.wait()
            if self.post_error is not None:
                raise self.post_error
            return (101,)

        async def cleanup_launch(launch: object) -> None:
            self.cleaned_launches.append(launch)

        async def abuse_warning(user_id: int, chat_id: int, text: str) -> None:
            self.warnings.append((user_id, chat_id, text))

        self.api = MiniAppApi(
            service=self.service,
            preferences=self.preferences,
            limiter=self.limiter,
            sessions=self.sessions,
            launches=self.launches,
            validator=TelegramInitDataValidator(
                TOKEN,
                wall_clock=lambda: 1_700_000_000,
            ),
            public_url=PUBLIC_URL,
            post_scripture=post_scripture,
            cleanup_launch=cleanup_launch,
            abuse_warning=abuse_warning,
        )
        self.active_init_data = _init_data()

    async def test_navigation_has_fractional_cost_but_expensive_work_does_not(
        self,
    ) -> None:
        token = await self.exchange()
        self.assertEqual(self.limiter.details[-1][2:], (1.0, "192.0.2.1"))

        response = await self.api.handle(
            self.request("GET", "/getbible/api/v1/translations", token=token)
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.limiter.details[-1][2:], (0.25, "192.0.2.1"))

        response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/search",
                token=token,
                body={"query": "grace"},
            )
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.limiter.details[-1][2:], (1.0, "192.0.2.1"))

    async def test_unknown_routes_and_wrong_methods_are_rejected_before_auth(self) -> None:
        invalid = await self.api.handle(
            self.request("POST", "/getbible/api/v1/wp-admin", body={})
        )
        wrong_method = await self.api.handle(
            self.request("GET", "/getbible/api/v1/post")
        )

        self.assertEqual(invalid.status, 404)
        self.assertEqual(wrong_method.status, 405)
        self.assertEqual(wrong_method.headers["Allow"], "POST, OPTIONS")
        self.assertEqual(self.limiter.calls, [])

    async def test_atomic_browser_post_handles_multiple_verses_as_one_action(self) -> None:
        token = await self.exchange()
        calls_before_post = len(self.limiter.details)
        response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/post",
                token=token,
                body={
                    "idempotency_key": "abcdef0123456789",
                    "selection_ids": [
                        "gbd_kjv_043_0003_0001",
                        "gbd_kjv_043_0003_0002",
                        "gbd_kjv_043_0003_0016",
                    ],
                },
            )
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.limiter.details), calls_before_post + 1)
        self.assertEqual(self.limiter.details[-1][2], 1.0)
        self.assertEqual(
            self.posted[0][1],
            (ScriptureQuery("John 3:1;John 3:2;John 3:16", "kjv"),),
        )

    async def test_books_include_canonical_testament_metadata(self) -> None:
        token = await self.exchange()

        response = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/books?translation=kjv",
                token=token,
            )
        )

        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(
            payload["items"],
            [{"number": 43, "name": "John", "testament": "new"}],
        )

    async def test_miniapp_edge_bounds_catalogs_and_search_payloads(self) -> None:
        token = await self.exchange()
        original_chapters = self.service.chapters
        original_search = self.service.search

        async def oversized_chapters(
            translation: str,
            book: BookOption,
        ) -> tuple[ChapterOption, ...]:
            return (ChapterOption(3, tuple(range(1, 252))),)

        self.service.chapters = oversized_chapters  # type: ignore[method-assign]
        oversized = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/chapters?translation=kjv&book=43",
                token=token,
            )
        )
        self.assertEqual(oversized.status, 503)

        async def bounded_search(
            query: str,
            options: SearchOptions,
        ) -> SearchPage:
            return SearchPage(
                query=query,
                translation=options.translation,
                total=1,
                items=(
                    SearchResult(
                        reference="r" * 300,
                        book_number=43,
                        book_name="John",
                        chapter=3,
                        verse=501,
                        text="A bounded result.",
                        terms=tuple(
                            f"term-{index}-{'x' * 70}"
                            for index in range(64)
                        ),
                    ),
                ),
            )

        self.service.chapters = original_chapters  # type: ignore[method-assign]
        self.service.search = bounded_search  # type: ignore[method-assign]
        bounded = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/search",
                token=token,
                body={"query": "bounded"},
            )
        )
        self.assertEqual(bounded.status, 200)
        item = json.loads(bounded.body)["items"][0]
        self.assertEqual(item["verse"], 501)
        self.assertLessEqual(len(item["reference"]), 180)
        self.assertLessEqual(len(item["terms"]), 20)
        self.assertTrue(all(len(term) <= 80 for term in item["terms"]))

        async def invalid_book_search(
            query: str,
            options: SearchOptions,
        ) -> SearchPage:
            return SearchPage(
                query=query,
                translation=options.translation,
                total=1,
                items=(
                    SearchResult(
                        reference="Book 1:1",
                        book_number=201,
                        book_name="Book",
                        chapter=1,
                        verse=1,
                        text="Invalid edge book.",
                    ),
                ),
            )

        self.service.search = invalid_book_search  # type: ignore[method-assign]
        invalid = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/search",
                token=token,
                body={"query": "invalid"},
            )
        )
        self.assertEqual(invalid.status, 503)
        self.service.search = original_search  # type: ignore[method-assign]

    async def test_new_abuse_block_sends_one_private_warning(self) -> None:
        token = await self.exchange()
        self.limiter.rejection = RobotRateLimited(
            300,
            blocked=True,
            new_block=True,
            violation_count=6,
            scopes=("user", "client"),
            user_id=42,
            chat_id=42,
            client_key="192.0.2.1",
        )
        response = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/translations",
                token=token,
            )
        )
        self.assertEqual(response.status, 429)
        self.assertEqual(len(self.warnings), 1)
        self.assertEqual(self.warnings[0][:2], (42, 42))
        self.assertIn("paused", self.warnings[0][2])
        self.assertEqual(self.api.snapshot()["api_abuse_blocks"], 1)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        token: str | None = None,
        origin: str | None = ORIGIN,
        init_data: str | None = None,
    ) -> MiniAppHttpRequest:
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Telegram-Init-Data"] = (
                self.active_init_data if init_data is None else init_data
            )
        return MiniAppHttpRequest(
            method=method,
            target=path,
            headers=headers,
            body=json.dumps(body or {}).encode(),
            client_key="192.0.2.1",
        )

    async def exchange(
        self,
        *,
        launch_token: str | None = None,
        query_id: str = "query-id",
    ) -> str:
        self.active_init_data = _init_data(
            start_param=launch_token,
            query_id=query_id,
        )
        body = {"init_data": self.active_init_data}
        response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body=body,
            )
        )
        self.assertEqual(response.status, 201)
        return json.loads(response.body)["session_token"]

    async def test_session_mutation_errors_preserve_safe_domain_messages(
        self,
    ) -> None:
        token = await self.exchange()
        invalid = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/basket/items",
                token=token,
                body={"selection_id": "abcdefghijklmnop"},
            )
        )
        self.assertEqual(invalid.status, 400)
        self.assertEqual(
            json.loads(invalid.body),
            {
                "error": "invalid_request",
                "message": "Selection is invalid or expired.",
            },
        )

    async def test_unexpected_builtin_errors_are_generic_and_never_leak(
        self,
    ) -> None:
        token = await self.exchange()
        for error_type in (ValueError, OverflowError):
            private_text = f"private {error_type.__name__} implementation detail"
            with (
                self.subTest(error_type=error_type.__name__),
                patch.object(
                    self.sessions,
                    "basket",
                    side_effect=error_type(private_text),
                ),
            ):
                response = await self.api.handle(
                    self.request(
                        "GET",
                        "/getbible/api/v1/basket",
                        token=token,
                    )
                )
            self.assertEqual(response.status, 500)
            self.assertEqual(
                json.loads(response.body),
                {
                    "error": "internal_error",
                    "message": "The request could not be completed.",
                },
            )
            self.assertNotIn(private_text, response.body.decode())

        private_text = "private preference persistence invariant"
        with patch.object(
            self.preferences,
            "update_preferences",
            side_effect=ValueError(private_text),
        ):
            response = await self.api.handle(
                self.request(
                    "PUT",
                    "/getbible/api/v1/preferences",
                    token=token,
                    body={"translation": "aov"},
                )
            )
        self.assertEqual(response.status, 500)
        self.assertEqual(
            json.loads(response.body),
            {
                "error": "internal_error",
                "message": "The request could not be completed.",
            },
        )
        self.assertNotIn(private_text, response.body.decode())

    async def test_direct_browser_and_forged_telegram_requests_are_denied(self) -> None:
        missing_origin = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": _init_data()},
                origin=None,
            )
        )
        self.assertEqual(missing_origin.status, 403)

        forged = _init_data().replace("Grace", "Mallory")
        response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": forged},
            )
        )
        self.assertEqual(response.status, 401)

    async def test_command_launch_is_owner_bound_and_sets_initial_route(self) -> None:
        launch = self.launches.create_launch(
            user_id=42,
            target_chat_id=-100,
            message_thread_id=9,
            initial_route="search",
            initial_query="grace",
        )
        response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": _init_data(start_param=launch.token)},
            )
        )
        payload = json.loads(response.body)
        self.assertEqual(response.status, 201)
        self.assertEqual(payload["entrypoint"], {"route": "search", "query": "grace"})

        replay = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": _init_data(start_param=launch.token)},
            )
        )
        self.assertEqual(replay.status, 409)

    async def test_reopened_webview_recovers_the_active_owner_bound_session(
        self,
    ) -> None:
        launch = self.launches.create_launch(
            user_id=42,
            target_chat_id=42,
            initial_route="bible",
        )
        first_init_data = _init_data(
            start_param=launch.token,
            query_id="first-query",
        )
        first = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": first_init_data},
            )
        )
        first_payload = json.loads(first.body)
        self.assertEqual(first.status, 201)

        self.active_init_data = _init_data(
            start_param=launch.token,
            query_id="reopened-query",
        )
        reopened = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": self.active_init_data},
            )
        )
        reopened_payload = json.loads(reopened.body)
        self.assertEqual(reopened.status, 200)
        self.assertEqual(
            reopened_payload["session_token"],
            first_payload["session_token"],
        )
        self.assertEqual(
            reopened_payload["entrypoint"],
            {"route": "bible", "query": ""},
        )

        resumed = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/session",
                token=reopened_payload["session_token"],
                origin=None,
            )
        )
        self.assertEqual(resumed.status, 200)

    async def test_expired_session_cleans_stale_launch_rows_before_rejection(
        self,
    ) -> None:
        launch = self.launches.create_launch(
            user_id=42,
            target_chat_id=-100,
            initial_route="search",
            source_ephemeral_message_id=250,
            source_ephemeral_receiver_user_id=999,
        )
        self.launches.remember_prompt(
            launch,
            ephemeral_message_id=901,
        )
        await self.exchange(launch_token=launch.token)
        self.clock.value = 601
        expired_init_data = _init_data(
            start_param=launch.token,
            query_id="expired-reopen",
        )

        response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": expired_init_data},
            )
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(self.cleaned_launches, [launch])

    async def test_valid_init_data_cannot_be_exchanged_twice(self) -> None:
        raw = _init_data()
        first = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": raw},
            )
        )
        second = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": raw},
            )
        )
        self.assertEqual(first.status, 201)
        self.assertEqual(second.status, 409)

    async def test_invalid_launch_and_bootstrap_failure_do_not_burn_init_data(
        self,
    ) -> None:
        raw = _init_data()
        invalid_launch = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={
                    "init_data": raw,
                    "launch_token": "abcdefghijklmnop",
                },
            )
        )
        self.assertEqual(invalid_launch.status, 401)

        recovered = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": raw},
            )
        )
        self.assertEqual(recovered.status, 201)

        self.active_init_data = _init_data(user_id=43)
        self.service.fail_translations_once = True
        failed_bootstrap = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": self.active_init_data},
            )
        )
        self.assertEqual(failed_bootstrap.status, 503)
        retried_bootstrap = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": self.active_init_data},
            )
        )
        self.assertEqual(retried_bootstrap.status, 201)

    async def test_consumed_launch_is_restored_when_response_building_fails(
        self,
    ) -> None:
        launch = self.launches.create_launch(
            user_id=42,
            target_chat_id=-100,
            initial_route="search",
            initial_query="grace",
        )
        self.active_init_data = _init_data(start_param=launch.token)
        self.preferences.fail_preferences_once = True
        first = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": self.active_init_data},
            )
        )
        self.assertEqual(first.status, 500)

        second = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": self.active_init_data},
            )
        )
        self.assertEqual(second.status, 201)

    async def test_session_rejects_a_different_valid_init_data_for_same_user(self) -> None:
        token = await self.exchange()
        self.active_init_data = _init_data(user_id=42, start_param="different-launch")
        response = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/session",
                token=token,
                origin=None,
            )
        )
        self.assertEqual(response.status, 401)

    async def test_api_path_tracks_public_url_and_returns_security_headers(self) -> None:
        token = await self.exchange()
        wrong_path = await self.api.handle(self.request("GET", "/api/v1/translations", token=token))
        self.assertEqual(wrong_path.status, 404)

        response = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/translations",
                token=token,
            )
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], ORIGIN)

        without_origin = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/session",
                token=token,
                origin=None,
            )
        )
        self.assertEqual(without_origin.status, 200)

        foreign_origin = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/session",
                token=token,
                origin="https://attacker.example",
            )
        )
        self.assertEqual(foreign_origin.status, 403)

    async def test_search_selection_posts_only_server_resolved_scripture(self) -> None:
        launch = self.launches.create_launch(
            user_id=42,
            target_chat_id=-100,
            initial_route="search",
            initial_query="loved",
        )
        token = await self.exchange(launch_token=launch.token)
        search_response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/search",
                token=token,
                body={"query": "loved"},
            )
        )
        search_payload = json.loads(search_response.body)

        selection_id = search_payload["items"][0]["selection_id"]
        basket_response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/basket/items",
                token=token,
                body={"selection_id": selection_id},
            )
        )
        basket_payload = json.loads(basket_response.body)
        self.assertEqual(basket_response.status, 200)
        self.assertEqual(
            basket_payload["items"][0]["text"],
            "For God so loved the world.",
        )

        post_response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/post",
                token=token,
                body={"idempotency_key": "abcdef0123456789"},
            )
        )
        self.assertEqual(post_response.status, 200)
        self.assertEqual(len(self.posted), 1)
        posted_launch, posted_queries = self.posted[0]
        self.assertEqual(posted_launch.target_chat_id, -100)
        self.assertEqual(posted_queries, (ScriptureQuery("John 3:16", "kjv"),))

        replay = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/post",
                token=token,
                body={"idempotency_key": "abcdef0123456789"},
            )
        )
        self.assertEqual(replay.status, 200)
        self.assertTrue(json.loads(replay.body)["idempotent_replay"])

    async def test_failed_post_attempt_cannot_be_retried_under_a_new_key(
        self,
    ) -> None:
        token = await self.exchange()
        search = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/search",
                token=token,
                body={"query": "loved"},
            )
        )
        selection_id = json.loads(search.body)["items"][0]["selection_id"]
        await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/basket/items",
                token=token,
                body={"selection_id": selection_id},
            )
        )

        self.post_error = RuntimeError("ambiguous Telegram failure")
        first = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/post",
                token=token,
                body={"idempotency_key": "abcdef0123456789"},
            )
        )
        self.assertEqual(first.status, 500)
        self.post_error = None

        retry = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/post",
                token=token,
                body={"idempotency_key": "dead-beef-dead-beef"},
            )
        )
        self.assertEqual(retry.status, 409)
        self.assertEqual(json.loads(retry.body)["error"], "post_outcome_locked")
        self.assertEqual(len(self.posted), 1)

    async def test_post_serializes_concurrent_basket_add_and_remove(self) -> None:
        token = await self.exchange()
        scripture = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/scripture",
                token=token,
                body={
                    "translation": "kjv",
                    "book": 43,
                    "chapter": 3,
                    "verse": 1,
                },
            )
        )
        items = json.loads(scripture.body)["items"]
        first_id = items[0]["selection_id"]
        second_id = items[1]["selection_id"]
        await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/basket/items",
                token=token,
                body={"selection_id": first_id},
            )
        )
        self.post_started = asyncio.Event()
        self.post_release = asyncio.Event()
        posting = asyncio.create_task(
            self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/post",
                    token=token,
                    body={"idempotency_key": "abcdef0123456789"},
                )
            )
        )
        await self.post_started.wait()
        adding = asyncio.create_task(
            self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/basket/items",
                    token=token,
                    body={"selection_id": second_id},
                )
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(adding.done())
        self.post_release.set()
        posted, added = await asyncio.gather(posting, adding)
        self.assertEqual(posted.status, 200)
        self.assertEqual(added.status, 200)
        self.assertEqual(
            [item["selection_id"] for item in json.loads(added.body)["items"]],
            [second_id],
        )

        await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/basket/items",
                token=token,
                body={"selection_id": first_id},
            )
        )
        self.post_started = asyncio.Event()
        self.post_release = asyncio.Event()
        posting = asyncio.create_task(
            self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/post",
                    token=token,
                    body={"idempotency_key": "dead-beef-dead-beef"},
                )
            )
        )
        await self.post_started.wait()
        removing = asyncio.create_task(
            self.api.handle(
                self.request(
                    "DELETE",
                    f"/getbible/api/v1/basket/items/{second_id}",
                    token=token,
                )
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(removing.done())
        self.post_release.set()
        posted, removed = await asyncio.gather(posting, removing)
        self.assertEqual(posted.status, 200)
        self.assertEqual(removed.status, 200)
        self.assertEqual(json.loads(removed.body)["items"], [])

    async def test_process_eviction_never_interrupts_an_active_post(self) -> None:
        first_token = await self.exchange(query_id="first")
        first_init_data = self.active_init_data
        scripture = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/scripture",
                token=first_token,
                init_data=first_init_data,
                body={
                    "translation": "kjv",
                    "book": 43,
                    "chapter": 3,
                    "verse": 1,
                },
            )
        )
        selection_id = json.loads(scripture.body)["items"][0]["selection_id"]
        await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/basket/items",
                token=first_token,
                init_data=first_init_data,
                body={"selection_id": selection_id},
            )
        )
        first_session = self.sessions.get(first_token, touch=False)
        self.assertIsNotNone(first_session)
        self.post_started = asyncio.Event()
        self.post_release = asyncio.Event()
        posting = asyncio.create_task(
            self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/post",
                    token=first_token,
                    init_data=first_init_data,
                    body={"idempotency_key": "abcdef0123456789"},
                )
            )
        )
        await self.post_started.wait()

        second_token = await self.exchange(query_id="second")
        process_cap = int(first_session.retained_selection_bytes) + 1
        with patch.object(
            miniapp_sessions,
            "MAX_MINIAPP_PROCESS_RETAINED_BYTES",
            process_cap,
        ):
            second_scripture = await self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/scripture",
                    token=second_token,
                    body={
                        "translation": "kjv",
                        "book": 43,
                        "chapter": 3,
                        "verse": 1,
                    },
                )
            )

        self.assertEqual(second_scripture.status, 401)
        self.assertIs(self.sessions.get(first_token, touch=False), first_session)
        self.assertIsNone(self.sessions.get(second_token, touch=False))
        revoking = asyncio.create_task(
            self.api.handle(
                self.request(
                    "DELETE",
                    "/getbible/api/v1/session",
                    token=first_token,
                    init_data=first_init_data,
                )
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(revoking.done())
        self.post_release.set()
        posted, revoked = await asyncio.gather(posting, revoking)
        self.assertEqual(posted.status, 200)
        self.assertEqual(revoked.status, 204)
        self.assertEqual(len(self.posted), 1)
        self.assertIsNone(self.sessions.get(first_token, touch=False))

    async def test_delayed_content_returns_unauthorized_after_session_eviction(
        self,
    ) -> None:
        token = await self.exchange()
        started = asyncio.Event()
        release = asyncio.Event()
        original_chapter = self.service.chapter

        async def controlled_chapter(
            translation: str,
            book: BookOption,
            chapter: ChapterOption,
        ) -> ChapterContent:
            started.set()
            await release.wait()
            return await original_chapter(translation, book, chapter)

        self.service.chapter = controlled_chapter  # type: ignore[method-assign]
        reading = asyncio.create_task(
            self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/scripture",
                    token=token,
                    body={
                        "translation": "kjv",
                        "book": 43,
                        "chapter": 3,
                        "verse": 1,
                    },
                )
            )
        )
        await started.wait()
        self.sessions.revoke(token)
        release.set()
        response = await reading

        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(response.body)["error"], "unauthorized")

    async def test_delayed_search_returns_unauthorized_after_session_eviction(
        self,
    ) -> None:
        token = await self.exchange()
        started = asyncio.Event()
        release = asyncio.Event()
        original_search = self.service.search

        async def controlled_search(
            query: str,
            options: SearchOptions,
        ) -> SearchPage:
            started.set()
            await release.wait()
            return await original_search(query, options)

        self.service.search = controlled_search  # type: ignore[method-assign]
        searching = asyncio.create_task(
            self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/search",
                    token=token,
                    body={"query": "loved"},
                )
            )
        )
        await started.wait()
        self.sessions.revoke(token)
        release.set()
        response = await searching

        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(response.body)["error"], "unauthorized")

    async def test_full_psalm_119_uses_one_main_api_chapter_read(self) -> None:
        self.service.long_chapter = True
        token = await self.exchange()

        response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/scripture",
                token=token,
                body={"translation": "kjv", "book": 19, "chapter": 119},
            )
        )

        payload = json.loads(response.body)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(payload["items"]), 176)
        self.assertEqual(payload["items"][0]["reference"], "Psalms 119:1")
        self.assertEqual(payload["items"][-1]["reference"], "Psalms 119:176")
        self.assertEqual(
            self.service.chapter_requests,
            [("kjv", 19, 119)],
        )
        self.assertEqual(self.service.selected, [])
        self.assertEqual(payload["sha"], "c" * 40)
        self.assertNotIn(42, self.preferences.reader_locations)

    async def test_content_reads_do_not_mutate_explicit_translation(self) -> None:
        token = await self.exchange()
        selected = await self.api.handle(
            self.request(
                "PUT",
                "/getbible/api/v1/preferences",
                token=token,
                body={"translation": "aov"},
            )
        )
        self.assertEqual(selected.status, 200)

        scripture = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/scripture",
                token=token,
                body={
                    "translation": "kjv",
                    "book": 43,
                    "chapter": 3,
                    "verse": 16,
                },
            )
        )
        search = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/search",
                token=token,
                body={
                    "query": "loved",
                    "options": {
                        "translation": "kjv",
                        "words": "all",
                        "match": "whole_word",
                        "scope": "bible",
                        "case_sensitive": False,
                        "diacritics": "exact",
                        "sort": "canonical",
                        "books": [],
                        "exclude": [],
                        "proximity": None,
                    },
                },
            )
        )

        self.assertEqual(scripture.status, 200)
        self.assertEqual(search.status, 200)
        self.assertEqual(self.preferences.translation_for(42), "aov")
        self.assertNotIn(42, self.preferences.reader_locations)

    async def test_search_accepts_the_librarian_one_diacritics_spellings(self) -> None:
        """A Mini App build cached before the upgrade must keep working.

        The client is a browser page, so a device can go on sending the 1.x
        vocabulary long after the robot is upgraded. Rejecting it would break
        search for that reader until the page happened to reload.
        """
        token = await self.exchange()
        for sent, expected in (
            ("insensitive", "fold"),
            ("sensitive", "exact"),
            ("fold", "fold"),
            ("exact", "exact"),
        ):
            with self.subTest(sent=sent):
                response = await self.api.handle(
                    self.request(
                        "POST",
                        "/getbible/api/v1/search",
                        token=token,
                        body={
                            "query": "loved",
                            "options": {"diacritics": sent},
                        },
                    )
                )

                self.assertEqual(response.status, 200)
                self.assertEqual(
                    self.service.search_requests[-1][1].diacritics,
                    expected,
                )

        rejected = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/search",
                token=token,
                body={"query": "loved", "options": {"diacritics": "folded"}},
            )
        )
        self.assertEqual(rejected.status, 400)

    async def test_search_forwards_the_requested_match_mode_unchanged(self) -> None:
        """The API must not second-guess the writing system on Librarian's behalf."""
        token = await self.exchange()
        queries = (
            "神",  # Han
            "イエス",  # Katakana
            "예수",  # Hangul
            "พระ",  # Thai
            "ພຣະ",  # Lao
            "ព្រះ",  # Khmer
            "ယေရှု",  # Myanmar
            "المسيح",  # Arabic
            "משיח",  # Hebrew
            "यीशु",  # Devanagari
            "Jesus",  # Latin
            "Jesus 耶稣",  # mixed Latin and Han
        )

        for requested in ("whole_word", "substring"):
            for query in queries:
                with self.subTest(query=query, match=requested):
                    response = await self.api.handle(
                        self.request(
                            "POST",
                            "/getbible/api/v1/search",
                            token=token,
                            body={
                                "query": query,
                                "options": {"match": requested},
                            },
                        )
                    )

                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        self.service.search_requests[-1][1].match,
                        requested,
                    )

    async def test_preferences_accept_only_non_content_allow_list(self) -> None:
        token = await self.exchange()
        response = await self.api.handle(
            self.request(
                "PUT",
                "/getbible/api/v1/preferences",
                token=token,
                body={
                    "translation": "kjv",
                    "search_defaults": {
                        "words": "all",
                        "match": "substring",
                        "scope": "new_testament",
                        "case_sensitive": False,
                        "diacritics": "fold",
                        "sort": "canonical",
                    },
                    "reader_location": {
                        "translation": "kjv",
                        "book": 43,
                        "chapter": 3,
                        "verse": 16,
                    },
                },
            )
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.preferences.reader_locations[42],
            ReaderLocation("kjv", 43, 3, 16),
        )

        cleared = await self.api.handle(
            self.request(
                "PUT",
                "/getbible/api/v1/preferences",
                token=token,
                body={"translation": "aov", "reader_location": None},
            )
        )
        self.assertEqual(cleared.status, 200)
        self.assertEqual(self.preferences.values[42], "aov")
        self.assertNotIn(42, self.preferences.reader_locations)

        before = self.preferences.preferences_for(42)
        invalid_combination = await self.api.handle(
            self.request(
                "PUT",
                "/getbible/api/v1/preferences",
                token=token,
                body={
                    "translation": "kjv",
                    "reader_location": {
                        "translation": "aov",
                        "book": 43,
                        "chapter": 3,
                        "verse": 16,
                    },
                },
            )
        )
        self.assertEqual(invalid_combination.status, 400)
        self.assertEqual(self.preferences.preferences_for(42), before)

        rejected = await self.api.handle(
            self.request(
                "PUT",
                "/getbible/api/v1/preferences",
                token=token,
                body={"query": "must never persist"},
            )
        )
        self.assertEqual(rejected.status, 400)

        content_rejected = await self.api.handle(
            self.request(
                "PUT",
                "/getbible/api/v1/preferences",
                token=token,
                body={
                    "reader_location": {
                        "translation": "kjv",
                        "book": 43,
                        "chapter": 3,
                        "verse": 16,
                        "text": "must never persist",
                    }
                },
            )
        )
        self.assertEqual(content_rejected.status, 400)
        self.assertEqual(self.preferences.preferences_for(42), before)

    async def test_translation_and_reader_location_change_atomically(self) -> None:
        token = await self.exchange()
        self.service.chapter_verses["aov"] = (1, 2)

        response = await self.api.handle(
            self.request(
                "PUT",
                "/getbible/api/v1/preferences",
                token=token,
                body={
                    "translation": "aov",
                    "reader_location": {
                        "translation": "aov",
                        "book": 43,
                        "chapter": 3,
                        "verse": 16,
                    },
                },
            )
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(self.preferences.translation_for(42), "aov")
        self.assertEqual(
            self.preferences.reader_locations[42],
            ReaderLocation("aov", 43, 3, 2),
        )
        payload = json.loads(response.body)["preferences"]
        self.assertEqual(payload["reader_location"]["verse"], 2)

        repeated = await self.api.handle(
            self.request(
                "PUT",
                "/getbible/api/v1/preferences",
                token=token,
                body={
                    "translation": "aov",
                    "reader_location": {
                        "translation": "aov",
                        "book": 43,
                        "chapter": 3,
                        "verse": 16,
                    },
                },
            )
        )
        self.assertEqual(repeated.status, 200)
        repeated_payload = json.loads(repeated.body)["preferences"]
        self.assertEqual(repeated_payload["reader_location"]["verse"], 2)
        self.assertEqual(
            self.preferences.reader_locations[42],
            ReaderLocation("aov", 43, 3, 2),
        )

    async def test_preferences_serialize_across_sessions_for_one_user(self) -> None:
        first_launch = self.launches.create_launch(user_id=42, target_chat_id=42)
        first_token = await self.exchange(
            launch_token=first_launch.token,
            query_id="first-session",
        )
        first_init_data = self.active_init_data
        second_launch = self.launches.create_launch(user_id=42, target_chat_id=42)
        second_token = await self.exchange(
            launch_token=second_launch.token,
            query_id="second-session",
        )
        second_init_data = self.active_init_data
        started = asyncio.Event()
        release = asyncio.Event()
        original_chapters = self.service.chapters

        async def controlled_chapters(
            translation: str,
            book: BookOption,
        ) -> tuple[ChapterOption, ...]:
            if translation == "aov":
                started.set()
                await release.wait()
            return await original_chapters(translation, book)

        self.service.chapters = controlled_chapters  # type: ignore[method-assign]
        older = asyncio.create_task(
            self.api.handle(
                self.request(
                    "PUT",
                    "/getbible/api/v1/preferences",
                    token=first_token,
                    init_data=first_init_data,
                    body={
                        "translation": "aov",
                        "reader_location": {
                            "translation": "aov",
                            "book": 43,
                            "chapter": 3,
                            "verse": 16,
                        },
                    },
                )
            )
        )
        await started.wait()
        newer = asyncio.create_task(
            self.api.handle(
                self.request(
                    "PUT",
                    "/getbible/api/v1/preferences",
                    token=second_token,
                    init_data=second_init_data,
                    body={
                        "translation": "kjv",
                        "reader_location": {
                            "translation": "kjv",
                            "book": 43,
                            "chapter": 3,
                            "verse": 16,
                        },
                    },
                )
            )
        )
        await asyncio.sleep(0)
        release.set()
        older_response, newer_response = await asyncio.gather(older, newer)

        self.assertEqual(older_response.status, 200)
        self.assertEqual(newer_response.status, 200)
        self.assertEqual(self.preferences.translation_for(42), "kjv")
        self.assertEqual(
            self.preferences.reader_locations[42],
            ReaderLocation("kjv", 43, 3, 16),
        )


if __name__ == "__main__":
    unittest.main()
