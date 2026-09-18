const DEFAULT_API_ROOT = "https://api.getbible.net/v2/";
const DEFAULT_QUERY_ROOT = "https://query.getbible.net/v2/";
const DEFAULT_SEARCH_ROOT = "https://search.getbible.net/v2/";
const DEFAULT_BOOKMARKS_ROOT = "https://bookmarks.getbible.net/v1/";
// A chapter is downloaded, not computed, so the only honest question a reader's
// deadline can ask is whether the response is still arriving. A wall clock
// answers a different question and answers it wrongly on a slow phone: it
// abandons a transfer that was progressing, and the reader is told Scripture
// could not be loaded when in fact it was still coming. The stall bound is
// therefore rearmed on every chunk of body, and only a transfer that has gone
// quiet is abandoned. The total bound exists purely so a connection that
// trickles forever cannot hold a request open without end.
const DEFAULT_STALL_TIMEOUT_MS = 20_000;
const DEFAULT_TOTAL_TIMEOUT_MS = 120_000;
const DEFAULT_ATTEMPTS = 3;
const DEFAULT_RETRY_BACKOFF_MS = 400;
const DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
// The complete Bookmarks API collection (every topic, coordinate and locale)
// is one document. It is larger than any single chapter read, so it carries
// its own ceiling instead of widening the bound every other read shares.
const BOOKMARKS_MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
// A refusal explains itself in a small problem document; anything larger is
// not an explanation and is not read.
const MAX_PROBLEM_BYTES = 16 * 1024;
// A Retry-After the page will honour by waiting inside the retry loop. Longer
// waits are surfaced to the reader instead of being sat out silently.
const MAX_RETRY_AFTER_WAIT_MS = 5_000;
const MAX_RETRY_AFTER_SECONDS = 3_600;
const MAX_SEARCH_PARAMETERS_LENGTH = 8_192;
// A search page is normally read through the browser's HTTP cache; a search
// restarted after a corpus change revalidates its first page with the server
// instead, so the page cut from the old corpus is never served again.
const SEARCH_CACHE_MODES = new Set(["default", "no-cache"]);
const SHA1_PATTERN = /^[0-9a-f]{40}$/;
const BOOKMARKS_PATH_PATTERN = /^[a-z0-9][a-z0-9_-]*(?:\/[a-z0-9][a-z0-9_-]*)*\.json$/;
const SEARCH_TRANSLATION_PATTERN = /^[a-z0-9][a-z0-9._-]{0,63}$/;

export const GETBIBLE_BOOKMARKS_MAX_RESPONSE_BYTES = BOOKMARKS_MAX_RESPONSE_BYTES;

export class PublicApiError extends Error {
  constructor(message, {
    code = "public_api_failed",
    status = 0,
    retryable = false,
    retryAfter = null,
  } = {}) {
    super(message);
    this.name = "PublicApiError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.retryAfter = normalizeRetryAfter(retryAfter);
  }
}

/**
 * Fixed-origin, bounded transport for the public GetBible API V2 protocol.
 */
export class GetBibleTransport {
  #apiRoot;
  #attempts;
  #bookmarksRoot;
  #clearTimeout;
  #fetch;
  #maxResponseBytes;
  #queryRoot;
  #retryBackoffMs;
  #searchRoot;
  #setTimeout;
  #stallTimeoutMs;
  #subtle;
  #totalTimeoutMs;

