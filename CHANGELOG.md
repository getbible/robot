# Changelog

All notable GetBible Robot changes are documented here. Dates describe repository changes; production deployment remains a separate reviewed decision.

## Unreleased

### Deployment updates that provably reach the phone

- Every packaged Mini App file — `index.html`, `app.js`, every `lib/*.js`
  module, styles, and locale catalogs — is now served with
  `Cache-Control: no-store`. Telegram WebViews keep their cache between
  launches and do not reliably perform conditional revalidation, which could
  leave a phone running a previous deployment's JavaScript against an
  upgraded server no matter how many fixes were deployed. A launch now always
  downloads the running server's complete module graph, so a manager upgrade
  (menu 16) followed by reopening the Mini App is guaranteed to run the new
  client code; the in-memory session store already forces open views back
  through the launch gate when the upgrade restarts the service.
- The manager's upgrade refresh path now states loudly that no new
  application code is deployed when the source checkout's `HEAD` already
  matches the deployed commit, so a `git pull` that did not advance the
  checkout (wrong branch, nothing new) can no longer look like a completed
  update.
- A dirty source checkout now fails `install`/`upgrade` with an explicit
  explanation instead of dying silently on the clean-tree check.

### Search-style contribution synchronization

- An explicit **Sync now** tap now always performs a real attempt. The
  client's exponential failure backoff (which grows to five minutes) paces
  only automatic retries: previously it also blocked manual taps and showed
  "the server asked us to wait" even though no request had been sent and the
  server had said nothing — the loop behind syncs that appeared permanently
  rate-limited while the application log showed no sync request at all. Only
  a genuine server `Retry-After` still defers a tap, and a request that died
  on the wire is now reported honestly as an interrupted connection.
- Both Mini App credentials are now long-lived, ending forced hourly-scale
  re-authentication: the authenticated session's default absolute lifetime is
  ninety days (`MINI_APP_SESSION_TTL_SECONDS` default `7776000`, new explicit
  range one to one hundred and eighty days, formerly written hour-scale
  values normalized upward without rewriting installed environment files),
  and the contributor `contribution_token` is valid for ninety days as well.
  A fresh Telegram launch still re-authenticates with fresh signed
  `initData`, and withdrawing approval still revokes contributor tokens
  immediately.
- Synchronization requests are now literally search-sized: every batch is
  capped at about 2 KB of JSON, and a request that dies on the wire while
  smaller requests pass is automatically halved — down to one event per
  request — so any network path that carries a search carries a full
  synchronization. This closes the failure family where large uploads were
  silently dropped between the phone and the application.

- The contribution review menu can reinstate a previously revoked or rejected
  contributor. Their record always stayed in the store keyed by their Telegram
  ID, but no menu could restore access and a fresh `/contributor` request
  never resets a removed state on its own; reinstatement queues the enrolment
  notice again and requires the disclosure to be acknowledged anew.

- Reworked Mini App contribution synchronization as the same request class
  the working search flow uses: **Sync now** converts the personal
  topic/assignment state into bounded idempotent events with deterministic
  content-derived IDs, appends the journalled explicit global add/remove
  intents, and drips them to the session-authenticated
  `POST /api/v1/contributions/events` endpoint in sequential batches of at
  most 50 events, obeying the server's `Retry-After` pacing on `429`.
- Made every events response return the complete result set — receipt counts,
  the full detailed contributor status, and the live catalogue
  revision/checksum — so the final batch settles the panel in one round trip.
  The one-time disclosure acknowledgement rides the first batch in the same
  POST body, replacing the separate `PATCH /contributions/status` route.
- Removed the capability-token snapshot transport that broke in production
  three times and the withdrawn Telegram `web_app_data` chunked push
  transport: `POST /api/v1/contributions/sync` now returns `404`, and the
  `gbc_` bearer, `X-Contribution-Token` response header, capability issuance,
  and the dedicated 1 MiB Caddy contribution-sync matcher are gone. Status
  polls remain `GET /contributions/status?details=1`, and the catalogue pull
  keeps its ETag revalidation.
