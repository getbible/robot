const SESSION_STORAGE_KEY = "getbible.miniapp.session";
const SESSION_RECORD_VERSION = 1;
const SESSION_TOKEN_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
// The public catalogue is preferred because it is what the reader uses next,
// but the session response already carries the same translation list from
// the robot. Waiting on the public origin beyond this bound, or failing it,
// must not keep a user out of an app whose server has just admitted them.
const PUBLIC_TRANSLATIONS_TIMEOUT_MS = 8_000;

export const SESSION_PUBLIC_TRANSLATIONS_TIMEOUT_MS =
  PUBLIC_TRANSLATIONS_TIMEOUT_MS;

export async function openBoundSession(
  api,
  {
    initData,
    launchToken = null,
    storage = window.sessionStorage,
    cryptoProvider = globalThis.crypto,
    publicTranslationsTimeoutMs = PUBLIC_TRANSLATIONS_TIMEOUT_MS,
  },
) {
  const context = await sessionContext(
    initData,
    launchToken,
    cryptoProvider,
  );
  const stored = readBoundSession(storage, context);
  if (stored) {
    try {
      const payload = await api.resumeSession(stored.token);
      return await withPublicTranslations(api, payload, {
        timeoutMs: publicTranslationsTimeoutMs,
      });
    } catch (error) {
      if (isAuthorizationFailure(error)) {
        clearBoundSession(storage);
      } else {
        throw error;
      }
    }
  }

  const payload = await api.createSession(launchToken);
  if (typeof payload?.session_token === "string") {
    writeBoundSession(storage, {
      token: payload.session_token,
      context,
    });
  }
  return withPublicTranslations(api, payload, {
    timeoutMs: publicTranslationsTimeoutMs,
  });
}

export async function sessionContext(
  initData,
  launchToken = null,
  cryptoProvider = globalThis.crypto,
) {
  if (typeof initData !== "string" || initData.length === 0) {
    throw new TypeError("Telegram initialization data is required.");
  }
  if (
    !cryptoProvider?.subtle ||
    typeof cryptoProvider.subtle.digest !== "function"
  ) {
    throw new TypeError("Secure browser hashing is unavailable.");
  }
  const launch =
    typeof launchToken === "string" && launchToken.length > 0
      ? launchToken
      : "";
  const encoded = new TextEncoder().encode(`${initData}\u0000${launch}`);
  const digest = await cryptoProvider.subtle.digest("SHA-256", encoded);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export function readBoundSession(storage, context) {
  let raw;
  try {
    raw = storage.getItem(SESSION_STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) {
    return null;
  }

  let record;
  try {
    record = JSON.parse(raw);
  } catch {
    clearBoundSession(storage);
    return null;
  }
  if (
    record?.version !== SESSION_RECORD_VERSION ||
    typeof record.context !== "string" ||
    record.context !== context ||
    typeof record.token !== "string" ||
    !SESSION_TOKEN_PATTERN.test(record.token)
  ) {
    clearBoundSession(storage);
    return null;
  }
  return { token: record.token, context: record.context };
}

export function writeBoundSession(storage, { token, context }) {
  if (
    typeof token !== "string" ||
    !SESSION_TOKEN_PATTERN.test(token) ||
    typeof context !== "string" ||
    !/^[a-f0-9]{64}$/.test(context)
  ) {
    return false;
  }
  try {
    storage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({
        version: SESSION_RECORD_VERSION,
        token,
        context,
      }),
    );
    return true;
  } catch {
    return false;
  }
}

export function clearBoundSession(storage = window.sessionStorage) {
  try {
    storage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // The server remains authoritative when browser storage is unavailable.
  }
}

async function withPublicTranslations(
  api,
  payload,
  { timeoutMs = PUBLIC_TRANSLATIONS_TIMEOUT_MS } = {},
) {
  if (!api || typeof api.translations !== "function") {
    return payload;
  }
  const serverTranslations = Array.isArray(payload?.translations)
    ? payload.translations
    : [];
  let failure;
  try {
    const translations = await withDeadline(api.translations(), timeoutMs);
    if (Array.isArray(translations) && translations.length > 0) {
      return { ...payload, translations };
    }
    failure = new TypeError("The public translation catalogue is empty.");
  } catch (error) {
    failure = error;
  }
  if (serverTranslations.length > 0) {
    return { ...payload, translations: serverTranslations };
  }
  throw failure;
}

function withDeadline(operation, timeoutMs) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    return operation;
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error("The public translation catalogue timed out."));
      }
    }, timeoutMs);
    Promise.resolve(operation).then(
      (value) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          resolve(value);
        }
      },
      (error) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(error);
        }
      },
    );
  });
}

function isAuthorizationFailure(error) {
  return [401, 403].includes(error?.status);
}