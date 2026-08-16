import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import ConfigurationError, Settings


class SettingsTestCase(unittest.TestCase):
    def environment(self, **overrides: str) -> dict[str, str]:
        values = {
            "TELEGRAM_API_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        }
        values.update(overrides)
        return values

    def test_data_and_public_web_bases_are_separate(self) -> None:
        with patch.dict(os.environ, self.environment(), clear=True):
            settings = Settings.from_env(load_environment_file=False)
        self.assertEqual(settings.api_base_url, "https://api.getbible.net")
        self.assertEqual(settings.web_base_url, "https://getbible.life")

    def test_search_capacity_defaults_are_safe_and_independent(self) -> None:
        with patch.dict(os.environ, self.environment(), clear=True):
            settings = Settings.from_env(load_environment_file=False)
        self.assertEqual(settings.max_response_bytes, 40 * 1024 * 1024)
        self.assertEqual(settings.search_max_response_bytes, 4 * 1024 * 1024)
        self.assertEqual(settings.reference_cache_limit, 1000)
        self.assertEqual(settings.chapter_cache_limit, 256)
        # Searches share one parsed corpus and one built index, so several can
        # run at once. Measured throughput is flat past the core count because
        # matching is CPU-bound Python, so this stays modest: enough that one
        # expensive query cannot stall every reader, not so many that everyone
        # waits behind a saturated interpreter. Scale out with instances.
        self.assertEqual(settings.max_concurrent_searches, 4)
        self.assertEqual(settings.search_shared_corpus_limit, 8)
        self.assertGreater(
            settings.search_shared_corpus_limit,
            settings.search_corpus_limit,
        )
        self.assertEqual(settings.max_concurrent_lookups, 8)
        self.assertEqual(settings.max_concurrent_updates, 16)
        self.assertTrue(settings.prewarm_default_translation)
        self.assertEqual(settings.telegram_delivery_mode, "polling")
        self.assertFalse(settings.mini_app_enabled)
        self.assertIsNone(settings.mini_app_public_url)

    def test_mini_app_configuration_is_explicit_and_loopback_only(self) -> None:
        with patch.dict(
            os.environ,
            self.environment(
                MINI_APP_ENABLED="true",
                MINI_APP_PUBLIC_URL="https://bot.example.com/getbible/app/",
                MINI_APP_PORT="9250",
            ),
            clear=True,
        ):
            settings = Settings.from_env(load_environment_file=False)

        self.assertTrue(settings.mini_app_enabled)
        self.assertEqual(
            settings.mini_app_public_url,
            "https://bot.example.com/getbible/app",
        )
        self.assertEqual(settings.mini_app_listen, "127.0.0.1")
        self.assertEqual(settings.mini_app_port, 9250)
        self.assertEqual(settings.mini_app_init_data_max_age_seconds, 300)
        self.assertEqual(settings.mini_app_launch_ttl_seconds, 300)
        self.assertEqual(settings.mini_app_session_ttl_seconds, 900)
        self.assertEqual(settings.mini_app_session_limit, 200)
        self.assertEqual(settings.mini_app_sessions_per_user, 2)
        self.assertEqual(settings.mini_app_max_available_selections, 256)
        self.assertEqual(settings.mini_app_max_selections, 100)
        self.assertEqual(
            settings.mini_app_trusted_proxy_cidrs,
            ("127.0.0.1/32", "::1/128"),
        )
        self.assertEqual(settings.mini_app_navigation_rate_cost, 0.25)
        self.assertEqual(settings.mini_app_ip_rate_capacity, 60)
        self.assertEqual(settings.mini_app_session_exchange_rate_capacity, 10)
        self.assertEqual(
            settings.mini_app_session_exchange_rate_refill_per_second,
            0.2,
        )
        self.assertTrue(settings.mini_app_access_log)

    def test_external_proxy_uses_specific_backend_and_trusted_source(self) -> None:
        with patch.dict(
            os.environ,
            self.environment(
                MINI_APP_ENABLED="true",
                MINI_APP_PUBLIC_URL="https://bot.example.com/getbible/app",
                REVERSE_PROXY_MODE="external",
                MINI_APP_LISTEN="10.0.0.20",
                MINI_APP_PORT="9250",
                MINI_APP_TRUSTED_PROXY_CIDRS="10.0.0.5/32",
            ),
            clear=True,
        ):
            settings = Settings.from_env(load_environment_file=False)
        self.assertEqual(settings.reverse_proxy_mode, "external")
        self.assertEqual(settings.mini_app_listen, "10.0.0.20")
        self.assertEqual(settings.mini_app_trusted_proxy_cidrs, ("10.0.0.5/32",))

    def test_external_proxy_defaults_to_operator_managed_network_boundary(self) -> None:
        with patch.dict(
            os.environ,
            self.environment(
                REVERSE_PROXY_MODE="external",
                MINI_APP_ENABLED="true",
                MINI_APP_PUBLIC_URL="https://bot.example.com/getbible/app",
                MINI_APP_LISTEN="0.0.0.0",
                MINI_APP_TRUSTED_PROXY_CIDRS="",
            ),
            clear=True,
        ):
            settings = Settings.from_env(load_environment_file=False)

        self.assertEqual(settings.reverse_proxy_mode, "external")
        self.assertEqual(settings.mini_app_listen, "0.0.0.0")
        self.assertEqual(
            settings.mini_app_trusted_proxy_cidrs,
            ("0.0.0.0/0", "::/0"),
        )

    def test_managed_proxy_rejects_an_all_addresses_forwarding_network(self) -> None:
        with (
            patch.dict(
                os.environ,
                self.environment(
                    MINI_APP_ENABLED="true",
                    MINI_APP_PUBLIC_URL="https://bot.example.com/getbible/app",
                    MINI_APP_TRUSTED_PROXY_CIDRS="0.0.0.0/0",
                ),
                clear=True,
            ),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_env(load_environment_file=False)

    def test_enabled_listener_ports_must_be_pairwise_distinct(self) -> None:
        common = {
            "TELEGRAM_DELIVERY_MODE": "webhook",
            "TELEGRAM_WEBHOOK_PUBLIC_URL": "https://bot.example.com/telegram/live",
            "TELEGRAM_WEBHOOK_SECRET_TOKEN": "A" * 32,
            "MINI_APP_ENABLED": "true",
            "MINI_APP_PUBLIC_URL": "https://bot.example.com/getbible/app",
        }
        with patch.dict(
            os.environ,
            self.environment(
                **common,
                TELEGRAM_WEBHOOK_PORT="9101",
                MINI_APP_PORT="9201",
                HEALTH_PORT="8081",
            ),
            clear=True,
        ):
            settings = Settings.from_env(load_environment_file=False)

        self.assertEqual(settings.webhook_port, 9101)
        self.assertEqual(settings.mini_app_port, 9201)
        self.assertEqual(settings.health_port, 8081)

        for collision in (
            {
                "TELEGRAM_WEBHOOK_PORT": "9101",
                "MINI_APP_PORT": "9101",
                "HEALTH_PORT": "8081",
            },
            {
                "TELEGRAM_WEBHOOK_PORT": "9101",
                "MINI_APP_PORT": "8081",
                "HEALTH_PORT": "8081",
            },
            {
                "TELEGRAM_WEBHOOK_PORT": "8081",
                "MINI_APP_PORT": "9201",
                "HEALTH_PORT": "8081",
            },
        ):
            with (
                self.subTest(collision=collision),
                patch.dict(
                    os.environ,
                    self.environment(**common, **collision),
                    clear=True,
                ),
                self.assertRaises(ConfigurationError),
            ):
                Settings.from_env(load_environment_file=False)

    def test_webhook_listener_supports_a_specific_remote_proxy_backend(self) -> None:
        with patch.dict(
            os.environ,
            self.environment(
                TELEGRAM_DELIVERY_MODE="webhook",
                TELEGRAM_WEBHOOK_PUBLIC_URL="https://bot.example.com/telegram/live",
                TELEGRAM_WEBHOOK_SECRET_TOKEN="A" * 32,
                TELEGRAM_WEBHOOK_LISTEN="10.0.0.20",
            ),
            clear=True,
        ):
            settings = Settings.from_env(load_environment_file=False)

        self.assertEqual(settings.webhook_listen, "10.0.0.20")

    def test_mini_app_configuration_fails_closed(self) -> None:
        invalid = (
            {"REVERSE_PROXY_MODE": "nginx"},
            {"MINI_APP_ENABLED": "true"},
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_PUBLIC_URL": "http://bot.example.com/app",
            },
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_PUBLIC_URL": "https://127.0.0.1/app",
            },
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_PUBLIC_URL": "https://user@bot.example.com/app",
            },
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_PUBLIC_URL": "https://bot.example.com/app?token=secret",
            },
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_PUBLIC_URL": "https://bot.example.com/a/../b",
            },
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_PUBLIC_URL": "https://bot.example.com/app",
                "MINI_APP_LISTEN": "0.0.0.0",
            },
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_PUBLIC_URL": "https://bot.example.com/app",
                "MINI_APP_PORT": "8081",
            },
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_PUBLIC_URL": "https://bot.example.com/app",
                "MINI_APP_INIT_DATA_MAX_AGE_SECONDS": "901",
            },
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_PUBLIC_URL": "https://bot.example.com/app",
                "MINI_APP_TRUSTED_PROXY_CIDRS": "not-a-network",
            },
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_PUBLIC_URL": "https://bot.example.com/app",
                "MINI_APP_NAVIGATION_RATE_COST": "0",
            },
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_PUBLIC_URL": "https://bot.example.com/app",
                "MINI_APP_SESSION_EXCHANGE_RATE_CAPACITY": "0",
            },
        )
        for overrides in invalid:
            with (
                self.subTest(overrides=overrides),
                patch.dict(
                    os.environ,
                    self.environment(**overrides),
                    clear=True,
                ),
                self.assertRaises(ConfigurationError),
            ):
                Settings.from_env(load_environment_file=False)

    def test_mini_app_port_cannot_overlap_enabled_webhook(self) -> None:
        with (
            patch.dict(
                os.environ,
                self.environment(
                    TELEGRAM_DELIVERY_MODE="webhook",
                    TELEGRAM_WEBHOOK_PUBLIC_URL="https://bot.example.com/telegram/live",
                    TELEGRAM_WEBHOOK_SECRET_TOKEN="A" * 32,
                    TELEGRAM_WEBHOOK_PORT="9250",
                    MINI_APP_ENABLED="true",
                    MINI_APP_PUBLIC_URL="https://bot.example.com/getbible/app",
                    MINI_APP_PORT="9250",
                ),
                clear=True,
            ),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_env(load_environment_file=False)

    def test_webhook_mode_requires_https_path_and_secret(self) -> None:
        invalid_webhook_value = "short"
        valid = self.environment(
            TELEGRAM_DELIVERY_MODE="webhook",
            TELEGRAM_WEBHOOK_PUBLIC_URL=(
                "https://bot.example.com/telegram/production"
            ),
            TELEGRAM_WEBHOOK_SECRET_TOKEN="A" * 32,
            TELEGRAM_WEBHOOK_IP_ADDRESS="1.1.1.1",
        )
        with patch.dict(os.environ, valid, clear=True):
            settings = Settings.from_env(load_environment_file=False)
        self.assertEqual(settings.telegram_delivery_mode, "webhook")
        self.assertEqual(settings.webhook_port, 9001)
        self.assertEqual(settings.webhook_ip_address, "1.1.1.1")

        invalid = (
            {"TELEGRAM_DELIVERY_MODE": "webhook"},
            {
                "TELEGRAM_DELIVERY_MODE": "webhook",
                "TELEGRAM_WEBHOOK_PUBLIC_URL": "http://bot.example.com/hook",
                "TELEGRAM_WEBHOOK_SECRET_TOKEN": "A" * 32,
            },
            {
                "TELEGRAM_DELIVERY_MODE": "webhook",
                "TELEGRAM_WEBHOOK_PUBLIC_URL": "https://bot.example.com/",
                "TELEGRAM_WEBHOOK_SECRET_TOKEN": "A" * 32,
            },
            {
                "TELEGRAM_DELIVERY_MODE": "webhook",
                "TELEGRAM_WEBHOOK_PUBLIC_URL": "https://127.0.0.1/hook",
                "TELEGRAM_WEBHOOK_SECRET_TOKEN": "A" * 32,
            },
            {
                "TELEGRAM_DELIVERY_MODE": "webhook",
                "TELEGRAM_WEBHOOK_PUBLIC_URL": "https://bot.example.com/.*",
                "TELEGRAM_WEBHOOK_SECRET_TOKEN": "A" * 32,
            },
            {
                "TELEGRAM_DELIVERY_MODE": "webhook",
                "TELEGRAM_WEBHOOK_PUBLIC_URL": "https://bot.example.com:9443/hook",
                "TELEGRAM_WEBHOOK_SECRET_TOKEN": "A" * 32,
            },
            {
                "TELEGRAM_DELIVERY_MODE": "webhook",
                "TELEGRAM_WEBHOOK_PUBLIC_URL": "https://bot.example.com/hook",
                "TELEGRAM_WEBHOOK_SECRET_TOKEN": invalid_webhook_value,
            },
            {
                "TELEGRAM_DELIVERY_MODE": "webhook",
                "TELEGRAM_WEBHOOK_PUBLIC_URL": "https://bot.example.com/hook",
                "TELEGRAM_WEBHOOK_SECRET_TOKEN": "A" * 32,
                "TELEGRAM_WEBHOOK_LISTEN": "0.0.0.0",
            },
        )
        for overrides in invalid:
            with (
                self.subTest(overrides=overrides),
                patch.dict(
                    os.environ,
                    self.environment(**overrides),
                    clear=True,
                ),
                self.assertRaises(ConfigurationError),
            ):
                Settings.from_env(load_environment_file=False)

    def test_help_and_welcome_files_support_normal_multiline_editing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            welcome = root / "welcome.txt"
            help_file = root / "help.txt"
            welcome.write_text("Welcome from a file.\nSecond line.\n", encoding="utf-8")
            help_file.write_text("Detailed help.\n/bible John 3:16\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                self.environment(
                    WELCOME_MESSAGE_FILE=str(welcome),
                    HELP_MESSAGE_FILE=str(help_file),
                ),
                clear=True,
            ):
                settings = Settings.from_env(load_environment_file=False)
        self.assertEqual(settings.welcome_message, "Welcome from a file.\nSecond line.")
        self.assertEqual(settings.help_message, "Detailed help.\n/bible John 3:16")

    def test_default_help_preserves_original_copy_with_completed_search(self) -> None:
        with patch.dict(os.environ, self.environment(), clear=True):
            settings = Settings.from_env(load_environment_file=False)

        self.assertEqual(
            settings.welcome_message,
            "Welcome to the official getBible.net telegram bot.\n"
            "/help for more info.",
        )
        self.assertIn(
            "You can use a reference to get verses like:",
            settings.help_message,
        )
        self.assertIn("/search grace", settings.help_message)
        self.assertIn("complete matching verses", settings.help_message)
        self.assertNotIn("Privacy", settings.help_message)
        self.assertNotIn("/start", settings.help_message)

    def test_conflicting_token_names_fail_closed(self) -> None:
        with (
            patch.dict(
                os.environ,
                self.environment(TELEGRAM_TOKEN="different-test-token"),
                clear=True,
            ),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_env(load_environment_file=False)

    def test_template_and_malformed_tokens_fail_closed(self) -> None:
        for token in (
            "replace-with-a-real-bot-token",
            "test-token",
            "123456:short",
        ):
            with (
                self.subTest(token=token),
                patch.dict(
                    os.environ,
                    {"TELEGRAM_API_TOKEN": token},
                    clear=True,
                ),
                self.assertRaises(ConfigurationError),
            ):
                Settings.from_env(load_environment_file=False)

    def test_token_file_and_container_listeners_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text(
                "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_API_TOKEN_FILE": str(token_file),
                    "CONTAINERIZED": "true",
                    "HEALTH_HOST": "0.0.0.0",
                    "MINI_APP_ENABLED": "true",
                    "MINI_APP_PUBLIC_URL": "https://bot.example.com/app",
                    "MINI_APP_LISTEN": "0.0.0.0",
                },
                clear=True,
            ):
                settings = Settings.from_env(load_environment_file=False)

        self.assertTrue(settings.containerized)
        self.assertEqual(settings.health_host, "0.0.0.0")
        self.assertEqual(settings.mini_app_listen, "0.0.0.0")

        with (
            patch.dict(
                os.environ,
                self.environment(
                    TELEGRAM_API_TOKEN_FILE="/run/secrets/token",
                ),
                clear=True,
            ),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_env(load_environment_file=False)

    def test_urls_reject_credentials_paths_and_nonlocal_http(self) -> None:
        invalid = (
            "http://api.getbible.net",
            "https://user@example.com",
            "https://getbible.life/path",
        )
        for value in invalid:
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    self.environment(GETBIBLE_WEB_BASE_URL=value),
                    clear=True,
                ),
                self.assertRaises(ConfigurationError),
            ):
                Settings.from_env(load_environment_file=False)

    def test_work_budgets_must_be_consistent(self) -> None:
        with (
            patch.dict(
                os.environ,
                self.environment(
                    MAX_VERSES_PER_REFERENCE="100",
                    MAX_TOTAL_VERSES="50",
                ),
                clear=True,
            ),
            self.assertRaises(ConfigurationError),
        ):
            Settings.from_env(load_environment_file=False)

    def test_search_budget_covers_the_index_build_it_can_provoke(self) -> None:
        with patch.dict(os.environ, self.environment(), clear=True):
            settings = Settings.from_env(load_environment_file=False)
        # A search waits for the build its first query triggers rather than
        # reporting a timeout for work that is still running. The default must
        # therefore leave room for a full build plus the matching deadline, and
        # must not be the reference-delivery budget.
        self.assertEqual(settings.search_timeout, 150.0)
        self.assertGreaterEqual(
            settings.search_timeout,
            settings.search_index_build_seconds + settings.search_deadline_seconds,
        )
        self.assertGreater(settings.search_timeout, settings.lookup_timeout)

    def test_search_budget_shorter_than_its_index_build_is_refused(self) -> None:
        for overrides in (
            {"SEARCH_TIMEOUT": "20"},
            {"SEARCH_TIMEOUT": "100", "SEARCH_INDEX_BUILD_SECONDS": "120"},
            {
                "SEARCH_TIMEOUT": "121",
                "SEARCH_INDEX_BUILD_SECONDS": "120",
                "SEARCH_DEADLINE_SECONDS": "5",
            },
        ):
            with (
                self.subTest(**overrides),
                patch.dict(
                    os.environ,
                    self.environment(**overrides),
                    clear=True,
                ),
                self.assertRaises(ConfigurationError),
            ):
                Settings.from_env(load_environment_file=False)

    def test_a_raised_index_build_budget_can_be_matched(self) -> None:
        with patch.dict(
            os.environ,
            self.environment(
                SEARCH_INDEX_BUILD_SECONDS="600",
                SEARCH_DEADLINE_SECONDS="30",
                SEARCH_TIMEOUT="700",
            ),
            clear=True,
        ):
            settings = Settings.from_env(load_environment_file=False)
        self.assertEqual(settings.search_timeout, 700.0)

    def test_output_chunk_budget_is_bounded(self) -> None:
        for value in ("0", "33"):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    self.environment(MAX_OUTPUT_CHUNKS=value),
                    clear=True,
                ),
                self.assertRaises(ConfigurationError),
            ):
                Settings.from_env(load_environment_file=False)

    def test_instance_file_and_audit_defaults_are_safe(self) -> None:
        with patch.dict(os.environ, self.environment(), clear=True):
            settings = Settings.from_env(load_environment_file=False)
        self.assertEqual(settings.instance_name, "local")
        self.assertIsNone(settings.log_file)
        self.assertIsNone(settings.user_preferences_file)
        self.assertEqual(settings.user_preference_limit, 10_000)
        self.assertEqual(settings.audit_log_mode, "metadata")
        self.assertEqual(settings.audit_identity_mode, "pseudonymous")
        self.assertEqual(settings.abuse_rejection_threshold, 6)
        self.assertEqual(settings.abuse_block_seconds, 300)

    def test_instance_file_and_audit_configuration_is_validated(self) -> None:
        invalid = (
            {"INSTANCE_NAME": "../escape"},
            {"INSTANCE_NAME": "A"},
            {"INSTANCE_NAME": "BadName"},
            {"INSTANCE_NAME": "bad--name"},
            {"LOG_FILE": "relative.log"},
            {"USER_PREFERENCES_FILE": "relative.sqlite3"},
            {"USER_PREFERENCE_LIMIT": "99"},
            {"AUDIT_LOG_MODE": "everything"},
            {"AUDIT_IDENTITY_MODE": "everything"},
            {"ABUSE_REJECTION_THRESHOLD": "1"},
            {"ABUSE_BLOCK_SECONDS": "0"},
        )
        for overrides in invalid:
            with (
                self.subTest(overrides=overrides),
                patch.dict(
                    os.environ,
                    self.environment(**overrides),
                    clear=True,
                ),
                self.assertRaises(ConfigurationError),
            ):
                Settings.from_env(load_environment_file=False)

    def test_content_audit_and_absolute_log_file_are_explicit(self) -> None:
        with patch.dict(
            os.environ,
            self.environment(
                INSTANCE_NAME="production",
                LOG_FILE="/var/log/getbible-robot/production.jsonl",
                AUDIT_LOG_MODE="content",
            ),
            clear=True,
        ):
            settings = Settings.from_env(load_environment_file=False)
        self.assertEqual(settings.instance_name, "production")
        self.assertEqual(
            settings.log_file,
            "/var/log/getbible-robot/production.jsonl",
        )
        self.assertEqual(settings.audit_log_mode, "content")


if __name__ == "__main__":
    unittest.main()
