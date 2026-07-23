# Installation

This guide installs GetBible Robot as a hardened `systemd` service on Linux. A development-only installation is described at the end.

## Supported runtime

- Python 3.10, 3.11, or 3.12.
- A Linux host with `systemd` for the supplied production unit.
- `git`, `python3-venv`, and `curl`.
- A Telegram bot token created through `@BotFather`.
- Outbound HTTPS access to Telegram and `https://api.getbible.net`.

Use a dedicated host or account where practical. Do not reuse a production Telegram token in development or CI.

## Production filesystem layout

```text
/opt/getbible-robot/                 application checkout
/opt/getbible-robot/venv/            exact Python environment
/etc/getbible-robot.env              secret runtime configuration
/etc/systemd/system/getbible-robot.service
/var/cache/getbible-robot/            Librarian cache
```

The checkout and virtual environment should not be writable by the service account. Only the cache directory needs runtime write access.

## 1. Install host prerequisites

On Debian or Ubuntu:

```bash
sudo apt-get update
sudo apt-get install --yes git python3 python3-venv curl
python3 --version
```

Confirm that the selected Python is in the supported range before continuing.

## 2. Create the service account and cache

```bash
if ! id getbible-robot >/dev/null 2>&1; then
  sudo useradd \
    --system \
    --home /nonexistent \
    --shell /usr/sbin/nologin \
    getbible-robot
fi

sudo install -d \
  -o getbible-robot \
  -g getbible-robot \
  -m 0700 \
  /var/cache/getbible-robot
```

The account has no interactive shell and no home directory.

## 3. Check out an reviewed commit

Choose a robot commit for which the `robot/security-gate` and CodeQL checks succeeded. Deploy an exact commit rather than a moving branch:

```bash
sudo git clone https://github.com/getbible/robot.git /opt/getbible-robot
cd /opt/getbible-robot
sudo git fetch --tags origin
sudo git checkout --detach <reviewed-commit-sha>
```

Record the deployed SHA:

```bash
git rev-parse HEAD
```

Keep the checkout owned by an administrative account, normally `root:root`:

```bash
sudo chown -R root:root /opt/getbible-robot
sudo chmod -R go-w /opt/getbible-robot
```

## 4. Build the exact virtual environment

The checked-in lock contains exact versions and hashes:

```bash
cd /opt/getbible-robot
sudo python3 -m venv venv
sudo venv/bin/python -m pip install --upgrade pip
sudo venv/bin/python -m pip install --require-hashes -r requirements.txt
sudo venv/bin/python -m pip check
```

Do not replace this with an unhashed `pip install -r requirements.in` in production. `requirements.in` is for maintainers; `requirements.txt` is the deployable lock.

## 5. Install and edit the configuration

```bash
sudo install \
  -o root \
  -g root \
  -m 0600 \
  .env.template \
  /etc/getbible-robot.env

sudo editor /etc/getbible-robot.env
```

At minimum, replace `TELEGRAM_API_TOKEN`. Retain the intentional URL separation unless operating approved alternatives:

```text
GETBIBLE_API_BASE_URL=https://api.getbible.net
GETBIBLE_WEB_BASE_URL=https://getbible.life
```

See [Configuration](CONFIGURATION.md) before changing limits or timeouts. The token must never be committed to Git.

Validate the configuration without starting polling:

```bash
sudo bash -c '
  set -a
  . /etc/getbible-robot.env
  set +a
  cd /opt/getbible-robot
  venv/bin/python -c "from config import Settings; Settings.from_env()"
'
```

A successful command prints nothing and exits with status zero.

## 6. Install and verify the service

```bash
sudo install \
  -o root \
  -g root \
  -m 0644 \
  deploy/getbible-robot.service \
  /etc/systemd/system/getbible-robot.service

sudo systemd-analyze verify /etc/systemd/system/getbible-robot.service
sudo systemctl daemon-reload
sudo systemctl enable --now getbible-robot.service
```

Inspect startup:

```bash
sudo systemctl status getbible-robot.service --no-pager
sudo journalctl -u getbible-robot.service -n 100 --no-pager
curl --fail http://127.0.0.1:8081/healthz
curl --fail http://127.0.0.1:8081/readyz
```

`/readyz` should return success only after Telegram command registration and initialization have completed.

## 7. Perform the first smoke test

Use a private conversation with the test bot:

```text
/help
/bible
/bible John 3:16
/bible John 3:16-18 kjv
/bible Gen 1:1 aov
/search grace
/search
```

Confirm that:

- verse text is correct;
- `/bible John 3:16` posts immediately using KJV while empty `/bible` waits for guided selection and confirmation;
- both search forms show selectable results and post nothing until **Post selected** is pressed;
- links use `https://getbible.life`;
- data retrieval succeeds through `https://api.getbible.net`;
- invalid and oversized input returns a generic safe response;
- no raw exception, filesystem path, token, or API URL appears in Telegram.

Complete the checklist in [Testing](TESTING.md) before production rollout.

## Development-only installation

For local deterministic testing, no Telegram token is required:

```bash
git clone https://github.com/getbible/robot.git
cd robot
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
venv/bin/python -m unittest discover -s tests -v
```

To run a live development bot, copy `.env.template` to `.env`, use a dedicated test token, set `HEALTH_PORT` to an unused loopback port, and run:

```bash
venv/bin/python bot.py
```

Never run two polling processes with the same Telegram token at the same time.
