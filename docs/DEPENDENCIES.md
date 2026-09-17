# Dependency policy

GetBible Robot separates human-maintained dependency intent from the exact environment deployed in production.

## Files and responsibilities

| File | Purpose | Edit directly? |
|---|---|---|
| `requirements.in` | Direct runtime dependency policy | Yes |
| `requirements-dev.in` | Direct development and security-tool policy | Yes |
| `requirements.txt` | Exact runtime versions and hashes | No; regenerate |
| `requirements-dev.txt` | Exact development versions and hashes | No; regenerate |
| `.github/dependabot.yml` | Weekly Python and GitHub Actions update proposals | Yes, carefully |

Production installs only `requirements.txt` with `--require-hashes`. CI installs both exact locks. A process never downloads a newer dependency merely because it restarted.

## Why the deployed environment remains exact

“Use the latest compatible release” and “run reproducibly” are different concerns:

- the input range tells the update system which releases are acceptable;
- Dependabot proposes a reviewed lock update when a newer acceptable release exists;
- CI tests the complete resolved environment;
- merging the PR selects that exact environment;
- deployment uses the checked-in hashes without resolving again.

Using an unbounded `pip install --upgrade` during service startup would allow an upstream release to change production without code review, tests, rollback metadata, or a stable software bill of materials. The robot therefore updates quickly through reviewed automation, not unpredictably at runtime.

## Runtime dependency set

The direct runtime inputs are:

```text
python-telegram-bot[webhooks]
requests
tornado
python-dotenv
```

plus the `exceptiongroup` and `typing-extensions` compatibility pins described
below. There is no Scripture engine among them. Catalogues, references, and
full-text search are HTTPS requests to the public GetBible Main, Query, and
Search APIs, made with `requests` from the robot and with `fetch` from the
browser; the robot parses no corpus and builds no index. The former
`getbible` (Librarian) package and its `regex` dependency are not inputs, and
no module may import them. See [Search](SEARCH.md) for what the robot relies
on instead.

Dependabot proposes newer releases of each input. Each proposal must
regenerate both locks, pass the complete robot gate, and demonstrate
unchanged command and Mini App contracts before merge.

## Regenerating locks

Use the same Python and resolver versions every time:

```bash
python3.14 -m venv .lock-venv
.lock-venv/bin/python -m pip install \
  pip==26.1.2 \
  pip-tools==7.6.0

.lock-venv/bin/python -m piptools compile \
  --resolver=backtracking \
  --generate-hashes \
  --allow-unsafe \
  --output-file=requirements.txt \
  requirements.in

.lock-venv/bin/python -m piptools compile \
  --resolver=backtracking \
  --generate-hashes \
  --allow-unsafe \
  --output-file=requirements-dev.txt \
  requirements-dev.in
```

The repository also provides `scripts/refresh-locks.sh` for these commands.

Because the generated lock supports Python 3.10 through 3.14, `exceptiongroup`
and `typing-extensions` are explicit runtime inputs. This prevents the
canonical lock compiled on Python 3.14 from omitting dependencies whose package
metadata selects them only on an older supported interpreter.

## Validating a dependency update

```bash
python3.10 -m venv /tmp/robot-py310
/tmp/robot-py310/bin/python -m pip install --require-hashes -r requirements.txt
/tmp/robot-py310/bin/python -m pip check

python3.11 -m venv /tmp/robot-py311
/tmp/robot-py311/bin/python -m pip install --require-hashes -r requirements.txt
/tmp/robot-py311/bin/python -m pip check

python3.12 -m venv /tmp/robot-py312
/tmp/robot-py312/bin/python -m pip install --require-hashes -r requirements.txt
/tmp/robot-py312/bin/python -m pip check

python3.13 -m venv /tmp/robot-py313
/tmp/robot-py313/bin/python -m pip install --require-hashes -r requirements.txt
/tmp/robot-py313/bin/python -m pip check

python3.14 -m venv /tmp/robot-py314
/tmp/robot-py314/bin/python -m pip install --require-hashes -r requirements-dev.txt
/tmp/robot-py314/bin/python -m pip check
```

Then run tests, Ruff, mypy, Bandit, strict source-aware dependency auditing, secret scanning, systemd verification, and CodeQL. Review release notes and the actual diff; a green dependency bot PR is evidence, not a substitute for review.

## Auditing the complete released environment

The audit helper submits the complete locked environment to strict advisory auditing:

```bash
venv/bin/python scripts/audit_runtime.py
```

Any vulnerability, audit error, or unhashed lock fails the check. Bandit separately scans the repository's own Python sources (`bot.py`, `config.py`, `modules`, `container`, and `scripts`) in CI and `scripts/run-checks.sh`.

## GitHub Actions dependencies

Actions are pinned to immutable commit SHAs in workflow files. Dependabot may update those SHAs. Review that each PR changes only the declared action and expected workflow references before merging.

GitHub-hosted runners satisfy the Node.js 24 runner requirement of the merged `actions/upload-artifact` v7 action. A future move to self-hosted runners must confirm runner version 2.327.1 or newer before using that workflow.

## Emergency security updates

For an actively exploited or high-impact vulnerability:

1. update the direct input or constraint;
2. regenerate both locks;
3. run the full gate rather than bypassing it;
4. deploy the reviewed commit;
5. verify the installed version with `pip freeze` and `pip check`;
6. retain the previous known-good commit for rollback unless it is itself vulnerable.
