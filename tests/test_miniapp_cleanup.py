import asyncio
import unittest

from modules.miniapp_cleanup import MiniAppLaunchCleanup
from modules.miniapp_sessions import MiniAppLaunch
from modules.service import ScriptureQuery


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

    async def test_ready_cleanup_forgets_source_and_deletes_prompt_once(self) -> None:
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

        self.assertIsNone(launch.source_ephemeral_message_id)
        self.assertIsNone(launch.source_ephemeral_receiver_user_id)
        self.assertEqual(launch.prompt_ephemeral_message_id, 654)

        await coordinator.cleanup_now(launch)
        await coordinator.cleanup_now(launch)

        self.assertIsNone(launch.prompt_ephemeral_message_id)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0].prompt_ephemeral_message_id, 654)
        self.assertIsNone(cleaned[0].source_ephemeral_message_id)
        self.assertIsNone(cleaned[0].source_ephemeral_receiver_user_id)
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


if __name__ == "__main__":
    unittest.main()
