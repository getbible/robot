"""Serve the real Mini App API, static shell, and SQLite contribution store.

The browser suite normally answers every API route with a mock, so a real
client had never met the real server in one test. This process is the
server half of that missing test: the genuine ``MiniAppApi`` (real session
exchange with signed Telegram initData, real contributor token issuance,
real rate limiters, real ``ContributionStore`` on disk) behind the genuine
Tornado adapters, with only the Scripture service and preference store
stubbed, exactly like ``tests/test_miniapp_tornado.py``.

It prints one JSON line to stdout — ``{"port", "init_data", "user_id"}`` —
and then serves until terminated. Every API exchange is logged to stderr as
one JSON line so a failing browser run shows what the server really said.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tornado.httpserver import HTTPServer  # noqa: E402
from tornado.netutil import bind_sockets  # noqa: E402
from tornado.web import Application, RedirectHandler, RequestHandler, URLSpec  # noqa: E402

from modules.catalog import TranslationOption  # noqa: E402
from modules.contributions import ContributionStore  # noqa: E402
from modules.miniapp_api import MiniAppApi, MiniAppHttpRequest  # noqa: E402
from modules.miniapp_auth import TelegramInitDataValidator  # noqa: E402
from modules.miniapp_sessions import MiniAppLaunchStore, MiniAppSessionStore  # noqa: E402
from modules.miniapp_tornado import (  # noqa: E402
    MAX_MINI_APP_REQUEST_BYTES,
    ClientAddressResolver,
    MiniAppStaticHandler,
    miniapp_api_handlers,
)
from modules.preferences import ReaderLocation, SearchDefaults, UserPreferences  # noqa: E402

BOT_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
PUBLIC_ORIGIN = "https://app.local"
PUBLIC_PATH = "/getbible"
USER_ID = 42


def signed_init_data(user_id: int) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "real-server-harness",
        "user": json.dumps(
            {"id": user_id, "first_name": "Grace"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class _Service:
    settings = SimpleNamespace(mini_app_max_selections=50, search_timeout=150.0)

    async def translations(self) -> tuple[TranslationOption, ...]:
        return (TranslationOption("kjv", "King James Version", "English"),)


class _Preferences:
    def translation_for(self, user_id: int) -> str:
        return "kjv"

    def preferences_for(self, user_id: int) -> UserPreferences:
        # The browser fixture only mocks John 3 from the public Scripture API.
        return UserPreferences("kjv", SearchDefaults(), ReaderLocation("kjv", 43, 3, 1))

    def update_preferences(self, user_id: int, **changes: object) -> UserPreferences:
        translation = changes.get("translation", "kjv")
        return UserPreferences(str(translation), SearchDefaults(), ReaderLocation("kjv", 43, 3, 1))


class _Limiter:
    async def acquire(self, **kwargs: object) -> None:
        return None


async def _post_scripture(*args: object, **kwargs: object) -> tuple[int, ...]:
    return (101,)


class _LoggingApi:
    """Forward to the real API and mirror every exchange to stderr."""

    def __init__(self, api: MiniAppApi) -> None:
        self._api = api

    async def handle(self, request: MiniAppHttpRequest):  # noqa: ANN201
        response = await self._api.handle(request)
        record = {
            "method": request.method,
            "target": request.target,
            "status": response.status,
            "request_body": request.body[:600].decode("utf-8", "replace"),
            "response_body": response.body[:600].decode("utf-8", "replace"),
        }
        print(json.dumps(record), file=sys.stderr, flush=True)
        return response


class _HarnessStateHandler(RequestHandler):
    """Expose what the store durably holds so the browser side can assert it."""

    def initialize(self, store: ContributionStore) -> None:
        self._store = store

    def get(self) -> None:
        events = self._store.list_events()
        self.set_header("Content-Type", "application/json")
        self.finish(
            json.dumps(
                {
                    "event_count": len(events),
                    "event_types": [event.event_type for event in events],
                    "status": self._store.contribution_status(USER_ID),
                }
            )
        )


def _build_store(options: argparse.Namespace) -> ContributionStore:
    directory = tempfile.mkdtemp(prefix="getbible-real-server-")
    store_path = Path(directory) / "contributions.sqlite3"
    store = ContributionStore(path=str(store_path))
    store.submit_application(USER_ID, first_name="Grace")
    if options.state != "pending":
        store.decide_application(USER_ID, options.state, actor="admin")
    if options.acknowledged:
        store.acknowledge_disclosure(USER_ID)
    if options.damaged_then_reopened:
        # A store already at the current schema version takes no migration
        # branch, so a table lost to an interrupted upgrade stayed missing
        # forever: the approval read still succeeded (the panel appeared)
        # while every token write failed. The store now self-heals on open.
        store.close()
        with sqlite3.connect(store_path) as connection:
            connection.executescript(
                "DROP TABLE contributor_capabilities; PRAGMA user_version=5;"
            )
        store = ContributionStore(path=str(store_path))
    if options.fail_token_issuance:
        # The residual read-works/write-fails split (a root-owned WAL sidecar,
        # a read-only file): the approval read succeeds, every token write
        # raises. Model it at the store boundary the API depends on.
        def _refuse(user_id: int, **kwargs: object) -> str:
            raise sqlite3.OperationalError("attempt to write a readonly database")

        store.issue_capability = _refuse  # type: ignore[method-assign]
    return store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--acknowledged", action="store_true")
    parser.add_argument("--state", default="approved")
    parser.add_argument("--damaged-then-reopened", action="store_true")
    parser.add_argument("--fail-token-issuance", action="store_true")
    options = parser.parse_args()
    store = _build_store(options)

    api = MiniAppApi(
        service=_Service(),  # type: ignore[arg-type]
        preferences=_Preferences(),  # type: ignore[arg-type]
        limiter=_Limiter(),  # type: ignore[arg-type]
        sessions=MiniAppSessionStore(max_sessions=10, ttl_seconds=3600),
        launches=MiniAppLaunchStore(max_launches=10, ttl_seconds=300),
        validator=TelegramInitDataValidator(BOT_TOKEN, max_age_seconds=3600),
        public_url=f"{PUBLIC_ORIGIN}{PUBLIC_PATH}",
        post_scripture=_post_scripture,
        contributions=store,
    )
    handlers = list(
        miniapp_api_handlers(
            _LoggingApi(api),  # type: ignore[arg-type]
            public_path=PUBLIC_PATH,
            address_resolver=ClientAddressResolver(("127.0.0.1/32", "::1/128")),
        )
    )
    handlers.append(URLSpec(r"/__harness/state", _HarnessStateHandler, {"store": store}))
    handlers.append(
        URLSpec(PUBLIC_PATH, RedirectHandler, {"url": f"{PUBLIC_PATH}/", "permanent": True})
    )
    handlers.append(
        URLSpec(
            rf"{PUBLIC_PATH}/(.*)",
            MiniAppStaticHandler,
            {"path": str(ROOT / "miniapp"), "default_filename": "index.html"},
        )
    )
    application = Application(
        handlers,
        compress_response=True,
        serve_traceback=False,
        static_hash_cache=True,
    )

    async def serve() -> None:
        sockets = bind_sockets(0, "127.0.0.1")
        server = HTTPServer(
            application,
            max_buffer_size=MAX_MINI_APP_REQUEST_BYTES,
            max_body_size=MAX_MINI_APP_REQUEST_BYTES,
        )
        server.add_sockets(sockets)
        port = sockets[0].getsockname()[1]
        print(
            json.dumps(
                {"port": port, "init_data": signed_init_data(USER_ID), "user_id": USER_ID}
            ),
            flush=True,
        )
        await asyncio.Event().wait()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
