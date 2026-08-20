# Testing

The release gate verifies the Robot runtime and the browser data plane as one deployable system. Only a final live Telegram acceptance test requires a bot token.

## Exact development environment

```bash
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
venv/bin/python -m pip check
cd miniapp
npm ci --ignore-scripts
npx playwright install --with-deps chromium
cd ..
```

Use the pinned Python, Node, npm, Playwright, and Chromium versions from the repository. Global packages may not substitute for locked dependencies.

## Complete gate

```bash
bash scripts/run-checks.sh
```

This command is the local equivalent of the permanent CI quality job. It must pass before a branch is considered deployable.

## Required suites

### Robot runtime

- deterministic Python tests on Python 3.10, 3.11, 3.12, 3.13, and 3.14,
  including Python 3.14 host-native lifecycle checks on Ubuntu 26.04;
- strict mypy and Ruff;
- enforced branch coverage;
- setup-manager and lifecycle checks;
- production container build and smoke test;
- Bandit/static security;
- exact dependency audit;
- secret scan;
- systemd verification;
- CodeQL.

### Browser Scripture plane

Tests must prove:

- translations, books, chapters, chapter text, and hashes use `api.getbible.net/v2` directly;
- explicit and grouped references use `query.getbible.net/v2` directly;
- no Telegram init data, Robot token, cookie, or credential reaches either public origin;
- the CSP meta element and response header contain the same fixed allowlist;
- redirects, oversized responses, malformed schemas, coordinate mismatches, and checksum mismatches are rejected;
- exact-scope hashes are stored and revalidated at least weekly;
- parent changes invalidate descendants;
- atomic cache replacement preserves the last valid record on failure;
- cold and warm IndexedDB paths produce the same normalized chapter model;
- a response that is still arriving is never abandoned, however long it takes,
  and one that has gone silent is abandoned as a retryable timeout;
- reads that may succeed on a second attempt are retried, and refusals that
  will repeat are raised on the first.

### Search boundary

Tests must prove:

- full-text search and pagination alone use Librarian, through Robot;
- a Librarian failure does not affect reader navigation;
- stale search and pagination responses cannot overwrite newer state;
- search verses normalize to the same descriptor used by reader verses;
- a query reaches Librarian with the match mode the reader asked for, in every
  writing system, so no layer reintroduces a match-mode detector;
- both diacritics vocabularies are accepted, and a profile stored before the
  Librarian 2 upgrade keeps its translation and reading position;
- a search is charged the search budget and not the reference-delivery budget,
  so the first search of a translation waits for the index build it provokes;
- the loader refuses a `SEARCH_TIMEOUT` shorter than the build it must cover;
- the session response declares that budget and the page waits it out, falling
  back to a search-shaped floor when a robot declares nothing.

### Browser selection domain

`BrowserSelectionStore` tests must cover:

- add, remove, reorder, and clear;
- bounded capacity;
- defensive snapshots;
- deterministic coordinate projection;
- coordinate deduplication across reader and search transport IDs;
- removal through either source token;
- no Robot request during select, unselect, reorder, clear, copy, or rendering;
- selected state surviving reader navigation within the active WebView session.

### Browser reading history

`ReadingHistoryStore` tests must cover:

- successful chapter and selection visits in newest-first order;
- stable-id move-to-front deduplication for repeat chapters and exact verses,
  including across event kinds, plus verse and translation separation and a
  full-capacity revisit;
- exact translation/book/chapter/verse restoration;
- the 1,000-entry bound and defensive snapshots;
- versioned authenticated-scope `localStorage` restoration, including legacy
  duplicate compaction, with memory fallback;
- malformed persistence rejection;
- individual removal and complete reset, including removal of the storage key;
- absence of verse bodies, Telegram identity, launch data, and Robot tokens;
- no Robot request while opening the History page, recording, removing, or
  clearing history; choosing an entry may use only the existing reader
  preference path.

### Bookmark and hybrid-storage domain

`BookmarkStore` and `TelegramBookmarkStorage` tests must cover:

- the shipped default topic definitions plus bounded add, rename, recolor,
  removal warnings, and restoration of missing defaults without overwriting
  user changes;
- at most 800 personal records, each unique by canonical
  book/chapter/verse across translations and assignable to multiple topics
  without consuming another verse slot;
- aggregate version 2, maximum topic/bookmark counts, and compact CloudStorage
  topic indexes that fit Telegram per-value limits;
- compact backup version 4 `colorIndexes`, version 1, 2, and 3 import,
  compatible-format merge, worst-case UTF-8 pretty-JSON size,
  cross-translation deduplication, malformed input rejection, and no partial
  mutation on failure;
- the 2,155-link/61-topic global catalog, unified personal/global ordering and
  **G** marker, browser-local exclusions, and idempotent per-link,
  per-topic, and all-catalog reset without personal sync or backup, including
  renamed numeric-topic remapping;
- authenticated user-scoped local keys with no raw Telegram user identifier;
- newest-timestamp startup reconciliation across `localStorage`, Telegram
  `DeviceStorage`, and Telegram `CloudStorage`, including deterministic ties,
  write coalescing, manifest-last cloud commits, and partial/unavailable API
  fallback;
