import { ApiError, MiniAppApi } from "./lib/api.js";
import { I18n } from "./lib/i18n.js";
import {
  DEFAULT_FILTERS,
  activeFilterCount,
  moveItem,
  normalizeBasket,
  normalizeBooks,
  normalizeChapters,
  normalizeFilters,
  normalizeScripture,
  normalizeSearch,
  normalizeSearchPage,
  normalizeSession,
  routeName,
  uniqueVerses,
} from "./lib/model.js";
import { TelegramBridge } from "./lib/telegram.js";

const bridge = new TelegramBridge();
const i18n = new I18n();
let api = null;
const SESSION_STORAGE_KEY = "getbible.miniapp.session";
let filterDraft = null;

const state = {
  route: "home",
  translations: [],
  filters: { ...DEFAULT_FILTERS, books: [], exclude: [] },
  basket: [],
  pendingSelections: new Set(),
  bookCache: new Map(),
  search: {
    query: "",
    status: "idle",
    error: null,
    searchId: null,
    page: 0,
    total: 0,
    hasMore: false,
    results: [],
    loadingMore: false,
  },
  bible: {
    translation: "kjv",
    books: [],
    chapters: [],
    selectedBook: null,
    selectedChapter: null,
    reference: "",
    verses: [],
    status: "idle",
    error: null,
  },
  filterBooksStatus: "idle",
  posting: false,
};

const elements = mapElements({
  boot: "boot-screen",
  bootMessage: "boot-message",
  accessDenied: "access-denied",
  accessMessage: "access-message",
  accessRetry: "access-retry",
  app: "app",
  offlineBanner: "offline-banner",
  homeHero: "home-hero",
  homeSelection: "home-selection",
  homeSelectionTitle: "home-selection-title",
  homeSelectionMeta: "home-selection-meta",
  translationShortcut: "translation-shortcut",
  searchForm: "search-form",
  searchQuery: "search-query",
  openFilters: "open-filters",
  filterCount: "filter-count",
  searchSort: "search-sort",
  searchSummary: "search-summary",
  searchSummaryTitle: "search-summary-title",
  searchSummaryMeta: "search-summary-meta",
  clearSearch: "clear-search",
  searchState: "search-state",
  searchResults: "search-results",
  loadMore: "load-more",
  bibleTranslation: "bible-translation",
  bibleBook: "bible-book",
  bibleChapter: "bible-chapter",
  bibleHeading: "bible-heading",
  bibleTranslationLabel: "bible-translation-label",
  bibleReference: "bible-reference",
  bibleVerseCount: "bible-verse-count",
  bibleState: "bible-state",
  bibleVerses: "bible-verses",
  selectionSummary: "selection-summary",
  clearSelection: "clear-selection",
  selectionEmpty: "selection-empty",
  selectionList: "selection-list",
  postState: "post-state",
  postSelection: "post-selection",
  emptyBrowse: "empty-browse",
  navSelectionCount: "nav-selection-count",
  filtersDialog: "filters-dialog",
  filtersForm: "filters-form",
  filterTranslation: "filter-translation",
  filterCase: "filter-case",
  filterDiacritics: "filter-diacritics",
  filterExclude: "filter-exclude",
  filterProximity: "filter-proximity",
  filterBooks: "filter-books",
  clearBookFilters: "clear-book-filters",
  resetFilters: "reset-filters",
  toastRegion: "toast-region",
  announcer: "announcer",
});

attachListeners();
i18n.apply();
void boot();

async function boot() {
  elements.boot.hidden = false;
  elements.accessDenied.hidden = true;
  elements.app.hidden = true;
  elements.bootMessage.textContent = "Opening getBible.Life…";

  if (!bridge.initialize()) {
    showAccessDenied(
      "This scripture experience is available only through the getBible.Life bot.",
    );
    return;
  }

  api = new MiniAppApi(bridge.initData);
  try {
    elements.bootMessage.textContent = "Securing your Telegram session…";
    const storedToken = readSessionToken();
    const payload = storedToken
      ? await api.resumeSession(storedToken)
      : await api.createSession(bridge.launchToken);
    if (!storedToken && typeof payload.session_token === "string") {
      writeSessionToken(payload.session_token);
    }
    const session = normalizeSession(payload);
    if (session.translations.length === 0) {
      throw new ApiError("No Bible translations are currently available.", {
        code: "translations_unavailable",
        retryable: true,
      });
    }

    state.translations = session.translations;
    state.filters = session.preferences.search_defaults;
    state.bible.translation = session.preferences.translation;
    state.basket = session.basket.items;
    populateTranslations();
    syncTranslationControls();
    renderAll();

    elements.boot.hidden = true;
    elements.app.hidden = false;
    loadHeroAsset();
    const entrypoint = session.entrypoint;
    setRoute(entrypoint.route);
    if (entrypoint.query) {
      elements.searchQuery.value = entrypoint.query;
      setRoute("search");
      await runSearch(entrypoint.query);
    }
  } catch (error) {
    const message =
      error instanceof ApiError
        ? error.message
        : "getBible.Life could not verify this Telegram session.";
    showAccessDenied(message);
  }
}

