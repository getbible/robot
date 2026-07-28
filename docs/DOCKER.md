# Docker deployment

The recommended Docker layout runs one Telegram bot in one container. The
image contains only GetBible Robot and its non-root supervisor. It does not
install Caddy, systemd, a firewall, certificates, or listeners on ports 80 and
443. The container serves the Mini App on one configurable application port;
TLS, DNS, and routing remain outside the container.

The same image also supports an explicit multi-bot mode for compact
deployments, but separate containers are the operational default because each
bot then has an independent memory limit, health state, restart policy, data
volume, update cycle, and application port.

## Recommended one-bot quick start

From a reviewed checkout:

```bash
cp docker/examples/compose.env.example .env
chmod 600 .env
${EDITOR:-vi} .env
./setup.sh docker-deploy
./setup.sh docker-doctor
```

At minimum, replace these values:

```dotenv
TELEGRAM_API_TOKEN=123456789:replace-with-the-token-from-BotFather
MINI_APP_PUBLIC_URL=https://bot.example.com/getbible/production
```

`compose.yaml` reads the project `.env` automatically. The same values may
instead be exported in the shell or supplied through:

```bash
./setup.sh docker-deploy --env-file /absolute/path/robot.env
```

No interactive installation runs during container startup. The supervisor
derives per-instance state paths, validates the complete application
configuration, starts the bot, waits for health, and restarts it safely when a
runtime failure occurs. Missing or invalid application values are written to
stdout/stderr as structured `ERROR` events:

```bash
./setup.sh docker-logs getbible-robot-production 200
```

For example, a missing token produces an
`instance_configuration_rejected` event containing
`TELEGRAM_API_TOKEN is required`. The supervisor remains available for status
and diagnostics instead of repeatedly crash-looping the application.

## Token handling

The default Compose model accepts `TELEGRAM_API_TOKEN` from `.env` because it
is the smallest fully automatic deployment path. Keep `.env` mode `0600`; it
is ignored by Git.

For production, the optional Compose-secret overlay removes the token from the
container environment and mounts it at `/run/secrets/telegram_bot_token`:

```bash
./setup.sh docker-deploy --secure
```

The overlay sources the secret from `TELEGRAM_API_TOKEN` in the operator
environment or supplied Compose environment file and mounts it mode `0400` as
UID/GID 10001. The application receives only
`TELEGRAM_API_TOKEN_FILE=/run/secrets/telegram_bot_token`.

The explicit multi-bot mode uses file-backed secrets because each instance
needs a different token. Those source files must be readable by the host UID
mapped to container UID 10001.

## Ports and external routing

The default container publishes only the Mini App port:

```dotenv
MINI_APP_PORT=9201
MINI_APP_HOST_PORT=9201
MINI_APP_BIND_ADDRESS=127.0.0.1
```

This produces `127.0.0.1:9201 -> container:9201`. Point an existing HTTPS
reverse proxy at that host port. If the reverse proxy is another container,
use a Compose override to attach both services to a private Docker network and
route directly to `robot:9201`; the host port can then be removed.

The health listener remains inside the container on port 8081 and is used by
the image `HEALTHCHECK`. It does not need a public or host mapping. Polling is
the default Telegram delivery mode, so no webhook port is required.

`MINI_APP_PUBLIC_URL` must remain the externally reachable HTTPS URL used by
Telegram. Its path must be forwarded unchanged to the application port.

## Operate the deployed container

The host setup manager discovers containers by the
`io.getbible.robot.container=true` label:

```bash
./setup.sh docker-list
./setup.sh docker-status
./setup.sh docker-doctor
./setup.sh docker-logs
./setup.sh docker-follow
./setup.sh docker-manage
./setup.sh docker-shell
```

If multiple Robot containers exist, the interactive commands show a numbered
selector. A known container name may be supplied directly:

```bash
./setup.sh docker-manage getbible-robot-production
./setup.sh docker-shell getbible-robot-production
```

`docker-manage` opens `/app/setup.sh` inside the selected container. Its menu
supports listing, status, diagnostics, start, stop, restart, configuration
reload, and a non-root Bash shell. Direct equivalents are:

```bash
docker exec getbible-robot-production /app/setup.sh list
docker exec getbible-robot-production /app/setup.sh status production
docker exec getbible-robot-production /app/setup.sh doctor production
docker exec getbible-robot-production /app/setup.sh restart production
docker exec -it getbible-robot-production /app/setup.sh
docker exec -it getbible-robot-production /bin/bash
docker logs --since 30m getbible-robot-production
```

