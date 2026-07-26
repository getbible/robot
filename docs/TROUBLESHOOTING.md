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
- outbound DNS, TCP 443, system time, or CA trust is broken;
- selected health port became occupied;
- file ownership was changed after setup;
- GetBible API or Telegram initialization is unavailable.

Do not put the token into a support ticket. Rotate it through `@BotFather` when in doubt.

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

Measure upstream latency and worker pressure. Do not raise concurrency, timeouts, result sizes, or message budgets until the workload and memory impact are tested.

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
