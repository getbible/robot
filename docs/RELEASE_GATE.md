# Security and reliability release gate

A commit is deployable only when every applicable item below is satisfied on the exact pull-request head. Automated checks are necessary but do not replace review of the source, tests, workflows, dependencies, and documentation.

## Source integrity

- The intended commit is identified by full SHA.
- The branch contains one coherent implementation, not competing temporary workflows or divergent feature branches.
- No generated patch payload, migration worker, debug artifact, obsolete code path, or contradictory documentation remains.
- No test, assertion, supported Python version, coverage floor, or security job is removed merely to obtain green checks.
- Module responsibilities and dependency direction remain explicit and cycle-free.

## Active Mini App doctrine

- Only full-text search and search pagination use Librarian; Robot's other
  protected paths are authentication, preference compatibility, final Post,
  and explicit bookmark chat backup/restore.
- Translation metadata, books, chapters, chapter text, and hashes use `api.getbible.net/v2` directly from the browser.
- Explicit and grouped references use `query.getbible.net/v2` directly from the browser.
- Public API requests contain no Telegram or Robot credentials.
- `BrowserSelectionStore` solely owns select, unselect, reorder, clear, counters, highlighting, and copy state.
- Reader and search verses share coordinate identity.
- No Robot basket or Scripture mutation occurs before final Post.
- Browser text and references are never final posting authority.
- Bookmarks and last-read are compact user state, not public Scripture cache
  entries; only an explicit backup/restore action sends bookmark JSON through
  Robot.

Any code, test, OpenAPI path, or documentation that presents the former Robot-proxied reader or per-click server basket as the active design blocks release.

## Browser transport and cache

- Main API and Query API origins are fixed HTTPS constants.
- Redirects are rejected; credentials are omitted; `no-referrer` is used.
- Request timeouts and response-size limits are enforced.
- The HTML CSP and Tornado response CSP contain identical public-origin allowlists.
- IndexedDB records are identity-free and versioned.
- Record count, total bytes, per-record bytes, and in-flight requests are bounded.
- Exact-scope SHA-1 values are stored and revalidated at least weekly.
- Translation and book hash changes invalidate descendants.
- Chapter replacement is atomic and requires stable pre/post hashes plus exact-byte verification.
- Failed refreshes preserve the last valid record.

## Browser selection behavior

- Selection capacity is bounded.
- Snapshots are defensive copies.
- Reader and Librarian transport IDs deduplicate by translation/book/chapter/verse.
- Selecting updates `aria-pressed`, verse number styling, verse body styling, range boundaries, counters, and navigation badges immediately.
- A second click removes the selection.
- Navigating away and back preserves selected styling for the active WebView session.
- Reorder, clear, and copy are local.
- A failed Post preserves the complete ordered selection.
- A successful Post clears it.

## Browser reading history

- Successful chapter opens and successful selection additions record history;
  an existing chapter or exact verse moves to the front without increasing the
  entry count, including an exact-coordinate revisit across event kinds.
- Entries contain bounded coordinates and display metadata, never verse bodies,
  Telegram identity, launch data, or Robot credentials.
- History is versioned, unique and newest-first, limited to 1,000 entries, and
  stored in authenticated user-scoped browser `localStorage` with memory
  fallback. It survives WebView sessions on that browser but never synchronizes
  through Telegram or Robot.
- History is a permanent fifth bottom-navigation route. Its normal page keeps
  the footer visible, follows Selected's heading and empty-state layout, and
  shows only the centered getBible icon in the top bar.
- Each entry shows verse text for the current translation, and choosing it
  restores the exact coordinate without changing that translation.
- Individual removal and full reset are local, and full reset removes the
  storage key.
- Displaying, recording, removing, and clearing history issue no Robot request.
  Choosing an entry may persist the existing reader preference, but uses no
  history or Scripture-content route.

## Bookmark and last-read persistence

- A selected Bible verse exposes an anchored, accessible topic menu without
  changing the five-item footer.
- Home exposes Search and Bible primary actions plus conditional Selected,
  History, and Bookmarks summaries. Only Search and Bible show the translation
  control; Home, History, Selected, and Bookmarks use the centered icon-only
  top bar.
- Home, Search, Bible, History, and Selected use one shared 24-pixel SVG icon
  box at mobile and desktop widths.
