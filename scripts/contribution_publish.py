#!/usr/bin/env python3
"""Configure optional credentials and commit already accepted bookmark changes.

This entry point is standard-library-only, including during an upgrade before
installing the new application's environment. Native credentials cross the
privilege boundary on stdin, never argv or the child's inherited environment.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.bookmark_sources import SourceError  # noqa: E402
from modules.contribution_publication import (  # noqa: E402
    DEFAULT_TRANSLATION_MODEL,
    ContributionPublisher,
    PublicationError,
    PublicationJournal,
    PublicationSettings,
)
from modules.contributions import ContributionStore  # noqa: E402

KEYS = (
    "CONTRIBUTION_GITHUB_TOKEN",
    "CONTRIBUTION_OPENAI_API_KEY",
    "CONTRIBUTION_TRANSLATION_MODEL",
)
ASSIGNMENT = re.compile(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


def read_configuration(path: Path) -> dict[str, str]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
        raise PublicationError("The publication configuration is not a bounded regular file.")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ASSIGNMENT.fullmatch(line)
        if match is None or match[1] not in KEYS:
            continue
        try:
            words = shlex.split(match[2], comments=True)
        except ValueError as error:
            raise PublicationError("A publication configuration value is malformed.") from error
        if len(words) > 1:
            raise PublicationError("A publication configuration value contains whitespace.")
        values[match[1]] = words[0] if words else ""
    return values


def write_configuration(path: Path, changes: Mapping[str, str]) -> None:
    """Replace only selected keys atomically, preserving permissions and owner."""
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
        raise PublicationError("The publication configuration is unsafe.")
    lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        match = ASSIGNMENT.fullmatch(line)
        if match is not None and match[1] in changes:
            key = match[1]
            if key not in seen:
                output.append(f'{key}="{changes[key]}"')
                seen.add(key)
        else:
            output.append(line)
    for key, value in changes.items():
        if key not in KEYS or re.search(r'["\\\r\n]', value):
            raise PublicationError("An unsafe publication configuration update was rejected.")
        if key not in seen:
            output.append(f'{key}="{value}"')
    descriptor, temporary = tempfile.mkstemp(prefix=".publication-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), stat.S_IMODE(metadata.st_mode) & 0o600)
            if os.geteuid() == 0:
                os.fchown(file.fileno(), metadata.st_uid, metadata.st_gid)
            file.write("\n".join(output) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure(
    path: Path, *, prompt: Callable[[str], str] = getpass.getpass, interactive: bool | None = None
) -> None:
    values = read_configuration(path)
    changes: dict[str, str] = {}
    if not values.get(KEYS[2]):
        changes[KEYS[2]] = DEFAULT_TRANSLATION_MODEL
    terminal = sys.stdin.isatty() if interactive is None else interactive
    labels = {
        KEYS[0]: "GitHub token for the bookmarks builder (optional; Enter to skip): ",
        KEYS[1]: "OpenAI API key for new-topic translations (optional; Enter to skip): ",
    }
    for key, label in labels.items():
        if values.get(key):
            continue
        token = ""
        if terminal:
            with suppress(EOFError):
                token = prompt(label).strip()
        if token:
            # Reject shell/dotenv syntax as well as controls. Neither token
            # provider uses these characters in its generated credentials.
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", token):
                print("The credential format was rejected; update will continue without it.")
                continue
            changes[key] = token
        else:
            print(f"{key} was not supplied; update will continue.")
    if changes:
        write_configuration(path, changes)
    print("Publication credentials can be added later with contributions INSTANCE tokens.")


def _handoff(args: argparse.Namespace, values: Mapping[str, Any]) -> int:
    import pwd

    if os.geteuid() != 0 or not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", args.run_as):
        raise PublicationError("Native publication requires a valid instance service account.")
    user = pwd.getpwnam(args.run_as)
    if user.pw_uid == 0:
        raise PublicationError(
            "Native publication must run as the instance service account, not root."
        )
    command = shutil.which("runuser", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if command is None:
        raise PublicationError("The service-account launcher is unavailable.")
    process = subprocess.run(
        [
            command,
            "--user",
            user.pw_name,
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            args.command,
            "--store",
            str(args.store),
            "--credentials-stdin",
        ],
        input=json.dumps({key: values.get(key, "") for key in KEYS}),
        text=True,
        cwd=ROOT,
        check=False,
        timeout=2400,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": user.pw_dir,
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    return process.returncode


def publication_status(path: Path) -> None:
    if not path.exists():
        print("Direct API publication: not started; the acceptance ledger is unchanged.")
        return
    journal = PublicationJournal(path)
    try:
        pending = journal.get("pending")
        last = journal.get("last")
        print("Direct API publication: " + ("pending" if pending else "no queued commit"))
        if last:
            print(f"  Last builder commit: {last['commit']} ({last['branch']})")
            print(f"  Accepted ledger revision: {last['revision']}")
        if pending:
            print(f"  Pending accepted revision: {pending['revision']}")
        if journal.get("error"):
            print("  The last attempt failed; run commit to retry the saved publication.")
    finally:
        journal.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("configure", "commit", "status"))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--run-as")
    parser.add_argument("--credentials-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            if args.env_file is None:
                parser.error("configure requires --env-file")
            configure(args.env_file)
            return 0
        if args.store is None or not args.store.is_absolute():
            parser.error("commit/status requires an absolute --store path")
        if args.credentials_stdin:
            payload = sys.stdin.read(8193)
            if len(payload) > 8192:
                raise PublicationError("The publication credential envelope is oversized.")
            values = json.loads(payload)
            if not isinstance(values, dict) or set(values) - set(KEYS):
                raise PublicationError("The publication credential envelope is invalid.")
        else:
            # File-based multi-instance configuration must never inherit a
            # different instance's publisher credentials from the parent.
            values = (
                read_configuration(args.env_file)
                if args.env_file
                else {key: os.environ.get(key, "") for key in KEYS}
            )
        settings = PublicationSettings.from_mapping(values)
        if args.run_as:
            return _handoff(args, values)
        journal_path = args.store.with_name(args.store.name + ".publication.sqlite3")
        if args.command == "status":
            publication_status(journal_path)
            return 0
        # This uses the existing idempotent v1..v6 migration, never a fresh
        # replacement database. The separate journal is not a schema upgrade.
        metadata = args.store.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise PublicationError("Run publication as the contribution database's owner.")
        store = ContributionStore(path=str(args.store))
        try:
            store.verify_writable()
        finally:
            store.close()
        journal = PublicationJournal(journal_path)
        try:
            ContributionPublisher(settings, journal).publish(args.store)
        finally:
            journal.close()
        return 0
    except (PublicationError, SourceError) as error:
        print(f"Publication: {error}", file=sys.stderr)
        return 1
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        # Do not render arbitrary exception strings: libraries may include a
        # response, a credential envelope or a command's input in them.
        print(
            f"Publication could not complete ({type(error).__name__}); "
            "accepted contributions remain stored.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
