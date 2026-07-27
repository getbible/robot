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
- Python is outside 3.10–3.12;
- required host packages cannot be installed;
- instance name is invalid or already exists;
- derived `gb-<instance>` account already exists unmanaged;
- token shape is invalid or token belongs to another local instance;
- webhook public URL, loopback port, fixed IP, or secret is invalid;
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
- the configured webhook URL does not reach the exact loopback path;
- the enabled Mini App listener cannot bind its assigned loopback port;
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

If setup reports a DNS error, create or correct the public `A`/`AAAA` record
and ensure inbound TCP `80` and `443` reach this host. If Caddy validation or
reload fails, inspect the reported unmanaged Caddyfile conflict; the manager
restores the previous Caddyfile and generated route automatically. Do not edit
the marked import or `/etc/caddy/getbible-robot.caddy`.

An ordinary browser may retrieve the application shell; that is not a security
failure. Protected data and action APIs must reject missing, expired,
malformed, replayed, or user-mismatched Telegram authorization. Launch the app
again from the bot, verify the server clock, and check that the configured bot
token belongs to the bot that opened the app. For group launch failures,
confirm the Main Mini App URL in `@BotFather`. See
[Mini App deployment](MINI_APP.md).

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

`LOG_LEVEL=INFO` is needed for normal audit events. `AUDIT_LOG_MODE=metadata` omits query content by design. Choose `content` only through an approved privacy decision.

## `/healthz` works but `/readyz` returns 503

The process is alive but the Scripture circuit is open or shutdown is in progress:

```bash
sudo getbible-robot runtime production
sudo getbible-robot logs production 100
```

Inspect circuit, repository failure, and timeout metrics. After `CIRCUIT_RECOVERY_SECONDS`, one request is allowed as a half-open recovery probe. Fix the upstream/network condition instead of restarting repeatedly.

## Scripture requests time out or report busy

`RobotBusy` means bounded worker capacity was not acquired within `LOOKUP_QUEUE_TIMEOUT`. A timed-out synchronous worker deliberately retains its permit until the real thread exits, preventing an unbounded executor queue.

Search work and direct references use independent pools and circuits. A large
first search may download and index a full translation; with
`PREWARM_DEFAULT_TRANSLATION=true`, this cost occurs once during startup for the
default translation. Later searches reuse the bounded cache.

If logs report `RepositoryResponseTooLarge`, compare the actual corpus size with
`GETBIBLE_MAX_RESPONSE_BYTES`. The production default is 64 MiB; do not lower it
to the old 8 MiB value, which cannot hold KJV. Search result construction remains
separately limited by `SEARCH_MAX_RESPONSE_BYTES`.

Measure upstream latency, memory, and the applicable worker pool. Do not raise
concurrency, timeouts, result sizes, or message budgets until the impact is
tested.

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
GETBIBLE_WEB_BASE_URL=https://getbible.life
```

Data comes from the API host; Telegram links use the website host. Run the renderer, service, catalog, and command tests before deploying any fix.

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

Likely causes include increased configured bounds, too many worker threads, repeated upstream stalls, a large translation/cache, or repeated Telegram initialization failure. Preserve bounded defaults and reproduce under load before changing `MemoryMax`.

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
