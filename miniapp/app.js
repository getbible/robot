import { ApiError, MiniAppApi } from "./lib/api.js";
import { I18n } from "./lib/i18n.js";
import {
  DEFAULT_FILTERS,
  abbreviateBookName,
  activeFilterCount,
  entrypointIntent,
  moveItem,
  nearestChapterVerse,
  normalizeBasket,
  normalizeBooks,
  normalizeChapters,
  normalizeFilters,
  normalizeScripture,
  normalizeSearch,
  normalizeSearchPage,
  normalizeSession,
  normalizeReaderLocation,
  planTranslationChange,
  resolveBibleEntrypoint,
  routeName,
  uniqueBookLabels,
  uniqueVerses,
} from "./lib/model.js";
import { TelegramBridge } from "./lib/telegram.js";
import {
  clearBoundSession,
  openBoundSession,
} from "./lib/session.js";

const bridge = new TelegramBridge();
const i18n = new I18n();
let api = null;
let filterDraft = null;
let accessAction = () => window.location.reload();
let readerPositionTimer = null;
let pendingReaderPosition = null;
let readerPositionSaveTask = null;
let savedReaderPositionKey = "";
let restoreReaderFocusAfterLoad = false;
let preferenceWriteTask = Promise.resolve();
let basketMutationTask = Promise.resolve();
let searchRequestId = 0;
let searchPageRequestId = 0;
let filterBooksRequestId = 0;
let sessionGeneration = 0;
let suppressDialogFocusRestoration = false;
const MAX_BOOK_CACHE_ENTRIES = 8;
const MAX_CHAPTER_CACHE_ENTRIES = 24;

const state = {
  route: "home",
  scrollPositions: new Map(),
  navigationCollapsed: false,
  lastScrollTop: 0,
  translations: [],
  translation: "kjv",
  filters: { ...DEFAULT_FILTERS, books: [], exclude: [] },
  basket: [],
  pendingSelections: new Set(),
  bookCache: new Map(),
  bookRequests: new Map(),
  chapterCache: new Map(),
  chapterRequests: new Map(),
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
    translation: null,
  },
  bible: {
    books: [],
    chapters: [],
    selectedBook: null,
    selectedChapter: null,
    entryReference: "",
    resumeLocation: null,
    reference: "",
    verses: [],
    navigation: { previous: null, next: null },
    targetVerse: 1,
    focusHighlights: [],
    returnToSearch: false,
    pickerStage: "books",
    pickerBook: null,
    pickerFocusBook: null,
    pickerChapters: [],
    pickerStatus: "idle",
    pickerError: null,
    pickerRequestId: 0,
    requestId: 0,
    status: "idle",
    error: null,
  },
  filterBooksStatus: "idle",
  posting: false,
};

const ERROR_MESSAGE_KEYS = Object.freeze({
  authorization_replayed: "error.session_invalid",
  forbidden: "error.forbidden",
  internal_error: "common.request_failed",
  invalid_request: "common.request_failed",
  invalid_response: "error.invalid_response",
  invalid_selection: "error.selection_changed",
  invalid_session_response: "error.session_invalid",
  invalid_session_token: "error.session_invalid",
  method_not_allowed: "error.forbidden",
  network_error: "error.network",
  not_found: "error.not_found",
  post_outcome_locked: "error.post_locked",
  rate_limited: "error.rate_limited",
  request_timeout: "error.timeout",
  request_too_large: "error.request_too_large",
  scripture_request_failed: "common.request_failed",
  scripture_temporarily_unavailable: "error.scripture_unavailable",
  search_not_found: "error.search_expired",
  session_not_ready: "error.session_invalid",
  translations_unavailable: "gate.translations_unavailable",
  unauthorized: "error.session_invalid",
});

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
  translationDialog: "translation-dialog",
  translationSelect: "translation-select",
  translationDetails: "translation-details",
  closeTranslation: "close-translation",
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
  bibleView: "bible-view",
  bibleHeading: "bible-heading",
  biblePrevious: "bible-previous",
  biblePassage: "bible-passage",
  bibleNext: "bible-next",
  bibleTranslationLabel: "bible-translation-label",
  bibleReference: "bible-reference",
  bibleVerseCount: "bible-verse-count",
  bibleSearchReturn: "bible-search-return",
  bibleState: "bible-state",
  bibleVerses: "bible-verses",
  bibleContinue: "bible-continue",
  bibleNavigationDialog: "bible-navigation-dialog",
  bibleNavigationTitle: "bible-navigation-title",
  biblePickerBack: "bible-picker-back",
  closeBibleNavigation: "close-bible-navigation",
  biblePickerContent: "bible-picker-content",
  biblePickerState: "bible-picker-state",
  bibleBookGrid: "bible-book-grid",
  bibleChapterGrid: "bible-chapter-grid",
  selectionSummary: "selection-summary",
  clearSelection: "clear-selection",
  selectionEmpty: "selection-empty",
  selectionList: "selection-list",
  postState: "post-state",
  postSelection: "post-selection",
  emptyBrowse: "empty-browse",
  navSelectionCount: "nav-selection-count",
  bottomNav: "bottom-nav",
  bottomNavHandle: "bottom-nav-handle",
  filtersDialog: "filters-dialog",
  filtersForm: "filters-form",
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
  elements.bootMessage.textContent = i18n.t("gate.opening");
  elements.accessRetry.textContent = i18n.t("gate.retry");
  accessAction = () => window.location.reload();

  if (!bridge.initialize()) {
    showAccessDenied(
      i18n.t("gate.body"),
    );
    return;
  }

  api = new MiniAppApi(bridge.initData);
  try {
    elements.bootMessage.textContent = i18n.t("gate.securing");
    const payload = await openBoundSession(api, {
      initData: bridge.initData,
      launchToken: bridge.launchToken,
    });
    const session = normalizeSession(payload);
    if (session.translations.length === 0) {
      throw new ApiError(i18n.t("gate.translations_unavailable"), {
        code: "translations_unavailable",
        retryable: true,
      });
    }

    state.translations = session.translations;
    state.translation = session.preferences.translation;
    state.filters = normalizeFilters(
      session.preferences.search_defaults,
      state.translation,
    );
    state.bible.resumeLocation = session.preferences.reader_location;
    savedReaderPositionKey = readerLocationKey(state.bible.resumeLocation);
    state.basket = session.basket.items;
    populateTranslations();
    syncTranslationControls();
    renderAll();

    elements.boot.hidden = true;
    elements.app.hidden = false;
    loadHeroAsset();
    const entrypoint = entrypointIntent(session.entrypoint);
    if (entrypoint.bible_reference) {
      state.bible.entryReference = entrypoint.bible_reference;
    }
    setRoute(entrypoint.route);
    if (entrypoint.search_query) {
      elements.searchQuery.value = entrypoint.search_query;
      await runSearch(entrypoint.search_query);
    }
  } catch (error) {
    if (error instanceof ApiError && [401, 409].includes(error.status)) {
      showExpiredAccess();
      return;
    }
    const message =
      error instanceof ApiError
        ? localizedErrorMessage(error)
        : i18n.t("gate.verify_failed");
    showAccessDenied(message);
  }
}

