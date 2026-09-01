import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { UI_CATALOGS } from "../lib/i18n.js";
import { TELEGRAM_POPUP_MESSAGE_LIMIT } from "../lib/telegram.js";

// Resolve a key the way I18n.t does (locale, then English fallback) without
// touching the DOM, and fill placeholders with worst-case values.
function localized(locale, key, values = {}) {
  const template = UI_CATALOGS[locale]?.[key] ?? UI_CATALOGS.en[key] ?? key;
  return template.replace(/\{(\w+)\}/g, (match, name) =>
    name in values ? String(values[name]) : match
  );
}

/**
 * Telegram's showAlert/showConfirm reject any message outside 1-256
 * characters with a synchronous throw. The contributor disclosure (300
 * characters in every locale) once went through showAlert and turned every
 * newly approved contributor's first Sync into a failure before a single
 * request was sent. Every message the app still routes through a popup must
 * fit in every locale, and the disclosure must never go near one again.
 */

const POPUP_KEYS = [
  "history.clear_confirm",
  "bookmarks.clear_confirm",
  "bookmarks.global_clear_confirm",
  "bookmarks.topic_delete_confirm",
  "bookmarks.topic_delete_global_confirm",
  "selection.clear_confirm",
];

const SAMPLE_VALUES = {
  name: "A".repeat(80),
  count: 9_999,
  global: 9_999,
  personal: 9_999,
  total: 9_999,
};

test("every message the app passes to a Telegram popup fits in every locale", () => {
  const english = UI_CATALOGS.en;
  const failures = [];
  let checked = 0;
  for (const locale of Object.keys(UI_CATALOGS)) {
    for (const key of POPUP_KEYS) {
      if (!(key in english)) {
        continue;
      }
      checked += 1;
      const message = localized(locale, key, SAMPLE_VALUES);
      if (message.trim().length > TELEGRAM_POPUP_MESSAGE_LIMIT) {
        failures.push(`${locale} ${key} = ${message.trim().length} chars`);
      }
    }
  }
  assert.ok(checked > 0);
  assert.deepEqual(failures, []);
});

test("the contributor disclosure is rendered by the Mini App, never by a Telegram popup", async () => {
  const app = await readFile(new URL("../app.js", import.meta.url), "utf8");
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  assert.doesNotMatch(app, /bridge\.(?:alert|confirm)\([^)]*contribution_disclosure/);
  assert.match(html, /<dialog\s+id="contributor-disclosure"/);
  assert.match(html, /id="contributor-disclosure-accept"/);
  assert.match(html, /id="contributor-disclosure-decline"/);
  // The disclosure text is genuinely too long for a popup, which is exactly
  // why it must stay in-app; this pins that fact so nobody "fixes" it back.
  assert.ok(
    localized("en", "bookmarks.contribution_disclosure").length >
      TELEGRAM_POPUP_MESSAGE_LIMIT,
  );
});
