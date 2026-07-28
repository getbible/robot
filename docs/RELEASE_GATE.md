# Security and reliability release gate

A robot commit is deployable only when every applicable item below is satisfied. A maintainer must review the actual code, workflow, dependency, and documentation diffs; automated green checks are necessary but not sufficient.

## Container artifacts

- `Dockerfile` installs the exact hashed production lock in a separate build
  stage and runs as UID/GID 10001.
- The image includes no Caddy, certificate manager, systemd, or host firewall.
- `compose.yaml` uses a read-only root filesystem, drops every capability,
  disables privilege escalation, and sets PID, CPU, memory, tmpfs, and graceful
  shutdown bounds.
- `compose.yaml` remains the environment-driven one-bot default and publishes
  only the Mini App application port; `compose.multi.yaml` is explicit.
- Missing application configuration produces structured container
  stdout/stderr, and both host-side and in-container setup commands are
  syntax/regression tested.
- Multi-instance configuration rejects invalid names, unsafe process-loader
  variables, missing health ports, and port collisions before child startup.
- The container supervisor has liveness, RSS, restart-backoff, restart-circuit,
  duplicate-poller, and signal-forwarding regression coverage.
- The Kubernetes example keeps one replica per bot token and declares startup,
  liveness, readiness, resource, secret, and persistent-state contracts.

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
- `setup.sh` passes `bash -n`, its fail-closed self-test, and the hermetic two-instance lifecycle test.
- Deterministic unit, asynchronous, regression, documentation-contract, audit-contract, and symbol-fuzz tests pass on every supported Python version.
- Ruff passes without new suppressions added solely for the release.
- mypy passes with the configured strictness.
- Bandit reports no medium/high finding in the robot, maintenance scripts, or exact installed Librarian source.
- `scripts/audit_runtime.py` runs `pip-audit --strict` against the complete released Librarian 1.2.0 lock.
- Secret scanning reports no real secret.
- The hardened instantiated `getbible-robot@ci.service` passes `systemd-analyze verify`.
- CodeQL succeeds.
- The permanent `robot/security-gate` and `robot/codeql-gate` statuses are green for the exact commit.
- CI validates the default, compatibility-single, multi-bot, and
  environment-sourced secret Compose models, builds the image, verifies its
  non-root user, and smoke-tests both supervisor and setup entrypoints.
- Production Compose files pull a published image and contain no implicit local
  build; `compose.build.yaml` is the explicit developer-only build overlay.
- Successful complete CI on `master` publishes only `edge` and the immutable
  full-commit tag to `ghcr.io/getbible/robot`.
- A published stable GitHub release tag exactly matches the project version and
  cannot publish semantic-version or `latest` image tags unless the exact
  commit already has green `robot/security-gate` and `robot/codeql-gate`
  statuses.
- Published AMD64/ARM64 images carry OCI source/license metadata, BuildKit SBOM
  and provenance records, and a signed GitHub artifact attestation.

## Dependency integrity

- Direct intent files and generated locks are committed together when dependencies change.
- Both lockfiles were generated using the documented Python and resolver versions.
- The complete lock diff was reviewed, including transitive additions and removals.
- GitHub Actions remain pinned to reviewed immutable commit SHAs.
- The runtime does not resolve or upgrade dependencies during service startup.
- Python 3.10 conditional requirements remain represented in the universal lock.
- The direct-source audit contract test fails for a URL mismatch, missing hash, duplicate source, or unfiltered source requirement.
- The robot uses `getbible>=1.2,<2` as compatible intent and an exact hashed resolved version.

## Parser and work budgets

