# Configuration

GetBible Robot validates all configuration before Telegram polling begins. Invalid security-sensitive values fail startup instead of silently falling back.

The production service reads `/etc/getbible-robot.env`. Local development may use a `.env` file in the checkout. Environment variables already present in the process take precedence over `.env` values.

## Required secret

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `TELEGRAM_API_TOKEN` | none | Required; no whitespace; at most 256 characters; placeholder values rejected | Telegram bot token from `@BotFather` |
| `TELEGRAM_TOKEN` | none | Deprecated migration alias | Accepted only when `TELEGRAM_API_TOKEN` is absent; startup fails if both disagree |

Store the token outside Git with restrictive permissions. Revoke and replace it immediately through `@BotFather` if disclosure is suspected.

## Scripture and public-link settings

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `TRANSLATION` | `kjv` | Lowercase letters, numbers, `_`, or `-`; 1–30 characters | Default translation abbreviation |
| `DEFAULT_VERSE` | `1 John 3:16` | Non-empty; at most 256 characters | Used when `/bible` has no arguments |
| `GETBIBLE_API_BASE_URL` | `https://api.getbible.net` | HTTPS base URL; no credentials, path, query, or fragment; loopback HTTP is allowed for tests | Machine-readable Scripture repository |
| `GETBIBLE_WEB_BASE_URL` | `https://getbible.life` | Same URL rules | Base for every clickable link shown in Telegram |
| `WELCOME_MESSAGE` | built-in text | Non-empty; at most 4096 characters | `/start` response |
| `HELP_MESSAGE` | built-in text | Non-empty; at most 4096 characters | `/help` response |

The API and website variables are intentionally different. The API value is used only for data access. The website value is used only for user-facing links.

Literal `\n` sequences in message settings are converted to newlines.

## Repository and worker timeouts

| Variable | Default | Allowed range | Purpose |
|---|---:|---:|---|
| `GETBIBLE_CONNECT_TIMEOUT` | `3.05` seconds | `0.1`–`30` | TCP/TLS connection timeout |
| `GETBIBLE_READ_TIMEOUT` | `6` seconds | `0.5`–`60` | Per-response read timeout |
| `GETBIBLE_REQUEST_RETRIES` | `1` | `0`–`5` | Retries for idempotent repository GET requests |
| `GETBIBLE_MAX_RESPONSE_BYTES` | `8388608` | `1024`–`134217728` | Maximum accepted repository response body |
| `LOOKUP_TIMEOUT` | `20` seconds | `1`–`90` | Overall asynchronous wait for one lookup |
| `LOOKUP_QUEUE_TIMEOUT` | `2` seconds | `0.1`–`30` | Maximum wait for bounded worker capacity |

A lookup timeout does not pretend that its worker thread stopped. The capacity permit remains occupied until the underlying thread actually exits, preventing an unbounded executor queue.

## Request, output, and concurrency budgets

| Variable | Default | Allowed range | Purpose |
|---|---:|---:|---|
| `MAX_INPUT_LENGTH` | `256` | `32`–`1024` | Maximum normalized command-argument length |
| `MAX_REFERENCES` | `8` | `1`–`16` | Maximum semicolon-separated references |
| `MAX_VERSES_PER_REFERENCE` | `100` | `1`–`200` | Maximum verses selected by one reference |
| `MAX_TOTAL_VERSES` | `100` | `1`–`200` | Maximum verses in the whole command |
| `MAX_OUTPUT_CHUNKS` | `8` | `1`–`32` | Maximum Telegram messages produced by one command |
| `MAX_CONCURRENT_LOOKUPS` | `4` | `1`–`32` | Worker threads and simultaneous repository operations |
| `MAX_CONCURRENT_UPDATES` | `16` | `1`–`64` | Telegram updates processed concurrently |

`MAX_TOTAL_VERSES` may not be lower than `MAX_VERSES_PER_REFERENCE`. Telegram text is measured in UTF-16 code units, not Python characters, before chunks are sent.

Do not increase these values merely to make an abusive request succeed. Load-test memory, API behavior, Telegram output, and the `MemoryMax` service setting before raising production limits.

## Inbound rate limits

Every supported or unknown command consumes both a user token and a chat token.

| Variable | Default | Allowed range | Purpose |
|---|---:|---:|---|
| `USER_RATE_CAPACITY` | `4` | `1`–`100` | Per-user burst capacity |
| `USER_RATE_REFILL_PER_SECOND` | `0.2` | `0.01`–`100` | Per-user sustained refill rate |
| `CHAT_RATE_CAPACITY` | `20` | `1`–`500` | Per-chat burst capacity |
| `CHAT_RATE_REFILL_PER_SECOND` | `1` | `0.01`–`500` | Per-chat sustained refill rate |
| `RATE_LIMIT_CACHE_SIZE` | `20000` | `100`–`100000` | Maximum combined user/chat bucket entries |

The bucket registry uses bounded least-recently-used retention so arbitrary identifiers cannot grow memory without limit.

## Circuit breaker

| Variable | Default | Allowed range | Purpose |
|---|---:|---:|---|
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | `1`–`50` | Consecutive repository/timeout failures before opening |
| `CIRCUIT_RECOVERY_SECONDS` | `30` | `1`–`3600` | Delay before allowing one half-open recovery probe |

Validation errors and request-limit rejections do not count as upstream failures.

## Runtime behavior, health, and logging

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `DELETE_COMMAND_MESSAGES` | `false` | `true` or `false` | Attempt to delete user commands after handling; permission failures are non-fatal |
| `DROP_PENDING_UPDATES` | `true` | `true` or `false` | Drop updates accumulated while the bot was offline at startup |
| `HEALTH_HOST` | `127.0.0.1` | `127.0.0.1`, `::1`, or `localhost` only | Health listener address |
| `HEALTH_PORT` | `8081` | `0`–`65535`; `0` disables | Health/readiness/metrics port |
| `LOG_LEVEL` | `INFO` | Standard Python logging level name | Structured JSON log threshold |

The health listener is deliberately loopback-only. Do not expose it publicly without an authenticated, access-controlled proxy.

## Environment-file example

```dotenv
TELEGRAM_API_TOKEN="123456789:replace-with-real-secret"
TRANSLATION="kjv"
GETBIBLE_API_BASE_URL="https://api.getbible.net"
GETBIBLE_WEB_BASE_URL="https://getbible.life"
HEALTH_HOST="127.0.0.1"
HEALTH_PORT="8081"
LOG_LEVEL="INFO"
```

Use quotes for values containing spaces. Do not place shell commands, command substitutions, or exported secrets in the file.

## Validate configuration

Local `.env`:

```bash
venv/bin/python -c 'from config import Settings; Settings.from_env()'
```

Production file:

```bash
sudo bash -c '
  set -a
  . /etc/getbible-robot.env
  set +a
  cd /opt/getbible-robot
  venv/bin/python -c "from config import Settings; Settings.from_env()"
'
```

A successful validation exits zero without printing the token or settings.
