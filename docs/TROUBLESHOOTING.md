# Troubleshooting

Start with the manager. It resolves the selected instance's account, paths, service, port, deployment metadata, and log file without printing its token.

```bash
sudo getbible-robot status production
sudo getbible-robot doctor production
sudo getbible-robot runtime production
sudo getbible-robot logs production 200
```

Review output for sensitive content before sharing it. Content audit mode can include user-provided search terms and final references.

## Setup stops before creating the service

Common causes:

- source is not a Git checkout or has tracked modifications;
- source lacks `setup.sh`, `.env.template`, the lock, or unit template;
- Python is outside 3.10–3.14;
- required host packages cannot be installed;
- instance name is invalid or already exists;
- derived `gb-<instance>` account already exists unmanaged;
- token shape is invalid or token belongs to another local instance;
- webhook public URL, private backend address/port, fixed IP, or secret is invalid;
- Mini App public URL or loopback port is invalid, already used, or matches the
  webhook port;
- health port is already listening;
- hashed dependency installation or `pip check` fails;
- target configuration fails validation;
- instantiated systemd unit fails verification.

Failures before installation commit are cleaned up transactionally. Correct the cause and rerun `sudo ./setup.sh install`.

## Installed service does not become ready

The manager intentionally retains a fully built instance when startup fails so it can be diagnosed:

```bash
sudo getbible-robot doctor production
sudo getbible-robot logs production 300
sudo journalctl -u getbible-robot@production.service -b --no-pager
```

Typical causes:

- Telegram rejected or revoked the token;
- another host/process is polling with the same token;
- the configured webhook URL does not reach the exact private backend path;
- the enabled Mini App listener cannot bind its assigned private backend port;
- outbound DNS, TCP 443, system time, or CA trust is broken;
- selected health port became occupied;
- file ownership was changed after setup;
- GetBible API or Telegram initialization is unavailable.

Do not put the token into a support ticket. Rotate it through `@BotFather` when in doubt.

## Repeated Telegram `Conflict` errors

Telegram permits only one active `getUpdates` poller for a bot token. The robot
now treats `Conflict` as an operational stop condition: it logs one critical
message, exits with status 75, and systemd does not restart that status.

Find and stop the other process or host before restarting:

```bash
sudo systemctl list-units --type=service --all | grep -Ei 'getbible|telegram'
sudo pgrep -af 'bot\.py|getbible.*robot'
sudo getbible-robot status production
```

If webhook delivery is preferred, prepare the public HTTPS route and run:

```bash
sudo getbible-robot delivery production
```

Do not “fix” the conflict by allowing both processes to restart.

## Webhook is registered but updates do not arrive

```bash
sudo getbible-robot doctor production
sudo getbible-robot status production
sudo getbible-robot logs production 200
```

Confirm DNS, certificate chain, inbound public port, reverse-proxy route, and
the exact path printed by the manager. The local webhook port must remain bound
to `127.0.0.1`; Telegram connects to the reverse proxy, not that port directly.
See [Telegram delivery](WEBHOOKS.md).

## Mini App does not open or authorize

```bash
sudo getbible-robot status production
sudo getbible-robot doctor production
sudo getbible-robot logs production 200
sudo ss -ltnp | grep ':9201'
```

The assigned port must listen only on `127.0.0.1`. `doctor` also confirms that
the generated route matches every enabled instance, the complete Caddyfile
validates, `caddy.service` is enabled and active, and the public HTTPS URL has a
valid certificate and returns the expected Mini App shell.

The cloud firewall must allow public TCP `80` and `443`, but must not expose
`9201` or the instance's assigned Mini App port. Requests reach the Mini App
through Caddy; they never connect to the loopback listener directly.

Probes such as `/.env`, `/.git/*`, and `/wp-admin` should return an empty `404`
from Caddy and must not appear as Tornado access events. The bounded
`/api/v1/*` namespace is intentionally forwarded so a newly deployed backend
route cannot be stranded behind stale endpoint enumeration; unknown API paths
must still receive Robot's deny-by-default `404`. If a non-API probe reaches
Python, or an API request becomes `502` while Robot is healthy, run
`sudo getbible-robot doctor INSTANCE`, then use the manager's Mini App or
upgrade flow to regenerate and transactionally validate the route.