function attachListeners() {
  elements.accessRetry.addEventListener("click", () => window.location.reload());
  window.addEventListener("online", updateConnectionState);
  window.addEventListener("offline", updateConnectionState);

  document.querySelectorAll("[data-route]").forEach((button) => {
    button.addEventListener("click", () => setRoute(button.dataset.route));
  });
  document.querySelectorAll("[data-home-route]").forEach((button) => {
    button.addEventListener("click", () => setRoute(button.dataset.homeRoute));
  });

  elements.searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void runSearch(elements.searchQuery.value);
  });
  elements.clearSearch.addEventListener("click", clearSearch);
  elements.loadMore.addEventListener("click", () => void loadNextSearchPage());
  elements.searchSort.addEventListener("change", () => {
    state.filters = normalizeFilters(
      { ...state.filters, sort: elements.searchSort.value },
      state.filters.translation,
    );
    void savePreferences();
    if (state.search.query) {
      void runSearch(state.search.query);
    }
  });

  elements.openFilters.addEventListener("click", () => void openFilters());
  elements.translationShortcut.addEventListener("click", () => {
    setRoute("search");
    void openFilters();
  });
  elements.filtersForm.addEventListener("submit", (event) => {
    event.preventDefault();
    applyFilters();
  });
  elements.filtersDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeFilters();
  });
  elements.filtersDialog.addEventListener("close", () => {
    filterDraft = null;
    syncBackAction();
  });
  elements.filtersDialog.addEventListener("click", (event) => {
    if (event.target === elements.filtersDialog) {
      closeFilters();
    }
  });
  document.querySelector("[data-close-dialog]").addEventListener("click", closeFilters);
  elements.resetFilters.addEventListener("click", resetFilters);
  elements.clearBookFilters.addEventListener("click", () => {
    elements.filterBooks
      .querySelectorAll('input[type="checkbox"]')
      .forEach((input) => {
        input.checked = false;
      });
  });
  elements.filterTranslation.addEventListener("change", () => {
    filterDraft = normalizeFilters(
      {
        ...(filterDraft ?? state.filters),
        translation: elements.filterTranslation.value,
        books: [],
      },
      state.filters.translation,
    );
    void loadFilterBooks(elements.filterTranslation.value);
  });
  elements.filtersForm.addEventListener("change", (event) => {
    if (event.target.name === "words") {
      const allWords = event.target.value === "all";
      elements.filterProximity.disabled = !allWords;
      if (!allWords) {
        elements.filterProximity.value = "";
      }
    }
  });

  elements.bibleTranslation.addEventListener("change", () => {
    state.bible.translation = elements.bibleTranslation.value;
    state.filters = normalizeFilters(
      { ...state.filters, translation: state.bible.translation, books: [] },
      state.bible.translation,
    );
    syncTranslationControls();
    void savePreferences();
    void loadBibleBooks();
  });
  elements.bibleBook.addEventListener("change", () => void selectBibleBook());
  elements.bibleChapter.addEventListener("change", () => void selectBibleChapter());

  elements.searchResults.addEventListener("click", onVerseCardClick);
  elements.bibleVerses.addEventListener("click", onVerseCardClick);
  elements.selectionList.addEventListener("click", onSelectionAction);
  elements.clearSelection.addEventListener("click", () => void clearBasket());
  elements.postSelection.addEventListener("click", () => void postBasket());
  elements.emptyBrowse.addEventListener("click", () => setRoute("bible"));
}

function loadHeroAsset() {
  const image = new Image();
  image.addEventListener("load", () => elements.homeHero.classList.add("has-image"));
  image.src = new URL("assets/ocean-light-hero.webp", document.baseURI).href;
}

function showAccessDenied(message) {
  elements.boot.hidden = true;
  elements.app.hidden = true;
  elements.accessDenied.hidden = false;
  elements.accessMessage.textContent = message;
  bridge.notifyError();
}

