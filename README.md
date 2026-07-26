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
/bible
/search grace
/search
/help
```

`/get` and `/getbible` remain aliases of `/bible`. Telegram-parsed command arguments are used, so `/bible@getBibleRobot John 3:16` works in groups without leaking the bot mention into the reference parser.

An explicit `/bible` reference preserves the legacy fast path and posts the complete selection immediately. An empty `/bible` opens a Telegram-native picker with the configured translation preselected, then guides the user through testament, book, chapter, first verse, last verse, and confirmation.

`/search grace` runs Librarian 1.2 search defaults and opens a paginated result panel. Results are never posted automatically: the user selects one or more verses and presses **Post selected**. An empty `/search` first opens the complete search-filter dashboard for translation, word mode, match mode, scope, case, diacritics, ordering, books, exclusions, and proximity. See [Interactive Bible and search workflows](docs/INTERACTIONS.md).

## Security and reliability controls

The robot provides layered controls at both the Telegram and Librarian boundaries:

- complete, bounded reference parsing with no silent fallback to a different verse;
- bounded input length, reference count, verses per reference, and total verses;
- bounded Telegram message count and UTF-16-aware message sizing;
- per-user and per-chat token buckets applied to every command;
- bounded rate-limit state under arbitrary identifier churn;
- rejection-notification cooldowns that prevent Telegram API amplification;
- owner-scoped, TTL/LRU-bounded interactive sessions and opaque callback tokens;
- a fixed worker pool and global lookup semaphore;
- separate reference/search worker pools and circuit breakers, so expensive
  corpus work cannot occupy every direct-reference worker;
- queue, connect, read, overall lookup, retry, corpus-byte, and search-output limits;
- optional default-translation prewarming to pay the first-search cost before
  readiness rather than during a user's request;
- timed-out workers retain capacity until their underlying threads actually exit;
- an upstream circuit breaker with one half-open recovery probe;
- typed user-safe errors and correlation IDs instead of raw exception text;
- checksum-verified book navigation and structurally validated catalog metadata;
- escaped Telegram HTML, percent-encoded URL segments, and safe chunk boundaries;
- optional command deletion that cannot fail a successful lookup;
- validated startup configuration and narrow Telegram update subscriptions;
- selectable long polling or reverse-proxied HTTPS webhook delivery, with
  duplicate pollers stopped instead of restarted;
- per-instance JSONL/journal logs with metadata-only auditing by default and explicit content opt-in;
- loopback-only health and readiness endpoints;
- isolated locked service accounts and restartable, capability-free, filesystem-protected `systemd` instances;
- deterministic tests, fuzz regressions, Ruff, mypy, Bandit, dependency auditing, secret scanning, and CodeQL.

See [Architecture](docs/ARCHITECTURE.md) and [the release gate](docs/RELEASE_GATE.md) for the complete model.

## Supported runtime

- Python 3.10, 3.11, or 3.12.
- Linux with `systemd` for the supplied production unit.
- A Telegram bot token from `@BotFather`.
- Outbound HTTPS access to Telegram and `https://api.getbible.net`.

## Dependency policy

Human-maintained intent lives in `requirements.in` and `requirements-dev.in`. Production and CI install the exact hashed locks in `requirements.txt` and `requirements-dev.txt`.

The robot accepts compatible Librarian 1.x releases beginning with 1.2:

```text
getbible>=1.2,<2
```

The generated runtime lock currently selects and hashes `getbible==1.2.0`. Dependabot proposes newer compatible releases by regenerating the exact lock for review. Production never resolves an unreviewed “latest” dependency during startup. See [Dependency policy](docs/DEPENDENCIES.md).

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

Deploy an exact reviewed robot commit and run the questionnaire:

```bash
git clone https://github.com/getbible/robot.git
cd robot
git checkout --detach <reviewed-commit-sha>
sudo ./setup.sh install
```

The manager creates a separate locked Linux account, exact hashed environment, root-only token and editable content files, cache, state, JSONL log, rotation policy, health port, and hardened systemd service for every named instance. Tokens are entered with terminal echo disabled and are never accepted as command-line arguments. The questionnaire lets each instance choose polling or an HTTPS webhook and configures its Telegram command menu and profile text.

```bash
sudo getbible-robot list
sudo getbible-robot status production
sudo getbible-robot logs production
sudo getbible-robot doctor production
sudo getbible-robot delivery production
sudo EDITOR=nano getbible-robot content production help
```

Run `sudo getbible-robot` without arguments for an interactive operations menu. Follow [Installation](docs/INSTALLATION.md) for the complete production contract.

## Health and metrics

Each instance receives a unique loopback port, beginning at `127.0.0.1:8081`:

```bash
curl --fail http://127.0.0.1:8081/healthz
curl --fail http://127.0.0.1:8081/readyz
curl --fail http://127.0.0.1:8081/metrics
```

Use `sudo getbible-robot runtime <instance>` to resolve and query the correct port. Set `HEALTH_PORT=0` to disable it. Do not expose an endpoint publicly without an authenticated, access-controlled proxy.

## Documentation

- [Documentation index](docs/README.md)
- [Installation](docs/INSTALLATION.md)
- [Configuration reference](docs/CONFIGURATION.md)
- [Interactive Bible and search workflows](docs/INTERACTIONS.md)
- [Polling and webhook delivery](docs/WEBHOOKS.md)
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
