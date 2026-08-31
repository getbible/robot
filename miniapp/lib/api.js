import {
  GetBibleApi,
  PublicApiError,
} from "./getbible-api.js";
import {
  BrowserSelectionError,
  BrowserSelectionStore,
} from "./selection-store.js";

const API_ROOT = "api/v1/";
const DEFAULT_TIMEOUT_MS = 15_000;
// A search may have to build a translation's index before it can answer, which
// the robot budgets in minutes rather than seconds. The page therefore waits on
// the budget the server announces at session bootstrap, and only falls back to
// this floor when an older robot announces nothing. Waiting less than the server
// works is the one thing that cannot be right: it turns a slow answer into a
// timeout the reader can do nothing about.
const DEFAULT_SEARCH_TIMEOUT_MS = 150_000;
const MAX_SEARCH_TIMEOUT_MS = 900_000;
// Covers the queue wait and the round trip either side of the robot's own
// deadline, so a server that gives up first can say why.
const SEARCH_TIMEOUT_GRACE_MS = 10_000;
const SESSION_TOKEN_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const CONTRIBUTION_TOKEN_PATTERN = /^gbc_[A-Za-z0-9_-]{43}$/;

export class ApiError extends Error {
  constructor(message, {
    code = "request_failed",
    status = 0,
    retryable = false,
    requestId = null,
    retryAfter = null,
  } = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.requestId = requestId;
    this.retryAfter = normalizeRetryAfterSeconds(retryAfter);
  }
}

export class MiniAppApi {
  #initData;
  #sessionToken = null;
  #contributionToken = null;
  #searchTimeoutMs = DEFAULT_SEARCH_TIMEOUT_MS;
  #timeoutMs;
  #cleanupAttempted = false;
  #baseUrl;
  #fetch;
  #publicApi;
  #setTimeout;
  #clearTimeout;
  #selections;

  constructor(initData, {
    timeoutMs = DEFAULT_TIMEOUT_MS,
    baseUrl = globalThis.document?.baseURI ?? globalThis.location?.href,
    fetchImplementation = globalThis.fetch,
    setTimeoutImplementation = globalThis.setTimeout,
    clearTimeoutImplementation = globalThis.clearTimeout,
    publicApi = null,
    selectionStore = null,
  } = {}) {
    if (typeof initData !== "string" || initData.length === 0) {
      throw new TypeError("Telegram initialization data is required.");
    }
    if (typeof fetchImplementation !== "function") {
      throw new TypeError("A fetch implementation is required.");
    }
    if (
      typeof setTimeoutImplementation !== "function" ||
      typeof clearTimeoutImplementation !== "function"
    ) {
      throw new TypeError("Browser timer implementations are required.");
    }
    try {
      this.#baseUrl = new URL(String(baseUrl)).href;
    } catch {
      throw new TypeError("A valid Mini App base URL is required.");
    }
    this.#initData = initData;
    this.#timeoutMs = timeoutMs;
    this.#fetch = (...args) =>
      Reflect.apply(fetchImplementation, globalThis, args);
    this.#setTimeout = (...args) =>
      Reflect.apply(setTimeoutImplementation, globalThis, args);
    this.#clearTimeout = (...args) =>
      Reflect.apply(clearTimeoutImplementation, globalThis, args);
    this.#publicApi = publicApi ?? new GetBibleApi();
    this.#selections = selectionStore ?? new BrowserSelectionStore();
    if (
      !this.#publicApi ||
      typeof this.#publicApi.translations !== "function" ||
      typeof this.#publicApi.chapter !== "function"
    ) {
      throw new TypeError("A GetBible public API client is required.");
    }
    if (
      !this.#selections ||
      typeof this.#selections.registerMany !== "function" ||
      typeof this.#selections.snapshot !== "function"
    ) {
      throw new TypeError("A browser selection store is required.");
    }
  }

