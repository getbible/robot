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
part of the shared getBible catalogue.

The shared catalogue lives outside this repository. Its source of truth is
[getbible/v1_bookmark_builder](https://github.com/getbible/v1_bookmark_builder),
and its published form is the public Bookmarks API at
`https://bookmarks.getbible.net/v1`. The robot ships no copy of it, serves no
catalogue route, and never writes the generated API directly. Final maintainer
acceptance commits the source files on the builder's default branch through
the GitHub API; its existing push workflow publishes the generated API.

Open the native review workflow with:

```bash
sudo getbible-robot contributions production
```

For a container instance the same review, acceptance and direct API publisher
are available without Git or a checkout. Status, export and commit can run
non-interactively:

```bash
docker exec -it getbible-robot-production /app/setup.sh contributions production
docker exec getbible-robot-production /app/setup.sh contributions production status
docker exec getbible-robot-production /app/setup.sh contributions production commit
```

The privacy-safe `export` remains optional and writes a mode-`0600` bundle below the instance's
`state/contribution-exports/`; no export/import step is required to publish.

The native menu has five stages. Use them in order:

1. **Status** — pending applications, unresolved topics, approved work, the
   ledger revision, the last direct commit (and any legacy pull request), and the catalogue version and
   checksum last observed from the Bookmarks API with the time of that check.
2. **Applications** — review pending applications, optionally revoke an
   enrolled contributor, and optionally reinstate a previously revoked or
   rejected one. A removed contributor's record stays in the store keyed by
   their Telegram ID, and a fresh `/contributor` request never resets that
   state on its own, so this stage is the one place access is restored (the
   enrolment notice is queued again and the disclosure must be
   re-acknowledged before new submissions).
3. **Topics** — map each contributor-local topic to an existing catalogue
   topic, merge it with another pending proposal, create/correct an English
   canonical topic, or reject/defer it.
4. **Verses** — review verse additions and removals, only after topic
   mappings are resolved.
5. **Accept and commit** — save the approved changes in the submission ledger,
   then update the builder sources with one atomic commit using its API.

The menu also offers **Commit previously accepted contributions / retry**,
**Add or replace GitHub / OpenAI credentials**, and **Inspect submitted changes
and accepted revisions**. These do not repeat moderation. The `inspect` command
shows accepted contents and a paginated history, including work no longer in the
pending review queue; `inspect --revision NUMBER` opens a historical revision.

Before stages 3, 4, and 5 the manager downloads the current catalogue from
the Bookmarks API (`fetch-catalog`, verified against `checksums.json`) into
the instance's `contribution-exports/bookmarks-catalog.json` and passes it to
the review commands as `--catalog-file`. Those stages refuse to run when
`bookmarks.getbible.net` cannot be reached: review is always against the
catalogue readers see, never against a file in this repository.

Application decisions queue a private Telegram notification. Approval also
causes the Mini App to show a one-time disclosure before anything is sent;
the acknowledgement rides the first event batch of the next Sync.
The collapsible, approved-contributor-only **Manage Contribution** panel sits
immediately below Global topics. Its explicit Sync action converts the
current personal topics/assignments into bounded idempotent events, appends
the journalled explicit global add/remove intents, and drips them to the
session-authenticated events endpoint in sequential batches of at most 50;
each batch body also carries the contributor's short-lived
`contribution_token`, which only approved contributors receive inside JSON
payloads. A `403` means the user is not an approved contributor or the token
went stale; the app recovers automatically with one ordinary status refresh.
Every response reports receipt counts, the contributor's detailed status, and
the accepted-ledger revision. Sessions without contribution authority do not
receive that panel; application decisions continue to arrive through the
private bot notification.
Personal bookmark writes remain local-first: a network or moderation-server
failure cannot undo them, and the same idempotent events are resent on a
later synchronization.

Topic proposals must use an English source name. The topic stage is where an
operator resolves spelling, aliases, colors, and overlapping proposals into
one stable canonical ID. Contributor deletion/recolor/rename events are review
requests, never authority to mutate the catalogue directly. Verse review shows
the operation, canonical topic, contributor, reference, and authoritative text
from the configured GetBible Query API translation. If that text cannot be
retrieved or validated, the CLI defers the affected work instead of displaying
client text or silently approving it.

A canonical topic becomes permanent once it is part of the public catalogue.
From that point its ID, English definition, and existence are locked: the
version-1 contribution bundle has no topic-deletion tombstone, so a
`topic_delete` can cancel only a contributed topic that has never been
published. Operators may still review individual verse removals from a
permanent topic, but the CLI defers the removal that would leave it with no
effective verse association. Deleting a published topic is a change in the
builder repository, outside this pipeline.

One acceptance takes at most 10,000 approved events. This is an intentional
dependency-safety ceiling: publish reviewed work in smaller cycles before the
queue reaches that size, because the CLI does not split an approved
topic-and-verse dependency chain automatically.

### Publishing

Acceptance is durable before publication begins. The publisher commits only
accepted topics, additions and removals to `getbible/v1_bookmark_builder`'s
current default branch (`main` or `master`) through HTTPS. No contribution
branch, pull request, local Git clone, SSH credential or dedicated publisher
account is needed. Topic definitions, sorted links and all missing translations
are validated together and committed together. Only `data/topics.json`,
`data/links/<topic>.json` and `data/locales/<locale>.json` can change.

Configure a repository-scoped `CONTRIBUTION_GITHUB_TOKEN` with Contents
read/write and permission under its branch rules. Configure
`CONTRIBUTION_OPENAI_API_KEY` for missing topic translations; no key keeps that
work queued. `CONTRIBUTION_TRANSLATION_MODEL` selects the structured-output model.
Existing topic translations are preserved, and previously accepted English-only
topics are checked for missing labels after upgrade.

For the example native instance:

```bash
sudo getbible-robot contributions production tokens
sudo getbible-robot contributions production inspect
sudo getbible-robot commit production
sudo getbible-robot contributions production status
```

Both credentials are optional during upgrade; missing values are prompted
without echo, and Enter skips them. Add them later with `tokens` or the existing
configuration editor. Explicit `tokens` entry can replace existing keys: Enter
keeps the value and `-` clears it. A missing GitHub token retains accepted work
for the next `commit`; a missing or failing OpenAI key leaves work requiring
translations pending. Publication failures return a nonzero exit status.
The update itself never starts a publication.

Receipts and translation results live in an adjacent private
`contributions.sqlite3.publication.sqlite3` journal. Keep this file with the
contribution database across backups, restores and upgrades. A failed HTTP
request cannot roll back acceptance. Retries preserve colleague edits, reuse
successful translations and reconcile a remotely committed but unacknowledged
commit before creating another. **Status** distinguishes direct publication
from historical Git publication records.

See [Contribution publication](CONTRIBUTION_PUBLICATION.md) for the source
contract, model prompt, native/container commands and recovery semantics.

### Going live

The direct source commit starts the builder's own push workflow, which
validates the sources, renders the API, and publishes it. The
robot learns of it on its own: when a contribution store is configured, a
background task reads `index.json` from `GETBIBLE_BOOKMARKS_BASE_URL` every
`BOOKMARK_CATALOG_CHECK_INTERVAL_SECONDS`, the first time shortly after
start. When the catalogue version or checksum moved, it downloads and verifies
the catalogue, records it, and decides for every applied event whether it is
now part of the public catalogue: an addition whose verse the topic now links,
a removal whose verse it no longer links, a topic that now exists, a deletion
whose topic is absent. Newly live events are stamped, and each affected
contributor receives one private "contributions live" notice naming the
catalogue version, through the same notification outbox as application
decisions. The check is idempotent; an unchanged checksum changes nothing,
and a failed check is logged as a warning and retried at the next interval.

Only that observation marks a contribution as published. The Mini App's
detailed status reports a topic as published once its canonical topic has
been seen in the public catalogue and its latest applied transition is not a
deletion, and the panel's **P** marker becomes **G** on the next status
refresh. Readers everywhere pick the change up from the API on their next
daily revalidation or explicit **Add all**; nothing on this instance is
served to them.

Telegram IDs, usernames, profile names, application decisions, and reviewer
notes remain in the private per-instance database and never enter the live catalogue,
bundle export, Git diff, commit message, branch name, or pull request.
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
- local and public contribution status/event routes reaching Robot and
  returning its expected unauthenticated JSON response;
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
  growth near `CONTRIBUTION_EVENT_LIMIT`, accepted work whose source commit
  has not produced a successful build, or a Bookmarks API check that keeps failing (the catalogue
  watcher logs a warning and retries at the next interval);
- interactive session evictions or saturation;
- `instance_memory_pressure`, memory approaching `MemoryMax`, or a child RSS
  guard restart;
- task or file-descriptor pressure;
- log write/rotation failures;
- a returned link not hosted on `getbible.life`.

Runtime metrics contain aggregates only. Do not expose the loopback listener publicly without an authenticated, access-controlled proxy.

Search is served by the public Search API, and its response carries an
`engine_version` that moves whenever matching semantics change, independent
of any translation `sha`; it is the value that separates an intended service
change from a regression when result counts move under a stable corpus.
Record it from the API response alongside result-volume dashboards; the robot
publishes no engine version of its own. `/metrics` publishes
`getbible_robot_search_circuit_open` for the robot's Telegram-native search
circuit; Mini App searches go from the browser and are not visible here. See
[Search](SEARCH.md).

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
   traffic, legitimate capacity, Telegram, host networking, the GetBible
   Main, Query, or Search API, rendering, configuration, or deployment.
5. Use `rollback` if the immediately previous application is known-good.
6. Run `doctor`, readiness, and the private smoke test before returning to service.
7. Add a deterministic regression test before deploying a code fix.
8. Report security defects according to [`SECURITY.md`](../SECURITY.md).

## Backups

The code and exact dependency locks are recoverable from Git. The robot keeps no Scripture corpus or cache. Retain securely:

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
interaction state under representative load. Catalogue and Query API
responses and Search API responses have separate byte budgets. The
Telegram-native search uses an independent pool and circuit so a slow Search
API cannot occupy all direct-reference workers; Mini App searches never pass
through the robot. The robot holds no corpus or index. A timed-out
synchronous operation intentionally retains its own permit until the
underlying thread exits.

Interactive state is process-local, bounded, and ephemeral. Restarting one instance expires only that instance's unfinished panels.