function attachListeners() {
  elements.accessRetry.addEventListener("click", () => accessAction());
  window.addEventListener("online", updateConnectionState);
  window.addEventListener("offline", updateConnectionState);

  document.querySelectorAll("[data-route]").forEach((button) => {
    button.addEventListener("click", () => {
      showNavigation();
      setRoute(button.dataset.route);
    });
  });
  document.querySelectorAll("[data-home-route]").forEach((button) => {
    button.addEventListener("click", () => {
      showNavigation();
      setRoute(button.dataset.homeRoute);
    });
  });
  document.querySelectorAll("[data-view]").forEach((view) => {
    view.addEventListener("scroll", () => onViewScroll(view), { passive: true });
  });
  elements.bottomNavHandle.addEventListener("click", () => {
    setNavigationCollapsed(!state.navigationCollapsed);
  });
  document.addEventListener("pointermove", (event) => {
    if (
      state.route !== "bible" &&
      window.innerHeight - event.clientY < 52
    ) {
      showNavigation();
    }
  }, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && state.route === "bible") {
      persistVisibleReaderPosition();
    }
  });
  window.addEventListener("pagehide", () => {
    if (state.route === "bible") {
      persistVisibleReaderPosition();
    }
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
      state.translation,
    );
    void savePreferences();
    if (state.search.query) {
      void runSearch(state.search.query);
    }
  });

  elements.openFilters.addEventListener("click", () => void openFilters());
  elements.translationShortcut.addEventListener("click", () => {
    openTranslationSelector();
  });
  elements.translationSelect.addEventListener("change", () => {
    void changeTranslation(elements.translationSelect.value);
  });
  elements.translationDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeTranslationSelector();
  });
  elements.translationDialog.addEventListener("close", () => {
    elements.translationSelect.value = state.translation;
    syncTranslationDetails();
    syncBackAction();
    if (!suppressDialogFocusRestoration && !elements.app.hidden) {
      elements.translationShortcut.focus({ preventScroll: true });
    }
  });
  elements.translationDialog.addEventListener("click", (event) => {
    if (event.target === elements.translationDialog) {
      closeTranslationSelector();
    }
  });
  elements.closeTranslation.addEventListener("click", closeTranslationSelector);
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
    syncInterfaceLocale(state.translation);
    renderLocalizedState();
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
  elements.filtersForm.addEventListener("change", (event) => {
    if (event.target.name === "words") {
      const allWords = event.target.value === "all";
      elements.filterProximity.disabled = !allWords;
      if (!allWords) {
        elements.filterProximity.value = "";
      }
    }
  });

  elements.biblePrevious.addEventListener("click", () => {
    void openChapterLocation(state.bible.navigation.previous);
  });
  elements.bibleNext.addEventListener("click", () => {
    void openChapterLocation(state.bible.navigation.next);
  });
  elements.bibleContinue.addEventListener("click", () => {
    void openChapterLocation(state.bible.navigation.next);
  });
  elements.biblePassage.addEventListener("click", showBiblePicker);
  elements.biblePickerBack.addEventListener("click", showBibleBookGrid);
  elements.closeBibleNavigation.addEventListener("click", closeBiblePicker);
  elements.bibleBookGrid.addEventListener("click", (event) => {
    const button = event.target.closest("[data-bible-book]");
    if (button) {
      void chooseBiblePickerBook(Number(button.dataset.bibleBook));
    }
  });
  elements.bibleChapterGrid.addEventListener("click", (event) => {
    const button = event.target.closest("[data-bible-chapter]");
    if (button) {
      void chooseBiblePickerChapter(Number(button.dataset.bibleChapter));
    }
  });
  for (const grid of [elements.bibleBookGrid, elements.bibleChapterGrid]) {
    grid.addEventListener("keydown", (event) => navigateBiblePickerGrid(event, grid));
    grid.addEventListener("focusin", (event) => {
      if (event.target.matches("button")) {
        const scope = event.target.closest(".passage-picker__book-grid") ?? grid;
        setBiblePickerTabStop(scope, event.target);
      }
    });
  }
  elements.bibleNavigationDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeBiblePicker();
  });
  elements.bibleNavigationDialog.addEventListener("close", () => {
    state.bible.pickerRequestId += 1;
    elements.biblePassage.setAttribute("aria-expanded", "false");
    syncBackAction();
    if (
      !suppressDialogFocusRestoration &&
      !elements.app.hidden &&
      !elements.bibleHeading.hidden
    ) {
      elements.biblePassage.focus({ preventScroll: true });
    }
  });
  elements.bibleNavigationDialog.addEventListener("click", (event) => {
    if (event.target === elements.bibleNavigationDialog) {
      closeBiblePicker();
    }
  });
  elements.bibleSearchReturn.addEventListener("click", () => {
    state.bible.returnToSearch = false;
    setRoute("search");
  });

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

function showAccessDenied(
  message,
  {
    actionLabel = i18n.t("common.try_again"),
    onAction = () => window.location.reload(),
  } = {},
) {
  closeOpenDialogs();
  elements.boot.hidden = true;
  elements.app.hidden = true;
  elements.accessDenied.hidden = false;
  elements.accessMessage.textContent = message;
  elements.accessRetry.textContent = actionLabel;
  accessAction = onAction;
  bridge.notifyError();
  window.requestAnimationFrame(() => {
    elements.accessRetry.focus({ preventScroll: true });
  });
}

function showExpiredAccess() {
  showAccessDenied(
    i18n.t("gate.expired"),
    {
      actionLabel: i18n.t("gate.close"),
      onAction: () => bridge.close(),
    },
  );
}

