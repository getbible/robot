import {
  GetBibleApi,
  PublicApiError,
} from "./getbible-api.js";
import {
  isDirectSelectionId,
  selectionIdentity,
} from "./getbible-model.js";

const API_ROOT = "api/v1/";
const DEFAULT_TIMEOUT_MS = 15_000;
const SESSION_TOKEN_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const DIRECT_SELECTION_COORDINATE_PATTERN =
  /^gbd_(.+)_([0-9]{3})_([0-9]{4})_([0-9]{4})$/;
const DEFAULT_BASKET_MAXIMUM = 100;

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
  #selectionRegistry = new Map();
  #basket = [];
  #basketMaximum = DEFAULT_BASKET_MAXIMUM;

  constructor(initData, {
    timeoutMs = DEFAULT_TIMEOUT_MS,
    baseUrl = globalThis.document?.baseURI ?? globalThis.location?.href,
    fetchImplementation = globalThis.fetch,
    setTimeoutImplementation = globalThis.setTimeout,
    clearTimeoutImplementation = globalThis.clearTimeout,
    publicApi = null,
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
    if (
      !this.#publicApi ||
      typeof this.#publicApi.translations !== "function" ||
      typeof this.#publicApi.chapter !== "function"
    ) {
      throw new TypeError("A GetBible public API client is required.");
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
    this.#hydrateBasket(payload.basket);
    this.#cleanupLaunch();
    return { ...payload, basket: this.#basketPayload() };
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
    this.#hydrateBasket(payload?.basket);
    this.#cleanupLaunch();
    return { ...payload, basket: this.#basketPayload() };
  }

  clearSession() {
    this.#sessionToken = null;
    this.#cleanupAttempted = false;
    this.#selectionRegistry.clear();
    this.#basket = [];
    this.#basketMaximum = DEFAULT_BASKET_MAXIMUM;
  }

  async revokeSession() {
    if (!this.#sessionToken) return;
    try {
      await this.#request("session", { method: "DELETE", keepalive: true });
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

  basket() {
    return Promise.resolve(this.#basketPayload());
  }

  addBasketItem(selection) {
    const item = this.#resolveSelection(selection);
    const identity = selectionIdentity(item);
    if (!identity) throw new TypeError("Scripture selection identity is invalid.");
    if (!this.#basket.some((current) => selectionIdentity(current) === identity)) {
      if (this.#basket.length >= this.#basketMaximum) {
        throw new ApiError("The Scripture basket is full.", {
          code: "invalid_selection",
        });
      }
      this.#basket.push(item);
    }
    return Promise.resolve(this.#basketPayload());
  }

  removeBasketItem(selectionId) {
    const item = this.#selectionRegistry.get(selectionId) ??
      this.#basket.find((current) => current.selection_id === selectionId);
    const identity = item ? selectionIdentity(item) : "";
    this.#basket = this.#basket.filter((current) =>
      current.selection_id !== selectionId &&
      (!identity || selectionIdentity(current) !== identity)
    );
    return Promise.resolve(this.#basketPayload());
  }

  reorderBasket(selectionIds) {
    if (!Array.isArray(selectionIds)) {
      throw new TypeError("selectionIds must be an array.");
    }
    const existing = new Map(this.#basket.map((item) => [item.selection_id, item]));
    if (
      selectionIds.length !== existing.size ||
      new Set(selectionIds).size !== selectionIds.length ||
      selectionIds.some((selectionId) => !existing.has(selectionId))
    ) {
      throw new ApiError("Basket order is invalid.", { code: "invalid_selection" });
    }
    this.#basket = selectionIds.map((selectionId) => existing.get(selectionId));
    return Promise.resolve(this.#basketPayload());
  }

  clearBasket() {
    this.#basket = [];
    return Promise.resolve(this.#basketPayload());
  }

  async postSelection(idempotencyKey) {
    await this.#synchronizeBasketForPost();
    const payload = await this.#request("post", {
      method: "POST",
      body: { idempotency_key: idempotencyKey },
      timeoutMs: 25_000,
    });
    this.#basket = [];
    return payload;
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

  #hydrateBasket(rawBasket) {
    const maximum = Number(rawBasket?.maximum);
    this.#basketMaximum = Number.isSafeInteger(maximum) && maximum > 0
      ? maximum
      : DEFAULT_BASKET_MAXIMUM;
    this.#basket = [];
    const items = Array.isArray(rawBasket?.items) ? rawBasket.items : [];
    for (const item of items) {
      if (!validSelectionDescriptor(item)) continue;
      const copy = selectionDescriptor(item);
      this.#selectionRegistry.set(copy.selection_id, copy);
      if (!this.#basket.some((current) =>
        selectionIdentity(current) === selectionIdentity(copy)
      )) {
        this.#basket.push(copy);
      }
    }
  }

  #registerPayloadSelections(payload) {
    const items = Array.isArray(payload?.items) ? payload.items : [];
    for (const item of items) {
      if (!validSelectionDescriptor(item)) continue;
      const copy = selectionDescriptor(item);
      this.#selectionRegistry.set(copy.selection_id, copy);
    }
  }

  #resolveSelection(selection) {
    if (typeof selection === "string") {
      const item = this.#selectionRegistry.get(selection);
      if (!item) {
        throw new ApiError("The selected Scripture is no longer available.", {
          code: "invalid_selection",
        });
      }
      return item;
    }
    if (!validSelectionDescriptor(selection)) {
      throw new TypeError("A valid Scripture selection is required.");
    }
    const item = selectionDescriptor(selection);
    this.#selectionRegistry.set(item.selection_id, item);
    return item;
  }

  #basketPayload() {
    return {
      items: this.#basket.map((item) => ({ ...item })),
      count: this.#basket.length,
      maximum: this.#basketMaximum,
    };
  }

  async #synchronizeBasketForPost() {
    await this.#request("basket", { method: "DELETE" });
    const authoritativeIds = [];
    for (const item of this.#basket) {
      const selectionId = isDirectSelectionId(item.selection_id)
        ? await this.#registerDirectSelection(item.selection_id)
        : item.selection_id;
      const payload = await this.#request("basket/items", {
        method: "POST",
        body: { selection_id: selectionId },
      });
      const responseItems = Array.isArray(payload?.items)
        ? payload.items
        : Array.isArray(payload?.basket?.items)
          ? payload.basket.items
          : [];
      const authoritative = responseItems.find((candidate) =>
        selectionIdentity(candidate) === selectionIdentity(item)
      );
      if (!authoritative || typeof authoritative.selection_id !== "string") {
        throw new ApiError("The selected Scripture could not be verified.", {
          code: "invalid_selection",
          retryable: true,
        });
      }
      authoritativeIds.push(authoritative.selection_id);
    }
    if (authoritativeIds.length > 1) {
      await this.#request("basket/order", {
        method: "PATCH",
        body: { selection_ids: authoritativeIds },
      });
    }
  }

  async #registerDirectSelection(selectionId) {
    const coordinates = directSelectionCoordinates(selectionId);
    if (!coordinates) {
      throw new TypeError("Direct Scripture selection identity is invalid.");
    }
    const payload = await this.#request("scripture", {
      method: "POST",
      body: {
        translation: coordinates.translation,
        book: coordinates.book,
        chapter: coordinates.chapter,
        verse: coordinates.verse,
      },
    });
    const items = Array.isArray(payload?.items) ? payload.items : [];
    const authoritative = items.find((item) =>
      item?.translation === coordinates.translation &&
      item?.book_number === coordinates.book &&
      item?.chapter === coordinates.chapter &&
      item?.verse === coordinates.verse &&
      typeof item?.selection_id === "string" &&
      SESSION_TOKEN_PATTERN.test(item.selection_id)
    );
    if (!authoritative) {
      throw new ApiError("The selected Scripture could not be verified.", {
        code: "invalid_selection",
        retryable: true,
      });
    }
    return authoritative.selection_id;
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
    const headers = { Accept: "application/json", "Cache-Control": "no-store" };
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
      throw new ApiError("getBible.Life could not connect. Check your connection and retry.", {
        code: "network_error",
        retryable: true,
      });
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
          retryable: Boolean(error?.retryable) || response.status === 429 || response.status >= 500,
          requestId: typeof error?.request_id === "string" ? error.request_id : null,
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

