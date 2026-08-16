#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

PROGRAM="getbible-robot"
VERSION="6"
SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)

ETC_ROOT="/etc/getbible-robot"
INSTANCE_ROOT="/opt/getbible-robot"
STATE_ROOT="/var/lib/getbible-robot"
CACHE_ROOT="/var/cache/getbible-robot"
LOG_ROOT="/var/log/getbible-robot"
METADATA_ROOT="${ETC_ROOT}/instances"
UNIT_PATH="/etc/systemd/system/getbible-robot@.service"
MANAGER_PATH="/usr/local/sbin/getbible-robot"
LOGROTATE_PATH="/etc/logrotate.d/getbible-robot"
SETUP_LOG="${LOG_ROOT}/setup.log"
CADDY_ROOT="/etc/caddy"
CADDYFILE="${CADDY_ROOT}/Caddyfile"
CADDY_ROUTES="${CADDY_ROOT}/getbible-robot.caddy"
CADDY_IMPORT_BEGIN="# BEGIN getbible-robot managed routes"
CADDY_IMPORT_END="# END getbible-robot managed routes"
CADDY_APT_KEYRING="/usr/share/keyrings/caddy-stable-archive-keyring.gpg"
CADDY_APT_SOURCE="/etc/apt/sources.list.d/caddy-stable.list"
CADDY_APT_KEY_URL="https://dl.cloudsmith.io/public/caddy/stable/gpg.key"
CADDY_APT_SOURCE_URL="https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt"
if [[ -f "${SCRIPT_DIR}/deploy/getbible-robot@.service" ]]; then
    UNIT_SOURCE="${SCRIPT_DIR}/deploy/getbible-robot@.service"
else
    UNIT_SOURCE="$UNIT_PATH"
fi

ACTIVE_INSTANCE=""
ACTIVE_USER=""
ACTIVE_PORT=""
ACTIVE_SHA=""
ACTIVE_SOURCE_URL=""
ACTIVE_CREATED_AT=""
TEMP_PATHS=()
INSTALLING_INSTANCE=""
INSTALLING_USER=""
INSTALL_TRANSACTION=0
UPGRADE_NEXT=""
CADDY_TRANSACTION_DIR=""
CADDY_WAS_ACTIVE=""
CADDY_WAS_ENABLED=""

DEFAULT_SYSTEMD_MEMORY_HIGH_MB=1536
DEFAULT_SYSTEMD_MEMORY_MAX_MB=2048
DEFAULT_SYSTEMD_MEMORY_SWAP_MAX_MB=512
DEFAULT_SYSTEMD_TASKS_MAX=256
DEFAULT_SYSTEMD_NOFILE_LIMIT=4096
DEFAULT_SYSTEMD_CPU_QUOTA_PERCENT=200
DEFAULT_MAX_CONCURRENT_LOOKUPS=8
DEFAULT_MAX_CONCURRENT_SEARCHES=4
DEFAULT_MAX_CONCURRENT_UPDATES=16

info() {
    printf '==> %s\n' "$*"
}

warn() {
    printf 'WARNING: %s\n' "$*" >&2
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    local path
    for path in "${TEMP_PATHS[@]:-}"; do
        if [[ -n "$path" && -e "$path" && "$path" == "${INSTANCE_ROOT}/"* ]]; then
            rm -rf --one-file-system -- "$path"
        fi
    done
    if [[ -n "$UPGRADE_NEXT" && -e "$UPGRADE_NEXT" && "$UPGRADE_NEXT" == "${INSTANCE_ROOT}/"* ]]; then
        rm -rf --one-file-system -- "$UPGRADE_NEXT"
    fi
    if ((INSTALL_TRANSACTION == 1)) && [[ -n "$INSTALLING_INSTANCE" ]]; then
        systemctl disable --now "$(service_name_for "$INSTALLING_INSTANCE")" \
            >/dev/null 2>&1 || true
        rm -rf --one-file-system -- \
            "${INSTANCE_ROOT:?}/${INSTALLING_INSTANCE}" \
            "${CACHE_ROOT:?}/${INSTALLING_INSTANCE}" \
            "${STATE_ROOT:?}/${INSTALLING_INSTANCE}"
        rm -f -- \
            "$(environment_file_for "$INSTALLING_INSTANCE")" \
            "$(metadata_file_for "$INSTALLING_INSTANCE")" \
            "$(log_file_for "$INSTALLING_INSTANCE")" \
            "$(welcome_file_for "$INSTALLING_INSTANCE")" \
            "$(help_file_for "$INSTALLING_INSTANCE")"
        rm -rf --one-file-system -- \
            "$(resource_dropin_dir_for "$INSTALLING_INSTANCE")"
        if [[ -n "$INSTALLING_USER" ]] && id "$INSTALLING_USER" >/dev/null 2>&1; then
            userdel "$INSTALLING_USER" >/dev/null 2>&1 || true
        fi
    fi
    if [[ -n "$CADDY_TRANSACTION_DIR" ]]; then
        rollback_caddy_transaction || true
    fi
}
trap cleanup EXIT

usage() {
    cat <<'EOF'
GetBible Robot secure multi-instance setup and operations manager.

Usage:
  sudo ./setup.sh install [options]
  sudo getbible-robot <command> [instance]

Commands:
  install     Securely install a new isolated instance
  list        List every managed instance
  start       Start an instance
  stop        Stop an instance
  restart     Restart an instance
  status      Show service, deployment, health, and log status
  runtime     Show detailed runtime and aggregate metrics
  logs        Show recent per-instance JSON logs
  follow      Follow per-instance JSON logs
  doctor      Run non-destructive deployment diagnostics
  repair      Restore secure application access for the service account
  config      Safely edit, validate, and optionally restart configuration
  delivery    Switch safely between polling and HTTPS webhook delivery
  miniapp     Configure the authenticated Telegram Mini App HTTPS route
  content     Edit the welcome or detailed help text
  update      Deploy the current reviewed checkout (alias for upgrade)
  upgrade     Deploy the exact commit from a reviewed source checkout
  rollback    Return to the immediately previous deployed application
  uninstall   Remove one instance after explicit confirmation
  docker-deploy [--multi] [--secure] [--build] [--env-file FILE]
              Pull and deploy the recommended Docker layout
  docker-update [--multi] [--secure] [--build] [--env-file FILE]
              Pull the configured image and recreate the Docker workload
  docker-init [--env-file FILE]
              Create a private, editable Compose environment file
  docker-config [--env-file FILE] [--build] [--no-restart]
              Edit, validate, and apply the single-bot Compose environment
  docker-validate [--multi] [--secure] [--build] [--env-file FILE]
              Validate Compose and single-bot application configuration
  docker-restart [--multi] [--secure] [--build] [--env-file FILE]
              Recreate the Compose workload so direct configuration edits apply
  docker-list List GetBible Robot containers
  docker-status [container]
              Show Docker and supervised bot status
  docker-logs [container] [lines]
              Show recent container stdout/stderr
  docker-follow [container]
              Follow container stdout/stderr
  docker-manage [container]
              Open the interactive setup utility inside a container
  docker-shell [container]
              Open a non-root Bash shell inside a container
  docker-doctor [container]
              Run container, supervisor, and log diagnostics
  menu        Open the interactive operations menu
  self-test   Run safe manager validation tests
  help        Show this help

When an instance argument is omitted, an interactive terminal presents a
numbered selector. Non-interactive commands must provide the instance name.

Install options:
  --source DIR                  Reviewed source checkout
  --mini-app-listen IP          Mini App listen address (default 127.0.0.1)
  --mini-app-port PORT          Reverse-proxy backend port for the Mini App
  --webhook-listen IP           Bot-host IP reachable by the webhook proxy
  --webhook-port PORT           Reverse-proxy backend port for webhook traffic
  --health-port PORT            Private health/metrics listener (0 disables)
  --max-concurrent-lookups N    Direct-reference/catalog workers (default 8)
  --max-concurrent-searches N   CPU-bound search workers (default 4)
  --max-concurrent-updates N    Telegram update concurrency (default 16)
  --memory-high-mb N            systemd memory pressure threshold (default 1536)
  --memory-max-mb N             systemd hard memory ceiling (default 2048)
  --memory-swap-max-mb N        systemd swap ceiling (default 512)
  --tasks-max N                 systemd task ceiling (default 256)
  --nofile-limit N              Open-file ceiling (default 4096)
  --cpu-quota-percent N         systemd CPU quota; 200 means two CPUs
EOF
}

require_root() {
    [[ ${EUID} -eq 0 ]] || die "Run this command through sudo or as root."
}

require_tty() {
    [[ -t 0 && -t 1 ]] || die "This operation requires an interactive terminal."
}