function setRoute(requestedRoute) {
  const route = routeName(requestedRoute);
  const currentView = document.querySelector(`[data-view="${state.route}"]`);
  if (currentView) {
    state.scrollPositions.set(state.route, currentView.scrollTop);
  }
  if (state.route === "bible" && route !== "bible") {
    persistVisibleReaderPosition();
  }
  state.route = route;
  document.querySelectorAll("[data-view]").forEach((view) => {
    view.hidden = view.dataset.view !== route;
    if (!view.hidden) {
      const saved = state.scrollPositions.get(route) ?? 0;
      window.requestAnimationFrame(() => {
        view.scrollTop = saved;
        state.lastScrollTop = saved;
      });
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
  showNavigation();
  syncBackAction();
  if (route === "bible" && state.bible.books.length === 0) {
    void loadBibleBooks();
  } else if (route === "bible") {
    elements.bibleHeading.classList.remove("is-hidden");
  }
  if (
    route === "search" &&
    state.search.query &&
    state.search.translation !== state.translation
  ) {
    void runSearch(state.search.query);
  }
  if (route === "selection") {
    renderSelection();
  }
}

function onViewScroll(view) {
  if (view.hidden || view.dataset.view !== state.route) {
    return;
  }
  const scrollTop = Math.max(0, view.scrollTop);
  const delta = scrollTop - state.lastScrollTop;
  state.scrollPositions.set(state.route, scrollTop);
  if (view === elements.bibleView) {
    if (delta > 10 && scrollTop > 64) {
      setNavigationCollapsed(true);
    }
  } else {
    if (scrollTop < 20 || delta < -8) {
      showNavigation();
    } else if (delta > 10 && scrollTop > 64) {
      setNavigationCollapsed(true);
    }
  }

  if (view === elements.bibleView && state.bible.status === "ready") {
    const hideToolbar = delta > 6 && scrollTop > 58;
    if (hideToolbar) {
      elements.bibleHeading.classList.add("is-hidden");
    } else if (delta < -4 || scrollTop < 24) {
      elements.bibleHeading.classList.remove("is-hidden");
    }
    rememberVisibleReaderPosition();
  }
  state.lastScrollTop = scrollTop;
}

function setNavigationCollapsed(collapsed) {
  state.navigationCollapsed = Boolean(collapsed);
  elements.bottomNav.classList.toggle(
    "is-collapsed",
    state.navigationCollapsed,
  );
  elements.bottomNavHandle.setAttribute(
    "aria-expanded",
    String(!state.navigationCollapsed),
  );
}

function showNavigation() {
  setNavigationCollapsed(false);
}

function rememberVisibleReaderPosition() {
  if (readerPositionTimer !== null) {
    window.clearTimeout(readerPositionTimer);
  }
  readerPositionTimer = window.setTimeout(() => {
    readerPositionTimer = null;
    persistVisibleReaderPosition();
  }, 900);
}

function persistVisibleReaderPosition() {
  if (readerPositionTimer !== null) {
    window.clearTimeout(readerPositionTimer);
    readerPositionTimer = null;
  }
  const location = currentReaderLocation();
  if (!location || elements.bibleView.hidden) {
    return;
  }
  state.bible.targetVerse = location.verse;
  state.bible.resumeLocation = location;
  void saveReaderPosition();
}

function currentReaderLocation() {
  if (
    state.bible.status !== "ready" ||
    !state.bible.selectedBook ||
    !state.bible.selectedChapter
  ) {
    return state.bible.resumeLocation;
  }
  const verses = [...elements.bibleVerses.querySelectorAll("[data-reader-verse]")];
  const visible =
    verses.find((verse) => verse.getBoundingClientRect().bottom > 56) ??
    verses.at(-1);
  const number = Number(visible?.dataset.readerVerse ?? state.bible.targetVerse);
  return {
    translation: state.translation,
    book: state.bible.selectedBook.number,
    chapter: state.bible.selectedChapter.number,
    verse: Number.isInteger(number) ? number : 1,
  };
}

async function saveReaderPosition() {
  if (!state.bible.resumeLocation) {
    return;
  }
  const location = { ...state.bible.resumeLocation };
  const key = readerLocationKey(location);
  if (!key || key === savedReaderPositionKey) {
    return;
  }
  pendingReaderPosition = location;
  if (!readerPositionSaveTask) {
    readerPositionSaveTask = drainReaderPositionSaves().finally(() => {
      readerPositionSaveTask = null;
    });
  }
  await readerPositionSaveTask;
}

async function drainReaderPositionSaves() {
  try {
    while (pendingReaderPosition) {
      const pending = pendingReaderPosition;
      pendingReaderPosition = null;
      const pendingKey = readerLocationKey(pending);
      if (pendingKey === savedReaderPositionKey) {
        continue;
      }
      await enqueuePreferenceWrite({ reader_location: pending });
      savedReaderPositionKey = pendingKey;
    }
  } catch (error) {
    pendingReaderPosition = null;
    handleSessionError(error);
  }
}

function readerLocationKey(location) {
  return location
    ? [
      location.translation,
      location.book,
      location.chapter,
      location.verse,
    ].join(":")
    : "";
}

function syncBackAction() {
  if (elements.bibleNavigationDialog.open) {
    bridge.setBackAction(
      state.bible.pickerStage === "chapters"
        ? showBibleBookGrid
        : closeBiblePicker,
    );
  } else if (elements.translationDialog.open) {
    bridge.setBackAction(closeTranslationSelector);
  } else if (elements.filtersDialog.open) {
    bridge.setBackAction(closeFilters);
  } else if (state.route !== "home") {
    bridge.setBackAction(() => setRoute("home"));
  } else {
    bridge.setBackAction(null);
  }
}

function populateTranslations() {
  elements.translationSelect.replaceChildren();
  for (const translation of state.translations) {
    const option = document.createElement("option");
    option.value = translation.code;
    option.textContent =
      `${translation.name} · ${translation.language} ` +
      `(${translation.code.toUpperCase()})`;
    elements.translationSelect.append(option);
  }
}

function syncTranslationControls() {
  const code = state.translation;
  const translation = state.translations.find((item) => item.code === code);
  state.filters = normalizeFilters(
    { ...state.filters, translation: code },
    code,
  );
  syncInterfaceLocale(code);
  elements.translationSelect.value = code;
  elements.translationShortcut.textContent = code.toUpperCase();
  elements.translationShortcut.setAttribute(
    "aria-label",
    i18n.t("translation.change_aria", {
      translation: translationName(code),
    }),
  );
  elements.closeTranslation.setAttribute(
    "aria-label",
    i18n.t("gate.close").replace(
      "getBible.Life",
      translationName(code),
    ),
  );
  syncTranslationDetails();
  elements.searchSort.value = state.filters.sort;
  const count = activeFilterCount(state.filters);
  elements.filterCount.hidden = count === 0;
  elements.filterCount.textContent = String(count);
}

function syncInterfaceLocale(code) {
  const translation = state.translations.find((item) => item.code === code);
  i18n.setLocale(translation?.lang ?? "en", translation?.direction ?? "ltr");
  i18n.apply();
}

function openTranslationSelector() {
  elements.translationSelect.value = state.translation;
  syncTranslationDetails();
  elements.translationDialog.showModal();
  elements.translationShortcut.setAttribute("aria-expanded", "true");
  syncBackAction();
  window.requestAnimationFrame(() => elements.translationSelect.focus());
}

function closeTranslationSelector() {
  if (elements.translationDialog.open) {
    elements.translationDialog.close();
  }
  elements.translationShortcut.setAttribute("aria-expanded", "false");
}

function syncTranslationDetails() {
  const code = elements.translationSelect.value || state.translation;
  const translation = state.translations.find((item) => item.code === code);
  elements.translationDetails.replaceChildren();
  if (!translation) {
    return;
  }
  const name = document.createElement("strong");
  name.textContent = translation.name;
  const metadata = document.createElement("span");
  metadata.textContent =
    `${translation.language} · ${translation.code.toUpperCase()}`;
  elements.translationDetails.append(name, metadata);
}

async function changeTranslation(value) {
  const generation = sessionGeneration;
  const plan = planTranslationChange(
    value,
    state.translations,
    currentReaderLocation(),
    {
      route: state.route,
      hasSearchQuery: Boolean(state.search.query),
    },
  );
  if (!plan || plan.translation === state.translation) {
    closeTranslationSelector();
    return;
  }

  if (readerPositionTimer !== null) {
    window.clearTimeout(readerPositionTimer);
    readerPositionTimer = null;
  }
  pendingReaderPosition = null;
  state.bible.requestId += 1;
  searchRequestId += 1;
  searchPageRequestId += 1;
  filterBooksRequestId += 1;
  state.translation = plan.translation;
  state.filters = normalizeFilters(
    { ...state.filters, translation: plan.translation, books: [] },
    plan.translation,
  );
  state.bible.resumeLocation = plan.reader_location;
  resetBibleForTranslationChange(plan.reload_reader);
  invalidateSearchForTranslationChange();
  syncTranslationControls();
  renderLocalizedState();
  closeTranslationSelector();
  announce(translationName(plan.translation));

  if (plan.reload_reader) {
    savedReaderPositionKey = "";
    const preferenceWrite = savePreferences(plan.reader_location);
    const readerLoad = loadBibleBooks();
    await Promise.allSettled([preferenceWrite, readerLoad]);
  } else if (plan.reader_location) {
    void resolveAndPersistTranslationLocation(plan, generation);
  } else {
    savedReaderPositionKey = "";
    void savePreferences(null);
  }
  if (plan.rerun_search) {
    await runSearch(state.search.query);
  }
}

async function resolveAndPersistTranslationLocation(plan, generation) {
  const candidate = plan.reader_location;
  if (!candidate) {
    return;
  }
  savedReaderPositionKey = "";
  const preferences = await savePreferences(candidate);
  if (
    !preferences ||
    generation !== sessionGeneration ||
    state.translation !== plan.translation
  ) {
    return;
  }
  state.bible.resumeLocation = preferences.reader_location;
  savedReaderPositionKey = readerLocationKey(preferences.reader_location);
}

function resetBibleForTranslationChange(loading) {
  closeBiblePicker();
  state.bible.books = [];
  state.bible.chapters = [];
  state.bible.selectedBook = null;
  state.bible.selectedChapter = null;
  state.bible.reference = "";
  state.bible.verses = [];
  state.bible.navigation = { previous: null, next: null };
  state.bible.focusHighlights = [];
  state.bible.pickerStage = "books";
  state.bible.pickerBook = null;
  state.bible.pickerFocusBook = null;
  state.bible.pickerChapters = [];
  state.bible.pickerStatus = "idle";
  state.bible.pickerError = null;
  state.bible.pickerRequestId += 1;
  state.bible.status = loading ? "loading" : "idle";
  state.bible.error = null;
}

function invalidateSearchForTranslationChange() {
  state.search.searchId = null;
  state.search.page = 0;
  state.search.total = 0;
  state.search.hasMore = false;
  state.search.results = [];
  state.search.loadingMore = false;
  state.search.translation = null;
  state.search.status = state.search.query ? "idle" : state.search.status;
}

function enqueuePreferenceWrite(payload) {
  const write = preferenceWriteTask
    .catch(() => undefined)
    .then(() => api.preferences(payload));
  preferenceWriteTask = write;
  return write;
}

function renderLocalizedState() {
  renderSearch();
  renderBible();
  renderBasketStatus();
  renderSelection();
}

async function runSearch(rawQuery) {
  const query = rawQuery.trim();
  if (!query) {
    elements.searchQuery.focus();
    announce(i18n.t("search.enter_query"));
    return;
  }
  const requestId = ++searchRequestId;
  const translation = state.translation;
  const filters = normalizeFilters(
    { ...state.filters, translation },
    translation,
  );
  bridge.dismissKeyboard();
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
    translation,
  };
  elements.searchQuery.value = query;
  renderSearch();
  try {
    const result = normalizeSearch(
      await api.search(query, filters),
      translation,
    );
    if (
      requestId !== searchRequestId ||
      state.translation !== translation
    ) {
      return;
    }
    state.search = {
      ...state.search,
      status: result.results.length === 0 ? "empty" : "ready",
      searchId: result.search_id,
      page: result.page,
      total: result.total,
      hasMore: result.has_more,
      results: result.results,
      translation: result.translation,
    };
    announce(i18n.plural("search.found", result.total));
  } catch (error) {
    if (handleSessionError(error)) {
      return;
    }
    if (requestId !== searchRequestId) {
      return;
    }
    state.search.status = "error";
    state.search.error = safeError(error);
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
  const requestId = ++searchPageRequestId;
  const searchId = state.search.searchId;
  const translation = state.search.translation;
  state.search.loadingMore = true;
  elements.loadMore.disabled = true;
  elements.loadMore.textContent = i18n.t("common.loading");
  try {
    const result = normalizeSearchPage(
      await api.searchPage(searchId, state.search.page + 1),
      searchId,
      translation,
    );
    if (
      requestId !== searchPageRequestId ||
      state.search.searchId !== searchId ||
      state.translation !== translation
    ) {
      return;
    }
    state.search.page = result.page;
    state.search.total = result.total;
    state.search.hasMore = result.has_more;
    state.search.results = uniqueVerses(state.search.results, result.results);
    announce(i18n.plural("search.more_loaded", result.results.length));
  } catch (error) {
    if (handleSessionError(error)) {
      return;
    }
    if (requestId !== searchPageRequestId) {
      return;
    }
    toast(safeError(error).message);
  } finally {
    state.search.loadingMore = false;
    renderSearch();
  }
}

function clearSearch() {
  searchRequestId += 1;
  searchPageRequestId += 1;
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
    translation: null,
  };
  elements.searchQuery.value = "";
  renderSearch();
  elements.searchQuery.focus();
}

function renderSearch() {
  const search = state.search;
  elements.searchResults.setAttribute(
    "aria-busy",
    String(search.status === "loading" || search.loadingMore),
  );
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
      title: i18n.t("search.failed"),
      message: search.error.message,
      action: search.error.retryable ? i18n.t("common.try_again") : null,
      onAction: search.error.retryable
        ? () => void runSearch(search.query)
        : null,
    });
    return;
  }

  elements.searchSummaryTitle.textContent = `“${search.query}”`;
  elements.searchSummaryMeta.textContent =
    `${formatVerseCount(search.total)} · ` +
    translationName(search.translation ?? state.translation);
  if (search.status === "empty") {
    renderState(elements.searchState, {
      icon: "⌕",
      title: i18n.t("search.no_results"),
      message: i18n.t("search.no_results_hint"),
      action: i18n.t("search.change_filters"),
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
  elements.loadMore.textContent = search.loadingMore
    ? i18n.t("common.loading")
    : i18n.t("search.load_more");
}

async function openFilters() {
  filterDraft = normalizeFilters(state.filters, state.translation);
  syncFilterForm(filterDraft);
  elements.filtersDialog.showModal();
  syncBackAction();
  await loadFilterBooks(state.translation);
}

function closeFilters() {
  if (elements.filtersDialog.open) {
    elements.filtersDialog.close();
  }
}

function syncFilterForm(filters = state.filters) {
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
      translation: state.translation,
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
    state.translation,
  );
  syncTranslationControls();
  renderLocalizedState();
  closeFilters();
  void savePreferences();
  if (state.search.query) {
    void runSearch(state.search.query);
  }
}

function resetFilters() {
  filterDraft = normalizeFilters(
    { ...DEFAULT_FILTERS, translation: state.translation },
    state.translation,
  );
  syncFilterForm(filterDraft);
  renderFilterBooks(
    state.bookCache.get(filterDraft.translation) ?? [],
    filterDraft.books,
  );
}

async function loadFilterBooks(translation) {
  const requestId = ++filterBooksRequestId;
  state.filterBooksStatus = "loading";
  elements.filterBooks.replaceChildren(paragraph(i18n.t("filters.loading_books")));
  try {
    const books = await getBooks(translation);
    if (
      requestId !== filterBooksRequestId ||
      translation !== state.translation
    ) {
      return;
    }
    state.filterBooksStatus = "ready";
    renderFilterBooks(
      books,
      filterDraft?.translation === translation ? filterDraft.books : [],
    );
  } catch (error) {
    if (handleSessionError(error)) {
      return;
    }
    if (requestId !== filterBooksRequestId) {
      return;
    }
    state.filterBooksStatus = "error";
    elements.filterBooks.replaceChildren(paragraph(safeError(error).message));
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
    books.length > 0 ? fragment : paragraph(i18n.t("filters.no_books")),
  );
}

async function savePreferences(readerLocation = undefined) {
  try {
    const expectedTranslation = state.translation;
    const {
      words,
      match,
      scope,
      case_sensitive,
      diacritics,
      sort,
    } = state.filters;
    const payload = {
      translation: expectedTranslation,
      search_defaults: {
        words,
        match,
        scope,
        case_sensitive,
        diacritics,
        sort,
      },
    };
    if (readerLocation !== undefined) {
      payload.reader_location = readerLocation;
    }
    const response = await enqueuePreferenceWrite(payload);
    const translation = response?.preferences?.translation;
    if (translation !== expectedTranslation) {
      throw new TypeError("Preference response translation did not match.");
    }
    const location = normalizeReaderLocation(
      response.preferences.reader_location,
      translation,
    );
    return {
      translation,
      reader_location:
        location?.translation === translation ? location : null,
    };
  } catch (error) {
    toast(i18n.t("translation.save_failed"));
    handleSessionError(error);
    return false;
  }
}

async function getBooks(translation) {
  const generation = sessionGeneration;
  if (state.bookCache.has(translation)) {
    const books = state.bookCache.get(translation);
    state.bookCache.delete(translation);
    state.bookCache.set(translation, books);
    return books;
  }
  let request = state.bookRequests.get(translation);
  if (!request) {
    request = api.books(translation).then((payload) =>
      normalizeBooks(payload, translation),
    );
    state.bookRequests.set(translation, request);
  }
  let books;
  try {
    books = await request;
  } finally {
    if (state.bookRequests.get(translation) === request) {
      state.bookRequests.delete(translation);
    }
  }
  if (books.length === 0) {
    throw new TypeError("Book response did not contain any valid books.");
  }
  if (generation === sessionGeneration) {
    state.bookCache.set(translation, books);
    trimLru(state.bookCache, MAX_BOOK_CACHE_ENTRIES);
  }
  return books;
}

async function getChapters(translation, book) {
  const generation = sessionGeneration;
  const key = `${translation}:${book}`;
  if (state.chapterCache.has(key)) {
    const chapters = state.chapterCache.get(key);
    state.chapterCache.delete(key);
    state.chapterCache.set(key, chapters);
    return chapters;
  }
  let request = state.chapterRequests.get(key);
  if (!request) {
    request = api.chapters(translation, book).then((payload) =>
      normalizeChapters(payload, { translation, book }),
    );
    state.chapterRequests.set(key, request);
  }
  let chapters;
  try {
    chapters = await request;
  } finally {
    if (state.chapterRequests.get(key) === request) {
      state.chapterRequests.delete(key);
    }
  }
  if (chapters.length === 0) {
    throw new TypeError("Chapter response did not contain any valid chapters.");
  }
  if (generation === sessionGeneration) {
    state.chapterCache.set(key, chapters);
    trimLru(state.chapterCache, MAX_CHAPTER_CACHE_ENTRIES);
  }
  return chapters;
}

function trimLru(cache, maximum) {
  while (cache.size > maximum) {
    cache.delete(cache.keys().next().value);
  }
}

async function loadBibleBooks() {
  const requestId = ++state.bible.requestId;
  state.bible.status = "loading";
  state.bible.error = null;
  state.bible.books = [];
  state.bible.chapters = [];
  state.bible.selectedBook = null;
  state.bible.selectedChapter = null;
  state.bible.verses = [];
  state.bible.navigation = { previous: null, next: null };
  renderBible();
  try {
    const books = await getBooks(state.translation);
    if (requestId !== state.bible.requestId) {
      return;
    }
    state.bible.books = books;
    state.bible.status = "choose_book";
    const entryReference = state.bible.entryReference;
    state.bible.entryReference = "";
    const entrypoint = resolveBibleEntrypoint(
      entryReference,
      state.bible.books,
      state.translations.map((translation) => translation.code),
    );
    if (entrypoint) {
      renderBible();
      await selectBibleBook(entrypoint.book_number, entrypoint.chapter);
      return;
    }
    const resume = state.bible.resumeLocation;
    if (
      resume?.translation === state.translation &&
      state.bible.books.some((book) => book.number === resume.book)
    ) {
      renderBible();
      await selectBibleBook(
        resume.book,
        resume.chapter,
        resume.verse,
        state.bible.focusHighlights,
      );
      return;
    }
  } catch (error) {
    if (handleSessionError(error)) {
      return;
    }
    if (requestId !== state.bible.requestId) {
      return;
    }
    state.bible.status = "error";
    state.bible.error = safeError(error);
  }
  renderBible();
  if (state.bible.status === "error") {
    focusBibleFailure();
  }
  if (
    state.route === "bible" &&
    state.bible.status === "choose_book" &&
    !elements.bibleNavigationDialog.open
  ) {
    showBiblePicker();
  }
}

async function selectBibleBook(
  requestedBookNumber = null,
  requestedChapter = null,
  targetVerse = 1,
  focusHighlights = [],
) {
  persistVisibleReaderPosition();
  const requestId = ++state.bible.requestId;
  const number = Number.isInteger(requestedBookNumber)
    ? requestedBookNumber
    : state.bible.selectedBook?.number;
  const book = state.bible.books.find((item) => item.number === number) ?? null;
  state.bible.selectedBook = book;
  state.bible.selectedChapter = null;
  state.bible.chapters = [];
  state.bible.verses = [];
  state.bible.navigation = { previous: null, next: null };
  if (!book) {
    state.bible.status = "choose_book";
    renderBible();
    return;
  }
  state.bible.status = "loading_chapters";
  renderBible();
  try {
    const chapters = await getChapters(state.translation, book.number);
    if (requestId !== state.bible.requestId) {
      return;
    }
    state.bible.chapters = chapters;
    state.bible.status = "choose_chapter";
    if (
      Number.isInteger(requestedChapter) &&
      state.bible.chapters.some((chapter) => chapter.number === requestedChapter)
    ) {
      renderBible();
      await selectBibleChapter(
        requestedChapter,
        targetVerse,
        focusHighlights,
      );
      return;
    }
  } catch (error) {
    if (handleSessionError(error)) {
      return;
    }
    if (requestId !== state.bible.requestId) {
      return;
    }
    state.bible.status = "error";
    state.bible.error = safeError(error);
  }
  renderBible();
  if (state.bible.status === "error") {
    focusBibleFailure();
  }
}

async function selectBibleChapter(
  requestedChapterNumber = null,
  targetVerse = 1,
  focusHighlights = [],
) {
  persistVisibleReaderPosition();
  const requestId = ++state.bible.requestId;
  const number = Number.isInteger(requestedChapterNumber)
    ? requestedChapterNumber
    : state.bible.selectedChapter?.number;
  const chapter =
    state.bible.chapters.find((item) => item.number === number) ?? null;
  state.bible.selectedChapter = chapter;
  state.bible.verses = [];
  state.bible.navigation = { previous: null, next: null };
  if (!chapter || !state.bible.selectedBook) {
    state.bible.status = "choose_chapter";
    renderBible();
    return;
  }
  state.bible.status = "loading_scripture";
  state.bible.targetVerse = nearestChapterVerse(chapter, targetVerse);
  state.bible.focusHighlights = Array.isArray(focusHighlights)
    ? focusHighlights
    : [];
  elements.bibleView.scrollTop = 0;
  renderBible();
  try {
    const scripture = normalizeScripture(
      await api.scripture(
        state.translation,
        state.bible.selectedBook.number,
        chapter.number,
        state.bible.targetVerse,
      ),
      {
        translation: state.translation,
        book: state.bible.selectedBook.number,
        chapter: chapter.number,
      },
    );
    if (requestId !== state.bible.requestId) {
      return;
    }
    state.bible.reference =
      scripture.reference ||
      `${state.bible.selectedBook.name} ${chapter.number}`;
    state.bible.verses = scripture.verses;
    state.bible.navigation = scripture.navigation;
    state.bible.targetVerse = scripture.target_verse;
    state.bible.resumeLocation = {
      translation: state.translation,
      book: state.bible.selectedBook.number,
      chapter: chapter.number,
      verse: scripture.target_verse,
    };
    state.bible.status = scripture.verses.length === 0 ? "empty" : "ready";
  } catch (error) {
    if (handleSessionError(error)) {
      return;
    }
    if (requestId !== state.bible.requestId) {
      return;
    }
    state.bible.status = "error";
    state.bible.error = safeError(error);
  }
  renderBible();
  const restoreFocus = restoreReaderFocusAfterLoad;
  restoreReaderFocusAfterLoad = false;
  if (state.bible.status === "ready") {
    void saveReaderPosition();
    scrollReaderToVerse(state.bible.targetVerse);
    announce(state.bible.reference);
    if (restoreFocus) {
      window.requestAnimationFrame(() => {
        elements.biblePassage.focus({ preventScroll: true });
      });
    }
  } else if (state.bible.status === "error") {
    focusBibleFailure();
  } else if (restoreFocus) {
    window.requestAnimationFrame(() => {
      const target = elements.bibleState.querySelector("button") ??
        elements.biblePassage;
      target.focus({ preventScroll: true });
    });
  }
}

async function openChapterLocation(location) {
  if (!location) {
    return;
  }
  state.bible.focusHighlights = [];
  if (state.bible.selectedBook?.number === location.book) {
    await selectBibleChapter(location.chapter, 1);
    return;
  }
  await selectBibleBook(location.book, location.chapter, 1);
}

async function openBibleAtVerse(verse, { fromSearch = false } = {}) {
  if (!verse) {
    return;
  }
  state.scrollPositions.set(state.route, currentRouteScrollTop());
  state.bible.returnToSearch = fromSearch;
  state.bible.focusHighlights = fromSearch ? verse.highlights : [];
  state.bible.resumeLocation = {
    translation: verse.translation,
    book: verse.book_number,
    chapter: verse.chapter,
    verse: verse.verse,
  };
  const translationChanged = state.translation !== verse.translation;
  state.bible.requestId += 1;
  state.translation = verse.translation;
  state.filters = normalizeFilters(
    { ...state.filters, translation: verse.translation, books: [] },
    verse.translation,
  );
  void savePreferences(state.bible.resumeLocation);
  syncTranslationControls();
  renderLocalizedState();
  setRoute("bible");
  if (translationChanged || state.bible.books.length === 0) {
    await loadBibleBooks();
    return;
  }
  await selectBibleBook(
    verse.book_number,
    verse.chapter,
    verse.verse,
    state.bible.focusHighlights,
  );
}

function currentRouteScrollTop() {
  const view = document.querySelector(`[data-view="${state.route}"]`);
  return view ? view.scrollTop : 0;
}

function showBiblePicker() {
  if (state.bible.books.length === 0) {
    if (!["loading", "loading_chapters"].includes(state.bible.status)) {
      void loadBibleBooks();
    }
    return;
  }
  state.bible.pickerError = null;
  if (
    state.bible.selectedBook &&
    state.bible.chapters.length > 0
  ) {
    state.bible.pickerStage = "chapters";
    state.bible.pickerBook = state.bible.selectedBook;
    state.bible.pickerFocusBook = state.bible.selectedBook.number;
    state.bible.pickerChapters = state.bible.chapters;
    state.bible.pickerStatus = "ready";
  } else {
    state.bible.pickerStage = "books";
    state.bible.pickerBook = null;
    state.bible.pickerFocusBook = state.bible.selectedBook?.number ?? null;
    state.bible.pickerChapters = [];
    state.bible.pickerStatus = "ready";
  }
  elements.bibleHeading.classList.remove("is-hidden");
  renderBiblePicker();
  if (!elements.bibleNavigationDialog.open) {
    elements.bibleNavigationDialog.showModal();
  }
  elements.biblePassage.setAttribute("aria-expanded", "true");
  syncBackAction();
  announce(i18n.t("bible.navigation"));
  focusBiblePickerChoice();
}

function closeBiblePicker() {
  if (elements.bibleNavigationDialog.open) {
    elements.bibleNavigationDialog.close();
    return;
  }
  state.bible.pickerRequestId += 1;
  elements.biblePassage.setAttribute("aria-expanded", "false");
}

function showBibleBookGrid() {
  state.bible.pickerRequestId += 1;
  state.bible.pickerStage = "books";
  state.bible.pickerStatus = "ready";
  state.bible.pickerError = null;
  renderBiblePicker();
  syncBackAction();
  focusBiblePickerChoice();
}

async function chooseBiblePickerBook(number) {
  const book = state.bible.books.find((item) => item.number === number);
  if (!book) {
    return;
  }
  const requestId = ++state.bible.pickerRequestId;
  const translation = state.translation;
  state.bible.pickerStage = "chapters";
  state.bible.pickerBook = book;
  state.bible.pickerFocusBook = book.number;
  state.bible.pickerChapters = [];
  state.bible.pickerStatus = "loading";
  state.bible.pickerError = null;
  renderBiblePicker();
  syncBackAction();
  window.requestAnimationFrame(() => {
    elements.biblePickerBack.focus({ preventScroll: true });
  });
  try {
    const chapters =
      state.bible.selectedBook?.number === book.number &&
      state.bible.chapters.length > 0
        ? state.bible.chapters
        : await getChapters(translation, book.number);
    if (
      requestId !== state.bible.pickerRequestId ||
      state.translation !== translation ||
      !elements.bibleNavigationDialog.open
    ) {
      return;
    }
    state.bible.pickerChapters = chapters;
    state.bible.pickerStatus = "ready";
  } catch (error) {
    if (handleSessionError(error)) {
      return;
    }
    if (requestId !== state.bible.pickerRequestId) {
      return;
    }
    state.bible.pickerStatus = "error";
    state.bible.pickerError = safeError(error);
  }
  renderBiblePicker();
  focusBiblePickerChoice();
}

async function chooseBiblePickerChapter(number) {
  const book = state.bible.pickerBook;
  const chapter = state.bible.pickerChapters.find(
    (item) => item.number === number,
  );
  if (!book || !chapter) {
    return;
  }
  bridge.notifySelection();
  if (
    state.bible.selectedBook?.number === book.number &&
    state.bible.selectedChapter?.number === chapter.number &&
    state.bible.status === "ready"
  ) {
    closeBiblePicker();
    return;
  }
  restoreReaderFocusAfterLoad = true;
  closeBiblePicker();
  if (state.bible.selectedBook?.number === book.number) {
    await selectBibleChapter(chapter.number, 1);
    return;
  }
  await selectBibleBook(book.number, chapter.number, 1);
}

function renderBiblePicker() {
  const bible = state.bible;
  const chaptersVisible = bible.pickerStage === "chapters";
  const navigationLabel = i18n.t("bible.navigation");
  const chapterAction = i18n.t("bible.choose_chapter");
  elements.biblePickerBack.hidden = !chaptersVisible;
  elements.biblePickerBack.setAttribute(
    "aria-label",
    i18n.t("bible.choose_book"),
  );
  elements.closeBibleNavigation.setAttribute(
    "aria-label",
    localizedCloseLabel(navigationLabel),
  );
  elements.bibleNavigationTitle.textContent = chaptersVisible
    ? `${bible.pickerBook?.name ?? i18n.t("bible.book")} · ${chapterAction}`
    : i18n.t("bible.choose_book");
  elements.bibleBookGrid.hidden = chaptersVisible;
  elements.bibleChapterGrid.hidden = !chaptersVisible;
  elements.biblePickerState.hidden = true;
  elements.biblePickerState.replaceChildren();
  elements.bibleBookGrid.replaceChildren();
  elements.bibleChapterGrid.replaceChildren();

  if (bible.pickerStatus === "loading") {
    elements.biblePickerState.hidden = false;
    const spinner = document.createElement("div");
    spinner.className = "spinner";
    spinner.setAttribute("aria-hidden", "true");
    const message = paragraph(i18n.t("common.loading"));
    elements.biblePickerState.append(spinner, message);
    return;
  }
  if (bible.pickerStatus === "error") {
    elements.biblePickerState.hidden = false;
    const message = paragraph(
      bible.pickerError?.message ?? i18n.t("common.request_failed"),
    );
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "button button--secondary";
    retry.textContent = i18n.t("common.try_again");
    retry.addEventListener(
      "click",
      () => void chooseBiblePickerBook(bible.pickerBook?.number),
      { once: true },
    );
    elements.biblePickerState.append(message, retry);
    window.requestAnimationFrame(() => retry.focus({ preventScroll: true }));
    return;
  }
  if (chaptersVisible) {
    renderBibleChapterGrid();
  } else {
    renderBibleBookGrid();
  }
}

function renderBibleBookGrid() {
  const groups = [
    ["old", i18n.t("filters.old")],
    ["new", i18n.t("filters.new")],
    ["other", i18n.t("filters.other")],
  ];
  const labels = uniqueBookLabels(state.bible.books);
  const labelsByNumber = new Map(
    state.bible.books.map((book, index) => [book.number, labels[index]]),
  );
  const fragment = document.createDocumentFragment();
  for (const [testament, label] of groups) {
    const books = state.bible.books.filter(
      (book) => bookTestament(book) === testament,
    );
    if (books.length === 0) {
      continue;
    }
    const section = document.createElement("section");
    section.className = "passage-picker__book-group";
    const heading = document.createElement("h3");
    heading.textContent = label;
    const grid = document.createElement("div");
    grid.className = "passage-picker__book-grid";
    for (const book of books) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "passage-picker__book";
      button.dataset.bibleBook = String(book.number);
      button.textContent = labelsByNumber.get(book.number) ||
        abbreviateBookName(book.name);
      button.setAttribute("aria-label", book.name);
      button.title = book.name;
      if (book.number === state.bible.selectedBook?.number) {
        button.classList.add("is-active");
        button.setAttribute("aria-current", "true");
      }
      grid.append(button);
    }
    section.append(heading, grid);
    fragment.append(section);
  }
  elements.bibleBookGrid.append(fragment);
  for (const grid of elements.bibleBookGrid.querySelectorAll(
    ".passage-picker__book-grid",
  )) {
    setBiblePickerTabStop(
      grid,
      grid.querySelector(
        `[data-bible-book="${state.bible.pickerFocusBook ?? ""}"]`,
      ) ??
        grid.querySelector('[aria-current="true"]') ??
        grid.querySelector("button"),
    );
  }
}

