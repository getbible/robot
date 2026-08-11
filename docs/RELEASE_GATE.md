# Security and reliability release gate

A commit is deployable only when every applicable item below is satisfied on the exact pull-request head. Automated checks are necessary but do not replace review of the source, tests, workflows, dependencies, and documentation.

## Source integrity

- The intended commit is identified by full SHA.
- The branch contains one coherent implementation, not competing temporary workflows or divergent feature branches.
- No generated patch payload, migration worker, debug artifact, obsolete code path, or contradictory documentation remains.
- No test, assertion, supported Python version, coverage floor, or security job is removed merely to obtain green checks.
- Module responsibilities and dependency direction remain explicit and cycle-free.

## Active Mini App doctrine

- Only full-text search and search pagination use Robot/Librarian.
- Translation metadata, books, chapters, chapter text, and hashes use `api.getbible.net/v2` directly from the browser.
- Explicit and grouped references use `query.getbible.net/v2` directly from the browser.
- Public API requests contain no Telegram or Robot credentials.
- `BrowserSelectionStore` solely owns select, unselect, reorder, clear, counters, highlighting, and copy state.
- Reader and search verses share coordinate identity.
- No Robot basket or Scripture mutation occurs before final Post.
- Browser text and references are never final posting authority.

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

- Telegram `initData` signature, age, user, chat, chat instance, and launch ownership are validated.
- Launches and sessions are short-lived, bounded, and owner-bound.
- The bot token never enters HTML, JavaScript, URLs, logs, browser storage, or public API traffic.
- Public cache contains no identity, session, preference, search, selection, or posting data.
- Structured logs contain no tokens, init data, verse bodies, or repository payloads.
- Forwarded client addresses are accepted only from configured trusted proxies.

## Runtime and concurrency

- Python 3.10, 3.11, and 3.12 deterministic suites pass.
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
