# Configuration

GetBible Robot validates all configuration before Telegram update delivery
begins. Invalid security-sensitive values fail startup instead of silently
falling back.

Each production instance reads `/etc/getbible-robot/<instance>.env`. Local development may use a `.env` file in the checkout. Environment variables already present in the process take precedence over `.env` values.

## Required secret

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `TELEGRAM_API_TOKEN` | none | Required Bot API token shape; template and malformed values rejected | Telegram bot token from `@BotFather` |
| `TELEGRAM_TOKEN` | none | Deprecated migration alias | Accepted only when `TELEGRAM_API_TOKEN` is absent; startup fails if both disagree |

Store the token outside Git with restrictive permissions. Revoke and replace it immediately through `@BotFather` if disclosure is suspected.

`setup.sh` accepts tokens only through a hidden interactive prompt, stores each file as `root:root` mode `0600`, and rejects reuse by another local instance.

## Telegram delivery and profile

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `TELEGRAM_DELIVERY_MODE` | `polling` | `polling` or `webhook` | Selects exactly one Bot API update transport |
| `TELEGRAM_WEBHOOK_PUBLIC_URL` | empty | Required in webhook mode; HTTPS URL with a non-root private path and no credentials/query/fragment; explicit public port is 80, 88, 443, or 8443 | URL registered with Telegram |
| `TELEGRAM_WEBHOOK_LISTEN` | `127.0.0.1` | Loopback only | Private application listener behind the reverse proxy |
| `TELEGRAM_WEBHOOK_PORT` | `9001` | `1024`–`65535` | Per-instance local listener port |
| `TELEGRAM_WEBHOOK_SECRET_TOKEN` | empty | Required in webhook mode; 32–256 safe token characters | Authenticates Telegram's webhook header |
| `TELEGRAM_WEBHOOK_IP_ADDRESS` | empty | Empty or globally routable IPv4/IPv6 | Optional fixed address sent to Telegram |
| `TELEGRAM_WEBHOOK_MAX_CONNECTIONS` | `16` | `1`–`100` | Maximum simultaneous Telegram webhook connections |
| `BOT_NAME` | `GetBible Robot` | 1–64 characters | Bot API display name synchronized at startup |
| `BOT_DESCRIPTION` | built-in text | 1–512 characters | Bot API description synchronized at startup |
| `BOT_SHORT_DESCRIPTION` | built-in text | 1–120 characters | Bot API short description synchronized at startup |

The public reverse proxy terminates TLS and forwards the exact private URL path
to the loopback listener. Do not expose `TELEGRAM_WEBHOOK_PORT` directly. See
[Telegram delivery](WEBHOOKS.md).

## Instance identity and audit logging

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `INSTANCE_NAME` | `local` | 2–24 lowercase letters, numbers, or single hyphens | Tags every JSON event and identifies the isolated deployment |
| `LOG_FILE` | empty | Empty or an absolute path | Optional JSONL application log in addition to journald |
| `AUDIT_LOG_MODE` | `metadata` | `metadata` or `content` | Controls whether user-provided query/reference text may enter audit fields |

The setup manager assigns these values per instance:

```dotenv
INSTANCE_NAME="production"
LOG_FILE="/var/log/getbible-robot/production.jsonl"
AUDIT_LOG_MODE="metadata"
```

`INSTANCE_NAME`, `LOG_FILE`, and `HEALTH_PORT` are manager-owned after installation. `getbible-robot config` rejects changes that would detach the environment from its isolated account, log, metadata, or health endpoint.

Metadata mode records operational choices and outcomes without Telegram message text: source workflow, translation, search filter modes, result counts, selected/reference-group counts, output message count, failures, and correlation IDs. It never records tokens, user IDs, chat IDs, verse bodies, or repository response bodies.

Content mode additionally records normalized search terms and final Scripture references. It must be enabled deliberately and used only where privacy, access, and retention requirements permit storing user-provided content.

## Scripture and public-link settings

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `TRANSLATION` | `kjv` | Lowercase letters, numbers, `_`, or `-`; 1–30 characters | Default translation abbreviation |
| `USER_PREFERENCES_FILE` | empty | Empty or an absolute path | SQLite database for per-user translation defaults; production setup assigns the isolated instance-state path |
| `USER_PREFERENCE_LIMIT` | `100000` | `100`–`1000000` | Maximum saved user translation records before oldest-record eviction |
| `GETBIBLE_API_BASE_URL` | `https://api.getbible.net` | HTTPS base URL; no credentials, path, query, or fragment; loopback HTTP is allowed for tests | Machine-readable Scripture repository |
| `GETBIBLE_WEB_BASE_URL` | `https://getbible.life` | Same URL rules | Base for every clickable link shown in Telegram |
| `WELCOME_MESSAGE` | built-in text | Non-empty; at most 4096 characters | `/start` response |
| `HELP_MESSAGE` | built-in text | Non-empty; at most 4096 characters | `/help` response |
| `WELCOME_MESSAGE_FILE` | empty | Empty or readable absolute UTF-8 path; takes precedence over `WELCOME_MESSAGE` | Editable multi-line `/start` content |
| `HELP_MESSAGE_FILE` | empty | Empty or readable absolute UTF-8 path; takes precedence over `HELP_MESSAGE` | Editable multi-line `/help` content |

