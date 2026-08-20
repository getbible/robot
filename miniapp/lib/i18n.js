import {
  BOOKMARK_LOCALE_EXTENSION,
  BOOKMARK_LOCALE_POLICY_SOURCES,
} from "./bookmark-locales.js";
import { SCOPED_LOCALE_OVERRIDES } from "./bookmark-locales-scoped-overrides.js";
import { TRANSLATED_MESSAGES } from "./locales.js";
import { ENGLISH_MESSAGES } from "./messages.en.js";

const translatedLocales = Object.keys(TRANSLATED_MESSAGES);
if (
  translatedLocales.some((locale) => !BOOKMARK_LOCALE_EXTENSION[locale]) ||
  Object.keys(BOOKMARK_LOCALE_EXTENSION).some(
    (locale) => !Object.hasOwn(TRANSLATED_MESSAGES, locale),
  ) ||
  Object.keys(SCOPED_LOCALE_OVERRIDES).some(
    (locale) => !Object.hasOwn(TRANSLATED_MESSAGES, locale),
  )
) {
  throw new TypeError("Localized Mini App catalogs do not match.");
}

export const UI_CATALOGS = Object.freeze({
  en: ENGLISH_MESSAGES,
  ...Object.fromEntries(
    Object.entries(TRANSLATED_MESSAGES).map(([locale, catalog]) => [
      locale,
      Object.freeze({
        ...catalog,
        ...(Object.hasOwn(BOOKMARK_LOCALE_POLICY_SOURCES, locale)
          ? {}
          : (SCOPED_LOCALE_OVERRIDES[locale] ?? {})),
        ...BOOKMARK_LOCALE_EXTENSION[locale],
      }),
    ]),
  ),
});

export class I18n {
  #catalogs;
  #fallback;
  #locale;

  constructor(catalogs = UI_CATALOGS, fallback = "en") {
    this.#catalogs = catalogs;
    this.#fallback = normalizeLocale(fallback) ?? "en";
    this.#locale = this.#fallback;
  }

  get locale() {
    return this.#locale;
  }

  setLocale(requestedLocale, direction = "ltr") {
    this.#locale = resolveLocale(
      requestedLocale,
      Object.keys(this.#catalogs),
      this.#fallback,
    );
    document.documentElement.lang = this.#locale;
    document.documentElement.dir = direction === "rtl" ? "rtl" : "ltr";
    return this.#locale;
  }

  t(key, values = {}) {
    const template =
      this.#catalogs[this.#locale]?.[key] ??
      this.#catalogs[this.#fallback]?.[key] ??
      key;
    return template.replace(/\{([a-z_]+)\}/gi, (match, name) =>
      Object.hasOwn(values, name) ? String(values[name]) : match,
    );
  }

  plural(key, count, values = {}) {
    const policyLocale = BOOKMARK_LOCALE_POLICY_SOURCES[this.#locale];
    const pluralLocales = [...new Set([
      policyLocale ?? this.#locale,
      this.#locale,
      this.#fallback,
    ])];
    const [supportedLocale] = Intl.PluralRules.supportedLocalesOf(pluralLocales);
    const category = new Intl.PluralRules(
      supportedLocale ?? this.#fallback,
    ).select(count);
    const localizedKey = `${key}_${category}`;
    const fallbackKey = `${key}_other`;
    const hasLocalizedKey = Object.hasOwn(
      this.#catalogs[this.#locale] ?? {},
      localizedKey,
    );
    let resolvedKey = hasLocalizedKey ? localizedKey : fallbackKey;
    if (
      category === "one" &&
      Number(count) !== 1 &&
      !this.#catalogs[this.#locale]?.[localizedKey]?.includes("{count}")
    ) {
      // Some CLDR `one` categories include 0, 2, 5, or numbers ending in 1.
      // A literal “One …” translation must never be used for those values;
      // governed numeric singular forms opt in by carrying `{count}`.
      resolvedKey = fallbackKey;
    }
    return this.t(resolvedKey, { ...values, count });
  }

  apply(root = document) {
    root.querySelectorAll("[data-i18n]").forEach((element) => {
      element.textContent = this.t(element.dataset.i18n);
    });
    root.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
      element.setAttribute(
        "placeholder",
        this.t(element.dataset.i18nPlaceholder),
      );
    });
    root.querySelectorAll("[data-i18n-aria-label]").forEach((element) => {
      element.setAttribute(
        "aria-label",
        this.t(element.dataset.i18nAriaLabel),
      );
    });
  }
}

export function resolveLocale(requestedLocale, availableLocales, fallback = "en") {
  const available = new Map(
    availableLocales.map((locale) => [normalizeLocale(locale), locale]),
  );
  const normalized = normalizeLocale(requestedLocale);
  if (normalized && available.has(normalized)) {
    return available.get(normalized);
  }
  const base = normalized?.split("-")[0];
  if (base && available.has(base)) {
    return available.get(base);
  }
  const normalizedFallback = normalizeLocale(fallback) ?? "en";
  return available.get(normalizedFallback) ?? availableLocales[0] ?? "en";
}

function normalizeLocale(value) {
  if (
    typeof value !== "string" ||
    value.length > 35 ||
    !/^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(value)
  ) {
    return null;
  }
  return value.toLocaleLowerCase();
}