function setRoute(requestedRoute) {
  const route = routeName(requestedRoute);
  state.route = route;
  document.querySelectorAll("[data-view]").forEach((view) => {
    view.hidden = view.dataset.view !== route;
    if (!view.hidden) {
      view.scrollTop = 0;
    }
  });
  document.querySelectorAll("[data-route]").forEach((button) => {
    const active = button.dataset.route === route;
    button.classList.toggle("is-active", active);
    if (active) {
      button.setAttribute("aria-current", "page");
    } else {
      button.removeAttribute("aria-current");
    }
  });
  syncBackAction();
  if (route === "bible" && state.bible.books.length === 0) {
    void loadBibleBooks();
  }
  if (route === "selection") {
    renderSelection();
  }
}

function syncBackAction() {
  if (elements.filtersDialog.open) {
    bridge.setBackAction(closeFilters);
  } else if (state.route !== "home") {
    bridge.setBackAction(() => setRoute("home"));
  } else {
    bridge.setBackAction(null);
  }
}

function populateTranslations() {
  for (const select of [
    elements.filterTranslation,
    elements.bibleTranslation,
  ]) {
    select.replaceChildren();
    for (const translation of state.translations) {
      const option = document.createElement("option");
      option.value = translation.code;
      option.textContent = `${translation.name} (${translation.code.toUpperCase()})`;
      select.append(option);
    }
  }
}

function syncTranslationControls() {
  const code = state.filters.translation;
  const translation = state.translations.find((item) => item.code === code);
  i18n.setLocale(translation?.lang ?? "en", translation?.direction ?? "ltr");
  i18n.apply();
  elements.filterTranslation.value = code;
  elements.bibleTranslation.value = state.bible.translation;
  elements.translationShortcut.textContent = code.toUpperCase();
  elements.translationShortcut.setAttribute(
    "aria-label",
    i18n.t("translation.change_aria", {
      translation: translationName(code),
    }),
  );
  elements.searchSort.value = state.filters.sort;
  const count = activeFilterCount(state.filters);
  elements.filterCount.hidden = count === 0;
  elements.filterCount.textContent = String(count);
}

async function runSearch(rawQuery) {
  const query = rawQuery.trim();
  if (!query) {
    elements.searchQuery.focus();
    announce("Enter words or a phrase to search.");
    return;
  }
  state.search = {
    query,
    status: "loading",
    error: null,
    searchId: null,
    page: 0,
    total: 0,
    hasMore: false,
    results: [],
    loadingMore: false,
  };
  elements.searchQuery.value = query;
  renderSearch();
  try {
    const result = normalizeSearch(await api.search(query, state.filters));
    state.search = {
      ...state.search,
      status: result.results.length === 0 ? "empty" : "ready",
      searchId: result.search_id,
      page: result.page,
      total: result.total,
      hasMore: result.has_more,
      results: result.results,
    };
    announce(
      result.total === 1
        ? "One verse found."
        : `${result.total} verses found.`,
    );
  } catch (error) {
    state.search.status = "error";
    state.search.error = safeError(error);
    handleSessionError(error);
  }
  renderSearch();
}

async function loadNextSearchPage() {
  if (
    state.search.loadingMore ||
    !state.search.hasMore ||
    !state.search.searchId
  ) {
    return;
  }
  state.search.loadingMore = true;
  elements.loadMore.disabled = true;
  elements.loadMore.textContent = "Loading…";
  try {
    const result = normalizeSearchPage(
      await api.searchPage(state.search.searchId, state.search.page + 1),
      state.search.searchId,
    );
    state.search.page = result.page;
    state.search.total = result.total;
    state.search.hasMore = result.has_more;
    state.search.results = uniqueVerses(state.search.results, result.results);
    announce(`${result.results.length} more verses loaded.`);
  } catch (error) {
    toast(safeError(error).message);
    handleSessionError(error);
  } finally {
    state.search.loadingMore = false;
    renderSearch();
  }
}

function clearSearch() {
  state.search = {
    query: "",
    status: "idle",
    error: null,
    searchId: null,
    page: 0,
    total: 0,
    hasMore: false,
    results: [],
    loadingMore: false,
  };
  elements.searchQuery.value = "";
  renderSearch();
  elements.searchQuery.focus();
}