The API and website variables are intentionally different. The API value is
used only for data access. The website value is used only for user-facing
links. `TRANSLATION` is the application fallback. Choosing another translation
in `/bible` or `/search` saves it for that Telegram user; later explicit
references, Bible pickers, and searches use the saved value. An empty `/bible`
never substitutes a default verse.

Literal `\n` sequences in inline message settings are converted to newlines.
Production setup creates restricted per-instance content files. Edit them with
`sudo EDITOR=nano getbible-robot content <instance> welcome|help`; do not move
their manager-owned paths in the environment file.

## Repository and worker timeouts

| Variable | Default | Allowed range | Purpose |
|---|---:|---:|---|
| `GETBIBLE_CONNECT_TIMEOUT` | `3.05` seconds | `0.1`–`30` | TCP/TLS connection timeout |
| `GETBIBLE_READ_TIMEOUT` | `6` seconds | `0.5`–`60` | Per-response read timeout |
| `GETBIBLE_REQUEST_RETRIES` | `1` | `0`–`5` | Retries for Librarian and navigation-catalog GET requests |
| `GETBIBLE_MAX_RESPONSE_BYTES` | `67108864` (64 MiB) | `1024`–`134217728` | Maximum accepted full repository/corpus response body |
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
| `SEARCH_RESULT_LIMIT` | `50` | `1`–`200` | Maximum selectable matches retained from one Librarian search |
| `SEARCH_DEADLINE_SECONDS` | `5` | `0.1`–`30` | Librarian's cooperative per-search execution deadline |
| `SEARCH_MAX_RESPONSE_BYTES` | `4194304` (4 MiB) | `65536`–`16777216` | Maximum constructed Librarian search result, separate from corpus downloads |
| `MAX_CONCURRENT_LOOKUPS` | `4` | `1`–`32` | Direct-reference/catalog worker threads and permits |
| `MAX_CONCURRENT_SEARCHES` | `1` | `1`–`8` | Independent expensive-search worker threads and permits |
| `MAX_CONCURRENT_UPDATES` | `16` | `1`–`64` | Telegram updates processed concurrently |

`MAX_TOTAL_VERSES` may not be lower than `MAX_VERSES_PER_REFERENCE`. Telegram text is measured in UTF-16 code units, not Python characters, before chunks are sent.

On 26 July 2026, the largest published corpus measured by uncompressed
`Content-Length` was `thai` at 30,950,679 bytes; KJV was 8,862,703 bytes. The
64 MiB repository cap therefore accommodates the currently observed full
translations, including larger non-66-book corpora, with bounded headroom.
It does not permit a 64 MiB search result: `SEARCH_MAX_RESPONSE_BYTES`,
`SEARCH_RESULT_LIMIT`, and Telegram message limits remain independent. Search
also has its own single-worker default so corpus parsing/indexing cannot occupy
the four direct-reference workers.

Do not increase these values merely to make an abusive request succeed.
Load-test memory, API behavior, Telegram output, and the `MemoryMax` service
setting before raising production limits.

## Inbound rate limits

Every supported or unknown command consumes both a user token and a chat token.
Free-text replies that start searches or change text filters are also charged.
Owner-scoped menu callbacks are serialized per session and do not consume these
command tokens, so a normal picker flow cannot exhaust its allowance merely by
navigating.

| Variable | Default | Allowed range | Purpose |
|---|---:|---:|---|
| `USER_RATE_CAPACITY` | `4` | `1`–`100` | Per-user burst capacity |
| `USER_RATE_REFILL_PER_SECOND` | `0.2` | `0.01`–`100` | Per-user sustained refill rate |
| `CHAT_RATE_CAPACITY` | `20` | `1`–`500` | Per-chat burst capacity |
| `CHAT_RATE_REFILL_PER_SECOND` | `1` | `0.01`–`500` | Per-chat sustained refill rate |
| `RATE_LIMIT_CACHE_SIZE` | `20000` | `100`–`100000` | Maximum combined user/chat bucket entries |
| `RATE_LIMIT_NOTICE_COOLDOWN` | `10` seconds | `1`–`300` | Minimum quiet period before another rejection warning for the same user/chat |

The bucket and rejection-notification registries use bounded least-recently-used retention so arbitrary identifiers cannot grow memory without limit. Rejected floods are silently discarded after the first warning instead of amplifying traffic through Telegram's API.