  constructor({
    apiRoot = DEFAULT_API_ROOT,
    queryRoot = DEFAULT_QUERY_ROOT,
    searchRoot = DEFAULT_SEARCH_ROOT,
    bookmarksRoot = DEFAULT_BOOKMARKS_ROOT,
    stallTimeoutMs = DEFAULT_STALL_TIMEOUT_MS,
    totalTimeoutMs = DEFAULT_TOTAL_TIMEOUT_MS,
    attempts = DEFAULT_ATTEMPTS,
    retryBackoffMs = DEFAULT_RETRY_BACKOFF_MS,
    maxResponseBytes = DEFAULT_MAX_RESPONSE_BYTES,
    fetchImplementation = globalThis.fetch,
    setTimeoutImplementation = globalThis.setTimeout,
    clearTimeoutImplementation = globalThis.clearTimeout,
    subtleCrypto = globalThis.crypto?.subtle,
  } = {}) {
    this.#apiRoot = safeRoot(apiRoot, "api.getbible.net");
    this.#queryRoot = safeRoot(queryRoot, "query.getbible.net");
    this.#searchRoot = safeRoot(searchRoot, "search.getbible.net");
    this.#bookmarksRoot = safeRoot(bookmarksRoot, "bookmarks.getbible.net");
    if (typeof fetchImplementation !== "function") {
      throw new TypeError("A public API fetch implementation is required.");
    }
    if (
      typeof setTimeoutImplementation !== "function" ||
      typeof clearTimeoutImplementation !== "function"
    ) {
      throw new TypeError("Browser timer implementations are required.");
    }
    if (!subtleCrypto || typeof subtleCrypto.digest !== "function") {
      throw new TypeError("Web Crypto digest support is required.");
    }
    if (
      !Number.isInteger(stallTimeoutMs) ||
      stallTimeoutMs < 1_000 ||
      stallTimeoutMs > 120_000
    ) {
      throw new RangeError("Public API stall timeout is invalid.");
    }
    if (
      !Number.isInteger(totalTimeoutMs) ||
      totalTimeoutMs < stallTimeoutMs ||
      totalTimeoutMs > 600_000
    ) {
      throw new RangeError("Public API total timeout is invalid.");
    }
    if (!Number.isInteger(attempts) || attempts < 1 || attempts > 5) {
      throw new RangeError("Public API attempt count is invalid.");
    }
    if (
      !Number.isInteger(retryBackoffMs) ||
      retryBackoffMs < 0 ||
      retryBackoffMs > 10_000
    ) {
      throw new RangeError("Public API retry backoff is invalid.");
    }
    if (
      !Number.isInteger(maxResponseBytes) ||
      maxResponseBytes < 4_096 ||
      maxResponseBytes > 16 * 1024 * 1024
    ) {
      throw new RangeError("Public API response limit is invalid.");
    }
    this.#stallTimeoutMs = stallTimeoutMs;
    this.#totalTimeoutMs = totalTimeoutMs;
    this.#attempts = attempts;
    this.#retryBackoffMs = retryBackoffMs;
    this.#maxResponseBytes = maxResponseBytes;
    this.#fetch = (...args) => Reflect.apply(fetchImplementation, globalThis, args);
    this.#setTimeout = (...args) =>
      Reflect.apply(setTimeoutImplementation, globalThis, args);
    this.#clearTimeout = (...args) =>
      Reflect.apply(clearTimeoutImplementation, globalThis, args);
    this.#subtle = subtleCrypto;
  }

