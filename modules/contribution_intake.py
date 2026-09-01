"""Telegram ``web_app_data`` intake for contributor push bundles.

The Mini App's Push action serializes one contribution envelope, compresses
it, and sends it through ``Telegram.WebApp.sendData()`` from the contributor
push keyboard button.  Telegram delivers each numbered chunk to the bot as an
ordinary ``message`` update over the existing polling or webhook channel, so
this intake needs no listener, port, proxy route, or bearer capability: the
sender is the already-authenticated ``update.effective_user``.

Every service message is consumed: it is deleted from the private chat right
after its chunk is durably staged, and a completed bundle is decoded,
digest-verified, and committed through the same atomic, replay-safe
``ContributionStore.synchronize_snapshot`` path the review pipeline trusts.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import sqlite3
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from telegram import ReplyKeyboardRemove, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import Settings

from .audit import audit_event, audit_identity
from .commands import LIMITER_SLOT, SETTINGS_SLOT, allow_command
from .contributions import (
    MAX_PUSH_CHUNK_PAYLOAD_CHARS,
    MAX_PUSH_CHUNKS,
    PUSH_ENCODINGS,
    ContributionError,
    ContributionIdempotencyConflict,
    ContributionNotAllowed,
    ContributionStore,
    PushChunkResult,
    SnapshotSyncResult,
)
from .contributor_command import (
    CONTRIBUTION_STORE_SLOT,
    PUSH_KEYBOARD_BUTTON_TEXT,
)
from .rate_limit import InboundRateLimiter

LOGGER = logging.getLogger(__name__)

PUSH_PROTOCOL_PREFIX = "GBC1"
MAX_PUSH_MESSAGE_BYTES = 4096
MAX_PUSH_PLAINTEXT_BYTES = 1024 * 1024
_PUSH_MESSAGE_RE = re.compile(
    r"GBC1\|(?P<bundle_id>[A-Za-z0-9._:-]{1,128})"
    r"\|(?P<index>[1-9][0-9]{0,2})\|(?P<count>[1-9][0-9]{0,2})"
    r"\|(?P<encoding>[a-z])\|(?P<digest>[0-9a-f]{64})"
    r"\|(?P<payload>[A-Za-z0-9_-]+)\Z"
)
_ENVELOPE_FIELDS = frozenset(
    {
        "protocol_version",
        "sync_id",
        "client_id",
        "snapshot",
        "operations",
        "disclosure_acknowledged",
    }
)

_UNAVAILABLE_TEXT = (
    "Contributor synchronization is temporarily unavailable on this instance. "
    "Your contribution remains safe in the Mini App; please push again later."
)
_UNREADABLE_TEXT = (
    "The pushed contribution could not be read. "
    "Open the Mini App and push again."
)
_INVALID_TEXT = (
    "The pushed contribution could not be accepted. "
    "Open the Mini App and push again."
)
_CONFLICT_TEXT = (
    "This push conflicts with an earlier submission. "
    "Open the Mini App and push again."
)
_NOT_APPROVED_TEXT = (
    "Your Telegram account is not an approved contributor, so this "
    "contribution was not accepted. Send /contributor to check your status."
)
_DISCLOSURE_TEXT = (
    "The contributor disclosure must be acknowledged before pushing. "
    "Open the Mini App, review the notice, and push again."
)


_REPLAYED_TEXT = (
    "This contribution was already received; nothing was duplicated. "
    "Pull inside the Mini App to see review progress."
)


class PushMessageError(ValueError):
    """A web_app_data payload is not a valid contribution push message."""


@dataclass(frozen=True, slots=True)
class PushChunk:
    """One parsed, transport-validated push message."""

    bundle_id: str
    index: int
    count: int
    encoding: str
    digest: str
    payload: str


def parse_push_message(data: object) -> PushChunk:
    """Parse and bound one raw ``web_app_data.data`` string."""
    if not isinstance(data, str):
        raise PushMessageError("Push data must be text.")
    if len(data.encode("utf-8")) > MAX_PUSH_MESSAGE_BYTES:
        raise PushMessageError("Push message exceeds the Telegram sendData bound.")
    match = _PUSH_MESSAGE_RE.fullmatch(data)
    if match is None:
        raise PushMessageError("Push message does not match the GBC1 protocol.")
    index = int(match.group("index"))
    count = int(match.group("count"))
    encoding = match.group("encoding")
    payload = match.group("payload")
    if count > MAX_PUSH_CHUNKS or index > count:
        raise PushMessageError("Push chunk numbering is out of bounds.")
    if encoding not in PUSH_ENCODINGS:
        raise PushMessageError("Push encoding is not supported.")
    if len(payload) > MAX_PUSH_CHUNK_PAYLOAD_CHARS:
        raise PushMessageError("Push chunk payload is too large.")
    return PushChunk(
        bundle_id=match.group("bundle_id"),
        index=index,
        count=count,
        encoding=encoding,
        digest=match.group("digest"),
        payload=payload,
    )


def decode_push_bundle(staged: PushChunkResult) -> dict[str, object]:
    """Decode, bound, digest-verify, and shape-check one assembled bundle."""
    assembled = staged.payload
    if not staged.complete or assembled is None:
        raise PushMessageError("Push bundle is not complete.")
    try:
        decoded = base64.urlsafe_b64decode(
            assembled + "=" * (-len(assembled) % 4)
        )
    except (binascii.Error, ValueError) as error:
        raise PushMessageError("Push bundle is not valid base64url.") from error
    plaintext = _bounded_inflate(decoded) if staged.encoding == "d" else decoded
    if len(plaintext) > MAX_PUSH_PLAINTEXT_BYTES:
        raise PushMessageError("Push bundle exceeds the plaintext bound.")
    if hashlib.sha256(plaintext).hexdigest() != staged.digest:
        raise PushMessageError("Push bundle digest verification failed.")
    try:
        envelope = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise PushMessageError("Push bundle is not valid JSON.") from error
    if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
        raise PushMessageError("Push bundle does not match protocol version 1.")
    if type(envelope["protocol_version"]) is not int or envelope["protocol_version"] != 1:
        raise PushMessageError("Push bundle protocol_version must be 1.")
    sync_id = envelope["sync_id"]
    client_id = envelope["client_id"]
    if not isinstance(sync_id, str) or not sync_id:
        raise PushMessageError("Push bundle sync_id must be non-empty text.")
    if not isinstance(client_id, str) or not client_id:
        raise PushMessageError("Push bundle client_id must be non-empty text.")
    if not isinstance(envelope["snapshot"], dict):
        raise PushMessageError("Push bundle snapshot must be an object.")
    operations = envelope["operations"]
    if not isinstance(operations, list) or any(
        not isinstance(operation, dict) for operation in operations
    ):
        raise PushMessageError("Push bundle operations must be an array of objects.")
    if not isinstance(envelope["disclosure_acknowledged"], bool):
        raise PushMessageError("Push bundle disclosure_acknowledged must be a boolean.")
    return envelope


def _bounded_inflate(data: bytes) -> bytes:
    """Decompress a zlib stream while refusing decompression bombs."""
    decompressor = zlib.decompressobj()
    try:
        plaintext = decompressor.decompress(data, MAX_PUSH_PLAINTEXT_BYTES + 1)
    except zlib.error as error:
        raise PushMessageError("Push bundle could not be decompressed.") from error
    if (
        len(plaintext) > MAX_PUSH_PLAINTEXT_BYTES
        or decompressor.unconsumed_tail
        or not decompressor.eof
        or decompressor.unused_data
    ):
        raise PushMessageError("Push bundle exceeds the plaintext bound.")
    return plaintext


async def contribution_push_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Consume one web_app_data push chunk from the contributor's private chat.

    The service message is consumed on every path so contribution data never
    lingers in the chat. A chunk that could not be staged (rate limiting, a
    busy store) is not lost: the Mini App keeps the whole transfer durable
    and resends it when no receipt appears, and a completed bundle whose
    commit was interrupted is recovered the same way.
    """
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    web_app_data = getattr(message, "web_app_data", None)
    if web_app_data is None:
        return
    try:
        if chat.type != ChatType.PRIVATE:
            # Keyboard-button Mini Apps exist only in private chats; anything
            # else is spoofed or malformed and is consumed without processing.
            return
        await _process_push_chunk(update, context, chat.id, user.id, web_app_data)
    finally:
        await _consume_service_message(message)