- Gave `POST /api/v1/contributions/events` its own dedicated contributor
  rate budget through the new `CONTRIBUTION_RATE_CAPACITY` (default `60`) and
  `CONTRIBUTION_RATE_REFILL_PER_SECOND` (default `5.0`) settings. Approved
  contributors are a small, individually reviewed group with potentially
  large personal datasets; their batches no longer compete with the public
  search and user limits in either direction, and an over-budget batch waits
  behind `429` and `Retry-After` instead of failing permanently.
- Restored the contributor token as a JSON-body credential: an approved
  contributor receives a `contribution_token` (`gbc_` plus 43
  URL-safe characters, held server-side only as a SHA-256 digest, ninety-day
  lifetime, at most 16 active per contributor, revoked immediately when
  approval is withdrawn) inside status-bearing JSON payloads only — never a
  header. Every event batch must now carry both the session bearer and that
  token or it is refused with `403 contribution_not_allowed` before any
  store work; the client harvests the freshest token from every response and
  recovers a missing or rotated one with a single ordinary status request.
- Showed drip progress ("part X of Y") in the contributor panel while a
  multi-batch synchronization runs.
- Pinned the personal-data inviolability guarantee in a real-browser test:
  a failed or pending synchronization leaves the personal topic and its
  bookmarks untouched, only a topic verifiably published in the live core
  catalogue is ever marked global, and nothing is removed.
- Downgraded a contribution store left at `user_version=6` by the withdrawn
  push transport automatically on open: the two dormant push staging tables
  are dropped and the schema returns to v5 without touching contributor,
  event, or catalogue state.

### Trusted topic contributions and reader polish

- Added private, numeric-Telegram-ID contributor applications through the
  hidden `/contributor` command, one-time sharing disclosure, durable local-first
  Mini App event synchronization, and an auditable per-instance moderation
  queue for English topic proposals and verse-association changes.
- Added the setup-manager review workflow for applications, topic
  merge/correction, and verse decisions with authoritative Query API text.
  Approved work publishes to the running instance immediately; native
  deployments can then generate a privacy-safe export and use a dedicated
  non-root Git account to create, commit, and push a review branch.
- Expanded setup-managed Caddy's exact API allow-list for the live catalogue
  and contribution endpoints. Existing managed-Caddy instances must run this
  release's checkout `setup.sh` for the one-time upgrade; it refreshes the
  routes transactionally and restores the prior Caddy files if cutover fails.
- Made canonical topics permanent after their first live publication, while
  folding pre-publication upsert/delete chains chronologically so superseded
  review events cannot remain stranded.
- Made repository topic metadata a deterministic English source with generated
  localization keys. Newly accepted topics fall back to English in locales
  awaiting a later translation pass, without changing existing translations.
- Kept the reader toolbar flush beneath the translation header, reset chapter
  navigation to unobscured verse one, and retained scroll-down hiding and
  scroll-up restoration.
- Sorted topic-management views alphabetically and added a clearable,
  most-recently-used group above the complete alphabetical verse-assignment
  list.

### Durable global topics and consistent navigation icons

- Mirrored authenticated-scope global-topic visibility, exclusions, and legacy
  mappings between localStorage and Telegram `DeviceStorage`, with timestamp
  reconciliation, migration, tombstones, coalesced bounded retries, and no
  `CloudStorage` access. Telegram Desktop can therefore restore global topics
  after its WebView local storage is discarded.
- Replaced the large global-topic explanation with compact **Add all**,
  **Remove all**, and information controls before topic search, while retaining
  per-topic add/remove and preserving every personal topic and bookmark.
- Replaced the footer's mixed Unicode/SVG artwork with five icons sharing the
  same 24-pixel SVG box on mobile and desktop.

### Localized bookmarks and translation-independent history

- Removed the redundant unconditional **See history** Home action while keeping
  the conditional History summary and permanent footer route.
- Kept bookmark-menu close and assignment-removal controls in separate grid
  cells at narrow widths so their 44-pixel targets cannot overlap.
- Added viewport-aware, progressively rendered verse hydration for global
  bookmark rows and reading-history entries through the existing bounded
  public chapter cache/API. Missing coordinates show a localized unavailable
  state; transient request failures stay distinct and retry when connectivity
  returns. No hydrated verse body enters bookmark or history persistence.