- A huge verse number or range terminates in bounded time and memory before repository access.
- Reversed, malformed, empty, or hostile-symbol references fail closed.
- Invalid input is never silently changed to verse 1 or another reference.
- Reference count, verses per reference, total verses, and input length are enforced.
- Ordinary references do not trigger speculative translation requests.
- An invalid explicit-translation reference is rejected before translation repository access.
- Per-user and per-chat state remains bounded under identifier churn.
- Every command, including `/start`, `/help`, `/search`, and unknown commands, consumes both rate-limit budgets.
- Authenticated Mini App clients have a separate bounded client budget;
  lightweight navigation has fractional cost while exchange, search,
  Scripture, and post operations retain full cost.
- Forwarded client addresses affect logging and limiting only when the direct
  peer belongs to an explicitly trusted proxy network.
- Every public command alias and implemented callback action is present in the enforced feature inventory.
- Repeated rejected commands produce one cooldown warning rather than one Telegram API call per rejection.
- Repeated individual user/client exhaustion opens a bounded temporary block
  and produces one private or per-user-ephemeral warning; chat-only saturation
  never attributes abuse to one user.
- Mini App launches and sessions remain owner-scoped and TTL/LRU bounded under
  user, chat, and request churn.

## Upstream and concurrency behavior

- Connect, read, retry, full-corpus byte, search-output byte, queue, and overall
  lookup limits are active.
- The full-corpus ceiling holds the largest supported translation with measured
  headroom; it does not enlarge the search-result or Telegram-output budgets.
- Search and direct-reference pools/circuits are independent, and a slow search
  does not consume all direct-reference capacity.
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
- Consecutive verses render with one newline and no blank paragraph.
- URL path components are percent encoded.
- Length is measured in Telegram UTF-16 code units and remains below 4096.
- The configured maximum output-message count is enforced.
- All public Scripture and search links begin with the configured `https://getbible.life` base.
- All Scripture data requests use the configured `https://api.getbible.net` base.
- Raw exceptions, user input, tokens, paths, and internal URLs are absent from user-facing errors.
- Missing message-deletion permission does not fail an otherwise successful command.
- `/bible@BotName John 3:16` works in a test group.
- Empty `/bible` resumes the Mini App Bible reader with translation, book,
  chapter, compact verse selection, basket, and confirmation controls.
- `/search <words>` opens complete wrapping selectable Mini App results without
  posting automatically.
- Empty `/search` opens the documented Mini App filter dashboard.
- Search result selection persists across pages and only **Post selected** sends Scripture.
- Protected Mini App APIs require fresh signature-verified Telegram `initData`
  plus a short-lived launch token tied to the same user and workflow.
- Missing, expired, replayed, foreign, malformed, and stale authorization fails
  before repository work or posting.
- Browser-supplied verse text is never authoritative; final posting re-resolves
  validated selected identifiers server-side.
- Only required Telegram message and callback-query update types are subscribed.
- Polling and webhook modes each start with only the validated transport
  arguments and never run together.
- Duplicate polling produces one critical event, stops the application, exits
  with status 75, and is not restarted by systemd.
- Webhook requests require the generated secret and reach only a loopback
  listener behind public HTTPS.
- The Mini App uses a separate private listener behind public HTTPS, and
  polling remains supported while it is enabled.
- The public browser shell exposes no data or action capability without
  successful Telegram and launch authorization.

## Startup, health, and observability

- Configuration validation fails closed for missing tokens, conflicting aliases, invalid instance/log/audit values, invalid URLs, and inconsistent bounds.
- Metadata audit mode omits search terms and final references.
- Content audit mode includes only the documented normalized query/reference fields.
- Disabled identity mode omits user/chat/client identity; pseudonymous mode
  records stable keyed identifiers; raw mode records only documented numeric
  Telegram IDs and resolved Mini App client IPs.
- No audit mode records a token, name, username, verse body, repository
  payload, Telegram `initData`, or launch/session credential.
- Every JSONL event is tagged with the selected instance.
- Telegram initialization and command registration complete before readiness is exposed.
- `/healthz`, `/readyz`, and `/metrics` behave as documented on loopback.
- Readiness becomes false when the circuit is open or service is closing.
- Metrics contain aggregate values only.
- Logs contain no secrets; message-derived search/reference content is absent unless content audit mode is explicitly enabled.
- A normal SIGTERM closes health, both worker pools, repository sessions, and
  the active Telegram transport cleanly.