async def _process_push_chunk(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    web_app_data: object,
) -> None:
    data = context.application.bot_data
    limiter = data.get(LIMITER_SLOT)
    if not isinstance(limiter, InboundRateLimiter):
        LOGGER.error("Contribution push intake has no inbound rate limiter")
        await _notify(context, chat_id, _UNAVAILABLE_TEXT)
        return
    if not await allow_command(update, context, limiter):
        return
    store = data.get(CONTRIBUTION_STORE_SLOT)
    if not isinstance(store, ContributionStore):
        await _notify(context, chat_id, _UNAVAILABLE_TEXT)
        return

    try:
        chunk = parse_push_message(getattr(web_app_data, "data", None))
    except PushMessageError:
        _audit_outcome(context, user_id, chat_id, outcome="unreadable")
        await _notify(context, chat_id, _UNREADABLE_TEXT)
        return

    # A transfer whose receipt already exists was fully committed earlier;
    # redelivered or resent chunks must not re-stage a ghost bundle that
    # contradicts the confirmation the contributor already saw.
    try:
        receipt = await asyncio.to_thread(store.sync_receipt, user_id, chunk.bundle_id)
    except (ContributionError, OSError, sqlite3.Error, RuntimeError):
        receipt = None
    if receipt is not None:
        _audit_outcome(context, user_id, chat_id, outcome="replayed")
        await _notify(context, chat_id, _REPLAYED_TEXT)
        return

    try:
        staged = await asyncio.to_thread(
            lambda: store.stage_push_chunk(
                user_id,
                bundle_id=chunk.bundle_id,
                index=chunk.index,
                count=chunk.count,
                encoding=chunk.encoding,
                digest=chunk.digest,
                payload=chunk.payload,
            )
        )
    except ContributionNotAllowed:
        await _reply_not_allowed(context, chat_id, store, user_id)
        return
    except ContributionError:
        _audit_outcome(context, user_id, chat_id, outcome="invalid_chunk")
        await _notify(context, chat_id, _UNREADABLE_TEXT)
        return
    except (OSError, sqlite3.Error, RuntimeError):
        LOGGER.warning("A push chunk could not be staged", exc_info=True)
        await _notify(context, chat_id, _UNAVAILABLE_TEXT)
        return

    if not staged.complete:
        text = (
            f"Received part {staged.received} of {staged.chunk_count}. "
            f"Tap “{PUSH_KEYBOARD_BUTTON_TEXT}” to send the next part."
        )
        message_id = await _show_progress(
            context,
            chat_id,
            staged.progress_message_id,
            text,
        )
        if message_id is not None and message_id != staged.progress_message_id:
            adopted = False
            try:
                adopted = await asyncio.to_thread(
                    store.set_push_progress_message,
                    user_id,
                    chunk.bundle_id,
                    message_id,
                )
            except (ContributionError, OSError, sqlite3.Error, RuntimeError):
                LOGGER.info("Push progress message could not be remembered")
            if not adopted and staged.progress_message_id is None:
                # A concurrent chunk handler adopted its own message first;
                # remove this duplicate instead of littering the chat.
                await _delete_progress(context, chat_id, message_id)
        return

    try:
        envelope = await asyncio.to_thread(decode_push_bundle, staged)
    except PushMessageError:
        _audit_outcome(context, user_id, chat_id, outcome="undecodable")
        await _finalize_progress(
            context,
            chat_id,
            staged.progress_message_id,
            _UNREADABLE_TEXT,
        )
        return
    if envelope["sync_id"] != chunk.bundle_id:
        # The transport bundle identity is the envelope's sync identity; a
        # mismatch means the payload does not belong to this transfer.
        _audit_outcome(context, user_id, chat_id, outcome="identity_mismatch")
        await _finalize_progress(
            context,
            chat_id,
            staged.progress_message_id,
            _UNREADABLE_TEXT,
        )
        return

    try:
        result = await asyncio.to_thread(
            lambda: store.synchronize_snapshot(
                user_id,
                sync_id=str(envelope["sync_id"]),
                client_id=str(envelope["client_id"]),
                snapshot=cast("Mapping[str, object]", envelope["snapshot"]),
                operations=cast(
                    "Sequence[Mapping[str, object]]",
                    envelope["operations"],
                ),
                disclosure_acknowledged=bool(envelope["disclosure_acknowledged"]),
            )
        )
    except ContributionIdempotencyConflict:
        _audit_outcome(context, user_id, chat_id, outcome="idempotency_conflict")
        await _finalize_progress(
            context,
            chat_id,
            staged.progress_message_id,
            _CONFLICT_TEXT,
        )
        return
    except ContributionNotAllowed:
        await _reply_not_allowed(
            context,
            chat_id,
            store,
            user_id,
            progress_message_id=staged.progress_message_id,
        )
        return
    except (ContributionError, RecursionError):
        _audit_outcome(context, user_id, chat_id, outcome="invalid_contribution")
        await _finalize_progress(
            context,
            chat_id,
            staged.progress_message_id,
            _INVALID_TEXT,
        )
        return
    except (OSError, sqlite3.Error, RuntimeError):
        LOGGER.warning("A pushed contribution could not be committed", exc_info=True)
        await _finalize_progress(
            context,
            chat_id,
            staged.progress_message_id,
            _UNAVAILABLE_TEXT,
        )
        return

    _audit_outcome(
        context,
        user_id,
        chat_id,
        outcome="replayed" if result.replayed_sync else "accepted",
    )
    await _finalize_progress(
        context,
        chat_id,
        staged.progress_message_id,
        _confirmation_text(result),
    )


