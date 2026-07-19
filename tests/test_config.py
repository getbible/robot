import os
import unittest
from unittest.mock import patch

from config import ConfigurationError, Settings


class SettingsTestCase(unittest.TestCase):
    def environment(self, **overrides: str) -> dict[str, str]:
        values = {"TELEGRAM_API_TOKEN": "123456:valid-token"}
        values.update(overrides)
        return values

    def test_data_and_public_web_bases_are_separate(self) -> None:
        with patch.dict(os.environ, self.environment(), clear=True):
            settings = Settings.from_env(load_environment_file=False)
        self.assertEqual(settings.api_base_url, "https://api.getbible.net")
        self.assertEqual(settings.web_base_url, "https://getbible.life")

    def test_conflicting_token_names_fail_closed(self) -> None:
        with (
            patch.dict(
                os.environ,
                self.environment(TELEGRAM_TOKEN="different-token"),
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


if __name__ == "__main__":
    unittest.main()