- Made reading-history identity and reopening translation-independent: repeated
  coordinates coalesce across translations, and an entry opens in the
  translation currently selected by the reader.
- Completed the missing Bookmarks, History, and Home-summary catalog coverage
  across every shipped UI catalog. The extension mirrors each existing
  whole-application alias/fallback language source, keeps those policies
  explicit instead of claiming native coverage, and adds the integer `few`
  forms needed by Czech, Polish, Russian, and Ukrainian bookmark counts. The 61
  built-in topic names now use stable localization keys and are read-only while
  remaining recolorable/removable; custom topic names remain user-managed.

### Long reading sessions and browser history

- Raised the authenticated Mini App session default from fifteen minutes to
  a multi-hour lifetime (since superseded by the ninety-day default above).
  Former valid values normalize upward without rewriting installed
  environments, so upgrades fix active readers while old-code rollback
  remains possible.
- Added a dedicated Bible reading-history page backed by bounded, versioned
  browser `sessionStorage`. Successful chapter opens and verse selections keep
  their exact reference and translation; revisiting the same chapter or verse
  moves its existing entry to the top. Readers can reopen an entry, remove one,
  or clear the complete history. Displaying, recording, removing, and clearing
  history remain local; restored reader positions use only the existing preference
  path.
- Added deterministic store, configuration, lifecycle, static-contract, and
  real-Chromium coverage for persistence, bounds, navigation, accessibility,
  and clearing behavior.
- Made History a permanent fifth action in the expandable bottom navigation,
  retained the footer on the History page, aligned its heading and empty state
  with Selected, and reduced its top bar to the centered getBible icon.

### Python 3.14 and Ubuntu 26.04

- Extended the supported runtime and deterministic CI matrix from Python
  3.10–3.12 to Python 3.10–3.14. Python 3.14 host lifecycle and quality checks
  run on Ubuntu 26.04 while Ruff and mypy continue enforcing Python 3.10 source
  compatibility.
- Moved the production container default to Python 3.14 on slim Debian Trixie,
  with the interpreter version available as a build argument.
- Made Python 3.14 the canonical lock-generation interpreter and retained the
  explicit compatibility dependencies needed for exact hashed installs on
  every supported interpreter.

### Scalable native and proxy deployment

- Raised the default production profile for an 8 GiB, i3-class host without
  imposing a RAM or CPU eligibility gate during setup:
  eight lookup workers, four CPU-bound search workers, sixteen concurrent
  updates, a 2 GiB per-instance memory ceiling, and matching task, file, swap,
  CPU, Docker, multi-container, and Kubernetes limits. Every limit remains an
  explicit validated environment or setup override.
- Added real per-instance systemd resource drop-ins and synchronized them
  transactionally through install, configuration, upgrade, rollback, and
  uninstall instead of documenting environment variables that systemd could
  not consume.
- Added generic external reverse-proxy mode for native Mini Apps. Setup asks
  for one public Mini App port on the bot host, binds all host interfaces by
  default so a remote proxy can reach it, skips Caddy, and leaves the
  proxy/firewall boundary under operator control. The mode works with HAProxy,
  Traefik, Nginx, or another HTTP reverse proxy. The listen-address override is
  still available for advanced deployments. Managed Caddy continues to own
  public HTTPS port 443 and forward to loopback.
- Made webhook backends independently configurable. Polling opens no webhook
  listener; webhook mode records a separate specific bind IP and per-instance
  port so a same-host or remote proxy can forward HTTPS without sharing the
  Mini App port.

### Waiting for Scripture instead of reporting a timeout for it

- Stopped charging a search the reference-delivery budget. `LOOKUP_TIMEOUT` is
  twenty seconds, sized for fetching a reference; the index build a search can
  provoke is granted two minutes. The first search of any translation but the
  prewarmed default therefore reported a timeout to the reader while the build
  they had triggered ran on to completion without them — searching Greek, or
  any translation the instance had not warmed, reliably failed on the first
  attempt. Searches now wait on `SEARCH_TIMEOUT`, which defaults to 150 seconds
  and which the loader refuses to start below `SEARCH_INDEX_BUILD_SECONDS` plus
  `SEARCH_DEADLINE_SECONDS`.