- Topic and bookmark counts, identifiers, names, colors, text excerpts, and
  serialized values are bounded.
- Whole-verse bookmark identity is canonical book/chapter/verse across
  translations; reassignment updates the existing bookmark rather than
  duplicating it.
- Topic search/detail, the add-topic plus-card, localized read-only built-in
  names, detail-level built-in/custom recoloring, custom inline rename with
  confirm/cancel, topic removal warnings, and bookmark
  assign/move/reopen/remove/clear behavior derive from `BookmarkStore`.
- The newest valid timestamped bookmark aggregate reconciles deterministically
  across scoped browser `localStorage`, Telegram `DeviceStorage`, and Telegram
  `CloudStorage`; writes are coalesced and partial API failure preserves an
  available valid copy.
- The compact last-read record contains only translation, book, chapter, verse,
  version, and timestamp, or a timestamped cleared marker, and uses the same
  local/device/cloud strategy without stale-position resurrection.
- Compact global-topic add-all/remove-all controls precede topic search, while
  per-topic controls remain available and whole-topic removal is recoverable
  through **Add all**. Their scoped preferences restore from Telegram
  `DeviceStorage` after local WebView storage loss and never access
  `CloudStorage`, personal bookmark sync, or backup documents.
- The **G** marker is visually centered, and an approved-contributor-only
  **Manage Contribution** panel appears directly below Global topics,
  separate from the add-topic form.
- History, selections, translation catalogs, chapters, and public cache entries
  never enter Telegram user storage.

## Contribution synchronization

- **Sync now** submits session-authenticated bounded idempotent batches of at
  most 50 contribution events — each request additionally capped at about
  2 KB, the proven size class of a search request — to the same-origin
  `POST /api/v1/contributions/events` endpoint, sequentially; the drip obeys
  the server's `Retry-After` pacing on `429` and halves a batch that dies on
  the wire, down to single events, so any network path that carries a search
  carries a synchronization.
- Every response returns the complete result set: receipt counts, the full
  detailed contributor status, and the live catalogue revision/checksum, so
  the final batch settles the panel in one round trip. Unavailable catalogue
  enrichment cannot turn a committed batch into a reported failure.
- A redelivered event replays idempotently per contributor and
  `client_event_id`; a reused ID with different content fails closed.
- Contributor authority and the acknowledged disclosure are rechecked in the
  durable SQLite store on every batch; the acknowledgement itself rides the
  first batch of the synchronization.
- Both credentials are required on every batch: the ordinary opaque session
  bearer plus a valid `contribution_token` in the POST body. A request
  missing either is refused with `403 contribution_not_allowed` before any
  store work, and only approved contributors ever receive a token.
- The contributor token travels only inside JSON payloads — the session
  bootstrap's `contributions` object, the detailed status response, and every
  events response's `status` — never in a custom header.
- Contribution batches draw on their own env-tunable budget
  (`CONTRIBUTION_RATE_CAPACITY`, `CONTRIBUTION_RATE_REFILL_PER_SECOND`),
  separate from the public search and user limits; an over-budget batch
  waits behind `429` and `Retry-After`, it never fails permanently.
- An explicit **Sync now** tap always performs a real request. The client's
  own failure backoff paces only automatic retries; a person is deferred
  solely while a genuine server `Retry-After` is in force, and a request that
  died on the wire is reported as an interrupted connection, never as a
  server instruction.
- Personal topics and bookmarks are never altered by any synchronization
  outcome: a failed or pending synchronization leaves them untouched, only a
  topic verifiably published in the live core catalogue is ever marked **G**,
  and nothing is removed.
- No WebSocket, extra port, custom header, or special body budget
  participates; every batch fits the ordinary 64 KiB API bound.

## Bookmark portability and chat recovery

- Browser JSON download/import is versioned, merge-safe, and bounded.
- Chat backup is an explicit authenticated, idempotent action that validates
  and canonicalizes the document before sending it to the user's private bot
  chat.
- The durable Telegram document carries an owner-bound Restore callback; the
  callback is accepted only from the owner's private chat and validates its
  file metadata.
- Every Restore callback creates a fresh, short-lived, one-time Bookmarks
  launch. Restore revalidates the downloaded document, requires user
  confirmation, persists the merge, and only then acknowledges that launch's
  restore reference.