- compact last-read reconciliation containing no chapter body, history,
  selection, session token, or Telegram init data, including a failed remote
  clear followed by tombstone reconciliation without stale resurrection;
- history and the public Scripture cache never being written through the
  Telegram adapter.

### Bookmark chat backup and restore

Backend and browser tests must prove:

- authenticated personal backup JSON uses v4 `colorIndexes`, accepts v1/v2/v3 on
  import, is schema-validated, canonicalized, size/count bounded, excludes
  global links, and is delivered idempotently to the requesting user's private
  bot chat;
- ambiguous or incomplete delivery attempts cannot create an uncontrolled
  retry;
- the document callback is owner-bound and accepted only from the owner's
  private bot chat;
- a callback validates Telegram file metadata and creates a fresh, short-lived,
  one-time Bookmarks launch;
- restore fetch validates the Telegram document again, merge requires explicit
  confirmation, persistence flush completes before DELETE acknowledgement, and
  acknowledgement consumes only that launch's restore reference;
- backup bodies never enter Robot database/session state, structured logs, or
  public browser cache.

### Real Chromium acceptance

The pinned Chromium test exercises the production HTML, CSP, JavaScript
modules, and browser APIs. It verifies:

1. the reader loads from Main API and chapter navigation works;
2. History is reachable from every footer route and keeps the footer visible;
3. History matches Selected's heading and empty-state layout, with a centered
   getBible icon and no translation or Close control;
4. revisiting a history coordinate moves it to the top without increasing the
   count, and exact-coordinate navigation, removal, reset, and empty-state
   navigation work;
5. Home shows Search, Bible, and History actions plus the Bookmarks summary,
   and Home/History/Selected/Bookmarks use the icon-only top bar;
6. selecting a reader verse reveals a compact bottom-right ellipsis without
   opening the menu; activating it opens the anchored menu, multi-topic
   assignment updates the reader, topic navigation returns to all topics or the
   source verse, and the five-item footer remains usable;
7. an explicit chat-backup action sends the current bookmark document through
   the authenticated Robot endpoint; and
8. personal and global links share one topic list, global links carry **G**,
   per-link hide and per-topic/all reset are browser-local, and no global link
   enters personal backup or sync; and
9. no legacy Robot Scripture or history request is emitted.

Focused model, storage, API, and backend tests separately verify selection
identity and unselect state transitions, Post authority and server failure
semantics, restore/import persistence, and acknowledgement ordering. Visual
selection state, browser selection behavior after Post, complete topic-view
refresh, and chat-failure fallback remain live smoke requirements below.

### Post authority

Backend tests must prove:

- the submitted selection is bounded and ordered;
- malformed, duplicate, or unavailable coordinates are rejected;
- browser text, names, references, and UI IDs are not authoritative;
- authoritative Scripture is obtained before rendering;
- idempotency binds to the exact ordered selection;
- ambiguous external outcomes remain locked;
- output is escaped and split by Telegram UTF-16 limits;
- known partial sends are rolled back best-effort.

## Failure injection

Inject and verify independent failures for:

- Main API timeout and malformed content;
- Query API timeout and unresolved references;
- IndexedDB failure with in-memory fallback;
- cache hash changes during download;
- Librarian search timeout;
- Robot session expiry;
- unavailable or corrupt scoped browser-local history storage;
- unavailable, partial, stale, or corrupt Telegram DeviceStorage/CloudStorage;
- oversized, malformed, wrong-owner, missing, or changed bookmark backup
  documents;
- restore persistence or acknowledgement failure;
- final Telegram send failure.

Each failure must remain inside its ownership boundary. Public API errors must not invalidate authentication; search errors must not disable reading; Post errors must not erase local selections.

## Live Telegram smoke test

After all deterministic gates pass, deploy one validated commit and verify in Telegram:

- bare `/bible` opens the reader;
- direct reference launch resolves correctly;
- chapter selection and navigation work;
- selected styling and unselect behavior work;
- Search and Reader selections interoperate;
- Copy preserves selection;
- Post delivers authoritative Scripture to the originating chat/topic;
- failed Post preserves the browser selection;
- successful Post clears the browser selection;
- history reopens the exact verse/translation and can be cleared per-entry or
  completely;
- history remains on the same browser after reopening but is absent on a clean
  second device;
- multi-topic personal assignment, topic management/default restoration, the
  800-record bound, and cross-translation canonical deduplication work;
- personal bookmarks and last-read synchronize on a second supported Telegram
  client;
- the unified topic list marks global links with **G** and supports per-link
  hide plus per-topic/all reset without global sync or backup;
- unchanged CloudStorage items remain stable, partial commits are rejected by
  their metadata fingerprint, and transient writes retry without another user
  action;
- JSON v4 download/import and private-chat backup/restore work, including
  v1/v2/v3 import, owner validation, confirmation, and persistence before
  acknowledgement;
- when private-chat backup transport fails, local JSON Download and Import stay
  enabled and usable;
- private command and launcher cleanup still behaves as documented.

Record the deployed commit SHA and CI/CodeQL run links with the release evidence.
