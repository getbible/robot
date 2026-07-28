# Changelog

All notable GetBible Robot changes are documented here. Dates describe repository changes; production deployment remains a separate reviewed decision.

## Unreleased

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

- Added a production Telegram Mini App for contained Scripture search, filtering,
  Bible navigation, full-text verse-card selection, basket review/reordering,
  and one final server-resolved post. Direct `/bible <reference>` commands keep
  their immediate native path.
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
- Retained both sides of an ephemeral group launch so a completed Mini App
  workflow removes the user's "Only visible to GetBibleBot" command as well as
  the bot's "Only visible to you" launch response. Immediate cleanup remains in
  place, and final cleanup retries each row independently and non-fatally.
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
- Added unsegmented-script detection so default Mandarin/CJK searches use
  substring matching instead of whitespace-dependent whole-word matching.
- Moved complete search verses into full-width selectable result buttons instead
  of duplicating them in the panel text above compact reference buttons. Match
  text is visibly bracketed inside each untruncated button, pages contain up to
  30 blocks, and **All**, **Old**, **New**, and **Other** can rerun the current
  query from the result panel. Selection, private delivery, and explicit final
  posting are unchanged.
- Restored the legacy clean-chat contract for every Scripture-producing path.
  Direct `/bible`, guided `/bible`, and `/search` now remove only their recorded
  initiating command, bot panels, prompts, acknowledgements, and prompt replies
  after all Scripture chunks are delivered; final Scripture remains, failures
  preserve recovery context, and Telegram deletion failures are non-fatal.
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
- Moved to the released `getbible>=1.2,<2` policy with `getbible==1.2.0` selected and hashed in both locks.

### Documentation

- Replaced the manual single-instance deployment path with the setup questionnaire and complete multi-instance installation, operations, upgrade, rollback, logging, diagnostics, troubleshooting, and removal contract.
- Added the interactive command contract, search filters, group behavior, safety model, and rollout roadmap.
- Added complete installation, configuration, testing, dependency, upgrade, rollback, uninstall, troubleshooting, architecture, operations, security, and release-gate documentation.
