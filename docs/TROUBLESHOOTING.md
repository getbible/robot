# Troubleshooting

Start with the service state, recent structured logs, and loopback health endpoints. Never paste a Telegram token, private message, full environment file, or sensitive infrastructure detail into a public issue.

## Standard diagnostic bundle

```bash
cd /opt/getbible-robot
printf 'commit: '; git rev-parse HEAD
printf 'python: '; venv/bin/python --version
sudo systemctl status getbible-robot.service --no-pager
sudo journalctl -u getbible-robot.service -n 200 --no-pager
curl --silent --show-error --include http://127.0.0.1:8081/healthz
curl --silent --show-error --include http://127.0.0.1:8081/readyz
curl --silent --show-error http://127.0.0.1:8081/metrics
venv/bin/python -m pip check
sha256sum requirements.txt deploy/getbible-robot.service
```

Review output for secrets before sharing it privately with maintainers.

## Service will not start

Check the pre-start configuration validation:

```bash
sudo journalctl -u getbible-robot.service -b --no-pager
sudo systemctl show getbible-robot.service \
  -p User -p Group -p EnvironmentFiles -p ExecStartPre -p ExecStart
```

Common causes:

- missing or placeholder `TELEGRAM_API_TOKEN`;
- conflicting `TELEGRAM_API_TOKEN` and deprecated `TELEGRAM_TOKEN`;
- invalid URL, timeout, limit, boolean, or log-level value;
- missing virtual environment or dependencies;
- checkout installed somewhere other than `/opt/getbible-robot` without updating the unit;
- missing service account or cache directory;
- unreadable environment file;
- unsupported Python version.

Validate configuration directly as documented in [Configuration](CONFIGURATION.md), then run:

```bash
sudo systemd-analyze verify /etc/systemd/system/getbible-robot.service
```

## Telegram token or network errors

Symptoms include failure during command registration, polling startup, or repeated Telegram client warnings.

- Confirm the token with `@BotFather`; rotate it rather than posting it for review.
- Ensure only one polling process uses the token.
- Check outbound DNS, TCP 443, system time, and CA certificates.
- Confirm the host is not intercepting TLS with an untrusted certificate.
- Restart only after correcting the cause; repeated restart loops can hit Telegram limits.

The health endpoint starts only after Telegram initialization succeeds. A missing listener during startup can therefore indicate a Telegram initialization failure rather than a health-server defect.

## `/healthz` works but `/readyz` returns 503

The process is alive but not ready. Inspect:

```bash
curl --silent http://127.0.0.1:8081/metrics
sudo journalctl -u getbible-robot.service -n 100 --no-pager
```

The normal reason is an open Scripture repository circuit after repeated timeouts or failures. Check:

- `getbible_robot_circuit_open`;
- `getbible_robot_repository_failures`;
- `getbible_robot_lookup_timeouts`;
- API reachability to `https://api.getbible.net`;
- connect/read/overall timeout settings.

After `CIRCUIT_RECOVERY_SECONDS`, one request is permitted as a recovery probe. Do not repeatedly restart merely to reset the circuit; fix the upstream or network condition.

## Scripture commands time out or report busy

`RobotBusy` means the bounded worker capacity could not be acquired within `LOOKUP_QUEUE_TIMEOUT`. A timed-out worker keeps its capacity until the underlying synchronous request really exits, which is intentional protection against an unbounded executor queue.

Check metrics and upstream latency. Do not increase concurrency or queue timeouts until memory, API capacity, and service limits have been load-tested.

## Ordinary references trigger translation errors

An ordinary reference such as `John 3:16` should be parsed locally with the default translation and should not probe a translation named `3:16`. Confirm the deployed commit includes the strict `ScriptureService.resolve_query` path and run:

```bash
venv/bin/python -m unittest tests.test_service -v
```

Explicit translation syntax is:

```text
/bible John 3:16 kjv
/bible Gen 1:1 aov
```

## Incorrect or unsafe links

Every Scripture/search link shown in Telegram must begin with `https://getbible.life`. Data comes from `https://api.getbible.net`.

Check only the non-secret settings:

