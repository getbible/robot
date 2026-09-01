#!/usr/bin/env python3
"""Review trusted bookmark contributions and publish approved catalogue changes.

The interactive review commands run as the isolated instance service account.
Repository publication is intentionally separate and must run as a dedicated,
non-root Git publisher account.  Telegram identities never enter an export,
commit, or Git command argument.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, Protocol, TextIO, cast

SCHEMA_VERSION = 1
DEFAULT_TRANSLATION = "kjv"
MAX_DISPLAY_TEXT = 500
MAX_NOTE_LENGTH = 500
MAX_ACTOR_LENGTH = 100
MAX_TOPIC_NAME_LENGTH = 80
MAX_ALIAS_COUNT = 20
MAX_ALIAS_LENGTH = 80
MAX_EVENTS_PER_REVIEW = 500
EVENT_SCAN_PAGE_SIZE = 10_000
MAX_EVENTS_PER_SCAN = 250_000
MAX_EFFECTIVE_TOPICS = 100
MAX_EFFECTIVE_ASSOCIATIONS = 10_000
MAX_OVERLAY_BYTES = 2 * 1024 * 1024
EXPECTED_GITHUB_REPOSITORY = "getbible/robot"
GIT_PUBLICATION_TIMEOUT_SECONDS = 2_400
MAX_BRANCH_COLLISION_ATTEMPTS = 20
MAX_GIT_IDENTITY_NAME_LENGTH = 160
MAX_GIT_IDENTITY_EMAIL_LENGTH = 254
_GIT_SAFE_CONFIG = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
)
_GIT_OBJECT_RE = re.compile(r"[0-9a-f]{40,64}\Z")
BOOK_CHAPTER_COUNTS = (
    50,
    40,
    27,
    36,
    34,
    24,
    21,
    4,
    31,
    24,
    22,
    25,
    29,
    36,
    10,
    13,
    10,
    42,
    150,
    31,
    12,
    8,
    66,
    52,
    5,
    48,
    12,
    14,
    3,
    9,
    1,
    4,
    7,
    3,
    3,
    3,
    2,
    14,
    4,
    28,
    16,
    24,
    21,
    28,
    16,
    16,
    13,
    6,
    6,
    4,
    4,
    5,
    3,
    6,
    4,
    3,
    1,
    13,
    5,
    5,
    3,
    5,
    1,
    1,
    1,
    22,
)

_ANSI_ESCAPE_RE = re.compile(r"(?:\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-_])")
_TOPIC_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_COLOR_RE = re.compile(r"#[0-9a-f]{6}\Z")
_CANONICAL_ENGLISH_TOPIC_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9 &'():?-]*[A-Za-z0-9)]\Z"
)
_GIT_BRANCH_COMPONENT_RE = re.compile(r"[^a-z0-9-]+")


class ReviewError(RuntimeError):
    """A safe, operator-facing contribution review failure."""


class StoreProtocol(Protocol):
    """The contribution-store surface consumed by this terminal client."""

    def list_applications(
        self,
        *,
        states: set[str] | None = None,
        limit: int = 100,
    ) -> Sequence[object]: ...

    def decide_application(
        self,
        user_id: int,
        state: str,
        *,
        actor: str,
        note: str = "",
    ) -> object: ...

    def list_source_topics(
        self,
        *,
        states: set[str] | None = None,
        limit: int = 500,
    ) -> Sequence[object]: ...

    def set_topic_mapping(
        self,
        contributor_id: int,
        local_topic_id: str,
        *,
        state: str,
        actor: str,
        canonical_topic_id: str | None = None,
        note: str = "",
        canonical_definition: Mapping[str, object] | None = None,
        name: str | None = None,
        color: str | None = None,
        aliases: Sequence[str] | None = None,
    ) -> object: ...

    def list_events(
        self,
        *,
        states: set[str] | None = None,
        types: set[str] | None = None,
        limit: int = 500,
        after_id: int = 0,
    ) -> Sequence[object]: ...

    def list_canonical_topics(self) -> Sequence[object]: ...

    def decide_event(
        self,
        event_id: int,
        state: str,
        *,
        actor: str,
        canonical_topic_id: str | None = None,
        note: str = "",
    ) -> object: ...

    def publish_approved_events_atomically(
        self,
        catalog: Mapping[str, object],
        event_ids: Sequence[int],
        *,
        actor: str,
        expected_revision: int | None = None,
        expected_checksum: str | None = None,
    ) -> object: ...

    def current_catalog(self) -> object | None: ...

    def published_topic_ids(self) -> Sequence[str]: ...

    def publication_state(self) -> Mapping[str, object]: ...

    def begin_repo_publication(
        self,
        revision: int,
        checksum: str,
        *,
        actor: str,
        lease_seconds: int = 900,
    ) -> str: ...

    def finish_repo_publication(
        self,
        token: str,
        revision: int,
        *,
        state: str,
        actor: str,
        branch: str | None = None,
        commit: str | None = None,
        error: str | None = None,
    ) -> None: ...


class VerseClientProtocol(Protocol):
    def fetch_verses(
        self,
        references: Sequence[tuple[int, int, int]],
    ) -> Mapping[object, object]: ...


@dataclass(frozen=True, slots=True)
class CanonicalTopic:
    id: str
    name: str
    color: str
    aliases: tuple[str, ...] = ()

    @classmethod
    def validated(cls, value: object) -> CanonicalTopic:
        payload = _record_mapping(value)
        topic_id = validate_topic_slug(payload.get("id"))
        name = validate_english_topic_name(payload.get("name"))
        color = validate_topic_color(payload.get("color"))
        aliases = validate_aliases(payload.get("aliases", ()), name=name)
        if topic_slug(name) != topic_id:
            raise ReviewError("The topic id must be the derived slug of its English name.")
        return cls(topic_id, name, color, aliases)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True, slots=True)
class Association:
    topic_id: str
    book: int
    chapter: int
    verse: int

    @classmethod
    def validated(cls, value: object) -> Association:
        payload = _record_mapping(value)
        book = _bounded_integer(payload.get("book"), "book", 1, len(BOOK_CHAPTER_COUNTS))
        chapter = _bounded_integer(
            payload.get("chapter"),
            "chapter",
            1,
            BOOK_CHAPTER_COUNTS[book - 1],
        )
        return cls(
            validate_topic_slug(payload.get("topic_id")),
            book,
            chapter,
            _bounded_integer(payload.get("verse"), "verse", 1, 2000),
        )

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContributionBundle:
    topics: tuple[CanonicalTopic, ...]
    additions: tuple[Association, ...]
    removals: tuple[Association, ...]

    @classmethod
    def empty(cls) -> ContributionBundle:
        return cls((), (), ())

    @classmethod
    def validated(cls, value: object) -> ContributionBundle:
        payload = _record_mapping(value)
        if set(payload) != {"schema_version", "topics", "associations"}:
            raise ReviewError("The contribution bundle has unsupported or missing fields.")
        if payload["schema_version"] != SCHEMA_VERSION:
            raise ReviewError("The contribution bundle schema version is unsupported.")
        raw_topics = payload["topics"]
        raw_associations = _record_mapping(payload["associations"])
        if not isinstance(raw_topics, list) or set(raw_associations) != {"add", "remove"}:
            raise ReviewError("The contribution bundle collections are invalid.")
        additions = raw_associations["add"]
        removals = raw_associations["remove"]
        if not isinstance(additions, list) or not isinstance(removals, list):
            raise ReviewError("The contribution associations must be arrays.")
        topics = tuple(CanonicalTopic.validated(item) for item in raw_topics)
        add_values = tuple(Association.validated(item) for item in additions)
        remove_values = tuple(Association.validated(item) for item in removals)
        result = cls(topics, add_values, remove_values).normalized()
        if len({topic.id for topic in result.topics}) != len(result.topics):
            raise ReviewError("The contribution bundle contains duplicate topic ids.")
        if set(result.additions) & set(result.removals):
            raise ReviewError("One association cannot be both added and removed.")
        return result

    @classmethod
    def read(cls, path: Path) -> ContributionBundle:
        try:
            if path.stat().st_size > MAX_OVERLAY_BYTES:
                raise ReviewError("The contribution bundle exceeds 2 MiB.")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ReviewError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReviewError(f"The contribution bundle could not be read: {error}") from error
        return cls.validated(payload)

    def normalized(self) -> ContributionBundle:
        topics_by_id: dict[str, CanonicalTopic] = {}
        for raw_topic in self.topics:
            topic = CanonicalTopic.validated(raw_topic)
            existing = topics_by_id.get(topic.id)
            if existing is not None and existing != topic:
                raise ReviewError(f"Conflicting definitions were supplied for topic {topic.id}.")
            topics_by_id[topic.id] = topic
        normalized_topics = tuple(sorted(topics_by_id.values(), key=lambda item: item.id))
        _validate_topic_name_uniqueness(normalized_topics)
        return ContributionBundle(
            normalized_topics,
            tuple(
                sorted(
                    {Association.validated(item) for item in self.additions},
                    key=_association_sort_key,
                )
            ),
            tuple(
                sorted(
                    {Association.validated(item) for item in self.removals},
                    key=_association_sort_key,
                )
            ),
        )

    def as_dict(self) -> dict[str, object]:
        normalized = self.normalized()
        return {
            "schema_version": SCHEMA_VERSION,
            "topics": [topic.as_dict() for topic in normalized.topics],
            "associations": {
                "add": [association.as_dict() for association in normalized.additions],
                "remove": [association.as_dict() for association in normalized.removals],
            },
        }

    def json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.as_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.json_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class GitPublication:
    branch: str
    commit: str


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    bundle: ContributionBundle
    event_ids: tuple[int, ...]
    base_bundle: ContributionBundle
    base_revision: int
    base_checksum: str


@dataclass(frozen=True, slots=True)
class CatalogExport:
    bundle: ContributionBundle
    revision: int
    checksum: str


class GitPublisher:
    """Import one reviewed bundle and push a branch from ``origin/master``."""

    def __init__(
        self,
        *,
        checkout: Path,
        expected_user: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        stdout: TextIO = sys.stdout,
    ) -> None:
        self._configured_checkout = checkout
        self.checkout = checkout.resolve()
        self.expected_user = expected_user
        self._run_process = runner
        self.stdout = stdout
        self._deadline: float | None = None

    def publish(
        self,
        bundle_path: Path,
        *,
        expected_bundle_checksum: str | None = None,
    ) -> GitPublication:
        self._deadline = time.monotonic() + GIT_PUBLICATION_TIMEOUT_SECONDS
        bundle = ContributionBundle.read(bundle_path.resolve())
        if expected_bundle_checksum is not None and (
            re.fullmatch(r"[0-9a-f]{64}", expected_bundle_checksum) is None
            or not hmac.compare_digest(bundle.checksum, expected_bundle_checksum)
        ):
            raise ReviewError("The reviewed bundle does not match its publication lease.")
        self._validate_identity()
        self._validate_checkout()
        self._validate_git_identity()
        self._validate_tooling()
        self._git(
            "fetch",
            "--no-tags",
            "--prune",
            "origin",
            "+refs/heads/master:refs/remotes/origin/master",
        )
        self._require_clean()
        base = self._git_output("rev-parse", "--verify", "refs/remotes/origin/master^{commit}")
        fetched = self._git_output("rev-parse", "--verify", "FETCH_HEAD^{commit}")
        if _GIT_OBJECT_RE.fullmatch(base) is None or not hmac.compare_digest(base, fetched):
            raise ReviewError("The fetched origin/master result could not be verified.")
        branch = self._unique_branch(bundle.checksum)
        self._git("switch", "--create", branch, base)
        try:
            self._require_clean()
            importer = self.checkout / "scripts" / "import_contribution_bundle.mjs"
            self._command("node", str(importer), str(bundle_path.resolve()))
            self._command("npm", "--prefix", "miniapp", "run", "check")
            changed = self._changed_paths()
            if not changed:
                raise ReviewError("The reviewed bundle makes no repository changes.")
            self._validate_changed_paths(changed)
            self._git("add", "--", *changed)
            self._git("diff", "--cached", "--check")
            self._validate_staged_files(changed)
            expected_tree = self._git_output("write-tree")
            if _GIT_OBJECT_RE.fullmatch(expected_tree) is None:
                raise ReviewError("Git did not produce a valid staged publication tree.")
            self._git("commit", "--message", self._commit_message(bundle))
            commit = self._git_output("rev-parse", "--verify", "HEAD^{commit}")
            committed_tree = self._git_output("rev-parse", "--verify", "HEAD^{tree}")
            parents = self._git_output("rev-list", "--parents", "--max-count=1", "HEAD").split()
            if _GIT_OBJECT_RE.fullmatch(commit) is None:
                raise ReviewError("Git did not produce a valid publication commit.")
            if not hmac.compare_digest(committed_tree, expected_tree):
                raise ReviewError("The publication commit tree changed after validation.")
            if len(parents) != 2 or parents[0] != commit or parents[1] != base:
                raise ReviewError("The publication commit is not based directly on origin/master.")
            self._validate_changed_paths(self._committed_paths())
            self._require_clean()
            self._validate_checkout_filesystem()
            self._validate_origin()
            self._git("push", "--set-upstream", "origin", branch)
        except Exception:
            # The branch and imported files are deliberately retained for
            # diagnosis.  Never reset or discard reviewed catalogue work.
            raise
        self.stdout.write(f"Pushed {branch} at {commit}.\n")
        return GitPublication(branch, commit)

    def _validate_identity(self) -> None:
        if not self.expected_user or self.expected_user == "root":
            raise ReviewError("A dedicated non-root Git publisher user is required.")
        if os.geteuid() == 0:
            raise ReviewError("Repository publication refuses to run as root.")
        try:
            import pwd

            actual = pwd.getpwuid(os.geteuid()).pw_name
        except (ImportError, KeyError) as error:
            raise ReviewError("The current operating-system user could not be verified.") from error
        if actual != self.expected_user:
            raise ReviewError(
                f"Repository publication must run as {self.expected_user}, not {actual}."
            )

    def _validate_checkout(self) -> None:
        if not self._configured_checkout.is_absolute():
            raise ReviewError("The configured Git checkout must be an absolute directory.")
        self._validate_checkout_filesystem()
        inside = self._git_output("rev-parse", "--is-inside-work-tree")
        if inside != "true":
            raise ReviewError("The configured path is not a Git work tree.")
        top_level = Path(
            self._git_output("rev-parse", "--path-format=absolute", "--show-toplevel")
        ).resolve()
        git_directory = Path(self._git_output("rev-parse", "--absolute-git-dir")).resolve()
        common_directory = Path(
            self._git_output("rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        expected_git_directory = self.checkout / ".git"
        if (
            top_level != self.checkout
            or git_directory != expected_git_directory
            or common_directory != expected_git_directory
        ):
            raise ReviewError("The configured checkout must be a standalone Git work tree.")
        self._validate_origin()
        self._require_clean()

    def _validate_git_identity(self) -> None:
        name = self._local_git_identity(
            "user.name",
            maximum=MAX_GIT_IDENTITY_NAME_LENGTH,
        )
        email = self._local_git_identity(
            "user.email",
            maximum=MAX_GIT_IDENTITY_EMAIL_LENGTH,
        )
        if "<" in name or ">" in name:
            raise ReviewError("The publisher checkout has an unsafe local Git user.name.")
        if (
            "<" in email
            or ">" in email
            or any(character.isspace() for character in email)
            or email.count("@") != 1
            or email.startswith("@")
            or email.endswith("@")
        ):
            raise ReviewError("The publisher checkout has an unsafe local Git user.email.")

    def _local_git_identity(self, key: str, *, maximum: int) -> str:
        result = self._git_probe("config", "--local", "--get", key)
        if result.returncode == 1:
            raise ReviewError(f"The publisher checkout requires local Git {key}.")
        if result.returncode != 0:
            raise ReviewError(f"Git could not inspect local Git {key}.")
        value = result.stdout or ""
        if value.endswith("\n"):
            value = value[:-1]
        if (
            not value
            or len(value) > maximum
            or value != value.strip()
            or any(
                (character.isspace() and character != " ")
                or unicodedata.category(character).startswith("C")
                for character in value
            )
        ):
            raise ReviewError(f"The publisher checkout has an unsafe local Git {key}.")
        return value

    def _validate_checkout_filesystem(self) -> None:
        expected_uid = os.geteuid()
        allowed_ancestor_uids = {0, expected_uid}
        current = Path(self._configured_checkout.anchor)
        for component in self._configured_checkout.parts[1:]:
            current /= component
            metadata = self._secure_metadata(current, directory=True)
            if metadata.st_uid not in allowed_ancestor_uids:
                raise ReviewError("A publisher checkout ancestor has an unsafe owner.")
            unsafe_write = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            sticky_root = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
            if unsafe_write and not sticky_root:
                raise ReviewError("A publisher checkout ancestor is group- or other-writable.")
        if self._configured_checkout.resolve() != self.checkout:
            raise ReviewError("The configured Git checkout path changed during validation.")

        directories = (
            self.checkout,
            self.checkout / ".git",
            self.checkout / "scripts",
            self.checkout / "data",
            self.checkout / "data" / "global-bookmarks",
            self.checkout / "miniapp",
            self.checkout / "miniapp" / "lib",
        )
        files = (
            self.checkout / ".git" / "HEAD",
            self.checkout / ".git" / "config",
            self.checkout / ".git" / "index",
            self.checkout / "bot.py",
            self.checkout / "setup.sh",
            self.checkout / "scripts" / "import_contribution_bundle.mjs",
            self.checkout / "data" / "global-bookmarks" / "tag-verse.csv",
            self.checkout / "data" / "global-bookmarks" / "topics.json",
            self.checkout / "miniapp" / "package.json",
            self.checkout / "miniapp" / "lib" / "bookmark-topic-definitions.js",
            self.checkout / "miniapp" / "lib" / "global-bookmark-data.js",
            self.checkout / "miniapp" / "lib" / "messages.en.js",
        )
        for path in directories:
            metadata = self._secure_metadata(path, directory=True)
            self._require_publisher_owned(path, metadata, expected_uid)
        for path in files:
            metadata = self._secure_metadata(path, directory=False)
            self._require_publisher_owned(path, metadata, expected_uid)

    @staticmethod
    def _secure_metadata(path: Path, *, directory: bool) -> os.stat_result:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ReviewError("A required publisher checkout path is unavailable.") from error
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_type(metadata.st_mode):
            label = "directory" if directory else "regular file"
            raise ReviewError(f"A required publisher checkout path is not a {label}: {path}.")
        return metadata

    @staticmethod
    def _require_publisher_owned(path: Path, metadata: os.stat_result, uid: int) -> None:
        if metadata.st_uid != uid:
            raise ReviewError(f"A required publisher checkout path has an unsafe owner: {path}.")
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ReviewError(
                f"A required publisher checkout path is group- or other-writable: {path}."
            )

    def _validate_origin(self) -> None:
        for arguments, label in (
            (("remote", "get-url", "--all", "origin"), "fetch"),
            (("remote", "get-url", "--push", "--all", "origin"), "push"),
        ):
            urls = [
                line.strip() for line in self._git_output(*arguments).splitlines() if line.strip()
            ]
            if not urls or any(
                _github_repository(url) != EXPECTED_GITHUB_REPOSITORY for url in urls
            ):
                raise ReviewError(
                    f"The configured origin {label} URL is not the canonical "
                    "getbible/robot repository."
                )

    def _require_clean(self) -> None:
        self._validate_index_mode()
        status = self._git_output("status", "--porcelain=v1", "--untracked-files=all")
        if status:
            raise ReviewError(
                "The publisher checkout is not clean; preserve or remove its changes first."
            )

    def _validate_index_mode(self) -> None:
        for key in ("core.sparseCheckout", "core.sparseCheckoutCone"):
            result = self._git_probe("config", "--bool", "--get", key)
            if result.returncode == 1:
                continue
            if result.returncode != 0 or result.stdout.strip() not in {"true", "false"}:
                raise ReviewError("Git could not inspect sparse-checkout configuration.")
            if result.stdout.strip() == "true":
                raise ReviewError("The publisher checkout cannot use sparse checkout.")
        records = self._git_output("ls-files", "-v", "-z").split("\0")
        unsafe = []
        for record in records:
            if not record:
                continue
            if len(record) < 3 or record[1] != " ":
                raise ReviewError("Git returned unreadable index flags.")
            marker = record[0]
            if marker == "S" or marker.islower():
                unsafe.append(record[2:])
        if unsafe:
            labels = ", ".join(sanitize_terminal(path, maximum=120) for path in unsafe[:5])
            raise ReviewError(
                f"The publisher checkout uses skip-worktree or assume-unchanged flags: {labels}."
            )

    def _validate_tooling(self) -> None:
        node_version = self._command("node", "--version").stdout.strip()
        match = re.fullmatch(r"v(\d+)\.\d+\.\d+", node_version)
        if match is None or int(match.group(1)) < 22:
            raise ReviewError(
                "Repository publication requires Node.js 22 or newer (matching project CI)."
            )
        npm_version = self._command("npm", "--version").stdout.strip()
        if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", npm_version) is None:
            raise ReviewError("Repository publication requires a usable npm installation.")

    def _unique_branch(self, checksum: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        stem = f"contributions/{timestamp}-{checksum[:10]}"
        for suffix in range(1, MAX_BRANCH_COLLISION_ATTEMPTS + 1):
            branch = stem if suffix == 1 else f"{stem}-{suffix}"
            local_code = self._git_returncode(
                "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
            )
            if local_code not in {0, 1}:
                raise ReviewError("Git could not inspect local publication branches.")
            remote_code = self._git_returncode(
                "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"
            )
            if remote_code not in {0, 2}:
                raise ReviewError("Git could not inspect remote publication branches.")
            if local_code == 1 and remote_code == 2:
                return branch
        raise ReviewError("Too many contribution publication branch names already exist.")

    def _changed_paths(self) -> list[str]:
        self._validate_index_mode()
        output = self._command(
            "git",
            "-C",
            str(self.checkout),
            *_GIT_SAFE_CONFIG,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "-z",
        ).stdout
        return parse_porcelain_paths(output)

    def _validate_staged_files(self, paths: Sequence[str]) -> None:
        for relative in paths:
            path = self.checkout / relative
            metadata = self._secure_metadata(path, directory=False)
            self._require_publisher_owned(path, metadata, os.geteuid())
            worktree_blob = self._git_output("hash-object", "--no-filters", "--", relative)
            index_blob = self._git_output("rev-parse", "--verify", f":{relative}")
            if _GIT_OBJECT_RE.fullmatch(worktree_blob) is None or not hmac.compare_digest(
                worktree_blob, index_blob
            ):
                raise ReviewError(
                    f"The staged publication content differs from the validated file: {relative}."
                )

    def _committed_paths(self) -> list[str]:
        output = self._command(
            "git",
            "-C",
            str(self.checkout),
            *_GIT_SAFE_CONFIG,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            "HEAD",
        ).stdout
        return sorted({path for path in output.split("\0") if path})

    @staticmethod
    def _validate_changed_paths(paths: Sequence[str]) -> None:
        allowed_exact = {
            "data/global-bookmarks/tag-verse.csv",
            "data/global-bookmarks/topics.json",
            "miniapp/lib/bookmark-topic-definitions.js",
            "miniapp/lib/global-bookmark-data.js",
            "miniapp/lib/messages.en.js",
        }
        unexpected = [path for path in paths if path not in allowed_exact]
        if unexpected:
            labels = ", ".join(sanitize_terminal(path, maximum=160) for path in unexpected)
            raise ReviewError(f"The importer changed unexpected paths: {labels}.")

    @staticmethod
    def _commit_message(bundle: ContributionBundle) -> str:
        association_count = len(bundle.additions) + len(bundle.removals)
        return (
            "Update reviewed global bookmark contributions\n\n"
            f"Topics: {len(bundle.topics)}\n"
            f"Verse associations: {association_count}\n"
            f"Bundle SHA-256: {bundle.checksum}\n"
        )

    def _git(self, *arguments: str) -> None:
        self._command("git", "-C", str(self.checkout), *_GIT_SAFE_CONFIG, *arguments)

    def _git_output(self, *arguments: str) -> str:
        return self._command(
            "git", "-C", str(self.checkout), *_GIT_SAFE_CONFIG, *arguments
        ).stdout.strip()

    def _git_returncode(self, *arguments: str) -> int:
        return self._git_probe(*arguments).returncode

    def _git_probe(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return self._run_process(
                ["git", "-C", str(self.checkout), *_GIT_SAFE_CONFIG, *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=self._remaining_timeout(120),
                env=self._command_environment(),
            )
        except FileNotFoundError as error:
            raise ReviewError("Required command is unavailable: git.") from error
        except subprocess.TimeoutExpired as error:
            raise ReviewError("Git exceeded its non-interactive publication timeout.") from error

    def _command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return self._run_process(
                list(arguments),
                cwd=self.checkout,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._remaining_timeout(600),
                env=self._command_environment(),
            )
        except FileNotFoundError as error:
            raise ReviewError(f"Required command is unavailable: {arguments[0]}.") from error
        except subprocess.CalledProcessError as error:
            detail = sanitize_terminal(
                (error.stderr or error.stdout or "command failed").strip(),
                maximum=1000,
            )
            raise ReviewError(f"{arguments[0]} failed: {detail}") from error
        except subprocess.TimeoutExpired as error:
            raise ReviewError(f"{arguments[0]} exceeded its non-interactive timeout.") from error

    @staticmethod
    def _command_environment() -> dict[str, str]:
        return {
            **os.environ,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def _remaining_timeout(self, maximum: int) -> float:
        if self._deadline is None:
            return float(maximum)
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise ReviewError("Repository publication exceeded its total time limit.")
        return min(float(maximum), remaining)


def parse_porcelain_paths(output: str) -> list[str]:
    """Parse Git porcelain-v1 ``-z`` output without stripping path bytes."""

    if not output:
        return []
    records = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise ReviewError("Git returned an unreadable work-tree status.")
        status = record[:2]
        path = record[3:]
        if "R" in status or "C" in status:
            if index >= len(records) or not records[index]:
                raise ReviewError("Git returned an incomplete renamed path.")
            _original_path = records[index]
            index += 1
            raise ReviewError("The catalogue importer unexpectedly renamed or copied a path.")
        paths.append(path)
    return sorted(set(paths))


def sanitize_terminal(value: object, *, maximum: int = MAX_DISPLAY_TEXT) -> str:
    """Return bounded single-line text that cannot control an operator terminal."""

    text = _ANSI_ESCAPE_RE.sub("", str(value or ""))
    normalized: list[str] = []
    for character in unicodedata.normalize("NFC", text):
        category = unicodedata.category(character)
        if character in "\r\n\t" or category.startswith("C"):
            normalized.append(" ")
        else:
            normalized.append(character)
    result = " ".join("".join(normalized).split())
    if len(result) > maximum:
        return result[: max(0, maximum - 1)].rstrip() + "…"
    return result


def validate_english_topic_name(value: object) -> str:
    name = sanitize_terminal(value, maximum=MAX_TOPIC_NAME_LENGTH + 1).strip()
    if not 2 <= len(name) <= MAX_TOPIC_NAME_LENGTH:
        raise ReviewError(
            f"The English topic name must contain 2-{MAX_TOPIC_NAME_LENGTH} characters."
        )
    return _canonical_topic_label(name)


def validate_topic_slug(value: object) -> str:
    slug = str(value or "").strip().casefold()
    if not 2 <= len(slug) <= 80 or _TOPIC_SLUG_RE.fullmatch(slug) is None:
        raise ReviewError("The topic id must be a 2-80 character lowercase English slug.")
    return slug


def topic_slug(name: str) -> str:
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = slug.casefold().replace("'", "")
    slug = _GIT_BRANCH_COMPONENT_RE.sub("-", slug).strip("-")
    return validate_topic_slug(slug)


def validate_topic_color(value: object) -> str:
    color = str(value or "").strip().casefold()
    if _COLOR_RE.fullmatch(color) is None:
        raise ReviewError("The topic color must use the #rrggbb form.")
    return color


def validate_aliases(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ReviewError("Topic aliases must be a list.")
    if len(value) > MAX_ALIAS_COUNT:
        raise ReviewError(f"A topic can have at most {MAX_ALIAS_COUNT} aliases.")
    aliases: list[str] = []
    for item in value:
        alias = sanitize_terminal(item, maximum=MAX_ALIAS_LENGTH + 1)
        if not 2 <= len(alias) <= MAX_ALIAS_LENGTH:
            raise ReviewError("Each English topic alias must contain 2-80 characters.")
        aliases.append(_canonical_topic_label(alias))
    canonical_name = validate_english_topic_name(name)
    if len({alias.casefold() for alias in aliases}) != len(aliases):
        raise ReviewError(
            "English topic aliases must use canonical formatting and be unique."
        )
    if any(alias.casefold() == canonical_name.casefold() for alias in aliases):
        raise ReviewError("An English topic alias must differ from the topic name.")
    return tuple(sorted(aliases, key=lambda item: (item.casefold(), item)))


def _canonical_topic_label(value: str) -> str:
    """Mirror the store's canonical English label contract.

    Repository publication executes a verified standalone copy of this script,
    so bundle validation must not import application code across that privilege
    boundary. Parity tests exercise these rules against ``ContributionStore``.
    """

    if (
        value != value.strip()
        or value != unicodedata.normalize("NFC", value)
        or re.search(r"\s{2,}", value) is not None
        or _CANONICAL_ENGLISH_TOPIC_RE.fullmatch(value) is None
        or re.search(r"[A-Za-z]", value) is None
    ):
        raise ReviewError(
            "The English topic name contains unsupported characters or formatting."
        )
    return value


def _validate_topic_name_uniqueness(topics: Sequence[CanonicalTopic]) -> None:
    """Match the repository importer's base+overlay name/alias invariant."""

    owners: dict[str, str] = {}
    for topic in topics:
        for label in (topic.name, *topic.aliases):
            normalized = " ".join(label.lower().split())
            owner = owners.get(normalized)
            if owner is not None:
                raise ReviewError(
                    "Canonical topics reuse an English name or alias: "
                    f"{label} ({owner} and {topic.id})."
                )
            owners[normalized] = topic.id


def atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Durably replace ``path`` without exposing a partial catalogue/export."""

    destination = path.resolve()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


def _record_mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return cast(dict[str, object], asdict(value))
    slots = getattr(value, "__slots__", ())
    if slots:
        return {name: getattr(value, name) for name in slots if hasattr(value, name)}
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return dict(attributes)
    raise ReviewError("A contribution record could not be interpreted.")


def _bounded_integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ReviewError(f"{label} is invalid.")
    try:
        result = int(cast(Any, value))
    except (TypeError, ValueError) as error:
        raise ReviewError(f"{label} is invalid.") from error
    if not minimum <= result <= maximum:
        raise ReviewError(f"{label} is outside the supported range.")
    return result


def _association_sort_key(value: Association) -> tuple[str, int, int, int]:
    return (value.topic_id, value.book, value.chapter, value.verse)


def _github_repository(url: str) -> str | None:
    candidate = url.strip()
    patterns = (
        r"https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?\Z",
        r"ssh://git@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?\Z",
        r"git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?\Z",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, candidate, flags=re.IGNORECASE)
        if match:
            return match.group("repo").removesuffix(".git").casefold()
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("applications", "topics", "verses", "status", "publish-live"):
        command = subparsers.add_parser(name)
        command.add_argument("--store", type=Path, required=True)
        command.add_argument("--actor", required=True)
        command.add_argument("--topics-file", type=Path)
        command.add_argument("--associations-file", type=Path)
        command.add_argument("--translation", default=DEFAULT_TRANSLATION)

    export = subparsers.add_parser("export")
    export.add_argument("--store", type=Path, required=True)
    export.add_argument("--actor", required=True)
    export.add_argument("--output", type=Path, required=True)

    begun = subparsers.add_parser("begin-repository-publication")
    begun.add_argument("--store", type=Path, required=True)
    begun.add_argument("--actor", required=True)
    begun.add_argument("--revision", type=int, required=True)
    begun.add_argument("--checksum", required=True)
    begun.add_argument("--lease-seconds", type=int, default=900)

    finished = subparsers.add_parser("finish-repository-publication")
    finished.add_argument("--store", type=Path, required=True)
    finished.add_argument("--actor", required=True)
    finished.add_argument("--lease-token", required=True)
    finished.add_argument("--revision", type=int, required=True)
    finished.add_argument("--state", choices=("pushed", "failed"), required=True)
    finished.add_argument("--branch")
    finished.add_argument("--commit")
    finished.add_argument("--error")

    repository = subparsers.add_parser("publish-repository")
    repository.add_argument("--bundle", type=Path, required=True)
    repository.add_argument("--checkout", type=Path, required=True)
    repository.add_argument("--expected-user", required=True)
    repository.add_argument("--expected-bundle-checksum", required=True)
    return parser


def _load_store(path: Path) -> StoreProtocol:
    if not path.is_absolute():
        raise ReviewError("The contribution store path must be absolute.")
    if path.is_symlink():
        raise ReviewError("The contribution store cannot be a symbolic link.")
    if path.exists():
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                raise ReviewError("The contribution store must be a regular file.")
        except OSError as error:
            raise ReviewError("The contribution store could not be inspected.") from error
    elif path.parent.is_symlink() or not path.parent.is_dir():
        raise ReviewError("The contribution store parent must be a real directory.")
    try:
        from modules.contributions import ContributionStore
    except ImportError as error:
        raise ReviewError("The deployed application does not support contributions.") from error
    try:
        return cast(StoreProtocol, ContributionStore(path=str(path)))
    except (sqlite3.Error, ValueError) as error:
        raise ReviewError("The contribution store could not be opened safely.") from error


def _fatal(message: object) -> NoReturn:
    sys.stderr.write(f"ERROR: {sanitize_terminal(message, maximum=1500)}\n")
    raise SystemExit(1)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "publish-repository":
            publication = GitPublisher(
                checkout=arguments.checkout,
                expected_user=arguments.expected_user,
            ).publish(
                arguments.bundle,
                expected_bundle_checksum=arguments.expected_bundle_checksum,
            )
            print(json.dumps(asdict(publication), sort_keys=True, separators=(",", ":")))
            return 0
        store = _load_store(arguments.store)
        if arguments.command == "applications":
            review_applications(store, actor=arguments.actor)
        elif arguments.command == "topics":
            review_topics(store, actor=arguments.actor, topics_file=arguments.topics_file)
        elif arguments.command == "verses":
            review_verses(
                store,
                actor=arguments.actor,
                translation=arguments.translation,
                topics_file=arguments.topics_file,
                associations_file=arguments.associations_file,
            )
        elif arguments.command == "status":
            print_status(store)
        elif arguments.command == "publish-live":
            publish_live(
                store,
                actor=arguments.actor,
                topics_file=arguments.topics_file,
                associations_file=arguments.associations_file,
            )
        elif arguments.command == "export":
            exported = export_current_catalog(store, arguments.output)
            print(
                json.dumps(
                    {
                        "checksum": exported.checksum,
                        "bundle_checksum": exported.bundle.checksum,
                        "path": str(arguments.output.resolve()),
                        "revision": exported.revision,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif arguments.command == "begin-repository-publication":
            token = store.begin_repo_publication(
                arguments.revision,
                arguments.checksum,
                actor=arguments.actor,
                lease_seconds=arguments.lease_seconds,
            )
            print(json.dumps({"token": token}, sort_keys=True, separators=(",", ":")))
        elif arguments.command == "finish-repository-publication":
            store.finish_repo_publication(
                arguments.lease_token,
                arguments.revision,
                state=arguments.state,
                branch=arguments.branch,
                commit=arguments.commit,
                error=arguments.error,
                actor=arguments.actor,
            )
        else:  # pragma: no cover - argparse prevents this path
            raise ReviewError("Unknown contribution-review command.")
    except (ReviewError, OSError, sqlite3.Error, ValueError) as error:
        _fatal(error)
    return 0


# Interactive review operations are defined below.  Keeping them outside
# ``main`` makes the state transitions directly unit-testable with an in-memory
# fake store and scripted prompts.


def review_applications(
    store: StoreProtocol,
    *,
    actor: str,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
) -> None:
    applications = list(store.list_applications(states={"pending", "deferred"}, limit=500))
    if not applications:
        output.write("No pending or deferred contributor applications.\n")
    for application in applications:
        payload = _record_mapping(application)
        user_id = _bounded_integer(payload.get("user_id"), "contributor id", 1, 2**63 - 1)
        output.write("\nContributor application\n")
        output.write(f"  Telegram ID: {user_id}\n")
        output.write(f"  Name: {_display_person(payload)}\n")
        username = sanitize_terminal(payload.get("username") or "not supplied")
        output.write(f"  Username: {username}\n")
        submitted = payload.get("requested_at") or payload.get("created_at")
        output.write(f"  Submitted: {_format_timestamp(submitted)}\n")
        action = _choice(
            "Approve, reject, defer, or stop? [a/r/d/s]: ",
            {"a", "r", "d", "s"},
            input_fn,
        )
        if action == "s":
            return
        states = {"a": "approved", "r": "rejected", "d": "deferred"}
        note = _optional_note(input_fn)
        store.decide_application(user_id, states[action], actor=_actor(actor), note=note)
        output.write(f"Application {states[action]}; notification queued.\n")

    if _yes_no("Review enrolled contributors for revocation? [y/N]: ", input_fn):
        approved = list(store.list_applications(states={"approved"}, limit=1000))
        if not approved:
            output.write("No enrolled contributors.\n")
        for application in approved:
            payload = _record_mapping(application)
            user_id = _bounded_integer(
                payload.get("user_id"), "contributor id", 1, 2**63 - 1
            )
            output.write("\nEnrolled contributor\n")
            output.write(f"  Telegram ID: {user_id}\n")
            output.write(f"  Name: {_display_person(payload)}\n")
            username = sanitize_terminal(payload.get("username") or "not supplied")
            output.write(f"  Username: {username}\n")
            action = _choice("Keep, revoke, or stop? [k/r/s]: ", {"k", "r", "s"}, input_fn)
            if action == "s":
                return
            if action == "r":
                store.decide_application(
                    user_id,
                    "revoked",
                    actor=_actor(actor),
                    note=_optional_note(input_fn),
                )
                output.write("Contributor access revoked; notification queued.\n")

    # A removed contributor's record stays in the store keyed by their
    # Telegram ID, and a fresh /contributor request never resets a revoked or
    # rejected state on its own — reinstatement is deliberately an explicit
    # operator decision made here.
    if not _yes_no(
        "Reinstate previously revoked or rejected contributors? [y/N]: ",
        input_fn,
    ):
        return
    removed = list(store.list_applications(states={"revoked", "rejected"}, limit=1000))
    if not removed:
        output.write("No revoked or rejected contributors.\n")
        return
    for application in removed:
        payload = _record_mapping(application)
        user_id = _bounded_integer(payload.get("user_id"), "contributor id", 1, 2**63 - 1)
        output.write("\nRemoved contributor\n")
        output.write(f"  Telegram ID: {user_id}\n")
        output.write(f"  Name: {_display_person(payload)}\n")
        username = sanitize_terminal(payload.get("username") or "not supplied")
        output.write(f"  Username: {username}\n")
        state = sanitize_terminal(str(payload.get("state") or "unknown"))
        output.write(f"  State: {state}\n")
        action = _choice(
            "Reinstate, keep removed, or stop? [r/k/s]: ",
            {"r", "k", "s"},
            input_fn,
        )
        if action == "s":
            return
        if action == "r":
            store.decide_application(
                user_id,
                "approved",
                actor=_actor(actor),
                note=_optional_note(input_fn),
            )
            output.write(
                "Contributor reinstated; the enrolment notification is queued "
                "and the contribution disclosure must be acknowledged again "
                "before new submissions.\n"
            )


def review_topics(
    store: StoreProtocol,
    *,
    actor: str,
    topics_file: Path | None,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
) -> None:
    canonical = _load_canonical_topics(topics_file)
    locked_topic_ids = _published_topic_ids(store)
    pending = list(store.list_source_topics(states={"pending", "deferred"}, limit=1000))
    if not pending:
        output.write("No unresolved contributor topics.\n")
    bundled_topic_ids = set(canonical)
    for stored in store.list_canonical_topics():
        definition = CanonicalTopic.validated(stored)
        canonical[definition.id] = definition
    resolved_during_merge: set[tuple[int, str]] = set()
    for source in pending:
        payload = _record_mapping(source)
        contributor_id = _bounded_integer(
            payload.get("contributor_id"), "contributor id", 1, 2**63 - 1
        )
        local_topic_id = sanitize_terminal(payload.get("local_topic_id"), maximum=100)
        previous_canonical_value = payload.get("canonical_topic_id")
        previous_canonical = (
            validate_topic_slug(previous_canonical_value) if previous_canonical_value else None
        )
        if (contributor_id, local_topic_id) in resolved_during_merge:
            continue
        output.write("\nContributor topic\n")
        output.write(f"  Contributor: {contributor_id}\n")
        output.write(f"  Local id: {local_topic_id}\n")
        output.write(f"  Proposed English name: {sanitize_terminal(payload.get('name'))}\n")
        output.write(f"  Proposed color: {sanitize_terminal(payload.get('color'))}\n")
        aliases = payload.get("aliases") or ()
        if isinstance(aliases, (list, tuple)):
            output.write(
                "  Proposed aliases: "
                + (", ".join(sanitize_terminal(item) for item in aliases) or "none")
                + "\n"
            )
        suggested = canonical.get(local_topic_id)
        if suggested is not None:
            output.write(f"  This local id exactly matches bundled topic {suggested.name}.\n")
            if payload.get("name") or payload.get("color") or payload.get("aliases"):
                output.write(
                    "  Proposed metadata will be ignored; the bundled English definition "
                    "and color remain authoritative.\n"
                )
            if _yes_no("Map to that authoritative bundled topic? [Y/n]: ", input_fn, default=True):
                store.set_topic_mapping(
                    contributor_id,
                    local_topic_id,
                    state="mapped",
                    actor=_actor(actor),
                    canonical_topic_id=suggested.id,
                    name=suggested.name,
                    color=suggested.color,
                    aliases=suggested.aliases,
                )
                output.write(f"Topic mapped to {suggested.id}.\n")
                continue
        output.write(
            "  Actions: [e] existing, [m] merge with pending, [n] new/corrected, "
            "[r] reject, [d] defer, [s] stop\n"
        )
        action = _choice("Selection: ", {"e", "m", "n", "r", "d", "s"}, input_fn)
        if action == "s":
            return
        if previous_canonical in locked_topic_ids and action in {"m", "n", "r", "d"}:
            raise ReviewError(
                "A published source mapping cannot be replaced, rejected, or deferred. "
                "Map it to the existing canonical topic, then review the proposed topic event."
            )
        if action in {"r", "d"}:
            state = "rejected" if action == "r" else "deferred"
            store.set_topic_mapping(
                contributor_id,
                local_topic_id,
                state=state,
                actor=_actor(actor),
                note=_optional_note(input_fn),
            )
            _decide_related_topic_events(
                store,
                contributor_id=contributor_id,
                local_topic_id=local_topic_id,
                state=state,
                actor=actor,
            )
            output.write(f"Topic {state}.\n")
            continue
        merged_source: tuple[int, str] | None = None
        if action == "e":
            chosen = _select_topic(canonical, input_fn=input_fn, output=output)
            definition = canonical[chosen]
        elif action == "m":
            chosen, definition, merged_source = _select_pending_mapping(
                store,
                source,
                canonical_topics=canonical,
                input_fn=input_fn,
                output=output,
            )
        else:
            proposed = CanonicalTopic(
                id=topic_slug(validate_english_topic_name(payload.get("name"))),
                name=validate_english_topic_name(payload.get("name")),
                color=validate_topic_color(payload.get("color")),
                aliases=validate_aliases(
                    payload.get("aliases") or (),
                    name=str(payload.get("name")),
                ),
            )
            new_topic = _prompt_topic_definition(proposed, input_fn=input_fn)
            chosen = new_topic.id
            definition = new_topic
        existing_definition = canonical.get(chosen)
        if existing_definition is not None and existing_definition != definition:
            raise ReviewError(
                "That topic id already exists with a different definition; "
                "map to its authoritative definition explicitly."
            )
        _validate_topic_name_uniqueness(
            tuple(topic for topic_id, topic in canonical.items() if topic_id != chosen)
            + (definition,)
        )
        _assert_stable_published_topic(
            previous_canonical=previous_canonical,
            chosen=chosen,
            definition=definition,
            canonical_topics=canonical,
            locked_topic_ids=locked_topic_ids,
        )
        if merged_source is not None:
            merged_contributor, merged_local_id = merged_source
            store.set_topic_mapping(
                merged_contributor,
                merged_local_id,
                state="mapped",
                actor=_actor(actor),
                canonical_topic_id=chosen,
                canonical_definition=(
                    None if chosen in bundled_topic_ids else definition.as_dict()
                ),
                name=definition.name,
                color=definition.color,
                aliases=definition.aliases,
            )
        store.set_topic_mapping(
            contributor_id,
            local_topic_id,
            state="mapped",
            actor=_actor(actor),
            canonical_topic_id=chosen,
            note="",
            name=definition.name,
            color=definition.color,
            aliases=definition.aliases,
            canonical_definition=(None if chosen in bundled_topic_ids else definition.as_dict()),
        )
        canonical[chosen] = definition
        if merged_source is not None:
            resolved_during_merge.add(merged_source)
        output.write(f"Topic mapped to {chosen}.\n")
    _review_topic_events(
        store,
        actor=actor,
        bundled_topics=bundled_topic_ids,
        canonical_topics=canonical,
        locked_topic_ids=locked_topic_ids,
        input_fn=input_fn,
        output=output,
    )


def review_verses(
    store: StoreProtocol,
    *,
    actor: str,
    translation: str,
    topics_file: Path | None = None,
    associations_file: Path | None = None,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
    verse_client: VerseClientProtocol | None = None,
) -> None:
    unresolved = list(store.list_source_topics(states={"pending", "deferred"}, limit=1))
    if unresolved:
        raise ReviewError("Resolve all pending contributor topics before reviewing verses.")
    pending_verse_events = _all_events(
        store,
        states={"pending", "deferred"},
        types={"verse_add", "verse_remove"},
    )
    events = pending_verse_events[:MAX_EVENTS_PER_REVIEW]
    if not events:
        output.write("No pending or deferred verse changes.\n")
        return
    if topics_file is None or associations_file is None:
        raise ReviewError(
            "Bundled topic and association sources are required for duplicate checks."
        )
    base_topics = _load_canonical_topics(topics_file)
    base_associations = _load_base_associations(topics_file, associations_file)
    current = ContributionBundle.validated(_catalog_payload(store.current_catalog()))
    permanent_topic_ids = set(base_topics) | _published_topic_ids(store)
    known_live_topics = set(base_topics) | {topic.id for topic in current.topics}
    topic_dependencies: dict[tuple[int, str], list[Mapping[str, object]]] = {}
    canonical_topic_dependencies: dict[str, list[Mapping[str, object]]] = {}
    for dependency in _all_events(store, types={"topic_upsert", "topic_delete"}):
        dependency_payload = _record_mapping(dependency)
        dependency_key = (
            _bounded_integer(
                dependency_payload.get("contributor_id"),
                "contributor id",
                1,
                2**63 - 1,
            ),
            sanitize_terminal(dependency_payload.get("local_topic_id"), maximum=128),
        )
        topic_dependencies.setdefault(dependency_key, []).append(dependency_payload)
        dependency_canonical = dependency_payload.get("canonical_topic_id")
        if dependency_canonical:
            canonical_topic_dependencies.setdefault(
                validate_topic_slug(dependency_canonical), []
            ).append(dependency_payload)

    eligible_events: list[object] = []
    approved_delete_additions: set[int] = set()
    for event in events:
        payload = _record_mapping(event)
        key = (
            _bounded_integer(payload.get("contributor_id"), "contributor id", 1, 2**63 - 1),
            sanitize_terminal(payload.get("local_topic_id"), maximum=128),
        )
        dependencies = topic_dependencies.get(key, [])
        unresolved_upsert = any(
            str(item.get("event_type")) == "topic_upsert"
            and str(item.get("state")) in {"pending", "deferred"}
            for item in dependencies
        )
        canonical_id = _canonical_topic(payload)
        canonical_dependencies = canonical_topic_dependencies.get(canonical_id, [])
        blocking_delete = any(
            str(item.get("event_type")) == "topic_delete"
            and str(item.get("state")) in {"pending", "deferred"}
            for item in canonical_dependencies
        )
        accepted_canonical_dependencies = [
            item
            for item in canonical_dependencies
            if str(item.get("state")) in {"approved", "applied"}
            and str(item.get("event_type")) in {"topic_upsert", "topic_delete"}
        ]
        latest_accepted_canonical = (
            max(accepted_canonical_dependencies, key=lambda item: _event_id(item))
            if accepted_canonical_dependencies
            else None
        )
        accepted_upsert = bool(
            latest_accepted_canonical
            and str(latest_accepted_canonical.get("event_type")) == "topic_upsert"
        )
        approved_delete = bool(
            latest_accepted_canonical
            and str(latest_accepted_canonical.get("event_type")) == "topic_delete"
            and str(latest_accepted_canonical.get("state")) == "approved"
        )
        deleted_and_absent = bool(
            latest_accepted_canonical
            and str(latest_accepted_canonical.get("event_type")) == "topic_delete"
            and str(latest_accepted_canonical.get("state")) == "applied"
            and canonical_id not in known_live_topics
        )
        if deleted_and_absent:
            store.decide_event(
                _event_id(payload),
                "rejected",
                actor=_actor(actor),
                canonical_topic_id=canonical_id,
                note="Superseded by the applied canonical topic deletion.",
            )
            output.write(
                f"Rejected verse change {_event_id(payload)} because canonical topic "
                f"{canonical_id} was already deleted.\n"
            )
            continue
        if unresolved_upsert or blocking_delete:
            store.decide_event(
                _event_id(payload),
                "deferred",
                actor=_actor(actor),
                canonical_topic_id=canonical_id,
                note="The canonical topic change must be decided and published first.",
            )
            output.write(
                f"Deferred verse change {_event_id(payload)} until its topic decision is final.\n"
            )
            continue
        if approved_delete and str(payload.get("event_type")) == "verse_add":
            approved_delete_additions.add(_event_id(payload))
        if canonical_id not in known_live_topics and not accepted_upsert and not approved_delete:
            rejected_upsert = any(
                str(item.get("event_type")) == "topic_upsert"
                and str(item.get("state")) == "rejected"
                for item in dependencies
            )
            state = "rejected" if rejected_upsert else "deferred"
            store.decide_event(
                _event_id(payload),
                state,
                actor=_actor(actor),
                canonical_topic_id=canonical_id,
                note="No accepted canonical topic exists for this verse change.",
            )
            output.write(
                f"{state.capitalize()} verse change {_event_id(payload)} because its "
                "canonical topic is not accepted.\n"
            )
            continue
        eligible_events.append(event)
    events = eligible_events
    if not events:
        output.write("No verse changes are eligible until their topic decisions are complete.\n")
        return

    client = verse_client or _load_verse_client(translation)
    coordinates = [_event_coordinate(event) for event in events]
    try:
        verses = client.fetch_verses(coordinates)
    except Exception as error:
        if _is_query_error(error):
            output.write("Authoritative verse text is unavailable; all changes remain deferred.\n")
            for event in events:
                payload = _record_mapping(event)
                store.decide_event(
                    _event_id(payload),
                    "deferred",
                    actor=_actor(actor),
                    canonical_topic_id=_canonical_topic(payload),
                    note="Authoritative Query API text was unavailable.",
                )
            return
        raise

    effective = (base_associations | set(current.additions)) - set(current.removals)
    approved_verse_operations = {
        _event_id(payload): payload
        for payload in (
            _record_mapping(event)
            for event in _all_events(
                store,
                states={"approved"},
                types={"verse_add", "verse_remove"},
            )
        )
    }

    def projected_associations(
        candidate: Mapping[str, object] | None = None,
    ) -> set[Association]:
        operations = dict(approved_verse_operations)
        if candidate is not None:
            operations[_event_id(candidate)] = dict(candidate)
        projected = set(effective)
        for operation_id in sorted(operations):
            operation = operations[operation_id]
            coordinate = _event_coordinate(operation)
            association = Association.validated(
                {
                    "topic_id": _canonical_topic(operation),
                    "book": coordinate[0],
                    "chapter": coordinate[1],
                    "verse": coordinate[2],
                }
            )
            event_type = str(operation.get("event_type") or operation.get("type") or "")
            if event_type == "verse_add":
                projected.add(association)
            else:
                projected.discard(association)
        return projected

    action_counts: dict[Association, dict[str, int]] = {}
    for event in pending_verse_events:
        event_payload = _record_mapping(event)
        coordinate = _event_coordinate(event_payload)
        association = Association.validated(
            {
                "topic_id": _canonical_topic(event_payload),
                "book": coordinate[0],
                "chapter": coordinate[1],
                "verse": coordinate[2],
            }
        )
        event_type = str(event_payload.get("event_type") or event_payload.get("type") or "")
        counts = action_counts.setdefault(association, {})
        counts[event_type] = counts.get(event_type, 0) + 1
    safe_additions: list[tuple[dict[str, object], str, str]] = []
    individual: list[tuple[dict[str, object], str, str]] = []
    delete_conflicts: list[tuple[dict[str, object], str, str]] = []
    for event in events:
        payload = _record_mapping(event)
        coordinate = _event_coordinate(payload)
        verse = _verse_for_coordinate(verses, coordinate)
        text = sanitize_terminal(getattr(verse, "text", ""), maximum=1200)
        display_reference = sanitize_terminal(
            getattr(verse, "display_reference", f"{coordinate[0]} {coordinate[1]}:{coordinate[2]}"),
            maximum=120,
        )
        event_type = str(payload.get("event_type") or payload.get("type") or "")
        association = Association.validated(
            {
                "topic_id": _canonical_topic(payload),
                "book": coordinate[0],
                "chapter": coordinate[1],
                "verse": coordinate[2],
            }
        )
        counts = action_counts[association]
        duplicate = (
            (event_type == "verse_add" and association in effective)
            or (event_type == "verse_remove" and association not in effective)
            or counts[event_type] > 1
        )
        conflict = len(counts) > 1
        payload["is_duplicate"] = duplicate
        payload["is_conflict"] = conflict
        payload["is_replay"] = (
            _bounded_integer(payload.get("replay_count") or 0, "replay count", 0, 2**31 - 1) > 0
        )
        if _event_id(payload) in approved_delete_additions:
            payload["is_conflict"] = True
            target = delete_conflicts
        else:
            target = (
                safe_additions
                if event_type == "verse_add"
                and not duplicate
                and not conflict
                and not payload["is_replay"]
                else individual
            )
        target.append((payload, display_reference, text))

    if safe_additions:
        output.write(f"\n{len(safe_additions)} safe additions are ready for review.\n")
        for payload, reference, text in safe_additions:
            _print_event(payload, reference, text, output)
        action = _choice(
            "Approve all safe additions, review individually, defer all, or stop? [a/i/d/s]: ",
            {"a", "i", "d", "s"},
            input_fn,
        )
        if action == "s":
            return
        if action in {"a", "d"}:
            state = "approved" if action == "a" else "deferred"
            for payload, _, _ in safe_additions:
                store.decide_event(
                    _event_id(payload),
                    state,
                    actor=_actor(actor),
                    canonical_topic_id=_canonical_topic(payload),
                    note="",
                )
                if state == "approved":
                    approved_verse_operations[_event_id(payload)] = payload
            output.write(f"Safe additions {state}.\n")
        else:
            individual = safe_additions + individual

    for payload, reference, text in individual:
        _print_event(payload, reference, text, output)
        event_type = str(payload.get("event_type") or payload.get("type") or "")
        canonical_id = _canonical_topic(payload)
        if event_type == "verse_remove" and canonical_id in permanent_topic_ids:
            projected = projected_associations(payload)
            if not any(item.topic_id == canonical_id for item in projected):
                store.decide_event(
                    _event_id(payload),
                    "deferred",
                    actor=_actor(actor),
                    canonical_topic_id=canonical_id,
                    note=(
                        "A permanent canonical topic must retain at least one "
                        "effective verse association."
                    ),
                )
                output.write(
                    "  Deferred: this would remove the final verse association "
                    "from a permanent topic.\n"
                )
                continue
        action = _choice(
            "Approve, reject, defer, or stop? [a/r/d/s]: ",
            {"a", "r", "d", "s"},
            input_fn,
        )
        if action == "s":
            return
        state = {"a": "approved", "r": "rejected", "d": "deferred"}[action]
        store.decide_event(
            _event_id(payload),
            state,
            actor=_actor(actor),
            canonical_topic_id=_canonical_topic(payload),
            note=_optional_note(input_fn) if state != "approved" else "",
        )
        if state == "approved":
            approved_verse_operations[_event_id(payload)] = payload
        output.write(f"Verse change {state}.\n")

    for payload, reference, text in delete_conflicts:
        _print_event(payload, reference, text, output)
        output.write("  Conflict: this topic has an approved deletion awaiting publication.\n")
        action = _choice(
            "Reject, defer, or stop? [r/d/s]: ",
            {"r", "d", "s"},
            input_fn,
        )
        if action == "s":
            return
        state = {"r": "rejected", "d": "deferred"}[action]
        store.decide_event(
            _event_id(payload),
            state,
            actor=_actor(actor),
            canonical_topic_id=_canonical_topic(payload),
            note=_optional_note(input_fn),
        )
        output.write(f"Verse change {state}.\n")


def print_status(store: StoreProtocol, *, output: TextIO = sys.stdout) -> None:
    applications = store.list_applications(states={"pending", "deferred"}, limit=10_000)
    topics = store.list_source_topics(states={"pending", "deferred"}, limit=10_000)
    events = _all_events(store, states={"pending", "deferred", "approved"})
    approved = sum(
        1
        for event in events
        if str(_record_mapping(event).get("state") or _record_mapping(event).get("status"))
        == "approved"
    )
    current_record = store.current_catalog()
    current = _catalog_payload(current_record)
    revision = _catalog_revision(current_record)
    output.write("Contribution review status\n")
    output.write(f"  Applications awaiting action: {len(applications)}\n")
    output.write(f"  Topics awaiting resolution: {len(topics)}\n")
    output.write(f"  Verse changes awaiting action: {len(events) - approved}\n")
    output.write(f"  Approved changes awaiting live publication: {approved}\n")
    output.write(f"  Live catalogue revision: {revision or 'none'}\n")
    if current is not None:
        bundle = ContributionBundle.validated(current)
        output.write(f"  Live catalogue checksum: {_catalog_checksum(current_record, bundle)}\n")
    repository = _record_mapping(store.publication_state())
    repo_state = sanitize_terminal(repository.get("repo_state") or "not started")
    repo_revision = repository.get("repo_revision")
    output.write(f"  Repository publication: {repo_state}")
    if isinstance(repo_revision, int) and repo_revision >= 0:
        output.write(f" (live revision {repo_revision})")
    output.write("\n")
    branch = repository.get("repo_branch")
    if branch:
        output.write(f"  Repository branch: {sanitize_terminal(branch, maximum=256)}\n")


def publish_live(
    store: StoreProtocol,
    *,
    actor: str,
    topics_file: Path | None = None,
    associations_file: Path | None = None,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
) -> object:
    if store.list_source_topics(states={"pending", "deferred"}, limit=1):
        raise ReviewError("Resolve all pending contributor topics before live publication.")
    plan = build_publication_plan(
        store,
        topics_file=topics_file,
        associations_file=associations_file,
    )
    bundle = plan.bundle
    if not plan.event_ids and bundle == plan.base_bundle:
        output.write("There are no approved contribution changes to publish.\n")
        return None
    output.write(
        f"Publish {len(bundle.topics)} topics, {len(bundle.additions)} additions, "
        f"and {len(bundle.removals)} removals to this instance?\n"
    )
    if not _yes_no("Publish live now? [y/N]: ", input_fn):
        output.write("Live publication cancelled.\n")
        return None
    publish_atomically = getattr(store, "publish_approved_events_atomically", None)
    if not callable(publish_atomically):
        raise ReviewError(
            "The contribution store lacks transactional event publication; update the instance."
        )
    revision = publish_atomically(
        bundle.as_dict(),
        plan.event_ids,
        actor=_actor(actor),
        expected_revision=plan.base_revision,
        expected_checksum=plan.base_checksum,
    )
    live_checksum = _catalog_checksum(revision, bundle)
    output.write(
        f"Published live catalogue revision {_catalog_revision(revision) or 'unknown'} "
        f"({live_checksum}).\n"
    )
    return revision


def build_publication_plan(
    store: StoreProtocol,
    *,
    topics_file: Path | None = None,
    associations_file: Path | None = None,
) -> PublicationPlan:
    if topics_file is None or associations_file is None:
        raise ReviewError(
            "Bundled topic and association sources are required for safe live publication."
        )
    current_record = store.current_catalog()
    current = ContributionBundle.validated(_catalog_payload(current_record))
    base_revision = _catalog_revision_number(current_record)
    base_checksum = _catalog_checksum(current_record, current)
    base_topics = _load_canonical_topics(topics_file)
    base_associations = _load_base_associations(topics_file, associations_file)
    permanent_topic_ids = set(base_topics) | _published_topic_ids(store)
    # Rebase the live overlay onto the deployed repository catalogue.  Once a
    # reviewed branch is merged, its definitions and additions are redundant;
    # removals remain necessary only while the target still exists in base.
    topics = {topic.id: topic for topic in current.topics if topic.id not in base_topics}
    established_overlay_topic_ids = set(topics)
    additions = {item for item in current.additions if item not in base_associations}
    removals = {item for item in current.removals if item in base_associations}
    mapped: dict[tuple[int, str], CanonicalTopic] = {}
    for source in store.list_source_topics(states={"mapped"}, limit=10_000):
        payload = _record_mapping(source)
        contributor_id = _bounded_integer(
            payload.get("contributor_id"), "contributor id", 1, 2**63 - 1
        )
        local_id = sanitize_terminal(payload.get("local_topic_id"), maximum=128)
        definition = payload.get("canonical_definition")
        if definition:
            mapped[(contributor_id, local_id)] = CanonicalTopic.validated(definition)
    canonical_definitions = {
        topic.id: topic
        for topic in (CanonicalTopic.validated(value) for value in store.list_canonical_topics())
    }

    event_ids: list[int] = []
    topic_event_ids: dict[str, list[int]] = {}
    latest_topic_transition: dict[str, str] = {}
    approved_upsert_topic_ids: set[str] = set()
    deleted_topic_ids: set[str] = set()
    allowed_new_topics = set(topics)
    events = _all_events(store, states={"approved"})
    unresolved_verse_events: dict[str, list[int]] = {}
    for unresolved_event in _all_events(
        store,
        states={"pending", "deferred"},
        types={"verse_add", "verse_remove"},
    ):
        unresolved_payload = _record_mapping(unresolved_event)
        unresolved_canonical = unresolved_payload.get("canonical_topic_id")
        if unresolved_canonical:
            unresolved_verse_events.setdefault(
                validate_topic_slug(unresolved_canonical), []
            ).append(_event_id(unresolved_payload))
    if len(events) > 10_000:
        raise ReviewError(
            "More than 10,000 approved events await publication; review a smaller batch."
        )
    for event in events:
        payload = _record_mapping(event)
        event_id = _event_id(payload)
        event_type = str(payload.get("event_type") or payload.get("type") or "")
        canonical_id = _canonical_topic(payload)
        if event_type == "topic_upsert":
            key = (
                _bounded_integer(payload.get("contributor_id"), "contributor id", 1, 2**63 - 1),
                sanitize_terminal(payload.get("local_topic_id"), maximum=128),
            )
            definition = mapped.get(key) or canonical_definitions.get(canonical_id)
            if canonical_id not in base_topics and definition is None:
                raise ReviewError(f"Approved topic event {event_id} has no canonical definition.")
            allowed_new_topics.add(canonical_id)
            approved_upsert_topic_ids.add(canonical_id)
            deleted_topic_ids.discard(canonical_id)
            latest_topic_transition[canonical_id] = event_type
            topic_event_ids.setdefault(canonical_id, []).append(event_id)
        elif event_type == "topic_delete":
            if canonical_id in permanent_topic_ids:
                raise ReviewError(
                    f"Approved event {event_id} attempts to delete permanent topic "
                    f"{canonical_id}."
                )
            if unresolved_verse_events.get(canonical_id):
                raise ReviewError(
                    "Review all related verse changes before deleting canonical topic "
                    f"{canonical_id}."
                )
            topics.pop(canonical_id, None)
            deleted_topic_ids.add(canonical_id)
            latest_topic_transition[canonical_id] = event_type
            topic_event_ids.setdefault(canonical_id, []).append(event_id)
            additions = {item for item in additions if item.topic_id != canonical_id}
            removals = {item for item in removals if item.topic_id != canonical_id}
        elif event_type in {"verse_add", "verse_remove"}:
            book, chapter, verse = _event_coordinate(payload)
            association = Association.validated(
                {
                    "topic_id": canonical_id,
                    "book": book,
                    "chapter": chapter,
                    "verse": verse,
                }
            )
            if event_type == "verse_add":
                removals.discard(association)
                if association in base_associations:
                    additions.discard(association)
                else:
                    additions.add(association)
            else:
                additions.discard(association)
                if association in base_associations:
                    removals.add(association)
                else:
                    removals.discard(association)
        else:
            raise ReviewError(f"Approved event {event_id} has an unsupported type.")
        if event_type not in {"topic_upsert", "topic_delete"}:
            event_ids.append(event_id)

    effective_associations = (base_associations | additions) - removals
    effective_topic_ids = {item.topic_id for item in effective_associations}
    for topic_id in sorted(allowed_new_topics - set(base_topics)):
        if topic_id in deleted_topic_ids:
            continue
        if topic_id not in effective_topic_ids:
            if topic_id not in established_overlay_topic_ids:
                topics.pop(topic_id, None)
            continue
        if topic_id in topics and topic_id not in approved_upsert_topic_ids:
            continue
        definition = canonical_definitions.get(topic_id)
        if definition is None:
            raise ReviewError(f"Canonical definition for contributed topic {topic_id} is missing.")
        topics[topic_id] = definition
    for topic_id, transition_ids in topic_event_ids.items():
        final_transition = latest_topic_transition[topic_id]
        if final_transition == "topic_delete" or (
            final_transition == "topic_upsert"
            and (topic_id in base_topics or topic_id in effective_topic_ids)
        ):
            # A later accepted transition supersedes every earlier transition
            # for this canonical ID. Apply the whole reviewed chain together so
            # an upsert followed by a pre-publication delete cannot leave an
            # approved event stranded forever.
            event_ids.extend(transition_ids)

    unknown_topic_ids = effective_topic_ids - (set(base_topics) | set(topics))
    if unknown_topic_ids:
        labels = ", ".join(sorted(unknown_topic_ids)[:10])
        raise ReviewError(
            "Publication contains verse associations without an accepted canonical topic: "
            f"{labels}."
        )
    _validate_topic_name_uniqueness((*base_topics.values(), *topics.values()))
    uncovered_topic_ids = (set(base_topics) | set(topics)) - effective_topic_ids
    if uncovered_topic_ids:
        labels = ", ".join(sorted(uncovered_topic_ids)[:10])
        raise ReviewError(
            f"Publication would leave canonical topics without verse associations: {labels}."
        )

    total_topics = len(set(base_topics) | set(topics))
    if total_topics > MAX_EFFECTIVE_TOPICS:
        raise ReviewError(
            f"Publication would contain {total_topics} topics; the limit is {MAX_EFFECTIVE_TOPICS}."
        )
    if len(effective_associations) > MAX_EFFECTIVE_ASSOCIATIONS:
        raise ReviewError("Publication would exceed the 10,000 effective verse-association limit.")

    bundle = ContributionBundle(
        tuple(topics.values()),
        tuple(additions),
        tuple(removals),
    ).normalized()
    if len(bundle.json_bytes()) > MAX_OVERLAY_BYTES:
        raise ReviewError("The cumulative contribution overlay would exceed 2 MiB.")
    return PublicationPlan(
        bundle,
        tuple(sorted(set(event_ids))),
        current,
        base_revision,
        base_checksum,
    )


def export_current_catalog(
    store: StoreProtocol,
    path: Path,
    *,
    output: TextIO = sys.stdout,
) -> CatalogExport:
    record = store.current_catalog()
    payload = _catalog_payload(record)
    if payload is None:
        raise ReviewError("No live contribution catalogue has been published yet.")
    bundle = ContributionBundle.validated(payload)
    if len(bundle.json_bytes()) > MAX_OVERLAY_BYTES:
        raise ReviewError("The live contribution catalogue exceeds 2 MiB.")
    revision = _catalog_revision_number(record)
    checksum = _catalog_checksum(record, bundle)
    atomic_write(path, bundle.json_bytes())
    output.write(f"Exported revision {revision} to {path} ({bundle.checksum}).\n")
    return CatalogExport(bundle, revision, checksum)


def _decide_related_topic_events(
    store: StoreProtocol,
    *,
    contributor_id: int,
    local_topic_id: str,
    state: str,
    actor: str,
) -> None:
    for event in _all_events(
        store,
        states={"pending", "deferred", "approved"},
        types={"topic_upsert", "topic_delete", "verse_add", "verse_remove"},
    ):
        payload = _record_mapping(event)
        if (
            payload.get("contributor_id") == contributor_id
            and payload.get("local_topic_id") == local_topic_id
        ):
            store.decide_event(
                _event_id(payload),
                state,
                actor=_actor(actor),
                canonical_topic_id=(
                    validate_topic_slug(payload.get("canonical_topic_id"))
                    if payload.get("canonical_topic_id")
                    else None
                ),
                note="Source topic was rejected." if state == "rejected" else "",
            )


def _review_topic_events(
    store: StoreProtocol,
    *,
    actor: str,
    bundled_topics: set[str],
    canonical_topics: Mapping[str, CanonicalTopic],
    locked_topic_ids: set[str],
    input_fn: Callable[[str], str],
    output: TextIO,
) -> None:
    canonical_snapshot = dict(canonical_topics)
    events = _all_events(
        store,
        states={"pending", "deferred"},
        types={"topic_upsert", "topic_delete"},
    )
    if not events:
        return
    sources = {
        (
            _bounded_integer(payload.get("contributor_id"), "contributor id", 1, 2**63 - 1),
            sanitize_terminal(payload.get("local_topic_id"), maximum=128),
        ): payload
        for payload in (_record_mapping(value) for value in store.list_source_topics(limit=10_000))
    }
    for event in events:
        payload = _record_mapping(event)
        contributor_id = _bounded_integer(
            payload.get("contributor_id"), "contributor id", 1, 2**63 - 1
        )
        local_topic_id = sanitize_terminal(payload.get("local_topic_id"), maximum=128)
        source = sources.get((contributor_id, local_topic_id))
        if source is None:
            store.decide_event(
                _event_id(payload),
                "deferred",
                actor=_actor(actor),
                note="Source topic record is unavailable.",
            )
            continue
        source_state = str(source.get("state") or "")
        canonical_id_value = source.get("canonical_topic_id")
        canonical_id = validate_topic_slug(canonical_id_value) if canonical_id_value else None
        if source_state == "deferred":
            if str(payload.get("state")) != "deferred":
                store.decide_event(
                    _event_id(payload),
                    "deferred",
                    actor=_actor(actor),
                    canonical_topic_id=canonical_id,
                    note="Source topic resolution was deferred.",
                )
            continue
        if source_state == "rejected":
            store.decide_event(
                _event_id(payload),
                "rejected",
                actor=_actor(actor),
                note="Source topic was rejected.",
            )
            continue
        if source_state != "mapped" or canonical_id is None:
            store.decide_event(
                _event_id(payload),
                "deferred",
                actor=_actor(actor),
                note="Canonical topic mapping is unresolved.",
            )
            continue

        event_type = str(payload.get("event_type") or "")
        output.write("\nTopic contribution\n")
        output.write(f"  Action: {'UPSERT' if event_type == 'topic_upsert' else 'DELETE'}\n")
        output.write(f"  Canonical topic: {canonical_id}\n")
        output.write(f"  Contributor: {contributor_id}\n")
        output.write(f"  Proposed name: {sanitize_terminal(payload.get('topic_name') or '')}\n")
        output.write(f"  Proposed color: {sanitize_terminal(payload.get('topic_color') or '')}\n")
        output.write(f"  Submitted: {_format_timestamp(payload.get('submitted_at'))}\n")
        if event_type == "topic_delete":
            if canonical_id in bundled_topics or canonical_id in locked_topic_ids:
                output.write(
                    "  Topics are permanent after their first live publication and "
                    "cannot be deleted by a contribution.\n"
                )
                action = _choice("Reject, defer, or stop? [r/d/s]: ", {"r", "d", "s"}, input_fn)
            else:
                output.write(
                    "  This cancels the never-published topic and its pending overlay links.\n"
                )
                action = _choice(
                    "Approve deletion, reject, defer, or stop? [a/r/d/s]: ",
                    {"a", "r", "d", "s"},
                    input_fn,
                )
        else:
            action = _choice(
                "Approve mapped topic, edit English definition, reject, defer, or stop? "
                "[a/e/r/d/s]: ",
                {"a", "e", "r", "d", "s"},
                input_fn,
            )
        if action == "s":
            return
        if action == "e":
            current_definition = source.get("canonical_definition")
            if current_definition:
                proposed = CanonicalTopic.validated(current_definition)
            elif canonical_id in bundled_topics:
                output.write(
                    "Bundled topic definitions stay authoritative; map to a new topic "
                    "to propose distinct metadata.\n"
                )
                continue
            else:
                proposed = CanonicalTopic(
                    canonical_id,
                    validate_english_topic_name(source.get("name")),
                    validate_topic_color(source.get("color")),
                    validate_aliases(source.get("aliases") or (), name=str(source.get("name"))),
                )
            corrected = _prompt_topic_definition(proposed, input_fn=input_fn)
            _assert_stable_published_topic(
                previous_canonical=canonical_id,
                chosen=corrected.id,
                definition=corrected,
                canonical_topics=canonical_snapshot,
                locked_topic_ids=locked_topic_ids,
            )
            _validate_topic_name_uniqueness(
                tuple(
                    topic
                    for topic_id, topic in canonical_snapshot.items()
                    if topic_id != canonical_id
                )
                + (corrected,)
            )
            updated_source = store.set_topic_mapping(
                contributor_id,
                local_topic_id,
                state="mapped",
                actor=_actor(actor),
                canonical_topic_id=corrected.id,
                canonical_definition=corrected.as_dict(),
                name=corrected.name,
                color=corrected.color,
                aliases=corrected.aliases,
            )
            canonical_snapshot[corrected.id] = corrected
            source = _record_mapping(updated_source)
            sources[(contributor_id, local_topic_id)] = source
            canonical_id = corrected.id
            action = "a"
        state = {"a": "approved", "r": "rejected", "d": "deferred"}[action]
        store.decide_event(
            _event_id(payload),
            state,
            actor=_actor(actor),
            canonical_topic_id=canonical_id,
            note=_optional_note(input_fn) if state != "approved" else "",
        )
        output.write(f"Topic change {state}.\n")


def _load_base_associations(
    topics_file: Path | None,
    associations_file: Path | None,
) -> set[Association]:
    if topics_file is None or associations_file is None:
        return set()
    topics = _load_canonical_topics(topics_file)
    names: dict[str, str] = {}
    for topic in topics.values():
        for name in (topic.name, *topic.aliases):
            folded = name.casefold()
            if folded in names and names[folded] != topic.id:
                raise ReviewError("Bundled topic names and aliases are not unique.")
            names[folded] = topic.id
    try:
        if associations_file.stat().st_size > 8 * 1024 * 1024:
            raise ReviewError("The bundled association source exceeds 8 MiB.")
        rows = associations_file.read_text(encoding="utf-8").splitlines()
    except ReviewError:
        raise
    except (OSError, UnicodeError) as error:
        raise ReviewError(f"Bundled associations could not be read: {error}") from error
    associations: set[Association] = set()
    for index, row in enumerate(rows, 1):
        if not row:
            continue
        columns = row.split(",")
        if len(columns) != 2:
            raise ReviewError(f"Bundled association row {index} is malformed.")
        match = re.fullmatch(r"([1-9]\d*) ([1-9]\d*):([1-9]\d*)", columns[0].strip())
        topic_id = names.get(columns[1].strip().casefold())
        if match is None or topic_id is None:
            raise ReviewError(f"Bundled association row {index} is invalid.")
        association = Association.validated(
            {
                "topic_id": topic_id,
                "book": int(match.group(1)),
                "chapter": int(match.group(2)),
                "verse": int(match.group(3)),
            }
        )
        if association in associations:
            raise ReviewError(f"Bundled association row {index} is duplicated.")
        associations.add(association)
    return associations


def _all_events(
    store: StoreProtocol,
    *,
    states: set[str] | None = None,
    types: set[str] | None = None,
) -> list[object]:
    events: list[object] = []
    after_id = 0
    while len(events) < MAX_EVENTS_PER_SCAN:
        page_limit = min(EVENT_SCAN_PAGE_SIZE, MAX_EVENTS_PER_SCAN - len(events))
        page = list(
            store.list_events(
                states=states,
                types=types,
                limit=page_limit,
                after_id=after_id,
            )
        )
        events.extend(page)
        if len(page) < page_limit:
            return events
        after_id = max(_event_id(_record_mapping(event)) for event in page)
    overflow = list(
        store.list_events(
            states=states,
            types=types,
            limit=1,
            after_id=after_id,
        )
    )
    if overflow:
        raise ReviewError("The contribution event scan reached its safety limit.")
    return events


def _published_topic_ids(store: StoreProtocol) -> set[str]:
    """Return canonical IDs whose definitions and existence must remain stable.

    Once a topic has appeared in a live revision, a repository branch may
    already contain it. Schema version 1 has no topic tombstone, so locking the
    historical live IDs prevents a later branch merge from resurrecting a
    locally deleted topic. The store derives this set from immutable catalogue
    revisions rather than applied events, since a pre-publication upsert/delete
    chain may be terminalized without ever making the topic live.
    """

    return {validate_topic_slug(topic_id) for topic_id in store.published_topic_ids()}


def _assert_stable_published_topic(
    *,
    previous_canonical: str | None,
    chosen: str,
    definition: CanonicalTopic,
    canonical_topics: Mapping[str, CanonicalTopic],
    locked_topic_ids: set[str],
) -> None:
    if previous_canonical is None or previous_canonical not in locked_topic_ids:
        return
    current = canonical_topics.get(previous_canonical)
    if chosen != previous_canonical or current is None or definition != current:
        raise ReviewError(
            "A published canonical topic definition cannot be changed. Keep its "
            "English name, topic ID, color, and aliases exactly as published."
        )


def _load_canonical_topics(path: Path | None) -> dict[str, CanonicalTopic]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReviewError(f"Canonical topic definitions could not be read: {error}") from error
    raw = payload.get("topics", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise ReviewError("Canonical topic definitions must be an array.")
    topics = [CanonicalTopic.validated(item) for item in raw]
    return {topic.id: topic for topic in topics}


def _select_topic(
    topics: Mapping[str, CanonicalTopic],
    *,
    input_fn: Callable[[str], str],
    output: TextIO,
) -> str:
    if not topics:
        raise ReviewError("No canonical topics are available; create a new topic instead.")
    needle = sanitize_terminal(
        input_fn("Search canonical English topics: "), maximum=100
    ).casefold()
    matches = [
        topic for topic in topics.values() if needle in topic.name.casefold() or needle in topic.id
    ]
    matches.sort(key=lambda item: (item.name.casefold(), item.id))
    if not matches:
        raise ReviewError("No canonical topic matches that search.")
    matches = matches[:50]
    for index, topic in enumerate(matches, 1):
        output.write(f"  {index}) {topic.name} [{topic.id}] {topic.color}\n")
    selection = _number_choice("Topic number: ", 1, len(matches), input_fn)
    return matches[selection - 1].id


def _select_pending_mapping(
    store: StoreProtocol,
    current: object,
    *,
    canonical_topics: Mapping[str, CanonicalTopic],
    input_fn: Callable[[str], str],
    output: TextIO,
) -> tuple[str, CanonicalTopic, tuple[int, str] | None]:
    current_payload = _record_mapping(current)
    candidates = []
    for source in store.list_source_topics(
        states={"pending", "deferred", "mapped"},
        limit=10_000,
    ):
        payload = _record_mapping(source)
        if payload.get("contributor_id") == current_payload.get("contributor_id") and payload.get(
            "local_topic_id"
        ) == current_payload.get("local_topic_id"):
            continue
        candidates.append(payload)
    if not candidates:
        raise ReviewError("No previously resolved contributor topic is available to merge.")
    candidates.sort(
        key=lambda item: (
            sanitize_terminal(item.get("name")).casefold(),
            _bounded_integer(
                item.get("contributor_id") or 0,
                "contributor id",
                0,
                2**63 - 1,
            ),
            str(item.get("local_topic_id") or ""),
        )
    )
    for index, payload in enumerate(candidates[:100], 1):
        output.write(
            f"  {index}) {sanitize_terminal(payload.get('name') or payload.get('local_topic_id'))} "
            f"[{sanitize_terminal(payload.get('state'))}] -> "
            f"{sanitize_terminal(payload.get('canonical_topic_id') or 'unresolved')}\n"
        )
    selection = _number_choice(
        "Resolved contributor topic number: ",
        1,
        min(100, len(candidates)),
        input_fn,
    )
    chosen = candidates[selection - 1]
    newly_mapped: tuple[int, str] | None = None
    canonical_value = chosen.get("canonical_topic_id")
    if canonical_value:
        canonical_id = validate_topic_slug(canonical_value)
        authoritative = canonical_topics.get(canonical_id)
        if authoritative is None:
            raise ReviewError("The selected canonical topic definition is unavailable.")
        stored_definition = chosen.get("canonical_definition")
        if (
            stored_definition is not None
            and CanonicalTopic.validated(stored_definition) != authoritative
        ):
            raise ReviewError("The selected canonical topic definition is inconsistent.")
        definition = authoritative
    else:
        proposed_name = chosen.get("name") or current_payload.get("name")
        proposed_color = chosen.get("color") or current_payload.get("color")
        proposed = CanonicalTopic(
            topic_slug(validate_english_topic_name(proposed_name)),
            validate_english_topic_name(proposed_name),
            validate_topic_color(proposed_color),
            validate_aliases(
                chosen.get("aliases") or current_payload.get("aliases") or (),
                name=str(proposed_name),
            ),
        )
        definition = _prompt_topic_definition(proposed, input_fn=input_fn)
        canonical_id = definition.id
        target_contributor = _bounded_integer(
            chosen.get("contributor_id"),
            "contributor id",
            1,
            2**63 - 1,
        )
        target_local_id = sanitize_terminal(chosen.get("local_topic_id"), maximum=128)
        newly_mapped = (target_contributor, target_local_id)
    return canonical_id, definition, newly_mapped


def _prompt_topic_definition(
    proposed: CanonicalTopic,
    *,
    input_fn: Callable[[str], str],
) -> CanonicalTopic:
    name = validate_english_topic_name(
        _prompt_default("English topic name", proposed.name, input_fn)
    )
    suggested_slug = topic_slug(name)
    topic_id = suggested_slug
    color = validate_topic_color(_prompt_default("Color", proposed.color, input_fn))
    default_aliases = ", ".join(proposed.aliases)
    raw_aliases = _prompt_default(
        "English aliases (comma-separated; blank for none)",
        default_aliases,
        input_fn,
    )
    aliases = validate_aliases(
        [item.strip() for item in raw_aliases.split(",") if item.strip()],
        name=name,
    )
    return CanonicalTopic(topic_id, name, color, aliases)


def _load_verse_client(translation: str) -> VerseClientProtocol:
    try:
        from modules.getbible_query import GetBibleQueryClient
    except ImportError as error:
        raise ReviewError(
            "The deployed application lacks the authoritative Query API client."
        ) from error
    return cast(VerseClientProtocol, GetBibleQueryClient(translation=translation))


def _is_query_error(error: Exception) -> bool:
    try:
        from modules.getbible_query import GetBibleQueryError
    except ImportError:
        return isinstance(error, (TimeoutError, ConnectionError, OSError))
    return isinstance(error, GetBibleQueryError)


def _verse_for_coordinate(
    verses: Mapping[object, object],
    coordinate: tuple[int, int, int],
) -> object:
    try:
        from modules.getbible_query import VerseReference

        key: object = VerseReference(*coordinate)
    except ImportError:
        key = coordinate
    verse = verses.get(key)
    if verse is None:
        verse = verses.get(coordinate)
    if verse is None:
        raise ReviewError("The authoritative Query API omitted a requested verse.")
    return verse


def _print_event(payload: Mapping[str, object], reference: str, text: str, output: TextIO) -> None:
    event_type = str(payload.get("event_type") or payload.get("type") or "")
    action = "ADD" if event_type == "verse_add" else "REMOVE"
    flags = []
    if payload.get("duplicate") or payload.get("is_duplicate"):
        flags.append("duplicate")
    if payload.get("conflict") or payload.get("is_conflict"):
        flags.append("conflict")
    replay_count = _bounded_integer(
        payload.get("replay_count") or 0,
        "replay count",
        0,
        2**31 - 1,
    )
    if payload.get("is_replay") or replay_count > 0:
        flags.append("idempotent replay")
    output.write("\nVerse contribution\n")
    output.write(f"  Action: {action}\n")
    output.write(f"  Topic: {sanitize_terminal(_canonical_topic(payload))}\n")
    output.write(f"  Reference: {sanitize_terminal(reference)}\n")
    output.write(f"  Text: {text}\n")
    output.write(f"  Contributor: {sanitize_terminal(payload.get('contributor_id'))}\n")
    submitted = payload.get("submitted_at") or payload.get("created_at")
    output.write(f"  Submitted: {_format_timestamp(submitted)}\n")
    output.write(f"  Review flags: {', '.join(flags) if flags else 'none'}\n")


def _event_coordinate(value: object) -> tuple[int, int, int]:
    payload = _record_mapping(value)
    nested = payload.get("payload")
    if isinstance(nested, Mapping):
        payload = {**payload, **nested}
    book = _bounded_integer(payload.get("book"), "book", 1, len(BOOK_CHAPTER_COUNTS))
    chapter = _bounded_integer(
        payload.get("chapter"),
        "chapter",
        1,
        BOOK_CHAPTER_COUNTS[book - 1],
    )
    return (book, chapter, _bounded_integer(payload.get("verse"), "verse", 1, 2000))


def _event_id(payload: Mapping[str, object]) -> int:
    return _bounded_integer(payload.get("id") or payload.get("event_id"), "event id", 1, 2**63 - 1)


def _canonical_topic(payload: Mapping[str, object]) -> str:
    nested = payload.get("payload")
    nested_topic = nested.get("canonical_topic_id") if isinstance(nested, Mapping) else None
    return validate_topic_slug(payload.get("canonical_topic_id") or nested_topic)


def _catalog_payload(value: object | None) -> object | None:
    if value is None:
        return None
    payload = _record_mapping(value)
    return (
        payload.get("catalog")
        or payload.get("payload")
        or payload.get("data")
        or (value if "schema_version" in payload else None)
    )


def _catalog_revision(value: object | None) -> int | None:
    if value is None:
        return None
    payload = _record_mapping(value)
    revision = payload.get("revision") or payload.get("id")
    return revision if isinstance(revision, int) and revision > 0 else None


def _catalog_revision_number(value: object | None) -> int:
    if value is None:
        return 0
    payload = _record_mapping(value)
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ReviewError("The live catalogue revision is invalid.")
    return revision


def _catalog_checksum(value: object | None, bundle: ContributionBundle) -> str:
    if value is not None:
        candidate = _record_mapping(value).get("checksum")
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
            return candidate
    encoded = json.dumps(
        bundle.as_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _display_person(payload: Mapping[str, object]) -> str:
    parts = [payload.get("first_name"), payload.get("last_name")]
    value = " ".join(sanitize_terminal(part) for part in parts if part)
    return value or "not supplied"


def _format_timestamp(value: object) -> str:
    if isinstance(value, bool):
        return sanitize_terminal(value)
    try:
        timestamp = int(cast(Any, value))
    except (TypeError, ValueError):
        return sanitize_terminal(value)
    if timestamp > 10_000_000_000_000:
        timestamp //= 1_000_000_000
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return sanitize_terminal(value)


def _actor(actor: str) -> str:
    value = sanitize_terminal(actor, maximum=MAX_ACTOR_LENGTH + 1)
    if not 1 <= len(value) <= MAX_ACTOR_LENGTH:
        raise ReviewError("The operator identity is invalid.")
    return value


def _optional_note(input_fn: Callable[[str], str]) -> str:
    note = sanitize_terminal(input_fn("Review note (optional): "), maximum=MAX_NOTE_LENGTH + 1)
    if len(note) > MAX_NOTE_LENGTH:
        raise ReviewError(f"Review notes can contain at most {MAX_NOTE_LENGTH} characters.")
    return note


def _choice(prompt: str, choices: set[str], input_fn: Callable[[str], str]) -> str:
    while True:
        value = sanitize_terminal(input_fn(prompt), maximum=20).casefold()
        if value in choices:
            return value
        print(f"Choose one of: {', '.join(sorted(choices))}.")


def _yes_no(
    prompt: str,
    input_fn: Callable[[str], str],
    *,
    default: bool = False,
) -> bool:
    value = sanitize_terminal(input_fn(prompt), maximum=20).casefold()
    if not value:
        return default
    return value in {"y", "yes"}


def _number_choice(
    prompt: str,
    minimum: int,
    maximum: int,
    input_fn: Callable[[str], str],
) -> int:
    while True:
        raw = sanitize_terminal(input_fn(prompt), maximum=20)
        if raw.isdigit() and minimum <= int(raw) <= maximum:
            return int(raw)
        print(f"Choose a number from {minimum} through {maximum}.")


def _prompt_default(label: str, default: str, input_fn: Callable[[str], str]) -> str:
    value = sanitize_terminal(input_fn(f"{label} [{default}]: "), maximum=500)
    return value or default


if __name__ == "__main__":
    raise SystemExit(main())
