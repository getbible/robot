import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

REQUIRED_DOCUMENTS = {
    ROOT / "README.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
    ROOT / "docs" / "README.md",
    ROOT / "docs" / "INSTALLATION.md",
    ROOT / "docs" / "DOCKER.md",
    ROOT / "docs" / "CONFIGURATION.md",
    ROOT / "docs" / "TESTING.md",
    ROOT / "docs" / "UPGRADING.md",
    ROOT / "docs" / "UNINSTALL.md",
    ROOT / "docs" / "DEPENDENCIES.md",
    ROOT / "docs" / "TROUBLESHOOTING.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "docs" / "OPERATIONS.md",
    ROOT / "docs" / "WEBHOOKS.md",
    ROOT / "docs" / "MINI_APP.md",
    ROOT / "docs" / "RELEASE_GATE.md",
}

EXPECTED_TEMPLATE_KEYS = {
    "TELEGRAM_API_TOKEN",
    "TELEGRAM_API_TOKEN_FILE",
    "TELEGRAM_DELIVERY_MODE",
    "TELEGRAM_WEBHOOK_PUBLIC_URL",
    "TELEGRAM_WEBHOOK_LISTEN",
    "TELEGRAM_WEBHOOK_PORT",
    "TELEGRAM_WEBHOOK_SECRET_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET_TOKEN_FILE",
    "TELEGRAM_WEBHOOK_IP_ADDRESS",
    "TELEGRAM_WEBHOOK_MAX_CONNECTIONS",
    "CONTAINERIZED",
    "INSTANCE_NAME",
    "LOG_FILE",
    "LOG_MAX_BYTES",
    "AUDIT_LOG_MODE",
    "AUDIT_IDENTITY_MODE",
    "BOT_NAME",
    "BOT_DESCRIPTION",
    "BOT_SHORT_DESCRIPTION",
    "MINI_APP_ENABLED",
    "MINI_APP_PUBLIC_URL",
    "MINI_APP_LISTEN",
    "MINI_APP_PORT",
    "MINI_APP_INIT_DATA_MAX_AGE_SECONDS",
    "MINI_APP_LAUNCH_TTL_SECONDS",
    "MINI_APP_SESSION_TTL_SECONDS",
    "MINI_APP_SESSION_LIMIT",
    "MINI_APP_SESSIONS_PER_USER",
    "MINI_APP_MAX_SEARCHES_PER_SESSION",
    "MINI_APP_MAX_AVAILABLE_SELECTIONS",
    "MINI_APP_MAX_SELECTIONS",
    "MINI_APP_BODY_TIMEOUT_SECONDS",
    "MINI_APP_IDLE_TIMEOUT_SECONDS",
    "MINI_APP_MAX_HEADER_BYTES",
    "MINI_APP_TRUSTED_PROXY_CIDRS",
    "MINI_APP_IP_RATE_CAPACITY",
    "MINI_APP_IP_RATE_REFILL_PER_SECOND",
    "MINI_APP_SESSION_EXCHANGE_RATE_CAPACITY",
    "MINI_APP_SESSION_EXCHANGE_RATE_REFILL_PER_SECOND",
    "MINI_APP_NAVIGATION_RATE_COST",
    "MINI_APP_ACCESS_LOG",
    "TRANSLATION",
    "USER_PREFERENCES_FILE",
    "USER_PREFERENCE_LIMIT",
    "GETBIBLE_API_BASE_URL",
    "GETBIBLE_WEB_BASE_URL",
    "WELCOME_MESSAGE",
    "HELP_MESSAGE",
    "WELCOME_MESSAGE_FILE",
    "HELP_MESSAGE_FILE",
    "GETBIBLE_CONNECT_TIMEOUT",
    "GETBIBLE_READ_TIMEOUT",
    "GETBIBLE_REQUEST_RETRIES",
    "GETBIBLE_MAX_RESPONSE_BYTES",
    "REFERENCE_CACHE_LIMIT",
    "BOOKS_CACHE_LIMIT",
    "CHAPTER_CACHE_LIMIT",
    "SEARCH_CORPUS_LIMIT",
    "TRANSLATION_CACHE_LIMIT",
    "CACHE_MAX_BYTES",
    "CACHE_MAINTENANCE_INTERVAL_SECONDS",
    "LOOKUP_TIMEOUT",
    "LOOKUP_QUEUE_TIMEOUT",
    "MAX_INPUT_LENGTH",
    "MAX_REFERENCES",
    "MAX_VERSES_PER_REFERENCE",
    "MAX_TOTAL_VERSES",
    "MAX_OUTPUT_CHUNKS",
    "SEARCH_RESULT_LIMIT",
    "SEARCH_DEADLINE_SECONDS",
    "SEARCH_MAX_RESPONSE_BYTES",
    "MAX_CONCURRENT_LOOKUPS",
    "MAX_CONCURRENT_SEARCHES",
    "MAX_CONCURRENT_UPDATES",
    "USER_RATE_CAPACITY",
    "USER_RATE_REFILL_PER_SECOND",
    "CHAT_RATE_CAPACITY",
    "CHAT_RATE_REFILL_PER_SECOND",
    "RATE_LIMIT_CACHE_SIZE",
    "RATE_LIMIT_NOTICE_COOLDOWN",
    "ABUSE_REJECTION_THRESHOLD",
    "ABUSE_WINDOW_SECONDS",
    "ABUSE_BLOCK_SECONDS",
    "ABUSE_WARNING_MESSAGE",
    "INTERACTION_SESSION_LIMIT",
    "INTERACTION_TTL_SECONDS",
    "CATALOG_CACHE_TTL_SECONDS",
    "CIRCUIT_FAILURE_THRESHOLD",
    "CIRCUIT_RECOVERY_SECONDS",
    "DELETE_COMMAND_MESSAGES",
    "DROP_PENDING_UPDATES",
    "PREWARM_DEFAULT_TRANSLATION",
    "HEALTH_HOST",
    "HEALTH_PORT",
    "LOG_LEVEL",
}


