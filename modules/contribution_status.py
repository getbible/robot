"""Read-only operator diagnostics for the contribution publication pipeline."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from .bookmark_sources import decode_json
from .contribution_publication import (
    REPOSITORY,
    TRANSLATION_CONTRACT_VERSION,
    AcceptedSnapshot,
    PublicationSettings,
    _owned_regular,
)


def describe_publication(
    store_path: Path,
    settings: PublicationSettings,
    *,
    output: Callable[[str], None] = print,
) -> None:
    """Report stored receipts and unsent acceptance without creating a journal.

    Receipt counts distinguish a successful sync/acceptance from a remote
    commit. Public API liveness is reported by the existing review status;
    this diagnostic makes no network requests or publication mutations.
    """
    output(f"Builder: https://github.com/{REPOSITORY}")
    output("Destination: builder default branch (main/master), discovered before committing.")
    output("  GitHub token: " + ("configured" if settings.github_token else "MISSING"))
    output("  OpenAI API key: " + ("configured" if settings.openai_key else "MISSING"))
    output(f"  Translation model: {settings.model}")
    snapshot = AcceptedSnapshot.read(store_path)
    path = store_path.with_name(store_path.name + ".publication.sqlite3")
    metadata: dict[str, Any] = {}
    processed: set[int] = set()
    if path.exists():
        _owned_regular(path)
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            metadata = {
                str(key): decode_json(value)
                for key, value in db.execute("SELECT key, value FROM publication_metadata")
            }
            processed = {int(row[0]) for row in db.execute("SELECT id FROM published_events")}
    pending = metadata.get("pending")
    last = metadata.get("last")
    unreceipted = sum(event["id"] not in processed for event in snapshot.events)
    initial_work = not metadata.get("initial_import_complete") and bool(
        snapshot.bundle["topics"]
        or snapshot.bundle["associations"]["add"]
        or snapshot.bundle["associations"]["remove"]
    )
    translation_check = bool(
        metadata.get("initial_import_complete")
        and metadata.get("translation_contract_version", 0) < TRANSLATION_CONTRACT_VERSION
        and (
            snapshot.bundle["topics"]
            or any(event["canonical_topic_id"] is not None for event in snapshot.events)
        )
    )
    waiting = bool(pending or unreceipted or initial_work or translation_check)
    output("Direct API publication: " + ("pending" if waiting else "no queued commit"))
    output(f"  Latest accepted ledger revision: {snapshot.revision}")
    output(f"  Accepted events without a builder receipt: {unreceipted}")
    if initial_work:
        output("  Previously accepted catalogue changes await their first publication check.")
    if translation_check:
        output("  Previously accepted topics await the upgrade's missing-translation check.")
    if pending:
        output(f"  Saved publication revision: {pending['revision']}")
        output(
            f"  Saved changes: {len(pending['definitions'])} topic definitions, "
            f"{len(pending['operations'])} verse operations."
        )
    if last:
        output(f"  Last builder commit: {last['commit']} ({last['branch']})")
        output(f"  Review commit: https://github.com/{REPOSITORY}/commit/{last['commit']}")
        output(f"  Last published ledger revision: {last['revision']}")
        if not last.get("changed"):
            output("  The last accepted batch already matched the builder; no commit was needed.")
    if metadata.get("error"):
        output("  The last attempt failed; accepted work and the saved publication are retained.")
    if waiting:
        if not settings.github_token:
            output("Next: contributions INSTANCE tokens, then contributions INSTANCE commit.")
        elif not settings.openai_key:
            output(
                "Next: add the OpenAI API key with contributions INSTANCE tokens; "
                "missing topic translations must be complete before commit."
            )
        else:
            output("Next: contributions INSTANCE commit; the command resumes saved work.")
    else:
        output("Use contributions INSTANCE inspect to see submitted and accepted changes.")
    output(f"Builder runs: https://github.com/{REPOSITORY}/actions/workflows/build.yml")
    output("A source commit is not proof of a successful build or a published API update.")