function renderSearch() {
  const search = state.search;
  elements.searchSummary.hidden = !["ready", "empty"].includes(search.status);
  elements.searchResults.replaceChildren();
  elements.searchState.hidden = true;
  elements.loadMore.hidden = true;

  if (search.status === "idle") {
    return;
  }
  if (search.status === "loading") {
    renderSkeletons(elements.searchResults, 4);
    return;
  }
  if (search.status === "error") {
    renderState(elements.searchState, {
      icon: "!",
      title: "Search did not complete",
      message: search.error.message,
      action: search.error.retryable ? "Try again" : null,
      onAction: search.error.retryable
        ? () => void runSearch(search.query)
        : null,
    });
    return;
  }

  elements.searchSummaryTitle.textContent = `“${search.query}”`;
  elements.searchSummaryMeta.textContent =
    `${formatCount(search.total, "verse")} · ` +
    translationName(state.filters.translation);
  if (search.status === "empty") {
    renderState(elements.searchState, {
      icon: "⌕",
      title: "No verses found",
      message: "Try fewer words, a broader scope, or different filters.",
      action: "Change filters",
      onAction: () => void openFilters(),
    });
    return;
  }

  const selected = selectedIds();
  for (const verse of search.results) {
    elements.searchResults.append(createVerseCard(verse, selected));
  }
  elements.loadMore.hidden = !search.hasMore;
  elements.loadMore.disabled = search.loadingMore;
  elements.loadMore.textContent = search.loadingMore ? "Loading…" : "Load more";
}

async function openFilters() {
  filterDraft = normalizeFilters(state.filters, state.filters.translation);
  syncFilterForm(filterDraft);
  elements.filtersDialog.showModal();
  syncBackAction();
  await loadFilterBooks(filterDraft.translation);
}

function closeFilters() {
  if (elements.filtersDialog.open) {
    elements.filtersDialog.close();
  }
}

function syncFilterForm(filters = state.filters) {
  elements.filterTranslation.value = filters.translation;
  setCheckedRadio("words", filters.words);
  setCheckedRadio("match", filters.match);
  setCheckedRadio("scope", filters.scope);
  elements.filterCase.checked = filters.case_sensitive;
  elements.filterDiacritics.checked = filters.diacritics === "insensitive";
  elements.filterExclude.value = filters.exclude.join(" ");
  elements.filterProximity.value =
    filters.proximity === null ? "" : String(filters.proximity);
  elements.filterProximity.disabled = filters.words !== "all";
}

function applyFilters() {
  const data = new FormData(elements.filtersForm);
  const proximityValue = String(data.get("proximity") ?? "").trim();
  state.filters = normalizeFilters(
    {
      translation: data.get("translation"),
      words: data.get("words"),
      match: data.get("match"),
      scope: data.get("scope"),
      case_sensitive: data.has("case_sensitive"),
      diacritics: data.has("ignore_diacritics") ? "insensitive" : "sensitive",
      sort: state.filters.sort,
      books: [...elements.filterBooks.querySelectorAll("input:checked")].map(
        (input) => Number(input.value),
      ),
      exclude: String(data.get("exclude") ?? "").trim().split(/\s+/),
      proximity: proximityValue === "" ? null : Number(proximityValue),
    },
    state.filters.translation,
  );
  state.bible.translation = state.filters.translation;
  syncTranslationControls();
  closeFilters();
  void savePreferences();
  if (state.search.query) {
    void runSearch(state.search.query);
  }
}

function resetFilters() {
  filterDraft = normalizeFilters(
    { ...DEFAULT_FILTERS, translation: state.filters.translation },
    state.filters.translation,
  );
  syncFilterForm(filterDraft);
  renderFilterBooks(
    state.bookCache.get(filterDraft.translation) ?? [],
    filterDraft.books,
  );
}

async function loadFilterBooks(translation) {
  state.filterBooksStatus = "loading";
  elements.filterBooks.replaceChildren(paragraph("Loading books…"));
  try {
    const books = await getBooks(translation);
    state.filterBooksStatus = "ready";
    renderFilterBooks(
      books,
      filterDraft?.translation === translation ? filterDraft.books : [],
    );
  } catch (error) {
    state.filterBooksStatus = "error";
    elements.filterBooks.replaceChildren(paragraph(safeError(error).message));
    handleSessionError(error);
  }
}

function renderFilterBooks(books, selectedBooks = state.filters.books) {
  const selected = new Set(selectedBooks);
  const fragment = document.createDocumentFragment();
  for (const book of books) {
    const label = document.createElement("label");
    label.className = "book-filter__option";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "books";
    input.value = String(book.number);
    input.checked = selected.has(book.number);
    const name = document.createElement("span");
    name.textContent = book.name;
    label.append(input, name);
    fragment.append(label);
  }
  elements.filterBooks.replaceChildren(
    books.length > 0 ? fragment : paragraph("No books are available."),
  );
}

