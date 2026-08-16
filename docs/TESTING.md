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

- full-text search and pagination alone use Robot/Librarian;
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

### Real Chromium acceptance

The pinned Chromium test must exercise the production HTML, CSP, JavaScript modules, and browser APIs. It must verify:

1. the reader loads from Main API;
2. chapter navigation works;
3. selecting a verse immediately sets `aria-pressed=true` and selected styling;
4. the verse number and body retain the selected visual state;
5. navigating away and back preserves highlighting;
6. the same verse from Search and Reader is one selection;
7. a second click unselects it;
8. no Robot basket or Scripture request occurs before Post;
9. failed Post preserves selection;
10. successful Post clears selection.

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
- successful Post clears the browser selection;
- private command and launcher cleanup still behaves as documented.

Record the deployed commit SHA and CI/CodeQL run links with the release evidence.