def _confirmation_text(result: SnapshotSyncResult) -> str:
    if result.replayed_sync:
        return _REPLAYED_TEXT
    if result.accepted == 0:
        return (
            "Contribution received. Everything was already up to date, so no "
            "new review items were created."
        )
    updates = "update" if result.accepted == 1 else "updates"
    return (
        f"Contribution received: {result.accepted} {updates} queued for "
        "review. You will be notified here when decisions are available, and "
        "Pull inside the Mini App picks up the reviewed catalogue."
    )


async def _reply_not_allowed(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    store: ContributionStore,
    user_id: int,
    *,
    progress_message_id: int | None = None,
) -> None:
    """Distinguish a missing disclosure from missing contributor authority."""
    disclosure_pending = False
    try:
        application = await asyncio.to_thread(store.application_for, user_id)
        disclosure_pending = (
            application is not None
            and application.state == "approved"
            and application.disclosure_acknowledged_at is None
        )
    except (ContributionError, OSError, sqlite3.Error, RuntimeError):
        LOGGER.info("Contributor state could not be read for a push failure notice")
    if disclosure_pending:
        await _finalize_progress(
            context,
            chat_id,
            progress_message_id,
            _DISCLOSURE_TEXT,
        )
        return
    await _delete_progress(context, chat_id, progress_message_id)
    await _notify(
        context,
        chat_id,
        _NOT_APPROVED_TEXT,
        reply_markup=ReplyKeyboardRemove(),
    )


