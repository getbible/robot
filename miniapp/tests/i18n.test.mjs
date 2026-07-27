import assert from "node:assert/strict";
import test from "node:test";

import { I18n, UI_CATALOGS, resolveLocale } from "../lib/i18n.js";

test("applies a selected translation locale immediately to text and direction", () => {
  const originalDocument = globalThis.document;
  const textElement = { dataset: { i18n: "home.title" }, textContent: "" };
  const placeholderElement = {
    dataset: { i18nPlaceholder: "search.placeholder" },
    attributes: {},
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
  };
  const ariaElement = {
    dataset: { i18nAriaLabel: "search.label" },
    attributes: {},
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
  };
  const root = {
    querySelectorAll(selector) {
      return {
        "[data-i18n]": [textElement],
        "[data-i18n-placeholder]": [placeholderElement],
        "[data-i18n-aria-label]": [ariaElement],
      }[selector] ?? [];
    },
  };

  try {
    globalThis.document = {
      documentElement: { lang: "en", dir: "ltr" },
    };
    const i18n = new I18n();

    assert.equal(i18n.setLocale("af", "ltr"), "af");
    i18n.apply(root);

    assert.equal(textElement.textContent, "Lees, vind en deel Sy Woord.");
    assert.equal(
      placeholderElement.attributes.placeholder,
      UI_CATALOGS.af["search.placeholder"],
    );
    assert.equal(
      ariaElement.attributes["aria-label"],
      UI_CATALOGS.af["search.label"],
    );
    assert.equal(globalThis.document.documentElement.lang, "af");
    assert.equal(globalThis.document.documentElement.dir, "ltr");
    assert.equal(i18n.plural("verse.count", 2), "2 verse");

    assert.equal(i18n.setLocale("hbo", "rtl"), "hbo");
    assert.doesNotThrow(() => i18n.plural("verse.count", 2));
    assert.equal(globalThis.document.documentElement.dir, "rtl");
  } finally {
    globalThis.document = originalDocument;
  }
});

test("resolves exact, regional, historical, and unknown translation locales", () => {
  const available = Object.keys(UI_CATALOGS);

  assert.equal(resolveLocale("af-ZA", available), "af");
  assert.equal(resolveLocale("zh-Hant", available), "zh-hant");
  assert.equal(resolveLocale("grc", available), "grc");
  assert.equal(resolveLocale("not a locale", available), "en");
});
