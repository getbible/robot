import assert from "node:assert/strict";
import test from "node:test";

import {
  CONTRIBUTION_JOURNAL_MAX_BYTES,
  IndexedDbContributionJournal,
} from "../lib/contribution-journal.js";

const KEY = `getbible.miniapp.contributions.v1:${"1".repeat(16)}:${"a".repeat(64)}`;

function openingIndexedDB(event, database = null) {
  return {
    open() {
      const listeners = new Map();
      const request = {
        result: database,
        error: null,
        addEventListener(name, handler) {
          listeners.set(name, handler);
        },
      };
      if (event) {
        queueMicrotask(() => listeners.get(event)?.());
      }
      return request;
    },
  };
}

class StallingDatabase {
  closed = false;
  listeners = new Map();
  objectStoreNames = { contains: () => true };

  addEventListener(name, handler) {
    this.listeners.set(name, handler);
  }

  close() {
    this.closed = true;
  }

  transaction() {
    return {
      addEventListener() {},
      objectStore: () => ({
        get: () => ({ addEventListener() {} }),
        put() {},
        delete() {},
      }),
    };
  }
}

test("a contribution journal whose IndexedDB never opens fails within its bound", async () => {
  await assert.rejects(
    IndexedDbContributionJournal.open({
      key: KEY,
      indexedDB: openingIndexedDB(null),
      timeoutMs: 20,
    }),
    /IndexedDB contribution open timed out/,
  );
});

test("a blocked contribution journal open rejects instead of waiting", async () => {
  await assert.rejects(
    IndexedDbContributionJournal.open({
      key: KEY,
      indexedDB: openingIndexedDB("blocked"),
      timeoutMs: 1_000,
    }),
    /blocked/,
  );
});

test("contribution journal operations that never settle fail within their bound", async () => {
  const database = new StallingDatabase();
  const journal = await IndexedDbContributionJournal.open({
    key: KEY,
    indexedDB: openingIndexedDB("success", database),
    timeoutMs: 20,
  });
  await assert.rejects(journal.read(), /IndexedDB contribution read timed out/);
  await assert.rejects(journal.write("{}"), /IndexedDB contribution write timed out/);
  await assert.rejects(journal.remove(), /IndexedDB contribution delete timed out/);
  await assert.rejects(
    journal.write("x".repeat(CONTRIBUTION_JOURNAL_MAX_BYTES + 1)),
    RangeError,
  );

  database.listeners.get("versionchange")();
  assert.equal(database.closed, true);
});

test("the contribution journal validates its key and bound", async () => {
  await assert.rejects(
    IndexedDbContributionJournal.open({
      key: "bad key",
      indexedDB: openingIndexedDB("success", new StallingDatabase()),
    }),
    TypeError,
  );
  await assert.rejects(
    IndexedDbContributionJournal.open({
      key: KEY,
      indexedDB: openingIndexedDB("success", new StallingDatabase()),
      timeoutMs: 0,
    }),
    RangeError,
  );
  await assert.rejects(
    IndexedDbContributionJournal.open({ key: KEY, indexedDB: null }),
    TypeError,
  );
});
