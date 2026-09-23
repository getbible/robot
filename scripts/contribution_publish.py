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
from modules.contribution_status import describe_publication  # noqa: E402
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


def write_configuration(
    path: Path, changes: Mapping[str, str], *, create: bool = False
) -> None:
    """Replace only selected keys atomically, preserving permissions and owner."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            raise
        metadata = None
    if metadata is not None and (
        not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024
    ):
        raise PublicationError("The publication configuration is unsafe.")
    lines = path.read_text(encoding="utf-8").splitlines() if metadata else []
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
            os.fchmod(file.fileno(), stat.S_IMODE(metadata.st_mode) & 0o600 if metadata else 0o600)
            if metadata is not None and os.geteuid() == 0:
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
    path: Path,
    *,
    prompt: Callable[[str], str] = getpass.getpass,
    interactive: bool | None = None,
    replace: bool = False,
    fallback: Mapping[str, str] | None = None,
    create: bool = False,
) -> None:
    values = dict(fallback or {})
    try:
        values.update(read_configuration(path))
    except FileNotFoundError:
        if not create:
            raise
    changes: dict[str, str] = {}
    # Container override files hold operator changes only. Writing a default
    # model there would silently shadow later Compose/instance model changes.
    if not create and not values.get(KEYS[2]):
        changes[KEYS[2]] = DEFAULT_TRANSLATION_MODEL
    terminal = sys.stdin.isatty() if interactive is None else interactive
    if replace and not terminal:
        raise PublicationError("Changing publication credentials requires an interactive terminal.")
    labels = {
        KEYS[0]: "GitHub token for the bookmarks builder (optional; Enter to skip): ",
        KEYS[1]: "OpenAI API key for new-topic translations (optional; Enter to skip): ",
    }
    for key, label in labels.items():
        if values.get(key) and not replace:
            continue
        if replace:
            state = "configured" if values.get(key) else "not configured"
            label = f"{key} ({state}; Enter to keep, '-' to clear): "
        token = ""
        if terminal:
            try:
                token = prompt(label).strip()
            except EOFError:
                if replace:
                    raise PublicationError(
                        "Credential entry was cancelled; no credentials were changed."
                    ) from None
        if token:
            if replace and token == "-":
                changes[key] = ""
                continue
            # Reject shell/dotenv syntax as well as controls. Neither token
            # provider uses these characters in its generated credentials.
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", token):
                if replace:
                    raise PublicationError(
                        "The credential format was rejected; no credentials were changed."
                    )
                print("The credential format was rejected; update will continue without it.")
                continue
            changes[key] = token
        elif not values.get(key):
            print(f"{key} was not supplied; update will continue.")
    if changes:
        write_configuration(path, changes, create=create)
        values.update(changes)
    if replace:
        print("Publication credentials saved." if changes else "Publication credentials unchanged.")
        for key in KEYS[:2]:
            print(f"  {key}: {'configured' if values.get(key) else 'not configured'}")
        print("Changes apply to the next contribution commit; no bot restart is needed.")
    else:
        print("Publication credentials can be added later with contributions INSTANCE tokens.")


def credential_overrides(path: Path) -> dict[str, str]:
    """Read only the selected instance's optional private, persistent overrides."""
    if not path.is_absolute():
        raise PublicationError("The publication credential file must use an absolute path.")
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink():
            raise PublicationError("The publication credential directory is unsafe.")
    parent_metadata = path.parent.stat()
    if parent_metadata.st_uid != os.geteuid() or parent_metadata.st_mode & 0o022:
        raise PublicationError("The publication credential directory is not private to its owner.")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {}
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise PublicationError("The publication credential file must be private to its owner.")
    return read_configuration(path)


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


def publication_status(path: Path, settings: PublicationSettings) -> None:
    describe_publication(path, settings)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("configure", "commit", "status"))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--store", type=Path)
    parser.add_argument("--run-as")
    parser.add_argument("--credentials-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            if args.env_file is None and args.credentials_file is None:
                parser.error("configure requires --env-file or --credentials-file")
            if args.credentials_file:
                credential_overrides(args.credentials_file)
                fallback = (
                    read_configuration(args.env_file)
                    if args.env_file
                    else {key: os.environ.get(key, "") for key in KEYS}
                )
                configure(
                    args.credentials_file, replace=args.replace, fallback=fallback, create=True
                )
            else:
                configure(args.env_file, replace=args.replace)
            return 0
        if args.replace:
            parser.error("--replace is only available for configure")
        if args.credentials_stdin and args.credentials_file:
            parser.error("--credentials-stdin and --credentials-file cannot be combined")
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
            if args.credentials_file:
                values.update(credential_overrides(args.credentials_file))
        settings = PublicationSettings.from_mapping(values)
        if args.run_as:
            return _handoff(args, values)
        journal_path = args.store.with_name(args.store.name + ".publication.sqlite3")
        if args.command == "status":
            publication_status(args.store, settings)
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
