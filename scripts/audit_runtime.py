"""Strictly audit the runtime lock while Librarian is a verified source archive.

`pip-audit --strict` cannot resolve vulnerability metadata for a package version
that is installed directly from a source URL and is not yet published on PyPI.
This helper refuses to hide arbitrary direct requirements: it permits exactly the
GetBible source requirement declared in `requirements.in`, verifies that the lock
contains the same URL plus a SHA-256 hash, removes only that logical requirement,
and strictly audits every remaining registry dependency.

After `requirements.in` moves to a normal released `getbible>=1.2,<2` requirement,
the helper passes the complete lock to pip-audit without filtering anything.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_DIRECT_PREFIX = "getbible @ "
_SHA256 = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)")


class AuditConfigurationError(RuntimeError):
    """Raised when dependency intent and the generated lock disagree."""


def declared_direct_getbible(input_text: str) -> str | None:
    """Return the one direct GetBible source requirement, if still configured."""
    matches = [
        line.strip()
        for line in input_text.splitlines()
        if line.strip().startswith(_DIRECT_PREFIX)
    ]
    if len(matches) > 1:
        raise AuditConfigurationError(
            "requirements.in must contain at most one direct GetBible source requirement."
        )
    return matches[0] if matches else None


def filter_verified_direct_requirement(
    lock_text: str,
    direct_requirement: str | None,
) -> tuple[str, bool]:
    """Remove exactly one verified direct source block from a pip-compile lock."""
    if direct_requirement is None:
        return lock_text, False

    lines = lock_text.splitlines(keepends=True)
    retained: list[str] = []
    direct_blocks: list[list[str]] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        normalized = line.rstrip().removesuffix("\\").rstrip()
        if normalized != direct_requirement:
            retained.append(line)
            index += 1
            continue

        block = [line]
        index += 1
        while index < len(lines):
            candidate = lines[index]
            stripped = candidate.strip()
            is_requirement_start = (
                candidate == candidate.lstrip()
                and bool(stripped)
                and not stripped.startswith("#")
            )
            if is_requirement_start:
                break
            block.append(candidate)
            index += 1
        direct_blocks.append(block)

    if len(direct_blocks) != 1:
        raise AuditConfigurationError(
            "The runtime lock must contain exactly one matching GetBible source requirement."
        )

    block_text = "".join(direct_blocks[0])
    hashes = _SHA256.findall(block_text)
    if not hashes:
        raise AuditConfigurationError(
            "The direct GetBible source requirement must have a locked SHA-256 hash."
        )

    filtered = "".join(retained)
    if _DIRECT_PREFIX in filtered:
        raise AuditConfigurationError(
            "An unverified GetBible direct requirement remains in the filtered lock."
        )
    return filtered, True


def audit_runtime_lock(input_path: Path, lock_path: Path) -> int:
    input_text = input_path.read_text(encoding="utf-8")
    lock_text = lock_path.read_text(encoding="utf-8")
    direct_requirement = declared_direct_getbible(input_text)
    audited_text, filtered = filter_verified_direct_requirement(
        lock_text,
        direct_requirement,
    )

    audit_path = lock_path
    temporary_name: str | None = None
    try:
        if filtered:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".txt",
                prefix="getbible-robot-audit-",
                delete=False,
            ) as temporary:
                temporary.write(audited_text)
                temporary_name = temporary.name
            audit_path = Path(temporary_name)
            print(
                "Verified the exact hashed GetBible source archive; "
                "strictly auditing every registry dependency."
            )
        else:
            print("Strictly auditing the complete released-package runtime lock.")

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--strict",
                "-r",
                str(audit_path),
            ],
            check=False,
        )
        return completed.returncode
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("requirements.in"),
        help="Direct runtime dependency input (default: requirements.in)",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path("requirements.txt"),
        help="Hashed runtime dependency lock (default: requirements.txt)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return audit_runtime_lock(args.input, args.lock)
    except (AuditConfigurationError, OSError) as error:
        print(f"Runtime audit configuration failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
