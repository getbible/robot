"""Bounded maintenance for Librarian's content-addressed disk cache."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)
_OBJECT_RE = re.compile(r"[0-9a-f]{40}\.json\Z")
_MAX_SCANNED_FILES = 20_000
_UNREFERENCED_GRACE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class CachePruneResult:
    bytes_before: int
    bytes_after: int
    files_removed: int
    scan_truncated: bool


def getbible_cache_root() -> Path:
    """Return the cache root selected by Librarian without importing internals."""
    configured = os.environ.get("GETBIBLE_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / "getbible"


def prune_translation_cache(
    *,
    max_bytes: int,
    now: float | None = None,
) -> CachePruneResult:
    """Remove stale objects and evict old cache entries above the byte budget.

    New objects receive a full-day grace period. This avoids racing Librarian's
    atomic object-then-metadata commit without depending on its private locks.
    Metadata eviction is safe because a later lookup simply refetches the
    immutable translation.
    """
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive.")
    root = getbible_cache_root()
    if not root.is_dir() or root.is_symlink():
        return CachePruneResult(0, 0, 0, False)

    current_time = time.time() if now is None else now
    regular_files: list[tuple[Path, int, float]] = []
    metadata_files: list[Path] = []
    truncated = False
    try:
        candidates = root.rglob("*")
        for path in candidates:
            if len(regular_files) >= _MAX_SCANNED_FILES:
                truncated = True
                break
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                info = path.stat()
            except OSError:
                continue
            regular_files.append((path, info.st_size, info.st_mtime))
            if path.name.endswith(".metadata.json"):
                metadata_files.append(path)
    except OSError:
        return CachePruneResult(0, 0, 0, False)

    file_details = {
        path.resolve(): (path, size, modified)
        for path, size, modified in regular_files
    }
    metadata_entries: list[tuple[Path, Path, int, float]] = []
    referenced: set[Path] = set()
    for metadata_path in metadata_files:
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8")).get("payload")
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            continue
        if isinstance(payload, str) and _OBJECT_RE.fullmatch(payload):
            payload_path = (metadata_path.parent / "objects" / payload).resolve()
            referenced.add(payload_path)
            details = file_details.get(metadata_path.resolve())
            if details is not None:
                metadata_entries.append(
                    (metadata_path, payload_path, details[1], details[2])
                )

    bytes_before = sum(size for _, size, _ in regular_files)
    bytes_after = bytes_before
    removed = 0
    unreferenced = sorted(
        (
            (path, size, modified)
            for path, size, modified in regular_files
            if path.parent.name == "objects"
            and _OBJECT_RE.fullmatch(path.name)
            and path.resolve() not in referenced
            and current_time - modified >= _UNREFERENCED_GRACE_SECONDS
        ),
        key=lambda item: item[2],
    )
    for path, size, _ in unreferenced:
        # Remove every safely stale object. The byte budget is also reported so
        # operators can see when live, referenced corpora alone exceed it.
        try:
            path.unlink()
        except OSError:
            continue
        bytes_after = max(0, bytes_after - size)
        removed += 1

    # If live cache entries still exceed the budget, evict the oldest complete
    # metadata/object pairs. Both files must have survived a full-day grace
    # period so a just-committed or currently refreshing entry is never chosen.
    remaining_references = Counter(
        payload_path for _, payload_path, _, _ in metadata_entries
    )
    for metadata_path, payload_path, metadata_size, metadata_modified in sorted(
        metadata_entries,
        key=lambda item: item[3],
    ):
        if bytes_after <= max_bytes:
            break
        payload_details = file_details.get(payload_path)
        if (
            current_time - metadata_modified < _UNREFERENCED_GRACE_SECONDS
            or payload_details is None
            or current_time - payload_details[2] < _UNREFERENCED_GRACE_SECONDS
        ):
            continue
        try:
            metadata_path.unlink()
        except OSError:
            continue
        bytes_after = max(0, bytes_after - metadata_size)
        removed += 1
        remaining_references[payload_path] -= 1
        if remaining_references[payload_path] > 0:
            continue
        payload_file, payload_size, _ = payload_details
        try:
            payload_file.unlink()
        except OSError:
            continue
        bytes_after = max(0, bytes_after - payload_size)
        removed += 1

    if bytes_after > max_bytes:
        LOGGER.warning(
            "GetBible cache remains above its byte budget after safe pruning "
            "(bytes=%d, budget=%d)",
            bytes_after,
            max_bytes,
        )
    return CachePruneResult(bytes_before, bytes_after, removed, truncated)


class CacheJanitor:
    """Run safe cache pruning periodically without blocking the event loop."""

    def __init__(self, *, max_bytes: int, interval_seconds: int) -> None:
        self._max_bytes = max_bytes
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="getbible-cache-janitor",
            )

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            try:
                result = await asyncio.to_thread(
                    prune_translation_cache,
                    max_bytes=self._max_bytes,
                )
                if result.files_removed:
                    LOGGER.info(
                        "Pruned stale GetBible cache objects "
                        "(files=%d, bytes_before=%d, bytes_after=%d)",
                        result.files_removed,
                        result.bytes_before,
                        result.bytes_after,
                    )
                if result.scan_truncated:
                    LOGGER.warning("GetBible cache scan reached its file safety limit")
            except Exception as error:
                LOGGER.warning(
                    "GetBible cache maintenance failed safely (%s)",
                    type(error).__name__,
                )
            await asyncio.sleep(self._interval)