  async json(relativePath, { maximumBytes = this.#maxResponseBytes } = {}) {
    const { bytes } = await this.#read(this.#apiUrl(relativePath), {
      accept: "application/json",
      maximumBytes,
    });
    return decodeJson(bytes);
  }

  async sha(relativePath) {
    const { bytes } = await this.#read(this.#apiUrl(relativePath), {
      accept: "text/plain, application/octet-stream;q=0.9",
      maximumBytes: 256,
    });
    let value;
    try {
      value = new TextDecoder("ascii", { fatal: true })
        .decode(bytes)
        .trim()
        .toLowerCase();
    } catch {
      throw new PublicApiError("GetBible returned an invalid checksum.", {
        code: "invalid_checksum",
        retryable: true,
      });
    }
    if (!SHA1_PATTERN.test(value)) {
      throw new PublicApiError("GetBible returned an invalid checksum.", {
        code: "invalid_checksum",
        retryable: true,
      });
    }
    return value;
  }

  async consistentJson(relativeJsonPath, relativeShaPath, {
    maximumBytes = this.#maxResponseBytes,
  } = {}) {
    let lastBefore = "";
    let lastAfter = "";
    for (let attempt = 0; attempt < 2; attempt += 1) {
      lastBefore = await this.sha(relativeShaPath);
      const { bytes } = await this.#read(this.#apiUrl(relativeJsonPath), {
        accept: "application/json",
        maximumBytes,
      });
      lastAfter = await this.sha(relativeShaPath);
      if (lastBefore !== lastAfter) {
        continue;
      }
      const actual = await sha1Hex(this.#subtle, bytes);
      if (actual !== lastAfter) {
        throw new PublicApiError("GetBible content did not match its checksum.", {
          code: "checksum_mismatch",
          retryable: true,
        });
      }
      return {
        payload: decodeJson(bytes),
        sha: lastAfter,
        retries: attempt,
      };
    }
    throw new PublicApiError(
      `GetBible content changed repeatedly during retrieval (${lastBefore} -> ${lastAfter}).`,
      { code: "content_changed", retryable: true },
    );
  }

  async query(translation, references, {
    maximumBytes = this.#maxResponseBytes,
  } = {}) {
    const encoded = encodeReferencePath(references);
    const url = new URL(`${translation}/${encoded}`, this.#queryRoot);
    const { bytes } = await this.#read(url, {
      accept: "application/json",
      maximumBytes,
    });
    return decodeJson(bytes);
  }

  /**
   * One full-text search on the fixed Search API origin.
   *
   * A search is a public, cacheable GET like any other Scripture read, so the
   * browser's HTTP cache is allowed to honour the API's Cache-Control rather
   * than every repeated query paying a fresh round trip. A refusal arrives as
   * a problem document, which is read (bounded) so the reader can be told
   * whether to change the search, wait, or try again.
   */
  async search(translation, parameters, {
    maximumBytes = this.#maxResponseBytes,
    cache = "default",
  } = {}) {
    if (
      typeof translation !== "string" ||
      !SEARCH_TRANSLATION_PATTERN.test(translation)
    ) {
      throw new TypeError("Search translation is invalid.");
    }
    if (!SEARCH_CACHE_MODES.has(cache)) {
      throw new TypeError("Search cache mode is invalid.");
    }
    const query = encodeSearchParameters(parameters);
    const url = new URL(`${translation}?${query}`, this.#searchRoot);
    const { bytes } = await this.#read(url, {
      accept: "application/json",
      maximumBytes,
      cache,
      problem: true,
    });
    return decodeJson(bytes);
  }

  /**
   * One document from the public Bookmarks API v1 origin.
   *
   * The exact bytes come back with their SHA-256 so the caller can hold a
   * document against the checksum the API's index publishes for it. The
   * browser's HTTP cache is bypassed on purpose: the Mini App keeps its own
   * verified copy of the catalogue and decides for itself when to revalidate.
   */
  async bookmarks(relativePath, {
    maximumBytes = BOOKMARKS_MAX_RESPONSE_BYTES,
  } = {}) {
    if (
      typeof relativePath !== "string" ||
      relativePath.length > 128 ||
      !BOOKMARKS_PATH_PATTERN.test(relativePath)
    ) {
      throw new TypeError("Bookmarks API path is invalid.");
    }
    const { bytes } = await this.#read(new URL(relativePath, this.#bookmarksRoot), {
      accept: "application/json",
      maximumBytes,
      ceiling: BOOKMARKS_MAX_RESPONSE_BYTES,
    });
    return { bytes, sha256: await sha256Hex(this.#subtle, bytes) };
  }

  #apiUrl(relativePath) {
    if (
      typeof relativePath !== "string" ||
      relativePath.startsWith("/") ||
      relativePath.includes("\\") ||
      relativePath.includes("..")
    ) {
      throw new TypeError("Public API path is invalid.");
    }
    return new URL(relativePath, this.#apiRoot);
  }

  /**
   * Read one public resource, retrying the failures that are worth retrying.
   *
   * A dropped connection or a stalled transfer says nothing about whether the
   * chapter exists; it says the network faltered once. Retrying is what turns
   * that into a slower load rather than a reader being told Scripture is
   * unavailable. A refusal that will repeat — a 404, an oversized body, a
   * malformed payload — is raised on the first attempt.
   */
  async #read(url, options) {
    for (let attempt = 1; ; attempt += 1) {
      try {
        return await this.#attempt(url, options);
      } catch (error) {
        if (
          attempt >= this.#attempts ||
          !(error instanceof PublicApiError) ||
          !error.retryable
        ) {
          throw error;
        }
        // A server that names its own wait is not asking to be retried on
        // this loop's schedule: a short wait is honoured here, a long one is
        // handed to the reader as advice rather than sat out in silence.
        const requestedWaitMs = error.retryAfter === null
          ? 0
          : Math.round(error.retryAfter * 1_000);
        if (requestedWaitMs > MAX_RETRY_AFTER_WAIT_MS) {
          throw error;
        }
        await this.#pause(Math.max(
          this.#retryBackoffMs * 2 ** (attempt - 1),
          requestedWaitMs,
        ));
      }
    }
  }

  #pause(delayMs) {
    if (delayMs <= 0) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      this.#setTimeout(resolve, delayMs);
    });
  }

  async #attempt(url, {
    accept,
    maximumBytes,
    ceiling = this.#maxResponseBytes,
    cache = "no-store",
    problem = false,
  }) {
    if (
      !Number.isInteger(maximumBytes) ||
      maximumBytes < 1 ||
      maximumBytes > ceiling
    ) {
      throw new RangeError("Public API response bound is invalid.");
    }
    const controller = new AbortController();
    let stallTimer = null;
    const clearStall = () => {
      if (stallTimer !== null) {
        this.#clearTimeout(stallTimer);
        stallTimer = null;
      }
    };
    // Rearmed by every chunk of body, so a transfer that is still arriving is
    // never abandoned for taking a long time — only one that has gone silent.
    const armStall = () => {
      clearStall();
      stallTimer = this.#setTimeout(
        () => controller.abort(),
        this.#stallTimeoutMs,
      );
    };
    const totalTimer = this.#setTimeout(
      () => controller.abort(),
      this.#totalTimeoutMs,
    );
    armStall();
    let response;
    try {
      response = await this.#fetch(url, {
        method: "GET",
        headers: { Accept: accept },
        credentials: "omit",
        cache,
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: controller.signal,
      });
    } catch (error) {
      clearStall();
      this.#clearTimeout(totalTimer);
      if (error?.name === "AbortError") {
        throw new PublicApiError("The GetBible API request timed out.", {
          code: "public_api_timeout",
          retryable: true,
        });
      }
      throw new PublicApiError("The browser could not reach the GetBible API.", {
        code: "public_api_network_error",
        retryable: true,
      });
    }

    try {
      return await this.#body(response, url, {
        maximumBytes,
        problem,
        onProgress: armStall,
        onIndefinite: clearStall,
        signal: controller.signal,
      });
    } catch (error) {
      // A body abandoned mid-stream surfaces differently across engines, so the
      // controller's own state is the reliable witness that this was our clock
      // firing rather than a malformed payload.
      if (
        !(error instanceof PublicApiError) &&
        (error?.name === "AbortError" || controller.signal.aborted)
      ) {
        throw new PublicApiError("The GetBible API response stalled.", {
          code: "public_api_timeout",
          retryable: true,
        });
      }
      throw error;
    } finally {
      clearStall();
      this.#clearTimeout(totalTimer);
    }
  }

  async #body(response, url, {
    maximumBytes,
    problem = false,
    onProgress,
    onIndefinite,
    signal,
  }) {
    if (!response.ok && problem) {
      throw await problemError(response, { onProgress, onIndefinite, signal });
    }
    if (!response.ok) {
      throw new PublicApiError(
        response.status === 404
          ? "The requested GetBible resource was not found."
          : "The GetBible API is temporarily unavailable.",
        {
          code: response.status === 404 ? "public_api_not_found" : "public_api_failed",
          status: response.status,
          retryable: response.status === 429 || response.status >= 500,
        },
      );
    }
    const announced = response.headers.get("content-length");
    if (announced !== null) {
      const size = Number(announced);
      if (!Number.isInteger(size) || size < 0 || size > maximumBytes) {
        throw new PublicApiError("GetBible response exceeded the safe browser limit.", {
          code: "public_api_response_too_large",
        });
      }
    }
    const bytes = await readBoundedBody(response, maximumBytes, {
      onProgress,
      onIndefinite,
      signal,
    });
    return { bytes, url: response.url || String(url) };
  }
}

