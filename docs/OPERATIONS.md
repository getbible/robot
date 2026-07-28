# Operations

## Docker instances

The container deployment uses its own non-root supervisor instead of systemd
and Caddy. Operate it without opening a shell:

```bash
docker exec getbible-robot getbible-robot-container list
docker exec getbible-robot getbible-robot-container status production
docker exec getbible-robot getbible-robot-container doctor production
docker exec getbible-robot getbible-robot-container restart production
docker exec getbible-robot getbible-robot-container reload
docker logs --since 30m getbible-robot
```

The status response includes PID, assigned ports, current RSS, the per-child
memory guard, memory-warning threshold and pressure state, restart count, last
exit, and recent liveness.

The versioned Compose file plus its private environment file are the Docker
deployment source of truth. Initialize, edit, validate, and apply them from the
host:

```bash
./setup.sh docker-init
./setup.sh docker-config
./setup.sh docker-validate
./setup.sh docker-restart
./setup.sh docker-update
```

`docker-config` creates a backup, validates the edited environment, restores it
on failure, and recreates the container to apply environment and Compose
resource changes. Direct edits are supported; follow them with
`docker-validate` and `docker-restart`. A plain `docker restart` does not reload
environment variables. Configuration and secrets are not mutated inside the
read-only container. In-container `reload` applies changed mounted multi-bot
instance files but cannot rewrite host Compose configuration. See [Docker
deployment](DOCKER.md).

`docker-update` pulls the exact `ROBOT_IMAGE` selected in `.env` before
recreation. Routine `docker-restart` retains the already installed image.
Production should use an exact semantic-version or immutable commit tag.

The installed `getbible-robot` manager is the supported interface for routine production operation. Every command accepts an instance name. If it is omitted in an interactive terminal, the manager lists installed instances for selection.

## Inventory

```bash
sudo getbible-robot list
sudo getbible-robot status production
sudo getbible-robot runtime production
```

`list` reports instance, isolated service account, service state, health port,
and abbreviated deployed commit. `status` adds the exact commit, Python
version, creation date, JSON log path, Telegram delivery, Mini App public/local
addresses, enablement, and readiness. `runtime` adds `pip check`, systemd
memory/task/restart counters, and aggregate application metrics.

No command prints the secret environment file or Telegram token.

## Start, stop, and restart

```bash
sudo getbible-robot start production
sudo getbible-robot stop production
sudo getbible-robot restart production
```

`start` enables the selected unit so it survives reboot, starts it, and waits
for the configured loopback readiness endpoint. `restart` preserves current
enablement. When the Mini App is enabled, both commands also verify its local
shell and public certificate/route/content before reporting success.

Never run a second process with the same Telegram token.

## Polling, webhook, and bot content

Show the selected mode:

```bash
sudo getbible-robot status production
sudo getbible-robot doctor production
```

Switch modes through the transactional manager:

```bash
sudo getbible-robot delivery production
```

Webhook mode is an outgoing Telegram HTTPS webhook, not a WebSocket. Configure
the public TLS reverse proxy first; the robot remains bound to loopback. Polling
removes the registered webhook during startup. A detected duplicate poller exits
with status 75 and is not restarted by systemd.

Edit the multi-line welcome or help content:

```bash
sudo EDITOR=nano getbible-robot content production welcome
sudo EDITOR=nano getbible-robot content production help
```

Use `config` for `BOT_NAME`, `BOT_DESCRIPTION`, and
`BOT_SHORT_DESCRIPTION`. Restarting synchronizes those values and the command
menu through Telegram's Bot API. See [Telegram delivery](WEBHOOKS.md).

## Telegram Mini App

Configure or disable the same-instance Mini App transactionally:

```bash
sudo getbible-robot miniapp production
sudo getbible-robot status production
sudo getbible-robot doctor production
```

The Mini App listener remains on `127.0.0.1` and uses a port separate from the
health and webhook listeners. Its public HTTPS route is required even when
Telegram updates use polling. The manager installs/configures the host Caddy
service transactionally and removes the public route when the Mini App is
disabled. See [Mini App deployment](MINI_APP.md) for DNS, Caddy, BotFather,
authentication, and verification requirements.

## Logs

Show a bounded recent window:

```bash
sudo getbible-robot logs production
sudo getbible-robot logs production 500
```

Follow new events:

```bash
sudo getbible-robot follow production
```

The canonical file is:

```text
/var/log/getbible-robot/<instance>.jsonl
```

The process also writes to journald:

```bash
sudo journalctl -u getbible-robot@production.service -n 200 --no-pager
```

Each JSON object contains an instance name, UTC timestamp, severity, logger, message, and optional controlled audit fields. `metadata` audit mode never stores Telegram query text or final references. `content` mode adds search terms and final references only; tokens, user IDs, chat IDs, verse bodies, and repository response bodies remain prohibited.

Logs rotate daily or at 10 MiB, retain 14 compressed rotations, and use `copytruncate` so the running file handler remains valid.

## Diagnostics

```bash
sudo getbible-robot doctor production
```

The non-destructive diagnostic checks:

