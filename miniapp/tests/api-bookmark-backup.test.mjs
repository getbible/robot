import assert from "node:assert/strict";
import test from "node:test";

import { MiniAppApi } from "../lib/api.js";

const SESSION = {
  session_token: "abcdefghijklmnop",
  expires_in: 10_800,
  user: { id: 42 },
  preferences: {
    translation: "kjv",
    search_defaults: {},
    reader_location: null,
  },
  entrypoint: { route: "bookmarks", bookmark_restore_available: true },
  basket: { count: 0, maximum: 100, items: [] },
};

const BACKUP = {
  format: "getbible-life-markings",
  version: 2,
  exportedAt: "2026-08-20T10:00:00.000Z",
  colors: [{ id: "grace", name: "Grace", value: "#bbf7d0" }],
  markings: [],
  notes: [],
};

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("backs up to the private bot chat and restores with explicit acknowledgement", async () => {
  const requests = [];
  const api = new MiniAppApi("signed-init-data", {
    baseUrl: "https://robot.example/getbible/",
    publicApi: {
      async translations() {
        return [];
      },
      async chapter() {
        return {};
      },
    },
    fetchImplementation: async (url, options) => {
      const path = new URL(url).pathname;
      requests.push({ path, options });
      if (path.endsWith("/session")) {
        return json(SESSION, 201);
      }
      if (path.endsWith("/cleanup")) {
        return new Response(null, { status: 204 });
      }
      if (path.endsWith("/bookmarks/backup")) {
        return json({ status: "backed_up", message_id: 701 });
      }
      if (path.endsWith("/bookmarks/restore") && options.method === "GET") {
        return json({
          backup: BACKUP,
          source: {
            file_name: "getbible-bookmarks.json",
            file_size: 200,
            topic_count: 1,
            bookmark_count: 0,
          },
        });
      }
      if (path.endsWith("/bookmarks/restore") && options.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return json({ error: { code: "not_found" } }, 404);
    },
  });

  await api.createSession("OwnerBoundLaunch1");
  await api.backupBookmarks(BACKUP, "abcdef0123456789");
  const restored = await api.restoreBookmarks();
  const acknowledged = await api.acknowledgeBookmarkRestore();

  assert.deepEqual(restored.backup, BACKUP);
  assert.equal(acknowledged, null);
  const backupRequest = requests.find(({ path }) =>
    path.endsWith("/bookmarks/backup")
  );
  assert.equal(backupRequest.options.method, "POST");
  assert.deepEqual(JSON.parse(backupRequest.options.body), {
    idempotency_key: "abcdef0123456789",
    backup: BACKUP,
  });
  const restoreRequests = requests.filter(({ path }) =>
    path.endsWith("/bookmarks/restore")
  );
  assert.deepEqual(
    restoreRequests.map(({ options }) => options.method),
    ["GET", "DELETE"],
  );
  for (const { options } of [backupRequest, ...restoreRequests]) {
    assert.equal(options.headers.Authorization, `Bearer ${SESSION.session_token}`);
    assert.equal(options.headers["X-Telegram-Init-Data"], undefined);
  }
});
