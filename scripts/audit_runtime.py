"""Strictly audit the hashed runtime lock against published vulnerability data.

Every runtime dependency is a released registry package, so the complete lock
is handed to ``pip-audit --strict`` unchanged.  The helper exists so CI and the
local check runner share one command line and one exit-code contract.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


class AuditConfigurationError(RuntimeError):
    """Raised when the lock to audit is missing or unreadable."""


def audit_command(lock_path: Path, *, python: str = sys.executable) -> list[str]:
    """Return the exact pip-audit invocation for one lock file."""
    if not lock_path.is_file():
        raise AuditConfigurationError(f"Runtime lock {lock_path} does not exist.")
    return [python, "-m", "pip_audit", "--strict", "-r", str(lock_path)]


def audit_runtime_lock(lock_path: Path) -> int:
    print("Strictly auditing the complete hashed runtime lock.")
    completed = subprocess.run(audit_command(lock_path), check=False)
    return completed.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path("requirements.txt"),
        help="hashed runtime lock to audit (default: requirements.txt)",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        return audit_runtime_lock(arguments.lock)
    except AuditConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
