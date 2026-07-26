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
venv/bin/python -m compileall -q bot.py config.py modules scripts tests
venv/bin/python -m unittest discover -s tests -v
venv/bin/ruff check .
venv/bin/mypy
```

The suite does not contact Telegram or the live GetBible API. It uses fakes and local fixtures for reproducibility.

Run one module while developing:

```bash
venv/bin/python -m unittest tests.test_service -v
venv/bin/python -m unittest tests.test_catalog -v
venv/bin/python -m unittest tests.test_interactions -v
venv/bin/python -m unittest tests.test_commands -v
venv/bin/python -m unittest tests.test_renderer -v
venv/bin/python -m unittest tests.test_security -v
venv/bin/python -m unittest tests.test_audit_runtime -v
venv/bin/python -m unittest tests.test_audit -v
venv/bin/python -m unittest tests.test_bot -v
venv/bin/python -m unittest tests.test_interactive_features -v
venv/bin/python -m unittest tests.test_logging -v
venv/bin/python -m unittest tests.test_setup_script -v
venv/bin/python -m unittest tests.test_utils -v
venv/bin/python -m unittest tests.test_documentation -v
```

## Security and dependency checks

Run the same source-aware checks used by CI:

```bash
librarian_path=$(
  venv/bin/python - <<'PY'
from pathlib import Path

import getbible

print(Path(getbible.__file__).resolve().parent)
PY
)
venv/bin/bandit -q -r \
  bot.py config.py modules scripts "$librarian_path" -ll
venv/bin/python scripts/audit_runtime.py
venv/bin/detect-secrets scan \
  --all-files \
  --exclude-files '(^|/)\.env\.template$' \
  --exclude-files '(^|/)requirements(-dev)?\.txt$'
```

Librarian 1.2.0 is installed as a released, hashed package, so `scripts/audit_runtime.py` submits the complete lock to `pip-audit --strict` without filtering a dependency. The helper retains fail-closed validation for any future direct source declaration. A malformed source declaration, missing hash, audit error, or vulnerable dependency fails the check.

Review secret-scan output rather than blindly suppressing it. The `.env.template` contains a deliberate placeholder and is excluded; real tokens are never acceptable. `scripts/run-checks.sh` also excludes the configured in-repository virtual environment and standard environment directory names so following the documented `venv` workflow does not scan installed dependency metadata.

Validate the manager:

```bash
bash -n setup.sh
bash -n tests/setup_manager_lifecycle.sh
bash setup.sh self-test
bash tests/setup_manager_lifecycle.sh
```

The lifecycle harness is hermetic: it redirects all managed paths into a
temporary root and substitutes only host boundaries such as systemd, Telegram,
and account management. It executes the real questionnaire and manager logic
for two instances, including transactional cleanup, duplicate-token rejection,
content-file permissions/editing, polling-to-webhook-to-polling switching,
selectors, lifecycle commands, diagnostics, configuration restoration,
upgrades, automatic failed-upgrade restoration, manual rollback, and isolated
uninstall. It never contacts Telegram or changes the host.

The CI quality job creates an isolated `ci` service fixture and verifies `getbible-robot@ci.service`, then performs an isolated hashed install, Ruff, mypy, robot/Librarian Bandit scans, strict source-aware dependency auditing, secret scanning, and manager tests. CodeQL runs separately. A deployable commit must have both permanent gate statuses green.

## What the regression suite must prove

The tests cover at least these invariants:

- huge verse numbers and ranges are rejected before list materialization or repository access;
- malformed references never silently become verse 1;
- ordinary references do not trigger speculative translation lookups;
- an empty `/bible` never substitutes a hidden default verse;
- explicit `/bible <reference>` commands still post immediately;
- group `/bible` commands, translation/book/chapter/verse pages, progress, and
  recoverable errors remain per-user ephemeral, with no ordinary group message
  before confirmed Scripture delivery;
- `/search <words>` returns complete, highlighted verses in per-user ephemeral
  group pages of at most 30 results, with every returned reference selectable
  and no ordinary group post before confirmation;
- full corpus downloads and constructed search output enforce independent byte
  ceilings;
- slow searches use independent capacity and circuit state, leaving direct
  references available;
- default-translation prewarming builds the initial search index;
- empty `/search` exposes Librarian 1.2 filters through a bounded dashboard;
- every registered command alias and every implemented callback action appears in an explicit test inventory;
- every translation, testament, book, chapter, verse, navigation, back, reset,
  cancel, filter, exclusion, proximity, selection, and confirmation control
  executes through the interaction state machine;
- callback sessions require the originating user and chat and expire under TTL/LRU bounds;
- guided navigation rejects malformed catalogs, oversized responses, redirects, and book checksum mismatches;
- selected search verses are compressed and revalidated before Librarian retrieval;
- malformed explicit-translation commands do not trigger repository lookups;
- request, response-message, queue, timeout, cache, and rate-limit state is bounded;
- a timed-out worker retains its capacity permit until the actual thread exits;
- Python 3.10 and newer enter the same typed timeout and queue-rejection paths;
- repeated upstream failures open the circuit and one later probe can recover it;
- cached mutable values cannot be corrupted by a caller;
- Telegram HTML and URL segments are escaped and encoded;
- Telegram limits are measured in UTF-16 code units, including emoji and other astral text;
- user-facing errors never echo raw exceptions, paths, URLs, tokens, or hostile input;
- deletion permission failures do not turn a successful lookup into a failed command;
- consecutive verses use exactly one newline with no blank paragraph;
- polling and webhook startup pass only the validated transport options;
- a Telegram polling conflict stops once and records the non-restarting state;
- Bot API command/profile synchronization and safe prewarm failure are covered;
- the complete released Librarian lock is included in strict dependency auditing;
- all required operator documents and relative links remain valid;
- all public links use `https://getbible.life` and data access uses `https://api.getbible.net`.
- missing optional translation-language labels cannot disable the `/bible` picker;
- one malformed translation entry is ignored without weakening validation of entries
  that become Telegram callback values;