async function savePreferences() {
  try {
    const {
      words,
      match,
      scope,
      case_sensitive,
      diacritics,
      sort,
    } = state.filters;
    const payload = await api.preferences({
      translation: state.filters.translation,
      search_defaults: {
        words,
        match,
        scope,
        case_sensitive,
        diacritics,
        sort,
      },
    });
    if (payload?.preferences) {
      state.filters = normalizeFilters(
        {
          ...state.filters,
          ...payload.preferences.search_defaults,
          translation: payload.preferences.translation,
        },
        state.filters.translation,
      );
      syncTranslationControls();
    }
  } catch (error) {
    toast("Your choice works now, but could not be saved for next time.");
    handleSessionError(error);
  }
}

async function getBooks(translation) {
  if (state.bookCache.has(translation)) {
    return state.bookCache.get(translation);
  }
  const books = normalizeBooks(await api.books(translation));
  state.bookCache.set(translation, books);
  return books;
}

async function loadBibleBooks() {
  state.bible.status = "loading";
  state.bible.error = null;
  state.bible.books = [];
  state.bible.chapters = [];
  state.bible.selectedBook = null;
  state.bible.selectedChapter = null;
  state.bible.verses = [];
  renderBible();
  try {
    state.bible.books = await getBooks(state.bible.translation);
    state.bible.status = "choose_book";
  } catch (error) {
    state.bible.status = "error";
    state.bible.error = safeError(error);
    handleSessionError(error);
  }
  renderBible();
}

async function selectBibleBook() {
  const number = Number(elements.bibleBook.value);
  const book = state.bible.books.find((item) => item.number === number) ?? null;
  state.bible.selectedBook = book;
  state.bible.selectedChapter = null;
  state.bible.chapters = [];
  state.bible.verses = [];
  if (!book) {
    state.bible.status = "choose_book";
    renderBible();
    return;
  }
  state.bible.status = "loading_chapters";
  renderBible();
  try {
    state.bible.chapters = normalizeChapters(
      await api.chapters(state.bible.translation, book.number),
    );
    state.bible.status = "choose_chapter";
  } catch (error) {
    state.bible.status = "error";
    state.bible.error = safeError(error);
    handleSessionError(error);
  }
  renderBible();
}

async function selectBibleChapter() {
  const number = Number(elements.bibleChapter.value);
  const chapter =
    state.bible.chapters.find((item) => item.number === number) ?? null;
  state.bible.selectedChapter = chapter;
  state.bible.verses = [];
  if (!chapter || !state.bible.selectedBook) {
    state.bible.status = "choose_chapter";
    renderBible();
    return;
  }
  state.bible.status = "loading_scripture";
  renderBible();
  try {
    const scripture = normalizeScripture(
      await api.scripture(
        state.bible.translation,
        state.bible.selectedBook.number,
        chapter.number,
      ),
    );
    state.bible.reference =
      scripture.reference ||
      `${state.bible.selectedBook.name} ${chapter.number}`;
    state.bible.verses = scripture.verses;
    state.bible.status = scripture.verses.length === 0 ? "empty" : "ready";
  } catch (error) {
    state.bible.status = "error";
    state.bible.error = safeError(error);
    handleSessionError(error);
  }
  renderBible();
}

function renderBible() {
  const bible = state.bible;
  elements.bibleTranslation.value = bible.translation;
  populateSelect(
    elements.bibleBook,
    bible.books,
    "Choose a book",
    bible.selectedBook?.number,
  );
  elements.bibleBook.disabled = bible.books.length === 0;
  populateSelect(
    elements.bibleChapter,
    bible.chapters,
    "Choose a chapter",
    bible.selectedChapter?.number,
    (chapter) => `Chapter ${chapter.number}`,
  );
  elements.bibleChapter.disabled = bible.chapters.length === 0;
  elements.bibleVerses.replaceChildren();
  elements.bibleState.hidden = true;
  elements.bibleHeading.hidden = true;

  if (["loading", "loading_chapters", "loading_scripture"].includes(bible.status)) {
    renderSkeletons(elements.bibleVerses, bible.status === "loading_scripture" ? 5 : 2);
    return;
  }
  if (bible.status === "error") {
    renderState(elements.bibleState, {
      icon: "!",
      title: "Scripture did not load",
      message: bible.error.message,
      action: bible.error.retryable ? "Try again" : null,
      onAction: bible.error.retryable ? retryBible : null,
    });
    return;
  }
  if (bible.status === "empty") {
    renderState(elements.bibleState, {
      icon: "◌",
      title: "No verses available",
      message: "Choose another chapter or translation.",
    });
    return;
  }
  if (bible.status !== "ready") {
    renderState(elements.bibleState, {
      icon: "▤",
      title:
        bible.status === "choose_chapter" ? "Choose a chapter" : "Choose a book",
      message:
        bible.status === "choose_chapter"
          ? "Select a chapter to read and choose its verses."
          : "Start with any available book in this translation.",
    });
    return;
  }

  elements.bibleHeading.hidden = false;
  elements.bibleTranslationLabel.textContent = translationName(bible.translation);
  elements.bibleReference.textContent = bible.reference;
  elements.bibleVerseCount.textContent = formatCount(bible.verses.length, "verse");
  const selected = selectedIds();
  for (const verse of bible.verses) {
    elements.bibleVerses.append(createVerseCard(verse, selected));
  }
}

