import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

import { UI_CATALOGS } from "../lib/i18n.js";

const root = new URL("../", import.meta.url);

test("keeps executable code and styling outside the document", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");

  assert.doesNotMatch(html, /<style(?:\s|>)/i);
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)[^>]*>/i);
  assert.match(html, /Content-Security-Policy/);
  assert.match(html, /telegram-web-app\.js/);
  assert.match(html, /robots" content="noindex, nofollow, noarchive"/);
});

test("uses only relative same-origin API paths and no authoritative verse payload", async () => {
  const api = await readFile(new URL("lib/api.js", root), "utf8");

  assert.match(api, /const API_ROOT = "api\/v1\/"/);
  assert.doesNotMatch(api, /fetch\("\//);
  assert.doesNotMatch(api, /sendData/);
  assert.match(api, /selection_id/);
  assert.doesNotMatch(api, /body: \{[^}]*\btext\b/s);
});

test("keeps Bible as the compact reader and selector without adding a fifth route", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const css = await readFile(new URL("styles.css", root), "utf8");
  const app = await readFile(new URL("app.js", root), "utf8");

  assert.equal([...html.matchAll(/data-route="/g)].length, 4);
  assert.match(html, /id="bible-passage"/);
  assert.match(html, /id="bible-previous"/);
  assert.match(html, /id="bible-next"/);
  assert.match(html, /id="bottom-nav-handle"/);
  assert.match(css, /\.reader-toolbar\.is-hidden/);
  assert.match(css, /\.bottom-nav\.is-collapsed/);
  assert.match(app, /rememberVisibleReaderPosition/);
  assert.match(app, /persistVisibleReaderPosition/);
  assert.match(app, /openBibleAtVerse/);
});

test("does not persist Telegram launch data or bearer tokens beyond sessionStorage", async () => {
  const app = await readFile(new URL("app.js", root), "utf8");
  const api = await readFile(new URL("lib/api.js", root), "utf8");
  const session = await readFile(new URL("lib/session.js", root), "utf8");
  const source = `${app}\n${api}\n${session}`;

  assert.doesNotMatch(source, /\blocalStorage\b/);
  assert.doesNotMatch(source, /\bDeviceStorage\b/);
  assert.match(session, /sessionStorage/);
  assert.doesNotMatch(source, /setItem\([^,]+,\s*(?:bridge\.)?initData/);
  assert.match(session, /subtle\.digest\("SHA-256"/);
});

test("references the optimized hero and consistent getBible.Life brand", async () => {
  const css = await readFile(new URL("styles.css", root), "utf8");
  const html = await readFile(new URL("index.html", root), "utf8");

  assert.match(css, /ocean-light-hero\.webp/);
  assert.equal(
    [...html.matchAll(/getbible-upright\.png/g)].length,
    4,
  );
  assert.doesNotMatch(html, /getbible-(?:book|mark)\.png/);
  await assert.rejects(access(new URL("assets/getbible-book.png", root)));
  await assert.rejects(access(new URL("assets/getbible-mark.png", root)));
  assert.match(html, /getBible<span>\.Life/);
  assert.doesNotMatch(html, /GetBible|GETBIBLE|getBible\.life/);
  assert.doesNotMatch(html, /GETBIBLE\.NET/);
});

test("has English catalog coverage for every marked interface and accessibility key", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const keys = [
    ...html.matchAll(
      /data-i18n(?:-placeholder|-aria-label)?="([^"]+)"/g,
    ),
  ].map((match) => match[1]);
  const missing = [...new Set(keys)].filter(
    (key) => typeof UI_CATALOGS.en[key] !== "string",
  );

  assert.deepEqual(missing, []);
  assert.ok(keys.length >= 60);
});

test("ships complete localized catalogs for every GetBible translation language", () => {
  const englishKeys = Object.keys(UI_CATALOGS.en).sort();
  const expectedLocales = [
    "af", "ar", "br", "ch", "chr", "cop", "cs", "cu", "da", "de", "el",
    "en", "enm", "eo", "es", "et", "eu", "fi", "fr", "gd", "got", "grc",
    "gv", "hbo", "he", "hr", "hu", "hy", "it", "ja", "ko", "la", "lt",
    "lv", "mg", "mi", "mlf", "mn", "my", "nb", "nd", "nl", "nn", "pl",
    "pon", "pot", "ppk", "prs", "pt", "rmq", "ro", "ru", "sn", "sq", "sr",
    "sv", "sw", "syr", "th", "tl", "tlh", "tpi", "tr", "tsg", "uk", "vi",
    "zh", "zh-hans", "zh-hant",
  ];

  assert.deepEqual(Object.keys(UI_CATALOGS).sort(), expectedLocales);
  for (const [locale, catalog] of Object.entries(UI_CATALOGS)) {
    assert.deepEqual(Object.keys(catalog).sort(), englishKeys, locale);
    for (const key of englishKeys) {
      assert.equal(typeof catalog[key], "string", `${locale}:${key}`);
      assert.notEqual(catalog[key].trim(), "", `${locale}:${key}`);
      assert.deepEqual(
        [...catalog[key].matchAll(/\{([a-z_]+)\}/g)].map((match) => match[1]).sort(),
        [...UI_CATALOGS.en[key].matchAll(/\{([a-z_]+)\}/g)]
          .map((match) => match[1])
          .sort(),
        `${locale}:${key}`,
      );
    }
  }
});

test("uses one exclusive translation selector and invalidates stale content", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const app = await readFile(new URL("app.js", root), "utf8");

  assert.equal(UI_CATALOGS.en["home.eyebrow"], "The Holy Word of God");
  assert.equal(UI_CATALOGS.en["home.title"], "Read, find, and share His Word.");
  assert.equal(
    UI_CATALOGS.en["home.body"],
    "Gather all the Scripture you need, then post them together.",
  );
  assert.match(html, />The Holy Word of God</);
  assert.match(html, />Read, find, and share His Word\.</);
  assert.doesNotMatch(app, /Scripture, beautifully close|Move quietly through Scripture/);
  assert.match(html, /id="translation-dialog"/);
  assert.match(html, /id="translation-select"/);
  assert.doesNotMatch(html, /id="filter-translation"/);
  assert.doesNotMatch(html, /id="bible-translation"/);
  assert.match(
    app,
    /translationShortcut\.addEventListener\("click",[\s\S]*?openTranslationSelector\(\)/,
  );
  assert.doesNotMatch(
    app,
    /translationShortcut\.addEventListener\("click",[\s\S]{0,160}setRoute\("search"\)/,
  );
  assert.match(
    app,
    /async function changeTranslation\([\s\S]*?resetBibleForTranslationChange\([\s\S]*?renderLocalizedState\(\)[\s\S]*?loadBibleBooks\(\)/,
  );
  assert.match(
    app,
    /function resetBibleForTranslationChange\([\s\S]*?state\.bible\.verses = \[\][\s\S]*?state\.bible\.status = loading \? "loading" : "idle"/,
  );
  assert.match(app, /searchRequestId \+= 1/);
  assert.match(app, /state\.bible\.requestId \+= 1/);
});

test("ships parseable OpenAPI JSON at the documented relative root", async () => {
  const raw = await readFile(new URL("api-contract.json", root), "utf8");
  const contract = JSON.parse(raw);

  assert.equal(contract.openapi, "3.1.0");
  assert.equal(contract.servers[0].url, "api/v1");
  assert.ok(contract.paths["/session"]);
  assert.ok(contract.paths["/basket/order"]);
  assert.ok(contract.paths["/post"]);
});