function renderBibleChapterGrid() {
  const fragment = document.createDocumentFragment();
  for (const chapter of state.bible.pickerChapters) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "passage-picker__chapter";
    button.dataset.bibleChapter = String(chapter.number);
    button.textContent = String(chapter.number);
    button.setAttribute(
      "aria-label",
      i18n.t("bible.chapter_number", { number: chapter.number }),
    );
    if (
      state.bible.pickerBook?.number === state.bible.selectedBook?.number &&
      chapter.number === state.bible.selectedChapter?.number
    ) {
      button.classList.add("is-active");
      button.setAttribute("aria-current", "true");
    }
    fragment.append(button);
  }
  elements.bibleChapterGrid.append(fragment);
  setBiblePickerTabStop(
    elements.bibleChapterGrid,
    elements.bibleChapterGrid.querySelector('[aria-current="true"]') ??
      elements.bibleChapterGrid.querySelector("button"),
  );
}

function bookTestament(book) {
  if (["old", "new", "other"].includes(book.testament)) {
    return book.testament;
  }
  if (book.number <= 39) {
    return "old";
  }
  if (book.number <= 66) {
    return "new";
  }
  return "other";
}

function setBiblePickerTabStop(container, target) {
  for (const button of container.querySelectorAll("button")) {
    button.tabIndex = button === target ? 0 : -1;
  }
}

