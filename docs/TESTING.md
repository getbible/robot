# Testing

Testing is divided into deterministic checks, security and packaging checks, local failure injection, and a final live Telegram smoke test. Only the final smoke test requires a bot token.

## Create the exact development environment

Use a clean virtual environment and the checked-in hashed development lock:

```bash
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
venv/bin/python -m pip check
```

A clean environment matters. A globally installed package can hide a missing lock entry or incompatible dependency.

## Fast deterministic test cycle

```bash
venv/bin/python -m compileall -q bot.py config.py modules tests
venv/bin/python -m unittest discover -s tests -v
venv/bin/ruff check .
venv/bin/mypy
```

The suite does not contact Telegram or the live GetBible API. It uses fakes and local fixtures for reproducibility.

Run one module while developing:

```bash
venv/bin/python -m unittest tests.test_service -v
venv/bin/python -m unittest tests.test_renderer -v
venv/bin/python -m unittest tests.test_security -v
```

## Security and dependency checks

```bash
venv/bin/bandit -q -r bot.py config.py modules -ll
venv/bin/pip-audit --strict -r requirements.txt
venv/bin/detect-secrets scan \
  --all-files \
  --exclude-files '(^|/)\.env\.template$' \
  --exclude-files '(^|/)requirements(-dev)?\.txt$'
```

Review secret-scan output rather than blindly suppressing it. The `.env.template` contains a deliberate placeholder and is excluded; real tokens are never acceptable.

Verify the production unit on a Linux host:

```bash
sudo systemd-analyze verify deploy/getbible-robot.service
```

The CI quality job also performs an isolated hashed install, Ruff, mypy, Bandit, `pip-audit`, secret scanning, and unit verification. CodeQL runs separately. A deployable commit must have both permanent gate statuses green.

## What the regression suite must prove

The tests cover at least these invariants:

- huge verse numbers and ranges are rejected before list materialization or repository access;
- malformed references never silently become verse 1;
- ordinary references do not trigger speculative translation lookups;
- malformed explicit-translation commands do not trigger repository lookups;
- request, response-message, queue, timeout, cache, and rate-limit state is bounded;
- a timed-out worker retains its capacity permit until the actual thread exits;
- repeated upstream failures open the circuit and one later probe can recover it;
- cached mutable values cannot be corrupted by a caller;
- Telegram HTML and URL segments are escaped and encoded;
- Telegram limits are measured in UTF-16 code units, including emoji and other astral text;
- user-facing errors never echo raw exceptions, paths, URLs, tokens, or hostile input;
- deletion permission failures do not turn a successful lookup into a failed command;
- all public links use `https://getbible.life` and data access uses `https://api.getbible.net`.

When a defect is found, first add a deterministic regression test that fails for the defect and then implement the fix. Never weaken an assertion or disable a security job merely to make CI green.

## Local failure-injection checks

Use a dedicated test configuration, not production.

### Unreachable repository

Set a loopback endpoint that is not listening:

```dotenv
GETBIBLE_API_BASE_URL="http://127.0.0.1:65534"
GETBIBLE_CONNECT_TIMEOUT="0.2"
GETBIBLE_READ_TIMEOUT="0.5"
GETBIBLE_REQUEST_RETRIES="0"
LOOKUP_TIMEOUT="2"
CIRCUIT_FAILURE_THRESHOLD="2"
```

Expected behavior:

- commands return a generic temporary-unavailable message;
- no internal URL or exception appears in Telegram;
- `/metrics` records repository failures;
- after the configured threshold, `/readyz` returns 503 and the circuit metric is open;
- after recovery time and restoration of the API URL, one probe is allowed.

### Worker saturation

With a controlled slow local repository, set:

```dotenv
MAX_CONCURRENT_LOOKUPS="1"
LOOKUP_TIMEOUT="1"
LOOKUP_QUEUE_TIMEOUT="0.2"
```

Expected behavior: one real worker remains occupied until it exits, later requests fail quickly as busy, and executor work does not accumulate without bound.

### Telegram deletion permissions

Set `DELETE_COMMAND_MESSAGES=true` and test in a group where the bot lacks deletion permission. Scripture must still be delivered; logs may record the non-fatal permission failure.

## Live Telegram smoke test

Use a separate test bot token and a private test chat. Stop any other polling process that uses the same token.

1. Copy `.env.template` to `.env`.
2. Set the test token and an unused loopback `HEALTH_PORT`.
3. Keep the production API/web boundaries:

   ```text
   GETBIBLE_API_BASE_URL=https://api.getbible.net
   GETBIBLE_WEB_BASE_URL=https://getbible.life
   ```

4. Start the bot:

   ```bash
   venv/bin/python bot.py
   ```

5. Verify health and readiness:

   ```bash
   curl --fail http://127.0.0.1:8081/healthz
   curl --fail http://127.0.0.1:8081/readyz
   curl --fail http://127.0.0.1:8081/metrics
   ```

6. Exercise Telegram:

   ```text
   /start
   /help
   /bible John 3:16
   /bible John 3:16-19;1 John 3:10-17
   /bible Gen 1:1 aov
   /bible John 3:16 kjv
   /bible John 1:1-999999999
   /bible John 1:16!
   /unknown
   ```

7. In a test group, verify `/bible@TestBotName John 3:16` and permission-safe command deletion.
8. Open a returned Scripture link and confirm its host is exactly `getbible.life`.
9. Send a short burst and confirm rate-limit responses occur without a crash or memory growth.
10. Stop with `Ctrl+C` and confirm the health listener, worker pool, and Librarian sessions close cleanly.

Do not paste tokens or private chat content into issues, CI logs, screenshots, or test artifacts.

## Production pre-rollout test

After installation but before announcing availability:

```bash
sudo systemctl status getbible-robot.service --no-pager
sudo journalctl -u getbible-robot.service -n 100 --no-pager
curl --fail http://127.0.0.1:8081/readyz
```

Then repeat the small private Telegram smoke set using the production bot. Record the deployed robot commit and lockfile checksums. Complete every item in [the release gate](RELEASE_GATE.md).
