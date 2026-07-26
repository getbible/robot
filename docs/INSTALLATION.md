# Installation

The supported production installation path is the interactive multi-instance manager in `setup.sh`. It creates an isolated Linux account, application checkout, exact hashed Python environment, secret file, cache, state directory, JSONL log, log rotation, and hardened `systemd` service for each instance.

## Quick production installation

Clone the reviewed repository and check out the exact commit whose CI and CodeQL gates passed:

```bash
git clone https://github.com/getbible/robot.git
cd robot
git checkout --detach <reviewed-commit-sha>
git status --short
sudo ./setup.sh install
```

The checkout must have no tracked modifications. The manager displays the exact commit before doing any work and requires confirmation.

After the first installation, the manager is available system-wide:

```bash
sudo getbible-robot list
sudo getbible-robot status <instance>
sudo getbible-robot logs <instance>
sudo getbible-robot doctor <instance>
```

Running `sudo getbible-robot` without a command opens the operations menu.

## Supported host

- Linux with `systemd`.
- Python 3.10, 3.11, or 3.12.
- Outbound HTTPS to Telegram, PyPI during installation, GitHub when cloning, and `https://api.getbible.net` at runtime.
- `git`, `curl`, `tar`, `logrotate`, `iproute2`, and the matching Python `venv` package.
- A dedicated Telegram Bot API token from `@BotFather` for each running instance.

On Debian, Ubuntu, Fedora, or another `dnf` host, the manager offers to install missing host packages. It refuses unsupported Python versions.

## Setup questionnaire

`sudo ./setup.sh install` asks for:

1. confirmation of the exact reviewed commit and Python interpreter;
2. an instance/user-space name;
3. the Telegram token, entered twice with terminal echo disabled;
4. the Telegram display name, short description, and description;
5. the default GetBible translation, with KJV selected by default;
6. polling or HTTPS webhook delivery;
7. for webhook mode, the public URL, local loopback port, optional fixed public
   IP, and confirmation that the reverse proxy is ready;
8. a unique loopback health/metrics port;
9. metadata-only or content audit logging;
10. whether handled Telegram command messages should be deleted;
11. whether the service should be enabled and started;
12. optional live token verification through Telegram.

The token is never accepted as a command-line argument, printed, added to setup logs, or copied into instance metadata. Setup also rejects a token already assigned to another local instance. One token represents one bot and must not be active in multiple local instances, regardless of whether it uses polling or a webhook.

Polling requires only outbound HTTPS. Webhook mode requires public HTTPS through
a reverse proxy; the application listener remains on loopback. See
[Telegram delivery](WEBHOOKS.md) before choosing webhook mode.

## Instance and account names

The instance name uses 2–24 lowercase letters, numbers, or single hyphens. For example:

```text
production
staging
community-02
```

The isolated locked Linux account is derived predictably:

```text
production   -> gb-production
staging      -> gb-staging
community-02 -> gb-community-02
```

The account is a system account with a locked password and `/usr/sbin/nologin`. It cannot modify application source, the virtual environment, configuration, other instances, or the manager.

## Per-instance filesystem layout

For an instance named `production`:

| Purpose | Path | Ownership/access |
|---|---|---|
| Application and exact virtual environment | `/opt/getbible-robot/production/app` | `root:gb-production`, mode `0750` directories/`0640` files |
| Secret environment | `/etc/getbible-robot/production.env` | `root:root`, mode `0600` |
| Welcome/help content | `/etc/getbible-robot/production.{welcome,help}.txt` | `root:gb-production`, mode `0640` |
| Non-secret deployment metadata | `/etc/getbible-robot/instances/production.conf` | `root:root`, mode `0600` |
| Runtime cache | `/var/cache/getbible-robot/production` | `gb-production`, mode `0700` |
| State/home | `/var/lib/getbible-robot/production` | `gb-production`, mode `0700` |
| Structured application log | `/var/log/getbible-robot/production.jsonl` | `gb-production`, mode `0640` |
| Service | `getbible-robot@production.service` | hardened template |

Every instance receives an independent account, token, environment, cache, health port, log, process, memory/task limit, and deployment record.

## Logging choice

The safe default is:

```dotenv
AUDIT_LOG_MODE="metadata"
```

It records event type, instance, translation, filter modes, result counts, selected counts, message counts, outcomes, and correlation IDs. It does not store Telegram text, Scripture references, search terms, user IDs, chat IDs, tokens, or response bodies.

If the operator explicitly chooses content logging, the manager sets:

```dotenv
AUDIT_LOG_MODE="content"
```

The same metadata is retained and final Scripture references plus search terms are added. This is useful for a controlled test instance, but it stores user-provided content and therefore requires an appropriate privacy, access, and retention policy.

Both modes write JSON objects to the per-instance file and to the system journal. Log rotation is installed in `/etc/logrotate.d/getbible-robot`.

## What setup validates

Before enabling a service, setup:

- verifies the source is a clean Git checkout;
- deploys the exact displayed commit in detached state;
- creates a fresh virtual environment;
- installs `requirements.txt` with `--require-hashes`;
- runs `pip check`;
- validates every environment value with the deployed code;
- makes the code readable only by the matching instance group;
- enters the application and imports its configuration as the real locked service account;
- optionally calls Telegram `getMe` without printing the token;
- verifies the instantiated `systemd` unit;
- starts the service only after all build checks pass;
- synchronizes the Bot API command list and configured profile metadata;
- verifies that Telegram's registered webhook state matches the selected
  polling/webhook mode;
- waits for `/readyz` when health checks are enabled.

Partial installation failures before unit verification are cleaned up transactionally. A service that starts but does not become ready is retained for diagnosis through `getbible-robot doctor` and `getbible-robot logs`.

## First validation

```bash
sudo getbible-robot status production
sudo getbible-robot doctor production
sudo getbible-robot logs production 100
```

For webhook mode, also validate the public route and Telegram registration:

```bash
sudo getbible-robot status production
sudo getbible-robot doctor production
```

Then use a private conversation with the dedicated bot:

```text
/help
/bible
/bible John 3:16
/bible John 3:16-18 kjv
/search grace
/search
```

Confirm:

- an explicit reference posts immediately;
- guided Bible and search workflows post only after confirmation;
- links use `https://getbible.life`;
- data comes from `https://api.getbible.net`;
- invalid or oversized input returns a safe generic response;
- no token, raw exception, secret path, Telegram user ID, or chat ID appears in logs;
- exact query content appears only when content audit mode was deliberately enabled.

Complete [Testing](TESTING.md) before production rollout.

## Install more instances

Run the same reviewed checkout again:

```bash
cd robot
sudo ./setup.sh install
sudo getbible-robot list
```

The selector automatically offers unused health ports and prevents instance, account, and token collisions.

## Development-only installation

No production account or `systemd` unit is required:

```bash
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
bash scripts/run-checks.sh
```

To run a live development bot, copy `.env.template` to `.env`, use a dedicated test token, keep `INSTANCE_NAME=local`, leave `LOG_FILE` empty or choose a writable absolute path, and select an unused loopback `HEALTH_PORT`.

Never run two active processes with the same Telegram token. A polling conflict
causes the robot to stop with a non-restarting exit status instead of repeatedly
fighting the other poller.
