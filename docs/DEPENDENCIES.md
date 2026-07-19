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

## Current Librarian transition

The robot currently uses the reviewed hardened Librarian source commit because the robot relies on APIs introduced in the forthcoming Librarian 1.2 line, while that release has not yet been confirmed as available from the package index.

The temporary entry is intentionally visible in `requirements.in`:

```text
getbible @ https://github.com/getbible/librarian/archive/95cdcafb6588d60eb2b1b000b4aa59f889c0f772.tar.gz
```

This is a transition mechanism, not the long-term policy. Do not replace it with a moving `master` or `staging` branch: moving branch installs are neither reproducible nor safely auditable.

## Switch after the Librarian release

After a tested Librarian release containing `RequestLimits`, typed repository failures, bounded response handling, and the hardened parser is published as version 1.2.0 or newer:

1. Confirm the release artifacts and Librarian CI are valid.
2. Replace the source URL in `requirements.in` with:

   ```text
   getbible>=1.2,<2
   ```

3. Regenerate both locks using Python 3.12 and the pinned lock tooling.
4. Install the runtime lock on Python 3.10, 3.11, and 3.12 with `--require-hashes`.
5. Run the complete robot release gate.
6. Merge only after all checks pass.

After this one-time transition, Dependabot will propose newer compatible Librarian releases within the 1.x series. A future 2.x release requires an intentional compatibility review and range change.

The exact lock may resolve, for example, `getbible==1.2.0` even though the input allows later 1.x versions. That is expected: the next Dependabot lock PR moves the deployed version after testing.

## Regenerating locks

Use the same Python and resolver versions every time:

```bash
python3.12 -m venv .lock-venv
.lock-venv/bin/python -m pip install \
  pip==24.3.1 \
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

Because the generated lock supports Python 3.10 through 3.12, `exceptiongroup` is an explicit runtime input. This prevents a lock compiled on Python 3.12 from omitting a package required on Python 3.10.

## Validating a dependency update

```bash
python3.10 -m venv /tmp/robot-py310
/tmp/robot-py310/bin/python -m pip install --require-hashes -r requirements.txt
/tmp/robot-py310/bin/python -m pip check

python3.11 -m venv /tmp/robot-py311
/tmp/robot-py311/bin/python -m pip install --require-hashes -r requirements.txt
/tmp/robot-py311/bin/python -m pip check

python3.12 -m venv /tmp/robot-py312
/tmp/robot-py312/bin/python -m pip install --require-hashes -r requirements-dev.txt
/tmp/robot-py312/bin/python -m pip check
```

Then run tests, Ruff, mypy, Bandit, `pip-audit`, secret scanning, systemd verification, and CodeQL. Review release notes and the actual diff; a green dependency bot PR is evidence, not a substitute for review.

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