function navigateBiblePickerGrid(event, container) {
  const current = event.target.closest("button");
  const scope = current?.closest(".passage-picker__book-grid") ?? container;
  const buttons = [...scope.querySelectorAll("button")];
  const index = buttons.indexOf(current);
  if (index < 0 || buttons.length === 0) {
    return;
  }
  const columns = Math.max(
    1,
    getComputedStyle(
      scope,
    ).gridTemplateColumns.split(/\s+/u).filter(Boolean).length,
  );
  const rtl = document.documentElement.dir === "rtl";
  const movements = {
    ArrowLeft: rtl ? 1 : -1,
    ArrowRight: rtl ? -1 : 1,
    ArrowUp: -columns,
    ArrowDown: columns,
    Home: -index,
    End: buttons.length - index - 1,
  };
  if (!Object.hasOwn(movements, event.key)) {
    return;
  }
  event.preventDefault();
  const targetIndex = Math.max(
    0,
    Math.min(buttons.length - 1, index + movements[event.key]),
  );
  const target = buttons[targetIndex];
  setBiblePickerTabStop(scope, target);
  target.focus({ preventScroll: true });
  target.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function focusBiblePickerChoice() {
  window.requestAnimationFrame(() => {
    const container =
      state.bible.pickerStage === "chapters"
        ? elements.bibleChapterGrid
        : elements.bibleBookGrid;
    const target =
      (
        state.bible.pickerStage === "books"
          ? container.querySelector(
            `[data-bible-book="${state.bible.pickerFocusBook ?? ""}"]`,
          )
          : null
      ) ??
      container.querySelector('[aria-current="true"]') ??
      container.querySelector("button");
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
}

function localizedCloseLabel(subject) {
  const template = i18n.t("gate.close");
  return template.includes("getBible.Life")
    ? template.replace("getBible.Life", subject)
    : `${template} · ${subject}`;
}

function scrollReaderToVerse(number) {
  window.requestAnimationFrame(() => {
    const verse = elements.bibleVerses.querySelector(
      `[data-reader-verse="${number}"]`,
    );
    if (!verse) {
      return;
    }
    verse.scrollIntoView({ block: "start", behavior: "auto" });
    elements.bibleView.scrollTop = Math.max(0, elements.bibleView.scrollTop - 48);
    state.scrollPositions.set("bible", elements.bibleView.scrollTop);
  });
}

function renderBible() {
  const bible = state.bible;
  elements.biblePassage.setAttribute(
    "aria-expanded",
    String(elements.bibleNavigationDialog.open),
  );
  if (elements.bibleNavigationDialog.open) {
    renderBiblePicker();
  }
  elements.bibleVerses.setAttribute(
    "aria-busy",
    String(
      ["loading", "loading_chapters", "loading_scripture"].includes(
        bible.status,
      ),
    ),
  );
  elements.bibleVerses.replaceChildren();
  elements.bibleState.hidden = true;
  elements.bibleHeading.hidden = true;
  elements.bibleSearchReturn.hidden = true;
  elements.bibleContinue.hidden = true;

  if (["loading", "loading_chapters", "loading_scripture"].includes(bible.status)) {
    if (bible.books.length > 0) {
      renderBibleToolbar(
        bible.selectedBook && bible.selectedChapter
          ? `${bible.selectedBook.name} ${bible.selectedChapter.number}`
          : i18n.t("bible.title"),
      );
    }
    renderSkeletons(elements.bibleVerses, bible.status === "loading_scripture" ? 5 : 2);
    return;
  }
  if (bible.status === "error") {
    if (bible.books.length > 0) {
      renderBibleToolbar(bible.reference || i18n.t("bible.title"));
    }
    renderState(elements.bibleState, {
      icon: "!",
      title: i18n.t("bible.load_failed"),
      message: bible.error.message,
      action: bible.error.retryable ? i18n.t("common.try_again") : null,
      onAction: bible.error.retryable ? retryBible : null,
    });
    return;
  }
  if (bible.status === "empty") {
    if (bible.books.length > 0) {
      renderBibleToolbar(bible.reference || i18n.t("bible.title"));
    }
    renderState(elements.bibleState, {
      icon: "◌",
      title: i18n.t("bible.no_verses"),
      message: i18n.t("bible.no_verses_hint"),
    });
    return;
  }
  if (bible.status !== "ready") {
    if (bible.books.length > 0) {
      renderBibleToolbar(
        bible.selectedBook?.name ?? i18n.t("bible.title"),
      );
    }
    renderState(elements.bibleState, {
      icon: "▤",
      title:
        bible.status === "choose_chapter"
          ? i18n.t("bible.choose_chapter")
          : i18n.t("bible.choose_book"),
      message:
        bible.status === "choose_chapter"
          ? i18n.t("bible.choose_chapter_hint")
          : i18n.t("bible.choose_book_hint"),
      action: bible.books.length > 0 ? i18n.t("bible.navigation") : null,
      onAction: bible.books.length > 0 ? showBiblePicker : null,
    });
    return;
  }

  renderBibleToolbar(bible.reference, formatVerseCount(bible.verses.length));
  elements.bibleSearchReturn.hidden = !bible.returnToSearch;
  const selected = selectedIds();
  bible.verses.forEach((verse, index) => {
    elements.bibleVerses.append(
      createReaderVerse(
        verse,
        selected,
        bible.verses[index - 1] ?? null,
        bible.verses[index + 1] ?? null,
      ),
    );
  });
  if (bible.navigation.next) {
    elements.bibleContinue.hidden = false;
    elements.bibleContinue.textContent =
      `› ${bible.navigation.next.book_name} ${bible.navigation.next.chapter}`;
  }
}

function renderBibleToolbar(reference, verseCount = "") {
  const bible = state.bible;
  elements.bibleHeading.hidden = false;
  elements.bibleHeading.classList.remove("is-hidden");
  elements.bibleTranslationLabel.textContent = translationName(state.translation);
  elements.bibleReference.textContent = reference;
  elements.bibleVerseCount.textContent = verseCount;
  elements.biblePassage.setAttribute(
    "aria-label",
    `${i18n.t("bible.navigation")}: ${reference}`,
  );
  elements.biblePrevious.disabled = !bible.navigation.previous;
  elements.bibleNext.disabled = !bible.navigation.next;
  elements.biblePrevious.setAttribute(
    "aria-label",
    chapterNavigationLabel(bible.navigation.previous),
  );
  elements.bibleNext.setAttribute(
    "aria-label",
    chapterNavigationLabel(bible.navigation.next),
  );
}

function chapterNavigationLabel(location) {
  return location
    ? `${location.book_name} ${i18n.t("bible.chapter_number", {
      number: location.chapter,
    })}`
    : i18n.t("bible.chapter");
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
  const result = document.createElement("article");
  result.className = "verse-result";
  const context = document.createElement("button");
  context.type = "button";
  context.className = "verse-context";
  context.dataset.openBible = verse.selection_id;
  context.setAttribute(
    "aria-label",
    `${i18n.t("nav.bible")}: ${verse.reference}`,
  );
  context.textContent = `${i18n.t("nav.bible")} ›`;
  result.append(button, context);
  return result;
}

function createReaderVerse(verse, selected, previous, following) {
  const button = document.createElement("button");
  const isSelected = selected.has(verse.selection_id);
  button.type = "button";
  button.className = `reader-verse${isSelected ? " is-selected" : ""}`;
  button.dataset.selectionId = verse.selection_id;
  button.dataset.readerVerse = String(verse.verse);
  button.setAttribute("aria-pressed", String(isSelected));
  button.setAttribute(
    "aria-label",
    i18n.t(isSelected ? "verse.remove_aria" : "verse.add_aria", {
      reference: verse.reference,
      text: verse.text,
    }),
  );
  button.disabled = state.pendingSelections.has(verse.selection_id);
  if (verse.verse === state.bible.targetVerse && state.bible.focusHighlights.length) {
    button.classList.add("is-focused");
  }
  if (
    isSelected &&
    (!previous || !selected.has(previous.selection_id))
  ) {
    button.classList.add("is-selection-start");
  }
  if (
    isSelected &&
    (!following || !selected.has(following.selection_id))
  ) {
    button.classList.add("is-selection-end");
  }

  const number = document.createElement("span");
  number.className = "reader-verse__number";
  number.textContent = String(verse.verse);
  const text = document.createElement("span");
  text.className = "reader-verse__text";
  appendHighlightedText(
    text,
    verse.text,
    verse.verse === state.bible.targetVerse
      ? state.bible.focusHighlights
      : [],
  );
  button.append(number, text);
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
  const context = event.target.closest("[data-open-bible]");
  if (context) {
    const verse = findVerse(context.dataset.openBible);
    void openBibleAtVerse(verse, {
      fromSearch: Boolean(elements.searchResults.contains(context)),
    });
    return;
  }
  const card = event.target.closest("[data-selection-id]");
  if (card) {
    if (state.route === "bible") {
      showNavigation();
    }
    void toggleBasketItem(card.dataset.selectionId);
  }
}

function findVerse(selectionId) {
  return [
    ...state.search.results,
    ...state.bible.verses,
    ...state.basket,
  ].find((verse) => verse.selection_id === selectionId) ?? null;
}

async function toggleBasketItem(selectionId) {
  if (state.posting || state.pendingSelections.has(selectionId)) {
    return;
  }
  state.pendingSelections.add(selectionId);
  refreshSelectionVisuals();
  await enqueueBasketMutation(async (generation) => {
    const removing = selectedIds().has(selectionId);
    try {
      const payload = removing
        ? await api.removeBasketItem(selectionId)
        : await api.addBasketItem(selectionId);
      if (generation !== sessionGeneration) {
        return;
      }
      state.basket = normalizeBasket(payload?.basket ?? payload).items;
      bridge.notifySelection();
      announce(
        removing
          ? i18n.t("selection.verse_removed")
          : i18n.t("selection.verse_added"),
      );
    } catch (error) {
      if (generation !== sessionGeneration) {
        return;
      }
      toast(safeError(error).message);
      bridge.notifyError();
      handleSessionError(error);
    } finally {
      if (generation === sessionGeneration) {
        state.pendingSelections.delete(selectionId);
        refreshSelectionVisuals();
      }
    }
  });
}

function refreshSelectionVisuals() {
  const selected = selectedIds();
  document.querySelectorAll("[data-selection-id]").forEach((card) => {
    const selectionId = card.dataset.selectionId;
    const isSelected = selected.has(selectionId);
    card.classList.toggle("is-selected", isSelected);
    card.setAttribute("aria-pressed", String(isSelected));
    card.disabled =
      state.posting || state.pendingSelections.has(selectionId);
  });
  if (state.bible.status === "ready") {
    renderBible();
  }
  renderBasketStatus();
  renderSelection();
}

function renderAll() {
  updateConnectionState();
  syncTranslationControls();
  renderLocalizedState();
}

function renderBasketStatus() {
  const count = state.basket.length;
  elements.navSelectionCount.hidden = count === 0;
  elements.navSelectionCount.textContent = String(Math.min(count, 99));
  elements.homeSelection.hidden = count === 0;
  elements.homeSelectionTitle.textContent = i18n.plural(
    "home.selection",
    count,
  );
  elements.homeSelectionMeta.textContent = i18n.t("home.selection_hint");
  elements.clearSelection.hidden = count === 0;
  elements.clearSelection.disabled = state.posting;
  bridge.setClosingConfirmation(count > 0);
}

function renderSelection() {
  const items = state.basket;
  elements.selectionEmpty.hidden = items.length > 0;
  elements.selectionList.hidden = items.length === 0;
  elements.postSelection.hidden = items.length === 0;
  elements.postSelection.disabled = items.length === 0 || state.posting;
  elements.postSelection.textContent = state.posting
    ? i18n.t("selection.posting")
    : i18n.plural("selection.post", items.length);
  elements.selectionSummary.textContent =
    items.length === 0
      ? i18n.t("selection.none")
      : i18n.plural("selection.order", items.length);
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
        "▤",
        "bible",
        `${i18n.t("nav.bible")}: ${verse.reference}`,
        false,
      ),
      selectionAction(
        "↑",
        "up",
        i18n.t("selection.move_earlier", { reference: verse.reference }),
        state.posting || index === 0,
      ),
      selectionAction(
        "↓",
        "down",
        i18n.t("selection.move_later", { reference: verse.reference }),
        state.posting || index === items.length - 1,
      ),
      selectionAction(
        "×",
        "remove",
        i18n.t("selection.remove_aria", { reference: verse.reference }),
        state.posting,
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
  if (button.dataset.selectionAction === "bible") {
    await openBibleAtVerse(state.basket[index]);
    return;
  }
  const offset = button.dataset.selectionAction === "up" ? -1 : 1;
  await enqueueBasketMutation(async (generation) => {
    const currentIndex = state.basket.findIndex(
      (verse) => verse.selection_id === item.dataset.itemId,
    );
    if (currentIndex < 0) {
      return;
    }
    const previous = state.basket;
    state.basket = moveItem(previous, currentIndex, offset);
    renderSelection();
    try {
      const payload = await api.reorderBasket(
        state.basket.map((verse) => verse.selection_id),
      );
      if (generation !== sessionGeneration) {
        return;
      }
      state.basket = normalizeBasket(payload?.basket ?? payload).items;
      bridge.notifySelection();
    } catch (error) {
      if (generation !== sessionGeneration) {
        return;
      }
      state.basket = previous;
      toast(safeError(error).message);
      handleSessionError(error);
    }
    if (generation === sessionGeneration) {
      renderSelection();
    }
  });
}

async function clearBasket() {
  if (state.posting || state.basket.length === 0) {
    return;
  }
  const generation = sessionGeneration;
  const confirmed = await bridge.confirm(
    i18n.t("selection.clear_confirm"),
  );
  if (
    !confirmed ||
    generation !== sessionGeneration ||
    state.posting
  ) {
    return;
  }
  await enqueueBasketMutation(async (activeGeneration) => {
    try {
      const payload = await api.clearBasket();
      if (activeGeneration !== sessionGeneration) {
        return;
      }
      state.basket = normalizeBasket(payload?.basket ?? payload).items;
      refreshSelectionVisuals();
      announce(i18n.t("selection.cleared"));
    } catch (error) {
      if (activeGeneration !== sessionGeneration) {
        return;
      }
      toast(safeError(error).message);
      handleSessionError(error);
    }
  });
}

async function postBasket() {
  if (state.posting || state.basket.length === 0) {
    return;
  }
  state.posting = true;
  elements.postState.hidden = true;
  refreshSelectionVisuals();
  await enqueueBasketMutation(async (generation) => {
    if (state.basket.length === 0) {
      state.posting = false;
      refreshSelectionVisuals();
      return;
    }
    try {
      const payload = await api.postSelection(idempotencyKey());
      if (generation !== sessionGeneration) {
        return;
      }
      state.basket = payload?.basket
        ? normalizeBasket(payload.basket).items
        : [];
      bridge.notifySuccess();
      refreshSelectionVisuals();
      toast(i18n.t("selection.posted"));
      window.setTimeout(() => {
        if (generation !== sessionGeneration) {
          return;
        }
        clearBoundSession();
        api.clearSession();
        bridge.close();
      }, 850);
    } catch (error) {
      if (generation !== sessionGeneration) {
        return;
      }
      const safe = safeError(error);
      renderState(elements.postState, {
        icon: "!",
        title: i18n.t("selection.post_failed"),
        message: safe.message,
        action: safe.retryable ? i18n.t("common.try_again") : null,
        onAction: safe.retryable ? () => void postBasket() : null,
      });
      bridge.notifyError();
      handleSessionError(error);
    } finally {
      if (generation === sessionGeneration) {
        state.posting = false;
        renderSelection();
      }
    }
  });
}

function renderState(container, { icon, title, message, action, onAction }) {
  container.hidden = false;
  const iconElement = document.createElement("div");
  iconElement.className = "state-panel__icon";
  iconElement.setAttribute("aria-hidden", "true");
  iconElement.textContent = icon;
  const heading = document.createElement("h2");
  heading.tabIndex = -1;
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

function focusBibleFailure() {
  if (
    state.route !== "bible" ||
    state.bible.status !== "error" ||
    elements.app.hidden
  ) {
    return;
  }
  window.requestAnimationFrame(() => {
    if (
      state.route !== "bible" ||
      state.bible.status !== "error" ||
      elements.app.hidden
    ) {
      return;
    }
    const target = elements.bibleState.querySelector("button") ??
      elements.bibleState.querySelector("h2");
    target?.focus({ preventScroll: true });
  });
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
  announce(i18n.t("common.loading_scripture"));
}

function selectedIds() {
  return new Set(state.basket.map((verse) => verse.selection_id));
}

function enqueueBasketMutation(operation) {
  const generation = sessionGeneration;
  const task = basketMutationTask
    .catch(() => undefined)
    .then(async () => {
      if (generation !== sessionGeneration) {
        return;
      }
      await operation(generation);
    });
  basketMutationTask = task.catch(() => undefined);
  return task;
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
    invalidateClientSessionState();
    clearBoundSession();
    api?.clearSession();
    showExpiredAccess();
    return true;
  }
  return false;
}

function invalidateClientSessionState() {
  sessionGeneration += 1;
  basketMutationTask = Promise.resolve();
  searchRequestId += 1;
  searchPageRequestId += 1;
  filterBooksRequestId += 1;
  state.bible.requestId += 1;
  state.bible.pickerRequestId += 1;
  if (readerPositionTimer !== null) {
    window.clearTimeout(readerPositionTimer);
    readerPositionTimer = null;
  }
  pendingReaderPosition = null;
  restoreReaderFocusAfterLoad = false;
  state.pendingSelections.clear();
  state.basket = [];
  state.posting = false;
  state.search.results = [];
  state.bible.verses = [];
  state.bookCache.clear();
  state.bookRequests.clear();
  state.chapterCache.clear();
  state.chapterRequests.clear();
  suppressDialogFocusRestoration = true;
  closeOpenDialogs();
  suppressDialogFocusRestoration = false;
  bridge.setBackAction(null);
  bridge.setClosingConfirmation(false);
}

function closeOpenDialogs() {
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
}

function safeError(error) {
  if (error instanceof ApiError) {
    return {
      message: localizedErrorMessage(error),
      retryable: error.retryable,
    };
  }
  return {
    message: i18n.t("common.request_failed"),
    retryable: true,
  };
}

function localizedErrorMessage(error) {
  return i18n.t(ERROR_MESSAGE_KEYS[error.code] ?? "common.request_failed");
}

function formatVerseCount(count) {
  return i18n.plural("verse.count", count);
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
