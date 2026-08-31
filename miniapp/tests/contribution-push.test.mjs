import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import zlib from "node:zlib";

import {
  MAX_PUSH_CHUNK_PAYLOAD_CHARS,
  MAX_PUSH_CHUNKS,
  MAX_PUSH_MESSAGE_BYTES,
  PUSH_PROTOCOL_PREFIX,
  buildPushMessages,
} from "../lib/contribution-push.js";

const SYNC_ID = "stable-client:s:1:0123456789abcdef";

function envelopeFixture(overrides = {}) {
  return {
    protocol_version: 1,
    sync_id: SYNC_ID,
    client_id: "stable-client",
    snapshot: {
      topics: [{ id: "my-grace", name: "My Grace", color: "#bbf7d0" }],
      assignments: [{ topic_id: "my-grace", book: 43, chapter: 3, verse: 16 }],
    },
    operations: [],
    disclosure_acknowledged: true,
    ...overrides,
  };
}

function operations(count) {
  return Array.from({ length: count }, (_, index) => ({
    client_event_id: "stable-client:e:" + index.toString(36),
    type: "verse_add",
    topic: {
      local_topic_id: "authority",
      name: "Authority of the Bible",
      color: "#93c5fd",
    },
    verse: { book: 1, chapter: 1, verse: index + 1 },
  }));
}

/** Reverse the wire format exactly as the bot's intake must. */
function decodeTransfer(result) {
  const parts = result.messages.map((message) => message.split("|"));
  for (const [index, fields] of parts.entries()) {
    assert.equal(fields.length, 7);
    assert.equal(fields[0], PUSH_PROTOCOL_PREFIX);
    assert.equal(fields[1], result.sync_id);
    assert.equal(fields[2], String(index + 1));
    assert.equal(fields[3], String(parts.length));
    assert.equal(fields[4], result.encoding);
    assert.equal(fields[5], result.digest);
    assert.match(fields[5], /^[0-9a-f]{64}$/);
  }
  const payload = parts.map((fields) => fields[6]).join("");
  const body = Buffer.from(payload, "base64url");
  const plaintext = result.encoding === "d" ? zlib.inflateSync(body) : body;
  const digest = createHash("sha256").update(plaintext).digest("hex");
  return { payload, plaintext, digest };
}

test("a compressed single-chunk transfer round-trips to the exact envelope", async () => {
  const envelope = envelopeFixture({
    // Highly repetitive content so the default deflate wins.
    operations: operations(40),
  });

  const result = await buildPushMessages(envelope);

  assert.equal(result.sync_id, SYNC_ID);
  assert.equal(result.encoding, "d");
  assert.equal(result.messages.length, 1);
  const decoded = decodeTransfer(result);
  assert.equal(decoded.digest, result.digest);
  assert.deepEqual(JSON.parse(decoded.plaintext.toString("utf8")), envelope);
});

test("a plain multi-chunk transfer reassembles across numbered chunks", async () => {
  const envelope = envelopeFixture({ operations: operations(300) });

  const result = await buildPushMessages(envelope, { compress: null });

  assert.equal(result.encoding, "j");
  assert.ok(result.messages.length > 1);
  assert.ok(result.messages.length <= MAX_PUSH_CHUNKS);

  const decoded = decodeTransfer(result);
  assert.equal(
    result.messages.length,
    Math.ceil(decoded.payload.length / MAX_PUSH_CHUNK_PAYLOAD_CHARS),
  );
  for (const message of result.messages) {
    assert.ok(Buffer.byteLength(message, "utf8") <= MAX_PUSH_MESSAGE_BYTES);
    const slice = message.split("|")[6];
    assert.ok(slice.length >= 1);
    assert.ok(slice.length <= MAX_PUSH_CHUNK_PAYLOAD_CHARS);
  }
  for (const message of result.messages.slice(0, -1)) {
    assert.equal(message.split("|")[6].length, MAX_PUSH_CHUNK_PAYLOAD_CHARS);
  }
  assert.equal(decoded.digest, result.digest);
  assert.deepEqual(JSON.parse(decoded.plaintext.toString("utf8")), envelope);
});

test("the digest always covers the plaintext, not the compressed body", async () => {
  const envelope = envelopeFixture({ operations: operations(40) });
  const plaintextDigest = createHash("sha256")
    .update(JSON.stringify(envelope), "utf8")
    .digest("hex");

  const compressed = await buildPushMessages(envelope);
  const plain = await buildPushMessages(envelope, { compress: null });

  assert.equal(compressed.encoding, "d");
  assert.equal(compressed.digest, plaintextDigest);
  assert.equal(plain.encoding, "j");
  assert.equal(plain.digest, plaintextDigest);
});

test("compression is used only when it strictly shrinks the envelope", async () => {
  const deflate = async (bytes) => new Uint8Array(zlib.deflateSync(bytes));
  const envelope = envelopeFixture();

  // Equal size is not smaller: the plain encoding must win the tie.
  const tied = await buildPushMessages(envelope, {
    compress: async (bytes) => new Uint8Array(bytes),
  });
  assert.equal(tied.encoding, "j");
  assert.deepEqual(
    JSON.parse(decodeTransfer(tied).plaintext.toString("utf8")),
    envelope,
  );

  const repetitive = envelopeFixture({ operations: operations(40) });
  const compressed = await buildPushMessages(repetitive, { compress: deflate });
  assert.equal(compressed.encoding, "d");
  const decoded = decodeTransfer(compressed);
  assert.deepEqual(JSON.parse(decoded.plaintext.toString("utf8")), repetitive);
});

test("a failing or useless compressor falls back to plain encoding", async () => {
  const envelope = envelopeFixture({ operations: operations(40) });

  const throwing = await buildPushMessages(envelope, {
    compress: async () => {
      throw new Error("no zlib here");
    },
  });
  assert.equal(throwing.encoding, "j");
  assert.deepEqual(
    JSON.parse(decodeTransfer(throwing).plaintext.toString("utf8")),
    envelope,
  );

  const inflating = await buildPushMessages(envelope, {
    compress: async (bytes) => new Uint8Array(bytes.byteLength + 100),
  });
  assert.equal(inflating.encoding, "j");
});

test("an envelope needing more than 64 chunks is rejected", async () => {
  // 64 chunks carry at most 64 * 3584 base64url chars (~168 KiB of bytes).
  const envelope = envelopeFixture({
    snapshot: { topics: [], assignments: [] },
    operations: [],
    padding: "x".repeat(200_000),
  });

  await assert.rejects(
    buildPushMessages(envelope, { compress: null }),
    RangeError,
  );
});

test("a missing or unsafe sync_id is rejected before encoding", async () => {
  await assert.rejects(buildPushMessages(undefined), TypeError);
  await assert.rejects(
    buildPushMessages(envelopeFixture({ sync_id: undefined })),
    TypeError,
  );
  await assert.rejects(
    buildPushMessages(envelopeFixture({ sync_id: 42 })),
    TypeError,
  );
  await assert.rejects(
    buildPushMessages(envelopeFixture({ sync_id: "pipes|break|framing" })),
    TypeError,
  );
  await assert.rejects(
    buildPushMessages(envelopeFixture({ sync_id: "s".repeat(129) })),
    TypeError,
  );
  await assert.rejects(
    buildPushMessages(envelopeFixture({ sync_id: "" })),
    TypeError,
  );
});