  async createSession(launchToken = null) {
    const payload = await this.#request("session", {
      method: "POST",
      body: {
        init_data: this.#initData,
        launch_token: launchToken,
      },
      authenticated: false,
    });
    this.#acceptSession(payload);
    this.#acceptLimits(payload);
    this.#selections.setMaximum(Number(payload?.basket?.maximum));
    this.#cleanupLaunch();
    return { ...payload, basket: this.#selections.snapshot() };
  }

  async resumeSession(sessionToken) {
    if (
      typeof sessionToken !== "string" ||
      !SESSION_TOKEN_PATTERN.test(sessionToken)
    ) {
      throw new ApiError("The saved secure session is invalid.", {
        code: "invalid_session_token",
        status: 401,
      });
    }
    this.#sessionToken = sessionToken;
    this.#cleanupAttempted = false;
    const payload = await this.#request("session");
    this.#acceptLimits(payload);
    this.#selections.setMaximum(Number(payload?.basket?.maximum));
    this.#cleanupLaunch();
    return { ...payload, basket: this.#selections.snapshot() };
  }

  clearSession() {
    this.#sessionToken = null;
    this.#contributionToken = null;
    this.#cleanupAttempted = false;
  }

  get contributionTransportReady() {
    return this.#contributionToken !== null;
  }

  async revokeSession() {
    if (!this.#sessionToken) return;
    try {
      await this.#request("session", {
        method: "DELETE",
        keepalive: true,
      });
    } finally {
      this.clearSession();
    }
  }

  translations() {
    return this.#publicRequest(() => this.#publicApi.translations());
  }

  books(translation) {
    return this.#publicRequest(() => this.#publicApi.books(translation));
  }

  chapters(translation, book) {
    return this.#publicRequest(() => this.#publicApi.chapters(translation, book));
  }

  async scripture(translation, book, chapter, verse = 1) {
    const payload = await this.#publicRequest(() =>
      this.#publicApi.chapter(translation, book, chapter, verse),
    );
    this.#registerPayloadSelections(payload);
    return payload;
  }

  scripturePreview(translation, book, chapter, verse = 1) {
    return this.#publicRequest(() =>
      this.#publicApi.chapter(
        translation,
        book,
        chapter,
        verse,
        { includeNavigation: false },
      ),
    );
  }

  resolveReference(translation, reference) {
    return this.#publicRequest(() =>
      this.#publicApi.resolveReference(translation, reference),
    );
  }

  async search(query, filters) {
    const payload = await this.#request("search", {
      method: "POST",
      body: { query, options: filters },
      timeoutMs: this.#searchTimeoutMs,
    });
    this.#registerPayloadSelections(payload);
    return payload;
  }

  async searchPage(searchId, page) {
    const payload = await this.#request(
      `search/${encodeURIComponent(searchId)}?${params({ page })}`,
    );
    this.#registerPayloadSelections(payload);
    return payload;
  }

  preferences(preferences) {
    return this.#request("preferences", {
      method: "PUT",
      body: preferences,
      keepalive: true,
    });
  }

  backupBookmarks(backup, idempotencyKey) {
    return this.#request("bookmarks/backup", {
      method: "POST",
      body: {
        idempotency_key: idempotencyKey,
        backup,
      },
      timeoutMs: 30_000,
    });
  }

  restoreBookmarks() {
    return this.#request("bookmarks/restore", {
      timeoutMs: 30_000,
    });
  }

  acknowledgeBookmarkRestore() {
    return this.#request("bookmarks/restore", {
      method: "DELETE",
      keepalive: true,
    });
  }

  contributionStatus() {
    return this.#request("contributions/status?details=1");
  }

  acknowledgeContributionDisclosure() {
    return this.#request("contributions/status?details=1", {
      method: "PATCH",
      body: { disclosure_acknowledged: true },
    });
  }

  syncContributions(envelope) {
    if (
      !envelope ||
      typeof envelope !== "object" ||
      Array.isArray(envelope)
    ) {
      throw new TypeError("A contribution sync envelope is required.");
    }
    if (!this.#contributionToken) {
      throw new ApiError("Contribution sync is not available for this session.", {
        code: "contribution_transport_not_ready",
        status: 403,
      });
    }
    return this.#request("contributions/sync", {
      method: "POST",
      body: envelope,
      timeoutMs: 30_000,
      contributionAuthenticated: true,
    });
  }

  bookmarkCatalog(etag = null) {
    const headers = {};
    if (etag !== null) {
      if (
        typeof etag !== "string" ||
        etag.length < 1 ||
        etag.length > 160 ||
        /[\u0000-\u001f\u007f]/.test(etag)
      ) {
        throw new TypeError("The bookmark catalogue ETag is invalid.");
      }
      headers["If-None-Match"] = etag;
    }
    return this.#request("bookmarks/catalog", {
      headers,
      allowNotModified: true,
      includeEtag: true,
      // The reviewed overlay improves fresh launches but the bundled catalog
      // is always usable, so an unreachable publisher must not hold the gate.
      timeoutMs: 4_000,
    });
  }

  registerSelections(selections) {
    return this.#selections.registerMany(selections);
  }

  basket() {
    return Promise.resolve(this.#selections.snapshot());
  }

  addBasketItem(selection) {
    return this.#selectionOperation(() => this.#selections.add(selection));
  }

  removeBasketItem(selectionId) {
    return this.#selectionOperation(() => this.#selections.remove(selectionId));
  }

  reorderBasket(selectionIds) {
    return this.#selectionOperation(() => this.#selections.reorder(selectionIds));
  }

  clearBasket() {
    return Promise.resolve(this.#selections.clear());
  }

  async postSelection(idempotencyKey) {
    const snapshot = this.#selections.snapshot();
    if (snapshot.items.length === 0) {
      throw new ApiError("The Scripture basket is empty.", {
        code: "invalid_selection",
      });
    }
    const payload = await this.#request("post", {
      method: "POST",
      body: {
        idempotency_key: idempotencyKey,
        selection_ids: snapshot.items.map((item) => item.selection_id),
      },
      timeoutMs: 25_000,
    });
    this.#selections.clear();
    return payload;
  }

  #acceptSession(payload) {
    if (
      !payload ||
      typeof payload.session_token !== "string" ||
      !SESSION_TOKEN_PATTERN.test(payload.session_token)
    ) {
      throw new ApiError("The secure Telegram session could not be created.", {
        code: "invalid_session_response",
      });
    }
    this.#sessionToken = payload.session_token;
    this.#cleanupAttempted = false;
  }

  #acceptLimits(payload) {
    const seconds = Number(payload?.limits?.search_timeout_seconds);
    if (!Number.isFinite(seconds) || seconds <= 0) {
      this.#searchTimeoutMs = DEFAULT_SEARCH_TIMEOUT_MS;
      return;
    }
    this.#searchTimeoutMs = Math.min(
      MAX_SEARCH_TIMEOUT_MS,
      Math.max(
        DEFAULT_TIMEOUT_MS,
        Math.round(seconds * 1_000) + SEARCH_TIMEOUT_GRACE_MS,
      ),
    );
  }

  #selectionOperation(operation) {
    try {
      return Promise.resolve(operation());
    } catch (error) {
      if (error instanceof BrowserSelectionError) {
        throw new ApiError(error.message, { code: error.code });
      }
      throw error;
    }
  }

  #registerPayloadSelections(payload) {
    this.#selections.registerMany(
      Array.isArray(payload?.items) ? payload.items : [],
    );
  }

  #cleanupLaunch() {
    if (this.#cleanupAttempted || !this.#sessionToken) return;
    this.#cleanupAttempted = true;
    void this.#request("cleanup", {
      method: "POST",
      keepalive: true,
      timeoutMs: 5_000,
    }).catch(() => undefined);
  }

  async #publicRequest(operation) {
    if (!this.#sessionToken) {
      throw new ApiError("Your secure session is not ready.", {
        code: "session_not_ready",
      });
    }
    try {
      return await operation();
    } catch (error) {
      if (error instanceof PublicApiError) throw publicApiError(error);
      if (error instanceof TypeError || error instanceof RangeError) {
        throw new ApiError("GetBible returned an unexpected response.", {
          code: "invalid_response",
          retryable: false,
        });
      }
      throw new ApiError("The browser could not load Scripture.", {
        code: "scripture_temporarily_unavailable",
        retryable: true,
      });
    }
  }

  async #request(path, {
    method = "GET",
    body,
    authenticated = true,
    keepalive = false,
    timeoutMs = this.#timeoutMs,
    headers: requestHeaders = {},
    contributionAuthenticated = false,
    allowNotModified = false,
    includeEtag = false,
  } = {}) {
    if (authenticated && !this.#sessionToken) {
      throw new ApiError("Your secure session is not ready.", {
        code: "session_not_ready",
      });
    }
    if (contributionAuthenticated && !this.#contributionToken) {
      throw new ApiError("Contribution sync is not available for this session.", {
        code: "contribution_transport_not_ready",
        status: 403,
      });
    }
    const controller = new AbortController();
    const timeout = this.#setTimeout(() => controller.abort(), timeoutMs);
    const headers = {
      Accept: "application/json",
      "Cache-Control": "no-store",
      ...requestHeaders,
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (authenticated) {
      headers.Authorization = `Bearer ${
        contributionAuthenticated
          ? this.#contributionToken
          : this.#sessionToken
      }`;
    }
    let response;
    try {
      response = await this.#fetch(new URL(`${API_ROOT}${path}`, this.#baseUrl), {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: controller.signal,
        keepalive,
      });
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new ApiError("The request took too long. Please try again.", {
          code: "request_timeout",
          retryable: true,
        });
      }
      throw new ApiError(
        "getBible.Life could not connect. Check your connection and retry.",
        { code: "network_error", retryable: true },
      );
    } finally {
      this.#clearTimeout(timeout);
    }
    if (response.ok) {
      this.#acceptContributionToken(response);
    }
    if (allowNotModified && response.status === 304) {
      return {
        not_modified: true,
        etag: response.headers.get("etag"),
      };
    }
    if (response.status === 204) return null;
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json().catch(() => null)
      : null;
    if (!response.ok) {
      const error = payload?.error;
      throw new ApiError(
        typeof payload?.message === "string"
          ? payload.message
          : typeof error?.message === "string"
            ? error.message
            : statusMessage(response.status),
        {
          code: typeof error === "string"
            ? error
            : typeof error?.code === "string"
              ? error.code
              : "request_failed",
          status: response.status,
          retryable:
            Boolean(payload?.retryable ?? error?.retryable) ||
            response.status === 429 ||
            response.status >= 500,
          requestId:
            typeof payload?.request_id === "string"
              ? payload.request_id
              : typeof error?.request_id === "string"
                ? error.request_id
                : null,
          retryAfter: retryAfterSeconds(response, payload),
        },
      );
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new ApiError("getBible.Life returned an unexpected response.", {
        code: "invalid_response",
        status: response.status,
        retryable: true,
      });
    }
    return includeEtag
      ? { ...payload, etag: response.headers.get("etag") }
      : payload;
  }

  #acceptContributionToken(response) {
    const token = response.headers.get("x-contribution-token");
    if (token === null) {
      return;
    }
    if (!CONTRIBUTION_TOKEN_PATTERN.test(token)) {
      throw new ApiError(
        "getBible.Life returned an invalid contribution capability.",
        {
          code: "invalid_response",
          status: response.status,
          retryable: true,
        },
      );
    }
    this.#contributionToken = token;
  }
}

