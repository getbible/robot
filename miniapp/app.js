import { ApiError, MiniAppApi } from "./lib/api.js";
import {
  ClipboardController,
  clipboardMessage,
} from "./lib/clipboard.js";
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
import { LatestRequestCoordinator } from "./lib/request-coordinator.js";
import { ReadingHistoryStore } from "./lib/reading-history-store.js";
import {
  SCRIPTURE_EXCERPT_MAXIMUM_CONCURRENCY,
  ScriptureExcerptResolver,
} from "./lib/scripture-excerpt-resolver.js";
import {
  BOOKMARK_BACKUP_MAX_BYTES,
  BOOKMARK_TOPIC_COLORS,
  BookmarkBackupError,
  BookmarkStore,
  bookmarkStorageScope,
} from "./lib/bookmark-store.js";
import { sortBookmarkTopics } from "./lib/bookmark-topic-sort.js";
import {
  ContributionSync,
  isEnglishContributionTopicName,
} from "./lib/contribution-sync.js";
import {
  GLOBAL_BOOKMARK_CATALOG,
  GLOBAL_BOOKMARK_SOURCE,
} from "./lib/global-bookmark-catalog.js";
import { GlobalBookmarkDeviceStorage } from "./lib/global-bookmark-device-storage.js";
import { loadLiveGlobalBookmarkCatalog } from "./lib/global-bookmark-live-catalog.js";
import { GlobalBookmarkPreferences } from "./lib/global-bookmark-preferences.js";
import { miniAppInstanceScope } from "./lib/instance-scope.js";
import { restoreBookmarkBackup } from "./lib/bookmark-restore.js";
import { TelegramBookmarkStorage } from "./lib/telegram-bookmark-storage.js";
import { TelegramBridge } from "./lib/telegram.js";
import {
  clearBoundSession,
  openBoundSession,
} from "./lib/session.js";

const bridge = new TelegramBridge();
const i18n = new I18n();
const instanceScope = miniAppInstanceScope(document.baseURI);
let readingHistory = null;
let bookmarkStore = null;
let bookmarkStorage = null;
let bookmarkStorageScopeValue = null;
let globalBookmarkDeviceStorage = null;
let globalBookmarkPreferences = null;
let globalBookmarkCatalog = GLOBAL_BOOKMARK_CATALOG;
let globalBookmarkCatalogChecksum = null;
let contributionSync = null;
let contributionSyncTask = null;
let contributionRetryTimer = null;
let contributionRetryDelayMs = 2_000;
// The default server refill is one contribution request per five seconds.
// Keep a margin so long baselines do not oscillate into the abuse backoff.
let contributionBatchDelayMs = 5_250;
let api = null;
let scriptureExcerpts = null;
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
let filterBooksRequestId = 0;
let sessionGeneration = 0;
let suppressDialogFocusRestoration = false;
let bookmarkDownloadUrl = null;
let bookmarkBackupTask = null;
let globalBookmarkTask = null;
let lastReadRevision = 0;
let historyExcerptController = null;
let bookmarkExcerptController = null;
const searchPageRequests = new LatestRequestCoordinator();
const MAX_BOOK_CACHE_ENTRIES = 8;
const MAX_CHAPTER_CACHE_ENTRIES = 24;
const EXCERPT_HYDRATION_BATCH_SIZE =
  SCRIPTURE_EXCERPT_MAXIMUM_CONCURRENCY;
const ICON_ONLY_ROUTES = new Set([
  "home",
  "history",
  "selection",
  "bookmarks",
]);

class ContributorTopicLanguageError extends Error {}

const state = {
  route: "home",
  scrollPositions: new Map(),
  headerCondensed: false,
  navigationCollapsed: false,
  navigationRevealScrollTop: null,
  lastScrollTop: 0,
  translations: [],
  translation: "kjv",
  filters: { ...DEFAULT_FILTERS, books: [], exclude: [] },
  basket: [],
  pendingSelections: new Set(),
  bookmarks: {
    popoverSelectionId: null,
    selectedTopicId: null,
    originVerse: null,
    originTopicId: null,
    originBookmarkId: null,
    originTopicScrollTop: null,
    search: "",
    storageStatus: null,
  },
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
  bookmark_backup_outcome_locked: "bookmarks.backup_failed",
  bookmark_backup_unavailable: "bookmarks.backup_failed",
  bookmark_restore_not_found: "bookmarks.restore_failed",
  global_bookmark_unavailable: "bookmarks.global_verse_unavailable",
  history_scripture_unavailable: "history.verse_unavailable",
  forbidden: "error.forbidden",
  internal_error: "common.request_failed",
  invalid_request: "common.request_failed",
  invalid_bookmark_backup: "bookmarks.import_failed",
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
  homeHistory: "home-history",
  homeHistoryTitle: "home-history-title",
  homeHistoryMeta: "home-history-meta",
  homeBookmarks: "home-bookmarks",
  homeBookmarksTitle: "home-bookmarks-title",
  homeBookmarksMeta: "home-bookmarks-meta",
  translationShortcut: "translation-shortcut",
  translationShortLabel: "translation-short-label",
  translationFullLabel: "translation-full-label",
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
  bibleHistory: "bible-history",
  bibleHistoryCount: "bible-history-count",
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
  historyView: "history-view",
  readingHistorySummary: "reading-history-summary",
  readingHistoryEmpty: "reading-history-empty",
  readingHistoryList: "reading-history-list",
  clearReadingHistory: "clear-reading-history",
  emptyHistoryBrowse: "empty-history-browse",
  bookmarksView: "bookmarks-view",
  bookmarksTitle: "bookmarks-title",
  bookmarksSummary: "bookmarks-summary",
  clearBookmarks: "clear-bookmarks",
  bookmarkStorageWarning: "bookmark-storage-warning",
  bookmarkGroupsPanel: "bookmark-groups-panel",
  bookmarkTopicSearch: "bookmark-topic-search",
  bookmarkGroupList: "bookmark-group-list",
  bookmarkGroupsEmpty: "bookmark-groups-empty",
  bookmarkTopicManager: "bookmark-topic-manager",
  bookmarkTopicForm: "bookmark-topic-form",
  bookmarkTopicName: "bookmark-topic-name",
  bookmarkTopicColor: "bookmark-topic-color",
  bookmarkTopicEditor: "bookmark-topic-editor",
  restoreDefaultBookmarkTopics: "restore-default-bookmark-topics",
  bookmarkDefaultTopicStatus: "bookmark-default-topic-status",
  loadGlobalBookmarks: "load-global-bookmarks",
  clearGlobalBookmarks: "clear-global-bookmarks",
  globalBookmarkStatus: "global-bookmark-status",
  bookmarkDetail: "bookmark-detail",
  bookmarkAllTopics: "bookmark-all-topics",
  bookmarkBackToVerse: "bookmark-back-to-verse",
  bookmarkDetailDot: "bookmark-detail-dot",
  bookmarkDetailTitle: "bookmark-detail-title",
  bookmarkDetailCount: "bookmark-detail-count",
  bookmarkTopicGlobalStatus: "bookmark-topic-global-status",
  loadTopicGlobalBookmarks: "load-topic-global-bookmarks",
  loadTopicGlobalBookmarksLabel: "load-topic-global-bookmarks-label",
  clearTopicGlobalBookmarks: "clear-topic-global-bookmarks",
  bookmarkDetailEmpty: "bookmark-detail-empty",
  bookmarkList: "bookmark-list",
  backupBookmarks: "backup-bookmarks",
  downloadBookmarks: "download-bookmarks",
  importBookmarks: "import-bookmarks",
  bookmarkImportFile: "bookmark-import-file",
  bookmarkBackupStatus: "bookmark-backup-status",
  bookmarkPopover: "bookmark-popover",
  bookmarkPopoverReference: "bookmark-popover-reference",
  bookmarkAssignedTopics: "bookmark-assigned-topics",
  bookmarkTopicPicker: "bookmark-topic-picker",
  clearRecentBookmarkTopics: "clear-recent-bookmark-topics",
  contributorTopicGuidance: "contributor-topic-guidance",
  closeBookmarkPopover: "close-bookmark-popover",
  selectionSummary: "selection-summary",
  clearSelection: "clear-selection",
  selectionEmpty: "selection-empty",
  selectionList: "selection-list",
  postState: "post-state",
  copySelection: "copy-selection",
  postSelection: "post-selection",
  emptyBrowse: "empty-browse",
  navSelectionCount: "nav-selection-count",
  bottomNav: "bottom-nav",
  bottomNavHandle: "bottom-nav-handle",
  filtersDialog: "filters-dialog",
  filtersForm: "filters-form",
  filterCase: "filter-case",
  filterExclude: "filter-exclude",
  filterProximity: "filter-proximity",
  filterBooks: "filter-books",
  clearBookFilters: "clear-book-filters",
  resetFilters: "reset-filters",
  toastRegion: "toast-region",
  announcer: "announcer",
});

