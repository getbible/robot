const SESSION_STORAGE_KEY = "getbible.miniapp.session";
const SESSION_RECORD_VERSION = 1;
const SESSION_TOKEN_PATTERN = /^[A-Za-z0-9._~-]{16,2048}$/;

export async function openBoundSession(
  api,
  {
    initData,
    launchToken = null,
    storage = window.sessionStorage,
    cryptoProvider = globalThis.crypto,
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
      return await api.resumeSession(stored.token);
    } catch (error) {
      if (isAuthorizationFailure(error)) {
        clearBoundSession(storage);
      }
      throw error;
    }
  }

  const payload = await api.createSession(launchToken);
  if (typeof payload?.session_token === "string") {
    writeBoundSession(storage, {
      token: payload.session_token,
      context,
    });
  }
  return payload;
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

function isAuthorizationFailure(error) {
  return [401, 403].includes(error?.status);
}
