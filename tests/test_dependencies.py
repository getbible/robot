import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

from modules.commands import (
    APPLICATION_SERVICES_SLOT,
    _components,
    _mini_app,
    _preference_store,
)
from modules.dependencies import ApplicationServices
from modules.interactions import InteractionStore
from modules.preferences import UserPreferenceStore
from modules.rate_limit import InboundRateLimiter


class ApplicationServicesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(default_translation="kjv")
        self.scripture = SimpleNamespace()
        self.limiter = InboundRateLimiter(
            user_capacity=5,
            user_refill_per_second=1.0,
            chat_capacity=10,
            chat_refill_per_second=2.0,
            max_entries=20,
        )
        self.interactions = InteractionStore(max_sessions=10, ttl_seconds=60)
        self.preferences = UserPreferenceStore(
            path=None,
            default_translation="kjv",
            max_users=10,
        )
        self.mini_app = SimpleNamespace()
        self.services = ApplicationServices(
            settings=self.settings,
            scripture=self.scripture,
            limiter=self.limiter,
            interactions=self.interactions,
            preferences=self.preferences,
            mini_app=self.mini_app,
        )
        self.context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={APPLICATION_SERVICES_SLOT: self.services}
            )
        )

    def tearDown(self) -> None:
        self.preferences.close()

    def test_handlers_resolve_one_typed_dependency_container(self) -> None:
        settings, scripture, limiter, interactions = _components(self.context)

        self.assertIs(settings, self.settings)
        self.assertIs(scripture, self.scripture)
        self.assertIs(limiter, self.limiter)
        self.assertIs(interactions, self.interactions)
        self.assertIs(_preference_store(self.context), self.preferences)
        self.assertIs(_mini_app(self.context), self.mini_app)

    def test_dependency_container_cannot_be_mutated_after_wiring(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.services.settings = SimpleNamespace(default_translation="asv")


if __name__ == "__main__":
    unittest.main()