validate_instance_name() {
    local value=${1:-}
    [[ ${#value} -ge 2 && ${#value} -le 24 ]] || return 1
    [[ "$value" =~ ^[a-z][a-z0-9-]*[a-z0-9]$ ]] || return 1
    [[ "$value" != *--* ]] || return 1
}

validate_translation() {
    local value=${1:-}
    [[ "$value" =~ ^[a-z0-9][a-z0-9_-]{0,29}$ ]]
}

validate_token_shape() {
    local value=${1:-}
    [[ "$value" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,64}$ ]]
}

validate_port() {
    local value=${1:-}
    [[ "$value" =~ ^[0-9]+$ ]] || return 1
    (( value >= 0 && value <= 65535 ))
}

validate_bounded_integer() {
    local value=${1:-}
    local minimum=$2
    local maximum=$3
    [[ "$value" =~ ^[0-9]+$ ]] || return 1
    ((value >= minimum && value <= maximum))
}

resource_dropin_dir_for() {
    printf '%s/getbible-robot@%s.service.d\n' "$(dirname "$UNIT_PATH")" "$1"
}

resource_dropin_for() {
    printf '%s/resources.conf\n' "$(resource_dropin_dir_for "$1")"
}

validate_proxy_listener() {
    local python_bin=$1
    local listen_address=$2
    "$python_bin" - "$listen_address" <<'PY' >/dev/null
from ipaddress import ip_address
import sys

address = ip_address(sys.argv[1])
if address.is_multicast or address.is_link_local:
    raise SystemExit(1)
PY
}

validate_specific_listener() {
    local python_bin=$1
    local listen_address=$2
    "$python_bin" - "$listen_address" <<'PY' >/dev/null
from ipaddress import ip_address
import sys

address = ip_address(sys.argv[1])
if address.is_unspecified or address.is_multicast or address.is_link_local:
    raise SystemExit(1)
PY
}

validate_resource_profile() {
    local memory_high_mb=$1
    local memory_max_mb=$2
    local memory_swap_max_mb=$3
    local tasks_max=$4
    local nofile_limit=$5
    local cpu_quota_percent=$6
    validate_bounded_integer "$memory_high_mb" 128 262144 ||
        die "MemoryHigh must be 128-262144 MiB."
    validate_bounded_integer "$memory_max_mb" 256 262144 ||
        die "MemoryMax must be 256-262144 MiB."
    ((memory_high_mb <= memory_max_mb)) ||
        die "MemoryHigh cannot exceed MemoryMax."
    validate_bounded_integer "$memory_swap_max_mb" 0 262144 ||
        die "MemorySwapMax must be 0-262144 MiB."
    validate_bounded_integer "$tasks_max" 32 65536 ||
        die "TasksMax must be 32-65536."
    validate_bounded_integer "$nofile_limit" 256 1048576 ||
        die "LimitNOFILE must be 256-1048576."
    validate_bounded_integer "$cpu_quota_percent" 10 6400 ||
        die "CPUQuota must be 10-6400 percent."
}

write_resource_dropin() {
    local instance=$1
    local memory_high_mb=$2
    local memory_max_mb=$3
    local memory_swap_max_mb=$4
    local tasks_max=$5
    local nofile_limit=$6
    local cpu_quota_percent=$7
    local directory
    local file
    validate_resource_profile \
        "$memory_high_mb" "$memory_max_mb" "$memory_swap_max_mb" \
        "$tasks_max" "$nofile_limit" "$cpu_quota_percent"
    directory=$(resource_dropin_dir_for "$instance")
    file=$(resource_dropin_for "$instance")
    install -d -o root -g root -m 0755 "$directory"
    {
        printf '[Service]\n'
        printf 'MemoryHigh=%sM\n' "$memory_high_mb"
        printf 'MemoryMax=%sM\n' "$memory_max_mb"
        printf 'MemorySwapMax=%sM\n' "$memory_swap_max_mb"
        printf 'TasksMax=%s\n' "$tasks_max"
        printf 'LimitNOFILE=%s\n' "$nofile_limit"
        printf 'CPUQuota=%s%%\n' "$cpu_quota_percent"
    } >"$file"
    chown root:root "$file"
    chmod 0644 "$file"
}

validate_delivery_mode() {
    [[ ${1:-} == "polling" || ${1:-} == "webhook" ]]
}

validate_reverse_proxy_mode() {
    [[ ${1:-} == "caddy" || ${1:-} == "external" ]]
}

validate_docker_container_name() {
    local value=${1:-}
    [[ -n "$value" && ${#value} -le 128 ]] || return 1
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
}

validate_webhook_url() {
    local value=${1:-}
    [[ "$value" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$ ]]
}

validate_mini_app_url() {
    local value=${1:-}
    local authority
    local host
    [[ "$value" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9_-]+)*/?$ ]] ||
        return 1
    authority=${value#https://}
    authority=${authority%%/*}
    host=${authority%%:*}
    [[ "$host" != "localhost" && "$host" != *.localhost ]] || return 1
    [[ "$host" != "0.0.0.0" && "$host" != 127.* && "$host" != 10.* &&
        "$host" != 192.168.* && "$host" != 169.254.* &&
        ! "$host" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. ]] || return 1
}

validate_plain_text() {
    local value=${1:-}
    local maximum=$2
    [[ -n "$value" && ${#value} -le maximum && "$value" != *\"* ]]
}

service_user_for() {
    printf 'gb-%s\n' "$1"
}

service_name_for() {
    printf 'getbible-robot@%s.service\n' "$1"
}

metadata_file_for() {
    printf '%s/%s.conf\n' "$METADATA_ROOT" "$1"
}

environment_file_for() {
    printf '%s/%s.env\n' "$ETC_ROOT" "$1"
}

log_file_for() {
    printf '%s/%s.jsonl\n' "$LOG_ROOT" "$1"
}

welcome_file_for() {
    printf '%s/%s.welcome.txt\n' "$ETC_ROOT" "$1"
}

help_file_for() {
    printf '%s/%s.help.txt\n' "$ETC_ROOT" "$1"
}

application_dir_for() {
    printf '%s/%s/app\n' "$INSTANCE_ROOT" "$1"
}

instance_exists() {
    [[ -f "$(metadata_file_for "$1")" ]]
}

prompt() {
    local label=$1
    local default=${2:-}
    local answer
    if [[ -n "$default" ]]; then
        read -r -p "${label} [${default}]: " answer
        printf '%s\n' "${answer:-$default}"
    else
        read -r -p "${label}: " answer
        printf '%s\n' "$answer"
    fi
}

confirm() {
    local label=$1
    local default=${2:-no}
    local suffix="[y/N]"
    local answer
    [[ "$default" == "yes" ]] && suffix="[Y/n]"
    read -r -p "${label} ${suffix}: " answer
    answer=${answer:-$default}
    [[ "${answer,,}" == "y" || "${answer,,}" == "yes" ]]
}

record_operation() {
    local action=$1
    local instance=${2:-none}
    local result=${3:-ok}
    local operator=${SUDO_USER:-root}
    if [[ -d "$LOG_ROOT" ]]; then
        printf '%s operator=%q action=%q instance=%q result=%q\n' \
            "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" \
            "$operator" "$action" "$instance" "$result" >>"$SETUP_LOG"
        chmod 0640 "$SETUP_LOG"
    fi
}

install_host_prerequisites() {
    local missing=()
    local command
    for command in git curl tar systemctl systemd-analyze logrotate runuser ss useradd nologin; do
        command -v "$command" >/dev/null 2>&1 || missing+=("$command")
    done
    if ((${#missing[@]} == 0)); then
        return
    fi

    info "Missing host tools: ${missing[*]}"
    confirm "Install the required host packages now?" yes ||
        die "Install the missing host tools and run setup again."

    if command -v apt-get >/dev/null 2>&1; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install --yes \
            ca-certificates curl git iproute2 logrotate python3 python3-venv tar util-linux
    elif command -v dnf >/dev/null 2>&1; then
        dnf install --assumeyes \
            ca-certificates curl git iproute logrotate python3 shadow-utils tar util-linux
    else
        die "Automatic package installation supports apt-get and dnf. Install git, curl, tar, systemd, logrotate, Python 3.10-3.14, and the matching venv package."
    fi
}

python_supported() {
    "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) else 1)
PY
}

select_python() {
    local candidates=()
    local candidate
    if [[ -n "${PYTHON_BIN:-}" ]]; then
        candidates+=("$PYTHON_BIN")
    fi
    candidates+=(python3.14 python3.13 python3.12 python3.11 python3.10 python3)
    for candidate in "${candidates[@]}"; do
        if command -v "$candidate" >/dev/null 2>&1 && python_supported "$candidate"; then
            command -v "$candidate"
            return
        fi
    done
    die "A supported Python 3.10 through 3.14 interpreter is required."
}

git_source_read() {
    local directory=$1
    shift
    # The manager runs as root, but the reviewed checkout belongs to the
    # operator. Prevent read-only source inspection from refreshing or locking
    # that checkout's index as root.
    GIT_OPTIONAL_LOCKS=0 git -C "$directory" "$@"
}

resolve_source_dir() {
    local requested=${1:-}
    local candidate
    if [[ -n "$requested" ]]; then
        candidate=$(readlink -f "$requested")
    elif git_source_read "$SCRIPT_DIR" rev-parse --show-toplevel \
        >/dev/null 2>&1; then
        candidate=$(git_source_read "$SCRIPT_DIR" rev-parse --show-toplevel)
    elif git_source_read "$PWD" rev-parse --show-toplevel >/dev/null 2>&1; then
        candidate=$(git_source_read "$PWD" rev-parse --show-toplevel)
    else
        die "Run install/upgrade from a GetBible Robot Git checkout or pass --source DIR."
    fi

    [[ -f "${candidate}/bot.py" ]] || die "Source checkout is missing bot.py."
    [[ -f "${candidate}/setup.sh" ]] || die "Source checkout is missing setup.sh."
    [[ -f "${candidate}/requirements.txt" ]] || die "Source checkout is missing requirements.txt."
    [[ -f "${candidate}/.env.template" ]] || die "Source checkout is missing .env.template."
    [[ -f "${candidate}/deploy/getbible-robot@.service" ]] ||
        die "Source checkout is missing deploy/getbible-robot@.service."
    [[ -f "${candidate}/deploy/welcome.txt" ]] ||
        die "Source checkout is missing deploy/welcome.txt."
    [[ -f "${candidate}/deploy/help.txt" ]] ||
        die "Source checkout is missing deploy/help.txt."
    git_source_read "$candidate" rev-parse --is-inside-work-tree \
        >/dev/null 2>&1 ||
        die "Source directory is not a Git checkout."
    git_source_read "$candidate" diff --quiet --ignore-submodules --
    git_source_read "$candidate" diff --cached --quiet --ignore-submodules --
    printf '%s\n' "$candidate"
}

source_url_for() {
    local source_dir=$1
    local url
    url=$(
        git_source_read "$source_dir" remote get-url origin 2>/dev/null || true
    )
    if [[ -z "$url" ]]; then
        url="https://github.com/getbible/robot.git"
    fi
    if [[ "$url" =~ ^https?://[^/]*@ ]]; then
        die "The Git origin URL contains credentials. Replace it with a credential-free URL before deployment."
    fi
    printf '%s\n' "$url"
}

load_instance() {
    local instance=$1
    local file
    validate_instance_name "$instance" || die "Invalid instance name: ${instance}"
    file=$(metadata_file_for "$instance")
    [[ -f "$file" ]] || die "Unknown instance: ${instance}"
    # Files are generated by this manager, root-owned, mode 0600, and contain no secrets.
    # shellcheck disable=SC1090
    source "$file"
    [[ "${INSTANCE:-}" == "$instance" ]] || die "Instance metadata identity mismatch."
    ACTIVE_INSTANCE=$INSTANCE
    ACTIVE_USER=$SERVICE_USER
    ACTIVE_PORT=$HEALTH_PORT
    ACTIVE_SHA=$DEPLOYED_SHA
    ACTIVE_SOURCE_URL=$SOURCE_URL
    ACTIVE_CREATED_AT=$CREATED_AT
}

instance_names() {
    local file
    shopt -s nullglob
    for file in "${METADATA_ROOT}"/*.conf; do
        basename "$file" .conf
    done
    shopt -u nullglob
}

select_instance() {
    local requested=${1:-}
    local names=()
    local index
    local choice
    if [[ -n "$requested" ]]; then
        load_instance "$requested"
        return
    fi
    mapfile -t names < <(instance_names | sort)
    ((${#names[@]} > 0)) || die "No managed instances are installed."
    if ((${#names[@]} == 1)); then
        load_instance "${names[0]}"
        return
    fi
    require_tty
    printf 'Select an instance:\n'
    for index in "${!names[@]}"; do
        printf '  %d) %s\n' "$((index + 1))" "${names[$index]}"
    done
    read -r -p "Selection: " choice
    [[ "$choice" =~ ^[0-9]+$ ]] || die "Selection must be a number."
    ((choice >= 1 && choice <= ${#names[@]})) || die "Selection is out of range."
    load_instance "${names[$((choice - 1))]}"
}

next_health_port() {
    local port
    local used
    for ((port = 8081; port <= 8181; port++)); do
        used=0
        while IFS= read -r existing; do
            load_instance "$existing"
            if [[ "$ACTIVE_PORT" == "$port" ]]; then
                used=1
                break
            fi
        done < <(instance_names)
        if ((used == 0)) && ! ss -ltnH 2>/dev/null | awk '{print $4}' |
            grep -Eq "(^|:)$port$"; then
            printf '%s\n' "$port"
            return
        fi
    done
    die "No unused automatic health port was found between 8081 and 8181."
}

next_webhook_port() {
    local port
    local used
    local existing
    local app_dir
    local env_file
    local configured
    local configured_url
    for ((port = 9001; port <= 9101; port++)); do
        used=0
        while IFS= read -r existing; do
            app_dir=$(application_dir_for "$existing")
            env_file=$(environment_file_for "$existing")
            if [[ -x "$app_dir/venv/bin/python" && -f "$env_file" ]]; then
                configured=$(dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_PORT")
                if [[ "$configured" == "$port" ]]; then
                    used=1
                    break
                fi
            fi
        done < <(instance_names)
        if ((used == 0)) && ! ss -ltnH 2>/dev/null | awk '{print $4}' |
            grep -Eq "(^|:)$port$"; then
            printf '%s\n' "$port"
            return
        fi
    done
    die "No unused automatic webhook port was found between 9001 and 9101."
}

next_mini_app_port() {
    local port
    local used
    local existing
    local app_dir
    local env_file
    local configured
    local configured_delivery
    local configured_health
    local configured_webhook
    for ((port = 9201; port <= 9301; port++)); do
        used=0
        while IFS= read -r existing; do
            app_dir=$(application_dir_for "$existing")
            env_file=$(environment_file_for "$existing")
            if [[ -x "$app_dir/venv/bin/python" && -f "$env_file" ]]; then
                configured=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PORT")
                configured_url=$(
                    dotenv_value "$app_dir" "$env_file" "MINI_APP_PUBLIC_URL"
                )
                configured_health=$(dotenv_value "$app_dir" "$env_file" "HEALTH_PORT")
                configured_delivery=$(
                    dotenv_value "$app_dir" "$env_file" "TELEGRAM_DELIVERY_MODE"
                )
                configured_webhook=$(
                    dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_PORT"
                )
                if [[ -n "$configured_url" && -n "$configured" && "$configured" != "0" &&
                    "$configured" == "$port" ]] ||
                    [[ "$configured_health" != "0" && "$configured_health" == "$port" ]] ||
                    [[ "$configured_delivery" == "webhook" &&
                        "$configured_webhook" == "$port" ]]; then
                    used=1
                    break
                fi
            fi
        done < <(instance_names)
        if ((used == 0)) && ! ss -ltnH 2>/dev/null | awk '{print $4}' |
            grep -Eq "(^|:)$port$"; then
            printf '%s\n' "$port"
            return
        fi
    done
    die "No unused automatic Mini App port was found between 9201 and 9301."
}

mini_app_port_conflicts() {
    local selected_instance=$1
    local selected_port=$2
    local existing
    local app_dir
    local env_file
    local metadata_file
    local value
    while IFS= read -r existing; do
        [[ "$existing" == "$selected_instance" ]] && continue
        metadata_file=$(metadata_file_for "$existing")
        value=$(sed -n -E 's/^HEALTH_PORT=([^[:space:]]+)$/\1/p' "$metadata_file" |
            head -n 1)
        if [[ "$value" != "0" && "$value" == "$selected_port" ]]; then
            return 0
        fi
        app_dir=$(application_dir_for "$existing")
        env_file=$(environment_file_for "$existing")
        [[ -x "$app_dir/venv/bin/python" && -f "$env_file" ]] || continue
        if [[ -n "$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PUBLIC_URL")" ]]; then
            value=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PORT")
            [[ -z "$value" || "$value" == "0" || "$value" != "$selected_port" ]] ||
                return 0
        fi
        value=$(dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_PORT")
        [[ -z "$value" || "$value" == "0" || "$value" != "$selected_port" ]] || return 0
    done < <(instance_names)
    return 1
}

generate_webhook_secret() {
    "$1" - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

token_from_env_file() {
    local file=$1
    sed -n \
        -e 's/^TELEGRAM_API_TOKEN="\([^"]*\)"$/\1/p' \
        -e 's/^TELEGRAM_API_TOKEN=\([A-Za-z0-9:_-]*\)$/\1/p' \
        "$file" | head -n 1
}

ensure_unique_token() {
    local candidate=$1
    local excluded_instance=${2:-}
    local instance
    local file
    local existing
    local existing_app
    while IFS= read -r instance; do
        [[ "$instance" == "$excluded_instance" ]] && continue
        file=$(environment_file_for "$instance")
        [[ -r "$file" ]] || continue
        existing_app=$(application_dir_for "$instance")
        if [[ -x "$existing_app/venv/bin/python" ]]; then
            existing=$(dotenv_value "$existing_app" "$file" "TELEGRAM_API_TOKEN")
        else
            existing=$(token_from_env_file "$file")
        fi
        if [[ -n "$existing" && "$existing" == "$candidate" ]]; then
            warn "That Telegram token is already assigned to instance '${instance}'. One bot token cannot safely run in two active instances."
            return 1
        fi
    done < <(instance_names)
    return 0
}

replace_env_value() {
    local python_bin=$1
    local file=$2
    local key=$3
    local value=$4
    "$python_bin" - "$file" "$key" "$value" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
    raise SystemExit("invalid environment key")
if any(character in value for character in '"\r\n'):
    raise SystemExit(f"{key} contains an unsupported character")
lines = path.read_text(encoding="utf-8").splitlines()
replacement = f'{key}="{value}"'
for index, line in enumerate(lines):
    if line.startswith(f"{key}="):
        lines[index] = replacement
        break
else:
    lines.extend(("", replacement))
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

ensure_env_value() {
    local python_bin=$1
    local file=$2
    local key=$3
    local value=$4
    if ! grep -Eq "^${key}=" "$file"; then
        replace_env_value "$python_bin" "$file" "$key" "$value"
    fi
}

migrate_env_default() {
    local python_bin=$1
    local file=$2
    local key=$3
    local old_value=$4
    local new_value=$5
    local current
    current=$(
        sed -n -E "s/^${key}=\"?([^\"[:space:]]+)\"?$/\\1/p" "$file" |
            head -n 1
    )
    if [[ "$current" == "$old_value" ]]; then
        replace_env_value "$python_bin" "$file" "$key" "$new_value"
    fi
}

migrate_instance_configuration() {
    local source_dir=$1
    local python_bin=$2
    local env_file=$3
    local service_user=$4
    local instance=$5
    local welcome_file
    local help_file
    local current_limit
    welcome_file=$(welcome_file_for "$instance")
    help_file=$(help_file_for "$instance")

    if [[ ! -f "$welcome_file" ]]; then
        install -o root -g "$service_user" -m 0640 \
            "${source_dir}/deploy/welcome.txt" "$welcome_file"
    fi
    if [[ ! -f "$help_file" ]]; then
        install -o root -g "$service_user" -m 0640 \
            "${source_dir}/deploy/help.txt" "$help_file"
    fi
    replace_env_value "$python_bin" "$env_file" "WELCOME_MESSAGE_FILE" "$welcome_file"
    replace_env_value "$python_bin" "$env_file" "HELP_MESSAGE_FILE" "$help_file"

    current_limit=$(
        sed -n -E 's/^GETBIBLE_MAX_RESPONSE_BYTES="?([0-9]+)"?$/\1/p' \
            "$env_file" | head -n 1
    )
    if [[ -z "$current_limit" || "$current_limit" == "8388608" ||
        "$current_limit" == "67108864" ]]; then
        replace_env_value "$python_bin" "$env_file" \
            "GETBIBLE_MAX_RESPONSE_BYTES" "41943040"
    fi
    ensure_env_value "$python_bin" "$env_file" \
        "SEARCH_MAX_RESPONSE_BYTES" "4194304"
    ensure_env_value "$python_bin" "$env_file" \
        "SEARCH_INDEX_BUILD_SECONDS" "120"
    ensure_env_value "$python_bin" "$env_file" "SEARCH_TIMEOUT" "150"
    ensure_env_value "$python_bin" "$env_file" "REFERENCE_CACHE_LIMIT" "1000"
    ensure_env_value "$python_bin" "$env_file" "BOOKS_CACHE_LIMIT" "16"
    ensure_env_value "$python_bin" "$env_file" "CHAPTER_CACHE_LIMIT" "256"
    ensure_env_value "$python_bin" "$env_file" "SEARCH_CORPUS_LIMIT" "1"
    ensure_env_value "$python_bin" "$env_file" \
        "SEARCH_SHARED_CORPUS_LIMIT" "8"
    ensure_env_value "$python_bin" "$env_file" "TRANSLATION_CACHE_LIMIT" "1"
    ensure_env_value "$python_bin" "$env_file" "CACHE_MAX_BYTES" "268435456"
    ensure_env_value "$python_bin" "$env_file" \
        "CACHE_MAINTENANCE_INTERVAL_SECONDS" "21600"
    ensure_env_value "$python_bin" "$env_file" "MAX_CONCURRENT_SEARCHES" \
        "$DEFAULT_MAX_CONCURRENT_SEARCHES"
    migrate_env_default "$python_bin" "$env_file" "MAX_CONCURRENT_LOOKUPS" "2" \
        "$DEFAULT_MAX_CONCURRENT_LOOKUPS"
    migrate_env_default "$python_bin" "$env_file" "MAX_CONCURRENT_UPDATES" "4" \
        "$DEFAULT_MAX_CONCURRENT_UPDATES"
    migrate_env_default "$python_bin" "$env_file" "RATE_LIMIT_CACHE_SIZE" "20000" "2000"
    migrate_env_default \
        "$python_bin" "$env_file" "INTERACTION_SESSION_LIMIT" "2000" "200"
    ensure_env_value "$python_bin" "$env_file" "PREWARM_DEFAULT_TRANSLATION" "true"
    ensure_env_value "$python_bin" "$env_file" \
        "USER_PREFERENCES_FILE" "${STATE_ROOT}/${instance}/preferences.sqlite3"
    migrate_env_default \
        "$python_bin" "$env_file" "USER_PREFERENCE_LIMIT" "100000" "10000"
    ensure_env_value "$python_bin" "$env_file" "TELEGRAM_DELIVERY_MODE" "polling"
    ensure_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_PUBLIC_URL" ""
    ensure_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_LISTEN" "127.0.0.1"
    ensure_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_PORT" "9001"
    ensure_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_SECRET_TOKEN" ""
    ensure_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_IP_ADDRESS" ""
    ensure_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_MAX_CONNECTIONS" "16"
    ensure_env_value "$python_bin" "$env_file" "BOT_NAME" "GetBible Robot"
    ensure_env_value "$python_bin" "$env_file" \
        "BOT_DESCRIPTION" "Read and search Scripture in Telegram with GetBible."
    ensure_env_value "$python_bin" "$env_file" \
        "BOT_SHORT_DESCRIPTION" "Read and search Scripture with GetBible."
    ensure_env_value "$python_bin" "$env_file" "MINI_APP_ENABLED" "false"
    ensure_env_value "$python_bin" "$env_file" "REVERSE_PROXY_MODE" "caddy"
    ensure_env_value "$python_bin" "$env_file" "MINI_APP_PUBLIC_URL" ""
    ensure_env_value "$python_bin" "$env_file" "MINI_APP_LISTEN" "127.0.0.1"
    ensure_env_value "$python_bin" "$env_file" "MINI_APP_PORT" "9201"
    ensure_env_value "$python_bin" "$env_file" \
        "MINI_APP_INIT_DATA_MAX_AGE_SECONDS" "300"
    ensure_env_value "$python_bin" "$env_file" \
        "MINI_APP_LAUNCH_TTL_SECONDS" "300"
    ensure_env_value "$python_bin" "$env_file" \
        "MINI_APP_SESSION_TTL_SECONDS" "900"
    migrate_env_default \
        "$python_bin" "$env_file" "MINI_APP_SESSION_LIMIT" "2000" "200"
    ensure_env_value "$python_bin" "$env_file" "MINI_APP_SESSIONS_PER_USER" "2"
    ensure_env_value \
        "$python_bin" "$env_file" "MINI_APP_MAX_SEARCHES_PER_SESSION" "2"
    ensure_env_value \
        "$python_bin" "$env_file" "MINI_APP_MAX_AVAILABLE_SELECTIONS" "256"
    ensure_env_value "$python_bin" "$env_file" "MINI_APP_MAX_SELECTIONS" "100"
    ensure_env_value \
        "$python_bin" "$env_file" "MINI_APP_BODY_TIMEOUT_SECONDS" "10"
    ensure_env_value \
        "$python_bin" "$env_file" "MINI_APP_IDLE_TIMEOUT_SECONDS" "30"
    ensure_env_value "$python_bin" "$env_file" "MINI_APP_MAX_HEADER_BYTES" "16384"
    ensure_env_value "$python_bin" "$env_file" "LOG_MAX_BYTES" "10485760"
    ensure_env_value "$python_bin" "$env_file" "SYSTEMD_MEMORY_HIGH_MB" \
        "$DEFAULT_SYSTEMD_MEMORY_HIGH_MB"
    ensure_env_value "$python_bin" "$env_file" "SYSTEMD_MEMORY_MAX_MB" \
        "$DEFAULT_SYSTEMD_MEMORY_MAX_MB"
    ensure_env_value "$python_bin" "$env_file" "SYSTEMD_MEMORY_SWAP_MAX_MB" \
        "$DEFAULT_SYSTEMD_MEMORY_SWAP_MAX_MB"
    ensure_env_value "$python_bin" "$env_file" "SYSTEMD_TASKS_MAX" \
        "$DEFAULT_SYSTEMD_TASKS_MAX"
    ensure_env_value "$python_bin" "$env_file" "SYSTEMD_NOFILE_LIMIT" \
        "$DEFAULT_SYSTEMD_NOFILE_LIMIT"
    ensure_env_value "$python_bin" "$env_file" "SYSTEMD_CPU_QUOTA_PERCENT" \
        "$DEFAULT_SYSTEMD_CPU_QUOTA_PERCENT"
    ensure_env_value "$python_bin" "$env_file" "CONTAINERIZED" "false"
    chown root:root "$env_file"
    chmod 0600 "$env_file"
    verify_content_access "$service_user" "$instance"
}

validate_environment() {
    local app_dir=$1
    local env_file=$2
    "$app_dir/venv/bin/python" - "$env_file" <<'PY'
import os
import sys
from dotenv import dotenv_values

values = dotenv_values(sys.argv[1])
if any(value is None for value in values.values()):
    raise SystemExit("Environment file contains a key without a value.")
os.environ.update({key: value for key, value in values.items() if value is not None})
from config import Settings
Settings.from_env(load_environment_file=False)
PY
}

dotenv_value() {
    local app_dir=$1
    local env_file=$2
    local key=$3
    "$app_dir/venv/bin/python" - "$env_file" "$key" <<'PY'
import sys
from dotenv import dotenv_values

value = dotenv_values(sys.argv[1]).get(sys.argv[2])
print("" if value is None else value)
PY
}

sync_resource_dropin_from_env() {
    local app_dir=$1
    local env_file=$2
    local instance=$3
    local memory_high_mb
    local memory_max_mb
    local memory_swap_max_mb
    local tasks_max
    local nofile_limit
    local cpu_quota_percent
    memory_high_mb=$(dotenv_value "$app_dir" "$env_file" "SYSTEMD_MEMORY_HIGH_MB")
    memory_max_mb=$(dotenv_value "$app_dir" "$env_file" "SYSTEMD_MEMORY_MAX_MB")
    memory_swap_max_mb=$(
        dotenv_value "$app_dir" "$env_file" "SYSTEMD_MEMORY_SWAP_MAX_MB"
    )
    tasks_max=$(dotenv_value "$app_dir" "$env_file" "SYSTEMD_TASKS_MAX")
    nofile_limit=$(dotenv_value "$app_dir" "$env_file" "SYSTEMD_NOFILE_LIMIT")
    cpu_quota_percent=$(
        dotenv_value "$app_dir" "$env_file" "SYSTEMD_CPU_QUOTA_PERCENT"
    )
    write_resource_dropin \
        "$instance" "$memory_high_mb" "$memory_max_mb" \
        "$memory_swap_max_mb" "$tasks_max" "$nofile_limit" \
        "$cpu_quota_percent"
}

preflight_mini_app_dns() {
    local python_bin=$1
    local public_url=$2
    "$python_bin" - "$public_url" <<'PY'
from __future__ import annotations

import ipaddress
import socket
import sys
from urllib.parse import urlsplit

hostname = urlsplit(sys.argv[1]).hostname
if not hostname:
    raise SystemExit("The Mini App URL has no hostname.")
try:
    records = socket.getaddrinfo(
        hostname,
        443,
        type=socket.SOCK_STREAM,
    )
except socket.gaierror:
    raise SystemExit(
        f"DNS does not resolve {hostname}. Create its public A/AAAA record first."
    ) from None
addresses = sorted({record[4][0] for record in records})
if not addresses:
    raise SystemExit(
        f"DNS does not resolve {hostname}. Create its public A/AAAA record first."
    )
if not any(ipaddress.ip_address(address).is_global for address in addresses):
    raise SystemExit(
        f"DNS for {hostname} does not resolve to a public address."
    )
print(f"DNS preflight: {hostname} -> {', '.join(addresses)}")
PY
}

repair_apt_package_state() {
    if command -v dpkg >/dev/null 2>&1; then
        info "Completing any pending Debian package configuration."
        if ! DEBIAN_FRONTEND=noninteractive dpkg --configure --pending; then
            die "The Debian package manager could not complete pending configuration. Run 'sudo dpkg --configure -a', resolve the reported package error, and rerun setup."
        fi
    fi

    if DEBIAN_FRONTEND=noninteractive apt-get check >/dev/null 2>&1; then
        return
    fi

    info "Repairing incomplete Debian package dependencies."
    if ! DEBIAN_FRONTEND=noninteractive apt-get --fix-broken install --yes ||
        ! DEBIAN_FRONTEND=noninteractive dpkg --configure --pending ||
        ! DEBIAN_FRONTEND=noninteractive apt-get check; then
        die "The Debian package manager remains inconsistent. Run 'sudo dpkg --configure -a' and 'sudo apt-get --fix-broken install', resolve the reported error, and rerun setup."
    fi
}

install_caddy_with_apt() {
    local repository_temp
    local downloaded_key
    local downloaded_source
    local generated_keyring

    repair_apt_package_state
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install --yes \
        apt-transport-https ca-certificates curl \
        debian-archive-keyring debian-keyring gnupg

    repository_temp=$(mktemp -d)
    downloaded_key="${repository_temp}/caddy-stable.asc"
    downloaded_source="${repository_temp}/caddy-stable.list"
    generated_keyring="${repository_temp}/caddy-stable-archive-keyring.gpg"

    if ! curl \
        --proto '=https' \
        --tlsv1.2 \
        --fail \
        --silent \
        --show-error \
        --location \
        --output "$downloaded_key" \
        "$CADDY_APT_KEY_URL"; then
        rm -rf --one-file-system -- "$repository_temp"
        die "Could not download the official Caddy repository signing key."
    fi
    if ! curl \
        --proto '=https' \
        --tlsv1.2 \
        --fail \
        --silent \
        --show-error \
        --location \
        --output "$downloaded_source" \
        "$CADDY_APT_SOURCE_URL"; then
        rm -rf --one-file-system -- "$repository_temp"
        die "Could not download the official Caddy APT repository definition."
    fi
    if [[ ! -s "$downloaded_key" || ! -s "$downloaded_source" ]] ||
        ! grep -Fq -- "https://dl.cloudsmith.io/public/caddy/stable/" \
            "$downloaded_source" ||
        ! grep -Fq -- "signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg" \
            "$downloaded_source"; then
        rm -rf --one-file-system -- "$repository_temp"
        die "The downloaded Caddy repository metadata was empty or unexpected."
    fi
    if ! gpg \
        --batch \
        --yes \
        --dearmor \
        --output "$generated_keyring" \
        "$downloaded_key"; then
        rm -rf --one-file-system -- "$repository_temp"
        die "The official Caddy repository signing key was invalid."
    fi
    if ! install -d -o root -g root -m 0755 \
        "$(dirname "$CADDY_APT_KEYRING")" \
        "$(dirname "$CADDY_APT_SOURCE")" ||
        ! install -o root -g root -m 0644 \
            "$generated_keyring" "$CADDY_APT_KEYRING" ||
        ! install -o root -g root -m 0644 \
            "$downloaded_source" "$CADDY_APT_SOURCE"; then
        rm -rf --one-file-system -- "$repository_temp"
        die "Could not install the official Caddy APT repository files."
    fi
    rm -rf --one-file-system -- "$repository_temp"

    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install --yes caddy
}

install_caddy_with_dnf() {
    local platform=""
    if [[ -r /etc/os-release ]]; then
        platform=$(
            sed -n \
                -e 's/^ID=//p' \
                -e 's/^ID_LIKE=//p' \
                /etc/os-release |
                tr -d '"' |
                tr '\n' ' '
        )
    fi
    if [[ " ${platform,,} " == *" fedora "* ]]; then
        dnf install --assumeyes dnf5-plugins
    else
        dnf install --assumeyes dnf-plugins-core
    fi
    dnf copr enable --assumeyes @caddy/caddy
    dnf install --assumeyes caddy
}

ensure_caddy_available() {
    if ! command -v caddy >/dev/null 2>&1; then
        confirm "Install Caddy for managed Mini App HTTPS now?" yes ||
            die "Caddy is required for setup-managed Mini App HTTPS."
        if command -v apt-get >/dev/null 2>&1; then
            install_caddy_with_apt
        elif command -v dnf >/dev/null 2>&1; then
            install_caddy_with_dnf
        else
            die "Automatic Caddy installation supports APT and DNF hosts. Install Caddy's official system package and rerun this command."
        fi
    fi
    command -v caddy >/dev/null 2>&1 ||
        die "The official package did not provide the caddy command."
    systemctl cat caddy.service >/dev/null 2>&1 ||
        die "The caddy command exists but caddy.service is missing. Install Caddy's official system package and rerun setup."

    install -d -o root -g root -m 0755 "$CADDY_ROOT"
    if ! systemctl is-active --quiet caddy.service; then
        local port
        for port in 80 443; do
            if ss -ltnH 2>/dev/null | awk '{print $4}' |
                grep -Eq "(^|:)$port$"; then
                die "TCP port ${port} is already occupied while Caddy is inactive."
            fi
        done
    fi
}

snapshot_caddy_file() {
    local path=$1
    local label=$2
    local destination=$3
    if [[ -e "$path" ]]; then
        cp -a -- "$path" "${destination}/${label}"
        : >"${destination}/${label}.exists"
    fi
}

restore_caddy_file() {
    local path=$1
    local label=$2
    local source=$3
    if [[ -f "${source}/${label}.exists" ]]; then
        cp -a -- "${source}/${label}" "$path"
    else
        rm -f -- "$path"
    fi
}

ensure_caddy_import() {
    local begin_count=0
    local end_count=0
    local expected
    local actual
    touch "$CADDYFILE"
    begin_count=$(grep -Fxc -- "$CADDY_IMPORT_BEGIN" "$CADDYFILE" || true)
    end_count=$(grep -Fxc -- "$CADDY_IMPORT_END" "$CADDYFILE" || true)
    if [[ "$begin_count" == "0" && "$end_count" == "0" ]]; then
        {
            [[ ! -s "$CADDYFILE" ]] || printf '\n'
            printf '%s\n' "$CADDY_IMPORT_BEGIN"
            printf 'import %s\n' "$CADDY_ROUTES"
            printf '%s\n' "$CADDY_IMPORT_END"
        } >>"$CADDYFILE"
    elif [[ "$begin_count" == "1" && "$end_count" == "1" ]]; then
        expected=$(
            printf '%s\nimport %s\n%s' \
                "$CADDY_IMPORT_BEGIN" "$CADDY_ROUTES" "$CADDY_IMPORT_END"
        )
        actual=$(
            awk \
                -v begin="$CADDY_IMPORT_BEGIN" \
                -v end="$CADDY_IMPORT_END" \
                '$0 == begin {capture = 1} capture {print} $0 == end {exit}' \
                "$CADDYFILE"
        )
        [[ "$actual" == "$expected" ]] ||
            die "The managed Caddy import block was modified; restore it before retrying."
    else
        die "The Caddyfile contains duplicate or incomplete GetBible managed markers."
    fi
    chown root:root "$CADDYFILE"
    chmod 0644 "$CADDYFILE"
}

render_caddy_routes() {
    local destination=$1
    local excluded_instance=${2:-}
    local input
    local instance
    local app_dir
    local env_file
    local enabled
    local proxy_mode
    local public_url
    local port
    local python_bin
    input=$(mktemp)
    while IFS= read -r instance; do
        [[ "$instance" == "$excluded_instance" ]] && continue
        app_dir=$(application_dir_for "$instance")
        env_file=$(environment_file_for "$instance")
        [[ -x "$app_dir/venv/bin/python" && -f "$env_file" ]] || continue
        enabled=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_ENABLED")
        [[ "$enabled" == "true" ]] || continue
        proxy_mode=$(dotenv_value "$app_dir" "$env_file" "REVERSE_PROXY_MODE")
        [[ "${proxy_mode:-caddy}" == "caddy" ]] || continue
        public_url=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PUBLIC_URL")
        port=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PORT")
        validate_mini_app_url "$public_url" ||
            die "Instance ${instance} has an invalid managed Mini App URL."
        validate_port "$port" && ((port >= 1024)) ||
            die "Instance ${instance} has an invalid managed Mini App port."
        printf '%s\t%s\t%s\n' "$instance" "$public_url" "$port" >>"$input"
    done < <(instance_names | sort)

    python_bin=$(select_python)
    "$python_bin" - "$input" "$destination" <<'PY'
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
routes: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
seen: list[tuple[str, str, str]] = []

for raw_line in source.read_text(encoding="utf-8").splitlines():
    if not raw_line:
        continue
    instance, public_url, raw_port = raw_line.split("\t")
    parts = urlsplit(public_url)
    host = parts.netloc.casefold()
    path = parts.path.rstrip("/")
    for previous_instance, previous_host, previous_path in seen:
        if host != previous_host:
            continue
        overlaps = (
            path == previous_path
            or not path
            or not previous_path
            or path.startswith(previous_path + "/")
            or previous_path.startswith(path + "/")
        )
        if overlaps:
            raise SystemExit(
                "Managed Mini App routes overlap: "
                f"{previous_instance} ({previous_host}{previous_path or '/'}) and "
                f"{instance} ({host}{path or '/'})."
            )
    seen.append((instance, host, path))
    routes[host].append((instance, path, int(raw_port)))

lines = [
    "# Generated by getbible-robot. Do not edit.",
    "# Contains public routes only; no tokens or user data.",
]
for host in sorted(routes):
    lines.extend(("", f"{host} {{"))
    host_routes = sorted(routes[host], key=lambda item: (-len(item[1]), item[0]))
    for instance, path, port in host_routes:
        matcher = "gb_" + re.sub(r"[^A-Za-z0-9_]", "_", instance)
        if path:
            static_paths = (
                path,
                path + "/",
                path + "/index.html",
                path + "/app.js",
                path + "/styles.css",
                path + "/api-contract.json",
                path + "/lib/*",
                path + "/assets/*",
            )
            api_paths = (
                path + "/api/v1/session",
                path + "/api/v1/translations",
                path + "/api/v1/books",
                path + "/api/v1/chapters",
                path + "/api/v1/scripture",
                path + "/api/v1/search",
                path + "/api/v1/basket",
                path + "/api/v1/basket/items",
                path + "/api/v1/basket/order",
                path + "/api/v1/preferences",
                path + "/api/v1/post",
                path + "/api/v1/cleanup",
            )
            lines.extend(
                (
                    f"    @{matcher}_static path {' '.join(static_paths)}",
                    f"    handle @{matcher}_static {{",
                    f"        reverse_proxy 127.0.0.1:{port}",
                    "    }",
                    f"    @{matcher}_api {{",
                    f"        path {' '.join(api_paths)}",
                    "        method GET POST PUT PATCH DELETE OPTIONS",
                    "    }",
                    f"    handle @{matcher}_api {{",
                    "        request_body {",
                    "            max_size 64KB",
                    "        }",
                    f"        reverse_proxy 127.0.0.1:{port}",
                    "    }",
                    f"    @{matcher}_api_token {{",
                    "        method GET DELETE OPTIONS",
                    f"        path_regexp ^{re.escape(path)}/api/v1/(?:search|basket/items)/[A-Za-z0-9_-]{{16,128}}$",
                    "    }",
                    f"    handle @{matcher}_api_token {{",
                    "        request_body {",
                    "            max_size 64KB",
                    "        }",
                    f"        reverse_proxy 127.0.0.1:{port}",
                    "    }",
                )
            )
        else:
            static_paths = (
                "/", "/index.html", "/app.js", "/styles.css",
                "/api-contract.json", "/lib/*", "/assets/*",
            )
            api_paths = (
                "/api/v1/session", "/api/v1/translations", "/api/v1/books",
                "/api/v1/chapters", "/api/v1/scripture", "/api/v1/search",
                "/api/v1/basket",
                "/api/v1/basket/items",
                "/api/v1/basket/order", "/api/v1/preferences",
                "/api/v1/post", "/api/v1/cleanup",
            )
            lines.extend(
                (
                    f"    @{matcher}_static path {' '.join(static_paths)}",
                    f"    handle @{matcher}_static {{",
                    f"        reverse_proxy 127.0.0.1:{port}",
                    "    }",
                    f"    @{matcher}_api {{",
                    f"        path {' '.join(api_paths)}",
                    "        method GET POST PUT PATCH DELETE OPTIONS",
                    "    }",
                    f"    handle @{matcher}_api {{",
                    "        request_body {",
                    "            max_size 64KB",
                    "        }",
                    f"        reverse_proxy 127.0.0.1:{port}",
                    "    }",
                    f"    @{matcher}_api_token {{",
                    "        method GET DELETE OPTIONS",
                    "        path_regexp ^/api/v1/(?:search|basket/items)/[A-Za-z0-9_-]{16,128}$",
                    "    }",
                    f"    handle @{matcher}_api_token {{",
                    "        request_body {",
                    "            max_size 64KB",
                    "        }",
                    f"        reverse_proxy 127.0.0.1:{port}",
                    "    }",
                )
            )
    lines.extend(("    handle {", '        respond "" 404', "    }", "}"))
destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
    rm -f -- "$input"
}

rollback_caddy_transaction() {
    local transaction=${CADDY_TRANSACTION_DIR:-}
    if [[ -z "$transaction" || ! -d "$transaction" ]]; then
        return 0
    fi
    restore_caddy_file "$CADDYFILE" caddyfile "$transaction"
    restore_caddy_file "$CADDY_ROUTES" routes "$transaction"
    if [[ "$CADDY_WAS_ACTIVE" == "active" ]]; then
        caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null
        systemctl reload caddy.service
    else
        systemctl stop caddy.service >/dev/null 2>&1 || true
    fi
    if [[ "$CADDY_WAS_ENABLED" == "enabled" ]]; then
        systemctl enable caddy.service >/dev/null
    else
        systemctl disable caddy.service >/dev/null 2>&1 || true
    fi
    rm -rf --one-file-system -- "$transaction"
    CADDY_TRANSACTION_DIR=""
    CADDY_WAS_ACTIVE=""
    CADDY_WAS_ENABLED=""
}

begin_caddy_transaction() {
    local excluded_instance=${1:-}
    local candidate
    [[ -z "$CADDY_TRANSACTION_DIR" ]] ||
        die "A managed Caddy transaction is already active."
    ensure_caddy_available
    CADDY_TRANSACTION_DIR=$(mktemp -d)
    CADDY_WAS_ACTIVE=$(systemctl is-active caddy.service 2>/dev/null || true)
    CADDY_WAS_ENABLED=$(systemctl is-enabled caddy.service 2>/dev/null || true)
    snapshot_caddy_file "$CADDYFILE" caddyfile "$CADDY_TRANSACTION_DIR"
    snapshot_caddy_file "$CADDY_ROUTES" routes "$CADDY_TRANSACTION_DIR"
    candidate="${CADDY_TRANSACTION_DIR}/routes.candidate"
    if ! (render_caddy_routes "$candidate" "$excluded_instance"); then
        rollback_caddy_transaction
        return 1
    fi
    install -o root -g root -m 0644 "$candidate" "$CADDY_ROUTES"
    if ! (ensure_caddy_import) ||
        ! caddy validate --config "$CADDYFILE" --adapter caddyfile; then
        warn "Managed Caddy configuration validation failed; restoring the previous files."
        rollback_caddy_transaction
        return 1
    fi
    if [[ "$CADDY_WAS_ACTIVE" == "active" ]]; then
        if ! systemctl reload caddy.service; then
            warn "Caddy reload failed; restoring the previous files."
            rollback_caddy_transaction
            return 1
        fi
    elif ! systemctl enable --now caddy.service; then
        warn "Caddy did not start; restoring the previous files."
        rollback_caddy_transaction
        return 1
    fi
}

commit_caddy_transaction() {
    if [[ -z "$CADDY_TRANSACTION_DIR" || ! -d "$CADDY_TRANSACTION_DIR" ]]; then
        return 0
    fi
    rm -rf --one-file-system -- "$CADDY_TRANSACTION_DIR"
    CADDY_TRANSACTION_DIR=""
    CADDY_WAS_ACTIVE=""
    CADDY_WAS_ENABLED=""
}

mini_app_local_url() {
    local app_dir=$1
    local env_file=$2
    "$app_dir/venv/bin/python" - "$env_file" <<'PY'
import sys
from urllib.parse import urlsplit
from dotenv import dotenv_values

values = dotenv_values(sys.argv[1])
public = urlsplit(values["MINI_APP_PUBLIC_URL"])
port = int(values["MINI_APP_PORT"])
listen = values.get("MINI_APP_LISTEN", "127.0.0.1")
if ":" in listen and not listen.startswith("["):
    listen = f"[{listen}]"
path = public.path.rstrip("/")
print(f"http://{listen}:{port}{path}/")
PY
}

probe_mini_app_url() {
    local url=$1
    local body
    body=$(curl --fail --silent --show-error --location --max-time 10 "$url") ||
        return 1
    grep -Fq '<title>getBible.Life</title>' <<<"$body"
}

wait_for_mini_app_url() {
    local url=$1
    local attempts=${2:-45}
    local delay_seconds=${3:-2}
    local index
    for ((index = 1; index <= attempts; index++)); do
        if ((index == attempts)); then
            probe_mini_app_url "$url" && return
        elif probe_mini_app_url "$url" 2>/dev/null; then
            return
        fi
        ((index == attempts)) || sleep "$delay_seconds"
    done
    return 1
}

verify_mini_app_local() {
    local app_dir=$1
    local env_file=$2
    local attempts=${3:-45}
    wait_for_mini_app_url \
        "$(mini_app_local_url "$app_dir" "$env_file")" "$attempts" 1
}

verify_mini_app_public() {
    local app_dir=$1
    local env_file=$2
    local attempts=${3:-45}
    local public_url
    public_url=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PUBLIC_URL")
    public_url="${public_url%/}/"
    wait_for_mini_app_url "$public_url" "$attempts" 2
}

verify_mini_app_instance() {
    local app_dir=$1
    local env_file=$2
    local attempts=${3:-45}
    if [[ "$(dotenv_value "$app_dir" "$env_file" "MINI_APP_ENABLED")" != "true" ]]; then
        return 0
    fi
    verify_mini_app_local "$app_dir" "$env_file" "$attempts" &&
        verify_mini_app_public "$app_dir" "$env_file" "$attempts"
}

verify_managed_caddy_routes() {
    local candidate
    candidate=$(mktemp)
    if ! (render_caddy_routes "$candidate") ||
        ! cmp --silent "$candidate" "$CADDY_ROUTES" ||
        ! caddy validate --config "$CADDYFILE" --adapter caddyfile ||
        ! systemctl is-active --quiet caddy.service ||
        ! systemctl is-enabled --quiet caddy.service; then
        rm -f -- "$candidate"
        return 1
    fi
    rm -f -- "$candidate"
}

validate_telegram_token_live() {
    local python_bin=$1
    local token=$2
    TELEGRAM_API_TOKEN="$token" "$python_bin" - <<'PY'
import json
import os
import urllib.request

token = os.environ["TELEGRAM_API_TOKEN"]
request = urllib.request.Request(
    f"https://api.telegram.org/bot{token}/getMe",
    headers={"User-Agent": "getbible-robot-setup/1"},
)
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
except (OSError, ValueError):
    raise SystemExit("Telegram token verification failed safely.") from None
if payload.get("ok") is not True:
    raise SystemExit("Telegram rejected the token.")
result = payload.get("result", {})
username = result.get("username", "unknown")
print(f"Telegram token accepted for @{username}.")
PY
}

telegram_delivery_status() {
    local app_dir=$1
    local env_file=$2
    "$app_dir/venv/bin/python" - "$env_file" <<'PY'
import json
import os
import sys
import time
import urllib.request
from dotenv import dotenv_values

values = dotenv_values(sys.argv[1])
os.environ.update({key: value for key, value in values.items() if value is not None})
from config import Settings

settings = Settings.from_env(load_environment_file=False)

last_result = None
for attempt in range(5):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.telegram_api_token}/getWebhookInfo",
        headers={"User-Agent": "getbible-robot-doctor/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("ok") is True:
        candidate = payload.get("result")
        if isinstance(candidate, dict):
            last_result = candidate
            actual_url = candidate.get("url", "")
            expected_url = (
                "" if settings.telegram_delivery_mode == "polling"
                else settings.webhook_public_url
            )
            if actual_url == expected_url:
                break
    if attempt < 4:
        time.sleep(1)
else:
    if last_result is None:
        raise SystemExit("Telegram delivery status could not be retrieved safely.")
    if settings.telegram_delivery_mode == "polling":
        raise SystemExit("Telegram still has a webhook while polling mode is configured.")
    raise SystemExit("Telegram webhook URL does not match this instance.")

if settings.telegram_delivery_mode == "polling":
    print("Telegram delivery: polling (no webhook registered)")
else:
    pending = last_result.get("pending_update_count", 0)
    error = last_result.get("last_error_message")
    print(f"Telegram delivery: webhook ({settings.webhook_public_url}, pending={pending})")
    if error:
        print(f"Telegram last webhook error: {error}")
PY
}

delete_telegram_webhook() {
    local app_dir=$1
    local env_file=$2
    "$app_dir/venv/bin/python" - "$env_file" <<'PY'
import json
import os
import sys
import urllib.request
import urllib.parse
from dotenv import dotenv_values

values = dotenv_values(sys.argv[1])
token = values.get("TELEGRAM_API_TOKEN")
if not token:
    raise SystemExit("Telegram token is missing.")
body = urllib.parse.urlencode({"drop_pending_updates": "false"}).encode()
request = urllib.request.Request(
    f"https://api.telegram.org/bot{token}/deleteWebhook",
    data=body,
    headers={"User-Agent": "getbible-robot-uninstall/1"},
)
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
except (OSError, ValueError):
    raise SystemExit("Telegram webhook removal failed safely.") from None
if payload.get("ok") is not True:
    raise SystemExit("Telegram rejected webhook removal.")
PY
}

write_metadata() {
    local instance=$1
    local service_user=$2
    local port=$3
    local sha=$4
    local source_url=$5
    local created_at=$6
    local file
    file=$(metadata_file_for "$instance")
    {
        printf 'INSTANCE=%q\n' "$instance"
        printf 'SERVICE_USER=%q\n' "$service_user"
        printf 'HEALTH_PORT=%q\n' "$port"
        printf 'DEPLOYED_SHA=%q\n' "$sha"
        printf 'SOURCE_URL=%q\n' "$source_url"
        printf 'CREATED_AT=%q\n' "$created_at"
    } >"$file"
    chown root:root "$file"
    chmod 0600 "$file"
}

safe_remove_tree() {
    local path
    path=$(readlink -m "$1")
    [[ "$path" == "${INSTANCE_ROOT}/"* ]] ||
        die "Refusing to remove a path outside ${INSTANCE_ROOT}: ${path}"
    [[ "$path" != "$INSTANCE_ROOT" && "$path" != "/" ]] ||
        die "Refusing to remove a broad path."
    rm -rf --one-file-system -- "$path"
}

install_python_environment() {
    local app_dir=$1
    local python_bin=$2

    info "Creating the isolated Python environment"
    "$python_bin" -m venv "${app_dir}/venv"
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
        "${app_dir}/venv/bin/python" -m pip install --upgrade pip
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
        "${app_dir}/venv/bin/python" -m pip install \
        --require-hashes \
        -r "${app_dir}/requirements.txt"
    "${app_dir}/venv/bin/python" -m pip check
}

secure_application_tree() {
    local app_dir=$1
    local service_user=$2

    [[ -d "$app_dir" ]] || die "Application directory is missing: ${app_dir}"
    id "$service_user" >/dev/null 2>&1 ||
        die "Service account is missing: ${service_user}"

    # The code and virtual environment remain root-owned and immutable to the
    # robot. Only the matching instance group may read and traverse them.
    chown -R "root:${service_user}" "$app_dir"
    chmod -R u=rwX,g=rX,o= "$app_dir"
}

verify_service_account_access() {
    local app_dir=$1
    local service_user=$2

    runuser --user "$service_user" -- /bin/sh -c '
        app_dir=$1
        cd -- "$app_dir"
        exec "$app_dir/venv/bin/python" -c \
            "from config import Settings; assert Settings is not None"
    ' getbible-robot-preflight "$app_dir"
}

verify_content_access() {
    local service_user=$1
    local instance=$2
    runuser --user "$service_user" -- test -r "$(welcome_file_for "$instance")"
    runuser --user "$service_user" -- test -r "$(help_file_for "$instance")"
}

prepare_application() {
    local source_dir=$1
    local source_url=$2
    local sha=$3
    local destination=$4
    local python_bin=$5
    local service_user=$6
    local env_file=${7:-}
    local parent
    local temporary
    parent=$(dirname "$destination")
    install -d -o root -g root -m 0755 "$parent"
    temporary=$(mktemp -d "${parent}/.application.XXXXXX")
    TEMP_PATHS+=("$temporary")

    info "Cloning exact source commit ${sha}"
    git clone --quiet --no-hardlinks --no-checkout "$source_dir" "${temporary}/app"
    git -C "${temporary}/app" checkout --quiet --detach "$sha"
    git -C "${temporary}/app" remote set-url origin "$source_url"
    [[ "$(git -C "${temporary}/app" rev-parse HEAD)" == "$sha" ]] ||
        die "Prepared source commit does not match the requested commit."

    install_python_environment "${temporary}/app" "$python_bin"

    if [[ -n "$env_file" ]]; then
        validate_environment "${temporary}/app" "$env_file"
    fi

    secure_application_tree "${temporary}/app" "$service_user"
    mv -- "${temporary}/app" "$destination"
    rmdir "$temporary"
    TEMP_PATHS=()
    verify_service_account_access "$destination" "$service_user"
}

install_shared_manager() {
    local source_dir=$1
    install -d -o root -g root -m 0755 \
        "$ETC_ROOT" "$METADATA_ROOT" "$INSTANCE_ROOT" "$STATE_ROOT" "$CACHE_ROOT"
    # Execute-only access lets each isolated account open its own known file
    # without listing the directory or reading another instance's mode-0640 log.
    install -d -o root -g root -m 0711 "$LOG_ROOT"
    install -o root -g root -m 0755 "${source_dir}/setup.sh" "$MANAGER_PATH"
    install -o root -g root -m 0644 \
        "${source_dir}/deploy/getbible-robot@.service" \
        "$UNIT_PATH"
    touch "$SETUP_LOG"
    chown root:root "$SETUP_LOG"
    chmod 0640 "$SETUP_LOG"
}

install_log_rotation() {
    cat >"$LOGROTATE_PATH" <<EOF
${LOG_ROOT}/*.jsonl {
    daily
    rotate 3
    size 10M
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su root root
}

${SETUP_LOG} {
    weekly
    rotate 12
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su root root
}
EOF
    chown root:root "$LOGROTATE_PATH"
    chmod 0644 "$LOGROTATE_PATH"
}

wait_for_readiness() {
    local port=$1
    local attempts=30
    local index
    if [[ "$port" == "0" ]]; then
        return
    fi
    for ((index = 1; index <= attempts; index++)); do
        if curl --fail --silent --show-error \
            "http://127.0.0.1:${port}/readyz" >/dev/null 2>&1; then
            return
        fi
        sleep 1
    done
    return 1
}

cmd_install() {
    require_root
    require_tty
    local source_request=""
    local requested_mini_app_port=""
    local requested_mini_app_listen=""
    local requested_webhook_listen=""
    local requested_webhook_port=""
    local requested_health_port=""
    local requested_reverse_proxy_mode=""
    local max_concurrent_lookups=$DEFAULT_MAX_CONCURRENT_LOOKUPS
    local max_concurrent_searches=$DEFAULT_MAX_CONCURRENT_SEARCHES
    local max_concurrent_updates=$DEFAULT_MAX_CONCURRENT_UPDATES
    local memory_high_mb=$DEFAULT_SYSTEMD_MEMORY_HIGH_MB
    local memory_max_mb=$DEFAULT_SYSTEMD_MEMORY_MAX_MB
    local memory_swap_max_mb=$DEFAULT_SYSTEMD_MEMORY_SWAP_MAX_MB
    local tasks_max=$DEFAULT_SYSTEMD_TASKS_MAX
    local nofile_limit=$DEFAULT_SYSTEMD_NOFILE_LIMIT
    local cpu_quota_percent=$DEFAULT_SYSTEMD_CPU_QUOTA_PERCENT
    while (($# > 0)); do
        case "$1" in
            --source)
                (($# >= 2)) || die "--source requires a directory."
                source_request=$2
                shift 2
                ;;
            --mini-app-port)
                (($# >= 2)) || die "--mini-app-port requires a port."
                requested_mini_app_port=$2
                shift 2
                ;;
            --mini-app-listen)
                (($# >= 2)) || die "--mini-app-listen requires an IP address."
                requested_mini_app_listen=$2
                shift 2
                ;;
            --webhook-port)
                (($# >= 2)) || die "--webhook-port requires a port."
                requested_webhook_port=$2
                shift 2
                ;;
            --webhook-listen)
                (($# >= 2)) || die "--webhook-listen requires an IP address."
                requested_webhook_listen=$2
                shift 2
                ;;
            --health-port)
                (($# >= 2)) || die "--health-port requires a port."
                requested_health_port=$2
                shift 2
                ;;
            --reverse-proxy)
                (($# >= 2)) || die "--reverse-proxy requires caddy or external."
                requested_reverse_proxy_mode=${2,,}
                shift 2
                ;;
            --max-concurrent-lookups)
                (($# >= 2)) || die "$1 requires an integer."
                max_concurrent_lookups=$2
                shift 2
                ;;
            --max-concurrent-searches)
                (($# >= 2)) || die "$1 requires an integer."
                max_concurrent_searches=$2
                shift 2
                ;;
            --max-concurrent-updates)
                (($# >= 2)) || die "$1 requires an integer."
                max_concurrent_updates=$2
                shift 2
                ;;
            --memory-high-mb)
                (($# >= 2)) || die "$1 requires an integer."
                memory_high_mb=$2
                shift 2
                ;;
            --memory-max-mb)
                (($# >= 2)) || die "$1 requires an integer."
                memory_max_mb=$2
                shift 2
                ;;
            --memory-swap-max-mb)
                (($# >= 2)) || die "$1 requires an integer."
                memory_swap_max_mb=$2
                shift 2
                ;;
            --tasks-max)
                (($# >= 2)) || die "$1 requires an integer."
                tasks_max=$2
                shift 2
                ;;
            --nofile-limit)
                (($# >= 2)) || die "$1 requires an integer."
                nofile_limit=$2
                shift 2
                ;;
            --cpu-quota-percent)
                (($# >= 2)) || die "$1 requires an integer."
                cpu_quota_percent=$2
                shift 2
                ;;
            *) die "Unknown install option: $1" ;;
        esac
    done

    [[ -z "$requested_reverse_proxy_mode" ]] ||
        validate_reverse_proxy_mode "$requested_reverse_proxy_mode" ||
        die "--reverse-proxy must be caddy or external."
    [[ -z "$requested_mini_app_port" ]] ||
        { validate_port "$requested_mini_app_port" &&
            ((requested_mini_app_port >= 1024)); } ||
        die "--mini-app-port must be 1024-65535."
    [[ -z "$requested_webhook_port" ]] ||
        { validate_port "$requested_webhook_port" &&
            ((requested_webhook_port >= 1024)); } ||
        die "--webhook-port must be 1024-65535."
    [[ -z "$requested_health_port" ]] || validate_port "$requested_health_port" ||
        die "--health-port must be 0-65535."
    validate_bounded_integer "$max_concurrent_lookups" 1 32 ||
        die "--max-concurrent-lookups must be 1-32."
    validate_bounded_integer "$max_concurrent_searches" 1 64 ||
        die "--max-concurrent-searches must be 1-64."
    validate_bounded_integer "$max_concurrent_updates" 1 64 ||
        die "--max-concurrent-updates must be 1-64."
    validate_resource_profile \
        "$memory_high_mb" "$memory_max_mb" "$memory_swap_max_mb" \
        "$tasks_max" "$nofile_limit" "$cpu_quota_percent"

    install_host_prerequisites
    local source_dir
    local source_url
    local sha
    local python_bin
    source_dir=$(resolve_source_dir "$source_request")
    source_url=$(source_url_for "$source_dir")
    sha=$(git_source_read "$source_dir" rev-parse HEAD)
    python_bin=$(select_python)

    printf '\nGetBible Robot secure instance setup\n'
    printf 'Source: %s\n' "$source_dir"
    printf 'Commit: %s\n' "$sha"
    printf 'Python: %s\n\n' "$("$python_bin" --version 2>&1)"
    confirm "Deploy this exact reviewed commit?" yes || die "Installation cancelled."

    local instance
    while true; do
        instance=$(prompt "Instance/user-space name" "production")
        if ! validate_instance_name "$instance"; then
            warn "Use 2-24 lowercase letters, numbers, or single hyphens; start with a letter and end with a letter or number."
            continue
        fi
        instance_exists "$instance" && {
            warn "Instance '${instance}' already exists. Use upgrade or config."
            continue
        }
        break
    done

    local service_user
    service_user=$(service_user_for "$instance")
    if id "$service_user" >/dev/null 2>&1; then
        die "Linux account '${service_user}' already exists but is not managed by this instance."
    fi

    local token
    local token_confirm
    while true; do
        read -r -s -p "Telegram Bot API token: " token
        printf '\n'
        validate_token_shape "$token" || {
            warn "The token does not match Telegram's Bot API token shape."
            continue
        }
        read -r -s -p "Repeat Telegram Bot API token: " token_confirm
        printf '\n'
        [[ "$token" == "$token_confirm" ]] || {
            warn "The two token entries differ."
            continue
        }
        ensure_unique_token "$token" || continue
        break
    done

    local bot_name
    local bot_description
    local bot_short_description
    while true; do
        bot_name=$(prompt "Telegram bot display name" "GetBible Robot")
        validate_plain_text "$bot_name" 64 && break
        warn "The bot display name must contain 1-64 characters and no double quote."
    done
    while true; do
        bot_short_description=$(
            prompt "Telegram bot short description" \
                "Read and search Scripture with GetBible."
        )
        validate_plain_text "$bot_short_description" 120 && break
        warn "The short description must contain 1-120 characters and no double quote."
    done
    while true; do
        bot_description=$(
            prompt "Telegram bot description" \
                "Read and search Scripture in Telegram with GetBible."
        )
        validate_plain_text "$bot_description" 512 && break
        warn "The description must contain 1-512 characters and no double quote."
    done

    local translation
    while true; do
        translation=$(prompt "Default GetBible translation" "kjv")
        translation=${translation,,}
        validate_translation "$translation" && break
        warn "Translation must use 1-30 lowercase letters, numbers, underscores, or hyphens."
    done

    local delivery_mode
    while true; do
        delivery_mode=$(prompt "Telegram delivery mode (polling/webhook)" "polling")
        delivery_mode=${delivery_mode,,}
        validate_delivery_mode "$delivery_mode" && break
        warn "Choose polling or webhook."
    done

    local webhook_public_url=""
    local webhook_ip_address=""
    local webhook_listen="127.0.0.1"
    local webhook_port="9001"
    local webhook_secret=""
    if [[ "$delivery_mode" == "webhook" ]]; then
        printf '\nWebhook mode requires a public HTTPS URL. Telegram connects to that URL;\n'
        printf 'your reverse proxy forwards the URL path to this instance backend.\n'
        while true; do
            webhook_public_url=$(
                prompt "Public HTTPS webhook URL" \
                    "https://bot.example.com/telegram/${instance}"
            )
            validate_webhook_url "$webhook_public_url" && break
            warn "Use a complete HTTPS URL with a private path and no query or fragment."
        done
        webhook_listen=${requested_webhook_listen:-127.0.0.1}
        if [[ -z "$requested_webhook_listen" ]]; then
            webhook_listen=$(
                prompt "Webhook backend bind IP (127.0.0.1 for same-host proxy)" \
                    "$webhook_listen"
            )
        fi
        validate_specific_listener "$python_bin" "$webhook_listen" ||
            die "Webhook listener must be a specific non-link-local IP address; wildcard listeners are forbidden."
        webhook_port=${requested_webhook_port:-$(next_webhook_port)}
        if [[ -z "$requested_webhook_port" ]]; then
            webhook_port=$(prompt "Private webhook backend port" "$webhook_port")
        fi
        validate_port "$webhook_port" && [[ "$webhook_port" != "0" ]] ||
            die "Webhook port must be an integer between 1 and 65535."
        if ss -ltnH 2>/dev/null | awk '{print $4}' |
            grep -Eq "(^|:)$webhook_port$"; then
            die "Webhook port ${webhook_port} is already listening."
        fi
        webhook_ip_address=$(prompt "Optional fixed public IP for Telegram" "")
        webhook_secret=$(generate_webhook_secret "$python_bin")
    elif [[ -n "$requested_webhook_port" || -n "$requested_webhook_listen" ]]; then
        die "Webhook listener options require selecting webhook delivery."
    fi

    local mini_app_enabled="false"
    local mini_app_public_url=""
    local mini_app_port="9201"
    local mini_app_listen="127.0.0.1"
    local reverse_proxy_mode="caddy"
    if confirm "Enable the authenticated Telegram Mini App?" yes; then
        mini_app_enabled="true"
        printf '\nThe Mini App needs a public HTTPS URL whose DNS already points to this host.\n'
        if [[ -n "$requested_reverse_proxy_mode" ]]; then
            reverse_proxy_mode=$requested_reverse_proxy_mode
        elif confirm "Use an existing external reverse proxy?" no; then
            reverse_proxy_mode="external"
        fi
        if [[ "$reverse_proxy_mode" == "caddy" ]]; then
            printf 'The manager configures Caddy automatic HTTPS and keeps the app on loopback.\n'
        else
            printf 'Your reverse proxy terminates public HTTPS and forwards this URL to one Mini App port.\n'
        fi
        while true; do
            mini_app_public_url=$(
                prompt "Public HTTPS Mini App URL" \
                    "https://bot.example.com/getbible/${instance}"
            )
            validate_mini_app_url "$mini_app_public_url" && break
            warn "Use a complete HTTPS URL without credentials, query, or fragment."
        done
        preflight_mini_app_dns "$python_bin" "$mini_app_public_url"
        mini_app_port=${requested_mini_app_port:-$(next_mini_app_port)}
        if [[ "$reverse_proxy_mode" == "external" &&
            -z "$requested_mini_app_port" ]]; then
            mini_app_port=$(prompt "Private Mini App backend port" "$mini_app_port")
        fi
        validate_port "$mini_app_port" && ((mini_app_port >= 1024)) ||
            die "Mini App port must be an integer between 1024 and 65535."
        if ss -ltnH 2>/dev/null | awk '{print $4}' |
            grep -Eq "(^|:)$mini_app_port$"; then
            die "Mini App port ${mini_app_port} is already listening."
        fi
        [[ "$mini_app_port" != "$webhook_port" || "$delivery_mode" != "webhook" ]] ||
            die "The Mini App and webhook listeners require different ports."
        if [[ "$reverse_proxy_mode" == "external" ]]; then
            mini_app_listen=${requested_mini_app_listen:-127.0.0.1}
            if [[ -z "$requested_mini_app_listen" ]]; then
                mini_app_listen=$(
                    prompt "Mini App listen address (127.0.0.1 for a same-host proxy)" \
                        "$mini_app_listen"
                )
            fi
            validate_proxy_listener "$python_bin" "$mini_app_listen" ||
                die "Mini App listen address must be a valid non-link-local IP address."
        elif [[ -n "$requested_mini_app_listen" ]]; then
            die "--mini-app-listen requires --reverse-proxy external."
        fi
    elif [[ -n "$requested_mini_app_port" ||
        -n "$requested_mini_app_listen" ||
        -n "$requested_reverse_proxy_mode" ]]; then
        die "Mini App proxy options require enabling the Mini App."
    fi

    local suggested_port
    local health_port
    suggested_port=$(next_health_port)
    while true; do
        if [[ -n "$requested_health_port" ]]; then
            health_port=$requested_health_port
        else
            health_port=$(prompt "Loopback health/metrics port (0 disables)" "$suggested_port")
        fi
        validate_port "$health_port" || {
            warn "Port must be an integer between 0 and 65535."
            continue
        }
        if [[ "$health_port" != "0" ]] &&
            ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:)$health_port$"; then
            [[ -z "$requested_health_port" ]] ||
                die "Health port ${health_port} is already listening."
            warn "Port ${health_port} is already listening."
            continue
        fi
        if [[ "$mini_app_enabled" == "true" && "$health_port" == "$mini_app_port" ]]; then
            [[ -z "$requested_health_port" ]] ||
                die "The health and Mini App listeners require different ports."
            warn "The health and Mini App listeners require different ports."
            continue
        fi
        if [[ "$delivery_mode" == "webhook" && "$health_port" != "0" &&
            "$health_port" == "$webhook_port" ]]; then
            [[ -z "$requested_health_port" ]] ||
                die "The health and webhook listeners require different ports."
            warn "The health and webhook listeners require different ports."
            continue
        fi
        break
    done

    local audit_mode="metadata"
    warn "Metadata audit logs record actions, translations, filter modes, counts, and outcomes without Telegram message text."
    if confirm "Also log exact Scripture references and search terms? This stores user-provided content on disk." no; then
        audit_mode="content"
    fi

    local delete_commands="false"
    if confirm "Attempt to delete handled Telegram command messages?" no; then
        delete_commands="true"
    fi

    local start_now="yes"
    if [[ "$delivery_mode" == "webhook" ]]; then
        if ! confirm "Is the configured public HTTPS webhook route ready?" no; then
            warn "The instance will be installed but not started. Configure HTTPS, then run '${PROGRAM} start ${instance}'."
            start_now="no"
        fi
    fi
    if [[ "$mini_app_enabled" == "true" && "$reverse_proxy_mode" == "external" ]]; then
        printf 'Configure the external reverse proxy to forward %s to http://%s:%s before starting.\n' \
            "$mini_app_public_url" "$mini_app_listen" "$mini_app_port"
        if ! confirm "Is the external Mini App HTTPS route ready?" no; then
            warn "The instance will be installed but not started. Configure the reverse proxy, then run '${PROGRAM} start ${instance}'."
            start_now="no"
        fi
    fi
    if [[ "$start_now" == "yes" ]]; then
        confirm "Enable and start this instance after validation?" yes || start_now="no"
    fi

    printf '\nPlanned isolated instance:\n'
    printf '  Instance:       %s\n' "$instance"
    printf '  Linux account:  %s (system, locked, no login shell)\n' "$service_user"
    printf '  Application:    %s/%s/app\n' "$INSTANCE_ROOT" "$instance"
    printf '  Configuration:  %s/%s.env (root-only)\n' "$ETC_ROOT" "$instance"
    printf '  Cache:          %s/%s\n' "$CACHE_ROOT" "$instance"
    printf '  State/home:     %s/%s\n' "$STATE_ROOT" "$instance"
    printf '  JSON log:       %s/%s.jsonl\n' "$LOG_ROOT" "$instance"
    printf '  Audit mode:     %s\n' "$audit_mode"
    printf '  Delivery:       %s\n' "$delivery_mode"
    if [[ "$delivery_mode" == "webhook" ]]; then
        printf '  Public webhook: %s\n' "$webhook_public_url"
        printf '  Proxy target:   http://%s:%s%s\n' \
            "$webhook_listen" "$webhook_port" \
            "$(printf '%s' "$webhook_public_url" | sed -E 's#^https://[^/]+##')"
    fi
    printf '  Mini App:       %s\n' "$mini_app_enabled"
    if [[ "$mini_app_enabled" == "true" ]]; then
        printf '  Mini App URL:   %s\n' "$mini_app_public_url"
        printf '  Proxy mode:     %s\n' "$reverse_proxy_mode"
        printf '  Proxy backend:  http://%s:%s\n' \
            "$mini_app_listen" "$mini_app_port"
    fi
    printf '  Worker profile: lookups=%s searches=%s updates=%s\n' \
        "$max_concurrent_lookups" "$max_concurrent_searches" \
        "$max_concurrent_updates"
    printf '  Service limits: high=%sMiB max=%sMiB swap=%sMiB tasks=%s nofile=%s CPU=%s%%\n' \
        "$memory_high_mb" "$memory_max_mb" "$memory_swap_max_mb" \
        "$tasks_max" "$nofile_limit" "$cpu_quota_percent"
    printf '  Health:         127.0.0.1:%s\n\n' "$health_port"
    confirm "Create this instance?" yes || die "Installation cancelled."

    if [[ "$mini_app_enabled" == "true" && "$reverse_proxy_mode" == "caddy" ]]; then
        ensure_caddy_available
    fi
    install_shared_manager "$source_dir"
    install_log_rotation

    local state_dir="${STATE_ROOT}/${instance}"
    local cache_dir="${CACHE_ROOT}/${instance}"
    local app_parent="${INSTANCE_ROOT}/${instance}"
    local app_dir="${app_parent}/app"
    local env_file
    local log_file
    local welcome_file
    local help_file
    local created_at
    local nologin_shell
    env_file=$(environment_file_for "$instance")
    log_file=$(log_file_for "$instance")
    welcome_file=$(welcome_file_for "$instance")
    help_file=$(help_file_for "$instance")
    created_at=$(date --utc +'%Y-%m-%dT%H:%M:%SZ')
    nologin_shell=$(command -v nologin || true)
    [[ -n "$nologin_shell" ]] || die "No nologin shell is installed."

    INSTALLING_INSTANCE=$instance
    INSTALLING_USER=$service_user
    INSTALL_TRANSACTION=1
    useradd \
        --system \
        --user-group \
        --home-dir "$state_dir" \
        --create-home \
        --shell "$nologin_shell" \
        "$service_user"
    passwd --lock "$service_user" >/dev/null 2>&1 || true
    install -d -o "$service_user" -g "$service_user" -m 0700 \
        "$state_dir" "$cache_dir"
    install -d -o root -g root -m 0755 "$app_parent"
    install -o "$service_user" -g "$service_user" -m 0640 /dev/null "$log_file"
    install -o root -g "$service_user" -m 0640 \
        "${source_dir}/deploy/welcome.txt" "$welcome_file"
    install -o root -g "$service_user" -m 0640 \
        "${source_dir}/deploy/help.txt" "$help_file"

    install -o root -g root -m 0600 "${source_dir}/.env.template" "$env_file"
    replace_env_value "$python_bin" "$env_file" "TELEGRAM_API_TOKEN" "$token"
    replace_env_value "$python_bin" "$env_file" "TELEGRAM_DELIVERY_MODE" "$delivery_mode"
    replace_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_PUBLIC_URL" "$webhook_public_url"
    replace_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_LISTEN" "$webhook_listen"
    replace_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_PORT" "$webhook_port"
    replace_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_SECRET_TOKEN" "$webhook_secret"
    replace_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_IP_ADDRESS" "$webhook_ip_address"
    replace_env_value "$python_bin" "$env_file" "BOT_NAME" "$bot_name"
    replace_env_value "$python_bin" "$env_file" "BOT_DESCRIPTION" "$bot_description"
    replace_env_value "$python_bin" "$env_file" "BOT_SHORT_DESCRIPTION" "$bot_short_description"
    replace_env_value "$python_bin" "$env_file" "MINI_APP_ENABLED" "$mini_app_enabled"
    replace_env_value "$python_bin" "$env_file" "REVERSE_PROXY_MODE" "$reverse_proxy_mode"
    replace_env_value "$python_bin" "$env_file" "MINI_APP_PUBLIC_URL" "$mini_app_public_url"
    replace_env_value "$python_bin" "$env_file" "MINI_APP_LISTEN" "$mini_app_listen"
    replace_env_value "$python_bin" "$env_file" "MINI_APP_PORT" "$mini_app_port"
    replace_env_value "$python_bin" "$env_file" "MINI_APP_TRUSTED_PROXY_CIDRS" ""
    replace_env_value "$python_bin" "$env_file" \
        "MAX_CONCURRENT_LOOKUPS" "$max_concurrent_lookups"
    replace_env_value "$python_bin" "$env_file" \
        "MAX_CONCURRENT_SEARCHES" "$max_concurrent_searches"
    replace_env_value "$python_bin" "$env_file" \
        "MAX_CONCURRENT_UPDATES" "$max_concurrent_updates"
    replace_env_value "$python_bin" "$env_file" \
        "SYSTEMD_MEMORY_HIGH_MB" "$memory_high_mb"
    replace_env_value "$python_bin" "$env_file" \
        "SYSTEMD_MEMORY_MAX_MB" "$memory_max_mb"
    replace_env_value "$python_bin" "$env_file" \
        "SYSTEMD_MEMORY_SWAP_MAX_MB" "$memory_swap_max_mb"
    replace_env_value "$python_bin" "$env_file" "SYSTEMD_TASKS_MAX" "$tasks_max"
    replace_env_value "$python_bin" "$env_file" \
        "SYSTEMD_NOFILE_LIMIT" "$nofile_limit"
    replace_env_value "$python_bin" "$env_file" \
        "SYSTEMD_CPU_QUOTA_PERCENT" "$cpu_quota_percent"
    replace_env_value "$python_bin" "$env_file" "TRANSLATION" "$translation"
    replace_env_value "$python_bin" "$env_file" \
        "USER_PREFERENCES_FILE" "${state_dir}/preferences.sqlite3"
    replace_env_value "$python_bin" "$env_file" "USER_PREFERENCE_LIMIT" "10000"
    replace_env_value "$python_bin" "$env_file" "WELCOME_MESSAGE_FILE" "$welcome_file"
    replace_env_value "$python_bin" "$env_file" "HELP_MESSAGE_FILE" "$help_file"
    replace_env_value "$python_bin" "$env_file" "HEALTH_PORT" "$health_port"
    replace_env_value "$python_bin" "$env_file" "DELETE_COMMAND_MESSAGES" "$delete_commands"
    replace_env_value "$python_bin" "$env_file" "INSTANCE_NAME" "$instance"
    replace_env_value "$python_bin" "$env_file" "LOG_FILE" "$log_file"
    replace_env_value "$python_bin" "$env_file" "AUDIT_LOG_MODE" "$audit_mode"

    prepare_application \
        "$source_dir" "$source_url" "$sha" "$app_dir" "$python_bin" \
        "$service_user" "$env_file"
    verify_content_access "$service_user" "$instance"
    write_metadata "$instance" "$service_user" "$health_port" "$sha" "$source_url" "$created_at"
    write_resource_dropin \
        "$instance" "$memory_high_mb" "$memory_max_mb" \
        "$memory_swap_max_mb" "$tasks_max" "$nofile_limit" \
        "$cpu_quota_percent"

    if confirm "Verify this token with Telegram now?" yes; then
        validate_telegram_token_live "$app_dir/venv/bin/python" "$token"
    fi
    unset token token_confirm

    systemctl daemon-reload
    systemd-analyze verify "$(service_name_for "$instance")"
    INSTALL_TRANSACTION=0

    if [[ "$start_now" == "yes" ]]; then
        local service
        service=$(service_name_for "$instance")
        if [[ "$mini_app_enabled" == "true" && "$reverse_proxy_mode" == "caddy" ]]; then
            begin_caddy_transaction ||
                die "The managed Caddy route could not be applied."
        fi
        systemctl enable --now "$service"
        if ! wait_for_readiness "$health_port"; then
            systemctl status "$service" --no-pager || true
            tail -n 100 "$log_file" || true
            record_operation install "$instance" failed-readiness
            die "The instance started but did not become ready. Run '${PROGRAM} doctor ${instance}'."
        fi
        telegram_delivery_status "$app_dir" "$env_file" ||
            die "The instance started, but Telegram delivery validation failed."
        if [[ "$mini_app_enabled" == "true" ]] &&
            ! verify_mini_app_instance "$app_dir" "$env_file"; then
            record_operation install "$instance" failed-miniapp-https
            die "The Mini App failed local or public HTTPS verification; the prior Caddy configuration will be restored."
        fi
        commit_caddy_transaction
    fi

    record_operation install "$instance" ok
    printf '\nInstance %s installed successfully.\n' "$instance"
    printf 'Manage it with:\n'
    printf '  sudo %s status %s\n' "$PROGRAM" "$instance"
    printf '  sudo %s logs %s\n' "$PROGRAM" "$instance"
    printf '  sudo %s doctor %s\n' "$PROGRAM" "$instance"
    if [[ "$mini_app_enabled" == "true" ]]; then
        printf '  sudo %s miniapp %s\n' "$PROGRAM" "$instance"
        printf '\nOne Telegram-side step remains:\n'
        printf '  Set this exact Main Mini App URL in @BotFather: %s\n' \
            "$mini_app_public_url"
    fi
}

cmd_list() {
    require_root
    local names=()
    local instance
    local service
    local state
    mapfile -t names < <(instance_names | sort)
    if ((${#names[@]} == 0)); then
        printf 'No managed GetBible Robot instances are installed.\n'
        return
    fi
    printf '%-24s %-28s %-9s %-6s %-12s\n' \
        "INSTANCE" "SERVICE USER" "STATE" "PORT" "COMMIT"
    for instance in "${names[@]}"; do
        load_instance "$instance"
        service=$(service_name_for "$instance")
        state=$(systemctl is-active "$service" 2>/dev/null || true)
        printf '%-24s %-28s %-9s %-6s %-12s\n' \
            "$instance" "$ACTIVE_USER" "${state:-unknown}" "$ACTIVE_PORT" "${ACTIVE_SHA:0:12}"
    done
}

cmd_start() {
    require_root
    select_instance "${1:-}"
    local service
    local app_dir
    local env_file
    local mini_app_enabled
    local reverse_proxy_mode
    local public_url
    local was_active
    local was_enabled
    service=$(service_name_for "$ACTIVE_INSTANCE")
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")
    mini_app_enabled=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_ENABLED")
    reverse_proxy_mode=$(dotenv_value "$app_dir" "$env_file" "REVERSE_PROXY_MODE")
    reverse_proxy_mode=${reverse_proxy_mode:-caddy}
    was_active=$(systemctl is-active "$service" 2>/dev/null || true)
    was_enabled=$(systemctl is-enabled "$service" 2>/dev/null || true)
    if [[ "$mini_app_enabled" == "true" ]]; then
        public_url=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PUBLIC_URL")
        preflight_mini_app_dns "$app_dir/venv/bin/python" "$public_url"
        if [[ "$reverse_proxy_mode" == "caddy" ]]; then
            begin_caddy_transaction ||
                die "The managed Caddy route could not be applied."
        fi
    fi
    if ! systemctl enable --now "$service" ||
        ! wait_for_readiness "$ACTIVE_PORT" ||
        ! verify_mini_app_instance "$app_dir" "$env_file"; then
        rollback_caddy_transaction
        if [[ "$was_active" == "active" ]]; then
            systemctl start "$service" >/dev/null 2>&1 || true
        else
            systemctl stop "$service" >/dev/null 2>&1 || true
        fi
        if [[ "$was_enabled" == "enabled" ]]; then
            systemctl enable "$service" >/dev/null 2>&1 || true
        else
            systemctl disable "$service" >/dev/null 2>&1 || true
        fi
        die "Service start, readiness, or Mini App HTTPS verification failed."
    fi
    commit_caddy_transaction
    record_operation start "$ACTIVE_INSTANCE" ok
    cmd_status "$ACTIVE_INSTANCE"
}

cmd_stop() {
    require_root
    select_instance "${1:-}"
    systemctl stop "$(service_name_for "$ACTIVE_INSTANCE")"
    record_operation stop "$ACTIVE_INSTANCE" ok
    cmd_status "$ACTIVE_INSTANCE"
}

cmd_restart() {
    require_root
    select_instance "${1:-}"
    local service
    local app_dir
    local env_file
    service=$(service_name_for "$ACTIVE_INSTANCE")
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")
    systemctl restart "$service"
    wait_for_readiness "$ACTIVE_PORT" ||
        die "Service restarted but readiness did not succeed."
    verify_mini_app_instance "$app_dir" "$env_file" ||
        die "Service restarted, but Mini App HTTPS verification failed."
    record_operation restart "$ACTIVE_INSTANCE" ok
    cmd_status "$ACTIVE_INSTANCE"
}

cmd_status() {
    require_root
    select_instance "${1:-}"
    local service
    local app_dir
    local log_file
    local env_file
    local delivery_mode
    local webhook_public_url
    local webhook_listen
    local webhook_port
    local mini_app_public_url
    local mini_app_listen
    local mini_app_port
    local reverse_proxy_mode
    local active
    local enabled
    service=$(service_name_for "$ACTIVE_INSTANCE")
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    log_file=$(log_file_for "$ACTIVE_INSTANCE")
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")
    active=$(systemctl is-active "$service" 2>/dev/null || true)
    enabled=$(systemctl is-enabled "$service" 2>/dev/null || true)
    printf 'Instance:      %s\n' "$ACTIVE_INSTANCE"
    printf 'Service:       %s (%s, %s)\n' "$service" "${active:-unknown}" "${enabled:-unknown}"
    printf 'Linux account: %s\n' "$ACTIVE_USER"
    printf 'Commit:        %s\n' "$ACTIVE_SHA"
    if [[ -x "$app_dir/venv/bin/python" ]]; then
        printf 'Python:        %s\n' "$("$app_dir/venv/bin/python" --version 2>&1)"
    else
        printf 'Python:        unavailable\n'
    fi
    printf 'Health port:   %s\n' "$ACTIVE_PORT"
    if [[ -x "$app_dir/venv/bin/python" && -f "$env_file" ]]; then
        delivery_mode=$(dotenv_value "$app_dir" "$env_file" "TELEGRAM_DELIVERY_MODE")
        printf 'Delivery:      %s\n' "${delivery_mode:-polling}"
        if [[ "$delivery_mode" == "webhook" ]]; then
            webhook_public_url=$(
                dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_PUBLIC_URL"
            )
            webhook_port=$(
                dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_PORT"
            )
            webhook_listen=$(
                dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_LISTEN"
            )
            printf 'Webhook URL:   %s\n' "$webhook_public_url"
            printf 'Webhook local: %s:%s\n' "$webhook_listen" "$webhook_port"
        fi
        mini_app_enabled=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_ENABLED")
        printf 'Mini App:      %s\n' "${mini_app_enabled:-false}"
        if [[ "$mini_app_enabled" == "true" ]]; then
            reverse_proxy_mode=$(dotenv_value "$app_dir" "$env_file" "REVERSE_PROXY_MODE")
            reverse_proxy_mode=${reverse_proxy_mode:-caddy}
            mini_app_public_url=$(
                dotenv_value "$app_dir" "$env_file" "MINI_APP_PUBLIC_URL"
            )
            mini_app_listen=$(
                dotenv_value "$app_dir" "$env_file" "MINI_APP_LISTEN"
            )
            mini_app_port=$(
                dotenv_value "$app_dir" "$env_file" "MINI_APP_PORT"
            )
            printf 'Mini App URL:  %s\n' "$mini_app_public_url"
            printf 'Proxy mode:    %s\n' "$reverse_proxy_mode"
            printf 'Proxy backend: %s:%s\n' "$mini_app_listen" "$mini_app_port"
        fi
    fi
    printf 'JSON log:      %s\n' "$log_file"
    printf 'Created:       %s\n' "$ACTIVE_CREATED_AT"
    if [[ "$ACTIVE_PORT" != "0" ]]; then
        if curl --fail --silent "http://127.0.0.1:${ACTIVE_PORT}/readyz" >/dev/null 2>&1; then
            printf 'Readiness:     ready\n'
        else
            printf 'Readiness:     unavailable\n'
        fi
    else
        printf 'Readiness:     disabled\n'
    fi
}

cmd_runtime() {
    require_root
    select_instance "${1:-}"
    local service
    local app_dir
    service=$(service_name_for "$ACTIVE_INSTANCE")
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    cmd_status "$ACTIVE_INSTANCE"
    printf '\nDependency check:\n'
    "$app_dir/venv/bin/python" -m pip check
    printf '\nSystemd runtime:\n'
    systemctl show "$service" \
        -p ActiveState \
        -p SubState \
        -p MainPID \
        -p NRestarts \
        -p MemoryCurrent \
        -p MemoryPeak \
        -p MemoryHigh \
        -p MemoryMax \
        -p MemorySwapCurrent \
        -p MemorySwapMax \
        -p TasksCurrent \
        -p TasksMax
    if [[ "$ACTIVE_PORT" != "0" ]]; then
        printf '\nAggregate metrics (no message or token content):\n'
        curl --fail --silent --show-error \
            "http://127.0.0.1:${ACTIVE_PORT}/metrics" || true
        printf '\n'
    fi
}

cmd_logs() {
    require_root
    local requested=${1:-}
    local lines=${2:-200}
    [[ "$lines" =~ ^[1-9][0-9]{0,4}$ ]] ||
        die "Log line count must be between 1 and 99999."
    select_instance "$requested"
    tail -n "$lines" "$(log_file_for "$ACTIVE_INSTANCE")"
}

cmd_follow() {
    require_root
    select_instance "${1:-}"
    tail -n 100 -F "$(log_file_for "$ACTIVE_INSTANCE")"
}

cmd_doctor() {
    require_root
    select_instance "${1:-}"
    local failures=0
    local service
    local app_dir
    local env_file
    local log_file
    local welcome_file
    local help_file
    local content_file
    local mini_app_enabled
    local mini_app_listen
    local mini_app_port
    local reverse_proxy_mode
    service=$(service_name_for "$ACTIVE_INSTANCE")
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")
    log_file=$(log_file_for "$ACTIVE_INSTANCE")
    welcome_file=$(welcome_file_for "$ACTIVE_INSTANCE")
    help_file=$(help_file_for "$ACTIVE_INSTANCE")

    printf 'Running diagnostics for %s...\n' "$ACTIVE_INSTANCE"
    id "$ACTIVE_USER" >/dev/null 2>&1 || {
        warn "Missing service account ${ACTIVE_USER}."
        ((failures += 1))
    }
    [[ -d "$app_dir" && -x "$app_dir/venv/bin/python" ]] || {
        warn "Application or virtual environment is missing."
        ((failures += 1))
    }
    [[ -f "$env_file" ]] || {
        warn "Environment file is missing."
        ((failures += 1))
    }
    [[ "$(stat -c '%U:%G:%a' "$env_file" 2>/dev/null || true)" == "root:root:600" ]] || {
        warn "Environment file must be root:root mode 0600."
        ((failures += 1))
    }
    [[ "$(stat -c '%U:%G:%a' "$log_file" 2>/dev/null || true)" == "${ACTIVE_USER}:${ACTIVE_USER}:640" ]] || {
        warn "JSON log must be owned by ${ACTIVE_USER}:${ACTIVE_USER} with mode 0640."
        ((failures += 1))
    }
    for content_file in "$welcome_file" "$help_file"; do
        [[ "$(stat -c '%U:%G:%a' "$content_file" 2>/dev/null || true)" == "root:${ACTIVE_USER}:640" ]] || {
            warn "Content file must be root:${ACTIVE_USER} mode 0640: ${content_file}"
            ((failures += 1))
        }
    done
    verify_content_access "$ACTIVE_USER" "$ACTIVE_INSTANCE" || {
        warn "The service account cannot read the welcome/help content files."
        ((failures += 1))
    }
    if [[ -x "$app_dir/venv/bin/python" && -f "$env_file" ]]; then
        verify_service_account_access "$app_dir" "$ACTIVE_USER" || {
            warn "The service account cannot enter or read the application directory."
            warn "Run '${PROGRAM} repair ${ACTIVE_INSTANCE}' from the current reviewed checkout."
            ((failures += 1))
        }
        validate_environment "$app_dir" "$env_file" || {
            warn "Configuration validation failed."
            ((failures += 1))
        }
        "$app_dir/venv/bin/python" -m pip check || ((failures += 1))
        local actual_sha
        actual_sha=$(git -C "$app_dir" rev-parse HEAD 2>/dev/null || true)
        [[ "$actual_sha" == "$ACTIVE_SHA" ]] || {
            warn "Application commit does not match deployment metadata."
            ((failures += 1))
        }
    fi
    systemd-analyze verify "$service" || ((failures += 1))
    systemctl status "$service" --no-pager || ((failures += 1))
    if systemctl is-active --quiet "$service" && [[ "$ACTIVE_PORT" != "0" ]]; then
        curl --fail --silent --show-error \
            "http://127.0.0.1:${ACTIVE_PORT}/healthz" >/dev/null ||
            ((failures += 1))
        curl --fail --silent --show-error \
            "http://127.0.0.1:${ACTIVE_PORT}/readyz" >/dev/null ||
            ((failures += 1))
    fi
    if systemctl is-active --quiet "$service" &&
        [[ -x "$app_dir/venv/bin/python" && -f "$env_file" ]]; then
        telegram_delivery_status "$app_dir" "$env_file" || {
            warn "Telegram delivery mode does not match the registered webhook state."
            ((failures += 1))
        }
        mini_app_enabled=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_ENABLED")
        if [[ "$mini_app_enabled" == "true" ]]; then
            mini_app_listen=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_LISTEN")
            mini_app_port=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PORT")
            reverse_proxy_mode=$(dotenv_value "$app_dir" "$env_file" "REVERSE_PROXY_MODE")
            reverse_proxy_mode=${reverse_proxy_mode:-caddy}
            if ! ss -ltnH 2>/dev/null | awk '{print $4}' |
                grep -Fq "${mini_app_listen}:${mini_app_port}"; then
                warn "The configured Mini App backend listener is not active."
                ((failures += 1))
            fi
            if [[ "$reverse_proxy_mode" == "caddy" ]]; then
                [[ "$mini_app_listen" == "127.0.0.1" ]] || {
                    warn "Managed Caddy mode requires the Mini App on IPv4 loopback."
                    ((failures += 1))
                }
                verify_managed_caddy_routes || {
                    warn "The setup-managed Caddy configuration is missing, stale, invalid, inactive, or disabled."
                    ((failures += 1))
                }
            fi
            verify_mini_app_local "$app_dir" "$env_file" 1 || {
                warn "The local Mini App shell did not pass its content check."
                ((failures += 1))
            }
            verify_mini_app_public "$app_dir" "$env_file" 1 || {
                warn "The public Mini App HTTPS route, certificate, or content check failed."
                ((failures += 1))
            }
        fi
    fi
    if ((failures > 0)); then
        record_operation doctor "$ACTIVE_INSTANCE" "failed-${failures}"
        die "Diagnostics found ${failures} problem(s)."
    fi
    record_operation doctor "$ACTIVE_INSTANCE" ok
    printf 'All deployment diagnostics passed.\n'
}

cmd_repair() {
    require_root
    select_instance "${1:-}"
    local service
    local app_dir
    local candidate
    local enabled
    service=$(service_name_for "$ACTIVE_INSTANCE")
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")

    systemctl stop "$service" 2>/dev/null || true
    for candidate in \
        "$app_dir" \
        "${INSTANCE_ROOT}/${ACTIVE_INSTANCE}/app.previous"; do
        if [[ -d "$candidate" ]]; then
            secure_application_tree "$candidate" "$ACTIVE_USER"
        fi
    done
    verify_service_account_access "$app_dir" "$ACTIVE_USER"
    systemctl reset-failed "$service" 2>/dev/null || true

    enabled=$(systemctl is-enabled "$service" 2>/dev/null || true)
    if [[ "$enabled" == "enabled" ]]; then
        systemctl start "$service"
        wait_for_readiness "$ACTIVE_PORT" ||
            die "Permissions were repaired, but the service did not become ready."
    fi

    record_operation repair "$ACTIVE_INSTANCE" ok
    printf 'Application access repaired for %s.\n' "$ACTIVE_INSTANCE"
    cmd_status "$ACTIVE_INSTANCE"
}

cmd_delivery() {
    require_root
    require_tty
    select_instance "${1:-}"
    local env_file
    local app_dir
    local backup
    local current_mode
    local requested_mode
    local webhook_public_url
    local webhook_listen
    local webhook_port
    local webhook_ip_address
    local webhook_secret
    local service
    local was_active
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    service=$(service_name_for "$ACTIVE_INSTANCE")
    current_mode=$(dotenv_value "$app_dir" "$env_file" "TELEGRAM_DELIVERY_MODE")
    current_mode=${current_mode:-polling}

    while true; do
        requested_mode=$(
            prompt "Telegram delivery mode (polling/webhook)" "$current_mode"
        )
        requested_mode=${requested_mode,,}
        validate_delivery_mode "$requested_mode" && break
        warn "Choose polling or webhook."
    done

    backup=$(mktemp "${ETC_ROOT}/.${ACTIVE_INSTANCE}.env.XXXXXX")
    cp -a "$env_file" "$backup"
    if [[ "$requested_mode" == "webhook" ]]; then
        webhook_public_url=$(
            dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_PUBLIC_URL"
        )
        webhook_public_url=$(
            prompt "Public HTTPS webhook URL" \
                "${webhook_public_url:-https://bot.example.com/telegram/${ACTIVE_INSTANCE}}"
        )
        validate_webhook_url "$webhook_public_url" || {
            rm -f "$backup"
            die "Use a complete HTTPS URL with a private path and no query or fragment."
        }
        webhook_listen=$(
            dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_LISTEN"
        )
        webhook_listen=$(
            prompt "Webhook backend bind IP (127.0.0.1 for same-host proxy)" \
                "${webhook_listen:-127.0.0.1}"
        )
        validate_specific_listener "$app_dir/venv/bin/python" "$webhook_listen" || {
            rm -f "$backup"
            die "Webhook listener must be a specific non-link-local IP address; wildcard listeners are forbidden."
        }
        webhook_port=$(
            dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_PORT"
        )
        if [[ -z "$webhook_port" || "$webhook_port" == "0" ]]; then
            webhook_port=$(next_webhook_port)
        fi
        webhook_port=$(prompt "Private webhook backend port" "$webhook_port")
        validate_port "$webhook_port" && [[ "$webhook_port" != "0" ]] || {
            rm -f "$backup"
            die "Webhook port must be an integer between 1 and 65535."
        }
        if [[ "$(dotenv_value "$app_dir" "$env_file" "MINI_APP_ENABLED")" == "true" &&
            "$webhook_port" == "$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PORT")" ]]; then
            rm -f "$backup"
            die "The webhook and Mini App listeners require different ports."
        fi
        webhook_ip_address=$(
            dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_IP_ADDRESS"
        )
        webhook_ip_address=$(
            prompt "Optional fixed public IP for Telegram" "$webhook_ip_address"
        )
        webhook_secret=$(
            dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_SECRET_TOKEN"
        )
        if [[ -z "$webhook_secret" ]] ||
            confirm "Rotate the webhook verification secret?" no; then
            webhook_secret=$(generate_webhook_secret "$app_dir/venv/bin/python")
        fi
        replace_env_value "$app_dir/venv/bin/python" "$env_file" \
            "TELEGRAM_WEBHOOK_PUBLIC_URL" "$webhook_public_url"
        replace_env_value "$app_dir/venv/bin/python" "$env_file" \
            "TELEGRAM_WEBHOOK_LISTEN" "$webhook_listen"
        replace_env_value "$app_dir/venv/bin/python" "$env_file" \
            "TELEGRAM_WEBHOOK_PORT" "$webhook_port"
        replace_env_value "$app_dir/venv/bin/python" "$env_file" \
            "TELEGRAM_WEBHOOK_IP_ADDRESS" "$webhook_ip_address"
        replace_env_value "$app_dir/venv/bin/python" "$env_file" \
            "TELEGRAM_WEBHOOK_SECRET_TOKEN" "$webhook_secret"
        printf 'Configure the public HTTPS route before restarting:\n'
        printf '  %s -> http://%s:%s%s\n' \
            "$webhook_public_url" "$webhook_listen" "$webhook_port" \
            "$(printf '%s' "$webhook_public_url" | sed -E 's#^https://[^/]+##')"
        if ! confirm "Is that HTTPS reverse-proxy route ready?" no; then
            cp -a "$backup" "$env_file"
            rm -f "$backup"
            die "Delivery mode was not changed."
        fi
    fi

    replace_env_value "$app_dir/venv/bin/python" "$env_file" \
        "TELEGRAM_DELIVERY_MODE" "$requested_mode"
    chown root:root "$env_file"
    chmod 0600 "$env_file"
    if ! validate_environment "$app_dir" "$env_file"; then
        cp -a "$backup" "$env_file"
        rm -f "$backup"
        die "Delivery configuration was invalid; the previous file was restored."
    fi

    was_active=$(systemctl is-active "$service" 2>/dev/null || true)
    if [[ "$was_active" == "active" ]]; then
        if ! systemctl restart "$service" || ! wait_for_readiness "$ACTIVE_PORT"; then
            warn "New delivery mode failed; restoring the previous configuration."
            cp -a "$backup" "$env_file"
            systemctl restart "$service" || true
            wait_for_readiness "$ACTIVE_PORT" || true
            rm -f "$backup"
            die "Delivery mode change was rolled back."
        fi
        telegram_delivery_status "$app_dir" "$env_file"
    fi
    rm -f "$backup"
    record_operation delivery "$ACTIVE_INSTANCE" "$requested_mode"
    cmd_status "$ACTIVE_INSTANCE"
}

cmd_miniapp() {
    require_root
    require_tty
    select_instance "${1:-}"
    local env_file
    local app_dir
    local backup
    local service
    local current_enabled
    local current_url
    local current_port
    local current_proxy_mode
    local requested_enabled="false"
    local public_url
    local port
    local webhook_port
    local default_choice="no"
    local was_active
    local was_enabled
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    service=$(service_name_for "$ACTIVE_INSTANCE")
    current_enabled=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_ENABLED")
    current_enabled=${current_enabled:-false}
    current_url=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PUBLIC_URL")
    current_port=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PORT")
    current_proxy_mode=$(dotenv_value "$app_dir" "$env_file" "REVERSE_PROXY_MODE")
    current_proxy_mode=${current_proxy_mode:-caddy}
    [[ "$current_enabled" == "true" ]] && default_choice="yes"

    if confirm "Enable the authenticated Telegram Mini App?" "$default_choice"; then
        requested_enabled="true"
    fi

    if [[ "$requested_enabled" == "$current_enabled" &&
        "$requested_enabled" == "false" ]]; then
        printf 'The Mini App is already disabled.\n'
        cmd_status "$ACTIVE_INSTANCE"
        return
    fi

    if [[ "$requested_enabled" == "false" && "$current_enabled" == "true" &&
        "$current_proxy_mode" == "caddy" ]]; then
        ensure_caddy_available
    fi
    if [[ "$requested_enabled" == "true" ]]; then
        while true; do
            public_url=$(
                prompt "Public HTTPS Mini App URL" \
                    "${current_url:-https://bot.example.com/getbible/${ACTIVE_INSTANCE}}"
            )
            validate_mini_app_url "$public_url" && break
            warn "Use a complete HTTPS URL without credentials, query, or fragment."
        done
        preflight_mini_app_dns "$app_dir/venv/bin/python" "$public_url" ||
            die "Mini App DNS preflight failed; no configuration was changed."
        if [[ "$current_proxy_mode" == "caddy" ]]; then
            ensure_caddy_available
        fi
        if [[ -z "$current_url" || -z "$current_port" || "$current_port" == "0" ]]; then
            current_port=$(next_mini_app_port)
        fi
        if [[ "$current_proxy_mode" == "external" ]]; then
            port=$(prompt "Private Mini App backend port" "$current_port")
        else
            port=$(prompt "Loopback Mini App listener port" "$current_port")
        fi
        validate_port "$port" && ((port >= 1024)) || {
            die "Mini App port must be an integer between 1024 and 65535."
        }
        webhook_port=$(dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_PORT")
        if [[ "$(dotenv_value "$app_dir" "$env_file" "TELEGRAM_DELIVERY_MODE")" == "webhook" &&
            "$port" == "$webhook_port" ]]; then
            die "The Mini App and webhook listeners require different ports."
        fi
        if [[ "$ACTIVE_PORT" != "0" && "$port" == "$ACTIVE_PORT" ]]; then
            die "The Mini App and health listeners require different ports."
        fi
        if mini_app_port_conflicts "$ACTIVE_INSTANCE" "$port"; then
            die "Mini App port ${port} is reserved by another managed instance."
        fi
        if [[ "$current_enabled" != "true" || "$port" != "$current_port" ]] &&
            ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:)$port$"; then
            die "Mini App port ${port} is already listening."
        fi
    fi

    backup=$(mktemp "${ETC_ROOT}/.${ACTIVE_INSTANCE}.env.XXXXXX")
    cp -a "$env_file" "$backup"
    if [[ "$requested_enabled" == "true" ]]; then
        replace_env_value "$app_dir/venv/bin/python" "$env_file" \
            "MINI_APP_PUBLIC_URL" "$public_url"
        if [[ "$current_proxy_mode" == "caddy" ]]; then
            replace_env_value "$app_dir/venv/bin/python" "$env_file" \
                "MINI_APP_LISTEN" "127.0.0.1"
        fi
        replace_env_value "$app_dir/venv/bin/python" "$env_file" \
            "MINI_APP_PORT" "$port"
    fi

    replace_env_value "$app_dir/venv/bin/python" "$env_file" \
        "MINI_APP_ENABLED" "$requested_enabled"
    chown root:root "$env_file"
    chmod 0600 "$env_file"
    if ! validate_environment "$app_dir" "$env_file"; then
        cp -a "$backup" "$env_file"
        rm -f "$backup"
        die "Mini App configuration was invalid; the previous file was restored."
    fi

    was_active=$(systemctl is-active "$service" 2>/dev/null || true)
    was_enabled=$(systemctl is-enabled "$service" 2>/dev/null || true)
    if [[ "$current_proxy_mode" == "caddy" ]]; then
        if ! begin_caddy_transaction; then
            cp -a "$backup" "$env_file"
            rm -f "$backup"
            die "Managed Caddy configuration failed; the previous environment was restored."
        fi
    fi

    local changed_ok="true"
    if [[ "$requested_enabled" == "true" ]]; then
        if [[ "$was_active" == "active" ]]; then
            systemctl restart "$service" || changed_ok="false"
        else
            systemctl enable --now "$service" || changed_ok="false"
        fi
        if [[ "$changed_ok" == "true" ]] &&
            ! wait_for_readiness "$ACTIVE_PORT"; then
            changed_ok="false"
        fi
        if [[ "$changed_ok" == "true" ]] &&
            ! verify_mini_app_instance "$app_dir" "$env_file"; then
            changed_ok="false"
        fi
    elif [[ "$was_active" == "active" ]]; then
        if ! systemctl restart "$service" ||
            ! wait_for_readiness "$ACTIVE_PORT"; then
            changed_ok="false"
        fi
    fi

    if [[ "$changed_ok" != "true" ]]; then
        warn "New Mini App configuration failed; restoring the previous configuration."
        cp -a "$backup" "$env_file"
        rollback_caddy_transaction
        if [[ "$was_active" == "active" ]]; then
            systemctl restart "$service" >/dev/null 2>&1 || true
            wait_for_readiness "$ACTIVE_PORT" || true
        else
            systemctl stop "$service" >/dev/null 2>&1 || true
        fi
        if [[ "$was_enabled" == "enabled" ]]; then
            systemctl enable "$service" >/dev/null 2>&1 || true
        else
            systemctl disable "$service" >/dev/null 2>&1 || true
        fi
        rm -f "$backup"
        die "Mini App change was rolled back."
    fi
    commit_caddy_transaction
    rm -f "$backup"
    record_operation miniapp "$ACTIVE_INSTANCE" "$requested_enabled"
    cmd_status "$ACTIVE_INSTANCE"
    if [[ "$requested_enabled" == "true" ]]; then
        printf '\nOne Telegram-side step remains:\n'
        printf '  Set this exact Main Mini App URL in @BotFather: %s\n' \
            "$public_url"
    fi
}

cmd_content() {
    require_root
    require_tty
    select_instance "${1:-}"
    local kind=${2:-}
    local file
    local backup
    local editor
    local app_dir
    local env_file
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")

    if [[ -z "$kind" ]]; then
        kind=$(prompt "Content to edit (welcome/help)" "help")
    fi
    case "${kind,,}" in
        welcome) file=$(welcome_file_for "$ACTIVE_INSTANCE") ;;
        help) file=$(help_file_for "$ACTIVE_INSTANCE") ;;
        *) die "Content must be welcome or help." ;;
    esac

    backup=$(mktemp "${ETC_ROOT}/.${ACTIVE_INSTANCE}.${kind}.XXXXXX")
    cp -a "$file" "$backup"
    editor=${EDITOR:-editor}
    if ! "$editor" "$file"; then
        cp -a "$backup" "$file"
        rm -f "$backup"
        die "Editor failed; the previous content was restored."
    fi
    chown "root:${ACTIVE_USER}" "$file"
    chmod 0640 "$file"
    if ! runuser --user "$ACTIVE_USER" -- test -r "$file" ||
        ! validate_environment "$app_dir" "$env_file"; then
        cp -a "$backup" "$file"
        rm -f "$backup"
        die "Content was invalid; the previous file was restored."
    fi
    rm -f "$backup"
    record_operation content "$ACTIVE_INSTANCE" "${kind,,}"
    if confirm "Restart ${ACTIVE_INSTANCE} and synchronize Telegram now?" yes; then
        cmd_restart "$ACTIVE_INSTANCE"
    else
        printf 'Content is valid but will apply only after restart.\n'
    fi
}

cmd_config() {
    require_root
    require_tty
    select_instance "${1:-}"
    local env_file
    local backup
    local app_dir
    local editor
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    backup=$(mktemp "${ETC_ROOT}/.${ACTIVE_INSTANCE}.env.XXXXXX")
    cp -a "$env_file" "$backup"
    editor=${EDITOR:-editor}
    "$editor" "$env_file"
    chown root:root "$env_file"
    chmod 0600 "$env_file"
    if ! validate_environment "$app_dir" "$env_file"; then
        cp -a "$backup" "$env_file"
        rm -f "$backup"
        die "Configuration was invalid; the previous file was restored."
    fi
    local edited_instance
    local edited_log_file
    local edited_port
    local edited_token
    local edited_welcome_file
    local edited_help_file
    local edited_mini_app_enabled
    local edited_mini_app_public_url
    local edited_mini_app_listen
    local edited_mini_app_port
    local edited_reverse_proxy_mode
    local edited_webhook_port
    local edited_delivery_mode
    edited_instance=$(dotenv_value "$app_dir" "$env_file" "INSTANCE_NAME")
    edited_log_file=$(dotenv_value "$app_dir" "$env_file" "LOG_FILE")
    edited_port=$(dotenv_value "$app_dir" "$env_file" "HEALTH_PORT")
    edited_token=$(dotenv_value "$app_dir" "$env_file" "TELEGRAM_API_TOKEN")
    edited_welcome_file=$(dotenv_value "$app_dir" "$env_file" "WELCOME_MESSAGE_FILE")
    edited_help_file=$(dotenv_value "$app_dir" "$env_file" "HELP_MESSAGE_FILE")
    edited_mini_app_enabled=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_ENABLED")
    edited_mini_app_public_url=$(
        dotenv_value "$app_dir" "$env_file" "MINI_APP_PUBLIC_URL"
    )
    edited_mini_app_listen=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_LISTEN")
    edited_mini_app_port=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PORT")
    edited_reverse_proxy_mode=$(dotenv_value "$app_dir" "$env_file" "REVERSE_PROXY_MODE")
    edited_webhook_port=$(dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_PORT")
    edited_delivery_mode=$(dotenv_value "$app_dir" "$env_file" "TELEGRAM_DELIVERY_MODE")
    if [[ "$edited_instance" != "$ACTIVE_INSTANCE" ||
        "$edited_log_file" != "$(log_file_for "$ACTIVE_INSTANCE")" ||
        "$edited_port" != "$ACTIVE_PORT" ||
        "$edited_welcome_file" != "$(welcome_file_for "$ACTIVE_INSTANCE")" ||
        "$edited_help_file" != "$(help_file_for "$ACTIVE_INSTANCE")" ||
        "$edited_mini_app_enabled" != "$(dotenv_value "$app_dir" "$backup" "MINI_APP_ENABLED")" ||
        "$edited_mini_app_public_url" != "$(dotenv_value "$app_dir" "$backup" "MINI_APP_PUBLIC_URL")" ||
        "$edited_mini_app_listen" != "$(dotenv_value "$app_dir" "$backup" "MINI_APP_LISTEN")" ||
        "$edited_reverse_proxy_mode" != "$(dotenv_value "$app_dir" "$backup" "REVERSE_PROXY_MODE")" ||
        "$edited_mini_app_port" != "$(dotenv_value "$app_dir" "$backup" "MINI_APP_PORT")" ]]; then
        cp -a "$backup" "$env_file"
        rm -f "$backup"
        die "INSTANCE_NAME, LOG_FILE, HEALTH_PORT, Mini App routing/listener settings, and content-file paths are manager-owned; the previous file was restored."
    fi
    if [[ "$edited_delivery_mode" == "webhook" &&
        "$edited_mini_app_port" == "$edited_webhook_port" ]]; then
        cp -a "$backup" "$env_file"
        rm -f "$backup"
        die "The Mini App and webhook listeners require different ports; the previous file was restored."
    fi
    if [[ -n "$edited_token" ]] &&
        ! ensure_unique_token "$edited_token" "$ACTIVE_INSTANCE"; then
        cp -a "$backup" "$env_file"
        rm -f "$backup"
        die "That token belongs to another local instance; the previous file was restored."
    fi
    if ! (sync_resource_dropin_from_env \
        "$app_dir" "$env_file" "$ACTIVE_INSTANCE"); then
        cp -a "$backup" "$env_file"
        rm -f "$backup"
        die "Native service limits were invalid; the previous file was restored."
    fi
    systemctl daemon-reload
    rm -f "$backup"
    record_operation config "$ACTIVE_INSTANCE" validated
    if confirm "Restart ${ACTIVE_INSTANCE} now?" yes; then
        cmd_restart "$ACTIVE_INSTANCE"
    else
        printf 'Configuration is valid but will apply only after restart.\n'
    fi
}

cmd_upgrade() {
    require_root
    require_tty
    local requested=""
    local source_request=""
    while (($# > 0)); do
        case "$1" in
            --source)
                [[ -n ${2:-} ]] || die "--source requires a directory."
                source_request=$2
                shift 2
                ;;
            -*)
                die "Unknown upgrade option: $1"
                ;;
            *)
                [[ -z "$requested" ]] || die "Only one instance may be upgraded."
                requested=$1
                shift
                ;;
        esac
    done
    select_instance "$requested"
    local source_dir
    local source_url
    local target_sha
    local python_bin
    local app_dir
    local next_dir
    local previous_dir
    local env_file
    local service
    source_dir=$(resolve_source_dir "$source_request")
    source_url=$(source_url_for "$source_dir")
    target_sha=$(git_source_read "$source_dir" rev-parse HEAD)
    python_bin=$(select_python)
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    next_dir="${INSTANCE_ROOT}/${ACTIVE_INSTANCE}/app.next"
    previous_dir="${INSTANCE_ROOT}/${ACTIVE_INSTANCE}/app.previous"
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")
    service=$(service_name_for "$ACTIVE_INSTANCE")

    [[ "$target_sha" != "$ACTIVE_SHA" ]] ||
        die "Instance ${ACTIVE_INSTANCE} already runs commit ${target_sha}."
    printf 'Upgrade %s\n  from %s\n  to   %s\n' \
        "$ACTIVE_INSTANCE" "$ACTIVE_SHA" "$target_sha"
    confirm "Build and deploy this exact reviewed commit?" yes ||
        die "Upgrade cancelled."

    migrate_instance_configuration \
        "$source_dir" "$python_bin" "$env_file" "$ACTIVE_USER" "$ACTIVE_INSTANCE"
    sync_resource_dropin_from_env "$app_dir" "$env_file" "$ACTIVE_INSTANCE"
    [[ ! -e "$next_dir" ]] || safe_remove_tree "$next_dir"
    UPGRADE_NEXT=$next_dir
    prepare_application \
        "$source_dir" "$source_url" "$target_sha" "$next_dir" "$python_bin" \
        "$ACTIVE_USER" "$env_file"
    install_shared_manager "$source_dir"
    install_log_rotation
    systemctl daemon-reload
    systemd-analyze verify "$service"

    systemctl stop "$service"
    [[ ! -e "$previous_dir" ]] || safe_remove_tree "$previous_dir"
    mv -- "$app_dir" "$previous_dir"
    mv -- "$next_dir" "$app_dir"
    UPGRADE_NEXT=""
    TEMP_PATHS=()
    write_metadata \
        "$ACTIVE_INSTANCE" "$ACTIVE_USER" "$ACTIVE_PORT" "$target_sha" \
        "$source_url" "$ACTIVE_CREATED_AT"
    systemctl daemon-reload

    if systemctl start "$service" &&
        wait_for_readiness "$ACTIVE_PORT" &&
        verify_mini_app_instance "$app_dir" "$env_file"; then
        record_operation upgrade "$ACTIVE_INSTANCE" ok
        printf 'Upgrade succeeded. app.previous is retained for one-step rollback.\n'
        printf 'The complete application tree, including Mini App assets, now runs commit %s.\n' \
            "$target_sha"
        cmd_status "$ACTIVE_INSTANCE"
        return
    fi

    warn "Upgrade failed readiness; restoring the previous application."
    systemctl stop "$service" || true
    safe_remove_tree "$app_dir"
    mv -- "$previous_dir" "$app_dir"
    write_metadata \
        "$ACTIVE_INSTANCE" "$ACTIVE_USER" "$ACTIVE_PORT" "$ACTIVE_SHA" \
        "$ACTIVE_SOURCE_URL" "$ACTIVE_CREATED_AT"
    systemctl daemon-reload
    systemctl start "$service"
    wait_for_readiness "$ACTIVE_PORT" || true
    record_operation upgrade "$ACTIVE_INSTANCE" rolled-back
    die "Upgrade failed and the previous application was restored."
}

cmd_rollback() {
    require_root
    require_tty
    select_instance "${1:-}"
    local app_dir
    local previous_dir
    local failed_dir
    local previous_sha
    local service
    local env_file
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    previous_dir="${INSTANCE_ROOT}/${ACTIVE_INSTANCE}/app.previous"
    failed_dir="${INSTANCE_ROOT}/${ACTIVE_INSTANCE}/app.failed"
    service=$(service_name_for "$ACTIVE_INSTANCE")
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")
    [[ -d "$previous_dir" ]] || die "No previous application is available."
    previous_sha=$(git -C "$previous_dir" rev-parse HEAD)
    printf 'Rollback %s from %s to %s\n' \
        "$ACTIVE_INSTANCE" "$ACTIVE_SHA" "$previous_sha"
    confirm "Perform this rollback?" no || die "Rollback cancelled."

    systemctl stop "$service"
    [[ ! -e "$failed_dir" ]] || safe_remove_tree "$failed_dir"
    mv -- "$app_dir" "$failed_dir"
    mv -- "$previous_dir" "$app_dir"
    write_metadata \
        "$ACTIVE_INSTANCE" "$ACTIVE_USER" "$ACTIVE_PORT" "$previous_sha" \
        "$ACTIVE_SOURCE_URL" "$ACTIVE_CREATED_AT"
    systemctl start "$service"
    if ! wait_for_readiness "$ACTIVE_PORT" ||
        ! verify_mini_app_instance "$app_dir" "$env_file"; then
        systemctl stop "$service" || true
        mv -- "$app_dir" "$previous_dir"
        mv -- "$failed_dir" "$app_dir"
        write_metadata \
            "$ACTIVE_INSTANCE" "$ACTIVE_USER" "$ACTIVE_PORT" "$ACTIVE_SHA" \
            "$ACTIVE_SOURCE_URL" "$ACTIVE_CREATED_AT"
        systemctl start "$service"
        die "Rollback target failed readiness; the original application was restored."
    fi
    mv -- "$failed_dir" "$previous_dir"
    record_operation rollback "$ACTIVE_INSTANCE" ok
    cmd_status "$ACTIVE_INSTANCE"
}

cmd_uninstall() {
    require_root
    require_tty
    select_instance "${1:-}"
    local confirmation
    local service
    local env_file
    local log_file
    local app_dir
    local mini_app_enabled
    local reverse_proxy_mode
    local preserve_log="yes"
    service=$(service_name_for "$ACTIVE_INSTANCE")
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")
    log_file=$(log_file_for "$ACTIVE_INSTANCE")
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    mini_app_enabled=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_ENABLED")
    reverse_proxy_mode=$(dotenv_value "$app_dir" "$env_file" "REVERSE_PROXY_MODE")
    reverse_proxy_mode=${reverse_proxy_mode:-caddy}
    warn "This removes only instance '${ACTIVE_INSTANCE}': its service, managed Mini App route, code, environment, content, cache, state, and service account."
    read -r -p "Type the exact instance name to continue: " confirmation
    [[ "$confirmation" == "$ACTIVE_INSTANCE" ]] || die "Confirmation did not match."
    if confirm "Delete the retained JSON log too?" no; then
        preserve_log="no"
    fi
    confirm "Permanently uninstall ${ACTIVE_INSTANCE}?" no ||
        die "Uninstall cancelled."

    if [[ "$reverse_proxy_mode" == "caddy" ]] &&
        command -v caddy >/dev/null 2>&1 &&
        [[ -f "$CADDYFILE" && -f "$CADDY_ROUTES" ]]; then
        begin_caddy_transaction "$ACTIVE_INSTANCE" ||
            die "The Mini App route could not be removed; the instance was not uninstalled."
        commit_caddy_transaction
    elif [[ "$mini_app_enabled" == "true" && "$reverse_proxy_mode" == "caddy" ]]; then
        die "Managed Caddy state is missing; restore it before uninstalling this enabled Mini App."
    fi
    if [[ -x "$app_dir/venv/bin/python" && -f "$env_file" ]]; then
        delete_telegram_webhook "$app_dir" "$env_file" ||
            warn "Telegram webhook removal failed. Rotate the token through @BotFather if this instance will not be restored."
    fi
    systemctl disable --now "$service" 2>/dev/null || true
    safe_remove_tree "${INSTANCE_ROOT}/${ACTIVE_INSTANCE}"
    rm -rf --one-file-system -- "${CACHE_ROOT:?}/${ACTIVE_INSTANCE}"
    rm -rf --one-file-system -- "${STATE_ROOT:?}/${ACTIVE_INSTANCE}"
    rm -f -- \
        "$env_file" \
        "$(metadata_file_for "$ACTIVE_INSTANCE")" \
        "$(welcome_file_for "$ACTIVE_INSTANCE")" \
        "$(help_file_for "$ACTIVE_INSTANCE")"
    rm -rf --one-file-system -- "$(resource_dropin_dir_for "$ACTIVE_INSTANCE")"
    if id "$ACTIVE_USER" >/dev/null 2>&1; then
        userdel "$ACTIVE_USER"
    fi
    if [[ "$preserve_log" == "no" ]]; then
        rm -f -- "$log_file"
    fi
    systemctl daemon-reload
    systemctl reset-failed "$service" 2>/dev/null || true
    record_operation uninstall "$ACTIVE_INSTANCE" ok
    printf 'Instance %s was removed. Revoke or rotate its Telegram token through @BotFather when appropriate.\n' "$ACTIVE_INSTANCE"
}

require_docker() {
    command -v docker >/dev/null 2>&1 ||
        die "Docker Engine with the Compose plugin is required."
    docker info >/dev/null 2>&1 ||
        die "Docker is not reachable. Start Docker or grant this operator access to its socket."
}

require_docker_compose() {
    require_docker
    docker compose version >/dev/null 2>&1 ||
        die "The Docker Compose plugin is required."
}

resolve_docker_source_dir() {
    local candidate
    for candidate in "$SCRIPT_DIR" "$PWD"; do
        if [[ -f "${candidate}/Dockerfile" &&
            -f "${candidate}/compose.yaml" &&
            -f "${candidate}/container/runtime.py" ]]; then
            readlink -f "$candidate"
            return
        fi
    done
    die "Run Docker deployment from a GetBible Robot checkout containing Dockerfile and compose.yaml."
}

DOCKER_SOURCE_DIR=""
DOCKER_COMPOSE_FILE=""
DOCKER_SECURE_OVERLAY=""
DOCKER_BUILD_OVERLAY=""
DOCKER_ENV_FILE=""
DOCKER_MULTI="0"
DOCKER_BUILD_LOCAL="0"
declare -a DOCKER_COMPOSE_ARGS=()

prepare_docker_compose() {
    require_docker_compose
    DOCKER_SOURCE_DIR=$(resolve_docker_source_dir)
    DOCKER_COMPOSE_FILE="${DOCKER_SOURCE_DIR}/compose.yaml"
    DOCKER_SECURE_OVERLAY=""
    DOCKER_BUILD_OVERLAY=""
    DOCKER_ENV_FILE=""
    DOCKER_MULTI="0"
    DOCKER_BUILD_LOCAL="0"
    DOCKER_COMPOSE_ARGS=()
    while (($#)); do
        case "$1" in
            --multi)
                DOCKER_COMPOSE_FILE="${DOCKER_SOURCE_DIR}/compose.multi.yaml"
                DOCKER_MULTI="1"
                shift
                ;;
            --secure)
                DOCKER_SECURE_OVERLAY="${DOCKER_SOURCE_DIR}/compose.secret.yaml"
                shift
                ;;
            --build)
                DOCKER_BUILD_OVERLAY="${DOCKER_SOURCE_DIR}/compose.build.yaml"
                DOCKER_BUILD_LOCAL="1"
                shift
                ;;
            --env-file)
                (($# >= 2)) || die "--env-file requires a file path."
                DOCKER_ENV_FILE=$(readlink -f "$2")
                shift 2
                ;;
            *)
                die "Unknown Docker Compose option: $1"
                ;;
        esac
    done
    [[ -f "$DOCKER_COMPOSE_FILE" ]] ||
        die "The checkout is missing Docker deployment files."
    if [[ "$DOCKER_BUILD_LOCAL" == "1" &&
        (! -f "$DOCKER_BUILD_OVERLAY" ||
            ! -f "${DOCKER_SOURCE_DIR}/Dockerfile") ]]; then
        die "The checkout is missing the local Docker build files."
    fi
    if [[ "$DOCKER_MULTI" == "1" && -n "$DOCKER_SECURE_OVERLAY" ]]; then
        die "--secure is for the environment-driven single-bot layout."
    fi
    if [[ -n "$DOCKER_ENV_FILE" ]]; then
        [[ -f "$DOCKER_ENV_FILE" ]] ||
            die "Docker environment file not found: ${DOCKER_ENV_FILE}"
        DOCKER_COMPOSE_ARGS+=(--env-file "$DOCKER_ENV_FILE")
    fi
    DOCKER_COMPOSE_ARGS+=(
        --project-directory "$DOCKER_SOURCE_DIR"
        --file "$DOCKER_COMPOSE_FILE"
    )
    if [[ -n "$DOCKER_SECURE_OVERLAY" ]]; then
        DOCKER_COMPOSE_ARGS+=(--file "$DOCKER_SECURE_OVERLAY")
    fi
    if [[ -n "$DOCKER_BUILD_OVERLAY" ]]; then
        DOCKER_COMPOSE_ARGS+=(--file "$DOCKER_BUILD_OVERLAY")
    fi
}

prepare_docker_image() {
    if [[ "$DOCKER_BUILD_LOCAL" == "1" ]]; then
        info "Building the local GetBible Robot image"
        docker compose "${DOCKER_COMPOSE_ARGS[@]}" build --pull robot
        return
    fi
    info "Pulling the configured GetBible Robot image"
    docker compose "${DOCKER_COMPOSE_ARGS[@]}" pull robot
}

docker_container_names() {
    docker ps --all \
        --filter "label=io.getbible.robot.container=true" \
        --format '{{.Names}}' |
        sort
}

select_docker_container() {
    local requested=${1:-}
    local names=()
    local index
    local choice
    local label
    require_docker
    if [[ -n "$requested" ]]; then
        validate_docker_container_name "$requested" ||
            die "Invalid Docker container name: ${requested}"
        label=$(
            docker inspect \
                --format '{{ index .Config.Labels "io.getbible.robot.container" }}' \
                "$requested" 2>/dev/null || true
        )
        [[ "$label" == "true" ]] ||
            die "Container is not a managed GetBible Robot container: ${requested}"
        printf '%s\n' "$requested"
        return
    fi
    mapfile -t names < <(docker_container_names)
    ((${#names[@]} > 0)) || die "No GetBible Robot Docker containers were found."
    if ((${#names[@]} == 1)); then
        printf '%s\n' "${names[0]}"
        return
    fi
    require_tty
    printf 'Select a GetBible Robot container:\n' >&2
    for index in "${!names[@]}"; do
        printf '  %d) %s\n' "$((index + 1))" "${names[$index]}" >&2
    done
    read -r -p "Selection: " choice
    [[ "$choice" =~ ^[0-9]+$ ]] || die "Selection must be a number."
    ((choice >= 1 && choice <= ${#names[@]})) ||
        die "Selection is out of range."
    printf '%s\n' "${names[$((choice - 1))]}"
}

cmd_docker_deploy() {
    prepare_docker_compose "$@"
    info "Validating Docker Compose configuration"
    docker compose "${DOCKER_COMPOSE_ARGS[@]}" config --quiet || return 1
    prepare_docker_image || return 1
    info "Deploying GetBible Robot"
    docker compose "${DOCKER_COMPOSE_ARGS[@]}" up --detach --no-build
    docker compose "${DOCKER_COMPOSE_ARGS[@]}" ps
    printf '\nInitial container output:\n'
    docker compose "${DOCKER_COMPOSE_ARGS[@]}" logs --no-color --tail 40
    printf '\nUse ./setup.sh docker-doctor for the complete deployment check.\n'
}

cmd_docker_init() {
    local source_dir
    source_dir=$(resolve_docker_source_dir)
    local env_file="${source_dir}/.env"
    while (($#)); do
        case "$1" in
            --env-file)
                (($# >= 2)) || die "--env-file requires a file path."
                env_file=$(readlink -m "$2")
                shift 2
                ;;
            *) die "Unknown Docker init option: $1" ;;
        esac
    done
    if [[ -e "$env_file" ]]; then
        [[ -f "$env_file" && ! -L "$env_file" ]] ||
            die "Docker environment path is not a regular file: ${env_file}"
        chmod 0600 "$env_file"
        printf 'Docker environment already exists: %s\n' "$env_file"
        return
    fi
    if [[ ! -d "$(dirname "$env_file")" ]]; then
        install -d -m 0700 "$(dirname "$env_file")"
    fi
    install -m 0600 \
        "${source_dir}/docker/examples/compose.env.example" \
        "$env_file"
    printf 'Created editable Docker environment: %s\n' "$env_file"
    printf 'Edit the required token, public URL, proxy networks, and limits before deployment.\n'
}

cmd_docker_validate() {
    prepare_docker_compose "$@"
    info "Validating Docker Compose configuration"
    docker compose "${DOCKER_COMPOSE_ARGS[@]}" config --quiet
    prepare_docker_image || return 1
    if [[ "$DOCKER_MULTI" == "1" ]]; then
        printf 'Compose configuration and image are valid. Each mounted multi-bot instance is validated by the supervisor.\n'
        return
    fi
    info "Validating the Robot application environment"
    docker compose "${DOCKER_COMPOSE_ARGS[@]}" run \
        --rm \
        --no-deps \
        --entrypoint python \
        robot \
        -c \
        'from config import Settings; Settings.from_env(load_environment_file=False)' ||
        return 1
    printf 'Docker Compose and Robot application configuration are valid.\n'
}

cmd_docker_restart() {
    prepare_docker_compose "$@"
    info "Validating Docker Compose configuration"
    docker compose "${DOCKER_COMPOSE_ARGS[@]}" config --quiet || return 1
    info "Recreating GetBible Robot so configuration changes take effect"
    docker compose "${DOCKER_COMPOSE_ARGS[@]}" up \
        --detach \
        --force-recreate \
        --no-build
    docker compose "${DOCKER_COMPOSE_ARGS[@]}" ps
    printf '\nRecent container output:\n'
    docker compose "${DOCKER_COMPOSE_ARGS[@]}" logs --no-color --tail 40
}

cmd_docker_config() {
    require_tty
    local source_dir
    source_dir=$(resolve_docker_source_dir)
    local env_file="${source_dir}/.env"
    local restart="1"
    local -a compose_options=()
    while (($#)); do
        case "$1" in
            --env-file)
                (($# >= 2)) || die "--env-file requires a file path."
                env_file=$(readlink -m "$2")
                compose_options+=(--env-file "$env_file")
                shift 2
                ;;
            --secure)
                compose_options+=(--secure)
                shift
                ;;
            --build)
                compose_options+=(--build)
                shift
                ;;
            --no-restart)
                restart="0"
                shift
                ;;
            *) die "Unknown Docker config option: $1" ;;
        esac
    done
    cmd_docker_init --env-file "$env_file"
    local backup
    local editor
    backup=$(mktemp)
    cp -- "$env_file" "$backup"
    chmod 0600 "$backup"
    editor=${EDITOR:-editor}
    if ! "$editor" "$env_file"; then
        cp -- "$backup" "$env_file"
        rm -f -- "$backup"
        die "Editor failed; the previous Docker environment was restored."
    fi
    chmod 0600 "$env_file"
    if ! cmd_docker_validate "${compose_options[@]}"; then
        cp -- "$backup" "$env_file"
        rm -f -- "$backup"
        die "Docker configuration validation failed; the previous file was restored."
    fi
    rm -f -- "$backup"
    if [[ "$restart" == "1" ]]; then
        cmd_docker_restart "${compose_options[@]}"
    else
        printf 'Validated Docker configuration saved without restarting the workload.\n'
    fi
}

cmd_docker_list() {
    require_docker
    local names=()
    mapfile -t names < <(docker_container_names)
    if ((${#names[@]} == 0)); then
        printf 'No GetBible Robot Docker containers are deployed.\n'
        return
    fi
    docker ps --all \
        --filter "label=io.getbible.robot.container=true" \
        --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Image}}'
}

cmd_docker_status() {
    local container
    container=$(select_docker_container "${1:-}")
    docker inspect \
        --format $'Container:     {{.Name}}\nImage:         {{.Config.Image}}\nState:         {{.State.Status}}\nHealth:        {{if .State.Health}}{{.State.Health.Status}}{{else}}not configured{{end}}\nStarted:       {{.State.StartedAt}}\nRestarts:      {{.RestartCount}}\nMemory limit:  {{.HostConfig.Memory}}\nPID limit:     {{.HostConfig.PidsLimit}}' \
        "$container"
    if [[ "$(docker inspect --format '{{.State.Running}}' "$container")" == "true" ]]; then
        printf '\nBot supervisor status:\n'
        docker exec "$container" getbible-robot-container status || true
    fi
}

cmd_docker_logs() {
    local container
    local lines=${2:-200}
    [[ "$lines" =~ ^[0-9]+$ ]] && ((lines >= 1 && lines <= 10000)) ||
        die "Docker log line count must be between 1 and 10000."
    container=$(select_docker_container "${1:-}")
    docker logs --tail "$lines" "$container"
}

cmd_docker_follow() {
    local container
    require_tty
    container=$(select_docker_container "${1:-}")
    docker logs --follow --tail 100 "$container"
}

cmd_docker_manage() {
    local container
    require_tty
    container=$(select_docker_container "${1:-}")
    docker exec --interactive --tty "$container" /app/setup.sh menu
}

cmd_docker_shell() {
    local container
    require_tty
    container=$(select_docker_container "${1:-}")
    docker exec --interactive --tty "$container" /bin/bash
}

cmd_docker_doctor() {
    local container
    container=$(select_docker_container "${1:-}")
    cmd_docker_status "$container"
    printf '\nBot supervisor diagnostics:\n'
    if [[ "$(docker inspect --format '{{.State.Running}}' "$container")" == "true" ]]; then
        docker exec "$container" getbible-robot-container doctor || true
    else
        printf 'Container is not running; supervisor diagnostics are unavailable.\n'
    fi
    printf '\nRecent stdout/stderr:\n'
    docker logs --tail 200 "$container" 2>&1 || true
}

cmd_self_test() {
    local failed=0
    validate_instance_name "production" || ((failed += 1))
    validate_instance_name "bot-02" || ((failed += 1))
    ! validate_instance_name "A" || ((failed += 1))
    ! validate_instance_name "../escape" || ((failed += 1))
    ! validate_instance_name "bot--02" || ((failed += 1))
    [[ "$(service_user_for "production")" == "gb-production" ]] || ((failed += 1))
    validate_translation "kjv" || ((failed += 1))
    validate_translation "chi_un" || ((failed += 1))
    ! validate_translation "KJV" || ((failed += 1))
    validate_port "0" || ((failed += 1))
    validate_port "65535" || ((failed += 1))
    ! validate_port "65536" || ((failed += 1))
    validate_delivery_mode "polling" || ((failed += 1))
    validate_delivery_mode "webhook" || ((failed += 1))
    ! validate_delivery_mode "streaming" || ((failed += 1))
    validate_docker_container_name "getbible-robot-production" ||
        ((failed += 1))
    ! validate_docker_container_name "../robot" || ((failed += 1))
    validate_webhook_url "https://bot.example.com/telegram/production" ||
        ((failed += 1))
    ! validate_webhook_url "http://bot.example.com/telegram/production" ||
        ((failed += 1))
    ! validate_webhook_url "https://bot.example.com/.*" ||
        ((failed += 1))
    validate_mini_app_url "https://bot.example.com/getbible/production" ||
        ((failed += 1))
    validate_mini_app_url "https://bot.example.com:8443/getbible/" ||
        ((failed += 1))
    ! validate_mini_app_url "http://bot.example.com/getbible" ||
        ((failed += 1))
    ! validate_mini_app_url "https://user@bot.example.com/getbible" ||
        ((failed += 1))
    ! validate_mini_app_url "https://bot.example.com/getbible?token=secret" ||
        ((failed += 1))
    ! validate_mini_app_url "https://127.0.0.1/getbible" ||
        ((failed += 1))
    ! validate_mini_app_url "https://bot.example.com/a/../b" ||
        ((failed += 1))
    validate_token_shape "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi" ||
        ((failed += 1))
    ! validate_token_shape "replace-with-a-real-bot-token" || ((failed += 1))
    [[ -f "$UNIT_SOURCE" ]] || ((failed += 1))
    [[ -f "${SCRIPT_DIR}/deploy/welcome.txt" ]] || ((failed += 1))
    [[ -f "${SCRIPT_DIR}/deploy/help.txt" ]] || ((failed += 1))
    if ((failed > 0)); then
        die "Manager self-test failed with ${failed} error(s)."
    fi
    printf 'Manager self-test passed.\n'
}

cmd_menu() {
    require_root
    require_tty
    local selection
    while true; do
        cat <<'EOF'

GetBible Robot operations
  1) Install a new instance
  2) List instances
  3) Show status
  4) Start
  5) Stop
  6) Restart
  7) Show logs
  8) Follow logs
  9) Runtime details
 10) Diagnostics
 11) Repair application access
 12) Edit configuration
 13) Switch polling/webhook delivery
 14) Configure Telegram Mini App
 15) Edit welcome/help content
 16) Update / upgrade deployment
 17) Roll back
 18) Uninstall
 19) Deploy / update recommended Docker container
 20) List Docker containers
 21) Open Docker container management
 22) Open shell inside a Docker container
 23) Show Docker logs
 24) Docker diagnostics
 25) Initialize editable Docker environment
 26) Edit, validate, and apply Docker configuration
 27) Validate Docker configuration
 28) Recreate Docker workload after direct edits
  0) Exit
EOF
        read -r -p "Selection: " selection
        case "$selection" in
            1) cmd_install ;;
            2) cmd_list ;;
            3) cmd_status ;;
            4) cmd_start ;;
            5) cmd_stop ;;
            6) cmd_restart ;;
            7) cmd_logs ;;
            8) cmd_follow ;;
            9) cmd_runtime ;;
            10) cmd_doctor ;;
            11) cmd_repair ;;
            12) cmd_config ;;
            13) cmd_delivery ;;
            14) cmd_miniapp ;;
            15) cmd_content ;;
            16) cmd_upgrade ;;
            17) cmd_rollback ;;
            18) cmd_uninstall ;;
            19) cmd_docker_deploy ;;
            20) cmd_docker_list ;;
            21) cmd_docker_manage ;;
            22) cmd_docker_shell ;;
            23) cmd_docker_logs ;;
            24) cmd_docker_doctor ;;
            25) cmd_docker_init ;;
            26) cmd_docker_config ;;
            27) cmd_docker_validate ;;
            28) cmd_docker_restart ;;
            0) return ;;
            *) warn "Unknown selection." ;;
        esac
    done
}

main() {
    local command=${1:-menu}
    [[ $# -eq 0 ]] || shift
    case "$command" in
        install) cmd_install "$@" ;;
        list) cmd_list "$@" ;;
        start) cmd_start "$@" ;;
        stop) cmd_stop "$@" ;;
        restart) cmd_restart "$@" ;;
        status) cmd_status "$@" ;;
        runtime) cmd_runtime "$@" ;;
        logs) cmd_logs "$@" ;;
        follow) cmd_follow "$@" ;;
        doctor) cmd_doctor "$@" ;;
        repair) cmd_repair "$@" ;;
        config) cmd_config "$@" ;;
        delivery) cmd_delivery "$@" ;;
        miniapp) cmd_miniapp "$@" ;;
        content) cmd_content "$@" ;;
        update|upgrade) cmd_upgrade "$@" ;;
        rollback) cmd_rollback "$@" ;;
        uninstall) cmd_uninstall "$@" ;;
        docker-deploy|docker-update) cmd_docker_deploy "$@" ;;
        docker-init) cmd_docker_init "$@" ;;
        docker-config) cmd_docker_config "$@" ;;
        docker-validate) cmd_docker_validate "$@" ;;
        docker-restart|docker-apply) cmd_docker_restart "$@" ;;
        docker-list) cmd_docker_list "$@" ;;
        docker-status) cmd_docker_status "$@" ;;
        docker-logs) cmd_docker_logs "$@" ;;
        docker-follow) cmd_docker_follow "$@" ;;
        docker-manage) cmd_docker_manage "$@" ;;
        docker-shell) cmd_docker_shell "$@" ;;
        docker-doctor) cmd_docker_doctor "$@" ;;
        menu) cmd_menu "$@" ;;
        self-test) cmd_self_test "$@" ;;
        help|-h|--help) usage ;;
        version|--version) printf '%s setup manager %s\n' "$PROGRAM" "$VERSION" ;;
        *) usage >&2; die "Unknown command: ${command}" ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