function retryAfterSeconds(response, payload) {
  const payloadValue = payload?.retry_after;
  if (payloadValue !== undefined && payloadValue !== null) {
    return normalizeRetryAfterSeconds(payloadValue);
  }
  const header = response?.headers?.get?.("retry-after");
  if (typeof header !== "string" || header.trim() === "") {
    return null;
  }
  const seconds = Number(header);
  if (Number.isFinite(seconds)) {
    return normalizeRetryAfterSeconds(seconds);
  }
  const timestamp = Date.parse(header);
  return Number.isFinite(timestamp)
    ? normalizeRetryAfterSeconds((timestamp - Date.now()) / 1_000)
    : null;
}

function normalizeRetryAfterSeconds(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) {
    return null;
  }
  return Math.min(3_600, Math.max(0, seconds));
}

function publicApiError(error) {
  const code = error.code === "public_api_timeout"
    ? "request_timeout"
    : error.code === "public_api_network_error"
      ? "network_error"
      : error.code === "public_api_not_found"
        ? "not_found"
        : error.code === "public_api_response_too_large"
          ? "request_too_large"
          : error.code === "invalid_public_response"
            ? "invalid_response"
            : "scripture_temporarily_unavailable";
  return new ApiError(error.message, {
    code,
    status: error.status,
    retryable: error.retryable,
  });
}

function params(values) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    query.set(key, String(value));
  }
  return query.toString();
}

function statusMessage(status) {
  if (status === 401 || status === 403) {
    return "Your Telegram session is no longer valid. Reopen getBible.Life from the bot.";
  }
  if (status === 409) {
    return "That selection changed. Refresh it and try again.";
  }
  if (status === 429) {
    return "Please wait a moment before trying again.";
  }
  if (status >= 500) {
    return "getBible.Life is temporarily unavailable. Please retry.";
  }
  return "getBible.Life could not complete that request.";
}
