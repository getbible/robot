import json
import logging
import tempfile
import unittest
from pathlib import Path

from bot import JsonFormatter, configure_logging


class LoggingTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in tuple(root.handlers):
            handler.close()
        root.handlers.clear()

    def test_formatter_includes_instance_and_controlled_audit_fields(self) -> None:
        record = logging.LogRecord(
            "modules.commands",
            logging.INFO,
            __file__,
            1,
            "Audit event: %s",
            ("search_completed",),
            None,
        )
        record.event = "search_completed"
        record.audit = {"translation": "kjv", "result_count": 2}
        payload = json.loads(JsonFormatter("production").format(record))
        self.assertEqual(payload["instance"], "production")
        self.assertEqual(payload["event"], "search_completed")
        self.assertEqual(payload["audit"]["translation"], "kjv")

    def test_file_logging_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "robot.jsonl"
            configure_logging(
                logging.INFO,
                instance_name="test",
                log_file=str(path),
            )
            logging.getLogger("test").info("ready")
            for handler in logging.getLogger().handlers:
                handler.flush()
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["instance"], "test")
        self.assertEqual(payload["message"], "ready")

    def test_file_logging_is_continuously_byte_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "robot.jsonl"
            configure_logging(
                logging.INFO,
                instance_name="test",
                log_file=str(path),
                log_max_bytes=1024,
            )
            for index in range(100):
                logging.getLogger("test").info("record-%d-%s", index, "x" * 80)
            for handler in logging.getLogger().handlers:
                handler.flush()
            self.assertLessEqual(path.stat().st_size, 1024)


if __name__ == "__main__":
    unittest.main()
