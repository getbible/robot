# Security and reliability release gate

A robot commit is deployable only when every item below is satisfied.

## Automated

- Python source compiles.
- Deterministic unit, asynchronous, regression, and symbol-fuzz tests pass on every supported Python version.
- Ruff and mypy pass without suppressing new failures.
- Bandit reports no medium/high findings.
- `pip-audit` reports no known vulnerability in the exact runtime lock.
- Secret scanning and CodeQL pass.
- The dependency lock installs with `--require-hashes`.
- No test, assertion, or security job is removed merely to obtain a green result.

## Behavioral

- A huge or malformed range terminates in bounded time and memory before repository access.
- Invalid input is never silently changed to verse 1 or another reference.
- Normal references do not trigger speculative translation requests.
- Repository stalls return a generic response within the configured outer timeout.
- Repeated upstream failures open the circuit and later permit one recovery probe.
- Rate-limiter and cache state remain bounded under identifier churn.
- Every Telegram chunk is valid escaped HTML below 4096 characters.
- All public links begin with the configured `https://getbible.life` base.
- All Scripture data requests use the configured `https://api.getbible.net` base.
- Raw exceptions, URLs, paths, and input are absent from user-facing error messages.
- Missing Telegram message-deletion permission does not fail a successful lookup.
- Shutdown closes the health listener, HTTP sessions, and worker pool.

## Operational

- The hardened systemd unit passes `systemd-analyze verify`.
- A clean host can install only from the checked-in lock.
- `/healthz`, `/readyz`, and `/metrics` behave as documented.
- A rollback to the previous known-good commit is rehearsed.
- The production bot token is absent from source, CI output, and artifacts.