- instance, token, health-port, account, path, cache, and log isolation is preserved;
- setup shell syntax and the manager's fail-closed validators pass;
- the setup questionnaire installs two isolated instances in a temporary host fixture;
- failed installation is cleaned transactionally without retaining an account, secret, cache, state, or application;
- list, selection, start, stop, restart, status, runtime, logs, follow, doctor,
  delivery, content, configuration, upgrade, rollback, menu, and uninstall
  manager paths execute;
- invalid configuration is restored, a failed upgrade restores the active application, and uninstalling one instance leaves the other intact;
- metadata audit mode omits query/reference content;
- content audit mode includes only the deliberately permitted normalized fields;
- JSONL events include the selected instance and controlled audit object.

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

In a group where the bot can delete messages, exercise all three completion
paths:

1. `/bible John 3:16`;
2. empty `/bible`, complete the picker, then press **Post Scripture**;
3. `/search grace`, select a result, then press **Post selected**.

For `/bible` and `/search` in a group, confirm the initiating registered command
and every picker/search panel, prompt, reply, progress state, and recoverable
notice are visible only to the initiating user. No ordinary group message may
be sent before the final confirmation action. If a command or legacy alias
arrives visibly from an older client, it may remain while the workflow is
active, but it must disappear after final Scripture delivery. Only Scripture
must remain. Also verify that **Cancel** removes the private workflow without
posting.

Repeat in a group where the bot lacks deletion permission. Scripture must still
be delivered; the workflow must not raise a user-facing failure merely because
cleanup was refused. Logs may record the non-fatal permission failure.

`DELETE_COMMAND_MESSAGES=true` additionally covers standalone handled commands
such as `/start` and `/help`; it does not disable the mandatory successful
`/bible` and `/search` workflow cleanup.

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
   /bible
   /bible John 3:16
   /bible John 3:16-19;1 John 3:10-17
   /bible Gen 1:1 aov
   /bible John 3:16 kjv
   /bible John 1:1-999999999
   /bible John 1:16!
   /search grace
   /search
   /unknown
   ```

7. In a group, confirm empty `/bible` is invisible to other members; continue
   with KJV, choose John 3, select 16 as both first and last verse, review, and
   confirm that only **Post Scripture** creates an ordinary message in the
   originating topic.
8. Confirm `/search grace` is invisible to other group members, shows every
   returned verse in full, bolds each matching word, pages at up to 30 complete
   results, and posts nothing publicly until **Post selected** is pressed.
9. In empty `/search`, change word, match, scope, book, exclusion, and proximity controls; run a search; page, select, deselect, and post multiple results.
10. In a test group, verify `/bible@TestBotName John 3:16`, empty `/bible@TestBotName`, selective search replies, ownership isolation between two users, and permission-safe command deletion.
11. Open a returned Scripture link and confirm its host is exactly `getbible.life`.
12. Leave a panel idle beyond `INTERACTION_TTL_SECONDS` and confirm its buttons expire safely.
13. Send a sustained rejected burst and confirm only one cooldown warning is sent, with no crash or memory growth.
14. Stop with `Ctrl+C` and confirm the health listener, worker pool, and Librarian sessions close cleanly.

Do not paste tokens or private chat content into issues, CI logs, screenshots, or test artifacts. Use metadata audit mode for normal production smoke testing. If content mode is being tested, use synthetic references/search terms and remove the test log according to policy.

## Production pre-rollout test

After installation but before announcing availability:

```bash
sudo getbible-robot status production
sudo getbible-robot doctor production
sudo getbible-robot runtime production
sudo getbible-robot logs production 100
```

Then repeat the small private Telegram smoke set using the production bot. If multiple instances share the host, confirm each selector resolves the intended service and that start/stop, cache, port, log, metrics, and token behavior do not cross instance boundaries. Record the deployed robot commit and lockfile checksums. Complete every item in [the release gate](RELEASE_GATE.md).