function safeRoot(value, expectedHost) {
  let url;
  try {
    url = new URL(String(value));
  } catch {
    throw new TypeError("GetBible API root is invalid.");
  }
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !url.pathname.endsWith("/")
  ) {
    throw new TypeError("GetBible API root is invalid.");
  }
  if (url.hostname !== expectedHost && !isTestRuntime()) {
    throw new TypeError("GetBible API origin is not allowlisted.");
  }
  return url;
}

function isTestRuntime() {
  return typeof process === "object" && process?.env?.NODE_ENV === "test";
}

function encodeSearchParameters(parameters) {
  const encoded = parameters instanceof URLSearchParams
    ? parameters.toString()
    : typeof parameters === "string"
      ? parameters
      : "";
  if (
    encoded.length === 0 ||
    encoded.length > MAX_SEARCH_PARAMETERS_LENGTH ||
    /[#?\s]/.test(encoded)
  ) {
    throw new TypeError("Search parameters are invalid.");
  }
  return encoded;
}

/**
 * Turn a refused search into the error the reader can act on.
 *
 * The problem document is advisory: the HTTP status decides whether the
 * request itself was wrong (400, never retried unchanged), the translation is
 * unknown (404), the public budget is spent (429, wait) or the service is
 * temporarily unable to answer (503 and other 5xx, retry). The body's `code`
 * only refines the 404 case, and a body that cannot be read leaves the status
 * mapping intact rather than hiding the refusal behind a parse failure.
 */
async function problemError(response, { onProgress, onIndefinite, signal }) {
  const status = response.status;
  let problem = null;
  try {
    const contentType = response.headers.get("content-type") || "";
    if (/application\/(?:problem\+)?json/i.test(contentType)) {
      const bytes = await readBoundedBody(response, MAX_PROBLEM_BYTES, {
        onProgress,
        onIndefinite,
        signal,
      });
      const decoded = decodeJson(bytes);
      problem = decoded && typeof decoded === "object" && !Array.isArray(decoded)
        ? decoded
        : null;
    }
  } catch {
    problem = null;
  }
  const problemCode = typeof problem?.code === "string" ? problem.code : "";
  const retryAfter = retryAfterSeconds(
    response.headers.get("retry-after"),
    problem?.retry_after,
  );
  const detail = typeof problem?.detail === "string"
    ? problem.detail.trim().slice(0, 300)
    : "";
  if (status === 400) {
    return new PublicApiError(
      detail || "The search request was not accepted.",
      { code: "search_invalid", status, retryable: false, retryAfter },
    );
  }
  if (status === 404 && problemCode === "translation_not_found") {
    return new PublicApiError(
      detail || "The requested translation cannot be searched.",
      { code: "translation_not_found", status, retryable: false, retryAfter },
    );
  }
  if (status === 429) {
    return new PublicApiError(
      detail || "The search service asked the browser to slow down.",
      { code: "search_rate_limited", status, retryable: true, retryAfter },
    );
  }
  return new PublicApiError(
    detail || "The search service is temporarily unavailable.",
    {
      code: "search_unavailable",
      status,
      retryable: status >= 500,
      retryAfter,
    },
  );
}

function retryAfterSeconds(header, bodyValue) {
  const fromBody = normalizeRetryAfter(bodyValue);
  if (fromBody !== null) {
    return fromBody;
  }
  if (typeof header !== "string" || header.trim() === "") {
    return null;
  }
  const seconds = Number(header);
  if (Number.isFinite(seconds)) {
    return normalizeRetryAfter(seconds);
  }
  const timestamp = Date.parse(header);
  return Number.isFinite(timestamp)
    ? normalizeRetryAfter((timestamp - Date.now()) / 1_000)
    : null;
}

function normalizeRetryAfter(value) {
  // Number(null) and Number("") are 0, which would forge a server-issued
  // "wait zero seconds" out of a refusal that carried no Retry-After at all.
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) {
    return null;
  }
  return Math.min(MAX_RETRY_AFTER_SECONDS, seconds);
}

