# Upgrading and rollback

Upgrade the robot by selecting a reviewed robot commit and installing its exact lock. Do not update dependencies independently on a production host.

## Preconditions

Before an upgrade, confirm:

- the target commit has green `robot/security-gate` and CodeQL statuses;
- its supported Python range includes the host interpreter;
- configuration changes and release notes have been reviewed;
- a private Telegram smoke test has succeeded in a test environment;
- the current deployed commit and environment file are recorded;
- the previous commit remains available for rollback.

## Record the current deployment

```bash
cd /opt/getbible-robot
CURRENT_SHA=$(git rev-parse HEAD)
printf '%s\n' "$CURRENT_SHA" | sudo tee /var/lib/getbible-robot.previous-sha
sudo cp -a /etc/getbible-robot.env /etc/getbible-robot.env.backup
sha256sum requirements.txt
```

Create `/var/lib/getbible-robot` first if it does not exist:

```bash
sudo install -d -o root -g root -m 0700 /var/lib/getbible-robot
```

Do not print the environment file or token into a terminal recording or ticket.

## Upgrade procedure

Set the exact target commit:

```bash
TARGET_SHA=<reviewed-commit-sha>
```

Fetch it and verify that the checkout has no local modifications:

```bash
cd /opt/getbible-robot
git status --short
git fetch --tags origin
git cat-file -e "${TARGET_SHA}^{commit}"
```

Stop the only polling process before replacing code or its environment:

```bash
sudo systemctl stop getbible-robot.service
```

Preserve the old virtual environment, select the new commit, and build a clean one:

```bash
cd /opt/getbible-robot
sudo rm -rf venv.previous
sudo mv venv venv.previous
sudo git checkout --detach "$TARGET_SHA"
sudo python3 -m venv venv
sudo venv/bin/python -m pip install --upgrade pip
sudo venv/bin/python -m pip install --require-hashes -r requirements.txt
sudo venv/bin/python -m pip check
```

Validate the new configuration and unit before starting:

```bash
sudo bash -c '
  set -a
  . /etc/getbible-robot.env
  set +a
  cd /opt/getbible-robot
  venv/bin/python -c "from config import Settings; Settings.from_env()"
'

sudo systemd-analyze verify /etc/systemd/system/getbible-robot.service
```

If the repository version contains an updated unit, install it explicitly:

```bash
sudo install \
  -o root \
  -g root \
  -m 0644 \
  deploy/getbible-robot.service \
  /etc/systemd/system/getbible-robot.service
sudo systemctl daemon-reload
```

Start and verify:

```bash
sudo systemctl start getbible-robot.service
sudo systemctl status getbible-robot.service --no-pager
curl --fail http://127.0.0.1:8081/healthz
curl --fail http://127.0.0.1:8081/readyz
sudo journalctl -u getbible-robot.service -n 100 --no-pager
```

Complete the private Telegram smoke test in [Testing](TESTING.md). Keep `venv.previous` until the rollout is accepted, then remove it:

```bash
sudo rm -rf /opt/getbible-robot/venv.previous
```

## Immediate rollback

Rollback if configuration validation, installation, startup, readiness, Scripture correctness, link domains, or the private smoke test fails.

```bash
sudo systemctl stop getbible-robot.service
cd /opt/getbible-robot
PREVIOUS_SHA=$(sudo cat /var/lib/getbible-robot.previous-sha)
sudo git checkout --detach "$PREVIOUS_SHA"
sudo rm -rf venv.failed
sudo mv venv venv.failed
sudo mv venv.previous venv
sudo cp -a /etc/getbible-robot.env.backup /etc/getbible-robot.env
sudo systemctl daemon-reload
sudo systemctl start getbible-robot.service
curl --fail http://127.0.0.1:8081/readyz
```

If `venv.previous` is unavailable, recreate it from the previous commit's own lock:

```bash
sudo rm -rf venv
sudo python3 -m venv venv
sudo venv/bin/python -m pip install --upgrade pip
sudo venv/bin/python -m pip install --require-hashes -r requirements.txt
sudo venv/bin/python -m pip check
sudo systemctl restart getbible-robot.service
```

Never combine code from one commit with a lockfile copied from another.

## Updating direct dependencies

Dependency maintainers edit only `requirements.in` or `requirements-dev.in`, then regenerate both locks using `scripts/refresh-locks.sh`. Review the resulting full lock diff, not merely the direct version line.

```bash
bash scripts/refresh-locks.sh
python3.10 -m venv /tmp/robot-py310
/tmp/robot-py310/bin/python -m pip install --require-hashes -r requirements.txt
python3.11 -m venv /tmp/robot-py311
/tmp/robot-py311/bin/python -m pip install --require-hashes -r requirements.txt
python3.12 -m venv /tmp/robot-py312
/tmp/robot-py312/bin/python -m pip install --require-hashes -r requirements-dev.txt
```

Then run the full release gate. Dependabot PRs follow the same rules; multiple lock-changing PRs should be rebased and merged sequentially so the final lock contains every accepted update.

## Librarian upgrades

The completed Librarian release transition uses:

```text
getbible>=1.2,<2
```

The current lock selects `getbible==1.2.0`. Dependabot proposes the newest compatible 1.x Librarian release. Regenerate both locks and run the complete matrix for every proposal. Production always installs the exact tested version and hashes from the merged lock; it does not resolve “latest” during service startup.

See [Dependency policy](DEPENDENCIES.md) for the compatibility and reproducibility rationale.

## Configuration migrations

When a release adds or changes settings:

1. compare `.env.template` with `/etc/getbible-robot.env` without exposing secret values;
2. add new settings deliberately rather than replacing the secret file wholesale;
3. validate with the target code before starting;
4. preserve the old environment file for rollback with mode `0600`;
5. remove obsolete aliases only after all hosts have migrated.

## Post-upgrade record

Record at least:

- deployment date and operator;
- robot commit SHA;
- `requirements.txt` SHA-256;
- Python version;
- service-unit checksum;
- configuration schema changes, without secret values;
- CI and CodeQL run URLs;
- smoke-test outcome;
- rollback commit.