If setup reports a DNS error, create or correct the public `A`/`AAAA` record
and ensure inbound TCP `80` and `443` reach this host. If Caddy validation or
reload fails, inspect the reported unmanaged Caddyfile conflict; the manager
restores the previous Caddyfile and generated route automatically. Do not edit
the marked import or `/etc/caddy/getbible-robot.caddy`.

An ordinary browser may retrieve the application shell; that is not a security
failure. Protected data and action APIs must reject missing, expired,
malformed, or user-mismatched authorization. Raw Telegram `initData` is
validated at the initial session exchange only; later protected requests use
Robot-issued opaque bearers. Launch the app
again from the bot, verify the server clock, and check that the configured bot
token belongs to the bot that opened the app. A button from before a restart
or upgrade still opens the Mini App: its launch token is gone from memory, so
the exchange logs that it is opening a private session at Home instead of the
route the button once promised. For group launch failures,
confirm the Main Mini App URL in `@BotFather`. See
[Mini App deployment](MINI_APP.md).

### Users cannot open the Mini App after an upgrade

"This launch is no longer active" after a *fresh* `/bible` means the exchange
refused signed launch data, not the button. Telegram clients reuse the same
signed data from one launch to the next; releases before this one refused it
when it named an earlier session or was older than five minutes, so later
launches on such a phone failed, on iPhone and Android alike. The launch token
the robot issued now proves the live tap and such data is accepted with it. If
the message persists on a current release, the server clock is the next
suspect: `doctor` reports it, and signed data more than thirty seconds in the
future is refused.

Launch tokens live in process memory. After an upgrade restart, every **Open
getBible.Life** button already sitting in a chat names a launch the new
process has never seen. Older releases answered that tap with `401` and a
gate offering only **Close**, and tapping the same button again repeated it,
which is exactly what "closing and reopening" means to a reader. Such a tap
now opens a private session at Home and logs that the launch was unknown; a
fresh `/bible` or `/search` message still gives the full launch. Telegram's
Android client also brings back a minimized Mini App tab whenever the
requested URL matches it; the menu button's URL now carries the packaged
client's fingerprint, so a tab minimized before an upgrade is not reused.

A shell that runs a previous deployment's JavaScript is a separate failure:
the module graph fails before a single line of the app runs, nothing is left
to show an error, and the opening spinner never ends. Whether a Telegram
WebView ever serves such a stale module is not confirmed, so the possibility
is removed instead. The shell loads its scripts from `build/<fingerprint>/`,
a prefix derived from the packaged client's bytes, so a launch after an
upgrade names addresses no earlier deployment used. If a module still cannot
be loaded, the `boot.js` watchdog shows the ordinary gate with **Try again**
instead of the spinner.

If a report persists after the upgrade:

```bash
curl -fsS "$MINI_APP_PUBLIC_URL/" | grep -o 'src="./build/[a-f0-9]*/app.js"'
curl -fsSI "$MINI_APP_PUBLIC_URL/build/<fingerprint>/app.js"
```

The shell must name a fingerprint and the module must answer `200` with
`Cache-Control: no-store` through the public route. An empty `404` for the
module means the reverse proxy does not forward `build/*`; rerun the upgrade or
regenerate the managed Caddy route, or add the prefix to an external proxy.
`doctor` and the upgrade postflight perform this exact check.

If **Sync now** fails, first verify the user remains approved with
`sudo getbible-robot contributions INSTANCE status`, then rerun
`sudo getbible-robot doctor INSTANCE`. The contributor panel appears for any
approved contributor with a live session. One click produces one or more
sequential `POST .../api/v1/contributions/events` requests; it must not open
a WebSocket or target another port. A lost response is safe to retry: every
event carries a stable `client_event_id` and Robot replays it without
duplicating moderation events. HTTP `401` means the session is invalid. `403`
means the user is not an approved contributor, current approval/disclosure
policy refused the batch, or the batch carried a stale `contribution_token`;
the app recovers a fresh token automatically with one ordinary status
refresh. `409` means an event ID
was reused with different content, `429` paces the drip through
`Retry-After`, and `413` means the general 64 KiB API bound was exceeded.

## An approved contributor sees the panel but every Sync fails

Search works, the contributor panel is visible, and every **Sync now** ends
in an error while nothing reaches the review queue.