## Interactive sessions and catalog cache

| Variable | Default | Allowed range | Purpose |
|---|---:|---:|---|
| `INTERACTION_SESSION_LIMIT` | `2000` | `10`–`20000` | Maximum active guided Bible/search sessions |
| `INTERACTION_TTL_SECONDS` | `600` | `60`–`3600` | Idle lifetime of an interactive panel |
| `CATALOG_CACHE_TTL_SECONDS` | `3600` | `60`–`86400` | In-process lifetime of validated translation, book, chapter, and verse navigation metadata |

Sessions are scoped to their originating chat and user and retained only in process memory. A restart expires every open panel. Translation and book catalog responses remain subject to the repository response-byte limit, retry budget, worker capacity, timeout, circuit breaker, and structural validation.

Production setup assigns
`USER_PREFERENCES_FILE=/var/lib/getbible-robot/<instance>/preferences.sqlite3`.
That bounded database survives upgrades and restarts and stores only Telegram
user ID, translation code, and update time. Each instance has its own file.

## Circuit breaker

| Variable | Default | Allowed range | Purpose |
|---|---:|---:|---|
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | `1`–`50` | Consecutive repository/timeout failures before opening |
| `CIRCUIT_RECOVERY_SECONDS` | `30` | `1`–`3600` | Delay before allowing one half-open recovery probe |

Validation errors and request-limit rejections do not count as upstream failures.

## Runtime behavior, health, and logging

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `DELETE_COMMAND_MESSAGES` | `false` | `true` or `false` | Attempt to delete standalone handled commands such as `/start` and `/help`; permission failures are non-fatal |
| `DROP_PENDING_UPDATES` | `true` | `true` or `false` | Drop updates accumulated while the bot was offline at startup |
| `PREWARM_DEFAULT_TRANSLATION` | `true` | `true` or `false` | Load and index the default search corpus before readiness; safe failure does not prevent direct references |
| `HEALTH_HOST` | `127.0.0.1` | `127.0.0.1`, `::1`, or `localhost` only | Health listener address |
| `HEALTH_PORT` | `8081` | `0`–`65535`; `0` disables | Health/readiness/metrics port |
| `LOG_LEVEL` | `INFO` | Standard Python logging level name | Structured JSON log threshold |

The health listener is deliberately loopback-only. Each running instance requires a unique nonzero port. Do not expose it publicly without an authenticated, access-controlled proxy.

Completed `/bible` and `/search` workflows have a stricter cleanup contract
independent of `DELETE_COMMAND_MESSAGES`. After every final Scripture chunk is
successfully delivered, the robot removes the initiating command and only the
bot panel, prompts, acknowledgements, and user replies recorded for that
workflow. The Scripture messages are never recorded for deletion. Failed
lookups preserve the workflow messages for diagnosis and retry, while Telegram
permission or deletion failures are logged and cannot turn a successful
Scripture delivery into an error.

In groups and supergroups, the registered `/bible` and `/search` commands and
their intermediate panels use Bot API 10.2 per-user ephemeral delivery. This is
independent of `DELETE_COMMAND_MESSAGES`: no ordinary group fallback is used if
the private panel cannot be delivered. Private chats continue to use ordinary
messages because the conversation is already private.

## Environment-file example

```dotenv
TELEGRAM_API_TOKEN="123456789:replace-with-real-secret"
TELEGRAM_DELIVERY_MODE="polling"
INSTANCE_NAME="production"
LOG_FILE="/var/log/getbible-robot/production.jsonl"
AUDIT_LOG_MODE="metadata"
TRANSLATION="kjv"
USER_PREFERENCES_FILE="/var/lib/getbible-robot/production/preferences.sqlite3"
USER_PREFERENCE_LIMIT="100000"
GETBIBLE_API_BASE_URL="https://api.getbible.net"
GETBIBLE_WEB_BASE_URL="https://getbible.life"
GETBIBLE_MAX_RESPONSE_BYTES="67108864"
SEARCH_MAX_RESPONSE_BYTES="4194304"
MAX_CONCURRENT_SEARCHES="1"
PREWARM_DEFAULT_TRANSLATION="true"
HEALTH_HOST="127.0.0.1"
HEALTH_PORT="8081"
LOG_LEVEL="INFO"
```

Use quotes for values containing spaces. Do not place shell commands, command substitutions, or exported secrets in the file. The manager parses this file as dotenv data and never sources it as shell code.

## Validate configuration

Local `.env`:

```bash
venv/bin/python -c 'from config import Settings; Settings.from_env()'
```

Production file:

```bash
sudo getbible-robot config production
sudo getbible-robot doctor production
```

`config` validates before accepting changes and restores the prior file on failure. `doctor` validates permissions, configuration, dependencies, deployment identity, unit, service, health, and readiness without printing the token.