- Made the Mini App wait for the budget the robot declares. The page allowed a
  search fifteen seconds while the robot was still working, so it abandoned the
  request and showed a timeout of its own making. The session response now
  carries `limits.search_timeout_seconds` and the page waits it out, falling
  back to a search-shaped floor when an older robot declares nothing.
- Replaced the reader's ten-second wall clock over a whole chapter download with
  a bound on inactivity. The stall timer is rearmed by every chunk of body, so a
  chapter that is still arriving is never abandoned for taking a long time — a
  long chapter on a slow connection is now slow, not failed — while a second,
  much larger ceiling still prevents a connection that trickles forever from
  holding a request open. The abort is raced against the response stream, so a
  deadline can end a read that is already waiting.
- Added bounded retry with exponential backoff to public reads. A stalled
  transfer, a dropped connection, a 429 or a 5xx is attempted again; a 404, an
  oversized body, a malformed payload or a checksum mismatch is raised on the
  first attempt and never retried.
- Stopped loading a chapter's book list and chapter list one after the other.
  They owe each other nothing, and running them in sequence made every cold
  chapter cost one extra public round trip before any text appeared.

### Librarian 2 script-aware search

- Upgraded the reviewed Librarian dependency to 2.0.0 and made that release the
  minimum compatible version.
- Removed the match-mode detector from the service, the command handler, and the
  Mini App API. Librarian 2 derives the matching strategy from the query text,
  so no layer here inspects a query to choose one. The 1.x branch flipped the
  whole query to substring on seeing one continuous-script character, which also
  loosened the space-delimited terms beside it and rewrote the match mode stored
  in the reader's own session.
- Continuous scripts — Han, kana, Hangul, Thai, Lao, Khmer, Myanmar, Tibetan —
  now match under the default filters, which returned nothing under 1.x.
- Adopted the `fold`/`exact` diacritics vocabulary with `fold` as the default,
  so unaccented Greek, unpointed Hebrew and unvowelled Arabic reach the text.
- Accepted the 1.x `insensitive`/`sensitive` spellings wherever a value can
  predate the upgrade, so saved profiles and Mini App clients cached on a device
  keep working.
- Made a stored preference record degrade field by field. An unreadable filter
  blob no longer discards the reader's translation and reading position with it.
- Rebuilt search-term highlighting on Librarian's own folding and script
  classification: no word boundary is tested in a continuous script, only the
  trailing edge is tested for an abjad stem, Brahmic and continuous marks are
  never folded, and precomposed letters that Unicode decomposition cannot reach
  now fold.
- Bounded index construction with the new `SEARCH_INDEX_BUILD_SECONDS` setting
  and spent it on startup prewarming rather than the interactive lookup timeout,
  so one build serves every later search instead of failing the request that
  paid for it and caching nothing.
- Published `getbible_robot_search_engine_version` on `/metrics` so an operator
  can tell a matching-semantics upgrade apart from a regression.
- Added `docs/SEARCH.md` and revised the architecture, configuration,
  dependency, interaction, operations, troubleshooting and upgrade documents.
- Replaced the 1.x match-policy regression tests with coverage asserting that
  twelve scripts reach Librarian exactly as asked, in both match modes, and
  added coverage for the preference migration and highlighting rules.

### Container deployment and small-host hardening

- Added a non-root, read-only, capability-free Docker image that contains no
  Caddy or TLS stack and serves only operator-selected Mini App, health, and
  optional webhook ports.
- Added single-bot and multi-bot container modes. The PID-1 supervisor validates
  unique ports, isolates per-instance cache/state, exposes container management
  commands, checks child liveness and RSS, backs off restarts, and opens a
  restart circuit instead of thrashing the host.
- Made one environment-driven bot per container the default Compose path, with
  only one published Mini App port, structured configuration failures in
  Docker logs, a production secret overlay, bounded Docker log rotation, and
  an explicit multi-bot Compose file.
- Added host setup-manager Docker discovery, deploy, status, log, diagnostics,
  management-menu, and shell commands plus `/app/setup.sh` inside the image.