function directSelectionCoordinates(selectionId) {
  if (!isDirectSelectionId(selectionId)) return null;
  const match = DIRECT_SELECTION_COORDINATE_PATTERN.exec(selectionId);
  if (!match) return null;
  const book = Number(match[2]);
  const chapter = Number(match[3]);
  const verse = Number(match[4]);
  if (![book, chapter, verse].every(Number.isSafeInteger)) return null;
  return { translation: match[1], book, chapter, verse };
}

function validSelectionDescriptor(selection) {
  return Boolean(
    selection &&
    typeof selection === "object" &&
    !Array.isArray(selection) &&
    typeof selection.selection_id === "string" &&
    selection.selection_id.length > 0 &&
    typeof selection.translation === "string" &&
    selection.translation.length > 0 &&
    typeof selection.reference === "string" &&
    selection.reference.length > 0 &&
    Number.isSafeInteger(selection.book_number) &&
    selection.book_number > 0 &&
    typeof selection.book_name === "string" &&
    selection.book_name.length > 0 &&
    Number.isSafeInteger(selection.chapter) &&
    selection.chapter > 0 &&
    Number.isSafeInteger(selection.verse) &&
    selection.verse > 0 &&
    typeof selection.text === "string" &&
    selection.text.length > 0 &&
    selectionIdentity(selection).length > 0
  );
}

function selectionDescriptor(selection) {
  return {
    selection_id: selection.selection_id,
    translation: selection.translation,
    reference: selection.reference,
    book_number: selection.book_number,
    book_name: selection.book_name,
    chapter: selection.chapter,
    verse: selection.verse,
    text: selection.text,
    terms: Array.isArray(selection.terms) ? [...selection.terms] : [],
    highlights: Array.isArray(selection.highlights)
      ? selection.highlights.map((item) => ({ ...item }))
      : [],
  };
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
  for (const [key, value] of Object.entries(values)) query.set(key, String(value));
  return query.toString();
}

function statusMessage(status) {
  if (status === 401 || status === 403) {
    return "Your Telegram session is no longer valid. Reopen getBible.Life from the bot.";
  }
  if (status === 409) return "That selection changed. Refresh it and try again.";
  if (status === 429) return "Please wait a moment before trying again.";
  if (status >= 500) return "getBible.Life is temporarily unavailable. Please retry.";
  return "getBible.Life could not complete that request.";
}