async function readBoundedBody(response, maximumBytes, {
  onProgress = () => undefined,
  onIndefinite = () => undefined,
  signal = null,
} = {}) {
  if (!response.body || typeof response.body.getReader !== "function") {
    // An unreadable stream cannot report progress, so the stall bound would be
    // measuring nothing. Stand it down and let the total bound guard instead.
    onIndefinite();
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > maximumBytes) {
      throw responseTooLarge();
    }
    return bytes;
  }
  const reader = response.body.getReader();
  // The deadline is only real if it can end a read that is already waiting, so
  // the abort is raced against the stream rather than trusted to reach it
  // through the transfer underneath.
  const abort = abortRace(signal);
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = abort === null
        ? await reader.read()
        : await Promise.race([reader.read(), abort.promise]);
      if (done) {
        break;
      }
      if (!(value instanceof Uint8Array)) {
        throw new PublicApiError("GetBible returned an invalid response stream.", {
          code: "invalid_public_response",
          retryable: true,
        });
      }
      onProgress();
      size += value.byteLength;
      if (size > maximumBytes) {
        await reader.cancel().catch(() => undefined);
        throw responseTooLarge();
      }
      chunks.push(value);
    }
  } catch (error) {
    // Release the transfer rather than leaving it draining behind an abandoned
    // read; a retry opens its own connection.
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    abort?.release();
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

/**
 * A promise that rejects when `signal` aborts, safe to race repeatedly and to
 * abandon unsettled when the body finishes first.
 */
function abortRace(signal) {
  if (!signal) {
    return null;
  }
  let fire = () => undefined;
  const promise = new Promise((_resolve, reject) => {
    fire = () => reject(abortedError());
  });
  // The winner of the race is usually the body, leaving this rejection with no
  // caller. Claim it here so a finished read can never surface as an unhandled
  // rejection.
  promise.catch(() => undefined);
  if (signal.aborted) {
    fire();
  } else {
    signal.addEventListener("abort", fire, { once: true });
  }
  return {
    promise,
    release: () => signal.removeEventListener("abort", fire),
  };
}

function abortedError() {
  const error = new Error("The GetBible API response was abandoned.");
  error.name = "AbortError";
  return error;
}

function responseTooLarge() {
  return new PublicApiError("GetBible response exceeded the safe browser limit.", {
    code: "public_api_response_too_large",
  });
}

function decodeJson(bytes) {
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    return JSON.parse(text);
  } catch {
    throw new PublicApiError("GetBible returned malformed JSON.", {
      code: "invalid_public_response",
      retryable: true,
    });
  }
}

async function sha1Hex(subtle, bytes) {
  return digestHex(subtle, "SHA-1", bytes);
}

async function sha256Hex(subtle, bytes) {
  return digestHex(subtle, "SHA-256", bytes);
}

async function digestHex(subtle, algorithm, bytes) {
  const digest = new Uint8Array(await subtle.digest(algorithm, bytes));
  return [...digest]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function encodeReferencePath(value) {
  if (typeof value !== "string") {
    throw new TypeError("Reference query is invalid.");
  }
  const reference = value.trim();
  if (!reference || reference.length > 4_096) {
    throw new TypeError("Reference query is invalid.");
  }
  return encodeURIComponent(reference)
    .replaceAll("%3A", ":")
    .replaceAll("%3B", ";")
    .replaceAll("%2C", ",")
    .replaceAll("%2D", "-");
}