function retryBible() {
  if (state.bible.selectedChapter) {
    void selectBibleChapter();
  } else if (state.bible.selectedBook) {
    void selectBibleBook();
  } else {
    void loadBibleBooks();
  }
}

function createVerseCard(verse, selected) {
  const button = document.createElement("button");
  const isSelected = selected.has(verse.selection_id);
  button.type = "button";
  button.className = `verse-card${isSelected ? " is-selected" : ""}`;
  button.dataset.selectionId = verse.selection_id;
  button.setAttribute("aria-pressed", String(isSelected));
  button.setAttribute(
    "aria-label",
    i18n.t(isSelected ? "verse.remove_aria" : "verse.add_aria", {
      reference: verse.reference,
      text: verse.text,
    }),
  );
  button.disabled = state.pendingSelections.has(verse.selection_id);

  const reference = document.createElement("span");
  reference.className = "verse-card__reference";
  reference.append(document.createTextNode(verse.reference));
  const translation = document.createElement("span");
  translation.className = "verse-card__translation";
  translation.textContent = verse.translation.toUpperCase();
  reference.append(translation);

  const text = document.createElement("p");
  text.className = "verse-card__text";
  appendHighlightedText(text, verse.text, verse.highlights);
  button.append(reference, text);
  return button;
}

function appendHighlightedText(container, text, highlights) {
  if (!highlights.length) {
    container.textContent = text;
    return;
  }
  let cursor = 0;
  for (const { start, end } of highlights) {
    container.append(document.createTextNode(text.slice(cursor, start)));
    const mark = document.createElement("mark");
    mark.textContent = text.slice(start, end);
    container.append(mark);
    cursor = end;
  }
  container.append(document.createTextNode(text.slice(cursor)));
}

function onVerseCardClick(event) {
  const card = event.target.closest("[data-selection-id]");
  if (card) {
    void toggleBasketItem(card.dataset.selectionId);
  }
}

async function toggleBasketItem(selectionId) {
  if (state.pendingSelections.has(selectionId)) {
    return;
  }
  state.pendingSelections.add(selectionId);
  refreshSelectionVisuals();
  const removing = selectedIds().has(selectionId);
  try {
    const payload = removing
      ? await api.removeBasketItem(selectionId)
      : await api.addBasketItem(selectionId);
    state.basket = normalizeBasket(payload?.basket ?? payload).items;
    bridge.notifySelection();
    announce(
      removing
        ? "Verse removed from your selection."
        : "Verse added to your selection.",
    );
  } catch (error) {
    toast(safeError(error).message);
    bridge.notifyError();
    handleSessionError(error);
  } finally {
    state.pendingSelections.delete(selectionId);
    refreshSelectionVisuals();
  }
}

function refreshSelectionVisuals() {
  const selected = selectedIds();
  document.querySelectorAll("[data-selection-id]").forEach((card) => {
    const selectionId = card.dataset.selectionId;
    const isSelected = selected.has(selectionId);
    card.classList.toggle("is-selected", isSelected);
    card.setAttribute("aria-pressed", String(isSelected));
    card.disabled = state.pendingSelections.has(selectionId);
  });
  renderBasketStatus();
  renderSelection();
}

function renderAll() {
  updateConnectionState();
  syncTranslationControls();
  renderSearch();
  renderBible();
  renderBasketStatus();
  renderSelection();
}