async def _consume_service_message(message: object) -> None:
    """Best-effort removal of the web_app_data service message from the chat."""
    delete = getattr(message, "delete", None)
    if not callable(delete):
        return
    try:
        await delete()
    except TelegramError:
        LOGGER.info("A push service message could not be deleted")


async def _notify(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    *,
    reply_markup: ReplyKeyboardRemove | None = None,
) -> int | None:
    """Send one plain-text notice; push feedback must never crash intake."""
    try:
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
    except TelegramError:
        LOGGER.warning("A push status notice could not be delivered")
        return None
    message_id = getattr(message, "message_id", None)
    return int(message_id) if isinstance(message_id, int) else None


def _edit_was_redundant(error: TelegramError) -> bool:
    """Telegram rejects edits whose text is byte-identical; that is success."""
    return "not modified" in str(error).casefold()


async def _show_progress(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    progress_message_id: int | None,
    text: str,
) -> int | None:
    """Keep multi-part transfers to one live progress message in the chat."""
    if progress_message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_message_id,
                text=text,
            )
        except TelegramError as error:
            if _edit_was_redundant(error):
                # An idempotently redelivered chunk reproduces the identical
                # progress text; the existing message already says it.
                return progress_message_id
            LOGGER.info("A push progress message could not be edited")
        else:
            return progress_message_id
    return await _notify(context, chat_id, text)


async def _finalize_progress(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    progress_message_id: int | None,
    text: str,
) -> None:
    """Turn the progress message into the final outcome, or send it fresh."""
    if progress_message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_message_id,
                text=text,
            )
        except TelegramError as error:
            if _edit_was_redundant(error):
                return
            LOGGER.info("A push outcome message could not be edited")
        else:
            return
    await _notify(context, chat_id, text)


async def _delete_progress(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    progress_message_id: int | None,
) -> None:
    if progress_message_id is None:
        return
    try:
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=progress_message_id,
        )
    except TelegramError:
        LOGGER.info("A push progress message could not be deleted")


def _audit_outcome(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    *,
    outcome: str,
) -> None:
    """Record one bounded intake outcome without logging contribution content."""
    settings = context.application.bot_data.get(SETTINGS_SLOT)
    if not isinstance(settings, Settings):
        return
    audit_event(
        LOGGER,
        settings,
        "contribution_push",
        metadata={"outcome": outcome},
        identity=audit_identity(settings, user_id=user_id, chat_id=chat_id),
    )
