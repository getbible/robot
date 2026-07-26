import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode

from modules.catalog import BookOption, ChapterOption, TranslationOption
from modules.interactions import SearchOptions, SearchResult
from modules.miniapp_api import MiniAppApi, MiniAppHttpRequest
from modules.miniapp_auth import TelegramInitDataValidator
from modules.miniapp_sessions import MiniAppLaunchStore, MiniAppSessionStore
from modules.preferences import SearchDefaults, UserPreferences
from modules.service import ScriptureQuery, SearchPage

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
PUBLIC_URL = "https://robot.example/getbible"
ORIGIN = "https://robot.example"


def _init_data(user_id: int = 42, *, start_param: str | None = None) -> str:
    fields = {
        "auth_date": "1700000000",
        "query_id": "query-id",
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

    def translation_for(self, user_id: int) -> str:
        return self.values.get(user_id, "kjv")

    def set_translation(self, user_id: int, translation: str) -> None:
        self.values[user_id] = translation

    def preferences_for(self, user_id: int) -> UserPreferences:
        return UserPreferences(
            self.translation_for(user_id),
            SearchDefaults(),
        )

    def set_search_defaults(
        self,
        user_id: int,
        defaults: SearchDefaults,
    ) -> None:
        return None


class _Limiter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def acquire(self, *, user_id: int, chat_id: int, cost: float = 1.0) -> None:
        self.calls.append((user_id, chat_id))


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
        self.long_chapter = False

    async def translations(self) -> tuple[TranslationOption, ...]:
        return (TranslationOption("kjv", "King James Version", "English"),)

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
        return (ChapterOption(3, (1, 2, 16)),)

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

    async def search(self, query: str, options: SearchOptions) -> SearchPage:
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
        return translation == "kjv"


class MiniAppApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = _Service()
        self.preferences = _Preferences()
        self.limiter = _Limiter()
        self.sessions = MiniAppSessionStore(max_sessions=10, ttl_seconds=600)
        self.launches = MiniAppLaunchStore(max_launches=10, ttl_seconds=300)
        self.posted: list[tuple[object, tuple[ScriptureQuery, ...]]] = []

        async def post_scripture(
            launch: object,
            queries: tuple[ScriptureQuery, ...],
        ) -> tuple[int, ...]:
            self.posted.append((launch, queries))
            return (101,)

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
        )
        self.active_init_data = _init_data()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        token: str | None = None,
        origin: str | None = ORIGIN,
    ) -> MiniAppHttpRequest:
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Telegram-Init-Data"] = self.active_init_data
        return MiniAppHttpRequest(
            method=method,
            target=path,
            headers=headers,
            body=json.dumps(body or {}).encode(),
            client_key="192.0.2.1",
        )

    async def exchange(self, *, launch_token: str | None = None) -> str:
        self.active_init_data = _init_data(start_param=launch_token)
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

    async def test_full_psalm_119_is_loaded_in_bounded_chunks(self) -> None:
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
        self.assertEqual(len(self.service.selected), 4)
        self.assertTrue(
            all(query.references.startswith("Psalms 119:") for query in self.service.selected)
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
                        "diacritics": "insensitive",
                        "sort": "canonical",
                    },
                },
            )
        )
        self.assertEqual(response.status, 200)

        rejected = await self.api.handle(
            self.request(
                "PUT",
                "/getbible/api/v1/preferences",
                token=token,
                body={"query": "must never persist"},
            )
        )
        self.assertEqual(rejected.status, 400)


if __name__ == "__main__":
    unittest.main()