The shell runs as the image's unprivileged UID/GID 10001. The root filesystem
remains read-only; only the instance data volume and bounded `/tmp` tmpfs are
writable.

## User experience under load

Resource controls do not change the Telegram command, search, selection,
posting, or Mini App interface. The expensive search worker has an independent
bounded executor. A slow search therefore does not consume the direct
Scripture lookup workers or block ordinary Telegram update handling.

The supplied small-host defaults use:

- four concurrent Telegram updates;
- two direct Scripture/catalog workers;
- one expensive search worker;
- bounded interactive and Mini App sessions;
- one resident search corpus and one translation cache;
- a 210 MiB per-bot RSS guard inside a 256 MiB container limit.

When capacity is temporarily exhausted, the application returns a controlled
retry message while liveness remains responsive. Three consecutive liveness
failures restart the bot child. Repeated failures open the restart circuit so
the container cannot consume the host in an endless restart/prewarm loop.

The search result, verse selection, single-verse/range behavior, saved default
translation, CJK handling, and explicit final posting flow are unchanged.

## Resource profile for the 500 MB host

Current KJV measurements are approximately:

| State | Robot RSS |
|---|---:|
| Mini App listener initialized | 72 MiB |
| Default KJV search index warmed | 111 MiB |
| All four KJV search modes exercised | 148 MiB |

The default 256 MiB container limit is the recommended one-bot profile for a
500 MB RAM, 2-vCPU host. It leaves memory outside the container for the kernel,
SSH, Docker, and an external reverse proxy. The child supervisor restarts the
bot before it can cross 210 MiB RSS, while Docker remains the final aggregate
limit.

Translations vary in size. The largest corpus observed in July 2026 was about
31 MB before Python parsing and indexing. Load-test non-default translations
before lowering the guard or increasing search concurrency.

## Multi-bot mode

Use multi-bot mode only when sharing one container is intentional:

```bash
mkdir -p docker/instances docker/secrets
cp docker/examples/production.env.example \
  docker/instances/production.env
printf '%s\n' 'TOKEN_FROM_BOTFATHER' \
  > docker/secrets/production.token
sudo chown 10001:10001 docker/secrets/production.token
chmod 400 docker/secrets/production.token
./setup.sh docker-deploy --multi
```

Each `/config/instances/*.env` file defines one bot. Every enabled Mini App,
health listener, or webhook listener must use a unique container port. Add the
matching secret and application-port mapping to `compose.multi.yaml` for each
additional instance, then run:

```bash
docker compose -f compose.multi.yaml up -d
docker exec getbible-robot getbible-robot-container reload
```

The supervisor rejects port collisions before starting a second bot and
isolates cache and SQLite state under `/data/<instance>`.

Budget about 210 MiB per warmed bot plus supervisor overhead. Two warmed bots
do not fit safely in the supplied 320 MiB multi-container limit or on the
current 500 MB host. Separate containers improve isolation but do not remove
the physical host memory requirement.

## Cluster deployment

[`deploy/kubernetes.example.yaml`](../deploy/kubernetes.example.yaml) is the
one-bot-per-workload cluster example. It includes:

- one replica per Telegram bot token;
- a mounted token Secret;
- persistent bounded cache and preference state;
- startup, liveness, and readiness probes;
- CPU, memory, PID, and ephemeral-storage controls;
- a Service exposing only the Mini App application port.

Keep `replicas: 1` for polling and for the current process-local Mini App
session model. Deploy additional bot tokens as separate workloads.

## External proxy contract

Forward the exact path in `MINI_APP_PUBLIC_URL` to the bot's application port.
Preserve `Host`, set trusted forwarding headers at the proxy, and do not expose
plain HTTP directly to the internet. Apply:

- request-header and request-body timeouts;
- a 64 KiB body cap;
- an idle connection timeout;
- connection and request-rate budgets;
- current TLS certificate and protocol policy.

The embedded Tornado server independently applies a 16 KiB header cap, 64 KiB
body cap, 10-second body timeout, 30-second idle/header timeout, and 128 KiB
socket buffer.

## Build validation

The exact Python dependency lock is installed with `--require-hashes`.

```bash
docker build --pull -t getbible-robot:test .
docker run --rm --entrypoint python getbible-robot:test \
  -c 'from config import Settings; print("runtime imports OK")'
docker run --rm --entrypoint /app/setup.sh \
  getbible-robot:test help
```

CI validates all Compose models, builds the exact image, verifies the non-root
user, and smoke-tests both the supervisor and the in-container setup utility.
