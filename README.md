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

The original `/start` handler remains available because Telegram sends that
command when a user first opens a bot or follows a start link. It is intentionally
not duplicated in the synchronized command menu; its editable welcome text
simply points to `/help`.

An explicit `/bible` reference preserves the fast path and posts the complete
selection immediately. Commands that require browsing open the authenticated
Telegram Mini App: an empty `/bible` opens Bible navigation, `/search grace`
opens contained results for that query, and an empty `/search` opens the search
form and filters. The Bible tab is a compact full-chapter reader whose verses
are selectable without separate raised cards. Search results can be selected
directly or opened in that reader for context. A bounded basket can contain one
verse, ranges, or separate references across pages before one final post.
Translation and the last content-free reader location persist per Telegram
user, with KJV as the application fallback. Intermediate browsing never floods
the chat. The header translation chip is the sole translation selector; it
keeps the current tab, updates localization immediately, and reloads the active
Bible passage before any new-translation label can appear with old text.
On Bot API 8.0+ clients the Mini App requests Telegram's native fullscreen
mode and follows Telegram's content-safe-area events; older or rejecting
clients retain the expanded-sheet fallback. In the Bible reader, scrolling
down reduces the bottom navigation to its safe-area-backed arrow handle.
Scrolling upward restores only the chapter heading. The navigation returns
when the handle is pressed or a verse selection changes.

The browser never supplies authoritative verse text. Every protected Mini App
request requires fresh Telegram-signed `initData` plus a short-lived,
user-bound launch token; the server resolves selected identifiers again before
posting. Telegram light/dark theme values drive the interface without becoming
an authorization input. See
[Interactive Bible and search workflows](docs/INTERACTIONS.md) and
[Mini App deployment](docs/MINI_APP.md).

## Security and reliability controls

The robot provides layered controls at both the Telegram and Librarian boundaries:

- complete, bounded reference parsing with no silent fallback to a different verse;
- bounded input length, reference count, verses per reference, and total verses;
- bounded Telegram message count and UTF-16-aware message sizing;
- per-user, per-chat, and Mini App client token buckets, with fractional cost
  for normal browser navigation;
- bounded rate-limit and temporary-abuse-block state under arbitrary identifier
  churn;
- private/ephemeral abuse notices and cooldowns that prevent Telegram API
  amplification;
- owner-scoped, TTL/LRU-bounded interactive and Mini App sessions;
- fresh Telegram Mini App signature validation plus user-bound, short-lived
  launch authorization for every data/action API;
- private Mini App serving behind external HTTPS (loopback on host-native
  installs or a container ingress network), with no bot token or
  authoritative Scripture text in browser state;
- a bounded per-instance preference database containing only Telegram user IDs,
  selected translation codes, non-content search defaults, four reader location
  identifiers, and update times;
- one hash-verified 64-chapter Main API cache with 15-minute freshness and a
  1 MiB per-chapter response ceiling;
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
- success-aware command cleanup that never removes the final Scripture, while
  deletion failures cannot invalidate delivery;
- validated startup configuration and narrow Telegram update subscriptions;
- selectable long polling or reverse-proxied HTTPS webhook delivery, with
  duplicate pollers stopped instead of restarted;
- continuously size-capped per-instance JSONL/journal logs with metadata-only
  auditing by default, independent disabled/pseudonymous/raw identity controls,
  trusted-proxy client-IP resolution, and explicit content opt-in;
- lifecycle-aware health and readiness endpoints with process and Mini App metrics;
- isolated locked service accounts and restartable, capability-free,
  filesystem-protected `systemd` instances with watchdog, memory-high,
  memory-max, and swap limits plus separate writable preference state;
- a non-root, read-only Docker image with no bundled reverse proxy, supporting
  one bot per workload or supervised multi-bot operation with unique ports;
- deterministic tests, fuzz regressions, Ruff, mypy, Bandit, dependency auditing, secret scanning, and CodeQL.

See [Architecture](docs/ARCHITECTURE.md) and [the release gate](docs/RELEASE_GATE.md) for the complete model.

## Supported runtime

- Python 3.10, 3.11, or 3.12.
- Docker/OCI on Linux for the recommended portable deployment.
- Linux with `systemd` for the supplied host-native deployment.
- A Telegram bot token from `@BotFather`.
- Outbound HTTPS access to Telegram and `https://api.getbible.net`.
- Public HTTPS when the Mini App is enabled. Host-native setup can manage Caddy
  on TCP `80`/`443`; Docker leaves those ports and ingress entirely external.

The Docker image does not claim ports 80 or 443 and contains no Caddy. It
serves each bot on configured application ports and leaves TLS and routing to
the surrounding platform. It supports both one bot per container and a
supervised multi-bot container. See [Docker deployment](docs/DOCKER.md).

Published Linux AMD64 and ARM64 images are available from GitHub Container
Registry:

```bash
docker pull ghcr.io/getbible/robot:2.1.0
```

Stable releases receive exact version, minor, major, and `latest` tags. The
full source-commit tag remains available for immutable rollback. The generated
Compose environment pins the exact reviewed release instead of following a
moving tag.

## Docker quick start

```bash
./setup.sh docker-init
${EDITOR:-vi} .env
./setup.sh docker-validate
./setup.sh docker-deploy
./setup.sh docker-doctor
```

The default Compose model pulls the configured published image, runs one bot
in one 256 MiB container, and publishes only its configured Mini App port.
Missing configuration is reported through container stdout/stderr. The
versioned `compose.yaml` and private generated `.env` expose the image version
plus application, memory, CPU, PID, cache, session, rate, abuse, and log
controls. Use `./setup.sh docker-config` to edit, validate, and apply values,
`./setup.sh docker-update` to pull and recreate after selecting a newer image,
`./setup.sh docker-manage` for the in-container operations menu, or
`./setup.sh docker-shell` for a non-root shell. Multi-bot mode remains
available through `./setup.sh docker-deploy --multi`; repository developers
can explicitly build local source with `./setup.sh docker-deploy --build`.

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
(cd miniapp && npm ci --ignore-scripts && npx playwright install chromium)
bash scripts/run-checks.sh
```

Run only the unit suite while iterating:

```bash
venv/bin/python -m unittest discover -s tests -v
(cd miniapp && npm run check)
(cd miniapp && npm run test:browser)
```

## Production installation summary

Deploy an exact reviewed robot commit and run the questionnaire:

```bash
git clone https://github.com/getbible/robot.git
cd robot
git checkout --detach <reviewed-commit-sha>
sudo ./setup.sh install
```

The manager creates a separate locked Linux account, exact hashed environment,
root-only token and editable content files, cache, state, JSONL log, rotation
policy, health port, and hardened systemd service for every named instance.
Tokens are entered with terminal echo disabled and are never accepted as
command-line arguments. The questionnaire lets each instance choose polling or
an HTTPS webhook, configures the optional same-instance Mini App on a separate
loopback port, transactionally manages its Caddy automatic-HTTPS route, and
synchronizes its Telegram command menu and profile text.

```bash
sudo getbible-robot list
sudo getbible-robot status production
sudo getbible-robot logs production
sudo getbible-robot doctor production
sudo getbible-robot delivery production
sudo getbible-robot miniapp production
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
- [Docker deployment](docs/DOCKER.md)
- [Configuration reference](docs/CONFIGURATION.md)
- [Interactive Bible and search workflows](docs/INTERACTIONS.md)
- [Telegram Mini App deployment](docs/MINI_APP.md)
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
