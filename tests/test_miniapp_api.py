import asyncio
import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from modules import miniapp_sessions
from modules.bookmark_backup import BookmarkBackupDocument, BookmarkRestoreFile
from modules.catalog import (
    BookOption,
    ChapterContent,
    ChapterOption,
    ChapterVerse,
    TranslationOption,
)
from modules.contributions import ContributionStore
from modules.errors import RobotRateLimited, ScriptureUnavailable
from modules.interactions import SearchOptions, SearchResult
from modules.miniapp_api import (
    MiniAppApi,
    MiniAppHttpRequest,
    _contribution_receipt_id,
)
from modules.miniapp_auth import TelegramInitDataValidator
from modules.miniapp_sessions import MiniAppLaunchStore, MiniAppSessionStore
from modules.preferences import ReaderLocation, SearchDefaults, UserPreferences
from modules.service import ScriptureQuery, SearchPage

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
PUBLIC_URL = "https://robot.example/getbible"
ORIGIN = "https://robot.example"


def _bookmark_backup() -> dict[str, object]:
    return {
        "format": "getbible-life-markings",
        "version": 2,
        "exportedAt": "2026-08-20T10:00:00.000Z",
        "colors": [
            {"id": "grace", "name": "Grace", "value": "#bbf7d0"},
        ],
        "markings": [
            {
                "id": "bookmark_1",
                "passage": {"translation": "kjv", "book": 43, "chapter": 3},
                "verse": 16,
                "start": None,
                "end": None,
                "quote": "For God so loved the world.",
                "reference": "John 3:16",
                "colorId": "grace",
                "createdAt": 1_777_000_000_000,
            }
        ],
        "notes": [],
    }


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
            search_timeout=150.0,
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
        self.contributions = ContributionStore(path=None)
        self.posted: list[tuple[object, tuple[ScriptureQuery, ...]]] = []
        self.cleaned_launches: list[object] = []
        self.warnings: list[tuple[int, int, str]] = []
        self.post_error: Exception | None = None
        self.post_started: asyncio.Event | None = None
        self.post_release: asyncio.Event | None = None
        self.bookmark_backup_started: asyncio.Event | None = None
        self.bookmark_backup_release: asyncio.Event | None = None
        self.bookmark_backup_error: Exception | None = None
        self.bookmark_backups: list[tuple[int, BookmarkBackupDocument]] = []
        self.loaded_restore_files: list[BookmarkRestoreFile] = []
        self.restore_payload = json.dumps(
            _bookmark_backup(),
            separators=(",", ":"),
        ).encode()

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

        async def send_bookmark_backup(
            user_id: int,
            document: BookmarkBackupDocument,
        ) -> int:
            self.bookmark_backups.append((user_id, document))
            if self.bookmark_backup_started is not None:
                self.bookmark_backup_started.set()
            if self.bookmark_backup_release is not None:
                await self.bookmark_backup_release.wait()
            if self.bookmark_backup_error is not None:
                raise self.bookmark_backup_error
            return 701

        async def load_bookmark_backup(restore: BookmarkRestoreFile) -> bytes:
            self.loaded_restore_files.append(restore)
            return self.restore_payload

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
            send_bookmark_backup=send_bookmark_backup,
            load_bookmark_backup=load_bookmark_backup,
            cleanup_launch=cleanup_launch,
            abuse_warning=abuse_warning,
            contributions=self.contributions,
        )
        self.active_init_data = _init_data()

    async def asyncTearDown(self) -> None:
        self.contributions.close()

    async def test_navigation_has_fractional_cost_but_expensive_work_does_not(
        self,
    ) -> None:
        token = await self.exchange()
        self.assertEqual(self.limiter.details[-1][2:], (1.0, "192.0.2.1"))

        response = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/translations",
                token=token,
                include_init_data=False,
            )
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

    async def test_bookmark_backup_is_private_bounded_and_idempotent(self) -> None:
        token = await self.exchange()
        body = {
            "idempotency_key": "abcdef0123456789",
            "backup": _bookmark_backup(),
        }

        first = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/bookmarks/backup",
                token=token,
                body=body,
            )
        )
        repeated = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/bookmarks/backup",
                token=token,
                body=body,
            )
        )

        self.assertEqual(first.status, 200)
        self.assertEqual(repeated.status, 200)
        self.assertFalse(json.loads(first.body)["idempotent_replay"])
        self.assertTrue(json.loads(repeated.body)["idempotent_replay"])
        self.assertEqual(len(self.bookmark_backups), 1)
        user_id, document = self.bookmark_backups[0]
        self.assertEqual(user_id, 42)
        self.assertEqual(document.bookmark_count, 1)
        self.assertNotIn(b"user_id", document.payload)
        self.assertEqual(self.limiter.details[-1][2], 1.0)

        changed = _bookmark_backup()
        changed["markings"] = []
        conflict = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/bookmarks/backup",
                token=token,
                body={**body, "backup": changed},
            )
        )
        self.assertEqual(conflict.status, 400)
        self.assertEqual(json.loads(conflict.body)["error"], "invalid_request")

    async def test_restore_payload_requires_owner_launch_and_explicit_ack(self) -> None:
        restore = BookmarkRestoreFile.validated(
            file_id="telegram-file-id",
            file_unique_id="telegram-unique-id",
            file_name="getbible-bookmarks.json",
            file_size=len(self.restore_payload),
        )
        launch = self.launches.create_launch(
            user_id=42,
            target_chat_id=42,
            initial_route="bookmarks",
            bookmark_restore=restore,
        )
        self.active_init_data = _init_data(start_param=launch.token)
        session_response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={
                    "init_data": self.active_init_data,
                    "launch_token": launch.token,
                },
            )
        )
        self.assertEqual(session_response.status, 201)
        session_payload = json.loads(session_response.body)
        self.assertEqual(session_payload["entrypoint"]["route"], "bookmarks")
        self.assertTrue(
            session_payload["entrypoint"]["bookmark_restore_available"]
        )
        token = session_payload["session_token"]

        first = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/bookmarks/restore",
                token=token,
            )
        )
        repeated = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/bookmarks/restore",
                token=token,
            )
        )
        self.assertEqual(first.status, 200)
        self.assertEqual(repeated.status, 200)
        self.assertEqual(json.loads(first.body)["backup"], _bookmark_backup())
        self.assertEqual(len(self.loaded_restore_files), 2)

        acknowledged = await self.api.handle(
            self.request(
                "DELETE",
                "/getbible/api/v1/bookmarks/restore",
                token=token,
            )
        )
        missing = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/bookmarks/restore",
                token=token,
            )
        )
        self.assertEqual(acknowledged.status, 204)
        self.assertEqual(missing.status, 404)
        self.assertEqual(
            json.loads(missing.body)["error"],
            "bookmark_restore_not_found",
        )

    async def test_ambiguous_bookmark_delivery_cannot_be_retried(self) -> None:
        token = await self.exchange()
        body = {
            "idempotency_key": "abcdef0123456789",
            "backup": _bookmark_backup(),
        }
        self.bookmark_backup_error = RuntimeError("ambiguous Telegram failure")

        first = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/bookmarks/backup",
                token=token,
                body=body,
            )
        )
        self.bookmark_backup_error = None
        retry = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/bookmarks/backup",
                token=token,
                body={**body, "idempotency_key": "dead-beef-dead-beef"},
            )
        )

        self.assertEqual(first.status, 500)
        self.assertEqual(retry.status, 409)
        self.assertEqual(
            json.loads(retry.body)["error"],
            "bookmark_backup_outcome_locked",
        )
        self.assertEqual(len(self.bookmark_backups), 1)

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

    async def test_contributor_status_is_read_only_and_disclosure_rides_the_push(
        self,
    ) -> None:
        self.contributions.submit_application(42, first_name="Old name")
        self.contributions.decide_application(42, "approved", actor="admin")
        token = await self.exchange()

        resumed = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/status",
                token=token,
            )
        )
        self.assertEqual(resumed.status, 200)
        self.assertEqual(
            json.loads(resumed.body),
            {
                "enabled": True,
                "state": "approved",
                "can_contribute": True,
                "disclosure_required": True,
            },
        )
        detailed = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/status?details=1",
                token=token,
            )
        )
        self.assertEqual(detailed.status, 200)
        self.assertEqual(json.loads(detailed.body)["topics"], [])
        self.assertEqual(json.loads(detailed.body)["summary"]["events"]["pending"], 0)
        invalid_query = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/status?details=0",
                token=token,
            )
        )
        self.assertEqual(invalid_query.status, 400)
        # Signed display metadata may be retained for private audit display,
        # but only the numeric ID controls this approved record.
        self.assertEqual(self.contributions.application_for(42).first_name, "Grace")

        # HTTPS no longer mutates disclosure state: the acknowledgement rides
        # the pushed envelope, so PATCH is a method error, not a mutation.
        patched = await self.api.handle(
            self.request(
                "PATCH",
                "/getbible/api/v1/contributions/status?details=1",
                token=token,
                body={"disclosure_acknowledged": True},
            )
        )
        self.assertEqual(patched.status, 405)
        self.assertEqual(patched.headers["Allow"], "GET, OPTIONS")

        # The push intake commits the envelope's acknowledgement server-side.
        self.contributions.synchronize_snapshot(
            42,
            sync_id="sync.disclosure.0001",
            client_id="browser.installation.0001",
            snapshot={"topics": [], "assignments": []},
            operations=[],
            disclosure_acknowledged=True,
        )
        acknowledged = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/status?details=1",
                token=token,
            )
        )
        self.assertEqual(acknowledged.status, 200)
        self.assertFalse(json.loads(acknowledged.body)["disclosure_required"])
        self.assertIn("summary", json.loads(acknowledged.body))

        result = self.contributions.record_events(
            42,
            [
                {
                    "client_event_id": "topic.grace.status",
                    "type": "topic_upsert",
                    "topic": {
                        "local_topic_id": "private.grace",
                        "name": "Grace",
                        "color": "#bbf7d0",
                    },
                }
            ],
        )
        self.contributions.set_topic_mapping(
            42,
            "private.grace",
            "grace",
            state="mapped",
            actor="admin",
            canonical_definition={
                "id": "grace",
                "name": "Grace",
                "color": "#bbf7d0",
                "aliases": [],
            },
        )
        event_id = result.event_ids["topic.grace.status"]
        self.contributions.decide_event(event_id, "approved", actor="admin")
        self.contributions.publish_approved_events_atomically(
            {
                "schema_version": 1,
                "topics": [
                    {
                        "id": "grace",
                        "name": "Grace",
                        "color": "#bbf7d0",
                        "aliases": [],
                    }
                ],
                "associations": {
                    "add": [
                        {
                            "topic_id": "grace",
                            "book": 43,
                            "chapter": 3,
                            "verse": 16,
                        }
                    ],
                    "remove": [],
                },
            },
            [event_id],
            actor="admin",
        )
        reconciled = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/status?details=1",
                token=token,
            )
        )
        reconciliation = json.loads(reconciled.body)
        self.assertEqual(
            reconciliation["topics"][0],
            {
                "local_topic_id": "private.grace",
                "state": "mapped",
                "published": True,
                "canonical_topic_id": "grace",
                "canonical_topic": {
                    "id": "grace",
                    "name": "Grace",
                    "color": "#bbf7d0",
                    "aliases": [],
                },
            },
        )
        self.assertNotIn("contributor_id", reconciled.body.decode())

    async def test_no_contribution_capability_token_is_ever_issued(self) -> None:
        """PUSH rides Telegram sendData now: HTTPS never mints a second secret."""
        self.contributions.submit_application(42, first_name="Grace")
        self.active_init_data = _init_data()
        bootstrap = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": self.active_init_data},
            )
        )
        self.assertEqual(bootstrap.status, 201)
        self.assertNotIn("X-Contribution-Token", bootstrap.headers)
        self.assertNotIn("gbc_", bootstrap.body.decode())
        session_token = json.loads(bootstrap.body)["session_token"]

        self.contributions.decide_application(42, "approved", actor="admin")
        for method, path in (
            ("GET", "/getbible/api/v1/contributions/status"),
            ("GET", "/getbible/api/v1/session"),
        ):
            with self.subTest(path=path):
                approved = await self.api.handle(
                    self.request(
                        method,
                        path,
                        token=session_token,
                        include_init_data=False,
                    )
                )
                self.assertEqual(approved.status, 200)
                self.assertNotIn("X-Contribution-Token", approved.headers)
                self.assertNotIn("gbc_", approved.body.decode())

        session = self.sessions.get(session_token, touch=False)
        self.assertIsNotNone(session)
        self.assertFalse(hasattr(session, "contribution_capability_token"))

    async def test_receipt_confirms_a_committed_push_and_stays_stable_on_replay(
        self,
    ) -> None:
        self.contributions.submit_application(42, first_name="Grace")
        self.contributions.decide_application(42, "approved", actor="admin")
        token = await self.exchange()

        unknown = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/receipt?sync_id=sync.snapshot.0001",
                token=token,
            )
        )
        self.assertEqual(unknown.status, 200)
        self.assertEqual(json.loads(unknown.body), {"found": False, "receipt": None})

        # The push transport (Telegram web_app_data) commits through the
        # unchanged atomic store entry point; HTTPS only reads the receipt.
        commit = dict(
            sync_id="sync.snapshot.0001",
            client_id="browser.installation.0001",
            snapshot={
                "topics": [
                    {
                        "id": "local.grace",
                        "name": "Grace",
                        "color": "#bbf7d0",
                    }
                ],
                "assignments": [
                    {
                        "topic_id": "local.grace",
                        "book": 43,
                        "chapter": 3,
                        "verse": 16,
                    }
                ],
            },
            operations=[
                {
                    "client_event_id": "compat.topic.grace.v1",
                    "type": "topic_upsert",
                    "topic": {
                        "local_topic_id": "local.grace",
                        "name": "Grace",
                        "color": "#bbf7d0",
                    },
                }
            ],
            disclosure_acknowledged=True,
        )
        result = self.contributions.synchronize_snapshot(42, **commit)
        self.assertFalse(result.replayed_sync)

        confirmed = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/receipt?sync_id=sync.snapshot.0001",
                token=token,
            )
        )
        self.assertEqual(confirmed.status, 200)
        payload = json.loads(confirmed.body)
        self.assertTrue(payload["found"])
        receipt = payload["receipt"]
        self.assertEqual(
            set(receipt),
            {"sync_id", "accepted", "replayed", "snapshot_digest", "event_ids"},
        )
        self.assertEqual(receipt["sync_id"], "sync.snapshot.0001")
        self.assertEqual(receipt["accepted"], 3)
        self.assertEqual(receipt["replayed"], 0)
        self.assertEqual(receipt["snapshot_digest"], result.snapshot_digest)
        # Exactly the committed client_event_ids appear: the two derived
        # snapshot-diff events plus the explicit compatibility operation.
        self.assertEqual(set(receipt["event_ids"]), set(result.event_ids))
        self.assertIn("compat.topic.grace.v1", receipt["event_ids"])
        self.assertEqual(self.limiter.details[-1][2], 0.25)

        # A replayed push (same sync_id, same data) changes nothing the
        # browser can observe through the receipt.
        replay = self.contributions.synchronize_snapshot(42, **commit)
        self.assertTrue(replay.replayed_sync)
        reread = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/receipt?sync_id=sync.snapshot.0001",
                token=token,
            )
        )
        self.assertEqual(reread.status, 200)
        self.assertEqual(json.loads(reread.body), payload)

    async def test_receipt_requires_a_session_and_exactly_one_valid_sync_id(
        self,
    ) -> None:
        unauthenticated = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/receipt?sync_id=sync.snapshot.0001",
            )
        )
        self.assertEqual(unauthenticated.status, 401)

        token = await self.exchange()
        for query in (
            "",
            "details=1",
            "sync_id=sync.snapshot.0001&details=1",
            "sync_id=sync.snapshot.0001&sync_id=sync.snapshot.0002",
            "sync_id=bad%20sync%20id",
            f"sync_id={'x' * 129}",
        ):
            with self.subTest(query=query):
                target = "/getbible/api/v1/contributions/receipt"
                if query:
                    target = f"{target}?{query}"
                rejected = await self.api.handle(
                    self.request("GET", target, token=token)
                )
                self.assertEqual(rejected.status, 400)
                self.assertEqual(
                    json.loads(rejected.body)["error"],
                    "invalid_request",
                )

    async def test_removed_contribution_write_routes_are_gone(self) -> None:
        """PUSH left HTTPS entirely: no sync, events, or status mutation remains."""
        for method, path in (
            ("POST", "/getbible/api/v1/contributions/sync"),
            ("POST", "/getbible/api/v1/contributions/events"),
            ("GET", "/getbible/api/v1/contributions/sync"),
            ("DELETE", "/getbible/api/v1/contributions/events"),
        ):
            with self.subTest(method=method, path=path):
                response = await self.api.handle(
                    self.request(method, path, body={})
                )
                self.assertEqual(response.status, 404)
        patched = await self.api.handle(
            self.request(
                "PATCH",
                "/getbible/api/v1/contributions/status",
                body={"disclosure_acknowledged": True},
            )
        )
        self.assertEqual(patched.status, 405)
        self.assertEqual(patched.headers["Allow"], "GET, OPTIONS")
        self.assertEqual(self.limiter.calls, [])

    async def test_oversized_bodies_are_rejected_before_authentication(self) -> None:
        too_large = await self.api.handle(
            MiniAppHttpRequest(
                method="POST",
                target="/getbible/api/v1/search",
                headers={
                    "Content-Type": "application/json",
                    "Origin": ORIGIN,
                },
                body=b"x" * (64 * 1024 + 1),
                client_key="192.0.2.1",
            )
        )
        self.assertEqual(too_large.status, 413)
        self.assertEqual(
            json.loads(too_large.body)["error"],
            "request_too_large",
        )
        self.assertEqual(self.limiter.calls, [])

    async def test_contributor_store_failure_never_blocks_scripture_session(self) -> None:
        self.contributions.close()
        self.active_init_data = _init_data()
        response = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": self.active_init_data},
            )
        )
        self.assertEqual(response.status, 201)
        payload = json.loads(response.body)
        self.assertEqual(
            payload["contributions"],
            {
                "enabled": False,
                "state": "unavailable",
                "can_contribute": False,
                "disclosure_required": False,
                "topics": [],
                "summary": {
                    "topics": {
                        "pending": 0,
                        "mapped": 0,
                        "published": 0,
                        "rejected": 0,
                        "deferred": 0,
                    },
                    "events": {
                        "pending": 0,
                        "approved": 0,
                        "rejected": 0,
                        "deferred": 0,
                        "applied": 0,
                    },
                },
            },
        )
        token = payload["session_token"]
        explicit = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/status?details=1",
                token=token,
            )
        )
        self.assertEqual(explicit.status, 503)
        self.assertEqual(
            json.loads(explicit.body),
            {
                "error": "contributions_unavailable",
                "message": "Contributor synchronization is temporarily unavailable.",
                "retryable": True,
            },
        )
        receipt = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/receipt?sync_id=sync.snapshot.0001",
                token=token,
            )
        )
        self.assertEqual(receipt.status, 503)
        self.assertTrue(json.loads(receipt.body)["retryable"])

    async def test_explicit_contribution_reads_require_a_configured_store(self) -> None:
        token = await self.exchange()
        with patch.object(self.api, "_contributions", None):
            status = await self.api.handle(
                self.request(
                    "GET",
                    "/getbible/api/v1/contributions/status",
                    token=token,
                )
            )
            receipt = await self.api.handle(
                self.request(
                    "GET",
                    "/getbible/api/v1/contributions/receipt"
                    "?sync_id=sync.snapshot.0001",
                    token=token,
                )
            )
        for response in (status, receipt):
            self.assertEqual(response.status, 503)
            self.assertEqual(
                json.loads(response.body)["error"],
                "contributions_unavailable",
            )
            self.assertTrue(json.loads(response.body)["retryable"])

    async def test_contribution_failure_access_log_records_only_safe_error_code(
        self,
    ) -> None:
        token = await self.exchange()
        self.api._audit_settings = SimpleNamespace(
            audit_log_mode="metadata",
            audit_identity_mode="disabled",
            telegram_api_token=TOKEN,
        )
        private_message = "Contributor synchronization is temporarily unavailable."

        with (
            patch.object(self.api, "_contributions", None),
            patch("modules.miniapp_api.audit_event") as record,
        ):
            unavailable = await self.api.handle(
                self.request(
                    "GET",
                    "/getbible/api/v1/contributions/receipt"
                    "?sync_id=sync.snapshot.0001",
                    token=token,
                )
            )

        self.assertEqual(unavailable.status, 503)
        self.assertIn(private_message, unavailable.body.decode())
        metadata = record.call_args.kwargs["metadata"]
        self.assertEqual(metadata["error_code"], "contributions_unavailable")
        self.assertEqual(metadata["route"], "contributions/receipt")
        self.assertNotIn("message", metadata)
        self.assertNotIn(private_message, json.dumps(metadata))

    async def test_contribution_receipts_are_stable_scoped_and_opaque(self) -> None:
        operations = [
            {
                "client_event_id": "topic.shared.v1",
                "type": "topic_upsert",
                "topic": {
                    "local_topic_id": "local.shared",
                    "name": "Shared",
                    "color": "#123456",
                },
            }
        ]
        tokens: dict[int, str] = {}
        for user_id in (42, 43):
            self.contributions.submit_application(user_id, first_name="Contributor")
            self.contributions.decide_application(user_id, "approved", actor="admin")
            self.contributions.acknowledge_disclosure(user_id)
            init_data = _init_data(
                user_id=user_id,
                query_id=f"query-{user_id}",
            )
            exchanged = await self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/session",
                    body={"init_data": init_data},
                )
            )
            self.assertEqual(exchanged.status, 201)
            tokens[user_id] = json.loads(exchanged.body)["session_token"]
            self.contributions.synchronize_snapshot(
                user_id,
                sync_id="sync.shared.0001",
                client_id=f"browser.installation.{user_id}",
                snapshot={"topics": [], "assignments": []},
                operations=operations,
            )

        receipts: dict[int, int] = {}
        for user_id, token in tokens.items():
            for attempt in ("first", "repeated"):
                confirmed = await self.api.handle(
                    self.request(
                        "GET",
                        "/getbible/api/v1/contributions/receipt"
                        "?sync_id=sync.shared.0001",
                        token=token,
                        include_init_data=False,
                    )
                )
                self.assertEqual(confirmed.status, 200)
                payload = json.loads(confirmed.body)
                self.assertTrue(payload["found"])
                receipt = payload["receipt"]["event_ids"]["topic.shared.v1"]
                self.assertGreaterEqual(receipt, 1)
                self.assertLessEqual(receipt, 9_007_199_254_740_991)
                if attempt == "first":
                    receipts[user_id] = receipt
                else:
                    self.assertEqual(receipt, receipts[user_id])

        # The same client_event_id yields a different receipt per contributor,
        # matching the deterministic contributor-scoped derivation.
        self.assertNotEqual(receipts[42], receipts[43])
        for user_id, receipt in receipts.items():
            self.assertEqual(
                receipt,
                _contribution_receipt_id(user_id, "topic.shared.v1"),
            )

        # Receipts are opaque: never the moderation store's own row IDs.
        internal_ids = {event.id for event in self.contributions.list_events()}
        self.assertTrue(set(receipts.values()).isdisjoint(internal_ids))

        # A receipt belongs to the contributor who pushed it: another user's
        # session cannot observe a sync it did not commit.
        self.contributions.synchronize_snapshot(
            42,
            sync_id="sync.private.0001",
            client_id="browser.installation.42",
            snapshot={"topics": [], "assignments": []},
            operations=operations,
        )
        foreign = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/contributions/receipt?sync_id=sync.private.0001",
                token=tokens[43],
                include_init_data=False,
            )
        )
        self.assertEqual(foreign.status, 200)
        self.assertEqual(json.loads(foreign.body), {"found": False, "receipt": None})

    async def test_live_catalog_is_authenticated_revisioned_and_revalidates_by_etag(
        self,
    ) -> None:
        unauthenticated = await self.api.handle(
            self.request("GET", "/getbible/api/v1/bookmarks/catalog")
        )
        self.assertEqual(unauthenticated.status, 401)

        token = await self.exchange()
        catalog = {
            "schema_version": 1,
            "topics": [
                {
                    "id": "grace",
                    "name": "Grace",
                    "color": "#bbf7d0",
                    "aliases": [],
                }
            ],
            "associations": {
                "add": [
                    {"topic_id": "grace", "book": 43, "chapter": 3, "verse": 16}
                ],
                "remove": [],
            },
        }
        published = self.contributions.publish_catalog(catalog, actor="admin")
        response = await self.api.handle(
            self.request("GET", "/getbible/api/v1/bookmarks/catalog", token=token)
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["revision"], 1)
        self.assertEqual(json.loads(response.body)["checksum"], published.checksum)
        self.assertEqual(response.headers["ETag"], published.etag)
        self.assertEqual(
            response.headers["Cache-Control"],
            "private, no-cache, max-age=0, must-revalidate",
        )

        unchanged = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/bookmarks/catalog",
                token=token,
                extra_headers={"If-None-Match": published.etag},
            )
        )
        self.assertEqual(unchanged.status, 304)
        self.assertEqual(unchanged.body, b"")

    async def test_contribution_preflight_and_wrong_methods_use_declared_contract(self) -> None:
        for path in (
            "/getbible/api/v1/contributions/status",
            "/getbible/api/v1/contributions/receipt",
        ):
            with self.subTest(path=path):
                preflight = await self.api.handle(self.request("OPTIONS", path))
                self.assertEqual(preflight.status, 204)
                self.assertIn(
                    "GET",
                    preflight.headers["Access-Control-Allow-Methods"],
                )

                wrong = await self.api.handle(self.request("POST", path, body={}))
                self.assertEqual(wrong.status, 405)
                self.assertEqual(wrong.headers["Allow"], "GET, OPTIONS")

        # The removed transport routes are not method errors: they no longer
        # exist at all, and preflight cannot resurrect them.
        for path in (
            "/getbible/api/v1/contributions/sync",
            "/getbible/api/v1/contributions/events",
        ):
            with self.subTest(path=path):
                gone = await self.api.handle(self.request("OPTIONS", path))
                self.assertEqual(gone.status, 404)
        self.assertEqual(self.limiter.calls, [])

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        token: str | None = None,
        origin: str | None = ORIGIN,
        init_data: str | None = None,
        include_init_data: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> MiniAppHttpRequest:
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
            if include_init_data:
                headers["X-Telegram-Init-Data"] = (
                    self.active_init_data if init_data is None else init_data
                )
        if extra_headers is not None:
            headers.update(extra_headers)
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

    async def test_identity_refresh_waits_for_a_successful_session_exchange(self) -> None:
        self.contributions.submit_application(42, first_name="Old name")
        original_observer = self.contributions.observe_identity
        with patch.object(
            self.contributions,
            "observe_identity",
            wraps=original_observer,
        ) as observer:
            invalid_init_data = _init_data(query_id="invalid-launch")
            invalid_launch = await self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/session",
                    body={
                        "init_data": invalid_init_data,
                        "launch_token": "abcdefghijklmnop",
                    },
                )
            )
            self.assertEqual(invalid_launch.status, 401)
            observer.assert_not_called()

            limited_init_data = _init_data(query_id="limited-launch")
            self.limiter.rejection = RobotRateLimited(
                30,
                blocked=False,
                new_block=False,
                violation_count=1,
                scopes=("user",),
                user_id=42,
                chat_id=42,
                client_key="192.0.2.1",
            )
            limited = await self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/session",
                    body={"init_data": limited_init_data},
                )
            )
            self.assertEqual(limited.status, 429)
            observer.assert_not_called()

            self.limiter.rejection = None
            accepted = await self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/session",
                    body={"init_data": limited_init_data},
                )
            )
            self.assertEqual(accepted.status, 201)
            observer.assert_called_once()
            self.assertEqual(
                self.contributions.application_for(42).first_name,
                "Grace",
            )

            replayed = await self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/session",
                    body={"init_data": limited_init_data},
                )
            )
            self.assertEqual(replayed.status, 200)
            observer.assert_called_once()

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
        self.assertEqual(replay.status, 200)
        self.assertEqual(
            json.loads(replay.body)["session_token"],
            payload["session_token"],
        )

    async def test_session_declares_the_search_budget_the_page_must_wait_out(
        self,
    ) -> None:
        # The browser cannot infer how long the robot will work on a search, and
        # a page that gives up first reports a timeout for work still in flight.
        # The budget is stated once, at bootstrap, and again on resume so a
        # reopened WebView does not fall back to a guess.
        created = await self.api.handle(
            self.request(
                "POST",
                "/getbible/api/v1/session",
                body={"init_data": _init_data()},
            )
        )
        payload = json.loads(created.body)
        self.assertEqual(created.status, 201)
        self.assertEqual(
            payload["limits"],
            {"search_timeout_seconds": self.service.settings.search_timeout},
        )

        resumed = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/session",
                token=payload["session_token"],
            )
        )
        self.assertEqual(
            json.loads(resumed.body)["limits"],
            payload["limits"],
        )

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

    async def test_exact_session_exchange_retry_returns_the_same_session(self) -> None:
        self.contributions.submit_application(42, first_name="Grace")
        self.contributions.decide_application(42, "approved", actor="admin")
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
        self.assertEqual(second.status, 200)
        self.assertEqual(
            json.loads(second.body)["session_token"],
            json.loads(first.body)["session_token"],
        )
        self.assertEqual(
            second.headers["X-Contribution-Token"],
            first.headers["X-Contribution-Token"],
        )

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

    async def test_session_bearer_does_not_require_repeated_init_data(self) -> None:
        token = await self.exchange()
        self.active_init_data = _init_data(user_id=42, start_param="different-launch")
        response = await self.api.handle(
            self.request(
                "GET",
                "/getbible/api/v1/session",
                token=token,
                origin=None,
                include_init_data=False,
            )
        )
        self.assertEqual(response.status, 200)

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

    async def test_active_bookmark_backup_pins_session_until_revoke(self) -> None:
        first_token = await self.exchange(query_id="bookmark-first")
        first_init_data = self.active_init_data
        first_session = self.sessions.get(first_token, touch=False)
        self.assertIsNotNone(first_session)
        self.bookmark_backup_started = asyncio.Event()
        self.bookmark_backup_release = asyncio.Event()
        backing_up = asyncio.create_task(
            self.api.handle(
                self.request(
                    "POST",
                    "/getbible/api/v1/bookmarks/backup",
                    token=first_token,
                    init_data=first_init_data,
                    body={
                        "idempotency_key": "abcdef0123456789",
                        "backup": _bookmark_backup(),
                    },
                )
            )
        )
        await self.bookmark_backup_started.wait()

        second_token = await self.exchange(query_id="bookmark-second")
        third_token = await self.exchange(query_id="bookmark-third")

        self.assertIs(
            self.sessions.get(first_token, touch=False),
            first_session,
        )
        self.assertIsNone(self.sessions.get(second_token, touch=False))
        self.assertIsNotNone(self.sessions.get(third_token, touch=False))

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

        self.bookmark_backup_release.set()
        backed_up, revoked = await asyncio.gather(backing_up, revoking)

        self.assertEqual(backed_up.status, 200)
        self.assertEqual(revoked.status, 204)
        self.assertEqual(len(self.bookmark_backups), 1)
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

    async def test_search_speaks_only_librarians_diacritics_vocabulary(self) -> None:
        """The API forwards the engine's own values rather than mapping onto them."""
        token = await self.exchange()
        for value in ("fold", "exact"):
            with self.subTest(value=value):
                response = await self.api.handle(
                    self.request(
                        "POST",
                        "/getbible/api/v1/search",
                        token=token,
                        body={
                            "query": "loved",
                            "options": {"diacritics": value},
                        },
                    )
                )

                self.assertEqual(response.status, 200)
                self.assertEqual(
                    self.service.search_requests[-1][1].diacritics,
                    value,
                )

        for rejected in ("insensitive", "sensitive", "folded"):
            with self.subTest(rejected=rejected):
                response = await self.api.handle(
                    self.request(
                        "POST",
                        "/getbible/api/v1/search",
                        token=token,
                        body={
                            "query": "loved",
                            "options": {"diacritics": rejected},
                        },
                    )
                )
                self.assertEqual(response.status, 400)

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
