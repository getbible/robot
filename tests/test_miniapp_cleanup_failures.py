import unittest

from modules.miniapp_cleanup import MiniAppLaunchCleanup
from modules.miniapp_sessions import MiniAppLaunch
from modules.service import ScriptureQuery


class MiniAppCleanupFailureTestCase(unittest.IsolatedAsyncioTestCase):
    def launch(self) -> MiniAppLaunch:
        return MiniAppLaunch(
            token="abcdefghijklmnop",
            user_id=42,
            target_chat_id=-100,
            message_thread_id=None,
            initial_route="bible",
            initial_query="",
            created_at=0,
            prompt_ephemeral_message_id=654,
        )

    async def test_permission_failure_is_silent_and_never_retried(self) -> None:
        attempts = 0

        async def denied(launch: MiniAppLaunch) -> None:
            nonlocal attempts
            attempts += 1
            raise PermissionError(f"cannot delete {launch.prompt_ephemeral_message_id}")

        coordinator = MiniAppLaunchCleanup(
            denied,
            ttl_seconds=300,
            max_pending=10,
        )
        launch = self.launch()
        coordinator.remember_prompt(launch)

        await coordinator.cleanup_now(launch)
        await coordinator.cleanup_now(launch)
        await coordinator.close()

        self.assertEqual(attempts, 1)
        self.assertIsNone(launch.prompt_ephemeral_message_id)

    async def test_cleanup_denial_cannot_invalidate_a_successful_post(self) -> None:
        attempts = 0

        async def denied(launch: MiniAppLaunch) -> None:
            nonlocal attempts
            attempts += 1
            raise PermissionError(f"cannot delete {launch.prompt_ephemeral_message_id}")

        async def post_scripture(
            launch: MiniAppLaunch,
            queries: tuple[ScriptureQuery, ...],
        ) -> tuple[int, ...]:
            self.assertIsNone(launch.prompt_ephemeral_message_id)
            self.assertEqual(queries, (ScriptureQuery("John 3:16", "kjv"),))
            return (101,)

        coordinator = MiniAppLaunchCleanup(
            denied,
            ttl_seconds=300,
            max_pending=10,
        )
        launch = self.launch()
        coordinator.remember_prompt(launch)

        result = await coordinator.post(
            launch,
            (ScriptureQuery("John 3:16", "kjv"),),
            post_scripture,
        )

        self.assertEqual(result, (101,))
        self.assertEqual(attempts, 1)
        await coordinator.close()


if __name__ == "__main__":
    unittest.main()