- service account existence;
- application and virtual environment;
- real service-account traversal, Python execution, and application import;
- root-only environment permissions;
- per-instance log ownership;
- complete configuration validation;
- readable, manager-owned welcome/help content;
- installed dependency consistency;
- deployed Git commit against metadata;
- instantiated unit verification;
- systemd status;
- Telegram webhook registration matching the configured delivery mode;
- enabled Mini App listener presence on its exact IPv4 loopback address;
- generated Caddy route equality, full Caddyfile validation, and Caddy service
  enablement/activity;
- local Mini App shell plus public TLS, routing, and response-content checks;
- health and readiness when running.

Use `runtime` for operational counters and `doctor` for an evidence-backed pass/fail deployment check.

## Repair application access

If `doctor` reports that the service account cannot enter or read the
application directory, run the repair command from the exact reviewed checkout:

```bash
sudo ./setup.sh repair production
```

The command stops only the selected instance, restores `root:gb-<instance>`
ownership and group-only read/traverse access on the active and retained
rollback trees, runs the import preflight as the actual locked account, clears
the systemd failure limit, and restarts the service when it is enabled. It does
not expose or modify the Telegram token.

## Configuration changes

```bash
sudo getbible-robot config production
```

The manager:

1. makes a restricted temporary backup;
2. opens the configured editor;
3. restores root ownership and mode `0600`;
4. validates the complete file with the deployed code;
5. automatically restores the prior file if validation fails;
6. offers to restart the selected instance.

Mini App enablement, URL, listen address, and port are manager-owned; change
them only through `getbible-robot miniapp`. See
[Configuration](CONFIGURATION.md) before changing any other bound. Never copy
one instance's token or environment file over another.

## Monitoring

Alert on:

- an inactive service or repeated restart growth;
- readiness unavailable longer than `CIRCUIT_RECOVERY_SECONDS`;
- an open upstream circuit;
- lookup timeouts, repository failures, queue rejections, or unexpected failures;
- sustained rate-limit rejection, abuse blocks, or one identity dominating
  request volume;
- a duplicate-poller exit or webhook pending/error growth;
- Mini App listener loss, authorization failures, expired-launch growth, or
  unexpected public API access without Telegram authorization;
- interactive session evictions or saturation;
- `instance_memory_pressure`, memory approaching `MemoryMax`, or a child RSS
  guard restart;
- task or file-descriptor pressure;
- log write/rotation failures;
- a returned link not hosted on `getbible.life`.

Runtime metrics contain aggregates only. Do not expose the loopback listener publicly without an authenticated, access-controlled proxy.

The structured events used for capacity diagnosis are
`capacity_queue_rejected`, `lookup_timed_out`,
`upstream_circuit_rejected`, `instance_memory_pressure`,
`instance_memory_limit_exceeded`, `inbound_rate_limited`, and
`instance_restart_circuit_open`. Mini App access events include route,
status, and duration. Depending on `AUDIT_IDENTITY_MODE`, identity fields are
absent, pseudonymous, or raw Telegram IDs/resolved client IPs.

Telegram command updates never contain the user's IP. For Mini App traffic,
configure `MINI_APP_TRUSTED_PROXY_CIDRS` to the exact reverse-proxy peers;
otherwise forwarded addresses are intentionally ignored. Raw identity mode is
appropriate only where access and retention policy allow personal-data logs.

## Incident response

1. Stop only the affected instance: `sudo getbible-robot stop <instance>`.
2. Revoke the token immediately through `@BotFather` if disclosure is possible.
3. Record `status`, `runtime`, the deployed commit, lock checksum, unit checksum, and a bounded log window.
4. Correlate pressure events, request duration/status, aggregate metrics, and
   the configured identity fields. Determine whether the failure is abusive
   traffic, legitimate capacity, Telegram, host networking, GetBible API,
   Librarian, rendering, configuration, or deployment.
5. Use `rollback` if the immediately previous application is known-good.
6. Run `doctor`, readiness, and the private smoke test before returning to service.
7. Add a deterministic regression test before deploying a code fix.
8. Report security defects according to [`SECURITY.md`](../SECURITY.md).

## Backups

The code and exact dependency locks are recoverable from Git. Cache data is recoverable from the GetBible API. Retain securely:

- the exact deployed and prior commits;
- an encrypted copy of `/etc/getbible-robot/<instance>.env` when policy requires it;
- durable preference data when required by policy; Mini App launch/session
  state is intentionally short-lived and should not be restored;
- deployment and smoke-test records;
- content logs only for the minimum approved retention period.

Do not use a copied virtual environment as a substitute for the matching commit and lock.

## Capacity

Increase bounds only after measuring memory, worker occupancy, Telegram output
count, API latency, request identity distribution, circuit behavior, and
interaction state under representative load. Full corpus downloads and
returned search results have separate byte budgets. Search uses an independent
pool so slow index work cannot occupy all direct-reference workers. A timed-out
synchronous operation intentionally retains its own permit until the
underlying thread exits.

Interactive state is process-local, bounded, and ephemeral. Restarting one instance expires only that instance's unfinished panels.