```bash
sudo grep -E '^(GETBIBLE_API_BASE_URL|GETBIBLE_WEB_BASE_URL)=' \
  /etc/getbible-robot.env
```

Expected values:

```text
GETBIBLE_API_BASE_URL="https://api.getbible.net"
GETBIBLE_WEB_BASE_URL="https://getbible.life"
```

Run renderer tests after any URL or formatting change:

```bash
venv/bin/python -m unittest tests.test_renderer -v
```

## Telegram rejects a message

The renderer escapes HTML, percent-encodes URL segments, measures UTF-16 code units, splits at safe block boundaries, and caps the number of output messages. A rejection can indicate malformed repository data, an untested Telegram HTML rule, or deployment of mismatched code/dependencies.

Record the incident correlation ID and deployed commit. Do not log or paste the full private Scripture request unless the user has consented and the channel is approved.

## Command deletion fails

Deletion is disabled by default. When `DELETE_COMMAND_MESSAGES=true`, group deletion may require administrator permissions. `BadRequest` and `Forbidden` are non-fatal; Scripture delivery should still succeed.

Either grant the narrowly required Telegram permission or set:

```dotenv
DELETE_COMMAND_MESSAGES="false"
```

## Rate limiting appears too strict

Every command consumes both a user and chat token, including `/help`, `/start`, `/search`, and unknown commands. This prevents cheap-command flooding from bypassing the lookup limiter.

Review `USER_RATE_*`, `CHAT_RATE_*`, and `getbible_robot_rate_limit_*` metrics. Increase values cautiously and keep `RATE_LIMIT_CACHE_SIZE` bounded.

Only the first rejection for a user/chat cooldown sends a Telegram warning. Later rejected requests are intentionally silent until `RATE_LIMIT_NOTICE_COOLDOWN` has elapsed without another warning attempt.

## An interactive panel expired or does not respond

Run `/bible` or `/search` again. Panels are intentionally process-local and expire after `INTERACTION_TTL_SECONDS`; a service restart invalidates every old callback token.

In a group, reply directly to the bot's selective search prompt from the same user who opened the panel. Unrelated messages, another user's reply, and replies to an older prompt are ignored.

Review `getbible_robot_interaction_*` metrics for active, expired, and evicted sessions. Do not remove ownership checks or make sessions unbounded to avoid an expiry.

## Health port is unavailable

Confirm the configured loopback port and identify the listener:

```bash
sudo ss -ltnp | grep ':8081 '
```

Choose another unused loopback port or set `HEALTH_PORT=0` to disable the endpoint. `HEALTH_HOST` cannot bind publicly by design.

## Hashed dependency installation fails

Use the lock belonging to the same robot commit. Do not remove `--require-hashes`.

```bash
git status --short
git rev-parse HEAD
head -n 8 requirements.txt
python3 --version
venv/bin/python -m pip --version
```

If the direct inputs changed, regenerate both locks with [the dependency procedure](DEPENDENCIES.md). Validate the runtime lock separately on Python 3.10, 3.11, and 3.12.

## CI fails at Ruff, mypy, Bandit, audit, or secret scanning

Open the exact failed step and artifact. Fix the source or dependency; do not:

- disable a rule without a documented false-positive analysis;
- remove a supported Python version to hide a lock problem;
- ignore a vulnerability without impact and compensating-control documentation;
- add a secret baseline that contains a real token;
- skip a job solely to obtain a green gate.

## Memory or restart pressure

Check:

```bash
systemctl show getbible-robot.service \
  -p MemoryCurrent -p MemoryPeak -p MemoryMax -p NRestarts
```

Likely causes include unusually large configured limits, too many worker threads, repeated upstream stalls, or an unexpectedly large translation/cache. Preserve the bounded defaults, inspect cache telemetry, and reproduce under load before changing `MemoryMax`.

## Safe support request

Include:

- robot commit SHA;
- Python version;
- operating-system and systemd version;
- redacted configuration names that differ from defaults, without values when sensitive;
- failing test/check name;
- correlation ID and sanitized log event;
- exact expected and observed behavior;
- whether the problem occurs with a dedicated test bot.

Report vulnerabilities privately according to [`SECURITY.md`](../SECURITY.md).