function renderBasketStatus() {
  const count = state.basket.length;
  elements.navSelectionCount.hidden = count === 0;
  elements.navSelectionCount.textContent = String(Math.min(count, 99));
  elements.homeSelection.hidden = count === 0;
  elements.homeSelectionTitle.textContent =
    count === 1 ? "One verse selected" : `${count} verses selected`;
  elements.homeSelectionMeta.textContent = "Review, reorder, and post together";
  elements.clearSelection.hidden = count === 0;
  bridge.setClosingConfirmation(count > 0);
}

function renderSelection() {
  const items = state.basket;
  elements.selectionEmpty.hidden = items.length > 0;
  elements.selectionList.hidden = items.length === 0;
  elements.postSelection.hidden = items.length === 0;
  elements.postSelection.disabled = items.length === 0 || state.posting;
  elements.postSelection.textContent = state.posting
    ? "Posting…"
    : `Post ${formatCount(items.length, "verse")}`;
  elements.selectionSummary.textContent =
    items.length === 0
      ? "No verses selected yet."
      : `${formatCount(items.length, "verse")} in posting order`;
  elements.selectionList.replaceChildren();

  items.forEach((verse, index) => {
    const item = document.createElement("li");
    item.className = "selection-item";
    item.dataset.itemId = verse.selection_id;

    const number = document.createElement("span");
    number.className = "selection-item__number";
    number.textContent = String(index + 1);

    const copy = document.createElement("div");
    copy.className = "selection-item__copy";
    const reference = document.createElement("strong");
    reference.textContent = `${verse.reference} · ${verse.translation.toUpperCase()}`;
    const text = document.createElement("p");
    text.textContent = verse.text;
    copy.append(reference, text);

    const actions = document.createElement("div");
    actions.className = "selection-item__actions";
    actions.append(
      selectionAction(
        "↑",
        "up",
        i18n.t("selection.move_earlier", { reference: verse.reference }),
        index === 0,
      ),
      selectionAction(
        "↓",
        "down",
        i18n.t("selection.move_later", { reference: verse.reference }),
        index === items.length - 1,
      ),
      selectionAction(
        "×",
        "remove",
        i18n.t("selection.remove_aria", { reference: verse.reference }),
        false,
        true,
      ),
    );
    item.append(number, copy, actions);
    elements.selectionList.append(item);
  });
}

function selectionAction(label, action, ariaLabel, disabled, destructive = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className =
    `selection-action${destructive ? " selection-action--remove" : ""}`;
  button.dataset.selectionAction = action;
  button.setAttribute("aria-label", ariaLabel);
  button.textContent = label;
  button.disabled = disabled;
  return button;
}

async function onSelectionAction(event) {
  const button = event.target.closest("[data-selection-action]");
  const item = event.target.closest("[data-item-id]");
  if (!button || !item) {
    return;
  }
  const index = state.basket.findIndex(
    (verse) => verse.selection_id === item.dataset.itemId,
  );
  if (index < 0) {
    return;
  }
  if (button.dataset.selectionAction === "remove") {
    await toggleBasketItem(item.dataset.itemId);
    return;
  }
  const offset = button.dataset.selectionAction === "up" ? -1 : 1;
  const previous = state.basket;
  state.basket = moveItem(previous, index, offset);
  renderSelection();
  try {
    const payload = await api.reorderBasket(
      state.basket.map((verse) => verse.selection_id),
    );
    state.basket = normalizeBasket(payload?.basket ?? payload).items;
    bridge.notifySelection();
  } catch (error) {
    state.basket = previous;
    toast(safeError(error).message);
    handleSessionError(error);
  }
  renderSelection();
}

async function clearBasket() {
  if (state.basket.length === 0) {
    return;
  }
  const confirmed = await bridge.confirm(
    "Remove every verse from your current selection?",
  );
  if (!confirmed) {
    return;
  }
  try {
    const payload = await api.clearBasket();
    state.basket = normalizeBasket(payload?.basket ?? payload).items;
    refreshSelectionVisuals();
    announce("Selection cleared.");
  } catch (error) {
    toast(safeError(error).message);
    handleSessionError(error);
  }
}

async function postBasket() {
  if (state.posting || state.basket.length === 0) {
    return;
  }
  state.posting = true;
  elements.postState.hidden = true;
  renderSelection();
  try {
    const payload = await api.postSelection(idempotencyKey());
    state.basket = payload?.basket
      ? normalizeBasket(payload.basket).items
      : [];
    bridge.notifySuccess();
    refreshSelectionVisuals();
    toast("Posted to Telegram.");
    window.setTimeout(() => {
      clearSessionToken();
      api.clearSession();
      bridge.close();
    }, 850);
  } catch (error) {
    const safe = safeError(error);
    renderState(elements.postState, {
      icon: "!",
      title: "Your verses were not posted",
      message: safe.message,
      action: safe.retryable ? "Try again" : null,
      onAction: safe.retryable ? () => void postBasket() : null,
    });
    bridge.notifyError();
    handleSessionError(error);
  } finally {
    state.posting = false;
    renderSelection();
  }
}

