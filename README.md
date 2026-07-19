# GetBible Robot

GetBible Robot is the Telegram interface for retrieving Scripture from GetBible. The bot is designed as a bounded public service: hostile input, stalled upstream requests, malformed repository data, and Telegram permission failures must not take down the process or expose internal errors.

## Service boundaries

These two URLs have intentionally different purposes:

- `https://api.getbible.net` is the machine-readable Scripture data source.
- `https://getbible.life` is the public website used by every clickable Scripture and search link sent to Telegram.

They are configured independently as `GETBIBLE_API_BASE_URL` and `GETBIBLE_WEB_BASE_URL`.

## Telegram commands

```text
/bible 1 John 3:16
/bible John 3:16-19;1 John 3:10-17
/bible Gen 1:1-5 codex
/bible Ps 1:1-5 aov
/search
/help
```

`/get` and `/getbible` remain aliases of `/bible`. Telegram's parsed command arguments are used, so `/bible@getBibleRobot John 3:16` works in groups without leaking the bot mention into the reference parser.

## Security and reliability controls

The robot enforces all of the following before or around repository work:

- complete, bounded reference parsing with no silent fallback to a different verse;
- at most 8 references and 100 total verses per command by default;
- per-user and per-chat token buckets with bounded in-memory state;
- a global lookup semaphore and bounded queue wait;
- explicit connect, read, overall lookup, retry, and response-size limits;
- synchronous Librarian work isolated in a bounded thread pool;
- an upstream circuit breaker;
- typed user-safe errors with correlation IDs instead of raw exceptions;
- HTML escaping, URL encoding, and verse-boundary Telegram message chunking;
- optional command deletion that never makes a successful request fail;
- aggregate-only metrics and structured logs that do not record message content;
- loopback health and readiness endpoints;
- deterministic tests, fuzz regressions, linting, type checking, Bandit, dependency auditing, secret scanning, and CodeQL.

See [the architecture](docs/ARCHITECTURE.md), [operations guide](docs/OPERATIONS.md), and [release gate](docs/RELEASE_GATE.md).

## Requirements

- Linux is recommended for production.
- Python 3.10 through 3.12.
- A Telegram bot token from `@BotFather`.
- `git` and a Python virtual environment.

Runtime dependencies are locked with hashes. The GetBible Librarian dependency is pinned to the reviewed hardening commit used by this release.

## Installation

```bash
git clone https://github.com/getbible/robot.git /opt/getbible-robot
cd /opt/getbible-robot

python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install --require-hashes -r requirements.txt

cp .env.template /etc/getbible-robot.env
sudo chmod 600 /etc/getbible-robot.env
sudo editor /etc/getbible-robot.env
```

At minimum, replace `TELEGRAM_API_TOKEN`. The configuration loader fails before polling if required values, URLs, limits, or token aliases conflict.

Run interactively:

```bash
set -a
. /etc/getbible-robot.env
set +a
venv/bin/python bot.py
```

## systemd

Create a dedicated account and writable cache directory, then install the supplied hardened unit:

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin getbible-robot
sudo install -d -o getbible-robot -g getbible-robot -m 0700 /var/cache/getbible-robot
sudo cp deploy/getbible-robot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now getbible-robot.service
```

The unit validates configuration before startup, restarts failed processes, runs without capabilities, protects the host filesystem, and applies task and memory limits.

## Health and metrics

The default endpoint binds only to `127.0.0.1:8081`:

```bash
curl --fail http://127.0.0.1:8081/healthz
curl --fail http://127.0.0.1:8081/readyz
curl --fail http://127.0.0.1:8081/metrics
```

Set `HEALTH_PORT=0` to disable it. Do not expose this endpoint publicly without an authenticated reverse proxy.

## Development checks

```bash
venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
venv/bin/python -m compileall -q bot.py config.py modules tests
venv/bin/python -m unittest discover -s tests -v
venv/bin/ruff check .
venv/bin/mypy
venv/bin/bandit -q -r bot.py config.py modules
venv/bin/pip-audit -r requirements.txt
```

Live Telegram and GetBible API credentials are not required by the deterministic test suite.

## License

GNU GPL v2.0. See [LICENSE](LICENSE).
