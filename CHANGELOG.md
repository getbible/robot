# Changelog

All notable GetBible Robot changes are documented here. Dates describe repository changes; production deployment remains a separate reviewed decision.

## Unreleased

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

- Restored the legacy clean-chat contract for every Scripture-producing path.
  Direct `/bible`, guided `/bible`, and `/search` now remove only their recorded
  initiating command, bot panels, prompts, acknowledgements, and prompt replies
  after all Scripture chunks are delivered; final Scripture remains, failures
  preserve recovery context, and Telegram deletion failures are non-fatal.
- Replaced public search-result messages with Telegram Bot API 10.2 ephemeral
  commands and per-user group panels. Complete highlighted verses now use
  bounded Previous/Next pages of at most 30 results, automatically reducing a
  page rather than truncating a verse when Telegram's text limit requires it.
  Only **Post selected** sends an ordinary group message, and failed ephemeral
  delivery never falls back to exposing results publicly.
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