- A transport or acknowledgement failure leaves existing bookmarks and the
  original chat document recoverable.
- Robot database/session state and structured logs never contain a bookmark
  backup body; a pending restore retains bounded Telegram file metadata only.

## Search isolation

- Search and pagination alone use Librarian.
- Search has independent executor, semaphore, timeout, cache, and circuit behavior.
- Search failure does not affect reader navigation.
- Stale responses cannot overwrite current query/filter state.
- Search output is bounded and normalized before registration in the browser selection store.
- The requested match mode reaches Librarian unaltered in every writing system.
- Index construction is bounded separately from a request deadline.

## Final Post authority

- Post accepts one bounded ordered selection snapshot.
- Malformed, duplicate, unavailable, or mixed-owner input is rejected.
- Browser text, names, references, and UI IDs cannot determine Telegram output.
- Authoritative Scripture is obtained and validated before rendering.
- Idempotency binds to the exact ordered selection.
- The first Telegram send occurs only after complete validation and rendering.
- Known partial sends are rolled back best-effort.
- Ambiguous outcomes remain locked rather than being retried under another key.
- Output is escaped, linked safely, and split by Telegram UTF-16/message-count limits.

## Authentication and privacy

- Telegram `initData` signature, age, user, chat, chat instance, and launch
  ownership are validated at the initial session exchange; later requests use
  Robot-issued opaque bearers instead of forwarding raw `initData` again.
- Launches and sessions are bounded and owner-bound; authenticated Mini App
  sessions have a ninety-day default absolute lifetime.
- The bot token never enters HTML, JavaScript, URLs, logs, browser storage, or public API traffic.
- Public cache contains no identity, session, preference, search, selection, or posting data.
- Public cache contains no history, bookmark, last-read, or backup data.
- Structured logs contain no tokens, init data, verse bodies, repository
  payloads, or bookmark backup bodies.
- Forwarded client addresses are accepted only from configured trusted proxies.

## Runtime and concurrency

- Python 3.10, 3.11, 3.12, 3.13, and 3.14 deterministic suites pass.
- Ruff, strict mypy, and branch coverage pass.
- Fixed executors and semaphores prevent unbounded queued work.
- Timeout cancellation does not prematurely release real worker capacity.
- Session, search, preference, and Post state transitions are serialized at their owning boundary.
- Shutdown stops ingress, drains real workers, closes clients, and completes Telegram shutdown.

## Container and host deployment

- The production image installs the exact hashed lock and runs unprivileged.
- The root filesystem is read-only; capabilities are dropped; privilege escalation is disabled.
- CPU, memory, PID, tmpfs, restart, and graceful-shutdown bounds remain enforced.
- Host Mini App, webhook, and health listeners remain separate and loopback/private behind ingress.
- Caddy/systemd setup remains transactional and rollback-safe.
- Managed Caddy forwards the bounded Mini App `/api/v1/*` namespace for both
  root and nested public URLs, applies the single 5 MiB bookmark-backup
  exception before the general 64 KiB matcher, and leaves the backend router
  deny-by-default.
- One bot token has one active polling/webhook workload.
- Production container build and smoke test pass.
- systemd verification passes.

## Security and supply chain

- Exact Python and npm dependency installs pass integrity checks.
- Dependency vulnerability audit passes.
- Static security scan passes.
- Secret scan passes without weakening rules or hiding real findings.
- CodeQL passes.
- Workflow actions remain pinned to immutable commit SHAs.

## Documentation

- `README.md`, `docs/ARCHITECTURE.md`, `docs/BROWSER_DATA.md`, `docs/MINI_APP.md`, `docs/INTERACTIONS.md`, `docs/TESTING.md`, API contracts, and operational guides describe the same active architecture.
- No example directs reader traffic through Robot.
- No example treats Robot session tokens as verse identity.
- No example documents per-click basket persistence as active behavior.
- Deployment and rollback instructions match the current source and artifacts.

## Permanent CI evidence

The exact PR head must have successful permanent runs for:

- CI runtime matrices;
- quality and security;
- production container;
- real Chromium Mini App acceptance;
- CodeQL.

The pull request remains draft while any required check is missing, cancelled, stale, or failing. Mark it ready only after the exact head is green and the diff has been reviewed for dormant or contradictory implementation.
