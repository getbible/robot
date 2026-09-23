"""Credential repair must work without exposing secrets or losing instance settings."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from modules.contribution_publication import PublicationError
from modules.contributions import ContributionStore
from scripts.contribution_publish import (
    KEYS,
    configure,
    credential_overrides,
    main,
    read_configuration,
)


class ContributionCredentialTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "robot.env"
        self.path.write_text(
            'TRANSLATION="aov"\n'
            'CONTRIBUTION_GITHUB_TOKEN="expired-github"\n'
            'CONTRIBUTION_OPENAI_API_KEY="expired-openai"\n'  # pragma: allowlist secret
            'CONTRIBUTION_TRANSLATION_MODEL="custom-model"\n',
            encoding="utf-8",
        )
        self.path.chmod(0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_explicit_configuration_replaces_existing_tokens_without_echo(self) -> None:
        prompt = Mock(side_effect=["replacement-github", "replacement-openai"])
        with redirect_stdout(io.StringIO()) as output:
            configure(self.path, prompt=prompt, interactive=True, replace=True)
        values = read_configuration(self.path)
        self.assertEqual(values[KEYS[0]], "replacement-github")
        self.assertEqual(values[KEYS[1]], "replacement-openai")
        self.assertEqual(values[KEYS[2]], "custom-model")
        self.assertIn('TRANSLATION="aov"', self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(prompt.call_count, 2)
        for token in (
            "expired-github",
            "expired-openai",
            "replacement-github",
            "replacement-openai",
        ):
            self.assertNotIn(token, output.getvalue())
            self.assertNotIn(token, repr(prompt.call_args_list))

    def test_enter_keeps_and_explicit_clear_disables_existing_credentials(self) -> None:
        with redirect_stdout(io.StringIO()):
            configure(self.path, prompt=Mock(side_effect=["", "-"]), interactive=True, replace=True)
        values = read_configuration(self.path)
        self.assertEqual(values[KEYS[0]], "expired-github")
        self.assertEqual(values[KEYS[1]], "")

    def test_invalid_or_cancelled_replacement_never_partially_changes_tokens(self) -> None:
        before = self.path.read_bytes()
        for values in (["replacement-github", 'bad"token'], ["replacement-github", EOFError]):
            with self.subTest(values=values), self.assertRaises(PublicationError):
                configure(
                    self.path, prompt=Mock(side_effect=values), interactive=True, replace=True
                )
            self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_configuration_requires_terminal_without_mutating_file(self) -> None:
        before = self.path.read_bytes()
        with self.assertRaisesRegex(PublicationError, "interactive terminal"):
            configure(self.path, interactive=False, replace=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_native_explicit_token_helper_reports_failure_instead_of_success(self) -> None:
        root = Path(__file__).resolve().parents[1]
        before = self.path.read_bytes()
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1/setup.sh"; prompt_contribution_tokens "$1" "$2" "$3" --replace',
                "native-token-test",
                str(root),
                sys.executable,
                str(self.path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires an interactive terminal", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_private_overlay_preserves_base_and_is_readable_after_reopening(self) -> None:
        before = self.path.read_bytes()
        target = self.root / "contribution-credentials.env"
        with redirect_stdout(io.StringIO()):
            configure(
                target,
                prompt=Mock(side_effect=["replacement-github", ""]),
                interactive=True,
                replace=True,
                fallback=read_configuration(self.path),
                create=True,
            )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(credential_overrides(target), {KEYS[0]: "replacement-github"})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_overlay_requires_private_regular_file_and_safe_parent(self) -> None:
        target = self.root / "credentials.env"
        self.assertEqual(credential_overrides(target), {})
        target.symlink_to(self.path)
        with self.assertRaises(PublicationError):
            credential_overrides(target)
        target.unlink()
        target.write_text('CONTRIBUTION_GITHUB_TOKEN="fixture-token"\n')
        target.chmod(0o644)
        with self.assertRaises(PublicationError):
            credential_overrides(target)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(PublicationError):
            credential_overrides(alias / "credentials.env")

    def test_commit_merges_private_overrides_without_inheriting_other_instance_tokens(self) -> None:
        store = self.root / "contributions.sqlite3"
        ContributionStore(path=str(store)).close()
        override = self.root / "credentials.env"
        override.write_text('CONTRIBUTION_GITHUB_TOKEN="replacement-github"\n')
        override.chmod(0o600)
        with (
            patch.dict(os.environ, {KEYS[0]: "other-instance", KEYS[1]: "other-instance"}),
            patch("scripts.contribution_publish.ContributionPublisher") as publisher,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            result = main(
                [
                    "commit",
                    "--store",
                    str(store),
                    "--env-file",
                    str(self.path),
                    "--credentials-file",
                    str(override),
                ]
            )
        self.assertEqual(result, 0)
        settings = publisher.call_args.args[0]
        self.assertEqual(settings.github_token, "replacement-github")
        self.assertEqual(settings.openai_key, "expired-openai")
        publisher.return_value.publish.assert_called_once_with(store)


if __name__ == "__main__":
    unittest.main()
