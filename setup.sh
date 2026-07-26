#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

PROGRAM="getbible-robot"
VERSION="3"
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
        if [[ -n "$INSTALLING_USER" ]] && id "$INSTALLING_USER" >/dev/null 2>&1; then
            userdel "$INSTALLING_USER" >/dev/null 2>&1 || true
        fi
    fi
}
trap cleanup EXIT

usage() {
    cat <<'EOF'
GetBible Robot secure multi-instance setup and operations manager.

Usage:
  sudo ./setup.sh install [--source DIR]
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
  content     Edit the welcome or detailed help text
  upgrade     Deploy the exact commit from a reviewed source checkout
  rollback    Return to the immediately previous deployed application
  uninstall   Remove one instance after explicit confirmation
  menu        Open the interactive operations menu
  self-test   Run safe manager validation tests
  help        Show this help

When an instance argument is omitted, an interactive terminal presents a
numbered selector. Non-interactive commands must provide the instance name.
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

validate_delivery_mode() {
    [[ ${1:-} == "polling" || ${1:-} == "webhook" ]]
}

validate_webhook_url() {
    local value=${1:-}
    [[ "$value" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$ ]]
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
        die "Automatic package installation supports apt-get and dnf. Install git, curl, tar, systemd, logrotate, Python 3.10-3.12, and the matching venv package."
    fi
}

python_supported() {
    "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)
PY
}

select_python() {
    local candidates=()
    local candidate
    if [[ -n "${PYTHON_BIN:-}" ]]; then
        candidates+=("$PYTHON_BIN")
    fi
    candidates+=(python3.12 python3.11 python3.10 python3)
    for candidate in "${candidates[@]}"; do
        if command -v "$candidate" >/dev/null 2>&1 && python_supported "$candidate"; then
            command -v "$candidate"
            return
        fi
    done
    die "A supported Python 3.10, 3.11, or 3.12 interpreter is required."
}

resolve_source_dir() {
    local requested=${1:-}
    local candidate
    if [[ -n "$requested" ]]; then
        candidate=$(readlink -f "$requested")
    elif git -C "$SCRIPT_DIR" rev-parse --show-toplevel >/dev/null 2>&1; then
        candidate=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)
    elif git -C "$PWD" rev-parse --show-toplevel >/dev/null 2>&1; then
        candidate=$(git -C "$PWD" rev-parse --show-toplevel)
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
    git -C "$candidate" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
        die "Source directory is not a Git checkout."
    git -C "$candidate" diff --quiet --ignore-submodules --
    git -C "$candidate" diff --cached --quiet --ignore-submodules --
    printf '%s\n' "$candidate"
}

