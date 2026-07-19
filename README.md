# GetBible Robot

GetBible Robot is a hardened Telegram interface for retrieving Scripture from GetBible. It is designed as a bounded public service: hostile input, stalled upstream requests, malformed repository data, Telegram permission failures, and identifier churn must not take down the process or expose internal errors.

## Service boundaries

These URLs have intentionally different purposes:

```text
Scripture data source: https://api.getbible.net
Telegram web links:   https://getbible.life
```

They are configured independently as `GETBIBLE_API_BASE_URL` and `GETBIBLE_WEB_BASE_URL`. Scripture JSON is retrieved from the API. Every clickable Scripture and search link sent to Telegram uses the public website.

## Commands

```text
/bible 1 John 3:16
/bible John 3:16-19;1 John 3:10-17
/bible Gen 1:1-5 codex
/bible Ps 1:1-5 aov
/search
/help
```

`/get` and `/getbible` remain aliases of `/bible`. Telegram-parsed command arguments are used, so `/bible@getBibleRobot John 3:16` works in groups without leaking the bot mention into the reference parser.

## Security and reliability controls

The robot provides layered controls at both the Telegram and Librarian boundaries:

- complete, bounded reference parsing with no silent fallback to a different verse;
- bounded input length, reference count, verses per reference, and total verses;
- bounded Telegram message count and UTF-16-aware message sizing;
- per-user and per-chat token buckets applied to every command;
- bounded rate-limit state under arbitrary identifier churn;
- a fixed worker pool and global lookup semaphore;
- queue, connect, read, overall lookup, retry, and response-byte limits;
- timed-out workers retain capacity until their underlying threads actually exit;
- an upstream circuit breaker with one half-open recovery probe;
- typed user-safe errors and correlation IDs instead of raw exception text;
- escaped Telegram HTML, percent-encoded URL segments, and safe chunk boundaries;
- optional command deletion that cannot fail a successful lookup;
- validated startup configuration and narrow Telegram update subscriptions;
- structured logs without message content and aggregate-only metrics;
- loopback-only health and readiness endpoints;
- a restartable, capability-free, filesystem-protected `systemd` service;
- deterministic tests, fuzz regressions, Ruff, mypy, Bandit, dependency auditing, secret scanning, and CodeQL.

See [Architecture](docs/ARCHITECTURE.md) and [the release gate](docs/RELEASE_GATE.md) for the complete model.

## Supported runtime

- Python 3.10, 3.11, or 3.12.
- Linux with `systemd` for the supplied production unit.
- A Telegram bot token from `@BotFather`.
- Outbound HTTPS access to Telegram and `https://api.getbible.net`.

## Dependency policy

Human-maintained intent lives in `requirements.in` and `requirements-dev.in`. Production and CI install the exact hashed locks in `requirements.txt` and `requirements-dev.txt`.

The hardened Librarian source commit is temporarily pinned because the robot depends on APIs in the forthcoming Librarian 1.2 line. After the corresponding package release is published and verified, the input changes to:

```text
getbible>=1.2,<2
```

Dependabot will then propose the newest compatible release and regenerate the exact lock for review. Production never resolves an unreviewed “latest” dependency during startup. See [Dependency policy](docs/DEPENDENCIES.md).

## Quick local test

No Telegram token or live API access is required for deterministic tests:

```bash
git clone https://github.com/getbible/robot.git
cd robot
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
bash scripts/run-checks.sh
```

Run only the unit suite while iterating:

```bash
venv/bin/python -m unittest discover -s tests -v
```

## Production installation summary

Deploy an exact reviewed robot commit:

```bash
sudo git clone https://github.com/getbible/robot.git /opt/getbible-robot
cd /opt/getbible-robot
sudo git checkout --detach <reviewed-commit-sha>

sudo python3 -m venv venv
sudo venv/bin/python -m pip install --upgrade pip
sudo venv/bin/python -m pip install --require-hashes -r requirements.txt
sudo venv/bin/python -m pip check
```

Create the service account and cache, install `/etc/getbible-robot.env` with mode `0600`, and install the supplied unit:

```bash
if ! id getbible-robot >/dev/null 2>&1; then
  sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin getbible-robot
fi
sudo install -d -o getbible-robot -g getbible-robot -m 0700 /var/cache/getbible-robot
sudo install -o root -g root -m 0600 .env.template /etc/getbible-robot.env
sudo editor /etc/getbible-robot.env
sudo install -o root -g root -m 0644 \
  deploy/getbible-robot.service \
  /etc/systemd/system/getbible-robot.service
sudo systemd-analyze verify /etc/systemd/system/getbible-robot.service
sudo systemctl daemon-reload
sudo systemctl enable --now getbible-robot.service
```

At minimum, replace `TELEGRAM_API_TOKEN` before startup. Follow [Installation](docs/INSTALLATION.md) rather than relying on this summary for production.

## Health and metrics

The default endpoint binds only to `127.0.0.1:8081`:

```bash
curl --fail http://127.0.0.1:8081/healthz
curl --fail http://127.0.0.1:8081/readyz
curl --fail http://127.0.0.1:8081/metrics
```

Set `HEALTH_PORT=0` to disable it. Do not expose the endpoint publicly without an authenticated, access-controlled proxy.

## Documentation

- [Documentation index](docs/README.md)
- [Installation](docs/INSTALLATION.md)
- [Configuration reference](docs/CONFIGURATION.md)
- [Testing and live smoke checks](docs/TESTING.md)
- [Upgrading and rollback](docs/UPGRADING.md)
- [Uninstalling](docs/UNINSTALL.md)
- [Dependency and Librarian release policy](docs/DEPENDENCIES.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Security and reliability release gate](docs/RELEASE_GATE.md)
- [Security policy](SECURITY.md)

## Contributing safely

Keep handlers thin, preserve typed failure boundaries, add a deterministic regression test before fixing a defect, and run `bash scripts/run-checks.sh`. Do not suppress a failing check, loosen a hard bound, remove a supported Python version, or reflect raw errors merely to make a change pass.

Security reports must use the private process in [`SECURITY.md`](SECURITY.md).

## License

GNU GPL v2.0. See [LICENSE](LICENSE).
