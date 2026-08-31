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

Run routine upgrades through either the installed manager or the reviewed
target checkout:

```bash
sudo getbible-robot update production --source /path/to/robot-target
```

When the target contains newer manager logic, the previously installed manager
hands the transaction to that reviewed target checkout's `setup.sh` before
changing the deployment. The target logic migrates configuration and
regenerates all setup-managed Caddy routes. Repeating the command at the
already deployed commit is supported: it
refreshes the manager, service limits, generated routes, and postflight checks
without replacing `app` or `app.previous`.

## Contributor enrolment and moderation

Contributor enrolment is intentionally private and operator-directed. Give a
candidate the hidden `/contributor` command; it does not appear in the bot
command menu and refuses applications outside a private chat. Repeating the
command is idempotent: pending applicants see that review is in progress, and
approved users see that they are enrolled and that approved changes can become
part of the shared core catalogue.

Open the native review workflow with:

```bash
sudo getbible-robot contributions production
```

For a container instance, the same live stages and a privacy-safe export are
available without granting Git access to the runtime image:

```bash
docker exec -it getbible-robot-production \
  /app/setup.sh contributions production
docker exec getbible-robot-production \
  /app/setup.sh contributions production status
docker exec getbible-robot-production \
  /app/setup.sh contributions production export
```

The export command prints its exact mode-`0600` path below
`/data/<instance>/state/contribution-exports/`. Automated Git publication from
a container export is not supported in this release; retain it only for a
separately reviewed manual repository import. Run a native deployment when the
guarded one-command branch workflow is required.

Use its stages in order:

1. review pending applications and optionally revoke an enrolled contributor;
2. map each contributor-local topic to an existing canonical topic, merge it
   with another pending proposal, create/correct an English canonical topic, or
   reject/defer it;
3. review verse additions and removals only after topic mappings are resolved;
4. publish approved changes to the running instance;
5. optionally export the privacy-safe live revision and push a repository
   branch.

Application decisions queue a private Telegram notification. Approval also
causes the Mini App to show a one-time disclosure before anything is shared,
and `/contributor` attaches a persistent **Push contribution** reply-keyboard
button to the approved contributor's private chat. The collapsible,
approved-contributor-only **Manage Contribution** panel sits immediately below
Global topics. Its **Push** action serializes the current topic/assignment
snapshot and hands it to Telegram as bounded chunks from that keyboard launch;
the bot stages each delivered service message durably, deletes it from the
chat, keeps one edited progress message for a multi-part transfer, commits the
completed bundle atomically, and confirms in the chat. Its **Pull** action
refreshes contributor status, confirms a pending push receipt, and revalidates
the current global catalogue; status otherwise refreshes only at session
bootstrap, never through background polling. Sessions without contribution
authority do not receive that panel; application decisions continue to arrive
through the private bot notification.
Personal bookmark writes remain local-first: a network or moderation-server
failure cannot undo them, and the durable outbox resends the identical
transfer on a later push.

Topic proposals must use an English source name. The topic stage is where an
operator resolves spelling, aliases, colors, and overlapping proposals into
one stable canonical ID. Contributor deletion/recolor/rename events are review
requests, never authority to mutate the core directly. Verse review shows the
operation, canonical topic, contributor, reference, and authoritative text
from the configured GetBible Query API translation. If that text cannot be
retrieved or validated, the CLI defers the affected work instead of displaying
client text or silently approving it.

A canonical topic becomes permanent when it first appears in a live catalogue
revision. From that point its ID, English definition, and existence are locked:
a repository branch may already contain it, and the version-1 contribution
bundle has no topic-deletion tombstone that could prevent a later branch merge
from resurrecting it. A `topic_delete` can therefore cancel only a contributed
topic that has never been live. Operators may still review individual verse
removals from a permanent topic, but the CLI defers the removal that would
leave it with no effective verse association. True published-topic deletion
requires a future, versioned bundle schema with explicit provenance-aware
tombstones.

