import base64
import hashlib
import json
import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import ReplyKeyboardRemove
from telegram.error import BadRequest

from modules.commands import LIMITER_SLOT, SETTINGS_SLOT
from modules.contribution_intake import (
    MAX_PUSH_PLAINTEXT_BYTES,
    PushMessageError,
    contribution_push_message,
    decode_push_bundle,
    parse_push_message,
)
from modules.contributions import ContributionStore, PushChunkResult
from modules.contributor_command import (
    CONTRIBUTION_STORE_SLOT,
    PUSH_KEYBOARD_BUTTON_TEXT,
)
from modules.errors import RobotRateLimited
from modules.rate_limit import InboundRateLimiter

SYNC_ID = "sync.push.0001"
PROGRESS_MESSAGE_ID = 901


def _envelope(**overrides: object) -> dict[str, object]:
    envelope: dict[str, object] = {
        "protocol_version": 1,
        "sync_id": SYNC_ID,
        "client_id": "browser.primary",
        "snapshot": {
            "topics": [{"id": "local.grace", "name": "Grace", "color": "#BBF7D0"}],
            "assignments": [
                {"topic_id": "local.grace", "book": 43, "chapter": 3, "verse": 16}
            ],
        },
        "operations": [],
        "disclosure_acknowledged": True,
    }
    envelope.update(overrides)
    return envelope