source_url_for() {
    local source_dir=$1
    local url
    url=$(git -C "$source_dir" remote get-url origin 2>/dev/null || true)
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
    if [[ -z "$current_limit" || "$current_limit" == "8388608" ]]; then
        replace_env_value "$python_bin" "$env_file" \
            "GETBIBLE_MAX_RESPONSE_BYTES" "67108864"
    fi
    ensure_env_value "$python_bin" "$env_file" \
        "SEARCH_MAX_RESPONSE_BYTES" "4194304"
    ensure_env_value "$python_bin" "$env_file" "MAX_CONCURRENT_SEARCHES" "1"
    ensure_env_value "$python_bin" "$env_file" "PREWARM_DEFAULT_TRANSLATION" "true"
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
    rotate 14
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
    if [[ ${1:-} == "--source" ]]; then
        [[ -n ${2:-} ]] || die "--source requires a directory."
        source_request=$2
        shift 2
    fi
    (($# == 0)) || die "Unexpected install arguments: $*"

    install_host_prerequisites
    local source_dir
    local source_url
    local sha
    local python_bin
    source_dir=$(resolve_source_dir "$source_request")
    source_url=$(source_url_for "$source_dir")
    sha=$(git -C "$source_dir" rev-parse HEAD)
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
    local webhook_port="9001"
    local webhook_secret=""
    if [[ "$delivery_mode" == "webhook" ]]; then
        printf '\nWebhook mode requires a public HTTPS URL. Telegram connects to that URL;\n'
        printf 'your reverse proxy forwards the URL path to this instance on loopback.\n'
        while true; do
            webhook_public_url=$(
                prompt "Public HTTPS webhook URL" \
                    "https://bot.example.com/telegram/${instance}"
            )
            validate_webhook_url "$webhook_public_url" && break
            warn "Use a complete HTTPS URL with a private path and no query or fragment."
        done
        webhook_port=$(next_webhook_port)
        webhook_port=$(prompt "Loopback webhook listener port" "$webhook_port")
        validate_port "$webhook_port" && [[ "$webhook_port" != "0" ]] ||
            die "Webhook port must be an integer between 1 and 65535."
        if ss -ltnH 2>/dev/null | awk '{print $4}' |
            grep -Eq "(^|:)$webhook_port$"; then
            die "Webhook port ${webhook_port} is already listening."
        fi
        webhook_ip_address=$(prompt "Optional fixed public IP for Telegram" "")
        webhook_secret=$(generate_webhook_secret "$python_bin")
    fi

    local suggested_port
    local health_port
    suggested_port=$(next_health_port)
    while true; do
        health_port=$(prompt "Loopback health/metrics port (0 disables)" "$suggested_port")
        validate_port "$health_port" || {
            warn "Port must be an integer between 0 and 65535."
            continue
        }
        if [[ "$health_port" != "0" ]] &&
            ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:)$health_port$"; then
            warn "Port ${health_port} is already listening."
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
    if [[ "$delivery_mode" == "webhook" ]] &&
        ! confirm "Is the public HTTPS reverse-proxy route already configured?" no; then
        warn "The instance will be installed but not started. Configure HTTPS, then run '${PROGRAM} start ${instance}'."
        start_now="no"
    else
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
        printf '  Proxy target:   http://127.0.0.1:%s%s\n' \
            "$webhook_port" "$(printf '%s' "$webhook_public_url" | sed -E 's#^https://[^/]+##')"
    fi
    printf '  Health:         127.0.0.1:%s\n\n' "$health_port"
    confirm "Create this instance?" yes || die "Installation cancelled."

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
    replace_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_PORT" "$webhook_port"
    replace_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_SECRET_TOKEN" "$webhook_secret"
    replace_env_value "$python_bin" "$env_file" "TELEGRAM_WEBHOOK_IP_ADDRESS" "$webhook_ip_address"
    replace_env_value "$python_bin" "$env_file" "BOT_NAME" "$bot_name"
    replace_env_value "$python_bin" "$env_file" "BOT_DESCRIPTION" "$bot_description"
    replace_env_value "$python_bin" "$env_file" "BOT_SHORT_DESCRIPTION" "$bot_short_description"
    replace_env_value "$python_bin" "$env_file" "TRANSLATION" "$translation"
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
        systemctl enable --now "$service"
        if ! wait_for_readiness "$health_port"; then
            systemctl status "$service" --no-pager || true
            tail -n 100 "$log_file" || true
            record_operation install "$instance" failed-readiness
            die "The instance started but did not become ready. Run '${PROGRAM} doctor ${instance}'."
        fi
        telegram_delivery_status "$app_dir" "$env_file" ||
            die "The instance started, but Telegram delivery validation failed."
    fi

    record_operation install "$instance" ok
    printf '\nInstance %s installed successfully.\n' "$instance"
    printf 'Manage it with:\n'
    printf '  sudo %s status %s\n' "$PROGRAM" "$instance"
    printf '  sudo %s logs %s\n' "$PROGRAM" "$instance"
    printf '  sudo %s doctor %s\n' "$PROGRAM" "$instance"
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
    service=$(service_name_for "$ACTIVE_INSTANCE")
    systemctl start "$service"
    wait_for_readiness "$ACTIVE_PORT" ||
        die "Service started but readiness did not succeed."
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
    service=$(service_name_for "$ACTIVE_INSTANCE")
    systemctl restart "$service"
    wait_for_readiness "$ACTIVE_PORT" ||
        die "Service restarted but readiness did not succeed."
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
    local webhook_port
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
            printf 'Webhook URL:   %s\n' "$webhook_public_url"
            printf 'Webhook local: 127.0.0.1:%s\n' "$webhook_port"
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
        -p MemoryMax \
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
        webhook_port=$(
            dotenv_value "$app_dir" "$env_file" "TELEGRAM_WEBHOOK_PORT"
        )
        if [[ -z "$webhook_port" || "$webhook_port" == "0" ]]; then
            webhook_port=$(next_webhook_port)
        fi
        webhook_port=$(prompt "Loopback webhook listener port" "$webhook_port")
        validate_port "$webhook_port" && [[ "$webhook_port" != "0" ]] || {
            rm -f "$backup"
            die "Webhook port must be an integer between 1 and 65535."
        }
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
            "TELEGRAM_WEBHOOK_PORT" "$webhook_port"
        replace_env_value "$app_dir/venv/bin/python" "$env_file" \
            "TELEGRAM_WEBHOOK_IP_ADDRESS" "$webhook_ip_address"
        replace_env_value "$app_dir/venv/bin/python" "$env_file" \
            "TELEGRAM_WEBHOOK_SECRET_TOKEN" "$webhook_secret"
        printf 'Configure the public HTTPS route before restarting:\n'
        printf '  %s -> http://127.0.0.1:%s%s\n' \
            "$webhook_public_url" "$webhook_port" \
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
    edited_instance=$(dotenv_value "$app_dir" "$env_file" "INSTANCE_NAME")
    edited_log_file=$(dotenv_value "$app_dir" "$env_file" "LOG_FILE")
    edited_port=$(dotenv_value "$app_dir" "$env_file" "HEALTH_PORT")
    edited_token=$(dotenv_value "$app_dir" "$env_file" "TELEGRAM_API_TOKEN")
    edited_welcome_file=$(dotenv_value "$app_dir" "$env_file" "WELCOME_MESSAGE_FILE")
    edited_help_file=$(dotenv_value "$app_dir" "$env_file" "HELP_MESSAGE_FILE")
    if [[ "$edited_instance" != "$ACTIVE_INSTANCE" ||
        "$edited_log_file" != "$(log_file_for "$ACTIVE_INSTANCE")" ||
        "$edited_port" != "$ACTIVE_PORT" ||
        "$edited_welcome_file" != "$(welcome_file_for "$ACTIVE_INSTANCE")" ||
        "$edited_help_file" != "$(help_file_for "$ACTIVE_INSTANCE")" ]]; then
        cp -a "$backup" "$env_file"
        rm -f "$backup"
        die "INSTANCE_NAME, LOG_FILE, HEALTH_PORT, and content-file paths are manager-owned; the previous file was restored."
    fi
    if [[ -n "$edited_token" ]] &&
        ! ensure_unique_token "$edited_token" "$ACTIVE_INSTANCE"; then
        cp -a "$backup" "$env_file"
        rm -f "$backup"
        die "That token belongs to another local instance; the previous file was restored."
    fi
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
    target_sha=$(git -C "$source_dir" rev-parse HEAD)
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

    if systemctl start "$service" && wait_for_readiness "$ACTIVE_PORT"; then
        record_operation upgrade "$ACTIVE_INSTANCE" ok
        printf 'Upgrade succeeded. app.previous is retained for one-step rollback.\n'
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
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    previous_dir="${INSTANCE_ROOT}/${ACTIVE_INSTANCE}/app.previous"
    failed_dir="${INSTANCE_ROOT}/${ACTIVE_INSTANCE}/app.failed"
    service=$(service_name_for "$ACTIVE_INSTANCE")
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
    if ! wait_for_readiness "$ACTIVE_PORT"; then
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
    local preserve_log="yes"
    service=$(service_name_for "$ACTIVE_INSTANCE")
    env_file=$(environment_file_for "$ACTIVE_INSTANCE")
    log_file=$(log_file_for "$ACTIVE_INSTANCE")
    app_dir=$(application_dir_for "$ACTIVE_INSTANCE")
    warn "This removes instance '${ACTIVE_INSTANCE}', its code, environment file, content, cache, state, service account, and Telegram delivery service."
    read -r -p "Type the exact instance name to continue: " confirmation
    [[ "$confirmation" == "$ACTIVE_INSTANCE" ]] || die "Confirmation did not match."
    if confirm "Delete the retained JSON log too?" no; then
        preserve_log="no"
    fi
    confirm "Permanently uninstall ${ACTIVE_INSTANCE}?" no ||
        die "Uninstall cancelled."

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
    validate_webhook_url "https://bot.example.com/telegram/production" ||
        ((failed += 1))
    ! validate_webhook_url "http://bot.example.com/telegram/production" ||
        ((failed += 1))
    ! validate_webhook_url "https://bot.example.com/.*" ||
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
 14) Edit welcome/help content
 15) Upgrade
 16) Roll back
 17) Uninstall
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
            14) cmd_content ;;
            15) cmd_upgrade ;;
            16) cmd_rollback ;;
            17) cmd_uninstall ;;
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
        content) cmd_content "$@" ;;
        upgrade) cmd_upgrade "$@" ;;
        rollback) cmd_rollback "$@" ;;
        uninstall) cmd_uninstall "$@" ;;
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
