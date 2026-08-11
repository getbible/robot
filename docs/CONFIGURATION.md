# Configuration

GetBible Robot validates all configuration before Telegram update delivery
begins. Invalid security-sensitive values fail startup instead of silently
falling back.

Each production instance reads `/etc/getbible-robot/<instance>.env`. Local development may use a `.env` file in the checkout. Environment variables already present in the process take precedence over `.env` values.

## Required secret

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `TELEGRAM_API_TOKEN` | none | Required Bot API token shape; template and malformed values rejected | Telegram bot token from `@BotFather` |
| `TELEGRAM_API_TOKEN_FILE` | empty | Absolute readable file; mutually exclusive with `TELEGRAM_API_TOKEN` | Docker/Kubernetes secret mount containing the token |
| `TELEGRAM_TOKEN` | none | Deprecated migration alias | Accepted only when `TELEGRAM_API_TOKEN` is absent; startup fails if both disagree |

Store the token outside Git with restrictive permissions. Revoke and replace it immediately through `@BotFather` if disclosure is suspected.

`setup.sh` accepts tokens only through a hidden interactive prompt, stores each file as `root:root` mode `0600`, and rejects reuse by another local instance.

## Telegram delivery and profile

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `TELEGRAM_DELIVERY_MODE` | `polling` | `polling` or `webhook` | Selects exactly one Bot API update transport |
| `TELEGRAM_WEBHOOK_PUBLIC_URL` | empty | Required in webhook mode; HTTPS URL with a non-root private path and no credentials/query/fragment; explicit public port is 80, 88, 443, or 8443 | URL registered with Telegram |
| `TELEGRAM_WEBHOOK_LISTEN` | `127.0.0.1` | Loopback; wildcard only when `CONTAINERIZED=true` | Private application listener behind the reverse proxy |
| `TELEGRAM_WEBHOOK_PORT` | `9001` | `1024`–`65535` | Per-instance local listener port |
| `TELEGRAM_WEBHOOK_SECRET_TOKEN` | empty | Required in webhook mode; 32–256 safe token characters | Authenticates Telegram's webhook header |
| `TELEGRAM_WEBHOOK_SECRET_TOKEN_FILE` | empty | Absolute readable file; mutually exclusive with direct value | Secret-mounted webhook header token |
| `TELEGRAM_WEBHOOK_IP_ADDRESS` | empty | Empty or globally routable IPv4/IPv6 | Optional fixed address sent to Telegram |
| `TELEGRAM_WEBHOOK_MAX_CONNECTIONS` | `16` | `1`–`100` | Maximum simultaneous Telegram webhook connections |
| `CONTAINERIZED` | `false` | `true` or `false` | Permits wildcard container listeners; set and owned by the image |
| `BOT_NAME` | `GetBible Robot` | 1–64 characters | Bot API display name synchronized at startup |
| `BOT_DESCRIPTION` | built-in text | 1–512 characters | Bot API description synchronized at startup |
| `BOT_SHORT_DESCRIPTION` | built-in text | 1–120 characters | Bot API short description synchronized at startup |

The public reverse proxy terminates TLS and forwards the exact private URL path
to the loopback listener. Do not expose `TELEGRAM_WEBHOOK_PORT` directly. See
[Telegram delivery](WEBHOOKS.md).