const clipboard = new ClipboardController({
  button: elements.copySelection,
  getItems: () => state.basket,
  message: (key) => clipboardMessage(key, i18n.locale),
  toast,
  notifySuccess: () => bridge.notifySuccess(),
  notifyError: () => bridge.notifyError(),
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
  scriptureExcerpts = new ScriptureExcerptResolver({
    loadChapter: async ({ translation, book, chapter, verse }) => {
      const scripture = normalizeScripture(
        await api.scripturePreview(translation, book, chapter, verse),
        { translation, book, chapter },
      );
      return scripture.verses;
    },
  });
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

    const storageScope = await bookmarkStorageScope(payload?.user?.id);
    bookmarkStorageScopeValue = storageScope;
    readingHistory = new ReadingHistoryStore({ scope: storageScope });
    const [liveCatalog, globalBookmarkStorage, synchronizedBookmarkStorage] =
      await Promise.all([
        loadLiveGlobalBookmarkCatalog({
          api,
          scope: storageScope,
          instanceScope,
        }),
        GlobalBookmarkDeviceStorage.open({
          scope: storageScope,
          webApp: bridge.webApp,
        }),
        TelegramBookmarkStorage.open({
          scope: storageScope,
          webApp: bridge.webApp,
          hydrateLastRead: true,
          onStatus: (status) => {
            state.bookmarks.storageStatus = status;
            updateBookmarkStorageWarning();
          },
        }),
      ]);
    globalBookmarkCatalog = liveCatalog.catalog;
    globalBookmarkCatalogChecksum = liveCatalog.checksum;
    globalBookmarkDeviceStorage = globalBookmarkStorage;
    globalBookmarkPreferences = new GlobalBookmarkPreferences({
      allowedTopicIds: globalBookmarkCatalog
        .topicDefinitions()
        .map((definition) => definition.id),
      allowedBookmarkIds: globalBookmarkCatalog.bookmarkIds(),
      scope: storageScope,
      storage: globalBookmarkStorage,
    });
    bookmarkStorage = synchronizedBookmarkStorage;
    bookmarkStore = new BookmarkStore({
      scope: storageScope,
      storage: bookmarkStorage,
    });
    const contributionOptions = {
      scope: storageScope,
      instanceScope,
      api,
      coreTopicIds: globalBookmarkCatalog
        .topicDefinitions()
        .map((definition) => definition.id),
      // Pace background batches below the server's small per-user burst
      // budget. A Retry-After response remains authoritative below.
      batchPause: () => new Promise((resolve) =>
        window.setTimeout(resolve, contributionBatchDelayMs)
      ),
    };
    try {
      contributionSync = await ContributionSync.open(contributionOptions);
    } catch {
      // IndexedDB/Web Locks may be unavailable in an older WebView. Personal
      // bookmarks still open normally; online contribution retries remain
      // memory-only and the contributor sees the durability warning.
      contributionSync = new ContributionSync({
        ...contributionOptions,
        storage: null,
      });
    }
    const storedLastRead = await bookmarkStorage.readLastRead();
    const synchronizedLastReadWasCleared = Boolean(
      bookmarkStorage.status.lastReadCleared,
    );
    lastReadRevision = Math.max(
      lastReadRevision,
      Number(bookmarkStorage.status.lastReadRecordUpdatedAt) || 0,
    );
    const synchronizedLocation = normalizeReaderLocation(
      storedLastRead,
      storedLastRead?.translation,
    );
    const canRestoreLocation = synchronizedLocation &&
      session.translations.some(
        (translation) => translation.code === synchronizedLocation.translation,
      );
    if (canRestoreLocation) {
      session.preferences.translation = synchronizedLocation.translation;
      session.preferences.search_defaults.translation =
        synchronizedLocation.translation;
      session.preferences.reader_location = synchronizedLocation;
    } else if (synchronizedLastReadWasCleared) {
      session.preferences.reader_location = null;
    } else if (session.preferences.reader_location) {
      void bookmarkStorage.writeLastRead(
        telegramLastReadRecord(session.preferences.reader_location),
      );
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
    if (canRestoreLocation) {
      void savePreferences(synchronizedLocation, { mirrorLastRead: false });
    } else if (synchronizedLastReadWasCleared) {
      void savePreferences(null, { mirrorLastRead: false });
    }

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
    if (entrypoint.bookmark_restore_available) {
      await restoreBookmarksFromChat();
    }
    void initializeContributionSync();
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

async function initializeContributionSync() {
  if (!contributionSync) {
    return;
  }
  try {
    const status = await contributionSync.refreshStatus();
    updateContributorPresentation();
    if (status.can_contribute && status.disclosure_required) {
      await bridge.alert(i18n.t("bookmarks.contribution_disclosure"));
      await contributionSync.acknowledgeDisclosure();
      updateContributorPresentation();
    }
    scheduleContributionSync();
  } catch (error) {
    if (error instanceof ApiError && [401, 409].includes(error.status)) {
      handleSessionError(error);
    } else if (
      (error instanceof ApiError ? error.retryable : navigator.onLine !== false) &&
      contributionRetryTimer === null
    ) {
      const delay = contributionRetryDelay(error);
      contributionRetryTimer = window.setTimeout(() => {
        contributionRetryTimer = null;
        void initializeContributionSync();
      }, delay);
    }
    updateContributorPresentation();
  }
}

function updateContributorPresentation() {
  elements.contributorTopicGuidance.hidden = !contributionSync?.canContribute;
  const messageKey = contributionSync?.persistenceFailed
    ? "bookmarks.contribution_storage_attention"
    : contributionSync?.recovering
      ? "bookmarks.contribution_sync_attention"
      : "bookmarks.contribution_english_guidance";
  elements.contributorTopicGuidance.textContent = i18n.t(
    messageKey,
  );
}

function mutatePersonalBookmarks(operation) {
  const before = bookmarkStore?.snapshot();
  const result = operation();
  const after = bookmarkStore?.snapshot();
  if (before && after && contributionSync) {
    try {
      void Promise.resolve(contributionSync.captureMutation(before, after))
        .then(() => {
          updateContributorPresentation();
          if (
            contributionSync.pendingCount > 0 ||
            contributionSync.recovering
          ) {
            scheduleContributionSync();
          }
        })
        .catch(() => updateContributorPresentation());
    } catch {
      // Contribution mirroring is deliberately subordinate to the completed
      // local bookmark write. A broken journal must never undo user data.
    }
  }
  return result;
}

function captureGlobalBookmarkRemoval(bookmark) {
  if (!contributionSync?.canContribute || !bookmarkStore) {
    return;
  }
  const definition = globalBookmarkCatalog.topicDefinition(
    bookmark?.catalog_topic_id,
  );
  if (!definition) {
    return;
  }
  try {
    const captured = contributionSync.captureGlobalRemoval(
      {
        id: definition.id,
        name: definition.name,
        color: definition.color,
      },
      {
        book: bookmark.book,
        chapter: bookmark.chapter,
        verse: bookmark.verse,
      },
      bookmarkStore.snapshot(),
    );
    void Promise.resolve(captured).then(() => {
      updateContributorPresentation();
      if (contributionSync.pendingCount > 0 || contributionSync.recovering) {
        scheduleContributionSync();
      }
    }).catch(() => updateContributorPresentation());
  } catch {
    // The global hide has already succeeded locally. Contribution recovery is
    // intentionally independent and may retry without undoing that choice.
    updateContributorPresentation();
  }
}

function captureGlobalBookmarkAdditions(bookmarks) {
  if (
    !contributionSync?.canContribute ||
    !bookmarkStore ||
    !Array.isArray(bookmarks) ||
    bookmarks.length === 0
  ) {
    return;
  }
  const assignments = bookmarks.flatMap((bookmark) => {
    const definition = globalBookmarkCatalog.topicDefinition(
      bookmark?.catalog_topic_id,
    );
    return definition
      ? [{
          topic: {
            id: definition.id,
            name: definition.name,
            color: definition.color,
          },
          verse: {
            book: bookmark.book,
            chapter: bookmark.chapter,
            verse: bookmark.verse,
          },
        }]
      : [];
  });
  if (assignments.length === 0) {
    return;
  }
  try {
    const captured = contributionSync.captureGlobalAdditions(
      assignments,
      bookmarkStore.snapshot(),
    );
    void Promise.resolve(captured).then(() => {
      updateContributorPresentation();
      if (contributionSync.pendingCount > 0 || contributionSync.recovering) {
        scheduleContributionSync();
      }
    }).catch(() => updateContributorPresentation());
  } catch {
    // The exclusions have already been restored locally. A failed mirror stays
    // subordinate to that completed preference write and remains visible in
    // the contributor storage guidance.
    updateContributorPresentation();
  }
}

function scheduleContributionSync() {
  if (
    contributionSyncTask ||
    contributionRetryTimer !== null ||
    !contributionSync?.canContribute ||
    !bookmarkStore ||
    navigator.onLine === false
  ) {
    return;
  }
  const snapshot = bookmarkStore.snapshot();
  let completed = false;
  contributionSyncTask = Promise.resolve()
    .then(() => contributionSync.synchronize(snapshot))
    .then(() => {
      completed = true;
      contributionRetryDelayMs = 2_000;
    })
    .catch((error) => {
      if (error instanceof ApiError && [401, 409].includes(error.status)) {
        handleSessionError(error);
        return;
      }
      if (error instanceof ApiError ? error.retryable : navigator.onLine !== false) {
        const delay = contributionRetryDelay(error);
        contributionRetryTimer = window.setTimeout(() => {
          contributionRetryTimer = null;
          scheduleContributionSync();
        }, delay);
      }
    })
    .finally(() => {
      contributionSyncTask = null;
      updateContributorPresentation();
      // Close the narrow race where a mutation is durably captured after the
      // drain observes an empty queue but before this task releases its local
      // single-flight marker.
      if (
        completed &&
        contributionRetryTimer === null &&
        (contributionSync?.pendingCount > 0 || contributionSync?.recovering)
      ) {
        queueMicrotask(scheduleContributionSync);
      }
    });
}

async function refreshLiveGlobalBookmarkCatalog() {
  if (!api || !bookmarkStorageScopeValue) {
    return;
  }
  const result = await loadLiveGlobalBookmarkCatalog({
    api,
    scope: bookmarkStorageScopeValue,
    instanceScope,
  });
  if (
    result.checksum === globalBookmarkCatalogChecksum ||
    (result.checksum === null && globalBookmarkCatalogChecksum === null)
  ) {
    return;
  }
  globalBookmarkCatalog = result.catalog;
  globalBookmarkCatalogChecksum = result.checksum;
  globalBookmarkPreferences = new GlobalBookmarkPreferences({
    allowedTopicIds: globalBookmarkCatalog
      .topicDefinitions()
      .map((definition) => definition.id),
    allowedBookmarkIds: globalBookmarkCatalog.bookmarkIds(),
    scope: bookmarkStorageScopeValue,
    storage: globalBookmarkDeviceStorage,
  });
  renderBookmarks();
  if (state.bible.status === "ready") {
    renderBible();
  }
}

function attachListeners() {
  elements.accessRetry.addEventListener("click", () => accessAction());
  window.addEventListener("online", () => {
    updateConnectionState();
    void refreshLiveGlobalBookmarkCatalog();
    scheduleContributionSync();
  });
  window.addEventListener("offline", updateConnectionState);

  document.querySelectorAll("[data-route]").forEach((button) => {
    button.addEventListener("click", () => {
      showNavigation();
      clearBookmarkNavigation();
      setRoute(button.dataset.route);
    });
  });
  document.querySelectorAll("[data-home-route]").forEach((button) => {
    button.addEventListener("click", () => {
      showNavigation();
      clearBookmarkNavigation();
      setRoute(button.dataset.homeRoute);
    });
  });
  document.querySelectorAll("[data-view]").forEach((view) => {
    view.addEventListener("scroll", () => onViewScroll(view), { passive: true });
  });
  elements.bottomNavHandle.addEventListener("click", () => {
    if (state.navigationCollapsed) {
      revealNavigation();
    } else {
      setNavigationCollapsed(true);
    }
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
    if (document.hidden) {
      if (state.route === "bible") {
        persistVisibleReaderPosition();
      }
      void globalBookmarkPreferences?.flush();
    }
  });
  window.addEventListener("pagehide", () => {
    if (state.route === "bible") {
      persistVisibleReaderPosition();
    }
    void globalBookmarkPreferences?.flush();
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
  elements.clearReadingHistory.addEventListener("click", () => {
    void clearReadingHistory();
  });
  elements.readingHistoryList.addEventListener(
    "click",
    onReadingHistoryAction,
  );
  elements.bibleSearchReturn.addEventListener("click", () => {
    state.bible.returnToSearch = false;
    setRoute("search");
  });

  elements.searchResults.addEventListener("click", onVerseCardClick);
  elements.bibleVerses.addEventListener("click", onVerseCardClick);
  elements.selectionList.addEventListener("click", onSelectionAction);
  elements.clearSelection.addEventListener("click", () => void clearBasket());
  elements.copySelection.addEventListener("click", () => void clipboard.copy());
  elements.postSelection.addEventListener("click", () => void postBasket());
  elements.emptyBrowse.addEventListener("click", () => setRoute("bible"));
  elements.emptyHistoryBrowse.addEventListener("click", () => setRoute("bible"));

  elements.bookmarkTopicSearch.addEventListener("input", () => {
    state.bookmarks.search = elements.bookmarkTopicSearch.value;
    renderBookmarkGroups();
  });
  elements.bookmarkGroupList.addEventListener("click", onBookmarkGroupAction);
  elements.bookmarkTopicForm.addEventListener("submit", onBookmarkTopicCreate);
  elements.bookmarkTopicEditor.addEventListener("click", onBookmarkTopicEdit);
  elements.bookmarkTopicEditor.addEventListener("change", onBookmarkTopicColorChange);
  elements.restoreDefaultBookmarkTopics.addEventListener("click", () => {
    restoreDefaultBookmarkTopics();
  });
  elements.bookmarkAllTopics.addEventListener("click", showAllBookmarkTopics);
  elements.bookmarkBackToVerse.addEventListener("click", () => {
    void returnToBookmarkOriginVerse();
  });
  elements.bookmarkList.addEventListener("click", onBookmarkListAction);
  elements.clearBookmarks.addEventListener("click", () => void clearBookmarks());
  elements.loadGlobalBookmarks.addEventListener("click", () => {
    void loadGlobalBookmarks();
  });
  elements.clearGlobalBookmarks.addEventListener("click", () => {
    void clearGlobalBookmarks();
  });
  elements.loadTopicGlobalBookmarks.addEventListener("click", () => {
    void loadTopicGlobalBookmarks();
  });
  elements.clearTopicGlobalBookmarks.addEventListener("click", () => {
    void clearTopicGlobalBookmarks();
  });
  elements.backupBookmarks.addEventListener("click", () => {
    void backupBookmarksToChat();
  });
  elements.downloadBookmarks.addEventListener("click", downloadBookmarkBackup);
  elements.importBookmarks.addEventListener("click", () => {
    elements.bookmarkImportFile.click();
  });
  elements.bookmarkImportFile.addEventListener("change", () => {
    void importBookmarkBackup();
  });
  elements.bookmarkAssignedTopics.addEventListener("click", (event) => {
    onBookmarkPopoverTopicAction(event);
  });
  elements.bookmarkTopicPicker.addEventListener("change", () => {
    const topicId = elements.bookmarkTopicPicker.value;
    if (topicId) {
      applyPopoverBookmark(topicId);
    }
  });
  elements.clearRecentBookmarkTopics.addEventListener("click", () => {
    if (!bookmarkStore?.clearRecentTopics()) {
      return;
    }
    const selectionId = state.bookmarks.popoverSelectionId;
    const verse = findVerse(selectionId);
    populateBookmarkTopicPicker(new Set(
      bookmarkAssignmentsForVerse(verse).map((assignment) => assignment.topic_id),
    ));
    announce(i18n.t("bookmarks.recent_topics_cleared"));
    window.requestAnimationFrame(() => {
      elements.bookmarkTopicPicker.focus({ preventScroll: true });
    });
  });
  elements.closeBookmarkPopover.addEventListener("click", closeBookmarkPopover);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !elements.bookmarkPopover.hidden) {
      event.preventDefault();
      closeBookmarkPopover();
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (
      !elements.bookmarkPopover.hidden &&
      !elements.bookmarkPopover.contains(event.target) &&
      !event.target.closest("[data-bookmark-trigger]")
    ) {
      closeBookmarkPopover({ restoreFocus: false });
    }
  });
}

function contributionRetryDelay(error) {
  const announcedDelay = Number.isFinite(error?.retryAfter)
    ? error.retryAfter * 1_000
    : contributionRetryDelayMs;
  const delay = Math.min(300_000, Math.max(250, announcedDelay));
  if (Number.isFinite(error?.retryAfter)) {
    contributionBatchDelayMs = Math.min(
      300_000,
      Math.max(contributionBatchDelayMs, delay + 250),
    );
  }
  contributionRetryDelayMs = Math.min(300_000, Math.max(2_000, delay * 2));
  return delay;
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
  if (route !== "history") {
    cancelHistoryExcerptHydration();
  }
  if (route !== "bookmarks") {
    cancelBookmarkExcerptHydration();
  }
  const currentView = document.querySelector(`[data-view="${state.route}"]`);
  const moveFocusToRoute =
    route !== state.route && currentView?.contains(document.activeElement);
  if (currentView) {
    state.scrollPositions.set(state.route, currentView.scrollTop);
  }
  if (state.route === "bible" && route !== "bible") {
    persistVisibleReaderPosition();
  }
  closeBookmarkPopover({ restoreFocus: false });
  state.route = route;
  elements.app.dataset.activeRoute = route;
  setHeaderCondensed(false);
  const savedScrollTop = state.scrollPositions.get(route) ?? 0;
  state.lastScrollTop = savedScrollTop;
  document.querySelectorAll("[data-view]").forEach((view) => {
    view.hidden = view.dataset.view !== route;
    if (!view.hidden) {
      window.requestAnimationFrame(() => {
        view.scrollTop = savedScrollTop;
      });
    }
  });
  let activeRouteButton = null;
  document.querySelectorAll("[data-route]").forEach((button) => {
    const active = button.dataset.route === route;
    button.classList.toggle("is-active", active);
    if (active) {
      button.setAttribute("aria-current", "page");
      activeRouteButton = button;
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
  if (route === "history") {
    renderReadingHistory({ renderItems: true });
  }
  if (route === "bookmarks") {
    renderBookmarks();
  }
  if (moveFocusToRoute) {
    window.requestAnimationFrame(() => {
      const routeHeading = {
        bookmarks: elements.bookmarksTitle,
        history: document.getElementById("reading-history-title"),
      }[route];
      (activeRouteButton ?? routeHeading)?.focus({ preventScroll: true });
    });
  }
}

function onViewScroll(view) {
  if (view.hidden || view.dataset.view !== state.route) {
    return;
  }
  const scrollTop = Math.max(0, view.scrollTop);
  const delta = scrollTop - state.lastScrollTop;
  state.scrollPositions.set(state.route, scrollTop);
  if (ICON_ONLY_ROUTES.has(state.route)) {
    setHeaderCondensed(false);
  } else if (delta > 6 && scrollTop > 58) {
    setHeaderCondensed(true);
  } else if (delta < -4 || scrollTop < 24) {
    setHeaderCondensed(false);
  }
  if (view !== elements.bibleView) {
    showNavigation();
  } else {
    if (state.navigationRevealScrollTop !== null && delta < -4) {
      state.navigationRevealScrollTop = scrollTop;
    }
    const movedBelowRevealPoint =
      state.navigationRevealScrollTop === null ||
      scrollTop > state.navigationRevealScrollTop + 10;
    if (delta > 10 && scrollTop > 64 && movedBelowRevealPoint) {
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

function setHeaderCondensed(condensed) {
  const nextCondensed = Boolean(condensed);
  if (state.headerCondensed === nextCondensed) {
    return;
  }
  state.headerCondensed = nextCondensed;
  elements.app.classList.toggle(
    "is-header-condensed",
    state.headerCondensed,
  );
}

function setNavigationCollapsed(collapsed) {
  state.navigationCollapsed = Boolean(collapsed);
  if (state.navigationCollapsed) {
    state.navigationRevealScrollTop = null;
  }
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
  state.navigationRevealScrollTop = null;
  setNavigationCollapsed(false);
}

function revealNavigation() {
  const currentView = document.querySelector(`[data-view="${state.route}"]`);
  state.navigationRevealScrollTop = Math.max(0, currentView?.scrollTop ?? 0);
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
      if (bookmarkStorage) {
        // The adapter writes the compact local copy synchronously before its
        // Telegram callbacks, so pagehide cannot strand the newest position
        // behind the network preference request.
        void bookmarkStorage.writeLastRead(telegramLastReadRecord(pending));
      }
      const response = await enqueuePreferenceWrite({
        reader_location: pending,
      });
      const saved = normalizeReaderLocation(
        response?.preferences?.reader_location,
        pending.translation,
      );
      const savedKey = readerLocationKey(saved);
      if (!saved || savedKey !== pendingKey) {
        throw new TypeError("Saved reader location did not match the request.");
      }
      savedReaderPositionKey = savedKey;
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
  if (!elements.bookmarkPopover.hidden) {
    bridge.setBackAction(closeBookmarkPopover);
  } else if (elements.bibleNavigationDialog.open) {
    bridge.setBackAction(
      state.bible.pickerStage === "chapters"
        ? showBibleBookGrid
        : closeBiblePicker,
    );
  } else if (elements.translationDialog.open) {
    bridge.setBackAction(closeTranslationSelector);
  } else if (elements.filtersDialog.open) {
    bridge.setBackAction(closeFilters);
  } else if (
    state.route === "bookmarks" &&
    state.bookmarks.selectedTopicId &&
    state.bookmarks.originVerse
  ) {
    bridge.setBackAction(() => void returnToBookmarkOriginVerse());
  } else if (state.route === "bookmarks" && state.bookmarks.selectedTopicId) {
    bridge.setBackAction(showAllBookmarkTopics);
  } else if (state.route === "bible" && state.bookmarks.originTopicId) {
    bridge.setBackAction(returnToBookmarkOriginTopic);
  } else if (state.route !== "home") {
    bridge.setBackAction(() => setRoute("home"));
  } else {
    bridge.setBackAction(null);
  }
}

function renderReadingHistory({
  renderItems = state.route === "history",
} = {}) {
  const count = readingHistory.size;
  elements.bibleHistoryCount.hidden = count === 0;
  elements.bibleHistoryCount.textContent = count > 99 ? "99+" : String(count);
  elements.bibleHistory.setAttribute(
    "aria-label",
    count === 0
      ? i18n.t("history.open")
      : `${i18n.t("history.open")}: ${i18n.plural("history.count", count)}`,
  );
  elements.readingHistorySummary.textContent = i18n.plural(
    "history.count",
    count,
  );
  elements.homeHistory.hidden = count === 0;
  elements.homeHistoryTitle.textContent = i18n.t("home.history_preview");
  elements.homeHistoryMeta.textContent = count === 0
    ? i18n.t("home.history_preview_hint")
    : i18n.plural("history.count", count);
  elements.clearReadingHistory.hidden = count === 0;
  elements.readingHistoryEmpty.hidden = count > 0;
  elements.readingHistoryList.hidden = count === 0;
  if (!renderItems) {
    if (state.route !== "history") {
      cancelHistoryExcerptHydration();
    }
    return;
  }
  cancelHistoryExcerptHydration();
  elements.readingHistoryList.replaceChildren();
  const entries = readingHistory.snapshot();
  const translation = state.translation;
  entries.forEach((entry) => {
    elements.readingHistoryList.append(
      createReadingHistoryItem(entry, translation),
    );
  });
  hydrateReadingHistory(entries, translation);
}

function createReadingHistoryItem(entry, translationCode) {
  const item = document.createElement("li");
  item.className = "history-item";

  const open = document.createElement("button");
  open.type = "button";
  open.className = "history-item__open";
  open.dataset.historyOpen = entry.id;
  open.setAttribute(
    "aria-label",
    i18n.t("history.open_aria", {
      reference: entry.reference,
      translation: translationName(translationCode),
    }),
  );

  const reference = document.createElement("strong");
  reference.dir = "auto";
  reference.dataset.historyReference = entry.id;
  reference.textContent = entry.reference;
  const translation = document.createElement("span");
  translation.dir = "auto";
  translation.dataset.historyTranslation = entry.id;
  translation.textContent = translationName(translationCode);
  const kind = document.createElement("small");
  kind.textContent = i18n.t(
    entry.kind === "selection" ? "history.selection" : "history.chapter",
  );
  const text = document.createElement("span");
  text.className = "history-item__text";
  text.dataset.historyText = entry.id;
  text.id = `history-excerpt-${entry.id}`;
  text.dir = "auto";
  text.textContent = i18n.t("common.loading_scripture");
  open.setAttribute("aria-describedby", text.id);
  open.append(reference, translation, kind, text);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "history-item__remove";
  remove.dataset.historyRemove = entry.id;
  remove.dataset.historyDisplayReference = entry.reference;
  remove.setAttribute(
    "aria-label",
    `${i18n.t("history.remove_aria", {
      reference: entry.reference,
    })} · ${translationName(translationCode)}`,
  );
  remove.textContent = "×";
  item.append(open, remove);
  return item;
}

function hydrateReadingHistory(entries, translation) {
  if (!scriptureExcerpts || entries.length === 0 || state.route !== "history") {
    return;
  }
  const targets = entries.map((entry) => ({
    id: entry.id,
    translation,
    book: entry.book,
    chapter: entry.chapter,
    verse: entry.verse,
  }));
  historyExcerptController = startVisibleScriptureExcerptHydration({
    targets,
    root: elements.historyView,
    elementForTarget: (target) => elements.readingHistoryList.querySelector(
      `[data-history-open="${target.id}"]`,
    ),
    isCurrent: () =>
      state.route === "history" && state.translation === translation,
    onPending: (target) => {
      elements.readingHistoryList.querySelector(
        `[data-history-open="${target.id}"]`,
      )?.setAttribute("aria-busy", "true");
    },
    onResult: (target, verse) => {
      const reference = elements.readingHistoryList.querySelector(
        `[data-history-reference="${target.id}"]`,
      );
      const text = elements.readingHistoryList.querySelector(
        `[data-history-text="${target.id}"]`,
      );
      if (!reference?.isConnected || !text?.isConnected) {
        return;
      }
      const open = text.closest("[data-history-open]");
      open?.setAttribute("aria-busy", "false");
      if (!verse || verse.status === "error") {
        text.textContent = i18n.t(
          navigator.onLine ? "common.request_failed" : "error.network",
        );
        return;
      }
      if (verse.status === "unavailable") {
        text.textContent = i18n.t("history.verse_unavailable");
        return;
      }
      reference.textContent = verse.reference;
      const translationLabel = elements.readingHistoryList.querySelector(
        `[data-history-translation="${target.id}"]`,
      );
      if (translationLabel?.isConnected) {
        translationLabel.textContent = translationName(translation);
      }
      text.textContent = verse.text;
      open?.setAttribute(
        "aria-label",
        i18n.t("history.open_aria", {
          reference: verse.reference,
          translation: translationName(translation),
        }),
      );
      const remove = elements.readingHistoryList.querySelector(
        `[data-history-remove="${target.id}"]`,
      );
      remove?.setAttribute(
        "aria-label",
        `${i18n.t("history.remove_aria", {
          reference: verse.reference,
        })} · ${translationName(translation)}`,
      );
      if (remove) {
        remove.dataset.historyDisplayReference = verse.reference;
      }
    },
  });
}

function cancelHistoryExcerptHydration() {
  historyExcerptController?.abort();
  historyExcerptController = null;
}

function onReadingHistoryAction(event) {
  const remove = event.target.closest("[data-history-remove]");
  if (remove) {
    const items = readingHistory.snapshot();
    const index = items.findIndex(
      (item) => item.id === remove.dataset.historyRemove,
    );
    const entry = items[index];
    const displayedReference =
      remove.dataset.historyDisplayReference || entry?.reference;
    if (entry && readingHistory.remove(entry.id)) {
      renderReadingHistory();
      announce(i18n.t("history.removed", { reference: displayedReference }));
      const remaining = [
        ...elements.readingHistoryList.querySelectorAll("[data-history-open]"),
      ];
      const adjacent = remaining[Math.min(index, remaining.length - 1)];
      (adjacent ?? elements.emptyHistoryBrowse).focus({ preventScroll: true });
    }
    return;
  }
  const open = event.target.closest("[data-history-open]");
  if (!open) {
    return;
  }
  const entry = readingHistory
    .snapshot()
    .find((item) => item.id === open.dataset.historyOpen);
  if (!entry) {
    return;
  }
  void openReadingHistoryEntry(entry);
}

async function openReadingHistoryEntry(entry) {
  const translation = state.translation;
  await openBibleAtVerse({
    translation,
    reference: entry.reference,
    book_number: entry.book,
    book_name: entry.book_name,
    chapter: entry.chapter,
    verse: entry.verse,
    highlights: [],
  }, {
    exactVerse: true,
    recordHistory: false,
    unavailableMessageKey: "history.verse_unavailable",
  });
  if (
    state.route !== "bible" ||
    state.bible.status !== "ready" ||
    state.translation !== translation ||
    state.bible.selectedBook?.number !== entry.book ||
    state.bible.selectedChapter?.number !== entry.chapter
  ) {
    return;
  }
  const openedVerse = state.bible.verses.find(
    (verse) => verse.verse === entry.verse,
  );
  recordReadingHistory(entry.kind, openedVerse);
  window.requestAnimationFrame(() => {
    const target = elements.bibleVerses.querySelector(
      `[data-reader-verse="${entry.verse}"]`,
    ) ?? elements.biblePassage;
    target.focus({ preventScroll: true });
    announce(openedVerse?.reference ?? state.bible.reference);
  });
}

async function clearReadingHistory() {
  if (readingHistory.size === 0) {
    return;
  }
  const confirmed = await bridge.confirm(i18n.t("history.clear_confirm"));
  if (!confirmed || state.route !== "history") {
    return;
  }
  readingHistory.clear();
  state.scrollPositions.set("history", 0);
  elements.historyView.scrollTop = 0;
  renderReadingHistory();
  announce(i18n.t("history.cleared"));
  elements.emptyHistoryBrowse.focus({ preventScroll: true });
}

function recordReadingHistory(kind, verse) {
  if (!verse) {
    return;
  }
  try {
    readingHistory.record({
      kind,
      translation: verse.translation,
      reference: verse.reference,
      book: verse.book_number,
      book_name: verse.book_name,
      chapter: verse.chapter,
      verse: verse.verse,
    });
  } catch {
    return;
  }
  state.scrollPositions.set("history", 0);
  elements.historyView.scrollTop = 0;
  renderReadingHistory();
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
  const displayName = translation?.name ?? code.toUpperCase();
  elements.translationShortLabel.textContent = code.toUpperCase();
  elements.translationFullLabel.textContent = displayName;
  elements.translationShortcut.title = displayName;
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
  const previousLocale = i18n.locale;
  i18n.setLocale(translation?.lang ?? "en", translation?.direction ?? "ltr");
  i18n.apply();
  if (i18n.locale !== previousLocale) {
    // These are transient rendered messages, not application state. Clear or
    // refresh them so a completed action from the previous locale cannot
    // leave one English (or otherwise stale) sentence in the new view.
    elements.bookmarkDefaultTopicStatus.textContent = "";
    elements.bookmarkBackupStatus.textContent = bookmarkBackupTask
      ? i18n.t("bookmarks.backup_sending")
      : "";
  }
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
  searchPageRequests.invalidate();
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
  renderReadingHistory();
  renderBasketStatus();
  renderSelection();
  renderBookmarks();
}

async function runSearch(rawQuery) {
  const query = rawQuery.trim();
  if (!query) {
    elements.searchQuery.focus();
    announce(i18n.t("search.enter_query"));
    return;
  }
  const requestId = ++searchRequestId;
  searchPageRequests.invalidate();
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
    diacritics: filters.diacritics,
  };
  elements.searchQuery.value = query;
  renderSearch();
  try {
    const result = normalizeSearch(
      await api.search(query, filters),
      translation,
      // Highlighting reads the verse the way the search read it, so it needs
      // the same diacritics policy the query ran under.
      filters.diacritics,
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
  const searchId = state.search.searchId;
  const translation = state.search.translation;
  const request = searchPageRequests.begin({ searchId, translation });
  state.search.loadingMore = true;
  elements.loadMore.disabled = true;
  elements.loadMore.textContent = i18n.t("common.loading");
  try {
    const result = normalizeSearchPage(
      await api.searchPage(searchId, state.search.page + 1),
      searchId,
      translation,
      state.search.diacritics ?? state.filters.diacritics,
    );
    if (
      !searchPageRequests.isCurrent(request) ||
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
    if (!searchPageRequests.isCurrent(request)) {
      return;
    }
    toast(safeError(error).message);
  } finally {
    searchPageRequests.complete(request, () => {
      if (
        state.search.searchId === searchId &&
        state.translation === translation
      ) {
        state.search.loadingMore = false;
        renderSearch();
      }
    });
  }
}

function clearSearch() {
  searchRequestId += 1;
  searchPageRequests.invalidate();
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
  setCheckedRadio("diacritics", filters.diacritics);
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
      diacritics: data.get("diacritics"),
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

async function savePreferences(
  readerLocation = undefined,
  { mirrorLastRead = true } = {},
) {
  try {
    const expectedTranslation = state.translation;
    const requestedLocation = readerLocation === undefined
      ? undefined
      : normalizeReaderLocation(readerLocation, expectedTranslation);
    if (readerLocation !== undefined && readerLocation !== null && !requestedLocation) {
      throw new TypeError("The requested reader location is invalid.");
    }
    if (readerLocation !== undefined && bookmarkStorage && mirrorLastRead) {
      if (requestedLocation) {
        void bookmarkStorage.writeLastRead(
          telegramLastReadRecord(requestedLocation),
        );
      } else {
        void bookmarkStorage.clearLastRead();
      }
    }
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
      payload.reader_location = requestedLocation;
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
    if (
      readerLocation !== undefined &&
      bookmarkStorage &&
      mirrorLastRead &&
      readerLocationKey(location) !== readerLocationKey(requestedLocation)
    ) {
      if (location) {
        void bookmarkStorage.writeLastRead(telegramLastReadRecord(location));
      } else {
        void bookmarkStorage.clearLastRead();
      }
    }
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

function telegramLastReadRecord(location) {
  const normalized = normalizeReaderLocation(
    location,
    location?.translation,
  );
  if (!normalized) {
    throw new TypeError("A valid last-read location is required.");
  }
  lastReadRevision = Math.max(Date.now(), lastReadRevision + 1);
  return {
    version: 1,
    record_updated_at: lastReadRevision,
    ...normalized,
  };
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

function scriptureCoordinateUnavailableError(
  messageKey = "bookmarks.global_verse_unavailable",
) {
  return new ApiError(i18n.t(messageKey), {
    code: messageKey === "history.verse_unavailable"
      ? "history_scripture_unavailable"
      : "global_bookmark_unavailable",
    retryable: false,
  });
}

async function loadBibleBooks({
  exactVerse = false,
  recordHistory = true,
  unavailableMessageKey = "bookmarks.global_verse_unavailable",
} = {}) {
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
      await selectBibleBook(
        entrypoint.book_number,
        entrypoint.chapter,
        1,
        [],
        { exactVerse, recordHistory, unavailableMessageKey },
      );
      return;
    }
    const resume = state.bible.resumeLocation;
    if (resume?.translation === state.translation) {
      if (state.bible.books.some((book) => book.number === resume.book)) {
        renderBible();
        await selectBibleBook(
          resume.book,
          resume.chapter,
          resume.verse,
          state.bible.focusHighlights,
          { exactVerse, recordHistory, unavailableMessageKey },
        );
        return;
      }
      if (exactVerse) {
        throw scriptureCoordinateUnavailableError(unavailableMessageKey);
      }
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
  {
    exactVerse = false,
    recordHistory = true,
    unavailableMessageKey = "bookmarks.global_verse_unavailable",
  } = {},
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
    state.bible.status = exactVerse ? "error" : "choose_book";
    state.bible.error = exactVerse
      ? safeError(scriptureCoordinateUnavailableError(unavailableMessageKey))
      : null;
    renderBible();
    if (exactVerse) {
      focusBibleFailure();
    }
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
        { exactVerse, recordHistory, unavailableMessageKey },
      );
      return;
    }
    if (exactVerse && Number.isInteger(requestedChapter)) {
      throw scriptureCoordinateUnavailableError(unavailableMessageKey);
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
  {
    exactVerse = false,
    recordHistory = true,
    unavailableMessageKey = "bookmarks.global_verse_unavailable",
  } = {},
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
    state.bible.status = exactVerse ? "error" : "choose_chapter";
    state.bible.error = exactVerse
      ? safeError(scriptureCoordinateUnavailableError(unavailableMessageKey))
      : null;
    renderBible();
    if (exactVerse) {
      focusBibleFailure();
    }
    return;
  }
  state.bible.status = "loading_scripture";
  state.bible.targetVerse = exactVerse
    ? targetVerse
    : nearestChapterVerse(chapter, targetVerse);
  state.bible.focusHighlights = Array.isArray(focusHighlights)
    ? focusHighlights
    : [];
  elements.bibleHeading.classList.remove("is-hidden");
  elements.bibleView.scrollTop = 0;
  state.lastScrollTop = 0;
  state.scrollPositions.set("bible", 0);
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
    if (
      exactVerse &&
      !scripture.verses.some((verse) => verse.verse === targetVerse)
    ) {
      throw scriptureCoordinateUnavailableError(unavailableMessageKey);
    }
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
    const visitedVerse = state.bible.verses.find(
      (verse) => verse.verse === state.bible.targetVerse,
    ) ?? state.bible.verses[0];
    if (recordHistory) {
      recordReadingHistory("chapter", visitedVerse);
    }
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

async function openBibleAtVerse(
  verse,
  {
    fromSearch = false,
    exactVerse = false,
    recordHistory = true,
    unavailableMessageKey = "bookmarks.global_verse_unavailable",
  } = {},
) {
  if (!verse) {
    return;
  }
  // An explicit result, bookmark, or History choice supersedes any direct
  // reference supplied by the launch. A still-pending books request must not
  // consume that older intent after this navigation has taken ownership.
  state.bible.entryReference = "";
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
  // Exact global targets are persisted only after the selected translation
  // confirms the coordinate exists.
  if (!exactVerse) {
    void savePreferences(state.bible.resumeLocation);
  }
  syncTranslationControls();
  renderLocalizedState();
  setRoute("bible");
  if (translationChanged || state.bible.books.length === 0) {
    await loadBibleBooks({ exactVerse, recordHistory, unavailableMessageKey });
    return;
  }
  await selectBibleBook(
    verse.book_number,
    verse.chapter,
    verse.verse,
    state.bible.focusHighlights,
    { exactVerse, recordHistory, unavailableMessageKey },
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
    elements.bibleHeading.classList.remove("is-hidden");
    verse.scrollIntoView({ block: "start", behavior: "auto" });
    const toolbarHeight = elements.bibleHeading.hidden
      ? 0
      : Math.ceil(elements.bibleHeading.getBoundingClientRect().height);
    const scriptureBreathingRoom = 10;
    elements.bibleView.scrollTop = Math.max(
      0,
      elements.bibleView.scrollTop - toolbarHeight - scriptureBreathingRoom,
    );
    state.lastScrollTop = elements.bibleView.scrollTop;
    state.scrollPositions.set("bible", elements.bibleView.scrollTop);
  });
}

function renderBible() {
  const bible = state.bible;
  if (!elements.bookmarkPopover.hidden) {
    closeBookmarkPopover({ restoreFocus: false });
  }
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
  const wrapper = document.createElement("div");
  wrapper.className = "reader-verse-row";
  const assignments = bookmarkAssignmentsForVerse(verse);
  const highlightedAssignment = assignments.find(
    (assignment) => assignment.topic_id === state.bookmarks.originTopicId,
  ) ?? assignments[0] ?? null;
  const bookmarkTopic = bookmarkStore?.topic(
    highlightedAssignment?.topic_id,
  ) ?? null;
  if (bookmarkTopic) {
    applyBookmarkColor(wrapper, bookmarkTopic.color);
    wrapper.dataset.bookmarkCount = String(assignments.length);
  }

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
  wrapper.append(button);

  if (isSelected || assignments.length > 0) {
    wrapper.classList.add("has-bookmark-trigger");
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "reader-bookmark-trigger";
    trigger.dataset.bookmarkTrigger = verse.selection_id;
    trigger.setAttribute(
      "aria-label",
      i18n.t("bookmarks.open_palette_aria", {
        reference: verse.reference,
      }),
    );
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute(
      "aria-expanded",
      String(state.bookmarks.popoverSelectionId === verse.selection_id),
    );
    trigger.textContent = "•••";
    wrapper.append(trigger);
  }
  return wrapper;
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
  const bookmarkTrigger = event.target.closest("[data-bookmark-trigger]");
  if (bookmarkTrigger) {
    openBookmarkPopover(bookmarkTrigger.dataset.bookmarkTrigger);
    return;
  }
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
      revealNavigation();
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
      if (!removing) {
        const addedVerse = state.basket.find(
          (verse) => verse.selection_id === selectionId,
        );
        recordReadingHistory("selection", addedVerse);
      }
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

function bookmarkTopicIds(bookmark) {
  const values = Array.isArray(bookmark?.topic_ids)
    ? bookmark.topic_ids
    : [bookmark?.topic_id];
  return [...new Set(values.filter((topicId) =>
    typeof topicId === "string" && topicId.length > 0
  ))];
}

function bookmarkHasTopic(bookmark, topicId) {
  return bookmarkTopicIds(bookmark).includes(topicId);
}

function globalBookmarkTopicMappings() {
  return globalBookmarkPreferences?.topicMappings ?? null;
}

function coreBookmarkTopicDefinition(topicId) {
  if (typeof topicId !== "string" || !topicId) {
    return null;
  }
  const topic = bookmarkStore?.topic(topicId);
  if (!topic) {
    return null;
  }
  return globalBookmarkCatalog.topicDefinitionForLocalTopic(
    topicId,
    [topic],
    globalBookmarkTopicMappings(),
  );
}

function bookmarkTopicPresentation(topic) {
  const definition = coreBookmarkTopicDefinition(topic?.id);
  const translated = definition ? i18n.t(definition.name_key) : null;
  return {
    core: Boolean(definition),
    definition,
    name: definition
      ? translated === definition.name_key ? definition.name : translated
      : topic?.name ?? "",
  };
}

function bookmarkTopicDisplayName(topic) {
  return bookmarkTopicPresentation(topic).name;
}

function sortedBookmarkTopics(topics) {
  return sortBookmarkTopics(
    topics,
    bookmarkTopicDisplayName,
    i18n.locale,
  );
}

function bookmarkAssignmentsForVerse(
  verse,
  snapshot = bookmarkStore?.snapshot(),
) {
  if (!bookmarkStore || !snapshot || !verse) {
    return [];
  }
  const assignments = new Map();
  for (const bookmark of globalBookmarkCatalog.bookmarksForVerse(
    verse,
    snapshot.topics,
    globalBookmarkTopicMappings(),
  )) {
    if (
      globalBookmarkPreferences?.hasTopic(bookmark.catalog_topic_id) &&
      !globalBookmarkPreferences.isBookmarkHidden(bookmark.id)
    ) {
      assignments.set(bookmark.topic_id, bookmark);
    }
  }
  const personal = bookmarkStore.bookmarkFor(verse);
  for (const topicId of bookmarkTopicIds(personal)) {
    if (bookmarkStore.topic(topicId)) {
      assignments.set(topicId, {
        ...personal,
        topic_id: topicId,
      });
    }
  }
  const topicOrder = new Map(
    snapshot.topics.map((topic, index) => [topic.id, index]),
  );
  return [...assignments.values()].sort((left, right) =>
    (topicOrder.get(left.topic_id) ?? Number.MAX_SAFE_INTEGER) -
      (topicOrder.get(right.topic_id) ?? Number.MAX_SAFE_INTEGER)
  );
}

function visibleGlobalBookmarksForTopic(topicId, topics) {
  return globalBookmarkCatalog.bookmarksForTopic(
    topicId,
    topics,
    globalBookmarkTopicMappings(),
  ).filter(
    (bookmark) => !globalBookmarkPreferences?.isBookmarkHidden(bookmark.id),
  );
}

function bookmarkViewForTopic(topicId, snapshot = bookmarkStore?.snapshot()) {
  if (!bookmarkStore || !snapshot) {
    return [];
  }
  const personal = bookmarkStore.bookmarksForTopic(topicId);
  const canonicalTopicId = globalBookmarkCatalog.canonicalTopicId(
    topicId,
    snapshot.topics,
    globalBookmarkTopicMappings(),
  );
  if (
    !canonicalTopicId ||
    !globalBookmarkPreferences?.hasTopic(canonicalTopicId)
  ) {
    return personal;
  }
  const personalCoordinates = new Set(personal.map(bookmarkCoordinateKey));
  const global = visibleGlobalBookmarksForTopic(topicId, snapshot.topics)
    .filter((bookmark) => !personalCoordinates.has(bookmarkCoordinateKey(bookmark)));
  return [...personal, ...global].sort(compareBookmarkViewEntries);
}

function activeGlobalBookmarkStats(snapshot = bookmarkStore?.snapshot()) {
  if (!snapshot || !globalBookmarkPreferences) {
    return { bookmarks: 0, topics: 0 };
  }
  const resolved = globalBookmarkCatalog.resolveTopics(
    snapshot.topics,
    globalBookmarkTopicMappings(),
  );
  const enabledTopics = [...resolved].filter(([canonicalTopicId]) =>
    globalBookmarkPreferences.hasTopic(canonicalTopicId)
  );
  return {
    bookmarks: enabledTopics.reduce(
      (count, [, localTopicId]) => count + visibleGlobalBookmarksForTopic(
        localTopicId,
        snapshot.topics,
      ).length,
      0,
    ),
    topics: enabledTopics.length,
  };
}

function bookmarkSummary(personal, global) {
  const parts = [];
  if (personal > 0 || global === 0) {
    parts.push(i18n.plural("bookmarks.personal_verse", personal));
  }
  if (global > 0) {
    parts.push(i18n.plural("bookmarks.global_link", global));
  }
  return parts.join(" · ");
}

function bookmarkCoordinateKey(bookmark) {
  return `${bookmark.book}/${bookmark.chapter}/${bookmark.verse}`;
}

function compareBookmarkViewEntries(left, right) {
  return (
    left.book - right.book ||
    left.chapter - right.chapter ||
    left.verse - right.verse ||
    Number(left.source === GLOBAL_BOOKMARK_SOURCE) -
      Number(right.source === GLOBAL_BOOKMARK_SOURCE)
  );
}

function renderBookmarks() {
  const snapshot = bookmarkStore?.snapshot() ?? {
    active_topic_id: null,
    topics: [],
    bookmarks: [],
  };
  globalBookmarkPreferences?.pruneTopicMappings(snapshot.topics);
  const personalCount = snapshot.bookmarks.length;
  const globalStats = activeGlobalBookmarkStats(snapshot);
  const summary = bookmarkSummary(personalCount, globalStats.bookmarks);
  const topicIds = new Set(snapshot.topics.map((topic) => topic.id));
  if (
    state.bookmarks.selectedTopicId &&
    !topicIds.has(state.bookmarks.selectedTopicId)
  ) {
    clearBookmarkNavigation();
  }

  elements.bookmarksSummary.textContent = summary;
  elements.homeBookmarksTitle.textContent = i18n.t(
    "home.bookmarks_preview",
  );
  elements.homeBookmarksMeta.textContent = personalCount + globalStats.bookmarks > 0
    ? summary
    : i18n.t("home.bookmarks_preview_hint");
  elements.clearBookmarks.hidden = personalCount === 0;
  elements.clearBookmarks.disabled = !bookmarkStore;
  setBookmarkBackupBusy(Boolean(bookmarkBackupTask));
  setGlobalBookmarkBusy(Boolean(globalBookmarkTask));
  if (globalBookmarkPreferences?.enabled && !globalBookmarkTask) {
    elements.globalBookmarkStatus.textContent = i18n.t(
      "bookmarks.global_current",
      globalStats,
    );
  } else if (!globalBookmarkTask) {
    elements.globalBookmarkStatus.textContent = "";
  }
  updateBookmarkStorageWarning();
  elements.bookmarkGroupsPanel.hidden = Boolean(
    state.bookmarks.selectedTopicId,
  );
  elements.bookmarkDetail.hidden = !state.bookmarks.selectedTopicId;

  if (state.route !== "bookmarks") {
    cancelBookmarkExcerptHydration();
    return;
  }
  if (state.bookmarks.selectedTopicId) {
    renderBookmarkDetail(state.bookmarks.selectedTopicId);
  } else {
    renderBookmarkGroups();
    renderBookmarkTopicEditor();
    setBookmarkColorInput(elements.bookmarkTopicColor);
  }
}

function updateBookmarkStorageWarning() {
  const cloudState = state.bookmarks.storageStatus?.cloud;
  const cloudAvailable = !cloudState || !["unavailable", "error"].includes(
    cloudState,
  );
  elements.bookmarkStorageWarning.hidden = Boolean(
    bookmarkStore?.persistent && cloudAvailable,
  );
}

function renderBookmarkGroups() {
  const snapshot = bookmarkStore?.snapshot();
  if (!snapshot) {
    elements.bookmarkGroupList.replaceChildren();
    elements.bookmarkGroupsEmpty.hidden = true;
    return;
  }
  const query = state.bookmarks.search.trim().toLocaleLowerCase();
  const topics = sortedBookmarkTopics(snapshot.topics.filter((topic) => {
    if (!query) {
      return true;
    }
    const presentation = bookmarkTopicPresentation(topic);
    return [
      presentation.name,
      topic.name,
      presentation.definition?.name,
      ...(presentation.definition?.aliases ?? []),
    ].some((name) => name?.toLocaleLowerCase().includes(query));
  }));
  const fragment = document.createDocumentFragment();
  for (const topic of topics) {
    const topicName = bookmarkTopicDisplayName(topic);
    const count = bookmarkViewForTopic(topic.id, snapshot).length;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "bookmark-group-card";
    button.dataset.bookmarkTopic = topic.id;
    applyBookmarkColor(button, topic.color);
    button.setAttribute(
      "aria-label",
      i18n.t("bookmarks.open_group_aria", {
        name: topicName,
        count: i18n.plural("bookmarks.count", count),
      }),
    );
    const dot = document.createElement("span");
    dot.className = "bookmark-dot";
    dot.setAttribute("aria-hidden", "true");
    const copy = document.createElement("span");
    copy.className = "bookmark-group-card__copy";
    const name = document.createElement("strong");
    name.textContent = topicName;
    const usage = document.createElement("span");
    usage.className = "bookmark-group-card__count";
    usage.textContent = i18n.plural("bookmarks.count", count);
    copy.append(name, usage);
    const arrow = document.createElement("span");
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "›";
    button.append(dot, copy, arrow);
    fragment.append(button);
  }
  elements.bookmarkGroupList.replaceChildren(fragment);
  elements.bookmarkGroupsEmpty.hidden = topics.length > 0;
}

function onBookmarkGroupAction(event) {
  const button = event.target.closest("[data-bookmark-topic]");
  if (!button || !bookmarkStore?.topic(button.dataset.bookmarkTopic)) {
    return;
  }
  clearBookmarkNavigation();
  state.bookmarks.selectedTopicId = button.dataset.bookmarkTopic;
  state.scrollPositions.set("bookmarks", 0);
  elements.bookmarksView.scrollTop = 0;
  renderBookmarks();
  syncBackAction();
  window.requestAnimationFrame(() => {
    elements.bookmarkDetailTitle.focus({ preventScroll: true });
  });
}

function showAllBookmarkTopics() {
  const previousTopicId = state.bookmarks.selectedTopicId;
  clearBookmarkNavigation();
  renderBookmarks();
  syncBackAction();
  window.requestAnimationFrame(() => {
    const target = previousTopicId
      ? elements.bookmarkGroupList.querySelector(
        `[data-bookmark-topic="${previousTopicId}"]`,
      )
      : null;
    (target ?? elements.bookmarkTopicSearch).focus({ preventScroll: true });
  });
}

function clearBookmarkNavigation() {
  state.bookmarks.selectedTopicId = null;
  state.bookmarks.originVerse = null;
  state.bookmarks.originTopicId = null;
  state.bookmarks.originBookmarkId = null;
  state.bookmarks.originTopicScrollTop = null;
}

async function returnToBookmarkOriginVerse() {
  const verse = state.bookmarks.originVerse;
  if (!verse) {
    return;
  }
  const topicId = state.bookmarks.selectedTopicId;
  const originBookmark = bookmarkViewForTopic(topicId).find((bookmark) =>
    bookmark.book === verse.book_number &&
    bookmark.chapter === verse.chapter &&
    bookmark.verse === verse.verse
  );
  state.bookmarks.originVerse = null;
  state.bookmarks.originTopicId = bookmarkStore?.topic(topicId)
    ? topicId
    : null;
  state.bookmarks.originBookmarkId = originBookmark?.id ?? null;
  state.bookmarks.originTopicScrollTop = elements.bookmarksView.scrollTop;
  await openBibleAtVerse(verse, { exactVerse: true });
  window.requestAnimationFrame(() => {
    const target = elements.bibleVerses.querySelector(
      `[data-reader-verse="${verse.verse}"]`,
    );
    target?.focus({ preventScroll: true });
  });
}

function returnToBookmarkOriginTopic() {
  const topicId = state.bookmarks.originTopicId;
  if (!topicId || !bookmarkStore?.topic(topicId)) {
    clearBookmarkNavigation();
    syncBackAction();
    return;
  }
  const bookmarkId = state.bookmarks.originBookmarkId;
  const scrollTop = Number.isFinite(state.bookmarks.originTopicScrollTop)
    ? Math.max(0, state.bookmarks.originTopicScrollTop)
    : state.scrollPositions.get("bookmarks") ?? 0;
  state.bookmarks.originTopicId = null;
  state.bookmarks.originBookmarkId = null;
  state.bookmarks.originTopicScrollTop = null;
  state.bookmarks.selectedTopicId = topicId;
  state.scrollPositions.set("bookmarks", scrollTop);
  setRoute("bookmarks");
  elements.bookmarksView.scrollTop = scrollTop;
  renderBookmarks();
  window.requestAnimationFrame(() => {
    elements.bookmarksView.scrollTop = scrollTop;
    const target = bookmarkId
      ? elements.bookmarkList.querySelector(
        `[data-bookmark-open="${bookmarkId}"]`,
      )
      : null;
    (target ?? elements.bookmarkDetailTitle).focus({ preventScroll: true });
  });
}

function renderBookmarkDetail(topicId) {
  cancelBookmarkExcerptHydration();
  const topic = bookmarkStore?.topic(topicId);
  if (!topic) {
    return;
  }
  const snapshot = bookmarkStore.snapshot();
  const canonicalTopicId = globalBookmarkCatalog.canonicalTopicId(
    topicId,
    snapshot.topics,
    globalBookmarkTopicMappings(),
  );
  const globalBookmarks = canonicalTopicId
    ? globalBookmarkCatalog.bookmarksForTopic(
      topicId,
      snapshot.topics,
      globalBookmarkTopicMappings(),
    )
    : [];
  const visibleGlobalBookmarks = canonicalTopicId
    ? visibleGlobalBookmarksForTopic(topicId, snapshot.topics)
    : [];
  const globalsEnabled = Boolean(
    canonicalTopicId && globalBookmarkPreferences?.hasTopic(canonicalTopicId),
  );
  const bookmarks = bookmarkViewForTopic(topicId, snapshot);
  const topicName = bookmarkTopicDisplayName(topic);
  applyBookmarkColor(elements.bookmarkDetail, topic.color);
  elements.bookmarkDetailTitle.tabIndex = -1;
  elements.bookmarkDetailTitle.textContent = topicName;
  elements.bookmarkDetailCount.textContent = i18n.plural(
    "bookmarks.count",
    bookmarks.length,
  );
  elements.bookmarkBackToVerse.hidden = !state.bookmarks.originVerse;
  elements.loadTopicGlobalBookmarks.hidden = !canonicalTopicId;
  elements.loadTopicGlobalBookmarksLabel.textContent = i18n.t(
    globalsEnabled
      ? "bookmarks.reload_topic_global"
      : "bookmarks.load_topic_global",
  );
  elements.clearTopicGlobalBookmarks.hidden = !canonicalTopicId || !globalsEnabled;
  if (canonicalTopicId) {
    elements.bookmarkTopicGlobalStatus.textContent = i18n.t(
      globalsEnabled
        ? "bookmarks.topic_global_loaded"
        : "bookmarks.topic_global_available",
      {
        bookmarks: globalsEnabled
          ? visibleGlobalBookmarks.length
          : globalBookmarks.length,
      },
    );
  } else {
    elements.bookmarkTopicGlobalStatus.textContent = "";
  }
  elements.bookmarkDetailEmpty.hidden = bookmarks.length > 0;
  elements.bookmarkList.hidden = bookmarks.length === 0;
  const fragment = document.createDocumentFragment();
  for (const bookmark of bookmarks) {
    const item = document.createElement("li");
    item.className = "bookmark-list__item";
    applyBookmarkColor(item, topic.color);

    const open = document.createElement("button");
    open.type = "button";
    open.className = "bookmark-list__open";
    open.dataset.bookmarkOpen = bookmark.id;
    open.setAttribute(
      "aria-label",
      bookmark.source === GLOBAL_BOOKMARK_SOURCE
        ? i18n.t("bookmarks.open_global_aria", {
          reference: bookmark.reference,
          translation: translationName(state.translation),
        })
        : i18n.t("bookmarks.open_aria", {
          reference: bookmark.reference,
          translation: translationName(bookmark.translation),
        }),
    );
    const reference = document.createElement("strong");
    reference.className = "bookmark-list__reference";
    const translation = bookmark.source === GLOBAL_BOOKMARK_SOURCE
      ? state.translation
      : bookmark.translation;
    const referenceText = document.createElement("span");
    referenceText.textContent = `${bookmark.reference} · ${translation.toUpperCase()}`;
    if (bookmark.source === GLOBAL_BOOKMARK_SOURCE) {
      referenceText.dataset.bookmarkPreviewReference = bookmark.id;
    }
    reference.append(referenceText);
    if (bookmark.source === GLOBAL_BOOKMARK_SOURCE) {
      const marker = document.createElement("span");
      marker.className = "bookmark-list__global-badge";
      marker.textContent = "G";
      marker.setAttribute("aria-hidden", "true");
      marker.title = i18n.t("bookmarks.global_marker");
      reference.append(marker);
    }
    const text = document.createElement("span");
    text.className = "bookmark-list__text";
    text.id = `bookmark-excerpt-${bookmark.id}`;
    if (bookmark.source === GLOBAL_BOOKMARK_SOURCE) {
      text.dataset.bookmarkPreviewText = bookmark.id;
    }
    text.textContent = bookmark.source === GLOBAL_BOOKMARK_SOURCE
      ? i18n.t("common.loading_scripture")
      : bookmark.text;
    open.append(reference);
    text.hidden = bookmark.source !== GLOBAL_BOOKMARK_SOURCE && !bookmark.text;
    if (!text.hidden) {
      open.setAttribute("aria-describedby", text.id);
    }
    open.append(text);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "bookmark-list__remove";
    remove.dataset.bookmarkRemove = bookmark.id;
    remove.dataset.bookmarkDisplayReference = bookmark.reference;
    remove.setAttribute(
      "aria-label",
      bookmark.source === GLOBAL_BOOKMARK_SOURCE
        ? i18n.t("bookmarks.remove_global_aria", {
          reference: bookmark.reference,
        })
        : i18n.t("bookmarks.remove_topic_assignment_aria", {
          reference: bookmark.reference,
          name: topicName,
        }),
    );
    remove.textContent = "×";
    item.append(open, remove);
    fragment.append(item);
  }
  elements.bookmarkList.replaceChildren(fragment);
  hydrateGlobalBookmarkDetail(
    bookmarks.filter((bookmark) =>
      bookmark.source === GLOBAL_BOOKMARK_SOURCE
    ),
    topicId,
    state.translation,
  );
}

function hydrateGlobalBookmarkDetail(bookmarks, topicId, translation) {
  if (
    !scriptureExcerpts ||
    bookmarks.length === 0 ||
    state.route !== "bookmarks"
  ) {
    return;
  }
  const targets = bookmarks.map((bookmark) => ({
    id: bookmark.id,
    translation,
    book: bookmark.book,
    chapter: bookmark.chapter,
    verse: bookmark.verse,
  }));
  bookmarkExcerptController = startVisibleScriptureExcerptHydration({
    targets,
    root: elements.bookmarksView,
    elementForTarget: (target) => elements.bookmarkList.querySelector(
      `[data-bookmark-open="${target.id}"]`,
    ),
    isCurrent: () =>
      state.route === "bookmarks" &&
      state.bookmarks.selectedTopicId === topicId &&
      state.translation === translation,
    onPending: (target) => {
      elements.bookmarkList.querySelector(
        `[data-bookmark-open="${target.id}"]`,
      )?.setAttribute("aria-busy", "true");
    },
    onResult: (target, verse) => {
      const reference = elements.bookmarkList.querySelector(
        `[data-bookmark-preview-reference="${target.id}"]`,
      );
      const text = elements.bookmarkList.querySelector(
        `[data-bookmark-preview-text="${target.id}"]`,
      );
      if (!reference?.isConnected || !text?.isConnected) {
        return;
      }
      const open = text.closest("[data-bookmark-open]");
      open?.setAttribute("aria-busy", "false");
      if (!verse || verse.status === "error") {
        text.textContent = i18n.t(
          navigator.onLine ? "common.request_failed" : "error.network",
        );
        return;
      }
      if (verse.status === "unavailable") {
        text.textContent = i18n.t("bookmarks.global_verse_unavailable");
        return;
      }
      reference.textContent = `${verse.reference} · ${translation.toUpperCase()}`;
      text.textContent = verse.text;
      open?.setAttribute(
        "aria-label",
        i18n.t("bookmarks.open_global_aria", {
          reference: verse.reference,
          translation: translationName(translation),
        }),
      );
      const remove = elements.bookmarkList.querySelector(
        `[data-bookmark-remove="${target.id}"]`,
      );
      remove?.setAttribute(
        "aria-label",
        i18n.t("bookmarks.remove_global_aria", {
          reference: verse.reference,
        }),
      );
      if (remove) {
        remove.dataset.bookmarkDisplayReference = verse.reference;
      }
    },
  });
}

function cancelBookmarkExcerptHydration() {
  bookmarkExcerptController?.abort();
  bookmarkExcerptController = null;
}

/**
 * Hydrate the first screenful immediately, then resolve nearby rows as they
 * enter the scroll viewport. History and global bookmarks share this one
 * bounded path so a long list cannot eagerly launch hundreds of chapter reads.
 */
function startVisibleScriptureExcerptHydration({
  targets,
  root,
  elementForTarget,
  isCurrent,
  onPending,
  onResult,
}) {
  if (!scriptureExcerpts || targets.length === 0) {
    return null;
  }
  const controller = new AbortController();
  const queued = [];
  const reconnectTargets = new Map();
  const scheduledIds = new Set();
  const targetsByElement = new Map();
  let observer = null;
  let draining = false;
  let drainScheduled = false;

  const requestDrain = () => {
    if (draining || drainScheduled || controller.signal.aborted) {
      return;
    }
    drainScheduled = true;
    window.queueMicrotask(() => {
      drainScheduled = false;
      void drain();
    });
  };

  const schedule = (target) => {
    if (
      controller.signal.aborted ||
      scheduledIds.has(target.id) ||
      !isCurrent()
    ) {
      return;
    }
    scheduledIds.add(target.id);
    reconnectTargets.delete(target.id);
    queued.push(target);
    onPending(target);
    requestDrain();
  };

  const drain = async () => {
    if (draining || controller.signal.aborted) {
      return;
    }
    draining = true;
    try {
      while (queued.length > 0 && !controller.signal.aborted) {
        if (!isCurrent()) {
          controller.abort();
          return;
        }
        const batch = queued.splice(0, EXCERPT_HYDRATION_BATCH_SIZE);
        await Promise.all(batch.map(async (target) => {
          let verse = null;
          try {
            [verse] = await scriptureExcerpts.resolve([target], {
              signal: controller.signal,
            });
          } catch {
            // A row-level failure renders its localized unavailable state and
            // never holds back the other visible rows in this bounded group.
          }
          if (!controller.signal.aborted && isCurrent()) {
            onResult(target, verse ?? null);
            if (!verse || verse.status === "error") {
              scheduledIds.delete(target.id);
              reconnectTargets.set(target.id, target);
            }
          }
        }));
      }
    } finally {
      draining = false;
      if (queued.length > 0 && !controller.signal.aborted) {
        requestDrain();
      }
    }
  };

  const Observer = window.IntersectionObserver;
  if (typeof Observer === "function") {
    observer = new Observer((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) {
          continue;
        }
        const target = targetsByElement.get(entry.target);
        if (target) {
          observer.unobserve(entry.target);
          schedule(target);
        }
      }
    }, {
      root,
      rootMargin: "360px 0px",
    });
  }

  targets.forEach((target, index) => {
    const element = elementForTarget(target);
    if (!element) {
      return;
    }
    element.addEventListener("focusin", () => {
      observer?.unobserve(element);
      schedule(target);
    }, { once: true });
    if (index < EXCERPT_HYDRATION_BATCH_SIZE || !observer) {
      schedule(target);
      return;
    }
    targetsByElement.set(element, target);
    observer.observe(element);
  });

  const retryOnReconnect = () => {
    const retryTargets = [...reconnectTargets.values()];
    reconnectTargets.clear();
    retryTargets.forEach(schedule);
  };
  window.addEventListener("online", retryOnReconnect);

  controller.signal.addEventListener("abort", () => {
    observer?.disconnect();
    window.removeEventListener("online", retryOnReconnect);
    queued.length = 0;
    reconnectTargets.clear();
    targetsByElement.clear();
  }, { once: true });
  return controller;
}

async function onBookmarkListAction(event) {
  const remove = event.target.closest("[data-bookmark-remove]");
  const open = event.target.closest("[data-bookmark-open]");
  const bookmarkId = remove?.dataset.bookmarkRemove ?? open?.dataset.bookmarkOpen;
  const snapshot = bookmarkStore?.snapshot();
  const topicId = state.bookmarks.selectedTopicId;
  const personalRecord = snapshot?.bookmarks.find((item) =>
    item.id === bookmarkId && bookmarkHasTopic(item, topicId)
  );
  const personalBookmark = personalRecord
    ? { ...personalRecord, topic_id: topicId }
    : null;
  const globalBookmark = personalBookmark
    ? null
    : globalBookmarkCatalog.bookmarkById(
      bookmarkId,
      snapshot?.topics ?? [],
      globalBookmarkTopicMappings(),
    );
  const bookmark = personalBookmark ?? (
    globalBookmark &&
    globalBookmarkPreferences?.hasTopic(globalBookmark.catalog_topic_id) &&
    !globalBookmarkPreferences.isBookmarkHidden(globalBookmark.id)
      ? globalBookmark
      : null
  );
  if (!bookmark) {
    return;
  }
  if (remove) {
    const displayedReference =
      remove.dataset.bookmarkDisplayReference || bookmark.reference;
    const rows = [...elements.bookmarkList.querySelectorAll(
      ".bookmark-list__item",
    )];
    const removalIndex = rows.indexOf(remove.closest(".bookmark-list__item"));
    if (bookmark.source === GLOBAL_BOOKMARK_SOURCE) {
      const hidden = globalBookmarkPreferences.hideBookmark(bookmark.id);
      void globalBookmarkPreferences.flush();
      if (hidden) {
        captureGlobalBookmarkRemoval(bookmark);
      }
    } else {
      mutatePersonalBookmarks(() =>
        bookmarkStore.removeBookmarkTopic(bookmark.id, bookmark.topic_id)
      );
    }
    renderBookmarks();
    if (state.bible.status === "ready") {
      renderBible();
    }
    announce(i18n.t(
      bookmark.source === GLOBAL_BOOKMARK_SOURCE
        ? "bookmarks.global_removed"
        : "bookmarks.removed_from_topic",
      {
        reference: displayedReference,
        name: bookmarkTopicDisplayName(bookmarkStore.topic(topicId)),
      },
    ));
    focusBookmarkListAfterRemoval(removalIndex);
    return;
  }
  const translation = bookmark.source === GLOBAL_BOOKMARK_SOURCE
    ? state.translation
    : state.translations.some((item) => item.code === bookmark.translation)
      ? bookmark.translation
      : state.translation;
  state.bookmarks.originVerse = null;
  state.bookmarks.originTopicId = bookmark.topic_id;
  state.bookmarks.originBookmarkId = bookmark.id;
  state.bookmarks.originTopicScrollTop = elements.bookmarksView.scrollTop;
  await openBibleAtVerse(bookmarkVerse(bookmark, translation), {
    exactVerse: bookmark.source === GLOBAL_BOOKMARK_SOURCE,
  });
}

function focusBookmarkListAfterRemoval(index) {
  window.requestAnimationFrame(() => {
    const remaining = [...elements.bookmarkList.querySelectorAll(
      "[data-bookmark-open]",
    )];
    const targetIndex = Math.max(0, Math.min(index, remaining.length - 1));
    (remaining[targetIndex] ?? elements.bookmarkDetailTitle).focus({
      preventScroll: true,
    });
  });
}

function renderBookmarkTopicEditor() {
  const snapshot = bookmarkStore?.snapshot();
  if (!snapshot) {
    elements.bookmarkTopicEditor.replaceChildren();
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const topic of sortedBookmarkTopics(snapshot.topics)) {
    const presentation = bookmarkTopicPresentation(topic);
    const row = document.createElement("div");
    row.className = "bookmark-topic-editor__row";
    row.dataset.topicEditor = topic.id;
    applyBookmarkColor(row, topic.color);

    const identity = document.createElement("div");
    identity.className = "bookmark-topic-editor__identity";
    const dot = document.createElement("span");
    dot.className = "bookmark-dot";
    dot.setAttribute("aria-hidden", "true");
    const name = document.createElement(
      presentation.core ? "span" : "input",
    );
    name.className = `bookmark-topic-editor__name${
      presentation.core ? " bookmark-topic-editor__name--core" : ""
    }`;
    if (presentation.core) {
      name.textContent = presentation.name;
      name.setAttribute(
        "aria-label",
        `${presentation.name}. ${i18n.t("bookmarks.core_topic_name")}`,
      );
    } else {
      name.type = "text";
      name.maxLength = 80;
      name.required = true;
      name.value = topic.name;
      name.dataset.topicName = topic.id;
      name.setAttribute(
        "aria-label",
        i18n.t("bookmarks.rename_aria", { name: presentation.name }),
      );
    }
    identity.append(dot, name);

    const color = document.createElement("input");
    color.className = "bookmark-topic-editor__color";
    color.type = "color";
    color.dataset.topicColor = topic.id;
    color.setAttribute(
      "aria-label",
      i18n.t("bookmarks.color_aria", { name: presentation.name }),
    );
    setBookmarkColorInput(color, topic.color);

    const actions = document.createElement("div");
    actions.className = "bookmark-topic-editor__actions";
    const save = document.createElement("button");
    save.type = "button";
    save.dataset.topicSave = topic.id;
    save.textContent = i18n.t("bookmarks.save_topic");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.dataset.topicDelete = topic.id;
    remove.setAttribute(
      "aria-label",
      i18n.t("bookmarks.remove_topic_aria", { name: presentation.name }),
    );
    remove.textContent = "×";
    actions.append(save, remove);
    row.append(identity, color, actions);
    fragment.append(row);
  }
  elements.bookmarkTopicEditor.replaceChildren(fragment);
}

function onBookmarkTopicCreate(event) {
  event.preventDefault();
  if (!bookmarkStore) {
    return;
  }
  try {
    const requestedName = elements.bookmarkTopicName.value;
    if (
      contributionSync?.canContribute &&
      !isEnglishContributionTopicName(requestedName)
    ) {
      throw new ContributorTopicLanguageError();
    }
    const topic = mutatePersonalBookmarks(() => bookmarkStore.addTopic(
      requestedName,
      elements.bookmarkTopicColor.value,
    ));
    elements.bookmarkTopicForm.reset();
    setBookmarkColorInput(elements.bookmarkTopicColor, topic.color);
    renderBookmarks();
    announce(i18n.t("bookmarks.topic_added"));
  } catch (error) {
    toast(i18n.t(
      error instanceof ContributorTopicLanguageError
        ? "bookmarks.contribution_english_required"
        : error instanceof RangeError
        ? "bookmarks.topic_limit"
        : "bookmarks.invalid_topic",
    ));
  }
}

function restoreDefaultBookmarkTopics() {
  if (!bookmarkStore) {
    return;
  }
  try {
    const result = bookmarkStore.ensureTopics(
      globalBookmarkCatalog.topicDefinitions({ defaultsOnly: true }),
      globalBookmarkTopicMappings(),
    );
    globalBookmarkPreferences?.setTopicMappings(
      result.topic_ids,
      bookmarkStore.snapshot().topics,
    );
    renderBookmarks();
    if (bookmarkStorage) {
      void bookmarkStorage.flush().catch(() => undefined);
    }
    const message = result.topics_added > 0
      ? i18n.plural("bookmarks.default_tags_restored", result.topics_added)
      : i18n.t("bookmarks.default_tags_current");
    elements.bookmarkDefaultTopicStatus.textContent = message;
    bridge.notifySuccess();
    announce(message);
  } catch (error) {
    const message = error instanceof RangeError
      ? i18n.t("bookmarks.default_tags_limit")
      : i18n.t("bookmarks.default_tags_failed");
    elements.bookmarkDefaultTopicStatus.textContent = message;
    bridge.notifyError();
    announce(message);
  }
}

function onBookmarkTopicColorChange(event) {
  const select = event.target.closest("[data-topic-color]");
  if (!select) {
    return;
  }
  const row = select.closest("[data-topic-editor]");
  if (row) {
    applyBookmarkColor(row, select.value);
  }
}

async function onBookmarkTopicEdit(event) {
  const save = event.target.closest("[data-topic-save]");
  const remove = event.target.closest("[data-topic-delete]");
  const topicId = save?.dataset.topicSave ?? remove?.dataset.topicDelete;
  const topic = bookmarkStore?.topic(topicId);
  if (!topic) {
    return;
  }
  if (save) {
    const row = save.closest("[data-topic-editor]");
    const name = row?.querySelector("[data-topic-name]")?.value;
    const color = row?.querySelector("[data-topic-color]")?.value;
    try {
      if (
        contributionSync?.canContribute &&
        !coreBookmarkTopicDefinition(topic.id) &&
        !isEnglishContributionTopicName(name)
      ) {
        throw new ContributorTopicLanguageError();
      }
      mutatePersonalBookmarks(() => bookmarkStore.updateTopic(
        topic.id,
        coreBookmarkTopicDefinition(topic.id) ? { color } : { name, color },
      ));
      renderBookmarks();
      announce(i18n.t("bookmarks.topic_updated"));
    } catch (error) {
      toast(i18n.t(
        error instanceof ContributorTopicLanguageError
          ? "bookmarks.contribution_english_required"
          : "bookmarks.invalid_topic",
      ));
    }
    return;
  }
  if (bookmarkStore.topicCount === 1) {
    toast(i18n.t("bookmarks.last_topic"));
    return;
  }
  const count = bookmarkStore.topicUsage(topic.id);
  const snapshot = bookmarkStore.snapshot();
  const topicName = bookmarkTopicDisplayName(topic);
  const canonicalTopicId = globalBookmarkCatalog.canonicalTopicId(
    topic.id,
    snapshot.topics,
    globalBookmarkTopicMappings(),
  );
  const globalCount = canonicalTopicId &&
    globalBookmarkPreferences?.hasTopic(canonicalTopicId)
    ? globalBookmarkCatalog.bookmarksForTopic(
      topic.id,
      snapshot.topics,
      globalBookmarkTopicMappings(),
    ).length
    : 0;
  const confirmed = await bridge.confirm(
    globalCount > 0
      ? i18n.t("bookmarks.topic_delete_global_confirm", {
        name: topicName,
        personal: count,
        global: globalCount,
      })
      : i18n.plural("bookmarks.topic_delete_confirm", count, {
        name: topicName,
      }),
  );
  if (!confirmed || !bookmarkStore.topic(topic.id)) {
    return;
  }
  if (canonicalTopicId) {
    globalBookmarkPreferences?.disableTopic(canonicalTopicId);
    await globalBookmarkPreferences?.flush();
  }
  mutatePersonalBookmarks(() => bookmarkStore.removeTopic(topic.id));
  renderBookmarks();
  if (state.bible.status === "ready") {
    renderBible();
  }
  announce(i18n.t("bookmarks.topic_removed"));
}

function setBookmarkColorInput(input, selected = null) {
  const value = selected ?? input.value ?? BOOKMARK_TOPIC_COLORS[0];
  input.value = validBookmarkColor(value)
    ? value.toLowerCase()
    : BOOKMARK_TOPIC_COLORS[0];
}

async function clearBookmarks() {
  if (!bookmarkStore || bookmarkStore.size === 0) {
    return;
  }
  const confirmed = await bridge.confirm(i18n.t("bookmarks.clear_confirm"));
  if (!confirmed) {
    return;
  }
  mutatePersonalBookmarks(() => bookmarkStore.clearBookmarks());
  renderBookmarks();
  if (state.bible.status === "ready") {
    renderBible();
  }
  announce(i18n.t("bookmarks.clear_done"));
}

async function loadGlobalBookmarks() {
  if (!bookmarkStore || !globalBookmarkPreferences || globalBookmarkTask) {
    return;
  }
  globalBookmarkTask = (async () => {
    // Yield once so the shared task reference is installed before cleanup.
    await Promise.resolve();
    setGlobalBookmarkBusy(true);
    elements.globalBookmarkStatus.textContent = i18n.t(
      "bookmarks.global_loading",
    );
    try {
      await refreshLiveGlobalBookmarkCatalog();
      const hiddenBookmarkIds = globalBookmarkPreferences.hiddenBookmarkIds;
      const definitions = globalBookmarkCatalog.topicDefinitions();
      const result = bookmarkStore.ensureTopics(
        definitions,
        globalBookmarkTopicMappings(),
      );
      const mappingChanged = globalBookmarkPreferences.setTopicMappings(
        result.topic_ids,
        bookmarkStore.snapshot().topics,
      );
      const preferencesChanged = globalBookmarkPreferences.enableTopics(
        definitions.map((definition) => definition.id),
        globalBookmarkCatalog.version,
      );
      await globalBookmarkPreferences.flush();
      const restoredSnapshot = bookmarkStore.snapshot();
      const restoredBookmarks = hiddenBookmarkIds
        .map((bookmarkId) => globalBookmarkCatalog.bookmarkById(
          bookmarkId,
          restoredSnapshot.topics,
          globalBookmarkTopicMappings(),
        ))
        .filter((bookmark) =>
          bookmark &&
          !globalBookmarkPreferences.isBookmarkHidden(bookmark.id)
        );
      captureGlobalBookmarkAdditions(restoredBookmarks);
      clearBookmarkNavigation();
      renderBookmarks();
      if (state.bible.status === "ready") {
        renderBible();
      }
      // Topic metadata can finish its existing Telegram replication in the
      // background; global associations remain device-local and never enter
      // personal CloudStorage or backup records.
      if (bookmarkStorage) {
        void bookmarkStorage.flush().catch(() => undefined);
      }
      const bookmarks = globalBookmarkCatalog.assignmentCountForTopics(
        bookmarkStore.snapshot().topics,
        globalBookmarkTopicMappings(),
      );
      const changed = preferencesChanged ||
        mappingChanged ||
        result.topics_added > 0 ||
        result.topics_updated > 0;
      const message = i18n.t(
        changed ? "bookmarks.global_loaded" : "bookmarks.global_current",
        {
          bookmarks,
          topics: globalBookmarkCatalog.topicDefinitions().length,
        },
      );
      elements.globalBookmarkStatus.textContent = message;
      bridge.notifySuccess();
      announce(message);
    } catch (error) {
      const message = error instanceof RangeError
        ? i18n.t("bookmarks.global_topic_limit")
        : i18n.t("bookmarks.global_failed");
      elements.globalBookmarkStatus.textContent = message;
      bridge.notifyError();
      announce(message);
    } finally {
      globalBookmarkTask = null;
      setGlobalBookmarkBusy(false);
    }
  })();
  await globalBookmarkTask;
}

async function clearGlobalBookmarks() {
  if (!bookmarkStore || !globalBookmarkPreferences || globalBookmarkTask) {
    return;
  }
  const confirmed = await bridge.confirm(
    i18n.t("bookmarks.global_clear_confirm"),
  );
  if (
    !confirmed ||
    !bookmarkStore ||
    !globalBookmarkPreferences ||
    globalBookmarkTask
  ) {
    return;
  }
  globalBookmarkTask = (async () => {
    // Yield once so the shared task reference is installed before cleanup.
    await Promise.resolve();
    setGlobalBookmarkBusy(true);
    try {
      globalBookmarkPreferences.clear();
      await globalBookmarkPreferences.flush();
      clearBookmarkNavigation();
      renderBookmarks();
      if (state.bible.status === "ready") {
        renderBible();
      }
      const message = i18n.t("bookmarks.global_cleared");
      elements.globalBookmarkStatus.textContent = message;
      bridge.notifySuccess();
      announce(message);
    } catch {
      const message = i18n.t("bookmarks.global_failed");
      elements.globalBookmarkStatus.textContent = message;
      bridge.notifyError();
      announce(message);
    } finally {
      globalBookmarkTask = null;
      setGlobalBookmarkBusy(false);
    }
  })();
  await globalBookmarkTask;
}

async function loadTopicGlobalBookmarks() {
  if (!bookmarkStore || !globalBookmarkPreferences || globalBookmarkTask) {
    return;
  }
  const topicId = state.bookmarks.selectedTopicId;
  globalBookmarkTask = (async () => {
    // Yield once so the shared task reference is installed before cleanup.
    await Promise.resolve();
    setGlobalBookmarkBusy(true);
    try {
      // Revalidate the reviewed overlay on an explicit pull so an already-open
      // Mini App can use an operator publication without a reload.
      await refreshLiveGlobalBookmarkCatalog();
      const snapshot = bookmarkStore.snapshot();
      const hiddenBookmarkIds = globalBookmarkPreferences.hiddenBookmarkIds;
      const canonicalTopicId = globalBookmarkCatalog.canonicalTopicId(
        topicId,
        snapshot.topics,
        globalBookmarkTopicMappings(),
      );
      if (!canonicalTopicId) {
        throw new TypeError("The global bookmark topic is unavailable.");
      }
      globalBookmarkPreferences.enableTopic(
        canonicalTopicId,
        globalBookmarkCatalog.version,
      );
      await globalBookmarkPreferences.flush();
      const restoredBookmarks = hiddenBookmarkIds
        .map((bookmarkId) => globalBookmarkCatalog.bookmarkById(
          bookmarkId,
          snapshot.topics,
          globalBookmarkTopicMappings(),
        ))
        .filter((bookmark) =>
          bookmark?.catalog_topic_id === canonicalTopicId &&
          !globalBookmarkPreferences.isBookmarkHidden(bookmark.id)
        );
      captureGlobalBookmarkAdditions(restoredBookmarks);
      renderBookmarks();
      const bookmarks = globalBookmarkCatalog.bookmarksForTopic(
        topicId,
        snapshot.topics,
        globalBookmarkTopicMappings(),
      ).length;
      const message = i18n.t("bookmarks.topic_global_loaded", { bookmarks });
      elements.bookmarkTopicGlobalStatus.textContent = message;
      bridge.notifySuccess();
      announce(message);
    } catch {
      const message = i18n.t("bookmarks.global_failed");
      elements.bookmarkTopicGlobalStatus.textContent = message;
      bridge.notifyError();
      announce(message);
    } finally {
      globalBookmarkTask = null;
      setGlobalBookmarkBusy(false);
    }
  })();
  await globalBookmarkTask;
}

async function clearTopicGlobalBookmarks() {
  if (!bookmarkStore || !globalBookmarkPreferences || globalBookmarkTask) {
    return;
  }
  const topicId = state.bookmarks.selectedTopicId;
  const snapshot = bookmarkStore.snapshot();
  const canonicalTopicId = globalBookmarkCatalog.canonicalTopicId(
    topicId,
    snapshot.topics,
    globalBookmarkTopicMappings(),
  );
  if (!canonicalTopicId || !globalBookmarkPreferences.disableTopic(canonicalTopicId)) {
    return;
  }
  await globalBookmarkPreferences.flush();
  renderBookmarks();
  const message = i18n.t("bookmarks.topic_global_cleared");
  elements.bookmarkTopicGlobalStatus.textContent = message;
  announce(message);
}

function setGlobalBookmarkBusy(busy) {
  const disabled = Boolean(busy) || !bookmarkStore;
  elements.loadGlobalBookmarks.disabled = disabled;
  elements.loadGlobalBookmarks.setAttribute("aria-busy", String(Boolean(busy)));
  elements.clearGlobalBookmarks.disabled = disabled ||
    !globalBookmarkPreferences?.enabled;
  elements.clearGlobalBookmarks.setAttribute(
    "aria-busy",
    String(Boolean(busy)),
  );
  for (const button of [
    elements.loadTopicGlobalBookmarks,
    elements.clearTopicGlobalBookmarks,
  ]) {
    button.disabled = disabled;
    button.setAttribute("aria-busy", String(Boolean(busy)));
  }
}

function downloadBookmarkBackup() {
  if (!bookmarkStore) {
    return;
  }
  const json = JSON.stringify(bookmarkStore.backup(), null, 2);
  if (bookmarkDownloadUrl) {
    URL.revokeObjectURL(bookmarkDownloadUrl);
  }
  bookmarkDownloadUrl = URL.createObjectURL(
    new Blob([json], { type: "application/json" }),
  );
  const link = document.createElement("a");
  link.href = bookmarkDownloadUrl;
  link.download =
    `getbible-bookmarks-${new Date().toISOString().slice(0, 10)}.json`;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  elements.bookmarkBackupStatus.textContent = i18n.t(
    "bookmarks.backup_ready",
  );
  announce(i18n.t("bookmarks.backup_ready"));
}

async function backupBookmarksToChat() {
  if (!bookmarkStore || bookmarkBackupTask) {
    return;
  }
  bookmarkBackupTask = (async () => {
    setBookmarkBackupBusy(true);
    elements.bookmarkBackupStatus.textContent = i18n.t(
      "bookmarks.backup_sending",
    );
    try {
      await bookmarkStorage?.flush();
      await api.backupBookmarks(bookmarkStore.backup(), idempotencyKey());
      const message = i18n.t("bookmarks.backup_chat_ready");
      elements.bookmarkBackupStatus.textContent = message;
      bridge.notifySuccess();
      announce(message);
    } catch (error) {
      handleSessionError(error);
      const message = i18n.t("bookmarks.backup_failed");
      elements.bookmarkBackupStatus.textContent = message;
      bridge.notifyError();
      announce(message);
    } finally {
      bookmarkBackupTask = null;
      setBookmarkBackupBusy(false);
    }
  })();
  await bookmarkBackupTask;
}

function setBookmarkBackupBusy(busy) {
  const disabled = Boolean(busy) || !bookmarkStore;
  elements.backupBookmarks.disabled = disabled;
  elements.downloadBookmarks.disabled = disabled;
  elements.importBookmarks.disabled = disabled;
}

async function restoreBookmarksFromChat() {
  if (!bookmarkStore) {
    return;
  }
  elements.bookmarkBackupStatus.textContent = i18n.t(
    "bookmarks.restore_loading",
  );
  try {
    const result = await restoreBookmarkBackup({
      fetchRestore: () => api.restoreBookmarks(),
      confirmRestore: (payload) => {
        const source = payload?.source ?? {};
        return bridge.confirm(i18n.t("bookmarks.restore_confirm", {
          bookmarks: Number.isInteger(source.bookmark_count)
            ? source.bookmark_count
            : 0,
          topics: Number.isInteger(source.topic_count)
            ? source.topic_count
            : 0,
          file: typeof source.file_name === "string"
            ? source.file_name
            : "getbible-bookmarks.json",
        }));
      },
      importBackup: (backup, options) => mutatePersonalBookmarks(() =>
        bookmarkStore.importBackup(backup, options)
      ),
      flushPersistence: () => bookmarkStorage?.flush(),
      acknowledgeRestore: () => api.acknowledgeBookmarkRestore(),
    });
    if (result.status === "declined") {
      elements.bookmarkBackupStatus.textContent = "";
      return;
    }
    clearBookmarkNavigation();
    renderBookmarks();
    if (state.bible.status === "ready") {
      renderBible();
    }
    if (!result.acknowledgementError) {
      elements.bookmarkBackupStatus.textContent = i18n.t(
        "bookmarks.restore_done",
      );
    } else {
      handleSessionError(result.acknowledgementError);
      elements.bookmarkBackupStatus.textContent = i18n.t(
        "bookmarks.restore_ack_failed",
      );
    }
    bridge.notifySuccess();
    announce(elements.bookmarkBackupStatus.textContent);
  } catch (error) {
    handleSessionError(error);
    const message = isBookmarkImportLimitError(error)
      ? i18n.t("bookmarks.import_limit")
      : i18n.t("bookmarks.restore_failed");
    elements.bookmarkBackupStatus.textContent = message;
    bridge.notifyError();
    announce(message);
  }
}

async function importBookmarkBackup() {
  const file = elements.bookmarkImportFile.files?.[0];
  elements.bookmarkImportFile.value = "";
  if (!file || !bookmarkStore) {
    return;
  }
  try {
    if (file.size > BOOKMARK_BACKUP_MAX_BYTES) {
      throw new TypeError("Bookmark backup is too large.");
    }
    const backup = JSON.parse(await file.text());
    const result = mutatePersonalBookmarks(() => bookmarkStore.importBackup(
      backup,
      { byteLength: file.size },
    ));
    clearBookmarkNavigation();
    renderBookmarks();
    if (state.bible.status === "ready") {
      renderBible();
    }
    const message = i18n.t("bookmarks.imported", {
      bookmarks: result.bookmarks_added,
      topics: result.topics_added,
      conflicts: result.conflicts_skipped,
      ranges: result.range_markings_skipped,
      notes: result.notes_skipped,
    });
    elements.bookmarkBackupStatus.textContent = message;
    bridge.notifySuccess();
    announce(message);
  } catch (error) {
    const message = isBookmarkImportLimitError(error)
      ? i18n.t("bookmarks.import_limit")
      : i18n.t("bookmarks.import_failed");
    elements.bookmarkBackupStatus.textContent = message;
    bridge.notifyError();
    announce(message);
  }
}

function isBookmarkImportLimitError(error) {
  return error instanceof BookmarkBackupError && /exceed the limit/i.test(
    error.message,
  );
}

function openBookmarkPopover(selectionId, { focusTopicId = null } = {}) {
  const verse = findVerse(selectionId);
  const trigger = elements.bibleVerses.querySelector(
    `[data-bookmark-trigger="${selectionId}"]`,
  );
  const row = trigger?.closest(".reader-verse-row");
  if (!verse || !trigger || !row || !bookmarkStore) {
    return;
  }
  closeBookmarkPopover({ restoreFocus: false });
  const assignments = bookmarkAssignmentsForVerse(verse);
  const activeAssignment = assignments.find(
    (assignment) => assignment.topic_id === focusTopicId,
  ) ?? assignments[0] ?? null;
  const activeTopic = bookmarkStore.topic(
    activeAssignment?.topic_id ?? bookmarkStore.activeTopicId,
  );
  if (!activeTopic) {
    return;
  }
  state.bookmarks.popoverSelectionId = selectionId;
  elements.bookmarkPopoverReference.textContent = verse.reference;
  applyBookmarkColor(elements.bookmarkPopover, activeTopic.color);
  renderBookmarkPopoverAssignments(assignments);
  populateBookmarkTopicPicker(
    new Set(assignments.map((assignment) => assignment.topic_id)),
  );
  row.append(elements.bookmarkPopover);
  row.classList.add("has-bookmark-popover");
  trigger.setAttribute("aria-expanded", "true");
  elements.bookmarkPopover.hidden = false;
  syncBackAction();
  window.requestAnimationFrame(() => {
    positionBookmarkPopover(row);
    const preferredTopic = focusTopicId
      ? elements.bookmarkAssignedTopics.querySelector(
        `[data-bookmark-topic-open="${focusTopicId}"]`,
      )
      : null;
    const target = preferredTopic ??
      elements.bookmarkAssignedTopics.querySelector("[data-bookmark-topic-open]") ??
      (!elements.bookmarkTopicPicker.disabled ? elements.bookmarkTopicPicker : null) ??
      elements.closeBookmarkPopover;
    target.focus({ preventScroll: true });
  });
}

function positionBookmarkPopover(row, attempt = 0) {
  if (!row || elements.bookmarkPopover.hidden) {
    return;
  }
  const viewRect = elements.bibleView.getBoundingClientRect();
  const rowRect = row.getBoundingClientRect();
  const toolbarRect = elements.bibleHeading.hidden
    ? null
    : elements.bibleHeading.getBoundingClientRect();
  const navigationRect = elements.bottomNav.getBoundingClientRect();
  const boundaryTop = Math.max(
    0,
    viewRect.top,
    toolbarRect?.bottom ?? 0,
  ) + 8;
  const boundaryBottom = Math.min(
    window.innerHeight,
    viewRect.bottom,
    navigationRect.top > boundaryTop ? navigationRect.top : window.innerHeight,
  ) - 8;
  const above = Math.max(0, rowRect.top - boundaryTop - 9);
  const below = Math.max(0, boundaryBottom - rowRect.bottom - 9);
  const naturalHeight = Math.min(elements.bookmarkPopover.scrollHeight, 420);
  if (attempt === 0 && Math.max(above, below) < Math.min(naturalHeight, 180)) {
    row.scrollIntoView({ block: "center", behavior: "auto" });
    window.requestAnimationFrame(() => positionBookmarkPopover(row, 1));
    return;
  }
  const placement = below >= naturalHeight || below >= above ? "below" : "above";
  const available = placement === "below" ? below : above;
  elements.bookmarkPopover.dataset.placement = placement;
  elements.bookmarkPopover.dataset.availableHeight = available >= 360
    ? "large"
    : available >= 280
      ? "medium"
      : available >= 200
        ? "compact"
        : "tight";
}

function renderBookmarkPopoverAssignments(assignments) {
  const fragment = document.createDocumentFragment();
  let rendered = 0;
  for (const assignment of assignments) {
    const topic = bookmarkStore?.topic(assignment.topic_id);
    if (!topic) {
      continue;
    }
    const topicName = bookmarkTopicDisplayName(topic);
    const row = document.createElement("div");
    row.className = "bookmark-assigned-topic";
    row.setAttribute("role", "listitem");
    applyBookmarkColor(row, topic.color);

    const open = document.createElement("button");
    open.type = "button";
    open.className = "bookmark-assigned-topic__open";
    open.dataset.bookmarkTopicOpen = topic.id;
    open.setAttribute(
      "aria-label",
      i18n.t(
        assignment.source === GLOBAL_BOOKMARK_SOURCE
          ? "bookmarks.open_global_topic_aria"
          : "bookmarks.open_topic_aria",
        { name: topicName },
      ),
    );
    const dot = document.createElement("span");
    dot.className = "bookmark-dot";
    dot.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.textContent = topicName;
    open.append(dot, name);
    if (assignment.source === GLOBAL_BOOKMARK_SOURCE) {
      const marker = document.createElement("span");
      marker.className = "bookmark-assigned-topic__global";
      marker.textContent = "G";
      marker.title = i18n.t("bookmarks.global_marker");
      marker.setAttribute("aria-hidden", "true");
      open.append(marker);
    }

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "bookmark-assigned-topic__remove";
    remove.dataset.bookmarkAssignmentRemove = assignment.id;
    remove.dataset.bookmarkTopic = topic.id;
    remove.dataset.bookmarkSource = assignment.source === GLOBAL_BOOKMARK_SOURCE
      ? GLOBAL_BOOKMARK_SOURCE
      : "personal";
    remove.setAttribute(
      "aria-label",
      i18n.t(
        assignment.source === GLOBAL_BOOKMARK_SOURCE
          ? "bookmarks.remove_global_topic_assignment_aria"
          : "bookmarks.remove_topic_assignment_aria",
        {
          name: topicName,
          reference: assignment.reference,
        },
      ),
    );
    remove.textContent = "×";
    row.append(open, remove);
    fragment.append(row);
    rendered += 1;
  }
  elements.bookmarkAssignedTopics.replaceChildren(fragment);
  elements.bookmarkAssignedTopics.hidden = rendered === 0;
}

function populateBookmarkTopicPicker(assignedTopicIds) {
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.disabled = true;
  placeholder.selected = true;
  placeholder.textContent = i18n.t("bookmarks.more_topics");
  const topics = bookmarkStore?.snapshot().topics ?? [];
  const eligible = sortedBookmarkTopics(
    topics.filter((topic) => !assignedTopicIds.has(topic.id)),
  );
  const eligibleById = new Map(eligible.map((topic) => [topic.id, topic]));
  const recent = (bookmarkStore?.recentTopicIds ?? [])
    .map((topicId) => eligibleById.get(topicId))
    .filter(Boolean);
  const groups = [];
  if (recent.length > 0) {
    const recentGroup = document.createElement("optgroup");
    recentGroup.label = i18n.t("bookmarks.recent_topics");
    recentGroup.append(...recent.map(bookmarkTopicOption));
    groups.push(recentGroup);
  }
  if (eligible.length > 0) {
    const allGroup = document.createElement("optgroup");
    allGroup.label = i18n.t("bookmarks.all_topics");
    allGroup.append(...eligible.map(bookmarkTopicOption));
    groups.push(allGroup);
  }
  elements.bookmarkTopicPicker.replaceChildren(placeholder, ...groups);
  elements.bookmarkTopicPicker.disabled = eligible.length === 0;
  elements.clearRecentBookmarkTopics.hidden =
    (bookmarkStore?.recentTopicIds.length ?? 0) === 0;
  if (eligible.length === 0) {
    placeholder.textContent = i18n.t("bookmarks.all_topics_assigned");
  }
}

function bookmarkTopicOption(topic) {
    const option = document.createElement("option");
    option.value = topic.id;
    option.textContent = `● ${bookmarkTopicDisplayName(topic)}`;
    option.style.color = topic.color;
    return option;
}

function onBookmarkPopoverTopicAction(event) {
  const remove = event.target.closest("[data-bookmark-assignment-remove]");
  if (remove) {
    removePopoverBookmarkAssignment({
      id: remove.dataset.bookmarkAssignmentRemove,
      topicId: remove.dataset.bookmarkTopic,
      source: remove.dataset.bookmarkSource,
    });
    return;
  }
  const open = event.target.closest("[data-bookmark-topic-open]");
  if (open) {
    openBookmarkTopicFromPopover(open.dataset.bookmarkTopicOpen);
  }
}

function openBookmarkTopicFromPopover(topicId) {
  const selectionId = state.bookmarks.popoverSelectionId;
  const verse = findVerse(selectionId);
  if (!verse || !bookmarkStore?.topic(topicId)) {
    return;
  }
  state.bookmarks.originVerse = bookmarkOriginVerse(verse);
  state.bookmarks.originTopicId = null;
  state.bookmarks.originBookmarkId = null;
  state.bookmarks.originTopicScrollTop = null;
  state.bookmarks.selectedTopicId = topicId;
  state.scrollPositions.set("bookmarks", 0);
  closeBookmarkPopover({ restoreFocus: false });
  setRoute("bookmarks");
  elements.bookmarksView.scrollTop = 0;
  renderBookmarks();
  window.requestAnimationFrame(() => {
    elements.bookmarkDetailTitle.focus({ preventScroll: true });
  });
}

function closeBookmarkPopover({ restoreFocus = true } = {}) {
  const selectionId = state.bookmarks.popoverSelectionId;
  const trigger = selectionId
    ? elements.bibleVerses.querySelector(
      `[data-bookmark-trigger="${selectionId}"]`,
    )
    : null;
  elements.bookmarkPopover.closest(".reader-verse-row")
    ?.classList.remove("has-bookmark-popover");
  elements.bookmarkPopover.hidden = true;
  delete elements.bookmarkPopover.dataset.availableHeight;
  trigger?.setAttribute("aria-expanded", "false");
  state.bookmarks.popoverSelectionId = null;
  if (elements.bookmarkPopover.parentElement !== elements.app) {
    elements.bottomNav.before(elements.bookmarkPopover);
  }
  syncBackAction();
  if (restoreFocus && trigger && document.contains(trigger)) {
    trigger.focus({ preventScroll: true });
  }
}

function applyPopoverBookmark(topicId) {
  const selectionId = state.bookmarks.popoverSelectionId;
  const verse = findVerse(selectionId);
  const topic = bookmarkStore?.topic(topicId);
  if (!verse || !topic || !bookmarkStore) {
    return;
  }
  try {
    mutatePersonalBookmarks(() => bookmarkStore.apply(verse, topic.id));
  } catch (error) {
    const message = error instanceof RangeError
      ? i18n.t("bookmarks.limit_reached")
      : i18n.t("common.request_failed");
    elements.bookmarkTopicPicker.value = "";
    toast(message);
    bridge.notifyError();
    announce(message);
    window.requestAnimationFrame(() => {
      elements.bookmarkTopicPicker.focus({ preventScroll: true });
    });
    return;
  }
  closeBookmarkPopover({ restoreFocus: false });
  renderBookmarks();
  renderBible();
  bridge.notifySelection();
  announce(i18n.t("bookmarks.saved", {
    reference: verse.reference,
    name: bookmarkTopicDisplayName(topic),
  }));
  reopenBookmarkPopover(selectionId, topic.id);
}

function removePopoverBookmarkAssignment({ id, topicId, source }) {
  const selectionId = state.bookmarks.popoverSelectionId;
  const verse = findVerse(selectionId);
  if (!verse || !bookmarkStore?.topic(topicId)) {
    return;
  }
  const removed = source === GLOBAL_BOOKMARK_SOURCE
    ? globalBookmarkPreferences?.hideBookmark(id)
    : mutatePersonalBookmarks(() => bookmarkStore.removeBookmarkTopic(
      id,
      topicId,
    ));
  if (!removed) {
    return;
  }
  if (source === GLOBAL_BOOKMARK_SOURCE) {
    void globalBookmarkPreferences.flush();
    const globalBookmark = globalBookmarkCatalog.bookmarkById(
      id,
      bookmarkStore.snapshot().topics,
      globalBookmarkTopicMappings(),
    );
    if (globalBookmark) {
      captureGlobalBookmarkRemoval(globalBookmark);
    }
  }
  closeBookmarkPopover({ restoreFocus: false });
  renderBookmarks();
  renderBible();
  announce(i18n.t(
    source === GLOBAL_BOOKMARK_SOURCE
      ? "bookmarks.global_removed"
      : "bookmarks.removed_from_topic",
    {
      reference: verse.reference,
      name: bookmarkTopicDisplayName(bookmarkStore.topic(topicId)),
    },
  ));
  reopenBookmarkPopover(selectionId);
}

function reopenBookmarkPopover(selectionId, focusTopicId = null) {
  window.requestAnimationFrame(() => {
    const trigger = elements.bibleVerses.querySelector(
      `[data-bookmark-trigger="${selectionId}"]`,
    );
    if (trigger) {
      openBookmarkPopover(selectionId, { focusTopicId });
      return;
    }
    elements.bibleVerses.querySelector(
      `[data-selection-id="${selectionId}"]`,
    )?.focus({ preventScroll: true });
  });
}

function bookmarkOriginVerse(verse) {
  return {
    selection_id: verse.selection_id,
    translation: verse.translation,
    reference: verse.reference,
    book_number: verse.book_number,
    book_name: verse.book_name,
    chapter: verse.chapter,
    verse: verse.verse,
    text: verse.text,
    highlights: [],
  };
}

function bookmarkVerse(bookmark, translation = bookmark.translation) {
  return {
    selection_id: "",
    translation,
    reference: bookmark.reference,
    book_number: bookmark.book,
    book_name: bookmark.book_name,
    chapter: bookmark.chapter,
    verse: bookmark.verse,
    text: bookmark.text,
    highlights: [],
  };
}

function colorToken(color) {
  const safeColor = validBookmarkColor(color) ? color : BOOKMARK_TOPIC_COLORS[0];
  return safeColor.slice(1);
}

function validBookmarkColor(color) {
  return typeof color === "string" && /^#[a-f0-9]{6}$/i.test(color);
}

function applyBookmarkColor(element, color) {
  const safeColor = validBookmarkColor(color)
    ? color.toLowerCase()
    : BOOKMARK_TOPIC_COLORS[0];
  element.dataset.bookmarkColor = colorToken(safeColor);
  element.style.setProperty("--bookmark-topic-color", safeColor);
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
  clipboard.sync({
    visible: items.length > 0,
    disabled: state.posting,
  });
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
        void api.revokeSession().catch(() => undefined);
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
  searchPageRequests.invalidate();
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
  state.bookmarks.popoverSelectionId = null;
  state.bookmarks.selectedTopicId = null;
  state.bookmarks.originVerse = null;
  state.bookmarks.originTopicId = null;
  state.bookmarks.storageStatus = null;
  bookmarkStore = null;
  bookmarkStorage = null;
  globalBookmarkPreferences = null;
  readingHistory = null;
  cancelHistoryExcerptHydration();
  cancelBookmarkExcerptHydration();
  scriptureExcerpts = null;
  if (bookmarkDownloadUrl) {
    URL.revokeObjectURL(bookmarkDownloadUrl);
    bookmarkDownloadUrl = null;
  }
  clipboard.sync({ visible: false, disabled: true });
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
  if (!elements.bookmarkPopover.hidden) {
    closeBookmarkPopover({ restoreFocus: false });
  }
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
