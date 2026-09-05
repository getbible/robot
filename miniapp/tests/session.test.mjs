import assert from "node:assert/strict";
import test from "node:test";

import {
  SESSION_PUBLIC_TRANSLATIONS_TIMEOUT_MS,
  clearBoundSession,
  openBoundSession,
  readBoundSession,
  sessionContext,
  writeBoundSession,
} from "../lib/session.js";

class MemoryStorage {
  constructor() {
    this.values = new Map();
  }

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.values.set(key, value);
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

const SESSION_TOKEN = "abcdefghijklmnop";

test("session context is bound to both initData and the current launch", async () => {
  const first = await sessionContext("signed-init-data", "launch-one");
  const reload = await sessionContext("signed-init-data", "launch-one");
  const nextLaunch = await sessionContext("signed-init-data", "launch-two");
  const nextInitData = await sessionContext("different-init-data", "launch-one");

  assert.equal(first, reload);
  assert.notEqual(first, nextLaunch);
  assert.notEqual(first, nextInitData);
  assert.match(first, /^[a-f0-9]{64}$/);
});

test("matching reload resumes while a new launch always exchanges", async () => {
  const storage = new MemoryStorage();
  const firstApi = {
    createSession: async (launchToken) => {
      assert.equal(launchToken, "launch-one");
      return { session_token: SESSION_TOKEN, entrypoint: { route: "search" } };
    },
    resumeSession: async () => assert.fail("first launch must not resume"),
  };
  await openBoundSession(firstApi, {
    initData: "signed-init-data",
    launchToken: "launch-one",
    storage,
  });

  let resumed = 0;
  await openBoundSession(
    {
      createSession: async () => assert.fail("reload must resume"),
      resumeSession: async (token) => {
        resumed += 1;
        assert.equal(token, SESSION_TOKEN);
        return { entrypoint: { route: "search" } };
      },
    },
    {
      initData: "signed-init-data",
      launchToken: "launch-one",
      storage,
    },
  );
  assert.equal(resumed, 1);

  let exchanged = 0;
  await openBoundSession(
    {
      createSession: async (launchToken) => {
        exchanged += 1;
        assert.equal(launchToken, "launch-two");
        return {
          session_token: "qrstuvwxyzABCDEF",
          entrypoint: { route: "bible" },
        };
      },
      resumeSession: async () => assert.fail("new launch must not resume"),
    },
    {
      initData: "signed-init-data",
      launchToken: "launch-two",
      storage,
    },
  );
  assert.equal(exchanged, 1);
});

test("authorization rejection clears stale storage and exchanges the launch", async () => {
  const storage = new MemoryStorage();
  const context = await sessionContext("signed-init-data", "launch-one");
  assert.equal(
    writeBoundSession(storage, { token: SESSION_TOKEN, context }),
    true,
  );

  const authorizationError = Object.assign(new Error("expired"), {
    status: 401,
  });
  const replacementToken = "qrstuvwxyzABCDEF";
  const payload = await openBoundSession(
    {
      createSession: async (launchToken) => {
        assert.equal(launchToken, "launch-one");
        return {
          session_token: replacementToken,
          entrypoint: { route: "search" },
        };
      },
      resumeSession: async () => {
        throw authorizationError;
      },
    },
    {
      initData: "signed-init-data",
      launchToken: "launch-one",
      storage,
    },
  );
  assert.equal(payload.session_token, replacementToken);
  assert.deepEqual(readBoundSession(storage, context), {
    token: replacementToken,
    context,
  });
});

test("malformed, mismatched, and explicitly cleared records cannot resume", async () => {
  const storage = new MemoryStorage();
  const context = await sessionContext("signed-init-data", null);
  storage.setItem("getbible.miniapp.session", "not-json");
  assert.equal(readBoundSession(storage, context), null);

  assert.equal(
    writeBoundSession(storage, { token: SESSION_TOKEN, context }),
    true,
  );
  assert.equal(readBoundSession(storage, "f".repeat(64)), null);

  assert.equal(
    writeBoundSession(storage, { token: SESSION_TOKEN, context }),
    true,
  );
  clearBoundSession(storage);
  assert.equal(readBoundSession(storage, context), null);
});

// The robot's session response already carries the translation list. The
// public catalogue is preferred, but a public origin that fails or stalls
// must never keep a user out of an app whose server has just admitted them.
const SERVER_TRANSLATIONS = [
  {
    code: "kjv",
    name: "King James Version",
    language: "English",
    lang: "en",
    direction: "ltr",
  },
];
const PUBLIC_TRANSLATIONS = [
  ...SERVER_TRANSLATIONS,
  {
    code: "aov",
    name: "Afrikaans Ou Vertaling",
    language: "Afrikaans",
    lang: "af",
    direction: "ltr",
  },
];

function catalogueApi({ translations, serverTranslations = SERVER_TRANSLATIONS }) {
  return {
    calls: [],
    async createSession(launchToken) {
      this.calls.push(["create", launchToken]);
      return {
        session_token: "SessionToken_0123456789abcdef",
        user: { id: 42 },
        ...(serverTranslations ? { translations: serverTranslations } : {}),
      };
    },
    async resumeSession(token) {
      this.calls.push(["resume", token]);
      return {
        session_token: token,
        user: { id: 42 },
        ...(serverTranslations ? { translations: serverTranslations } : {}),
      };
    },
    translations,
  };
}

function openWithCatalogue(api, overrides = {}) {
  return openBoundSession(api, {
    initData: "signed-init-data",
    storage: new MemoryStorage(),
    publicTranslationsTimeoutMs: 20,
    ...overrides,
  });
}

test("the public translation catalogue is preferred when it arrives", async () => {
  const api = catalogueApi({ translations: async () => PUBLIC_TRANSLATIONS });
  const payload = await openWithCatalogue(api);
  assert.deepEqual(payload.translations, PUBLIC_TRANSLATIONS);
  assert.deepEqual(api.calls, [["create", null]]);
});

test("the robot's translations stand in when the public catalogue fails", async () => {
  const api = catalogueApi({
    translations: async () => {
      throw new Error("public origin unreachable");
    },
  });
  assert.deepEqual((await openWithCatalogue(api)).translations, SERVER_TRANSLATIONS);
});

test("the robot's translations stand in when the public catalogue stalls", async () => {
  const api = catalogueApi({ translations: () => new Promise(() => {}) });
  const started = Date.now();
  const payload = await openWithCatalogue(api);
  assert.deepEqual(payload.translations, SERVER_TRANSLATIONS);
  assert.ok(Date.now() - started < 2_000);
  assert.ok(SESSION_PUBLIC_TRANSLATIONS_TIMEOUT_MS >= 5_000);
});

test("an empty public catalogue also falls back to the robot's list", async () => {
  const api = catalogueApi({ translations: async () => [] });
  assert.deepEqual((await openWithCatalogue(api)).translations, SERVER_TRANSLATIONS);
});

test("without any translation source the public failure is raised", async () => {
  const api = catalogueApi({
    translations: async () => {
      throw new Error("public origin unreachable");
    },
    serverTranslations: null,
  });
  await assert.rejects(openWithCatalogue(api), /public origin unreachable/);
});

test("a resumed session gets the same catalogue fallback", async () => {
  const storage = new MemoryStorage();
  await openWithCatalogue(
    catalogueApi({ translations: async () => PUBLIC_TRANSLATIONS }),
    { storage },
  );
  const api = catalogueApi({ translations: () => new Promise(() => {}) });
  const payload = await openWithCatalogue(api, { storage });
  assert.deepEqual(api.calls, [["resume", "SessionToken_0123456789abcdef"]]);
  assert.deepEqual(payload.translations, SERVER_TRANSLATIONS);
});