def _encode(plaintext: bytes, encoding: str) -> str:
    body = zlib.compress(plaintext) if encoding == "d" else plaintext
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def _messages(
    envelope: dict[str, object],
    *,
    encoding: str = "d",
    chunks: int = 1,
    bundle_id: str | None = None,
) -> list[str]:
    plaintext = json.dumps(envelope).encode("utf-8")
    digest = hashlib.sha256(plaintext).hexdigest()
    payload = _encode(plaintext, encoding)
    size = -(-len(payload) // chunks)
    slices = [payload[offset : offset + size] for offset in range(0, len(payload), size)]
    assert len(slices) == chunks
    bundle = bundle_id if bundle_id is not None else str(envelope["sync_id"])
    return [
        f"GBC1|{bundle}|{index}|{chunks}|{encoding}|{digest}|{piece}"
        for index, piece in enumerate(slices, start=1)
    ]


def _staged(
    payload: str | None,
    *,
    encoding: str = "j",
    digest: str,
    complete: bool = True,
    progress_message_id: int | None = None,
) -> PushChunkResult:
    return PushChunkResult(
        complete=complete,
        received=1,
        chunk_count=1,
        encoding=encoding,
        digest=digest,
        payload=payload,
        progress_message_id=progress_message_id,
        restarted=False,
    )


class ParsePushMessageTestCase(unittest.TestCase):
    def test_valid_message_roundtrips_every_protocol_field(self) -> None:
        plaintext = json.dumps(_envelope()).encode("utf-8")
        message = _messages(_envelope(), encoding="j")[0]

        chunk = parse_push_message(message)

        self.assertEqual(chunk.bundle_id, SYNC_ID)
        self.assertEqual(chunk.index, 1)
        self.assertEqual(chunk.count, 1)
        self.assertEqual(chunk.encoding, "j")
        self.assertEqual(chunk.digest, hashlib.sha256(plaintext).hexdigest())
        self.assertEqual(chunk.payload, _encode(plaintext, "j"))

    def test_wrong_protocol_prefix_is_rejected(self) -> None:
        message = _messages(_envelope())[0].replace("GBC1|", "GBC2|", 1)
        with self.assertRaisesRegex(PushMessageError, "GBC1 protocol"):
            parse_push_message(message)

    def test_non_text_data_is_rejected(self) -> None:
        with self.assertRaisesRegex(PushMessageError, "must be text"):
            parse_push_message(b"GBC1|x|1|1|j|" + b"0" * 64 + b"|AAAA")

    def test_message_over_the_senddata_bound_is_rejected(self) -> None:
        message = f"GBC1|{SYNC_ID}|1|1|j|{'0' * 64}|{'A' * 4096}"
        self.assertGreater(len(message.encode("utf-8")), 4096)
        with self.assertRaisesRegex(PushMessageError, "sendData bound"):
            parse_push_message(message)

    def test_chunk_payload_over_the_chunk_bound_is_rejected(self) -> None:
        message = f"GBC1|{SYNC_ID}|1|1|j|{'0' * 64}|{'A' * 3585}"
        self.assertLessEqual(len(message.encode("utf-8")), 4096)
        with self.assertRaisesRegex(PushMessageError, "payload is too large"):
            parse_push_message(message)

    def test_index_greater_than_count_is_rejected(self) -> None:
        message = f"GBC1|{SYNC_ID}|3|2|j|{'0' * 64}|AAAA"
        with self.assertRaisesRegex(PushMessageError, "numbering is out of bounds"):
            parse_push_message(message)

    def test_count_over_the_chunk_limit_is_rejected(self) -> None:
        message = f"GBC1|{SYNC_ID}|1|65|j|{'0' * 64}|AAAA"
        with self.assertRaisesRegex(PushMessageError, "numbering is out of bounds"):
            parse_push_message(message)

    def test_unknown_encoding_letter_is_rejected(self) -> None:
        message = f"GBC1|{SYNC_ID}|1|1|x|{'0' * 64}|AAAA"
        with self.assertRaisesRegex(PushMessageError, "encoding is not supported"):
            parse_push_message(message)

    def test_digest_outside_lowercase_hex_is_rejected(self) -> None:
        for digest in ("G" * 64, "A" * 64, "0" * 63):
            with self.subTest(digest=digest[:4]):
                message = f"GBC1|{SYNC_ID}|1|1|j|{digest}|AAAA"
                with self.assertRaisesRegex(PushMessageError, "GBC1 protocol"):
                    parse_push_message(message)

    def test_payload_outside_base64url_alphabet_is_rejected(self) -> None:
        for payload in ("AAA+", "AAA/", "AAAA==", ""):
            with self.subTest(payload=payload):
                message = f"GBC1|{SYNC_ID}|1|1|j|{'0' * 64}|{payload}"
                with self.assertRaisesRegex(PushMessageError, "GBC1 protocol"):
                    parse_push_message(message)


class DecodePushBundleTestCase(unittest.TestCase):
    def test_complete_bundle_decodes_in_both_encodings(self) -> None:
        envelope = _envelope()
        plaintext = json.dumps(envelope).encode("utf-8")
        digest = hashlib.sha256(plaintext).hexdigest()
        for encoding in ("d", "j"):
            with self.subTest(encoding=encoding):
                staged = _staged(
                    _encode(plaintext, encoding),
                    encoding=encoding,
                    digest=digest,
                )
                self.assertEqual(decode_push_bundle(staged), envelope)

    def test_incomplete_bundle_is_rejected(self) -> None:
        staged = _staged(None, digest="0" * 64, complete=False)
        with self.assertRaisesRegex(PushMessageError, "not complete"):
            decode_push_bundle(staged)

    def test_digest_mismatch_is_rejected(self) -> None:
        plaintext = json.dumps(_envelope()).encode("utf-8")
        staged = _staged(
            _encode(plaintext, "j"),
            digest=hashlib.sha256(b"something else").hexdigest(),
        )
        with self.assertRaisesRegex(PushMessageError, "digest verification failed"):
            decode_push_bundle(staged)

    def test_decompression_bomb_is_rejected_by_the_plaintext_bound(self) -> None:
        plaintext = b"\x00" * (MAX_PUSH_PLAINTEXT_BYTES + 1)
        staged = _staged(
            _encode(plaintext, "d"),
            encoding="d",
            digest=hashlib.sha256(plaintext).hexdigest(),
        )
        with self.assertRaisesRegex(PushMessageError, "plaintext bound"):
            decode_push_bundle(staged)

    def test_deeply_nested_json_is_rejected_not_raised(self) -> None:
        plaintext = b"[" * 60_000 + b"]" * 60_000
        staged = _staged(
            _encode(plaintext, "j"),
            digest=hashlib.sha256(plaintext).hexdigest(),
        )
        # json.loads answers pathological nesting with RecursionError; the
        # decoder must convert it into the ordinary unreadable outcome.
        with self.assertRaisesRegex(PushMessageError, "not valid JSON"):
            decode_push_bundle(staged)

    def test_wrong_envelope_key_set_is_rejected(self) -> None:
        extra = _envelope()
        extra["extra"] = True
        missing = _envelope()
        del missing["operations"]
        for label, envelope in (("extra", extra), ("missing", missing)):
            with self.subTest(shape=label):
                plaintext = json.dumps(envelope).encode("utf-8")
                staged = _staged(
                    _encode(plaintext, "j"),
                    digest=hashlib.sha256(plaintext).hexdigest(),
                )
                with self.assertRaisesRegex(PushMessageError, "protocol version 1"):
                    decode_push_bundle(staged)

    def test_protocol_version_other_than_one_is_rejected(self) -> None:
        for version in (2, True, "1"):
            with self.subTest(version=repr(version)):
                plaintext = json.dumps(_envelope(protocol_version=version)).encode("utf-8")
                staged = _staged(
                    _encode(plaintext, "j"),
                    digest=hashlib.sha256(plaintext).hexdigest(),
                )
                with self.assertRaisesRegex(PushMessageError, "protocol_version must be 1"):
                    decode_push_bundle(staged)


def _limiter() -> InboundRateLimiter:
    return InboundRateLimiter(
        user_capacity=10,
        user_refill_per_second=1.0,
        chat_capacity=10,
        chat_refill_per_second=1.0,
        max_entries=100,
    )


def _context(
    store: ContributionStore | None,
    *,
    limiter: InboundRateLimiter | None = None,
) -> SimpleNamespace:
    data = {
        LIMITER_SLOT: limiter or _limiter(),
        SETTINGS_SLOT: SimpleNamespace(
            audit_log_mode="metadata",
            audit_identity_mode="disabled",
            telegram_api_token="123456:abcdefghijklmnopqrstuvwxyzABCDEFGHIJ",
        ),
    }
    if store is not None:
        data[CONTRIBUTION_STORE_SLOT] = store
    return SimpleNamespace(
        application=SimpleNamespace(bot_data=data),
        bot=SimpleNamespace(
            send_message=AsyncMock(
                return_value=SimpleNamespace(message_id=PROGRESS_MESSAGE_ID)
            ),
            edit_message_text=AsyncMock(),
            delete_message=AsyncMock(),
        ),
    )


def _update(data: object, *, private: bool = True) -> tuple[SimpleNamespace, AsyncMock]:
    delete = AsyncMock()
    update = SimpleNamespace(
        effective_message=SimpleNamespace(
            web_app_data=SimpleNamespace(data=data),
            delete=delete,
        ),
        effective_chat=SimpleNamespace(
            id=42,
            type="private" if private else "group",
        ),
        effective_user=SimpleNamespace(
            id=42,
            first_name="Grace",
            last_name="Reader",
            username="grace_reader",
            language_code="en",
        ),
    )
    return update, delete


class ContributionPushMessageTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = ContributionStore(path=None)

    async def asyncTearDown(self) -> None:
        self.store.close()

    def approve(self, *, disclosure: bool = True) -> None:
        self.store.submit_application(42, first_name="Grace")
        self.store.decide_application(42, "approved", actor="admin")
        if disclosure:
            self.store.acknowledge_disclosure(42)

    async def test_single_chunk_push_is_committed_and_confirmed(self) -> None:
        self.approve()
        update, delete = _update(_messages(_envelope())[0])
        context = _context(self.store)

        await contribution_push_message(update, context)

        delete.assert_awaited_once_with()
        sent = context.bot.send_message.await_args
        self.assertEqual(sent.kwargs["chat_id"], 42)
        self.assertIn("Contribution received", sent.kwargs["text"])
        self.assertIn("queued for review", sent.kwargs["text"])
        self.assertEqual(
            [event.event_type for event in self.store.list_events()],
            ["topic_upsert", "verse_add"],
        )
        receipt = self.store.sync_receipt(42, SYNC_ID)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["accepted"], 2)

    async def test_multi_chunk_transfer_edits_one_progress_message(self) -> None:
        self.approve()
        first, second = _messages(_envelope(), chunks=2)
        context = _context(self.store)

        update, first_delete = _update(first)
        await contribution_push_message(update, context)

        first_delete.assert_awaited_once_with()
        progress = context.bot.send_message.await_args
        self.assertIn("Received part 1 of 2", progress.kwargs["text"])
        self.assertIn(PUSH_KEYBOARD_BUTTON_TEXT, progress.kwargs["text"])
        context.bot.edit_message_text.assert_not_awaited()
        self.assertIsNone(self.store.sync_receipt(42, SYNC_ID))

        update, second_delete = _update(second)
        await contribution_push_message(update, context)

        second_delete.assert_awaited_once_with()
        # The remembered progress message becomes the confirmation; no
        # second notice is sent for the completing chunk.
        context.bot.send_message.assert_awaited_once()
        edited = context.bot.edit_message_text.await_args
        self.assertEqual(edited.kwargs["chat_id"], 42)
        self.assertEqual(edited.kwargs["message_id"], PROGRESS_MESSAGE_ID)
        self.assertIn("queued for review", edited.kwargs["text"])
        self.assertEqual(len(self.store.list_events()), 2)

    async def test_envelope_sync_id_must_match_the_transport_bundle_id(self) -> None:
        self.approve()
        message = _messages(_envelope(), bundle_id="sync.other")[0]
        update, delete = _update(message)
        context = _context(self.store)

        await contribution_push_message(update, context)

        delete.assert_awaited_once_with()
        self.assertIn(
            "could not be read",
            context.bot.send_message.await_args.kwargs["text"],
        )
        self.assertEqual(self.store.list_events(), ())
        self.assertIsNone(self.store.sync_receipt(42, SYNC_ID))
        self.assertIsNone(self.store.sync_receipt(42, "sync.other"))

    async def test_unapproved_sender_is_refused_and_keyboard_removed(self) -> None:
        update, delete = _update(_messages(_envelope())[0])
        context = _context(self.store)

        await contribution_push_message(update, context)

        delete.assert_awaited_once_with()
        sent = context.bot.send_message.await_args
        self.assertIn("not an approved contributor", sent.kwargs["text"])
        self.assertIsInstance(sent.kwargs["reply_markup"], ReplyKeyboardRemove)
        self.assertEqual(self.store.list_events(), ())

    async def test_missing_disclosure_gets_the_notice_and_keeps_the_keyboard(self) -> None:
        self.approve(disclosure=False)
        message = _messages(_envelope(disclosure_acknowledged=False))[0]
        update, delete = _update(message)
        context = _context(self.store)

        await contribution_push_message(update, context)

        delete.assert_awaited_once_with()
        sent = context.bot.send_message.await_args
        self.assertIn("disclosure must be acknowledged", sent.kwargs["text"])
        self.assertIsNone(sent.kwargs["reply_markup"])
        self.assertEqual(self.store.list_events(), ())

    async def test_replaying_a_full_transfer_duplicates_nothing(self) -> None:
        self.approve()
        message = _messages(_envelope())[0]
        await contribution_push_message(_update(message)[0], _context(self.store))
        self.assertEqual(len(self.store.list_events()), 2)

        update, delete = _update(message)
        context = _context(self.store)
        await contribution_push_message(update, context)

        delete.assert_awaited_once_with()
        text = context.bot.send_message.await_args.kwargs["text"]
        self.assertIn("already received", text)
        self.assertIn("nothing was duplicated", text)
        self.assertEqual(len(self.store.list_events()), 2)

    async def test_missing_store_reports_unavailable_after_consuming(self) -> None:
        update, delete = _update(_messages(_envelope())[0])
        context = _context(None)

        await contribution_push_message(update, context)

        delete.assert_awaited_once_with()
        self.assertIn(
            "temporarily unavailable",
            context.bot.send_message.await_args.kwargs["text"],
        )

    async def test_group_chat_update_is_consumed_without_any_reply(self) -> None:
        self.approve()
        update, delete = _update(_messages(_envelope())[0], private=False)
        context = _context(self.store)

        await contribution_push_message(update, context)

        delete.assert_awaited_once_with()
        context.bot.send_message.assert_not_awaited()
        context.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(self.store.list_events(), ())
        self.assertIsNone(self.store.sync_receipt(42, SYNC_ID))

    async def test_redelivered_chunk_after_commit_replays_without_restaging(self) -> None:
        self.approve()
        first, second = _messages(_envelope(), chunks=2)
        await contribution_push_message(_update(first)[0], _context(self.store))
        await contribution_push_message(_update(second)[0], _context(self.store))
        self.assertIsNotNone(self.store.sync_receipt(42, SYNC_ID))

        update, delete = _update(first)
        context = _context(self.store)
        await contribution_push_message(update, context)

        delete.assert_awaited_once_with()
        text = context.bot.send_message.await_args.kwargs["text"]
        # A redelivered chunk of a committed transfer must answer with the
        # replay notice, never re-stage a ghost bundle whose progress message
        # would contradict the confirmation the contributor already saw.
        self.assertIn("already received", text)
        self.assertNotIn("Received part", text)
        connection = self.store._connection_required()
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM contribution_push_bundles"
            ).fetchone()[0],
            0,
        )

    async def test_identical_progress_edit_rejection_sends_no_duplicate(self) -> None:
        self.approve()
        first, _second, _third = _messages(_envelope(), chunks=3)
        context = _context(self.store)
        await contribution_push_message(_update(first)[0], context)
        context.bot.send_message.assert_awaited_once()

        # Telegram rejects edits whose text is byte-identical, which is what
        # an idempotently redelivered chunk produces; the stored message
        # already shows the right progress, so nothing new may be posted.
        context.bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message is not modified")
        )
        await contribution_push_message(_update(first)[0], context)

        context.bot.send_message.assert_awaited_once()
        context.bot.delete_message.assert_not_awaited()

    async def test_losing_progress_candidate_is_deleted(self) -> None:
        self.approve()
        first = _messages(_envelope(), chunks=3)[0]
        update, delete = _update(first)
        context = _context(self.store)

        # Another concurrently handled chunk adopted its own message between
        # this handler's staging and adoption; the duplicate must be removed.
        with patch.object(
            self.store,
            "set_push_progress_message",
            return_value=False,
        ):
            await contribution_push_message(update, context)

        delete.assert_awaited_once_with()
        context.bot.send_message.assert_awaited_once()
        context.bot.delete_message.assert_awaited_once_with(
            chat_id=42,
            message_id=PROGRESS_MESSAGE_ID,
        )

    async def test_rate_limited_push_is_consumed_without_staging(self) -> None:
        self.approve()
        limiter = _limiter()
        limiter.acquire = AsyncMock(side_effect=RobotRateLimited(3))
        limiter.should_notify_rejection = AsyncMock(return_value=False)
        update, delete = _update(_messages(_envelope())[0])
        context = _context(self.store, limiter=limiter)

        with patch.object(self.store, "stage_push_chunk") as staging:
            await contribution_push_message(update, context)

        delete.assert_awaited_once_with()
        limiter.acquire.assert_awaited_once_with(user_id=42, chat_id=42)
        staging.assert_not_called()
        context.bot.send_message.assert_not_awaited()
        self.assertEqual(self.store.list_events(), ())


if __name__ == "__main__":
    unittest.main()
