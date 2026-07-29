import { setClipboardBasket } from "./clipboard.js";

const API_ROOT = "api/v1/";
const DEFAULT_TIMEOUT_MS = 15_000;

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

  constructor(initData, { timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
    if (typeof initData !== "string" || initData.length === 0) {
      throw new TypeError("Telegram initialization data is required.");
    }
    this.#initData = initData;
    this.#timeoutMs = timeoutMs;
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
      payload.session_token.length < 16
    ) {
      throw new ApiError("The secure Telegram session could not be created.", {
        code: "invalid_session_response",
      });
    }
    this.#sessionToken = payload.session_token;
    this.#cleanupAttempted = false;
    this.#syncBasket(payload.basket);
    this.#cleanupLaunch();
    return payload;
  }

  async resumeSession(sessionToken) {
    if (
      typeof sessionToken !== "string" ||
      !/^[A-Za-z0-9._~-]{16,2048}$/.test(sessionToken)
    ) {
      throw new ApiError("The saved secure session is invalid.", {
        code: "invalid_session_token",
        status: 401,
      });
    }
    this.#sessionToken = sessionToken;
    this.#cleanupAttempted = false;
    const payload = await this.#request("session");
    this.#syncBasket(payload.basket);
    this.#cleanupLaunch();
    return payload;
  }

  clearSession() {
    this.#sessionToken = null;
    this.#cleanupAttempted = false;
    setClipboardBasket({ items: [] });
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

  async basket() {
    const payload = await this.#request("basket");
    this.#syncBasket(payload);
    return payload;
  }

  async addBasketItem(selectionId) {
    const payload = await this.#request("basket/items", {
      method: "POST",
      body: { selection_id: selectionId },
    });
    this.#syncBasket(payload?.basket ?? payload);
    return payload;
  }

  async removeBasketItem(selectionId) {
    const payload = await this.#request(
      `basket/items/${encodeURIComponent(selectionId)}`,
      { method: "DELETE" },
    );
    this.#syncBasket(payload?.basket ?? payload);
    return payload;
  }

  async reorderBasket(selectionIds) {
    const payload = await this.#request("basket/order", {
      method: "PATCH",
      body: { selection_ids: selectionIds },
    });
    this.#syncBasket(payload?.basket ?? payload);
    return payload;
  }

  async clearBasket() {
    const payload = await this.#request("basket", { method: "DELETE" });
    setClipboardBasket({ items: [] });
    return payload;
  }

  async postSelection(idempotencyKey) {
    const payload = await this.#request("post", {
      method: "POST",
      body: { idempotency_key: idempotencyKey },
      timeoutMs: 25_000,
    });
    if (payload?.status === "posted") {
      setClipboardBasket({ items: [] });
    }
    return payload;
  }

  #syncBasket(candidate) {
    setClipboardBasket(candidate);
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
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
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
      response = await fetch(new URL(`${API_ROOT}${path}`, document.baseURI), {
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
      if (error instanceof DOMException && error.name === "AbortError") {
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
      window.clearTimeout(timeout);
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