## Telegram Mini App

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `MINI_APP_ENABLED` | `false` | `true` or `false`; URL required when true | Enables the same-instance Telegram Mini App |
| `MINI_APP_PUBLIC_URL` | empty | Absolute HTTPS URL; optional fixed path; no credentials, query, or fragment | URL opened by Telegram and routed by the public proxy |
| `MINI_APP_LISTEN` | `127.0.0.1` | Loopback; wildcard only when `CONTAINERIZED=true` | Private Mini App HTTP listener |
| `MINI_APP_PORT` | `9201` | `1024`–`65535`; manager-reserved and different from health/webhook ports | Per-instance Mini App listener port |
| `MINI_APP_INIT_DATA_MAX_AGE_SECONDS` | `300` | `30`–`900` | Maximum accepted age of signed Telegram `initData` |
| `MINI_APP_LAUNCH_TTL_SECONDS` | `300` | `30`–`900` | Lifetime of the user-bound bot launch token |
| `MINI_APP_SESSION_TTL_SECONDS` | `900` | `60`–`3600` | Absolute lifetime of authenticated server-side Mini App state |
| `MINI_APP_SESSION_LIMIT` | `200` | `10`–`20000` | Maximum active Mini App sessions |
| `MINI_APP_SESSIONS_PER_USER` | `2` | `1`–`10` | Maximum active sessions retained for one Telegram user |
| `MINI_APP_MAX_SEARCHES_PER_SESSION` | `2` | `1`–`8` | Retained authoritative search pages per session |
| `MINI_APP_MAX_AVAILABLE_SELECTIONS` | `256` | `250`–`1000` | Recent selectable verse objects per session, excluding the separately bounded basket |
| `MINI_APP_MAX_SELECTIONS` | `100` | `1`–`200` | Maximum selected verse items before normalization |
| `MINI_APP_BODY_TIMEOUT_SECONDS` | `10` | `1`–`60` | Maximum time to receive one HTTP request body |
| `MINI_APP_IDLE_TIMEOUT_SECONDS` | `30` | `5`–`300` | Idle connection and incomplete-header timeout |
| `MINI_APP_MAX_HEADER_BYTES` | `16384` | `4096`–`65536` | Maximum accepted HTTP header block |
| `MINI_APP_TRUSTED_PROXY_CIDRS` | `127.0.0.1/32,::1/128` | Comma-separated IPv4/IPv6 networks | Direct peers allowed to supply a forwarded client address |
| `MINI_APP_IP_RATE_CAPACITY` | `60` | `10`–`10000` | Per-client burst capacity for authenticated Mini App API requests |
| `MINI_APP_IP_RATE_REFILL_PER_SECOND` | `10` | `0.1`–`10000` | Per-client sustained refill rate |
| `MINI_APP_SESSION_EXCHANGE_RATE_CAPACITY` | `10` | `1`–`10000` | Per-client burst for unauthenticated session-exchange attempts |
| `MINI_APP_SESSION_EXCHANGE_RATE_REFILL_PER_SECOND` | `0.2` | `0.01`–`10000` | Per-client sustained session-exchange refill |
| `MINI_APP_NAVIGATION_RATE_COST` | `0.25` | `0.05`–`1` | Fractional request cost for lightweight authenticated navigation |
| `MINI_APP_ACCESS_LOG` | `true` | `true` or `false` | Log every Mini App request; errors are always logged |

The HTML shell may be publicly retrievable because Telegram Mini Apps run in a
browser engine on each user's device. It remains inert without fresh
signature-verified Telegram `initData` and a short-lived launch token tied to
the same user and bot workflow. Never put the bot token or authoritative verse
text in browser state. See [Mini App deployment](MINI_APP.md).

`MINI_APP_ENABLED`, `MINI_APP_PUBLIC_URL`, `MINI_APP_LISTEN`, and
`MINI_APP_PORT` are manager-owned in production. Change them only through
`sudo getbible-robot miniapp <instance>` so DNS, retained ports, generated
Caddy routes, validation, reload, public verification, and rollback remain one
transaction.

Telegram Bot API updates do not expose an end-user IP address. Mini App HTTP
requests do. Forwarded addresses are trusted only when the direct connection
comes from `MINI_APP_TRUSTED_PROXY_CIDRS`; untrusted forwarding headers are
ignored. Configure the exact proxy address or network rather than a public or
unnecessarily broad range.

