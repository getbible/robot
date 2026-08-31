// Encoder for the GBC1 contribution push protocol.
//
// A push travels through Telegram's own uplink: each message is handed to
// Telegram.WebApp.sendData() from the contributor reply-keyboard launch and
// arrives at the bot as a web_app_data service message on its normal update
// channel. No HTTP endpoint, port, or bearer capability participates.
//
// Wire format per message (ASCII, at most 4096 bytes):
//   GBC1|<sync_id>|<index>|<count>|<encoding>|<digest>|<payload>
// where encoding is "d" (zlib deflate) or "j" (plain UTF-8 JSON), digest is
// the SHA-256 hex of the serialized plaintext envelope, and payload is a
// base64url slice of the (optionally compressed) envelope bytes.

export const PUSH_PROTOCOL_PREFIX = "GBC1";
export const MAX_PUSH_MESSAGE_BYTES = 4096;
export const MAX_PUSH_CHUNKS = 64;
export const MAX_PUSH_CHUNK_PAYLOAD_CHARS = 3584;

const SYNC_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;

export async function buildPushMessages(envelope, { compress } = {}) {
  const syncId = envelope?.sync_id;
  if (typeof syncId !== "string" || !SYNC_ID_PATTERN.test(syncId)) {
    throw new TypeError("A push envelope requires a safe sync_id.");
  }
  const serialized = JSON.stringify(envelope);
  const plaintext = new TextEncoder().encode(serialized);
  const digest = await sha256Hex(plaintext);
  let encoding = "j";
  let body = plaintext;
  const deflate = compress === undefined ? defaultCompressor() : compress;
  if (typeof deflate === "function") {
    try {
      const compressed = await deflate(plaintext);
      if (
        compressed instanceof Uint8Array &&
        compressed.byteLength > 0 &&
        compressed.byteLength < plaintext.byteLength
      ) {
        encoding = "d";
        body = compressed;
      }
    } catch {
      // Compression is an optimization; the plain encoding always works.
    }
  }
  const payload = base64UrlEncode(body);
  const chunkCount = Math.max(
    1,
    Math.ceil(payload.length / MAX_PUSH_CHUNK_PAYLOAD_CHARS),
  );
  if (chunkCount > MAX_PUSH_CHUNKS) {
    throw new RangeError("The contribution is too large to push in one transfer.");
  }
  const messages = [];
  for (let index = 0; index < chunkCount; index += 1) {
    const slice = payload.slice(
      index * MAX_PUSH_CHUNK_PAYLOAD_CHARS,
      (index + 1) * MAX_PUSH_CHUNK_PAYLOAD_CHARS,
    );
    const message = [
      PUSH_PROTOCOL_PREFIX,
      syncId,
      String(index + 1),
      String(chunkCount),
      encoding,
      digest,
      slice,
    ].join("|");
    // Every part of the message is ASCII, so length equals byte length.
    if (message.length > MAX_PUSH_MESSAGE_BYTES) {
      throw new RangeError("A push message exceeds the Telegram sendData bound.");
    }
    messages.push(message);
  }
  return { sync_id: syncId, digest, encoding, messages };
}

function defaultCompressor() {
  if (typeof globalThis.CompressionStream !== "function") {
    return null;
  }
  return async (bytes) => {
    const stream = new Blob([bytes])
      .stream()
      .pipeThrough(new CompressionStream("deflate"));
    const buffer = await new Response(stream).arrayBuffer();
    return new Uint8Array(buffer);
  };
}

async function sha256Hex(bytes) {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function base64UrlEncode(bytes) {
  let binary = "";
  const step = 0x8000;
  for (let index = 0; index < bytes.length; index += step) {
    binary += String.fromCharCode(...bytes.subarray(index, index + step));
  }
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}
