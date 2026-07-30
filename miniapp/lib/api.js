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
  #setTimeout;
  #clearTimeout;

  constructor(initData, {
    timeoutMs = DEFAULT_TIMEOUT_MS,
    baseUrl = globalThis.document?.baseURI ?? globalThis.location?.href,
    fetchImplementation = globalThis.fetch,
    setTimeoutImplementation = globalThis.setTimeout,
    clearTimeoutImplementation = globalThis.clearTimeout,
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
    this.#cleanupLaunch();
    return payload;
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
    this.#cleanupLaunch();
    return payload;
  }

  clearSession() {
    this.#sessionToken = null;
    this.#cleanupAttempted = false;
  }

  async revokeSession() {
    if (!this.#sessionToken) {
      return;
    }
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
    return this.#request("translations");
  }

  books(translation) {
    return this.#request(`books?${params({ translation })}`);
  }

  chapters(translation, book) {
    return this.#request(`chapters?${params({ translation, book })}`);
  }

  scripture(translation, book, chapter, verse = 1) {
    return this.#request("scripture", {
      method: "POST",
      body: { translation, book, chapter, verse },
    });
  }

  search(query, filters) {
    return this.#request("search", {
      method: "POST",
      body: {
        query,
        options: filters,
      },
    });
  }

  searchPage(searchId, page) {
    return this.#request(
      `search/${encodeURIComponent(searchId)}?${params({ page })}`,
    );
  }

  preferences(preferences) {
    return this.#request("preferences", {
      method: "PUT",
      body: preferences,
      keepalive: true,
    });
  }

  basket() {
    return this.#request("basket");
  }

  addBasketItem(selectionId) {
    return this.#request("basket/items", {
      method: "POST",
      body: { selection_id: selectionId },
    });
  }

  removeBasketItem(selectionId) {
    return this.#request(
      `basket/items/${encodeURIComponent(selectionId)}`,
      { method: "DELETE" },
    );
  }

  reorderBasket(selectionIds) {
    return this.#request("basket/order", {
      method: "PATCH",
      body: { selection_ids: selectionIds },
    });
  }

  clearBasket() {
    return this.#request("basket", { method: "DELETE" });
  }

  postSelection(idempotencyKey) {
    return this.#request("post", {
      method: "POST",
      body: { idempotency_key: idempotencyKey },
      timeoutMs: 25_000,
    });
  }

  #cleanupLaunch() {
    if (this.#cleanupAttempted || !this.#sessionToken) {
      return;
    }
    this.#cleanupAttempted = true;
    void this.#request("cleanup", {
      method: "POST",
      keepalive: true,
      timeoutMs: 5_000,
    }).catch(() => undefined);
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
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
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
      throw new ApiError("getBible.Life could not connect. Check your connection and retry.", {
        code: "network_error",
        retryable: true,
      });
    } finally {
      this.#clearTimeout(timeout);
    }

    if (response.status === 204) {
      return null;
    }

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
          code:
            typeof error === "string"
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