- Added `docker-init`, `docker-config`, `docker-validate`, and
  Compose-recreating `docker-restart` operations so the versioned Compose file
  and private generated environment remain an editable, validated deployment
  source of truth.
- Exposed application concurrency, cache, session, timeout, rate, abuse,
  memory, CPU, PID, tmpfs, and log-retention controls through the default
  Compose environment without rebuilding the image.
- Added Compose and Kubernetes examples with secrets, persistent state,
  resource limits, and startup/liveness/readiness probes.
- Reduced default worker, update, rate-map, interaction, Mini App session,
  chapter, parsed-translation, and response-byte budgets for a 500 MB host.
- Added per-user Mini App session limits, explicit retained-selection/search
  bounds, Mini App header/body/idle connection limits, process RSS metrics,
  cache-object pruning, and continuously byte-capped JSONL logging.
- Added bounded Mini App client/IP request budgets, trusted-proxy address
  resolution, fractional-cost normal navigation, repeated-abuse blocking,
  private/ephemeral user warnings, and configurable
  disabled/pseudonymous/raw identity audit fields.
- Added structured queue, timeout, circuit, rate/abuse, Mini App request, and
  supervisor memory-pressure events so operators can identify which configured
  barrier is affecting users before increasing limits.
- Changed the systemd unit to native readiness/watchdog notification with
  `MemoryHigh`, a lower `MemoryMax`, a swap ceiling, and restart-storm
  protection.

### Mini App

- Unified the Telegram fullscreen header across every route: Home now uses the
  same centered, scroll-aware GetBible mark and translation control while its
  separate hero logo remains unchanged. Long API translation names shorten
  within a symmetric protected center lane, the visible capsule uses one
  compact shared height without reducing its touch target, and the complete
  name remains available to assistive technology and in the selector.
- Added guarded Telegram Bot API 8.0+ native fullscreen with `expand()` fallback,
  runtime fullscreen/viewport/device/content-safe-area synchronization, and
  complete listener cleanup. Older, unsupported, and rejecting Telegram clients
  continue in the expanded sheet without blocking startup.
- Changed Bible reading navigation so downward scrolling collapses the bottom
  tray to a true arrow-only handle above the device safe area. Upward reading
  no longer reopens the tray, bottom-edge pointer movement is ignored in Bible,
  and selecting or removing a verse deliberately reveals the updated Selected
  state. Other routes retain their existing scroll-aware behavior.
- Replaced the reader's book/chapter dropdown flow with a partial-width,
  translation-aware passage sheet: API-localized book buttons, collision-free
  compact labels, numbered chapter grids through Psalm 150, predictable
  Back/Close/Escape/backdrop/Telegram Back behavior, keyboard and RTL focus
  handling, and immediate chapter opening without hiding the whole reader.
- Made translation and nearest valid reader location one atomic server
  preference transition, made repeated PUTs canonical and retry-safe,
  serialized transitions across every active session for the same user,
  guarded every late client mutation by session generation, and separated the
  complete-chapter selection budget from retained basket entries so long
  chapters cannot expose already-expired verse controls.
- Added per-session and process-wide retained-selection byte/object ceilings,
  including metadata and highlighting terms, aligned translation/chapter
  response contracts with the full validated Main API catalog bounds, and
  added real Chromium coverage for the passage sheet, translation reload, RTL
  placement, focus recovery, request races, and immediate-close persistence.
- Made the header chip the exclusive translation selector, separated Search
  filters from translation and Bible passage selection, and made translation
  changes immediately invalidate old content and reload the same visible
  passage with latest-response-wins request guards.
- Made Search and Scripture reads side-effect-free, serialized and atomically
  validated preference writes, synchronized active sessions, and verified each
  chapter body against its stable published hash before caching it.
- Upgraded the existing Bible tab into a compact full-chapter reader and
  selection surface without adding a command or navigation tab. Bare `/bible`
  resumes a content-free saved location; direct references still post through
  Librarian. Reader chapters use a hash-consistent, response-bounded Main API
  client and a fixed 64-entry cache, while search results can open exact reader
  context or enter the existing basket directly.