## Instance identity and audit logging

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `INSTANCE_NAME` | `local` | 2–24 lowercase letters, numbers, or single hyphens | Tags every JSON event and identifies the isolated deployment |
| `LOG_FILE` | empty | Empty or an absolute path | Optional JSONL application log in addition to journald |
| `LOG_MAX_BYTES` | `10485760` | 1 MiB–1 GiB | Continuous byte ceiling for the optional JSONL file |
| `AUDIT_LOG_MODE` | `metadata` | `metadata` or `content` | Controls whether user-provided query/reference text may enter audit fields |
| `AUDIT_IDENTITY_MODE` | `pseudonymous` | `disabled`, `pseudonymous`, or `raw` | Controls user/chat/client identity fields in structured events |

The setup manager assigns these values per instance:

```dotenv
INSTANCE_NAME="production"
LOG_FILE="/var/log/getbible-robot/production.jsonl"
AUDIT_LOG_MODE="metadata"
AUDIT_IDENTITY_MODE="pseudonymous"
```

`INSTANCE_NAME`, `LOG_FILE`, `HEALTH_PORT`, and `MINI_APP_LISTEN` are
manager-owned after installation. `getbible-robot config` rejects changes that
would detach the environment from its isolated account, log, metadata, or
loopback listeners.

Metadata mode records operational choices and outcomes without Telegram
message text: source workflow, translation, search filter modes, result counts,
selected/reference-group counts, output message count, failures, and
correlation IDs.

Content mode additionally records normalized search terms and final Scripture references. It must be enabled deliberately and used only where privacy, access, and retention requirements permit storing user-provided content.

Identity mode is independent of content mode. `disabled` omits Telegram and
client identities. `pseudonymous` records stable token-keyed identifiers that
can reveal repeated activity without exposing the original value. `raw`
records Telegram numeric user/chat IDs and the resolved Mini App client IP so
an operator can investigate and contact an abusive Telegram user. Raw identity
logs are personal data and require appropriate access and retention controls.
No mode records tokens, names, usernames, verse bodies, repository response
bodies, Telegram `initData`, or launch/session credentials.

## Scripture and public-link settings

| Variable | Default | Validation | Purpose |
|---|---:|---|---|
| `TRANSLATION` | `kjv` | Lowercase letters, numbers, `_`, or `-`; 1–30 characters | Default translation abbreviation |
| `USER_PREFERENCES_FILE` | empty | Empty or an absolute path | SQLite database for per-user translation defaults; production setup assigns the isolated instance-state path |
| `USER_PREFERENCE_LIMIT` | `10000` | `100`–`1000000` | Maximum saved user translation records before oldest-record eviction |
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
| `GETBIBLE_MAX_RESPONSE_BYTES` | `41943040` (40 MiB) | `1024`–`134217728` | Maximum accepted full repository/corpus response body |
| `LOOKUP_TIMEOUT` | `20` seconds | `1`–`90` | Overall asynchronous wait for one lookup |
| `LOOKUP_QUEUE_TIMEOUT` | `2` seconds | `0.1`–`30` | Maximum wait for bounded worker capacity |
| `REFERENCE_CACHE_LIMIT` | `1000` | `100`–`50000` | Parsed reference and selection cache entries |
| `BOOKS_CACHE_LIMIT` | `16` | `1`–`1000` | In-memory translation book indexes |
| `CHAPTER_CACHE_LIMIT` | `256` | `16`–`10000` | In-memory chapter payloads |
| `SEARCH_CORPUS_LIMIT` | `1` | `1`–`4` | Full translation search corpora retained, in this client and in Librarian's process-wide registry |
| `TRANSLATION_CACHE_LIMIT` | `1` | `1`–`8` | Parsed full translation payloads retained |
| `CACHE_MAX_BYTES` | `268435456` | 32 MiB–8 GiB | Disk-cache budget enforced after the one-day race-safety grace |
| `CACHE_MAINTENANCE_INTERVAL_SECONDS` | `21600` | `300`–`604800` | Interval for pruning stale objects and over-budget cache entries |

A lookup timeout does not pretend that its worker thread stopped. The capacity permit remains occupied until the underlying thread actually exits, preventing an unbounded executor queue.

## Request, output, and concurrency budgets

