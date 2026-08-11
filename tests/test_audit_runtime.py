import textwrap
import unittest

from scripts.audit_runtime import (
    AuditConfigurationError,
    declared_direct_getbible,
    filter_verified_direct_requirement,
)

DIRECT = (
    "getbible @ https://github.com/getbible/librarian/archive/"
    "95cdcafb6588d60eb2b1b000b4aa59f889c0f772.tar.gz"
)
HASH = "a" * 64


class RuntimeAuditTestCase(unittest.TestCase):
    def test_exact_hashed_direct_requirement_is_the_only_filtered_block(self) -> None:
        lock = textwrap.dedent(
            f"""
            # generated lock
            certifi==1.0 \\
                --hash=sha256:{'b' * 64}
            {DIRECT} \\
                --hash=sha256:{HASH}
                # via -r requirements.in
            requests==2.0 \\
                --hash=sha256:{'c' * 64}
            """
        ).lstrip()

        filtered, changed = filter_verified_direct_requirement(lock, DIRECT)

        self.assertTrue(changed)
        self.assertNotIn("getbible @", filtered)
        self.assertIn("certifi==1.0", filtered)
        self.assertIn("requests==2.0", filtered)

    def test_released_requirement_audits_the_complete_lock(self) -> None:
        input_text = "getbible>=2.0.0,<3\npython-telegram-bot==22.8\n"
        lock = "getbible==2.0.0\npython-telegram-bot==22.8\n"

        requirement = declared_direct_getbible(input_text)
        filtered, changed = filter_verified_direct_requirement(lock, requirement)

        self.assertIsNone(requirement)
        self.assertFalse(changed)
        self.assertEqual(filtered, lock)

    def test_missing_or_unhashed_direct_source_fails_closed(self) -> None:
        with self.assertRaises(AuditConfigurationError):
            filter_verified_direct_requirement("requests==2.0\n", DIRECT)

        unhashed = f"{DIRECT}\nrequests==2.0\n"
        with self.assertRaises(AuditConfigurationError):
            filter_verified_direct_requirement(unhashed, DIRECT)

    def test_multiple_direct_source_declarations_fail_closed(self) -> None:
        with self.assertRaises(AuditConfigurationError):
            declared_direct_getbible(f"{DIRECT}\n{DIRECT}\n")


if __name__ == "__main__":
    unittest.main()