**On a Mini App older than this release** the cause was the first-sync
disclosure itself: it was shown through Telegram's `showAlert`, whose SDK
throws for any message over 256 characters, so the very first Sync failed
before any request was sent and — since the acknowledgement travels with
that first batch — every later Sync repeated it. Upgrade the instance; the
disclosure is now an in-app sheet. No server data needs repair: the
contributor's approval and personal data were never touched.

**On this release** the remaining mechanism is a contribution store that
accepts **reads** (the application row says approved, so the panel appears)
but refuses **writes**, so the server cannot issue a contributor token or
record events. The Mini App names it directly ("could not issue a
contributor token … ask the administrator to run Diagnostics") and the
application log carries an `ERROR` line beginning `A contributor token could
not be issued`.

Causes seen in practice:

- A table missing from a store already at the current schema version (an
  interrupted upgrade or a rollback across the retired sync transports). The
  store now recreates any missing table on every open, so a service restart
  repairs it; `setup.sh doctor` reports `missing tables` if it recurs.
- A root-owned `contributions.sqlite3-wal` or `-shm` sidecar beside a correctly
  owned database, left by a session run as root. The service can read but not
  write. `setup.sh doctor` and every upgrade preflight now refuse a sidecar not
  owned by the service account; stop the service, `chown` or remove the
  sidecars, start it again.
- A read-only file or directory, a full disk, or a quota. `setup.sh doctor`
  runs the same rolled-back write probe the runtime needs and prints the
  exact SQLite error.

Run `setup.sh doctor <instance>`; the store checks name the fault. Every other
part of the contribution path is exercised end to end by the real-server
browser test (`miniapp/tests/browser/contributor-real-server.test.mjs`).

## Service fails with `status=200/CHDIR`

This status means systemd could not enter the configured application directory
as the instance account; Python and Telegram have not started yet. Confirm and
repair it from the exact reviewed checkout:

```bash
sudo getbible-robot doctor production
sudo ./setup.sh repair production
sudo getbible-robot status production
```

The repair keeps the application root-owned, grants read/traverse access only
to `gb-production`, verifies the import as that real account, clears the
systemd start limit, and starts the service if it is enabled. Do not work around
this with world-writable or recursively mode-`0777` permissions.

## Configuration will not validate

Use:

```bash
sudo getbible-robot config production
```

If validation fails, the manager restores the prior file automatically. Common invalid values include:

- missing/template/malformed token;
- conflicting `TELEGRAM_API_TOKEN` and `TELEGRAM_TOKEN`;
- invalid `INSTANCE_NAME`, relative `LOG_FILE`, or audit mode;
- public/non-loopback health host;
- invalid URL, limit, timeout, port, boolean, translation, or log level;
- an enabled Mini App without an HTTPS public URL, a non-loopback Mini App
  listener, or a Mini App/webhook port collision;
- total verse budget smaller than the per-reference budget.

Do not source the environment file or print it. The manager parses it through `python-dotenv` and validates it with the deployed application.

## JSON log is missing or not updating

```bash
sudo getbible-robot doctor production
sudo stat /var/log/getbible-robot/production.jsonl
sudo journalctl -u getbible-robot@production.service -n 100 --no-pager
sudo logrotate --debug /etc/logrotate.d/getbible-robot
```

Expected file ownership is `gb-production:gb-production`, mode `0640`. The service unit grants write access only to that exact file and the instance cache.

`LOG_LEVEL=INFO` is needed for normal audit events. `AUDIT_LOG_MODE=metadata`
omits query content by design. Choose `content` only through an approved
privacy decision. Identity is independent: `AUDIT_IDENTITY_MODE=disabled`
omits it, `pseudonymous` records keyed identifiers, and `raw` records numeric
Telegram IDs and resolved Mini App client IPs. Raw logs require appropriate
access and retention.

For Docker, application logs are on stdout/stderr:

```bash
./setup.sh docker-logs getbible-robot-production 500
./setup.sh docker-doctor getbible-robot-production
```

If an edited Docker setting did not take effect, validate and recreate the
container rather than using a plain Docker restart:

```bash
./setup.sh docker-validate
./setup.sh docker-restart
```

## A client address looks wrong or identical for every Mini App user

Telegram command updates do not contain end-user IP addresses. Client IP
logging applies only to Mini App HTTP requests.

Managed Caddy mode trusts forwarded addresses from loopback. External mode
assumes the operator controls backend access and trusts the standard forwarded
client address supplied by HAProxy, Traefik, Nginx, or another reverse proxy.
If all requests show the proxy address, confirm that the proxy sends
`X-Forwarded-For`. `MINI_APP_TRUSTED_PROXY_CIDRS` remains available as an
optional advanced restriction.

## Rate limits or abuse controls affect normal navigation

Inspect `inbound_rate_limited` and `mini_app_request` events. A normal
authenticated translation/book/chapter/verse navigation request costs
`MINI_APP_NAVIGATION_RATE_COST` (default `0.25`); session exchange, search,
Scripture retrieval, and posting cost one full token.

A temporary block requires repeated individual user or client exhaustion
within `ABUSE_WINDOW_SECONDS`. Chat-wide saturation does not create an
individual block. When a block begins, the robot sends one private or
per-user-ephemeral `ABUSE_WARNING_MESSAGE`; subsequent warnings are
cooldown-limited.

Before increasing a rate, verify whether the same raw or pseudonymous identity
dominates the events. If legitimate users are distributed normally, change
the corresponding Compose environment value, run `docker-validate`, and apply
it with `docker-restart`.

## `/healthz` works but `/readyz` returns 503

The process is alive but the Scripture circuit is open or shutdown is in progress:

```bash
sudo getbible-robot runtime production
sudo getbible-robot logs production 100
```

Inspect circuit, repository failure, and timeout metrics. After `CIRCUIT_RECOVERY_SECONDS`, one request is allowed as a half-open recovery probe. Fix the upstream/network condition instead of restarting repeatedly.

## Scripture requests time out or report busy

`RobotBusy` means bounded worker capacity was not acquired within `LOOKUP_QUEUE_TIMEOUT`. A timed-out synchronous worker deliberately retains its permit until the real thread exits, preventing an unbounded executor queue.

Search work and direct references use independent pools and circuits. A
Telegram-native search is one HTTPS request to the public Search API bounded
by `SEARCH_TIMEOUT`; nothing is downloaded or indexed, so a slow search means
a slow upstream, not local work. Mini App searches never reach the robot.

If logs report `RepositoryResponseTooLarge`, compare the response with
`GETBIBLE_MAX_RESPONSE_BYTES` for catalogue and Query API bodies, or with
`SEARCH_MAX_RESPONSE_BYTES` for Search API bodies; the two budgets are
independent.

Measure upstream latency, memory, and the applicable worker pool. Do not raise
concurrency, timeouts, result sizes, or message budgets until the impact is
tested.

Structured `capacity_queue_rejected`, `lookup_timed_out`,
`upstream_circuit_rejected`, `instance_memory_pressure`, and
`instance_memory_limit_exceeded` events identify whether the barrier is worker
capacity, upstream latency, circuit protection, a warning threshold, or the
hard child RSS guard. `instance_memory_pressure_cleared` confirms recovery
below the warning band.

## `/bible` returns a temporary-unavailable reference

Find the matching request ID without exposing the environment file or token:

```bash
sudo getbible-robot logs production 500 | grep -i 'reference-id'
```

Replace `reference-id` with the eight-character reference displayed by the bot.
The matching operator log records only controlled exception class names. It
does not record exception messages, repository URLs, filesystem paths, tokens,
or user content.

An empty `/bible` command must open the translation picker without resolving a
Scripture reference. The live translation catalog may omit optional display
language labels; such omissions are accepted, while structurally unsafe entries
are omitted individually. If every entry is unusable, the catalog still fails
closed.

## Interactive panel expired

Run `/bible` or `/search` again. Panels are process-local and expire after `INTERACTION_TTL_SECONDS`. Restarting one instance invalidates only that instance's panels.

In a group, the user who opened the panel must use its controls and reply directly to its selective prompt. Other users and older prompt replies are ignored.

## Incorrect links or results

Expected boundaries:

```text
GETBIBLE_API_BASE_URL=https://api.getbible.net
GETBIBLE_QUERY_BASE_URL=https://query.getbible.net
GETBIBLE_SEARCH_BASE_URL=https://search.getbible.net
GETBIBLE_WEB_BASE_URL=https://getbible.life
```

Catalogues come from the Main API, references from the Query API, search
results from the Search API; Telegram links use the website host. Run the renderer, service, catalog, and command tests before deploying any fix.

## Search answers `429` or `503`

Search is served by the public Search API, from the browser for the Mini App
and from the robot for the Telegram-native `/search`. Both report its
`application/problem+json` answers rather than retrying through them:

- `429 rate_limited` — the origin's rate limit was reached; the `Retry-After`
  header says how long to wait. The Mini App shows the wait and offers a
  retry; the robot reports the search as temporarily unavailable and counts
  the answer against its search circuit. Raising `MAX_CONCURRENT_SEARCHES`
  cannot push through a `429`; it only queues more requests behind it.
- `503 busy` or `503 search_timeout` — the service is saturated or the query
  exceeded its own execution budget. Wait the announced `Retry-After`, then
  retry; narrow a very broad query with a scope or book filter.
- `503 repository_unavailable` or `readiness_failed` — the service cannot
  reach its own data. Nothing on the robot host can repair it.

Confirm the origin's state from the host, without the robot:

```bash
curl -sS -D - -o /dev/null "https://search.getbible.net/v2/kjv?q=grace&limit=1"
```

A `200` with `Cache-Control` and `ETag` headers means the service is
answering; a problem document with `Retry-After` means wait. If the robot's
`/readyz` reports an open search circuit while the origin answers `200`,
check outbound DNS, TCP 443, and CA trust from the service account, then wait
`CIRCUIT_RECOVERY_SECONDS` for the half-open probe. `/metrics` publishes
`getbible_robot_search_circuit_open` for that circuit.

## Search reports an unknown translation

`404 translation_not_found` means the Search API does not serve the requested
translation code. `/search` uses the reader's saved translation, so the code
usually comes from an earlier `/bible` choice. Confirm it directly:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "https://search.getbible.net/v2/<translation>?q=grace&limit=1"
```

A `301` here means the code is not a catalogue code; both clients treat a
redirect as an error rather than following it to a guessed translation.
Choose another translation in `/bible` or `/search`.

## Search returns more or fewer results than it used to

Matching semantics belong to the Search API, and its response carries an
`engine_version` that moves whenever they change, independent of any
translation `sha`. Confirm a step in result counts coincides with that value
rather than with your own deployment:

```bash
curl -sS "https://search.getbible.net/v2/kjv?q=grace&limit=1" | grep -o '"engine_version":[0-9]*'
```

If it has not moved, investigate the query, the filters and the translation
`sha` instead. The robot no longer publishes an engine version of its own.
See [Search](SEARCH.md).

## A Mini App page open across the upgrade cannot search

A page loaded before this release still posts to the robot's former
`/api/v1/search` route, which no longer exists and answers `404`. Reopening
the Mini App loads the current build from `build/<fingerprint>/`, which
searches the Search API directly, and the error stops.

## Search is slow

The robot adds no work of its own to a search: no download, no parsing, no
index. Measure the origin from the host and compare it with what the reader
sees:

```bash
curl -sS -o /dev/null -w '%{time_total}\n' "https://search.getbible.net/v2/kjv?q=grace&limit=25"
```

A Telegram-native search that exceeds `SEARCH_TIMEOUT` is reported as
temporarily unavailable and logged as `lookup_timed_out` on a search
operation; the Mini App's own deadline is stall-based, so a response that is
still arriving is never abandoned.

## Upgrade fails

The manager builds `app.next` before stopping the service. If the new application fails readiness after the swap, it automatically restores the prior `app`.

After any reported rollback:

```bash
sudo getbible-robot status production
sudo getbible-robot doctor production
sudo getbible-robot logs production 300
```

Do not manually copy a lock or virtual environment between application trees.

## Memory or restart pressure

```bash
sudo getbible-robot runtime production
```

Likely causes include increased configured bounds, too many worker threads, repeated upstream stalls, or repeated Telegram initialization failure. The robot holds no translation corpus or index, so its resident size does not grow with the translations searched. Preserve bounded defaults and reproduce under load before changing `MemoryMax`.

## Safe support request

Include:

- instance name, but no token;
- exact robot commit;
- Python, operating-system, and systemd versions;
- CI and CodeQL URLs;
- `doctor` result;
- redacted names of non-default settings;
- failing test/check;
- incident correlation ID and sanitized JSON event;
- expected and observed behavior;
- whether it reproduces with a dedicated test bot.

Report vulnerabilities privately according to [`SECURITY.md`](../SECURITY.md).