| Variable | Default | Allowed range | Purpose |
|---|---:|---:|---|
| `MAX_INPUT_LENGTH` | `256` | `32`–`1024` | Maximum normalized command-argument length |
| `MAX_REFERENCES` | `8` | `1`–`16` | Maximum semicolon-separated references |
| `MAX_VERSES_PER_REFERENCE` | `100` | `1`–`200` | Maximum verses selected by one reference |
| `MAX_TOTAL_VERSES` | `100` | `1`–`200` | Maximum verses in the whole command |
| `MAX_OUTPUT_CHUNKS` | `8` | `1`–`32` | Maximum Telegram messages produced by one command or final Mini App post |
| `SEARCH_RESULT_LIMIT` | `50` | `1`–`200` | Maximum selectable matches retained from one Librarian search |
| `SEARCH_DEADLINE_SECONDS` | `5` | `0.1`–`30` | Librarian's cooperative per-search execution deadline, covering request-owned work only |
| `SEARCH_INDEX_BUILD_SECONDS` | `120` | `1`–`600` | Bound on building one translation's search index, and the budget startup prewarming spends |
| `SEARCH_MAX_RESPONSE_BYTES` | `4194304` (4 MiB) | `65536`–`16777216` | Maximum constructed Librarian search result, separate from corpus downloads |
| `MAX_CONCURRENT_LOOKUPS` | `2` | `1`–`32` | Direct-reference/catalog worker threads and permits |
| `MAX_CONCURRENT_SEARCHES` | `1` | `1`–`8` | Independent expensive-search worker threads and permits |
| `MAX_CONCURRENT_UPDATES` | `4` | `1`–`64` | Telegram updates processed concurrently |

`MAX_TOTAL_VERSES` may not be lower than `MAX_VERSES_PER_REFERENCE`. Telegram text is measured in UTF-16 code units, not Python characters, before chunks are sent.

An index build serves every later search of that translation, so it is bounded
by `SEARCH_INDEX_BUILD_SECONDS` instead of being charged to whichever request
happened to arrive first. Within a request, `LOOKUP_TIMEOUT` still applies: a
cold build of a non-default translation may exceed it, in which case that one
search fails while the build continues and serves the searches after it. Keeping
`PREWARM_DEFAULT_TRANSLATION` enabled is what keeps requests off the build path
entirely. See [Search](SEARCH.md).

On 26 July 2026, the largest published corpus measured by uncompressed
`Content-Length` was `thai` at 30,950,679 bytes; KJV was 8,862,703 bytes. The
40 MiB repository cap therefore accommodates the currently observed full
translations, including larger non-66-book corpora, with bounded headroom.
It does not permit a 40 MiB search result: `SEARCH_MAX_RESPONSE_BYTES`,
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
| `RATE_LIMIT_CACHE_SIZE` | `2000` | `100`–`100000` | Maximum combined user/chat bucket entries |
| `RATE_LIMIT_NOTICE_COOLDOWN` | `10` seconds | `1`–`300` | Minimum quiet period before another rejection warning for the same user/chat |
| `ABUSE_REJECTION_THRESHOLD` | `6` | `2`–`100` | Individual rate-limit violations within the window before a temporary block |
| `ABUSE_WINDOW_SECONDS` | `60` | `10`–`3600` | Sliding interval for repeated individual violations |
| `ABUSE_BLOCK_SECONDS` | `300` | `10`–`86400` | Temporary user/client pause after the threshold |
| `ABUSE_WARNING_MESSAGE` | built-in text | Non-empty; at most 4096 characters | Private or ephemeral notice sent when repeated activity is paused |

Mini App session exchange and expensive search, Scripture, and post requests
consume a full request token. Lightweight translation/book/chapter/verse
navigation consumes `MINI_APP_NAVIGATION_RATE_COST`, preserving normal browsing
responsiveness.

User, chat, client, abuse, and rejection-notification registries use bounded
least-recently-used retention so arbitrary identifiers cannot grow memory
without limit. Only repeated individual user/client exhaustion creates an
abuse block; chat-wide saturation does not accuse one user. Rejected floods
are silently discarded after the first cooldown warning instead of amplifying
traffic through Telegram's API.

