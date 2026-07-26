export const UI_CATALOGS = Object.freeze({
  en: Object.freeze({
    "gate.opening": "Opening getBible.Life…",
    "gate.protected": "Protected Mini App",
    "gate.title": "Open getBible.Life from Telegram",
    "gate.body":
      "This scripture experience is available only through the getBible.Life bot.",
    "gate.retry": "Try again",
    "nav.label": "getBible.Life",
    "nav.home": "Home",
    "nav.search": "Search",
    "nav.bible": "Bible",
    "nav.selected": "Selected",
    "home.eyebrow": "Scripture, beautifully close",
    "home.title": "Read, find, and share the Word.",
    "home.body":
      "Move quietly through Scripture, gather the verses you need, then post them together.",
    "home.search": "Search Scripture",
    "home.search_hint": "Find words, phrases, and themes",
    "home.browse": "Browse the Bible",
    "home.browse_hint": "Choose a book, chapter, and verses",
    "home.review": "Review",
    "search.eyebrow": "Find Scripture",
    "search.title": "Search the Word",
    "search.body":
      "Find a verse, select it where you read it, and build one clean post.",
    "search.label": "Search Scripture",
    "search.placeholder": "Search words or a phrase",
    "search.submit": "Search",
    "search.options": "Search options",
    "search.filters": "Filters",
    "search.sort_label": "Sort results",
    "search.sort_canonical": "Bible order",
    "search.sort_relevance": "Relevance",
    "search.clear": "Clear",
    "search.results_label": "Scripture search results",
    "search.load_more": "Load more",
    "bible.eyebrow": "Browse Scripture",
    "bible.title": "Choose a passage",
    "bible.body": "Open a chapter, then tap every verse you want to include.",
    "bible.navigation": "Bible navigation",
    "bible.translation": "Translation",
    "bible.book": "Book",
    "bible.chapter": "Chapter",
    "bible.choose_book": "Choose a book",
    "bible.choose_chapter": "Choose a chapter",
    "bible.verses_label": "Chapter verses",
    "selection.eyebrow": "Your selection",
    "selection.title": "Ready to post",
    "selection.none": "No verses selected yet.",
    "selection.clear": "Clear",
    "selection.empty_title": "Your selected verses will appear here",
    "selection.empty_body":
      "Browse or search, then tap the full verse card to add it.",
    "selection.browse": "Browse the Bible",
    "selection.order_label": "Selected verses in posting order",
    "selection.post": "Post selected verses",
    "filters.eyebrow": "Search settings",
    "filters.title": "Filter results",
    "filters.close": "Close filters",
    "filters.words": "Words",
    "filters.all": "All",
    "filters.any": "Any",
    "filters.phrase": "Phrase",
    "filters.match": "Match",
    "filters.whole_word": "Whole word",
    "filters.substring": "Substring",
    "filters.scope": "Scope",
    "filters.old": "Old",
    "filters.new": "New",
    "filters.other": "Other",
    "filters.books": "Books",
    "filters.select_all": "Select all",
    "filters.books_help": "Leave every book unchecked to search all books.",
    "filters.case": "Case sensitive",
    "filters.case_hint": "Match upper and lowercase exactly",
    "filters.diacritics": "Ignore diacritics",
    "filters.diacritics_hint": "Treat accented characters as equal",
    "filters.exclude": "Exclude words",
    "filters.exclude_placeholder": "Optional, separated by spaces",
    "filters.proximity": "Maximum words apart",
    "filters.proximity_placeholder": "No limit",
    "filters.proximity_hint": "Available when matching all words.",
    "filters.reset": "Reset",
    "filters.apply": "Show results",
    "connection.offline": "You’re offline. Reconnect to continue.",
    "translation.change_aria":
      "Change default translation, currently {translation}",
    "verse.add_aria": "Add {reference}: {text}",
    "verse.remove_aria": "Remove {reference}: {text}",
    "selection.move_earlier": "Move {reference} earlier",
    "selection.move_later": "Move {reference} later",
    "selection.remove_aria": "Remove {reference}",
  }),
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
