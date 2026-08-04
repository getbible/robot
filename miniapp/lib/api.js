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
const SESSION_TOKEN_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;

export class ApiError extends Error {
  constructor(message, {
    code = "request_failed",
    status = 0,
    retryable = false,
    requestId = null,
  } = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.requestId = requestId;
  }
}

export class MiniAppApi {
  #initData;
  #sessionToken = null;
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
    this.#selections.setMaximum(Number(payload?.basket?.maximum));
    this.#cleanupLaunch();
    return { ...payload, basket: this.#selections.snapshot() };
  }

  clearSession() {
    this.#sessionToken = null;
    this.#cleanupAttempted = false;
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

  resolveReference(translation, reference) {
    return this.#publicRequest(() =>
      this.#publicApi.resolveReference(translation, reference),
    );
  }

  async search(query, filters) {
    const payload = await this.#request("search", {
      method: "POST",
      body: { query, options: filters },
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
  } = {}) {
    if (authenticated && !this.#sessionToken) {
      throw new ApiError("Your secure session is not ready.", {
        code: "session_not_ready",
      });
    }
    const controller = new AbortController();
    const timeout = this.#setTimeout(() => controller.abort(), timeoutMs);
    const headers = {
      Accept: "application/json",
      "Cache-Control": "no-store",
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (authenticated) {
      headers.Authorization = `Bearer ${this.#sessionToken}`;
      headers["X-Telegram-Init-Data"] = this.#initData;
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
            Boolean(error?.retryable) ||
            response.status === 429 ||
            response.status >= 500,
          requestId:
            typeof error?.request_id === "string" ? error.request_id : null,
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
    return payload;
  }
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