## Interactive sessions and catalog cache

| Variable | Default | Allowed range | Purpose |
|---|---:|---:|---|
| `INTERACTION_SESSION_LIMIT` | `200` | `10`–`20000` | Maximum active guided Bible/search sessions |
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
| `HEALTH_HOST` | `127.0.0.1` | Loopback; wildcard only when `CONTAINERIZED=true` | Health listener address |
| `HEALTH_PORT` | `8081` | `0`–`65535`; `0` disables | Health/readiness/metrics port |
| `LOG_LEVEL` | `INFO` | Standard Python logging level name | Structured JSON log threshold |

The host-native health listener is deliberately loopback-only. Docker may bind
it to the container network for platform probes. Each running instance requires
a unique nonzero port. Do not expose it publicly without authenticated,
access-controlled ingress.

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
MINI_APP_ENABLED="true"
MINI_APP_PUBLIC_URL="https://bot.example.com/getbible/production"
MINI_APP_LISTEN="127.0.0.1"
MINI_APP_PORT="9201"
MINI_APP_INIT_DATA_MAX_AGE_SECONDS="300"
MINI_APP_LAUNCH_TTL_SECONDS="300"
MINI_APP_SESSION_LIMIT="200"
MINI_APP_SESSIONS_PER_USER="2"
INSTANCE_NAME="production"
LOG_FILE="/var/log/getbible-robot/production.jsonl"
LOG_MAX_BYTES="10485760"
AUDIT_LOG_MODE="metadata"
AUDIT_IDENTITY_MODE="pseudonymous"
MINI_APP_TRUSTED_PROXY_CIDRS="127.0.0.1/32,::1/128"
MINI_APP_IP_RATE_CAPACITY="60"
MINI_APP_IP_RATE_REFILL_PER_SECOND="10"
MINI_APP_SESSION_EXCHANGE_RATE_CAPACITY="10"
MINI_APP_SESSION_EXCHANGE_RATE_REFILL_PER_SECOND="0.2"
MINI_APP_NAVIGATION_RATE_COST="0.25"
MINI_APP_ACCESS_LOG="true"
ABUSE_REJECTION_THRESHOLD="6"
ABUSE_WINDOW_SECONDS="60"
ABUSE_BLOCK_SECONDS="300"
TRANSLATION="kjv"
USER_PREFERENCES_FILE="/var/lib/getbible-robot/production/preferences.sqlite3"
USER_PREFERENCE_LIMIT="10000"
GETBIBLE_API_BASE_URL="https://api.getbible.net"
GETBIBLE_WEB_BASE_URL="https://getbible.life"
GETBIBLE_MAX_RESPONSE_BYTES="41943040"
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

## Docker Compose controls

These operator values belong to the Compose `.env` layer rather than the
application's own `Settings` object:

| Variable | Recommended value | Purpose |
|---|---|---|
| `ROBOT_IMAGE` | `ghcr.io/getbible/robot:2.1.0` | Exact published runtime image |
| `ROBOT_CONTAINER_NAME` | `getbible-robot-production` | Stable container identity |
| `ROBOT_MEMORY_LIMIT` | `256m` | Aggregate container memory ceiling |
| `ROBOT_MEMORY_RESERVATION` | `160m` | Compose memory reservation |
| `ROBOT_CPU_LIMIT` | `1` | Container CPU quota |
| `ROBOT_PIDS_LIMIT` | `48` | Container process/thread ceiling |
| `ROBOT_TMPFS_SIZE` | `16m` | Bounded writable `/tmp` |
| `ROBOT_LOG_MAX_SIZE` | `10m` | Per-file Docker log bound |
| `ROBOT_LOG_MAX_FILES` | `3` | Docker log retention count |

`docker-init` generates the complete editable example. `docker-validate`
pulls and validates `ROBOT_IMAGE`; `docker-update` then recreates the workload
from that image. `ROBOT_BUILD_IMAGE` applies only when the operator explicitly
adds `--build` to use `compose.build.yaml`.
