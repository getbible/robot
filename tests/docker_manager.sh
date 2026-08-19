#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT

FAKE_BIN="${TEST_ROOT}/bin"
FAKE_DOCKER_LOG="${TEST_ROOT}/docker.calls"
mkdir -p "$FAKE_BIN"
export FAKE_DOCKER_LOG

cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%q ' "$@" >>"$FAKE_DOCKER_LOG"
printf '\n' >>"$FAKE_DOCKER_LOG"

case "${1:-}" in
    info)
        exit 0
        ;;
    compose)
        if [[ "${2:-}" == "version" ]]; then
            printf 'Docker Compose version fixture\n'
        elif [[ " $* " == *" ps "* ]]; then
            printf 'NAME                         STATUS\n'
            printf 'getbible-robot-production    Up (healthy)\n'
        elif [[ " $* " == *" logs "* ]]; then
            printf '{"event":"supervisor_started"}\n'
        fi
        ;;
    ps)
        if [[ "$*" == *"{{.Names}}"* ]]; then
            printf 'getbible-robot-production\n'
        else
            printf 'NAMES                        STATUS         PORTS\n'
            printf 'getbible-robot-production    Up (healthy)   127.0.0.1:9201->9201/tcp\n'
        fi
        ;;
    inspect)
        if [[ "$*" == *"io.getbible.robot.container"* ]]; then
            printf 'true\n'
        elif [[ "$*" == *".State.Running"* ]]; then
            printf 'true\n'
        else
            printf 'Container:     /getbible-robot-production\n'
            printf 'Image:         getbible-robot:fixture\n'
            printf 'State:         running\n'
            printf 'Health:        healthy\n'
            printf 'Restarts:      0\n'
        fi
        ;;
    exec)
        printf '{"ok":true,"instances":[{"instance":"production","state":"running"}]}\n'
        ;;
    logs)
        printf '{"level":"INFO","event":"fixture"}\n'
        ;;
    *)
        printf 'Unexpected fake Docker invocation: %s\n' "$*" >&2
        exit 1
        ;;
esac
EOF
chmod 0700 "${FAKE_BIN}/docker"

export PATH="${FAKE_BIN}:${PATH}"
export TELEGRAM_API_TOKEN="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
export MINI_APP_PUBLIC_URL="https://bot.example.com/getbible/production"

ENV_FILE="${TEST_ROOT}/deployment/robot.env"
INIT_OUTPUT=$(
    bash "${ROOT}/setup.sh" docker-init --env-file "$ENV_FILE"
)
[[ "$INIT_OUTPUT" == *"Created editable Docker environment"* &&
    -f "$ENV_FILE" ]] || {
    printf 'Docker init did not create the editable environment.\n' >&2
    exit 1
}
[[ "$(stat -c '%a' "$ENV_FILE")" == "600" ]] || {
    printf 'Docker init environment permissions are not private.\n' >&2
    exit 1
}
for expected in \
    'ROBOT_MEMORY_LIMIT=2g' \
    'ROBOT_MEMORY_RESERVATION=1536m' \
    'CONTAINER_INSTANCE_MEMORY_LIMIT_MB=1792' \
    'ROBOT_CPU_LIMIT=2' \
    'ROBOT_PIDS_LIMIT=256' \
    'MAX_CONCURRENT_LOOKUPS=8' \
    'MAX_CONCURRENT_SEARCHES=4' \
    'MAX_CONCURRENT_UPDATES=16' \
    'MINI_APP_SESSION_TTL_SECONDS=10800' \
    'MINI_APP_HOST_PORT=9201' \
    'TELEGRAM_WEBHOOK_HOST_PORT=9001'; do
    grep -Fqx -- "$expected" "$ENV_FILE" || {
        printf 'Docker init omitted recommended override: %s\n' "$expected" >&2
        exit 1
    }
done

LIST_OUTPUT=$(bash "${ROOT}/setup.sh" docker-list)
[[ "$LIST_OUTPUT" == *"getbible-robot-production"* ]] || {
    printf 'Docker list did not show the fixture container.\n' >&2
    exit 1
}

