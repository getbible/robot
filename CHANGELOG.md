# Changelog

All notable GetBible Robot changes are documented here. Dates describe repository changes; production deployment remains a separate reviewed decision.

## Unreleased

### Security

- Added strict bounded reference handling through the hardened GetBible Librarian API.
- Added per-user and per-chat rate limits for every command with bounded identifier state.
- Added bounded repository response sizes, retries, connect/read/overall timeouts, worker capacity, and queue waits.
- Added a circuit breaker with readiness integration and one half-open recovery probe.
- Ensured a timed-out synchronous worker retains its capacity permit until the real thread exits.
- Replaced raw exception responses with typed user-safe messages and correlation IDs.
- Added HTML escaping, URL encoding, UTF-16-aware Telegram sizing, safe chunk boundaries, and a maximum output-message count.
- Added startup configuration validation, loopback health endpoints, structured privacy-preserving logs, and a hardened systemd unit.

### Reliability

- Separated the machine-readable API base (`api.getbible.net`) from public Telegram links (`getbible.life`).
- Stopped speculative translation lookups for ordinary or malformed references.
- Made message deletion optional and permission failures non-fatal.
- Limited polling to message updates required by the command bot.
- Added clean health, worker-pool, and Librarian-session shutdown behavior.

### Testing and delivery

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
- Updated mypy to 2.3.0.
- Made Python 3.10's `exceptiongroup` requirement explicit in the universal runtime input.
- Temporarily selected the reviewed hardened Librarian source commit pending its 1.2 package release. The documented long-term policy is `getbible>=1.2,<2` plus an exact hashed lock.

### Documentation

- Added complete installation, configuration, testing, dependency, upgrade, rollback, uninstall, troubleshooting, architecture, operations, security, and release-gate documentation.
