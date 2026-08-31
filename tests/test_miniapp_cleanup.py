import asyncio
import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

from modules.miniapp_api import MiniAppHttpRequest
from modules.miniapp_auth import TelegramInitDataValidator
from modules.miniapp_cleanup import MiniAppLaunchCleanup
from modules.miniapp_sessions import MiniAppLaunch, MiniAppSessionStore
from modules.miniapp_tornado import MiniAppServer
from modules.service import ScriptureQuery

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


def _init_data(user_id: int = 42, *, query_id: str = "query-id") -> str:
    fields = {
        "auth_date": "1700000000",
        "query_id": query_id,
        "user": json.dumps(
            {"id": user_id, "first_name": "Grace"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class MiniAppLaunchCleanupTestCase(unittest.IsolatedAsyncioTestCase):
    def launch(self) -> MiniAppLaunch:
        return MiniAppLaunch(
            token="abcdefghijklmnop",
            user_id=42,
            target_chat_id=-100,
            message_thread_id=9,
            initial_route="bible",
            initial_query="",
            created_at=0,
            prompt_ephemeral_message_id=654,
            source_ephemeral_message_id=250,
            source_ephemeral_receiver_user_id=999,
        )

    async def test_ready_cleanup_claims_source_and_launcher_together(self) -> None:
        cleaned: list[MiniAppLaunch] = []

        async def callback(launch: MiniAppLaunch) -> None:
            cleaned.append(launch)

        coordinator = MiniAppLaunchCleanup(
            callback,
            ttl_seconds=300,
            max_pending=10,
        )
        launch = self.launch()
        coordinator.remember_prompt(launch)

        self.assertEqual(launch.source_ephemeral_message_id, 250)
        self.assertEqual(launch.source_ephemeral_receiver_user_id, 999)
        self.assertEqual(launch.prompt_ephemeral_message_id, 654)

        await coordinator.cleanup_now(launch)
        await coordinator.cleanup_now(launch)

        self.assertIsNone(launch.prompt_ephemeral_message_id)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].prompt_ephemeral_message_id, 654)
        self.assertEqual(cleaned[0].source_ephemeral_message_id, 250)
        self.assertEqual(cleaned[0].source_ephemeral_receiver_user_id, 999)
        await coordinator.close()

    async def test_post_cannot_repeat_cleanup_owned_by_the_coordinator(self) -> None:
        cleaned: list[MiniAppLaunch] = []
        post_observations: list[tuple[int | None, int | None]] = []

        async def cleanup_callback(launch: MiniAppLaunch) -> None:
            cleaned.append(launch)

        async def post_callback(
            launch: MiniAppLaunch,
            queries: tuple[ScriptureQuery, ...],
        ) -> tuple[int, ...]:
            self.assertEqual(queries, (ScriptureQuery("John 3:16", "kjv"),))
            post_observations.append(
                (
                    launch.prompt_message_id,
                    launch.prompt_ephemeral_message_id,
                )
            )
            return (101,)

        coordinator = MiniAppLaunchCleanup(
            cleanup_callback,
            ttl_seconds=300,
            max_pending=10,
        )
        launch = self.launch()
        coordinator.remember_prompt(launch)

        result = await coordinator.post(
            launch,
            (ScriptureQuery("John 3:16", "kjv"),),
            post_callback,
        )
        await coordinator.cleanup_now(launch)

        self.assertEqual(result, (101,))
        self.assertEqual(post_observations, [(None, None)])
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].prompt_ephemeral_message_id, 654)
        self.assertEqual(cleaned[0].source_ephemeral_message_id, 250)
        self.assertEqual(cleaned[0].source_ephemeral_receiver_user_id, 999)
        await coordinator.close()

    async def test_failed_post_still_removes_the_private_launch_row_once(self) -> None:
        cleaned: list[MiniAppLaunch] = []

        async def cleanup_callback(launch: MiniAppLaunch) -> None:
            cleaned.append(launch)

        async def failing_post(
            launch: MiniAppLaunch,
            queries: tuple[ScriptureQuery, ...],
        ) -> tuple[int, ...]:
            del launch, queries
            raise RuntimeError("post failed")

        coordinator = MiniAppLaunchCleanup(
            cleanup_callback,
            ttl_seconds=300,
            max_pending=10,
        )
        launch = self.launch()
        coordinator.remember_prompt(launch)

        with self.assertRaisesRegex(RuntimeError, "post failed"):
            await coordinator.post(
                launch,
                (ScriptureQuery("John 3:16", "kjv"),),
                failing_post,
            )

        self.assertEqual(len(cleaned), 1)
        await coordinator.cleanup_now(launch)
        self.assertEqual(len(cleaned), 1)
        await coordinator.close()

    async def test_expiry_removes_an_unopened_launcher_once(self) -> None:
        cleaned: list[MiniAppLaunch] = []
        sleep_started = asyncio.Event()
        release_sleep = asyncio.Event()
        cleanup_finished = asyncio.Event()

        async def controlled_sleep(seconds: float) -> None:
            self.assertEqual(seconds, 300)
            sleep_started.set()
            await release_sleep.wait()

        async def cleanup_callback(launch: MiniAppLaunch) -> None:
            cleaned.append(launch)
            cleanup_finished.set()

        coordinator = MiniAppLaunchCleanup(
            cleanup_callback,
            ttl_seconds=300,
            max_pending=10,
            sleep=controlled_sleep,
        )
        launch = self.launch()
        coordinator.remember_prompt(launch)
        await sleep_started.wait()
        release_sleep.set()
        await cleanup_finished.wait()

        self.assertEqual(len(cleaned), 1)
        await coordinator.cleanup_now(launch)
        self.assertEqual(len(cleaned), 1)
        await coordinator.close()

    async def test_graceful_close_cleans_every_pending_prompt(self) -> None:
        cleaned: list[MiniAppLaunch] = []

        async def cleanup_callback(launch: MiniAppLaunch) -> None:
            cleaned.append(launch)

        coordinator = MiniAppLaunchCleanup(
            cleanup_callback,
            ttl_seconds=300,
            max_pending=10,
        )
        first = self.launch()
        second = self.launch()
        second.token = "qrstuvwxyzABCDEF"
        second.prompt_ephemeral_message_id = 655
        coordinator.remember_prompt(first)
        coordinator.remember_prompt(second)

        await coordinator.close()
        await coordinator.close()

        self.assertEqual(
            {launch.prompt_ephemeral_message_id for launch in cleaned},
            {654, 655},
        )


class MiniAppCleanupRequestTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.init_data = _init_data()
        self.validator = TelegramInitDataValidator(
            TOKEN,
            wall_clock=lambda: 1_700_000_000,
        )
        principal = self.validator.validate(self.init_data)
        self.sessions = MiniAppSessionStore(
            max_sessions=10,
            ttl_seconds=600,
        )
        self.launch = MiniAppLaunch(
            token="abcdefghijklmnop",
            user_id=42,
            target_chat_id=-100,
            message_thread_id=None,
            initial_route="bible",
            initial_query="",
            created_at=0,
            prompt_ephemeral_message_id=654,
        )
        self.session = self.sessions.create(
            principal,
            translation="kjv",
            launch=self.launch,
            init_data_digest=hashlib.sha256(self.init_data.encode()).digest(),
        )
        self.cleaned: list[MiniAppLaunch] = []
        cleaned = self.cleaned

        class Cleanup:
            async def cleanup_now(self, launch: MiniAppLaunch) -> None:
                cleaned.append(launch)

        self.server = object.__new__(MiniAppServer)
        self.server._public_origin = "https://robot.example"
        self.server._validator = self.validator
        self.server.sessions = self.sessions
        self.server._cleanup = Cleanup()

    def request(
        self,
        origin: str = "https://robot.example",
        *,
        token: str | None = None,
    ) -> MiniAppHttpRequest:
        return MiniAppHttpRequest(
            method="POST",
            target="/getbible/api/v1/cleanup",
            headers={
                "Origin": origin,
                "Authorization": f"Bearer {token or self.session.token}",
            },
            client_key="192.0.2.1",
        )

    async def test_ready_signal_authenticates_and_cleans_its_bound_launch(self) -> None:
        status = await self.server._cleanup_session_request(self.request())

        self.assertEqual(status, 204)
        self.assertEqual(self.cleaned, [self.launch])

    async def test_ready_signal_rejects_foreign_origin_before_cleanup(self) -> None:
        status = await self.server._cleanup_session_request(
            self.request("https://attacker.example")
        )

        self.assertEqual(status, 403)
        self.assertEqual(self.cleaned, [])

    async def test_ready_signal_hides_authentication_failures(self) -> None:
        status = await self.server._cleanup_session_request(
            self.request(token="missing-session-token")
        )

        self.assertEqual(status, 401)
        self.assertEqual(self.cleaned, [])

    async def test_cleanup_is_not_rejected_by_the_normal_request_limiter(self) -> None:
        self.assertFalse(hasattr(self.server, "api"))

        status = await self.server._cleanup_session_request(self.request())

        self.assertEqual(status, 204)
        self.assertEqual(self.cleaned, [self.launch])


if __name__ == "__main__":
    unittest.main()
