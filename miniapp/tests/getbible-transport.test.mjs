import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  GetBibleTransport,
  PublicApiError,
} from "../lib/getbible-transport.js";

function response(body, { status = 200, contentType = "application/json" } = {}) {
  return new Response(body, {
    status,
    headers: { "Content-Type": contentType },
  });
}

test("hash-consistent reads verify the exact public response bytes", async () => {
  const raw = JSON.stringify({ abbreviation: "kjv", verses: [] });
  const sha = createHash("sha1").update(raw).digest("hex");
  const requests = [];
  const transport = new GetBibleTransport({
    fetchImplementation: async (url, options) => {
      requests.push({ url: String(url), options });
      return String(url).endsWith(".sha")
        ? response(`${sha}\n`, { contentType: "text/plain" })
        : response(raw);
    },
  });

  const result = await transport.consistentJson(
    "kjv/43/3.json",
    "kjv/43/3.sha",
  );

  assert.equal(result.sha, sha);
  assert.deepEqual(result.payload, { abbreviation: "kjv", verses: [] });
  assert.equal(requests.length, 3);
  for (const request of requests) {
    assert.equal(request.options.credentials, "omit");
    assert.equal(request.options.cache, "no-store");
    assert.equal(request.options.redirect, "error");
    assert.equal(request.options.headers.Authorization, undefined);
  }
});

test("hash mismatch rejects content instead of poisoning the cache", async () => {
  const transport = new GetBibleTransport({
    fetchImplementation: async (url) =>
      String(url).endsWith(".sha")
        ? response(`${"a".repeat(40)}\n`, { contentType: "text/plain" })
        : response(JSON.stringify({ value: 1 })),
  });

  await assert.rejects(
    transport.consistentJson("kjv/43/3.json", "kjv/43/3.sha"),
    (error) =>
      error instanceof PublicApiError && error.code === "checksum_mismatch",
  );
});

test("query references are encoded on the fixed query origin", async () => {
  let requested = null;
  const transport = new GetBibleTransport({
    fetchImplementation: async (url) => {
      requested = String(url);
      return response(JSON.stringify({ books: [] }));
    },
  });

  await transport.query("kjv", "John 3:16; Romans 8:1-2");

  assert.equal(
    requested,
    "https://query.getbible.net/v2/kjv/John%203:16;%20Romans%208:1-2",
  );
});

test("announced oversized responses are rejected before reading", async () => {
  const transport = new GetBibleTransport({
    fetchImplementation: async () => new Response("{}", {
      headers: {
        "Content-Type": "application/json",
        "Content-Length": String(3 * 1024 * 1024),
      },
    }),
  });

  await assert.rejects(
    transport.json("translations.json"),
    (error) =>
      error instanceof PublicApiError &&
      error.code === "public_api_response_too_large",
  );
});
