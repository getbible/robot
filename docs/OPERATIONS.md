# Operations

This guide covers routine production operation after [installation](INSTALLATION.md). Use [Upgrading](UPGRADING.md) for changes, [Troubleshooting](TROUBLESHOOTING.md) for diagnosis, and [Uninstalling](UNINSTALL.md) for removal.

## Runtime inventory

Record and monitor:

```bash
cd /opt/getbible-robot
git rev-parse HEAD
venv/bin/python --version
sha256sum requirements.txt deploy/getbible-robot.service
sudo systemctl status getbible-robot.service --no-pager
```

The authoritative configuration is `/etc/getbible-robot.env`; do not print or copy it into logs. `TELEGRAM_API_TOKEN` is canonical. The deprecated `TELEGRAM_TOKEN` exists only for migration, and startup fails if both disagree.

Keep the external boundaries separate:

```text
GETBIBLE_API_BASE_URL=https://api.getbible.net
GETBIBLE_WEB_BASE_URL=https://getbible.life
```

## Routine service commands

```bash
sudo systemctl start getbible-robot.service
sudo systemctl stop getbible-robot.service
sudo systemctl restart getbible-robot.service
sudo systemctl reload-or-restart getbible-robot.service
sudo systemctl status getbible-robot.service --no-pager
sudo journalctl -u getbible-robot.service -f
```

The service validates configuration before starting, registers Telegram commands, starts the loopback health listener, and only then reports successful initialization.

Never run a second polling process with the same token while the service is active.

## Health, readiness, and metrics

Default loopback endpoints:

```bash
curl --fail http://127.0.0.1:8081/healthz
curl --fail http://127.0.0.1:8081/readyz
curl --fail http://127.0.0.1:8081/metrics
```

- `/healthz` confirms that the process and listener are alive.
- `/readyz` returns 503 while the Scripture circuit is open or the service is closing.
- `/metrics` exposes aggregate counters only; it contains no message text, token, reference, or verse text.

Keep the listener on loopback. Set `HEALTH_PORT=0` only when an external supervisor provides equivalent checks.

## Monitoring

Alert on:

- `getbible_robot_ready == 0` for longer than the expected circuit recovery interval;
- `getbible_robot_circuit_open == 1`;
- growth in `lookup_timeouts`, `repository_failures`, `unexpected_failures`, or `queue_rejections`;
- sustained user/chat rate-limit rejection;
- sustained `getbible_robot_interaction_evictions` growth or sessions remaining at the configured limit;
- unusual growth in expired interactive sessions without completed posts;
- repeated service restarts;
- memory approaching `MemoryMax`;
- file-descriptor or task pressure;
- failure of a private scheduled Scripture probe;
- a returned Telegram link whose host is not `getbible.life`.

Useful systemd counters:

```bash
systemctl show getbible-robot.service \
  -p ActiveState \
  -p SubState \
  -p NRestarts \
  -p MemoryCurrent \
  -p MemoryPeak \
  -p MemoryMax \
  -p TasksCurrent \
  -p TasksMax
```

## Logs and privacy

Logs are JSON objects written to the journal. They contain event descriptions, exception class names, aggregate state, and random correlation IDs. They must not contain Telegram message text, bot tokens, full user references, filesystem secrets, or repository response bodies.

Retrieve a bounded window:

```bash
sudo journalctl \
  -u getbible-robot.service \
  --since '30 minutes ago' \
  --no-pager
```

Treat correlation IDs as diagnostic handles, not proof of user identity.

## Configuration changes

1. Back up the secret file with mode `0600`.
2. Edit only the intended keys.
3. Validate with the currently deployed code.
4. Restart the service.
5. Verify readiness, logs, and a private Telegram lookup.
6. Roll back the file if validation or behavior fails.

```bash
sudo cp -a /etc/getbible-robot.env /etc/getbible-robot.env.backup
sudo editor /etc/getbible-robot.env
sudo bash -c '
  set -a
  . /etc/getbible-robot.env
  set +a
  cd /opt/getbible-robot
  venv/bin/python -c "from config import Settings; Settings.from_env()"
'
sudo systemctl restart getbible-robot.service
curl --fail http://127.0.0.1:8081/readyz
```

See [Configuration](CONFIGURATION.md) before changing bounds.

## Deployment and upgrade

Deploy only an exact robot commit whose permanent security and CodeQL gates are green. Install only its checked-in lock with `--require-hashes`. Follow [Upgrading and rollback](UPGRADING.md); do not combine source, lockfiles, or virtual environments from different commits.

The deployment record should include the robot commit, lockfile checksum, Python version, unit checksum, CI URLs, smoke-test result, and rollback commit.

## Backup scope

The only secret state is the external environment file and Telegram token. The code and dependency locks are recoverable from Git. The cache is recoverable from the GetBible API and normally does not require backup.

For disaster recovery, retain securely:

- the deployed robot commit SHA;
- an encrypted copy of `/etc/getbible-robot.env`, subject to secret-retention policy;
- the previous known-good robot commit;
- installation and host-configuration records.

Do not back up virtual environments as a substitute for the lockfile.

## Incident response

1. Stop or disable the service if it produces unsafe, incorrect, or uncontrolled responses.
2. Revoke the Telegram token through `@BotFather` immediately if disclosure is possible.
3. Preserve the deployed commit, lock checksums, unit checksum, and relevant journal window.
4. Verify whether the issue is Telegram, host networking, the GetBible API, Librarian, rendering, or configuration.
5. Roll back to the last complete release-gate success when safe.
6. Confirm `/readyz`, a private lookup, rate limiting, and link domains.
7. Add a deterministic regression test before redeploying the fix.
8. Report security defects privately under [`SECURITY.md`](../SECURITY.md).

## Rollback

The preferred rollback keeps the previous virtual environment during the acceptance window. Follow the exact procedure in [Upgrading](UPGRADING.md).

An emergency rebuild from the previous commit is:

```bash
sudo systemctl stop getbible-robot.service
cd /opt/getbible-robot
git checkout --detach <last-known-good-commit>
sudo rm -rf venv
sudo python3 -m venv venv
sudo venv/bin/python -m pip install --upgrade pip
sudo venv/bin/python -m pip install --require-hashes -r requirements.txt
sudo venv/bin/python -m pip check
sudo systemctl start getbible-robot.service
curl --fail http://127.0.0.1:8081/readyz
```

Do not reuse a lockfile from a different commit.

## Capacity changes

Increase limits only after a test reproduces the expected workload and measures memory, worker occupancy, Telegram message count, repository latency, and circuit behavior.

Hard complexity limits should never be disabled for administrators. Scaling process count multiplies per-process cache, interaction state, and worker memory. Increasing `MAX_CONCURRENT_LOOKUPS` without increasing API capacity can worsen an outage. A timed-out lookup intentionally retains its worker permit until the actual thread exits.

Interactive Bible and search state is deliberately ephemeral. Restarting the process, reaching the bounded LRU capacity, or exceeding `INTERACTION_TTL_SECONDS` expires an unfinished panel; the user can safely start the command again. Do not persist query text or selected verses in logs or metrics.

## Planned retirement

Use [Uninstalling](UNINSTALL.md). Stopping or deleting files does not revoke the Telegram token; bot retirement or token rotation must be completed separately through `@BotFather`.
