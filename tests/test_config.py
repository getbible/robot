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
        self.assertEqual(settings.max_response_bytes, 64 * 1024 * 1024)
        self.assertEqual(settings.search_max_response_bytes, 4 * 1024 * 1024)
        self.assertEqual(settings.max_concurrent_searches, 1)
        self.assertTrue(settings.prewarm_default_translation)
        self.assertEqual(settings.telegram_delivery_mode, "polling")

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
        self.assertEqual(settings.audit_log_mode, "metadata")

    def test_instance_file_and_audit_configuration_is_validated(self) -> None:
        invalid = (
            {"INSTANCE_NAME": "../escape"},
            {"INSTANCE_NAME": "A"},
            {"INSTANCE_NAME": "BadName"},
            {"INSTANCE_NAME": "bad--name"},
            {"LOG_FILE": "relative.log"},
            {"AUDIT_LOG_MODE": "everything"},
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
