# Security and reliability release gate

A robot commit is deployable only when every applicable item below is satisfied. A maintainer must review the actual code, workflow, dependency, and documentation diffs; automated green checks are necessary but not sufficient.

## Source and review

- The intended robot commit is identified by full SHA.
- No unrelated change is hidden in a dependency or documentation update.
- Security bounds are not disabled or increased without load-test evidence.
- No test, assertion, supported Python version, or security job is removed merely to obtain a green result.
- New failure behavior has a deterministic regression test.
- The source contains no temporary self-modifying or one-time repair workflow.

## Automated matrix

- The exact runtime lock installs with `--require-hashes` on Python 3.10, 3.11, and 3.12.
- `pip check` succeeds in every runtime environment.
- Python source, maintenance helpers, and tests compile.
- Deterministic unit, asynchronous, regression, documentation-contract, audit-contract, and symbol-fuzz tests pass on every supported Python version.
- Ruff passes without new suppressions added solely for the release.
- mypy passes with the configured strictness.
- Bandit reports no medium/high finding in the robot, maintenance scripts, or exact installed Librarian source.
- `scripts/audit_runtime.py` verifies the temporary Librarian URL/hash and runs `pip-audit --strict` against every registry dependency; after the package release it audits the complete lock.
- Secret scanning reports no real secret.
- The hardened systemd unit passes `systemd-analyze verify`.
- CodeQL succeeds.
- The permanent `robot/security-gate` and `robot/codeql-gate` statuses are green for the exact commit.

## Dependency integrity

- Direct intent files and generated locks are committed together when dependencies change.
- Both lockfiles were generated using the documented Python and resolver versions.
- The complete lock diff was reviewed, including transitive additions and removals.
- GitHub Actions remain pinned to reviewed immutable commit SHAs.
- The runtime does not resolve or upgrade dependencies during service startup.
- Python 3.10 conditional requirements remain represented in the universal lock.
- Until the Librarian 1.2 package release exists, the temporary reviewed source commit remains explicit, exactly URL-matched, and SHA-256 locked.
- The direct-source audit contract test fails for a URL mismatch, missing hash, duplicate source, or unfiltered source requirement.
- After the Librarian release, the robot uses the documented compatible package range and an exact hashed resolved version.

## Parser and work budgets

- A huge verse number or range terminates in bounded time and memory before repository access.
- Reversed, malformed, empty, or hostile-symbol references fail closed.
- Invalid input is never silently changed to verse 1 or another reference.
- Reference count, verses per reference, total verses, and input length are enforced.
- Ordinary references do not trigger speculative translation requests.
- An invalid explicit-translation reference is rejected before translation repository access.
- Per-user and per-chat state remains bounded under identifier churn.
- Every command, including `/start`, `/help`, `/search`, and unknown commands, consumes both rate-limit budgets.

## Upstream and concurrency behavior

- Connect, read, retry, response-byte, queue, and overall lookup limits are active.
- Repository stalls return a generic response within the configured outer timeout.
- Python 3.10 and newer enter the same typed timeout and queue-saturation paths.
- A timed-out worker retains its permit until the underlying thread actually exits.
- Repeated stalls cannot accumulate work in the executor's internal queue without bound.
- Repeated upstream failures open the circuit.
- One later half-open probe can close the circuit after recovery.
- Cancellation cannot permanently retain a half-open probe marker.
- Validation and caller-limit errors do not incorrectly open the upstream circuit.
- Shutdown waits for worker completion before closing Librarian HTTP sessions.

## Telegram behavior

- Every Telegram chunk is valid escaped HTML.
- URL path components are percent encoded.
- Length is measured in Telegram UTF-16 code units and remains below 4096.
- The configured maximum output-message count is enforced.
- All public Scripture and search links begin with the configured `https://getbible.life` base.
- All Scripture data requests use the configured `https://api.getbible.net` base.
- Raw exceptions, user input, tokens, paths, and internal URLs are absent from user-facing errors.
- Missing message-deletion permission does not fail an otherwise successful command.
- `/bible@BotName John 3:16` works in a test group.
- Only required Telegram update types are subscribed.

## Startup, health, and observability

- Configuration validation fails closed for missing tokens, conflicting aliases, invalid URLs, and inconsistent bounds.
- Telegram initialization and command registration complete before readiness is exposed.
- `/healthz`, `/readyz`, and `/metrics` behave as documented on loopback.
- Readiness becomes false when the circuit is open or service is closing.
- Metrics contain aggregate values only.
- Logs contain no message text or secrets.
- A normal SIGTERM closes health, workers, repository sessions, and polling cleanly.

## Documentation and operations

- README links to the canonical documentation index.
- Installation was followed successfully on a clean host or clean test image.
- Every current environment variable appears in the configuration reference and `.env.template`.
- Required operator documents exist and every relative Markdown link resolves.
- Deterministic and live test steps match the current commands and files.
- Dependency refresh instructions reproduce the checked-in lock process.
- Upgrade and rollback were rehearsed with the target and previous commits.
- Uninstall steps were reviewed for service, code, cache, secret, account, and token handling.
- Troubleshooting guidance matches current metrics, paths, and failure behavior.
- The deployment record contains the robot SHA, lock checksum, Python version, unit checksum, CI URLs, smoke-test result, and rollback SHA.

## Live pre-production smoke test

Using a dedicated test bot first, and then a private production-bot chat before announcement:

- `/start`, `/help`, `/search`, and an unknown command behave safely.
- A single verse, range, multiple references, default translation, and explicit translation return correct text.
- Huge and malformed references fail safely.
- A short burst exercises rate limiting without a crash.
- A group mention command parses correctly.
- Returned links open on `getbible.life`.
- API failure injection produces generic errors and circuit/readiness behavior.
- No token, exception, path, or private message appears in logs or artifacts.

## Final rollout decision

Release only after the exact commit satisfies the complete gate. If any item cannot be tested, record why, its risk, and a concrete compensating control; do not silently mark it complete.

After deployment, monitor readiness, circuit state, timeouts, queue rejections, restarts, and memory. Keep the previous known-good commit and environment backup until the rollout is accepted.
