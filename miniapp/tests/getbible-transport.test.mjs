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

/**
 * A body delivered in separate chunks, with a caller-controlled gap between
 * them, so a deadline can be observed reacting to progress rather than to
 * elapsed time.
 */
function chunkedResponse(chunks, { gapMs = 0 } = {}) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      for (const chunk of chunks) {
        if (gapMs > 0) {
          await new Promise((resolve) => setTimeout(resolve, gapMs));
        }
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

/** Records every armed delay so timer policy can be asserted without waiting. */
function recordingTimers() {
  const armed = [];
  return {
    armed,
    setTimeoutImplementation(callback, delay) {
      armed.push(delay);
      return setTimeout(callback, delay);
    },
    clearTimeoutImplementation(handle) {
      clearTimeout(handle);
    },
  };
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

test("searches are cacheable public GETs on the fixed search origin", async () => {
  const requests = [];
  const transport = new GetBibleTransport({
    fetchImplementation: async (url, options) => {
      requests.push({ url: String(url), options });
      return response(JSON.stringify({ query: {}, results: {}, matches: [] }));
    },
  });

  const parameters = new URLSearchParams({ q: "faith hope", limit: "25", offset: "0" });
  const payload = await transport.search("kjv", parameters);

  assert.deepEqual(payload, { query: {}, results: {}, matches: [] });
  assert.equal(requests.length, 1);
  assert.equal(
    requests[0].url,
    "https://search.getbible.net/v2/kjv?q=faith+hope&limit=25&offset=0",
  );
  const { options } = requests[0];
  assert.equal(options.method, "GET");
  assert.equal(options.headers.Accept, "application/json");
  assert.equal(options.headers.Authorization, undefined);
  assert.equal(options.credentials, "omit");
  // A search is a public read the API marks cacheable; the browser's cache
  // may honour that, unlike the checksum-verified catalogue reads above.
  assert.equal(options.cache, "default");
  assert.equal(options.redirect, "error");
  assert.equal(options.referrerPolicy, "no-referrer");
});

test("search requests refuse anything that is not a clean query string", async () => {
  const transport = new GetBibleTransport({
    fetchImplementation: async () => response("{}"),
  });

  for (const [translation, parameters] of [
    ["../kjv", new URLSearchParams({ q: "x" })],
    ["kjv/John 3:16", new URLSearchParams({ q: "x" })],
    ["KJV", new URLSearchParams({ q: "x" })],
    ["kjv", ""],
    ["kjv", new URLSearchParams()],
    ["kjv", "q=x#fragment"],
    ["kjv", "q=x?again"],
    ["kjv", "q=faith hope"],
    ["kjv", `q=${"x".repeat(9_000)}`],
    ["kjv", { q: "x" }],
  ]) {
    await assert.rejects(
      transport.search(translation, parameters),
      TypeError,
      `${translation} ${String(parameters).slice(0, 40)}`,
    );
  }
});

test("a refused search is explained by its problem document", async () => {
  const cases = [
    [400, "missing_search", "search_invalid", false],
    [400, "request_limit", "search_invalid", false],
    [401, "unauthorized", "search_unavailable", false],
    [404, "translation_not_found", "translation_not_found", false],
    [404, "unknown_version", "search_unavailable", false],
    [405, "method_not_allowed", "search_unavailable", false],
    [429, "rate_limited", "search_rate_limited", true],
    [503, "busy", "search_unavailable", true],
    [503, "repository_unavailable", "search_unavailable", true],
    [503, "readiness_failed", "search_unavailable", true],
    [500, "internal", "search_unavailable", true],
  ];
  for (const [status, code, expectedCode, retryable] of cases) {
    const transport = new GetBibleTransport({
      attempts: 1,
      fetchImplementation: async () => response(
        JSON.stringify({
          type: "about:blank",
          title: "Refused",
          status,
          code,
          detail: `because ${code}`,
          instance: "/v2/kjv",
        }),
        { status, contentType: "application/problem+json" },
      ),
    });
    await assert.rejects(
      transport.search("kjv", new URLSearchParams({ q: "x" })),
      (error) =>
        error instanceof PublicApiError &&
        error.code === expectedCode &&
        error.status === status &&
        error.retryable === retryable &&
        error.retryAfter === null &&
        error.message === `because ${code}`,
      `${status} ${code}`,
    );
  }
});

test("a refusal without a readable problem body still maps by status", async () => {
  const bodies = [
    () => new Response("<html>nope</html>", { status: 503, headers: { "Content-Type": "text/html" } }),
    () => new Response("{not json", { status: 503, headers: { "Content-Type": "application/problem+json" } }),
    () => new Response("[]", { status: 503, headers: { "Content-Type": "application/problem+json" } }),
    () => new Response(`{"pad":"${"x".repeat(20 * 1024)}"}`, {
      status: 503,
      headers: { "Content-Type": "application/problem+json" },
    }),
    () => new Response(null, { status: 503 }),
  ];
  for (const body of bodies) {
    const transport = new GetBibleTransport({
      attempts: 1,
      fetchImplementation: async () => body(),
    });
    await assert.rejects(
      transport.search("kjv", new URLSearchParams({ q: "x" })),
      (error) =>
        error instanceof PublicApiError &&
        error.code === "search_unavailable" &&
        error.status === 503 &&
        error.retryable === true &&
        error.retryAfter === null,
    );
  }
});

test("Retry-After is read from the body first, then the header, and bounded", async () => {
  const cases = [
    [{ retry_after: 12 }, "30", 12],
    [{}, "45", 45],
    [{}, "  ", null],
    [{}, "soon", null],
    [{ retry_after: -1 }, "7", 7],
    [{ retry_after: 99_999 }, null, 3_600],
    [{}, new Date(Date.now() + 90_000).toUTCString(), 89],
  ];
  for (const [extra, header, expected] of cases) {
    const transport = new GetBibleTransport({
      attempts: 1,
      fetchImplementation: async () => new Response(
        JSON.stringify({ status: 429, code: "rate_limited", ...extra }),
        {
          status: 429,
          headers: {
            "Content-Type": "application/problem+json",
            ...(header === null ? {} : { "Retry-After": header }),
          },
        },
      ),
    });
    await assert.rejects(
      transport.search("kjv", new URLSearchParams({ q: "x" })),
      (error) =>
        error.code === "search_rate_limited" &&
        (expected === 89
          ? error.retryAfter >= 88 && error.retryAfter <= 90
          : error.retryAfter === expected),
      JSON.stringify([extra, header]),
    );
  }
});

test("a short Retry-After is waited out and a long one ends the retry loop", async () => {
  const refusal = (retryAfter) => async () => new Response(
    JSON.stringify({ status: 503, code: "busy", retry_after: retryAfter }),
    { status: 503, headers: { "Content-Type": "application/problem+json" } },
  );

  const patientTimers = recordingTimers();
  let patientAttempts = 0;
  const patient = new GetBibleTransport({
    attempts: 2,
    retryBackoffMs: 0,
    fetchImplementation: async () => {
      patientAttempts += 1;
      return refusal(0.5)();
    },
    setTimeoutImplementation: patientTimers.setTimeoutImplementation,
    clearTimeoutImplementation: patientTimers.clearTimeoutImplementation,
  });
  await assert.rejects(
    patient.search("kjv", new URLSearchParams({ q: "x" })),
    (error) => error.code === "search_unavailable",
  );
  assert.equal(patientAttempts, 2);
  assert.ok(patientTimers.armed.includes(500), "the server's wait is honoured");

  let impatientAttempts = 0;
  const impatient = new GetBibleTransport({
    attempts: 3,
    retryBackoffMs: 0,
    fetchImplementation: async () => {
      impatientAttempts += 1;
      return refusal(30)();
    },
  });
  await assert.rejects(
    impatient.search("kjv", new URLSearchParams({ q: "x" })),
    (error) => error.code === "search_unavailable" && error.retryAfter === 30,
  );
  assert.equal(impatientAttempts, 1);
});

test("public API errors keep their original shape for older callers", () => {
  const legacy = new PublicApiError("x", { code: "public_api_failed", status: 503, retryable: true });
  assert.equal(legacy.retryAfter, null);
  const bounded = new PublicApiError("x", { retryAfter: "7200" });
  assert.equal(bounded.retryAfter, 3_600);
  const missing = new PublicApiError("x", { retryAfter: "" });
  assert.equal(missing.retryAfter, null);
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

test("a body that keeps arriving is never abandoned for taking long", async () => {
  // Every gap here exceeds the stall bound on its own. Only a deadline that is
  // rearmed by arriving bytes lets this chapter finish; a wall clock over the
  // whole read would abandon a transfer that was progressing throughout.
  const raw = JSON.stringify({ verses: [1, 2, 3] });
  const transport = new GetBibleTransport({
    stallTimeoutMs: 1_000,
    totalTimeoutMs: 30_000,
    fetchImplementation: async () =>
      chunkedResponse(
        [raw.slice(0, 5), raw.slice(5, 12), raw.slice(12)],
        { gapMs: 700 },
      ),
  });

  assert.deepEqual(await transport.json("kjv/19/119.json"), {
    verses: [1, 2, 3],
  });
});

test("a body that stops arriving is abandoned as a retryable timeout", async () => {
  let attempts = 0;
  const transport = new GetBibleTransport({
    stallTimeoutMs: 1_000,
    totalTimeoutMs: 30_000,
    attempts: 1,
    fetchImplementation: async () => {
      attempts += 1;
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode('{"verses"'));
            // and then nothing, ever
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    },
  });

  await assert.rejects(
    transport.json("kjv/19/119.json"),
    (error) =>
      error instanceof PublicApiError &&
      error.code === "public_api_timeout" &&
      error.retryable === true,
  );
  assert.equal(attempts, 1);
});

test("a stalled read is retried rather than reported as unavailable", async () => {
  const raw = JSON.stringify({ verses: [] });
  let attempts = 0;
  const transport = new GetBibleTransport({
    attempts: 3,
    retryBackoffMs: 0,
    fetchImplementation: async () => {
      attempts += 1;
      if (attempts < 3) {
        throw Object.assign(new Error("aborted"), { name: "AbortError" });
      }
      return response(raw);
    },
  });

  assert.deepEqual(await transport.json("kjv/42/1.json"), { verses: [] });
  assert.equal(attempts, 3);
});

test("transient upstream failures are retried and permanent ones are not", async () => {
  for (const [status, expectedAttempts, code] of [
    [503, 3, "public_api_failed"],
    [429, 3, "public_api_failed"],
    [404, 1, "public_api_not_found"],
  ]) {
    let attempts = 0;
    const transport = new GetBibleTransport({
      attempts: 3,
      retryBackoffMs: 0,
      fetchImplementation: async () => {
        attempts += 1;
        return response("{}", { status });
      },
    });

    await assert.rejects(
      transport.json("kjv/42/1.json"),
      (error) => error instanceof PublicApiError && error.code === code,
    );
    assert.equal(attempts, expectedAttempts, `status ${status}`);
  }
});

test("an oversized body is refused on the first attempt", async () => {
  let attempts = 0;
  const transport = new GetBibleTransport({
    attempts: 3,
    retryBackoffMs: 0,
    fetchImplementation: async () => {
      attempts += 1;
      return new Response("{}", {
        headers: {
          "Content-Type": "application/json",
          "Content-Length": String(3 * 1024 * 1024),
        },
      });
    },
  });

  await assert.rejects(
    transport.json("translations.json"),
    (error) =>
      error instanceof PublicApiError &&
      error.code === "public_api_response_too_large",
  );
  assert.equal(attempts, 1);
});

test("both deadlines are armed for a read, and only browser timers are used", async () => {
  const timers = recordingTimers();
  const transport = new GetBibleTransport({
    stallTimeoutMs: 20_000,
    totalTimeoutMs: 120_000,
    fetchImplementation: async () => response(JSON.stringify({ verses: [] })),
    setTimeoutImplementation: timers.setTimeoutImplementation,
    clearTimeoutImplementation: timers.clearTimeoutImplementation,
  });

  await transport.json("kjv/42/1.json");

  assert.ok(timers.armed.includes(20_000), "stall bound is armed");
  assert.ok(timers.armed.includes(120_000), "total ceiling is armed");
});

test("incoherent or unbounded deadline policy is refused at construction", () => {
  for (const options of [
    { stallTimeoutMs: 500 },
    { stallTimeoutMs: 200_000 },
    { stallTimeoutMs: 30_000, totalTimeoutMs: 10_000 },
    { totalTimeoutMs: 900_000 },
    { attempts: 0 },
    { attempts: 9 },
    { retryBackoffMs: -1 },
  ]) {
    assert.throws(
      () => new GetBibleTransport({ ...options, fetchImplementation: async () => response("{}") }),
      RangeError,
      JSON.stringify(options),
    );
  }
});
