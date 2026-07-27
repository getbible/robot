import assert from "node:assert/strict";
import test from "node:test";

import {
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

test("authorization rejection clears the stale stored session", async () => {
  const storage = new MemoryStorage();
  const context = await sessionContext("signed-init-data", "launch-one");
  assert.equal(
    writeBoundSession(storage, { token: SESSION_TOKEN, context }),
    true,
  );

  const authorizationError = Object.assign(new Error("expired"), {
    status: 401,
  });
  await assert.rejects(
    openBoundSession(
      {
        createSession: async () => assert.fail("must attempt resume"),
        resumeSession: async () => {
          throw authorizationError;
        },
      },
      {
        initData: "signed-init-data",
        launchToken: "launch-one",
        storage,
      },
    ),
    authorizationError,
  );
  assert.equal(readBoundSession(storage, context), null);
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
