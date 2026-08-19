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
  assert.doesNotMatch(api, /clipboard/i);
});

test("declares one accessible copy control without import-time DOM installation", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const clipboard = await readFile(new URL("lib/clipboard.js", root), "utf8");

  assert.equal([...html.matchAll(/id="copy-selection"/g)].length, 1);
  assert.match(
    html,
    /id="copy-selection"[\s\S]*?type="button"[\s\S]*?aria-label="Copy selected verses"[\s\S]*?hidden/,
  );
  assert.ok(
    html.indexOf('id="copy-selection"') < html.indexOf('id="post-selection"'),
  );
  assert.doesNotMatch(clipboard, /MutationObserver/);
  assert.doesNotMatch(clipboard, /DOMContentLoaded/);
  assert.doesNotMatch(clipboard, /typeof window !== "undefined"/);
});

test("revokes the live session when a successful post closes the Mini App", async () => {
  const app = await readFile(new URL("app.js", root), "utf8");

  assert.match(
    app,
    /clearBoundSession\(\);\s*void api\.revokeSession\(\)\.catch\(\(\) => undefined\);\s*bridge\.close\(\);/,
  );
});

test("keeps Bible as the compact reader and selector without adding a fifth route", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const css = await readFile(new URL("styles.css", root), "utf8");
  const app = await readFile(new URL("app.js", root), "utf8");

  assert.equal([...html.matchAll(/data-route="/g)].length, 4);
  const readerToolbar = html.match(/<div id="bible-heading"[\s\S]*?<\/div>/)?.[0] ?? "";
  const bottomNavigation = html.match(/<nav[\s\S]*?id="bottom-nav"[\s\S]*?<\/nav>/)?.[0] ?? "";
  const historyOffset = bottomNavigation.indexOf('id="bible-history"');
  const historyTrigger = bottomNavigation.slice(
    bottomNavigation.lastIndexOf("<button", historyOffset),
    bottomNavigation.indexOf(">", historyOffset) + 1,
  );
  assert.match(html, /id="bible-passage"/);
  assert.match(html, /id="bible-previous"/);
  assert.match(html, /id="bible-next"/);
  assert.doesNotMatch(readerToolbar, /id="bible-history"/);
  assert.match(bottomNavigation, /id="bible-history"/);
  assert.match(bottomNavigation, /class="bottom-nav__icon bottom-nav__history-icon"/);
  assert.match(bottomNavigation, /aria-hidden="true"\s+focusable="false"/);
  assert.doesNotMatch(historyTrigger, /data-route=/);
  assert.match(html, /aria-controls="reading-history-dialog"/);
  assert.match(html, /aria-haspopup="dialog"/);
  assert.match(html, /id="reading-history-dialog"/);
  assert.match(html, /aria-labelledby="reading-history-title"/);
  assert.match(html, /id="bible-navigation-dialog"/);
  assert.match(html, /id="bible-book-grid"/);
  assert.match(html, /id="bible-chapter-grid"/);
  assert.match(html, /id="bible-picker-back"/);
  assert.match(html, /id="close-bible-navigation"/);
  assert.match(html, /aria-labelledby="bible-navigation-title"/);
  assert.doesNotMatch(html, /id="bible-(?:book|chapter)"[^-]/);
  assert.match(html, /id="bottom-nav-handle"/);
  assert.match(html, /id="translation-short-label"/);
  assert.match(html, /id="translation-full-label"/);
  assert.match(
    html,
    /id="translation-full-label"\s+class="translation-chip__full"\s+dir="auto"/,
  );
  assert.match(css, /\.reader-toolbar\.is-hidden/);
  assert.match(
    css,
    /\.reader-toolbar\s*{[\s\S]*?grid-template-columns: 44px minmax\(0, 1fr\) 44px/,
  );
  assert.match(css, /data-telegram-fullscreen="true"/);
  assert.match(css, /\.is-header-condensed/);
  assert.match(css, /--fullscreen-control-height: 32px/);
  assert.match(css, /--fullscreen-control-hit-size: 44px/);
  assert.match(css, /--fullscreen-control-clearance: clamp\(72px, 20vw, 120px\)/);
  assert.match(css, /--fullscreen-translation-max-width: min\(48vw, 248px\)/);
  assert.match(css, /--fullscreen-topbar-height: 70px/);
  assert.doesNotMatch(css, /:not\(\[data-active-route="home"\]\)/);
  assert.match(css, /\.bottom-nav\.is-collapsed/);
  assert.match(css, /\.bottom-nav\.is-collapsed \.bottom-nav__items/);
  assert.match(
    css,
    /\.app-shell\[data-active-route="bible"\] \.bottom-nav__items\s*{\s*grid-template-columns: repeat\(5, minmax\(0, 1fr\)\)/,
  );
  assert.match(
    css,
    /\.bottom-nav__history-icon\s*{[\s\S]*?width: 27px;[\s\S]*?fill: none;[\s\S]*?stroke: currentColor/,
  );
  assert.doesNotMatch(
    css,
    /translateY\(calc\(100% - var\(--nav-handle-height\)/,
  );
  assert.match(css, /\.sheet__surface--passage/);
  assert.match(css, /\.sheet__surface--history/);
  assert.match(css, /\.sheet__header\.history-header/);
  assert.match(css, /\.history-content[\s\S]*?overflow: hidden/);
  assert.match(css, /\.history-list[\s\S]*?overflow-y: auto/);
  assert.match(css, /grid-template-columns: repeat\(6, minmax\(0, 1fr\)\)/);
  assert.match(css, /@media \(max-width: 367px\)/);
  assert.match(app, /rememberVisibleReaderPosition/);
  assert.match(app, /persistVisibleReaderPosition/);
  assert.match(app, /openBibleAtVerse/);
  assert.match(app, /recordReadingHistory\("chapter", visitedVerse\)/);
  assert.match(app, /recordReadingHistory\("selection", addedVerse\)/);
  assert.match(app, /readingHistory\.remove/);
  assert.match(app, /readingHistory\.clear/);
  assert.match(app, /elements\.bibleHistory\.hidden = route !== "bible"/);
  assert.match(app, /async function chooseBiblePickerBook/);
  assert.match(app, /async function chooseBiblePickerChapter/);
  assert.match(app, /requestId !== state\.bible\.pickerRequestId/);
  assert.match(app, /abbreviateBookName\(book\.name\)/);
  assert.match(app, /uniqueBookLabels\(state\.bible\.books\)/);
  assert.match(app, /localizedCloseLabel\(navigationLabel\)/);
  assert.match(app, /state\.route !== "bible"/);
  assert.match(app, /function setHeaderCondensed/);
  assert.doesNotMatch(app, /state\.route !== "home" && Boolean\(condensed\)/);
  assert.match(app, /elements\.translationFullLabel\.textContent/);
  assert.match(app, /if \(state\.route === "bible"\) \{\s*revealNavigation\(\)/);
  assert.match(app, /scrollTop > state\.navigationRevealScrollTop \+ 10/);
  assert.match(app, /sessionGeneration \+= 1/);
  assert.match(app, /generation !== sessionGeneration/);
  assert.match(app, /button\.setAttribute\("aria-label", book\.name\)/);
});

test("does not persist Telegram launch data or bearer tokens beyond sessionStorage", async () => {
  const app = await readFile(new URL("app.js", root), "utf8");
  const api = await readFile(new URL("lib/api.js", root), "utf8");
  const session = await readFile(new URL("lib/session.js", root), "utf8");
  const history = await readFile(
    new URL("lib/reading-history-store.js", root),
    "utf8",
  );
  const source = `${app}\n${api}\n${session}\n${history}`;

  assert.doesNotMatch(source, /\blocalStorage\b/);
  assert.doesNotMatch(source, /\bDeviceStorage\b/);
  assert.match(session, /sessionStorage/);
  assert.match(history, /sessionStorage/);
  assert.doesNotMatch(source, /setItem\([^,]+,\s*(?:bridge\.)?initData/);
  assert.match(session, /subtle\.digest\("SHA-256"/);
});

test("stores bounded coordinate-only reading history in the browser session", async () => {
  const history = await readFile(
    new URL("lib/reading-history-store.js", root),
    "utf8",
  );

  assert.match(history, /DEFAULT_MAXIMUM = 1_000/);
  assert.match(history, /getbible\.miniapp\.reading-history/);
  assert.doesNotMatch(history, /\blocalStorage\b/);
  assert.doesNotMatch(history, /session_token|init_data|user_id|verse_text/);
  assert.match(history, /this\.#storage\.removeItem\(STORAGE_KEY\)/);
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
  assert.ok(contract.paths["/session"].delete);
  assert.ok(contract.paths["/cleanup"].post);
  assert.ok(contract.paths["/basket/order"]);
  assert.ok(contract.paths["/post"]);
  assert.equal(
    contract.paths["/basket/order"].patch.requestBody.content[
      "application/json"
    ].schema.properties.selection_ids.maxItems,
    200,
  );
  assert.equal(contract.components.schemas.Basket.properties.items.maxItems, 200);
  assert.equal(contract.components.schemas.Basket.properties.maximum.maximum, 200);
  assert.equal(
    contract.paths["/translations"].get.responses["200"].content[
      "application/json"
    ].schema.properties.items.maxItems,
    1000,
  );
  assert.equal(
    contract.paths["/chapters"].get.responses["200"].content[
      "application/json"
    ].schema.properties.items.maxItems,
    500,
  );
  assert.equal(
    contract.components.schemas.Chapter.properties.number.maximum,
    1000,
  );
  assert.equal(
    contract.components.schemas.Chapter.properties.verses.maxItems,
    250,
  );
  assert.equal(contract.components.schemas.Verse.properties.verse.maximum, 2000);
  assert.equal(contract.components.schemas.Verse.properties.terms.maxItems, 20);
  assert.equal(
    contract.components.schemas.Verse.properties.terms.items.maxLength,
    80,
  );
  assert.equal(
    contract.paths["/session"].post.requestBody.content[
      "application/json"
    ].schema.properties.launch_token.minLength,
    16,
  );
  assert.equal(
    contract.paths["/session"].post.requestBody.content[
      "application/json"
    ].schema.properties.launch_token.maxLength,
    128,
  );
  assert.equal(
    contract.paths["/search/{search_id}"].get.parameters[0].schema.minLength,
    16,
  );
  assert.equal(
    contract.paths["/search/{search_id}"].get.parameters[0].schema.maxLength,
    128,
  );
  const sessionToken =
    contract.components.schemas.NewSession.allOf[1].properties.session_token;
  assert.equal(sessionToken.minLength, 16);
  assert.equal(sessionToken.maxLength, 128);
  assert.equal(sessionToken.pattern, "^[A-Za-z0-9_-]+$");

  // The page waits on a budget the robot states, rather than guessing one and
  // reporting a timeout for a search the robot was still running.
  assert.ok(
    contract.components.schemas.SessionState.required.includes("limits"),
  );
  assert.equal(
    contract.components.schemas.SessionState.properties.limits.$ref,
    "#/components/schemas/Limits",
  );
  const searchBudget =
    contract.components.schemas.Limits.properties.search_timeout_seconds;
  assert.equal(searchBudget.minimum, 1);
  assert.equal(searchBudget.maximum, 900);
});