## Documentation and operations

- README links to the canonical documentation index.
- The setup questionnaire completed successfully on a clean host or clean test image.
- The hermetic setup lifecycle passes transactional failure cleanup, two-instance isolation, all manager command paths, configuration restoration, upgrade failure restoration, rollback, and isolated uninstall.
- Managed Caddy routes pass DNS preflight, deterministic generation, complete
  validation, reload rollback, local/public verification, retained-port
  isolation, and per-instance uninstall cleanup.
- Two test instances demonstrate distinct accounts, applications, environments, tokens, caches, ports, logs, processes, and state.
- Instance selection resolves the intended target for list, start, stop,
  restart, status, runtime, logs, follow, doctor, delivery, Mini App, content,
  config, upgrade, rollback, and uninstall.
- Setup rejects duplicate instance names, unmanaged account collisions, reused tokens, dirty source, unsupported Python, invalid ports, and malformed secrets.
- Every current environment variable appears in the configuration reference and `.env.template`.
- Required operator documents exist and every relative Markdown link resolves.
- Deterministic and live test steps match the current commands and files.
- Dependency refresh instructions reproduce the checked-in lock process.
- Upgrade and rollback were rehearsed with the target and previous commits.
- Uninstall steps were reviewed for the selected service, code, cache, state, secret, account, retained log, and token handling without affecting other instances.
- Troubleshooting guidance matches current metrics, paths, and failure behavior.
- The deployment record contains the robot SHA, lock checksum, Python version, unit checksum, CI URLs, smoke-test result, and rollback SHA.

## Live pre-production smoke test

Using a dedicated test bot first, and then a private production-bot chat before announcement:

- `/start`, `/help`, `/search`, and an unknown command behave safely.
- A single verse, range, multiple references, default translation, and explicit translation return correct text.
- The empty `/bible` Mini App flow posts a single verse and a range in private
  chat and a group.
- Default and filtered Mini App searches page, select, deselect, and post one
  or multiple results.
- Search never posts before explicit confirmation.
- Light/dark themes, compact reader text, auto-hiding chapter and bottom
  controls, safe-area layout, selected states, and accessible focus/contrast
  are verified in Telegram clients.
- The partial passage sheet leaves Scripture visible, opens at the current
  chapters, returns to API-localized books, reaches Psalm 150, and closes via
  Close, backdrop, Escape, and Telegram Back without trapping focus.
- Changing translation immediately replaces the chapter and atomically retains
  the nearest valid resume point, including when the Mini App closes during the
  transition.
- Ordinary-browser access and expired/mismatched launch attempts cannot read
  protected data or trigger a post.
- A right-to-left translation and a non-66-book translation navigate correctly.
- Huge and malformed references fail safely.
- A short burst exercises rate limiting without a crash.
- A group mention command parses correctly.
- Returned links open on `getbible.life`.
- API failure injection produces generic errors and circuit/readiness behavior.
- In metadata plus disabled-identity mode, no token, identity, exception detail,
  secret path, query, reference, or private message appears in logs or
  artifacts.
- Pseudonymous and raw identity tests contain only the documented identity
  fields; raw-mode testing uses synthetic Telegram IDs and client addresses.
- In content-mode testing, only synthetic search terms and final references
  appear; tokens, names, usernames, verse bodies, browser authorization data,
  and repository payloads remain absent.

## Final rollout decision

Release only after the exact commit satisfies the complete gate. If any item cannot be tested, record why, its risk, and a concrete compensating control; do not silently mark it complete.

After deployment, monitor readiness, circuit state, timeouts, queue rejections, restarts, and memory. Keep the previous known-good commit and environment backup until the rollout is accepted.
