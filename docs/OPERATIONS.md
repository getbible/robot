# Operations

The installed `getbible-robot` manager is the supported interface for routine production operation. Every command accepts an instance name. If it is omitted in an interactive terminal, the manager lists installed instances for selection.

## Inventory

```bash
sudo getbible-robot list
sudo getbible-robot status production
sudo getbible-robot runtime production
```

`list` reports instance, isolated service account, service state, health port, and abbreviated deployed commit. `status` adds the exact commit, Python version, creation date, JSON log path, enablement, and readiness. `runtime` adds `pip check`, systemd memory/task/restart counters, and aggregate application metrics.

No command prints the secret environment file or Telegram token.

## Start, stop, and restart

```bash
sudo getbible-robot start production
sudo getbible-robot stop production
sudo getbible-robot restart production
```

Start and restart wait for the configured loopback readiness endpoint. If `HEALTH_PORT=0`, only the service transition is checked.

Never run a second process with the same Telegram token.

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
- root-only environment permissions;
- per-instance log ownership;
- complete configuration validation;
- installed dependency consistency;
- deployed Git commit against metadata;
- instantiated unit verification;
- systemd status;
- health and readiness when running.

Use `runtime` for operational counters and `doctor` for an evidence-backed pass/fail deployment check.

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

See [Configuration](CONFIGURATION.md) before changing any bound. Never copy one instance's token or environment file over another.

## Monitoring

Alert on:

- an inactive service or repeated restart growth;
- readiness unavailable longer than `CIRCUIT_RECOVERY_SECONDS`;
- an open upstream circuit;
- lookup timeouts, repository failures, queue rejections, or unexpected failures;
- sustained rate-limit rejection;
- interactive session evictions or saturation;
- memory approaching `MemoryMax`;
- task or file-descriptor pressure;
- log write/rotation failures;
- a returned link not hosted on `getbible.life`.

Runtime metrics contain aggregates only. Do not expose the loopback listener publicly without an authenticated, access-controlled proxy.

## Incident response

1. Stop only the affected instance: `sudo getbible-robot stop <instance>`.
2. Revoke the token immediately through `@BotFather` if disclosure is possible.
3. Record `status`, `runtime`, the deployed commit, lock checksum, unit checksum, and a bounded log window.
4. Determine whether the failure is Telegram, host networking, GetBible API, Librarian, rendering, configuration, or deployment.
5. Use `rollback` if the immediately previous application is known-good.
6. Run `doctor`, readiness, and the private smoke test before returning to service.
7. Add a deterministic regression test before deploying a code fix.
8. Report security defects according to [`SECURITY.md`](../SECURITY.md).

## Backups

The code and exact dependency locks are recoverable from Git. Cache data is recoverable from the GetBible API. Retain securely:

- the exact deployed and prior commits;
- an encrypted copy of `/etc/getbible-robot/<instance>.env` when policy requires it;
- deployment and smoke-test records;
- content logs only for the minimum approved retention period.

Do not use a copied virtual environment as a substitute for the matching commit and lock.

## Capacity

Increase bounds only after measuring memory, worker occupancy, Telegram output count, API latency, circuit behavior, and interaction state under representative load. Hard complexity limits also apply to administrators. A timed-out synchronous lookup intentionally retains its worker permit until the underlying thread exits.

Interactive state is process-local, bounded, and ephemeral. Restarting one instance expires only that instance's unfinished panels.
