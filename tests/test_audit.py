import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from modules.audit import audit_event, audit_identity


class AuditEventTestCase(unittest.TestCase):
    def test_metadata_mode_omits_user_content(self) -> None:
        logger = Mock(spec=logging.Logger)
        audit_event(
            logger,
            SimpleNamespace(audit_log_mode="metadata"),
            "search_completed",
            metadata={"translation": "kjv", "result_count": 12},
            content={"query": "private words"},
        )
        record = logger.info.call_args.kwargs["extra"]
        self.assertEqual(record["event"], "search_completed")
        self.assertEqual(
            record["audit"],
            {"translation": "kjv", "result_count": 12},
        )

    def test_content_mode_includes_normalized_bounded_content(self) -> None:
        logger = Mock(spec=logging.Logger)
        audit_event(
            logger,
            SimpleNamespace(audit_log_mode="content"),
            "scripture_posted",
            metadata={"translation": "kjv"},
            content={"reference": "John  3:16\nRomans 8:1"},
        )
        record = logger.info.call_args.kwargs["extra"]
        self.assertEqual(
            record["audit"]["reference"],
            "John 3:16 Romans 8:1",
        )

    def test_invalid_event_and_field_names_fail_closed(self) -> None:
        logger = Mock(spec=logging.Logger)
        with self.assertRaises(ValueError):
            audit_event(logger, SimpleNamespace(audit_log_mode="metadata"), "../event")
        with self.assertRaises(ValueError):
            audit_event(
                logger,
                SimpleNamespace(audit_log_mode="metadata"),
                "valid_event",
                metadata={"../field": "value"},
            )

    def test_identity_fields_are_raw_disabled_or_stably_pseudonymous(self) -> None:
        base = {
            "audit_log_mode": "metadata",
            "telegram_api_token": "123456789:test-secret-value",
        }
        raw = audit_identity(
            SimpleNamespace(**base, audit_identity_mode="raw"),
            user_id=42,
            chat_id=-100,
            client_ip="192.0.2.1",
        )
        self.assertEqual(raw["telegram_user_id"], 42)
        self.assertEqual(raw["telegram_chat_id"], -100)
        self.assertEqual(raw["client_ip"], "192.0.2.1")

        first = audit_identity(
            SimpleNamespace(**base, audit_identity_mode="pseudonymous"),
            user_id=42,
            chat_id=-100,
            client_ip="192.0.2.1",
        )
        second = audit_identity(
            SimpleNamespace(**base, audit_identity_mode="pseudonymous"),
            user_id=42,
            chat_id=-100,
            client_ip="192.0.2.1",
        )
        self.assertEqual(first, second)
        self.assertNotIn("42", str(first))
        self.assertNotIn("192.0.2.1", str(first))

        disabled = audit_identity(
            SimpleNamespace(**base, audit_identity_mode="disabled"),
            user_id=42,
            client_ip="192.0.2.1",
        )
        self.assertEqual(disabled, {})


if __name__ == "__main__":
    unittest.main()