One live publication revision accepts at most 10,000 approved events. This is
an intentional dependency-safety ceiling: publish reviewed work in smaller
cycles before the queue reaches that size, because the CLI does not split an
approved topic-and-verse dependency chain automatically.

**Publish to this live instance** creates a cumulative, checksummed catalogue
revision in the same private SQLite store, so the instance serves it
immediately. Mini Apps revalidate on their next open, reconnect, or explicit
global-topic pull; an already-open idle reader is not interrupted. The bundled
catalogue remains the offline/error fallback. This step is independent of Git
and survives restarts and application upgrades.

Repository publication is deliberately separate. Configure
`CONTRIBUTION_GIT_CHECKOUT` and `CONTRIBUTION_GIT_USER` through
`getbible-robot config`. The user must be a dedicated non-root account, must own
a clean checkout whose `origin` is `getbible/robot`, and must already have a
non-interactive Git credential with branch-push permission. The publisher
fetches `origin/master`, creates a unique `contributions/...` branch, imports
the deterministic JSON export, regenerates English topic constants and global
catalogue assets, runs the Mini App checks, commits, and pushes. Install
Node.js 22 or newer and npm for that publisher account before using this step.

Set the publisher's commit identity in the checkout-local Git config; global
configuration, including root's identity, is not used:

```bash
sudo -u getbible-publisher git -C /srv/getbible-robot-publisher/robot config --local user.name "GetBible Contribution Publisher"
sudo -u getbible-publisher git -C /srv/getbible-robot-publisher/robot config --local user.email "publisher@getbible.net"
```

It does not
open or merge a pull request. A failed export or Git operation cannot roll back
the live revision; the restricted export is retained for diagnosis and retry.

Telegram IDs, usernames, profile names, application decisions, and reviewer
notes remain in the private per-instance database and never enter the live
catalogue response, JSON export, Git diff, commit message, or branch name.
Protect and retain that database as personal moderation data. Audit-log
identity mode does not weaken this database boundary.

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
- local Mini App shell plus public TLS and response-content checks;
- local and public `bookmarks/catalog`, `contributions/status`, and
  `contributions/receipt` GET routes reaching Robot and returning its expected
  unauthenticated JSON `401` response;
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
- contribution-store failures, pending notification retries, sustained event
  growth near `CONTRIBUTION_EVENT_LIMIT`, or approved work awaiting live
  publication;
- interactive session evictions or saturation;
- `instance_memory_pressure`, memory approaching `MemoryMax`, or a child RSS
  guard restart;
- task or file-descriptor pressure;
- log write/rotation failures;
- a returned link not hosted on `getbible.life`.

Runtime metrics contain aggregates only. Do not expose the loopback listener publicly without an authenticated, access-controlled proxy.

`/metrics` publishes `getbible_robot_search_engine_version`. Librarian moves
that number whenever matching semantics change, independent of any translation
SHA, so it is the value that separates an intended upgrade from a regression
when result counts move under a stable corpus. Record it alongside result-volume
dashboards. A first search of a translation that has no index yet may report
`lookup_timed_out` while the build completes in its worker; the searches after
it are served from the built index. See [Search](SEARCH.md).

The structured events used for capacity diagnosis are
`capacity_queue_rejected`, `lookup_timed_out`,
`upstream_circuit_rejected`, `instance_memory_pressure`,
`instance_memory_limit_exceeded`, `inbound_rate_limited`, and
`instance_restart_circuit_open`. Mini App access events include route,
status, and duration. Depending on `AUDIT_IDENTITY_MODE`, identity fields are
absent, pseudonymous, or raw Telegram IDs/resolved client IPs.

Telegram command updates never contain the user's IP. For Mini App traffic,
managed Caddy trusts loopback and external proxy mode delegates the backend
network boundary to the operator. `MINI_APP_TRUSTED_PROXY_CIDRS` is an optional
advanced restriction. Raw identity mode is appropriate only where access and
retention policy allow personal-data logs.

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
- the private per-instance contribution database and any unpushed reviewed
  exports, encrypted with access and retention controls appropriate for raw
  Telegram profile data;
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
