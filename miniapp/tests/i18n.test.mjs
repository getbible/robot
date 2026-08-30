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

test("falls back to the canonical English name for a newly accepted topic", () => {
  const originalDocument = globalThis.document;
  try {
    globalThis.document = {
      documentElement: { lang: "en", dir: "ltr" },
    };
    const catalogs = {
      en: Object.freeze({
        "bookmark_topics.prayer-and-fasting": "Prayer and Fasting",
      }),
      af: Object.freeze({}),
    };
    const i18n = new I18n(catalogs);

    i18n.setLocale("af");
    assert.equal(
      i18n.t("bookmark_topics.prayer-and-fasting"),
      "Prayer and Fasting",
    );
    assert.equal(
      Object.hasOwn(catalogs.af, "bookmark_topics.prayer-and-fasting"),
      false,
    );
  } finally {
    globalThis.document = originalDocument;
  }
});

test("uses governed few forms for integer bookmark counts", () => {
  const originalDocument = globalThis.document;
  const expected = {
    cs: ["2 záložky", "5 záložek"],
    pl: ["2 zakładki", "5 zakładek"],
    ru: ["2 закладки", "5 закладок"],
    uk: ["2 закладки", "5 закладок"],
  };

  try {
    globalThis.document = {
      documentElement: { lang: "en", dir: "ltr" },
    };
    const i18n = new I18n();

    for (const [locale, [few, many]] of Object.entries(expected)) {
      i18n.setLocale(locale);
      assert.equal(i18n.plural("bookmarks.count", 2), few, locale);
      assert.equal(i18n.plural("bookmarks.count", 5), many, locale);
    }
    i18n.setLocale("cu");
    assert.equal(i18n.plural("bookmarks.count", 2), "2 закладки");
    assert.equal(i18n.plural("bookmarks.count", 5), "5 закладок");
    assert.equal(i18n.plural("bookmarks.count", 21), "21 закладка");

    i18n.setLocale("ru");
    assert.equal(i18n.plural("bookmarks.count", 21), "21 закладка");
    assert.doesNotMatch(
      i18n.plural("bookmarks.default_tags_restored", 21),
      /\{count\}/,
    );
    i18n.setLocale("uk");
    assert.equal(i18n.plural("bookmarks.count", 21), "21 закладка");

    i18n.setLocale("tl");
    assert.equal(i18n.plural("bookmarks.count", 2), "2 bookmark");
    i18n.setLocale("tsg");
    assert.equal(i18n.plural("bookmarks.count", 5), "5 bookmark");

    i18n.setLocale("fr");
    assert.equal(i18n.plural("bookmarks.count", 0), "0 signets");
  } finally {
    globalThis.document = originalDocument;
  }
});