- Kept the established translation-driven light/dark interface while adding a
  scroll-aware compact chapter toolbar, edge-friendly verse layout, and a
  responsive bottom navigation that collapses to a safe-area-backed handle.
- Added a production Telegram Mini App for contained Scripture search, filtering,
  Bible navigation, full-text verse-card selection, basket review/reordering,
  and one final server-resolved post. Direct `/bible <reference>` commands keep
  their immediate native path.
- Added **Copy selected verses** using the current server-returned basket. The
  clipboard receives grouped references, translation codes, verse numbers, and
  complete Unicode Scripture as plain text, with safe linked rich text where the
  Telegram WebView supports it; Copy never invokes Telegram posting.
- Added the `getBible.Life` mobile interface with Telegram light/dark themes,
  safe-area and viewport integration, an ocean-light startup treatment, and
  translation-driven language and right-to-left direction metadata.
- Added signed Telegram `initData` validation, one-time owner-bound launch
  tokens, absolute-lifetime bounded sessions and baskets, same-origin APIs,
  recoverable exchange ordering, replay rejection, basket-bound idempotent
  posting, a global fail-before-send output cap, best-effort partial-send
  rollback, a loopback-only web listener, and strict browser security headers.
  The public shell remains inert without Telegram authorization.
- Added transactional setup-manager support, reverse-proxy and BotFather
  instructions, diagnostics, configuration validation, API documentation, and
  Python/JavaScript regression coverage for the complete Mini App lifecycle.
- Added setup-managed Caddy automatic HTTPS with DNS/package preflight,
  deterministic multi-instance routes, complete validation, byte-preserving
  rollback, public certificate/content verification, and isolated route
  removal on disable or uninstall.
- Fixed fresh Ubuntu/Debian installations by completing interrupted package
  configuration, repairing incomplete dependencies, and bootstrapping Caddy's
  official signed APT repository before installing the package. DNF hosts now
  enable Caddy's official COPR repository as well.
- Polished the mobile Mini App by fitting the supplied upright GetBible icon in
  the header, dismissing the phone keyboard for both form-button and keyboard
  Search submissions, recovering an active owner/chat-bound session when
  Telegram recreates the WebView, and replacing expired-launch reload loops
  with explicit close-and-restart guidance.
- Retained both sides of an ephemeral group launch so the user's “Only visible
  to GetBibleBot” command and the bot's “Only visible to you” launcher can both
  be removed together by authenticated readiness, post, expiry, capacity
  eviction, or shutdown. Direct commands retry one unconfirmed transient
  deletion failure; unavailable-message and permission failures remain silent.
- Updated the home hero to “The Holy Word of God” and “Read, find, and share
  His Word,” added complete same-origin interface catalogs for every language
  tag in the GetBible translation inventory, and made translation changes
  immediately rerender static, generated, accessibility, plural, error, and
  right-to-left interface state without reloading the Mini App.

### Security

- Added a transactional multi-instance setup manager with hidden token entry, duplicate-token prevention, exact-commit deployment, isolated locked accounts, root-only configuration, and instantiated unit verification.
- Added per-instance JSONL logs, rotation, instance tagging, metadata-only auditing by default, and explicit privacy-gated content auditing.
- Added hardened templated systemd isolation so instances cannot share application writes, environments, caches, state, logs, ports, or process limits.
- Added owner-scoped opaque callback sessions with TTL/LRU retention and selective-reply verification.
- Added checksum-verified, response-bounded translation/book/chapter/verse navigation metadata.
- Added a rate-limit rejection cooldown so hostile floods cannot amplify Telegram API traffic.
- Rejected the shipped Telegram token template and malformed Bot API token shapes at startup.
- Added strict bounded reference handling through the hardened GetBible Librarian API.
- Added per-user and per-chat rate limits for every command with bounded identifier state.
- Added bounded repository response sizes, retries, connect/read/overall timeouts, worker capacity, and queue waits.
- Added a circuit breaker with readiness integration and one half-open recovery probe.
- Ensured a timed-out synchronous worker retains its capacity permit until the real thread exits.
- Replaced raw exception responses with typed user-safe messages and correlation IDs.
- Added HTML escaping, URL encoding, UTF-16-aware Telegram sizing, safe chunk boundaries, and a maximum output-message count.
- Added startup configuration validation, loopback health endpoints, structured privacy-preserving logs, and a hardened systemd unit.

