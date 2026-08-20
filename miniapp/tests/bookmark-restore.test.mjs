import assert from "node:assert/strict";
import test from "node:test";

import { restoreBookmarkBackup } from "../lib/bookmark-restore.js";

const PAYLOAD = Object.freeze({
  backup: { version: 2 },
  source: { file_size: 123 },
});

function harness({ confirmed = true, persistence = { cloud: "synced" } } = {}) {
  const events = [];
  return {
    events,
    options: {
      async fetchRestore() {
        events.push("fetch");
        return PAYLOAD;
      },
      async confirmRestore(payload) {
        events.push("confirm");
        assert.equal(payload, PAYLOAD);
        return confirmed;
      },
      importBackup(backup, options) {
        events.push("import");
        assert.equal(backup, PAYLOAD.backup);
        assert.deepEqual(options, { byteLength: 123 });
        return { bookmarks_added: 1 };
      },
      async flushPersistence() {
        events.push("flush");
        return persistence;
      },
      async acknowledgeRestore() {
        events.push("acknowledge");
      },
    },
  };
}

test("persists a confirmed restore before acknowledging its launch reference", async () => {
  const { events, options } = harness();

  const result = await restoreBookmarkBackup(options);

  assert.equal(result.status, "restored");
  assert.equal(result.acknowledgementError, null);
  assert.deepEqual(result.imported, { bookmarks_added: 1 });
  assert.deepEqual(events, ["fetch", "confirm", "import", "flush", "acknowledge"]);
});

test("leaves the restore reference untouched when confirmation is declined", async () => {
  const { events, options } = harness({ confirmed: false });

  const result = await restoreBookmarkBackup(options);

  assert.equal(result.status, "declined");
  assert.deepEqual(events, ["fetch", "confirm"]);
});

test("does not acknowledge when the imported document has no durable copy", async () => {
  const { events, options } = harness({
    persistence: { local: "error", device: "error", cloud: "error" },
  });

  await assert.rejects(
    restoreBookmarkBackup(options),
    /could not be persisted/i,
  );
  assert.deepEqual(events, ["fetch", "confirm", "import", "flush"]);
});

test("reports acknowledgement failure after keeping the persisted import", async () => {
  const { events, options } = harness({ persistence: { local: "ready" } });
  options.acknowledgeRestore = async () => {
    events.push("acknowledge");
    throw new Error("offline");
  };

  const result = await restoreBookmarkBackup(options);

  assert.match(result.acknowledgementError.message, /offline/);
  assert.deepEqual(events, ["fetch", "confirm", "import", "flush", "acknowledge"]);
});

test("requires every restore dependency", async () => {
  await assert.rejects(
    restoreBookmarkBackup({}),
    /fetchRestore must be a function/i,
  );
});
