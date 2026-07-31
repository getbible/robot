const DEFAULT_API_ROOT = "https://api.getbible.net/v2/";
const DEFAULT_QUERY_ROOT = "https://query.getbible.net/v2/";
const DEFAULT_TIMEOUT_MS = 10_000;
const DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const SHA1_PATTERN = /^[0-9a-f]{40}$/;

export class PublicApiError extends Error {
  constructor(message, {
    code = "public_api_failed",
    status = 0,
    retryable = false,
  } = {}) {
    super(message);
    this.name = "PublicApiError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
  }
}

/**
 * Fixed-origin, bounded transport for the public GetBible API V2 protocol.
 */
export class GetBibleTransport {
  #apiRoot;
  #clearTimeout;
  #fetch;
  #maxResponseBytes;
  #queryRoot;
  #setTimeout;
  #subtle;
  #timeoutMs;

  constructor({
    apiRoot = DEFAULT_API_ROOT,
    queryRoot = DEFAULT_QUERY_ROOT,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    maxResponseBytes = DEFAULT_MAX_RESPONSE_BYTES,
    fetchImplementation = globalThis.fetch,
    setTimeoutImplementation = globalThis.setTimeout,
    clearTimeoutImplementation = globalThis.clearTimeout,
    subtleCrypto = globalThis.crypto?.subtle,
  } = {}) {
    this.#apiRoot = safeRoot(apiRoot, "api.getbible.net");
    this.#queryRoot = safeRoot(queryRoot, "query.getbible.net");
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
    if (!Number.isInteger(timeoutMs) || timeoutMs < 500 || timeoutMs > 60_000) {
      throw new RangeError("Public API timeout is invalid.");
    }
    if (
      !Number.isInteger(maxResponseBytes) ||
      maxResponseBytes < 4_096 ||
      maxResponseBytes > 16 * 1024 * 1024
    ) {
      throw new RangeError("Public API response limit is invalid.");
    }
    this.#timeoutMs = timeoutMs;
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

  async #read(url, { accept, maximumBytes }) {
    if (
      !Number.isInteger(maximumBytes) ||
      maximumBytes < 1 ||
      maximumBytes > this.#maxResponseBytes
    ) {
      throw new RangeError("Public API response bound is invalid.");
    }
    const controller = new AbortController();
    const timeout = this.#setTimeout(() => controller.abort(), this.#timeoutMs);
    let response;
    try {
      response = await this.#fetch(url, {
        method: "GET",
        headers: { Accept: accept },
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: controller.signal,
      });
    } catch (error) {
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
    } finally {
      this.#clearTimeout(timeout);
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
    const bytes = await readBoundedBody(response, maximumBytes);
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

async function readBoundedBody(response, maximumBytes) {
  if (!response.body || typeof response.body.getReader !== "function") {
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > maximumBytes) {
      throw responseTooLarge();
    }
    return bytes;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      if (!(value instanceof Uint8Array)) {
        throw new PublicApiError("GetBible returned an invalid response stream.", {
          code: "invalid_public_response",
          retryable: true,
        });
      }
      size += value.byteLength;
      if (size > maximumBytes) {
        await reader.cancel().catch(() => undefined);
        throw responseTooLarge();
      }
      chunks.push(value);
    }
  } finally {
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
  const digest = new Uint8Array(await subtle.digest("SHA-1", bytes));
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