### Reliability

- Reworked guided `/bible` into a bounded selection basket. Users can add one
  verse, a contiguous range, several separate verses/ranges, or selections from
  other chapters/books; review and remove individual entries; and post the
  compacted selection once.
- Added bounded per-instance persistence for each Telegram user's chosen
  translation. KJV remains the application fallback, while later direct Bible
  references, guided Bible sessions, and searches reuse the saved choice.
- Stopped charging ordinary owner-scoped menu callbacks against command token
  buckets and serialized callbacks per session, preventing normal rapid
  navigation from rate-limiting or racing itself.
- Made default Mandarin/CJK searches reach the text. Librarian 2 indexes
  continuous scripts as positioned character n-grams, so whole-word — the
  default — reaches them without the application detecting the script or
  substituting a match mode.
- Moved complete search verses into full-width selectable result buttons instead
  of duplicating them in the panel text above compact reference buttons. Match
  text is visibly bracketed inside each untruncated button, pages contain up to
  30 blocks, and **All**, **Old**, **New**, and **Other** can rerun the current
  query from the result panel. Selection, private delivery, and explicit final
  posting are unchanged.
- Restored the clean-chat contract for every Scripture workflow. The initiating
  private command and Mini App launcher are claimed together when the
  authenticated session is ready. Direct commands are removed after successful
  Scripture delivery. Post leaves only final Scripture; Copy and Close leave no
  bot-created interaction row, and cleanup failures never alter the user action.
- Replaced public search-result messages with Telegram Bot API 10.2 ephemeral
  commands and per-user group panels. Complete selectable verse blocks now use
  bounded Previous/Next pages of at most 30 results. Only **Post selected**
  sends an ordinary group message, and failed ephemeral delivery never falls
  back to exposing results publicly.
- Extended the same per-user ephemeral group transport to `/bible`. Empty
  `/bible` now keeps translation, book, chapter, verse-range, review, progress,
  and recoverable-error interactions private; direct `/bible <reference>` keeps
  its command private and posts only Scripture. Both paths preserve forum-topic
  routing, and a failed guided post restores the private confirmation controls.
- Restored the original default welcome/help copy with only the completed search
  guidance updated. The original Telegram `/start` entry handler remains
  available but is no longer duplicated in the visible command menu.
- Fixed empty `/bible` failing before the picker opened when an otherwise valid
  translation omitted its optional display-language label. The catalog now
  falls back to its language code, ignores isolated unusable entries, and still
  fails closed if no safe translation remains.
- Added message-free exception-chain classes to request-reference log entries so
  operators can correlate Telegram error references without logging secrets,
  URLs, paths, or upstream content.
- Raised the bounded full-repository response allowance from 8 MiB to 64 MiB
  so KJV and larger translations can be loaded, while retaining an independent
  4 MiB constructed-search-result ceiling.
- Isolated expensive searches in their own worker pool and circuit, and added
  optional default-translation prewarming to remove the normal first-search
  latency from user traffic.
- Added polling/HTTPS-webhook selection and transactional switching, automatic
  Telegram webhook registration, duplicate-poller shutdown without restart,
  and webhook diagnostics.
- Added per-instance editable welcome/help files plus Bot API command, name,
  description, and short-description synchronization.
- Restored compact legacy Scripture formatting with one newline—not a blank
  paragraph—between adjacent verses.
