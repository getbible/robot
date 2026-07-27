import { TRANSLATED_MESSAGES } from "./locales.js";
import { ENGLISH_MESSAGES } from "./messages.en.js";

export const UI_CATALOGS = Object.freeze({
  en: ENGLISH_MESSAGES,
  ...TRANSLATED_MESSAGES,
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
    const [supportedLocale] = Intl.PluralRules.supportedLocalesOf([
      this.#locale,
    ]);
    const category = new Intl.PluralRules(
      supportedLocale ?? this.#fallback,
    ).select(count);
    const localizedKey = `${key}_${category}`;
    const fallbackKey = `${key}_other`;
    const hasLocalizedKey = Object.hasOwn(
      this.#catalogs[this.#locale] ?? {},
      localizedKey,
    );
    const resolvedKey = hasLocalizedKey ? localizedKey : fallbackKey;
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