STATUS_OUTPUT=$(
    bash "${ROOT}/setup.sh" docker-status getbible-robot-production
)
[[ "$STATUS_OUTPUT" == *"Bot supervisor status:"* &&
    "$STATUS_OUTPUT" == *'"state":"running"'* ]] || {
    printf 'Docker status did not include supervisor state.\n' >&2
    exit 1
}

LOG_OUTPUT=$(
    bash "${ROOT}/setup.sh" docker-logs getbible-robot-production 10
)
[[ "$LOG_OUTPUT" == *'"event":"fixture"'* ]] || {
    printf 'Docker logs did not return container stdout.\n' >&2
    exit 1
}

DOCTOR_OUTPUT=$(
    bash "${ROOT}/setup.sh" docker-doctor getbible-robot-production
)
[[ "$DOCTOR_OUTPUT" == *"Bot supervisor diagnostics:"* &&
    "$DOCTOR_OUTPUT" == *"Recent stdout/stderr:"* ]] || {
    printf 'Docker diagnostics did not include all sections.\n' >&2
    exit 1
}

DEPLOY_OUTPUT=$(bash "${ROOT}/setup.sh" docker-deploy)
[[ "$DEPLOY_OUTPUT" == *"Pulling the configured GetBible Robot image"* &&
    "$DEPLOY_OUTPUT" == *"Deploying GetBible Robot"* &&
    "$DEPLOY_OUTPUT" == *"Initial container output:"* ]] || {
    printf 'Docker deploy did not complete the expected lifecycle.\n' >&2
    exit 1
}

VALIDATE_OUTPUT=$(
    bash "${ROOT}/setup.sh" docker-validate --env-file "$ENV_FILE"
)
[[ "$VALIDATE_OUTPUT" == *"application configuration are valid"* ]] || {
    printf 'Docker validate did not complete the application validation.\n' >&2
    exit 1
}

RESTART_OUTPUT=$(
    bash "${ROOT}/setup.sh" docker-restart --env-file "$ENV_FILE"
)
[[ "$RESTART_OUTPUT" == *"configuration changes take effect"* ]] || {
    printf 'Docker restart did not recreate the workload.\n' >&2
    exit 1
}

LOCAL_BUILD_OUTPUT=$(
    bash "${ROOT}/setup.sh" docker-deploy --build --env-file "$ENV_FILE"
)
[[ "$LOCAL_BUILD_OUTPUT" == *"Building the local GetBible Robot image"* ]] || {
    printf 'Docker local-build deployment did not build the source image.\n' >&2
    exit 1
}

grep -Fq -- "compose --project-directory ${ROOT} --file ${ROOT}/compose.yaml config --quiet" \
    "$FAKE_DOCKER_LOG" || {
    printf 'Docker deploy did not validate the recommended Compose file.\n' >&2
    exit 1
}
grep -Fq -- "compose --project-directory ${ROOT} --file ${ROOT}/compose.yaml pull robot" \
    "$FAKE_DOCKER_LOG" || {
    printf 'Docker deploy did not pull the configured published image.\n' >&2
    exit 1
}
grep -Fq -- "compose --project-directory ${ROOT} --file ${ROOT}/compose.yaml up --detach --no-build" \
    "$FAKE_DOCKER_LOG" || {
    printf 'Docker deploy did not start the prepared image without rebuilding.\n' >&2
    exit 1
}
grep -Fq -- "compose --env-file ${ENV_FILE} --project-directory ${ROOT} --file ${ROOT}/compose.yaml pull robot" \
    "$FAKE_DOCKER_LOG" || {
    printf 'Docker validate did not pull the configured published image.\n' >&2
    exit 1
}
grep -Fq -- "compose --env-file ${ENV_FILE} --project-directory ${ROOT} --file ${ROOT}/compose.yaml --file ${ROOT}/compose.build.yaml build --pull robot" \
    "$FAKE_DOCKER_LOG" || {
    printf 'Docker local-build deployment did not use the build overlay.\n' >&2
    exit 1
}
grep -Fq -- "--force-recreate --no-build" "$FAKE_DOCKER_LOG" || {
    printf 'Docker restart did not force a configuration-aware recreation.\n' >&2
    exit 1
}

printf 'Docker manager test passed.\n'