function renderState(container, { icon, title, message, action, onAction }) {
  container.hidden = false;
  const iconElement = document.createElement("div");
  iconElement.className = "state-panel__icon";
  iconElement.setAttribute("aria-hidden", "true");
  iconElement.textContent = icon;
  const heading = document.createElement("h2");
  heading.textContent = title;
  const copy = document.createElement("p");
  copy.textContent = message;
  const children = [iconElement, heading, copy];
  if (action && onAction) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button button--secondary";
    button.textContent = action;
    button.addEventListener("click", onAction, { once: true });
    children.push(button);
  }
  container.replaceChildren(...children);
}

function renderSkeletons(container, count) {
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < count; index += 1) {
    const skeleton = document.createElement("div");
    skeleton.className = "skeleton-card";
    skeleton.setAttribute("aria-hidden", "true");
    fragment.append(skeleton);
  }
  container.replaceChildren(fragment);
  announce("Loading Scripture.");
}

function populateSelect(select, items, placeholder, selected, label = (item) => item.name) {
  const placeholderOption = document.createElement("option");
  placeholderOption.value = "";
  placeholderOption.textContent = placeholder;
  const fragment = document.createDocumentFragment();
  fragment.append(placeholderOption);
  for (const item of items) {
    const option = document.createElement("option");
    option.value = String(item.number);
    option.textContent = label(item);
    option.selected = item.number === selected;
    fragment.append(option);
  }
  select.replaceChildren(fragment);
}

function selectedIds() {
  return new Set(state.basket.map((verse) => verse.selection_id));
}

function translationName(code) {
  const translation = state.translations.find((item) => item.code === code);
  return translation ? `${translation.name} (${code.toUpperCase()})` : code.toUpperCase();
}

function setCheckedRadio(name, value) {
  const input = elements.filtersForm.querySelector(
    `input[name="${name}"][value="${value}"]`,
  );
  if (input) {
    input.checked = true;
  }
}

function updateConnectionState() {
  elements.offlineBanner.hidden = navigator.onLine;
}

function handleSessionError(error) {
  if (error instanceof ApiError && [401, 403].includes(error.status)) {
    clearSessionToken();
    api?.clearSession();
    showAccessDenied(
      "Your secure session has expired. Reopen getBible.Life from the Telegram bot.",
    );
  }
}

function safeError(error) {
  if (error instanceof ApiError) {
    return {
      message: error.message,
      retryable: error.retryable,
    };
  }
  return {
    message: "getBible.Life could not complete that request.",
    retryable: true,
  };
}

function formatCount(count, singular) {
  return `${count} ${count === 1 ? singular : `${singular}s`}`;
}

function announce(message) {
  elements.announcer.textContent = "";
  window.requestAnimationFrame(() => {
    elements.announcer.textContent = message;
  });
}

let toastTimer = null;
function toast(message) {
  if (toastTimer !== null) {
    window.clearTimeout(toastTimer);
  }
  const item = document.createElement("div");
  item.className = "toast";
  item.textContent = message;
  elements.toastRegion.replaceChildren(item);
  toastTimer = window.setTimeout(() => {
    elements.toastRegion.replaceChildren();
    toastTimer = null;
  }, 3_600);
}

function paragraph(text) {
  const element = document.createElement("p");
  element.textContent = text;
  return element;
}

function idempotencyKey() {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function mapElements(mapping) {
  const result = {};
  for (const [key, id] of Object.entries(mapping)) {
    const element = document.getElementById(id);
    if (!element) {
      throw new Error(`Required Mini App element is missing: ${id}`);
    }
    result[key] = element;
  }
  return result;
}

function readSessionToken() {
  try {
    return window.sessionStorage.getItem(SESSION_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeSessionToken(token) {
  try {
    window.sessionStorage.setItem(SESSION_STORAGE_KEY, token);
  } catch {
    // A blocked storage area only removes reload recovery; the in-memory
    // authenticated session remains valid.
  }
}

function clearSessionToken() {
  try {
    window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // The server remains authoritative even when browser storage is blocked.
  }
}