class DocumentationContractTestCase(unittest.TestCase):
    def test_required_operator_documents_exist(self) -> None:
        missing = sorted(
            str(path.relative_to(ROOT))
            for path in REQUIRED_DOCUMENTS
            if not path.is_file()
        )
        self.assertEqual(missing, [])

    def test_relative_markdown_links_resolve(self) -> None:
        failures: list[str] = []
        for document in sorted(REQUIRED_DOCUMENTS):
            text = document.read_text(encoding="utf-8")
            for match in LINK.finditer(text):
                target = match.group(1).strip()
                if target.startswith(("https://", "http://", "mailto:", "#")):
                    continue
                relative = target.split("#", 1)[0].split("?", 1)[0]
                if not relative:
                    continue
                resolved = (document.parent / relative).resolve()
                if not resolved.exists():
                    failures.append(f"{document.relative_to(ROOT)} -> {target}")
        self.assertEqual(failures, [])

    def test_environment_template_covers_every_runtime_setting(self) -> None:
        keys = {
            line.split("=", 1)[0]
            for line in (ROOT / ".env.template").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
        }
        self.assertEqual(keys, EXPECTED_TEMPLATE_KEYS)

    def test_setup_manager_and_template_are_present(self) -> None:
        self.assertTrue((ROOT / "setup.sh").is_file())
        self.assertTrue((ROOT / "deploy" / "getbible-robot@.service").is_file())

    def test_published_container_version_matches_project(self) -> None:
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'(?m)^version = "([^"]+)"$', project)
        self.assertIsNotNone(match)
        version = match.group(1) if match is not None else ""
        expected_image = f"ghcr.io/getbible/robot:{version}"
        compose_environment = (
            ROOT / "docker" / "examples" / "compose.env.example"
        ).read_text(encoding="utf-8")
        kubernetes = (ROOT / "deploy" / "kubernetes.example.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"ROBOT_IMAGE={expected_image}", compose_environment)
        self.assertIn(f"image: {expected_image}", kubernetes)

    def test_container_publish_workflow_is_versioned_and_attested(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "container-publish.yml"
        ).read_text(encoding="utf-8")
        for required in (
            "workflow_run:",
            "release:",
            "robot/security-gate",
            "robot/codeql-gate",
            "linux/amd64,linux/arm64",
            "ghcr.io",
            "provenance: mode=max",
            "sbom: true",
            "actions/attest@",
        ):
            with self.subTest(required=required):
                self.assertIn(required, workflow)

    def test_github_actions_are_immutably_pinned(self) -> None:
        failures: list[str] = []
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
            for line_number, line in enumerate(
                workflow.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                match = re.match(r"\s*uses:\s*[^@\s]+@([^\s#]+)", line)
                if match is not None and re.fullmatch(
                    r"[0-9a-f]{40}", match.group(1)
                ) is None:
                    failures.append(
                        f"{workflow.relative_to(ROOT)}:{line_number} -> {match.group(1)}"
                    )
        self.assertEqual(failures, [])

    def test_operator_docs_use_the_multi_instance_layout(self) -> None:
        documents = {
            ROOT / "README.md",
            ROOT / "SECURITY.md",
            ROOT / "docs" / "INSTALLATION.md",
            ROOT / "docs" / "CONFIGURATION.md",
            ROOT / "docs" / "TESTING.md",
            ROOT / "docs" / "UPGRADING.md",
            ROOT / "docs" / "UNINSTALL.md",
            ROOT / "docs" / "TROUBLESHOOTING.md",
            ROOT / "docs" / "OPERATIONS.md",
        }
        stale = (
            "/etc/getbible-robot.env",
            "getbible-robot.service",
            "deploy/getbible-robot.service",
        )
        failures = []
        for document in documents:
            text = document.read_text(encoding="utf-8")
            for value in stale:
                if value in text:
                    failures.append(f"{document.relative_to(ROOT)} -> {value}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