- Fixed hardened deployments failing with systemd `200/CHDIR` by granting the locked instance group read/traverse access while retaining root ownership and no access for other users.
- Added a service-account import preflight to install, upgrade, diagnostics, and the new targeted `repair` operation.
- Added interactive and direct `list`, `start`, `stop`, `restart`, `status`, `runtime`, `logs`, `follow`, `doctor`, `repair`, `config`, `upgrade`, `rollback`, and `uninstall` operations.
- Added prebuilt upgrades, complete application-tree swaps, readiness checks, automatic failed-upgrade restoration, and one-step manual rollback.
- Added transaction cleanup for incomplete installs and safe instance selection when several bots share one server.
- Preserved immediate legacy posting for `/bible <reference>` while replacing empty-command default-verse substitution with a guided picker.
- Added Librarian 1.2 default search for `/search <words>` and a complete filter dashboard for empty `/search`.
- Added bounded multi-select search results; Scripture is posted only after explicit confirmation.
- Added translation-specific testament/book navigation and first/last verse range selection.
- Fixed local secret scanning so the documented in-repository `venv` is excluded.
- Separated the machine-readable API base (`api.getbible.net`) from public Telegram links (`getbible.life`).
- Stopped speculative translation lookups for ordinary or malformed references.
- Made message deletion optional and permission failures non-fatal.
- Limited polling to message and callback-query updates required by the command bot.
- Added clean health, worker-pool, and Librarian-session shutdown behavior.

### Testing and delivery

- Added an attested multi-platform GHCR publishing workflow. Complete CI on
  `master` publishes `edge` plus an immutable commit tag; a published
  `vX.Y.Z` release with green CI/CodeQL gates publishes exact, minor, major,
  and `latest` tags.
- Changed production Compose deployments to pull the selected published image,
  pinned the generated environment to the reviewed release, and retained local
  source builds only through the explicit `compose.build.yaml`/`--build`
  development path.
- Replaced the mocked application-preparation boundary with the real clone, permission-hardening, move, and locked-account preflight path in the lifecycle suite.
- Added a CI host regression that starts with a root-only application tree and proves the actual system account can enter and import it only after the production permission function runs.
- Added a hermetic two-instance setup-manager lifecycle covering transactional failure cleanup, operations, configuration restoration, upgrade recovery, rollback, and isolated uninstall.
- Added an enforced callback-action inventory plus complete reference
  navigation, search-filter, reply, full-result selection, reset, cancel,
  command-registration, lifecycle-hook, and optional Telegram-action regressions.
- Added setup syntax/self-tests, unit-template contracts, audit privacy tests, JSONL formatter tests, and multi-instance documentation contracts.
- Added deterministic catalog integrity, interactive ownership/expiry, picker, search-confirmation, and cooldown regressions.
- Added deterministic parser, service, circuit, timeout, rate-limit, renderer, error, configuration, health, command, and documentation-contract tests.
- Added a Python 3.10–3.12 CI matrix with exact hashed installs.
- Added Ruff, mypy, Bandit, `pip-audit`, secret scanning, systemd verification, CodeQL, and permanent release-gate statuses.
- Added reproducible lock-refresh and local-check scripts.
- Grouped future Dependabot updates to reduce generated-lock conflicts.

### Dependencies

- Updated `actions/checkout` to its reviewed v7 commit.
- Updated `actions/setup-python` to its reviewed v6 commit.
- Updated `actions/upload-artifact` to its reviewed v7.0.1 commit.
- Updated CodeQL init and analyze actions to reviewed immutable commits.
- Updated `python-telegram-bot` from 21.0 to 22.8.
- Updated `python-dotenv` to 1.2.2.
- Updated Ruff to 0.15.22.
- Updated Bandit to 1.9.4.
- Updated `pip-audit` to 2.10.1.
- Updated pip-tools to 7.6.0.
- Updated `types-requests` to 2.33.0.20260712.
- Made non-vulnerable `requests==2.34.2` an explicit runtime dependency.
- Updated mypy to 2.3.0.
- Made Python 3.10's `exceptiongroup` requirement explicit in the universal runtime input.
- Moved to the released `getbible>=2.0.0,<3` policy with `getbible==2.0.0` selected and hashed in both locks.

### Documentation

- Replaced the manual single-instance deployment path with the setup questionnaire and complete multi-instance installation, operations, upgrade, rollback, logging, diagnostics, troubleshooting, and removal contract.
- Added the interactive command contract, search filters, group behavior, safety model, and rollout roadmap.
- Added complete installation, configuration, testing, dependency, upgrade, rollback, uninstall, troubleshooting, architecture, operations, security, and release-gate documentation.
