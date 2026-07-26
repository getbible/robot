import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

from modules.miniapp_auth import (
    MiniAppAuthenticationError,
    TelegramInitDataValidator,
)

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


def _init_data(
    *,
    now: int = 1_700_000_000,
    user_id: int = 42,
    extra: dict[str, str] | None = None,
) -> str:
    fields = {
        "auth_date": str(now),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(
            {"id": user_id, "first_name": "Grace"},
            separators=(",", ":"),
        ),
    }
    if extra is not None:
        fields.update(extra)
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret,
        check.encode(),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(fields)


class TelegramInitDataValidatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = TelegramInitDataValidator(
            TOKEN,
            max_age_seconds=300,
            wall_clock=lambda: 1_700_000_000,
        )

    def test_valid_payload_returns_only_trusted_identity_and_context(self) -> None:
        raw = _init_data(
            extra={
                "chat": json.dumps(
                    {"id": -100123, "type": "supergroup"},
                    separators=(",", ":"),
                ),
                "chat_instance": "1234567890123456789",
                "chat_type": "supergroup",
                "start_param": "abcdefghijklmnop",
            }
        )

        principal = self.validator.validate(raw)

        self.assertEqual(principal.user_id, 42)
        self.assertEqual(principal.chat_id, -100123)
        self.assertEqual(principal.rate_limit_chat_id, -100123)
        self.assertEqual(principal.start_param, "abcdefghijklmnop")

    def test_tampering_is_rejected_with_constant_public_error(self) -> None:
        raw = _init_data().replace("Grace", "Mallory")

        with self.assertRaisesRegex(
            MiniAppAuthenticationError,
            "Invalid Telegram authorization",
        ):
            self.validator.validate(raw)

    def test_stale_and_future_payloads_are_rejected(self) -> None:
        for timestamp in (1_699_999_699, 1_700_000_031):
            with (
                self.subTest(timestamp=timestamp),
                self.assertRaises(MiniAppAuthenticationError),
            ):
                self.validator.validate(_init_data(now=timestamp))

    def test_authenticated_session_can_recheck_integrity_without_launch_age(self) -> None:
        principal = self.validator.validate(
            _init_data(now=1_600_000_000),
            check_freshness=False,
        )
        self.assertEqual(principal.user_id, 42)

    def test_duplicate_fields_are_rejected_even_with_a_valid_first_value(self) -> None:
        raw = f"{_init_data()}&auth_date=1700000000"

        with self.assertRaises(MiniAppAuthenticationError):
            self.validator.validate(raw)

    def test_bot_and_non_integer_users_are_rejected(self) -> None:
        invalid_users = (
            {"id": True, "first_name": "Bad"},
            {"id": 42, "first_name": "Bad", "is_bot": True},
        )
        for user in invalid_users:
            with self.subTest(user=user):
                raw = _init_data(extra={"user": json.dumps(user, separators=(",", ":"))})
                with self.assertRaises(MiniAppAuthenticationError):
                    self.validator.validate(raw)


if __name__ == "__main__":
    unittest.main()
