#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT

# shellcheck source=../setup.sh
source "$ROOT/setup.sh"

ETC_ROOT="${TEST_ROOT}/etc/getbible-robot"
INSTANCE_ROOT="${TEST_ROOT}/opt/getbible-robot"
STATE_ROOT="${TEST_ROOT}/var/lib/getbible-robot"
CACHE_ROOT="${TEST_ROOT}/var/cache/getbible-robot"
LOG_ROOT="${TEST_ROOT}/var/log/getbible-robot"
METADATA_ROOT="${ETC_ROOT}/instances"
UNIT_PATH="${TEST_ROOT}/etc/systemd/system/getbible-robot@.service"
MANAGER_PATH="${TEST_ROOT}/usr/local/sbin/getbible-robot"
LOGROTATE_PATH="${TEST_ROOT}/etc/logrotate.d/getbible-robot"
SETUP_LOG="${LOG_ROOT}/setup.log"
CADDY_ROOT="${TEST_ROOT}/etc/caddy"
CADDYFILE="${CADDY_ROOT}/Caddyfile"
CADDY_ROUTES="${CADDY_ROOT}/getbible-robot.caddy"
UNIT_SOURCE="${ROOT}/deploy/getbible-robot@.service"

USERS_FILE="${TEST_ROOT}/users"
SERVICES_DIR="${TEST_ROOT}/services"
SOURCE_DIR="${TEST_ROOT}/source"
FOLLOW_MARKER="${TEST_ROOT}/follow"
RUNUSER_LOG="${TEST_ROOT}/runuser"
CADDY_LOG="${TEST_ROOT}/caddy.log"
DNS_LOG="${TEST_ROOT}/dns.log"
SYSTEM_PYTHON=$(command -v python3)
mkdir -p \
    "$METADATA_ROOT" \
    "$(dirname "$UNIT_PATH")" \
    "$(dirname "$MANAGER_PATH")" \
    "$(dirname "$LOGROTATE_PATH")" \
    "$CADDY_ROOT" \
    "$SERVICES_DIR"
: >"$USERS_FILE"
: >"$CADDY_LOG"
: >"$DNS_LOG"

fail() {
    printf 'lifecycle assertion failed: %s\n' "$*" >&2
    exit 1
}

assert_file() {
    [[ -f "$1" ]] || fail "missing file $1"
}

assert_dir() {
    [[ -d "$1" ]] || fail "missing directory $1"
}

assert_absent() {
    [[ ! -e "$1" ]] || fail "unexpected path $1"
}

assert_contains() {
    local file=$1
    local expected=$2
    grep -Fq -- "$expected" "$file" ||
        fail "$file does not contain $expected"
}

assert_equal() {
    [[ "$1" == "$2" ]] || fail "expected '$2', got '$1'"
}

assert_mode() {
    local path=$1
    local expected=$2
    local actual
    actual=$(command stat -c '%a' "$path")
    [[ "$actual" == "$expected" ]] ||
        fail "expected mode ${expected} for ${path}, got ${actual}"
}

service_state_file() {
    printf '%s/%s.state\n' "$SERVICES_DIR" "$1"
}

service_enabled_file() {
    printf '%s/%s.enabled\n' "$SERVICES_DIR" "$1"
}

require_root() {
    :
}

require_tty() {
    :
}

install_host_prerequisites() {
    :
}

host_capacity_preflight() {
    :
}

preflight_mini_app_dns() {
    printf '%s\n' "$2" >>"$DNS_LOG"
    [[ ${FAIL_DNS_PREFLIGHT:-0} != "1" ]]
}

ensure_caddy_available() {
    install -d -o root -g root -m 0755 "$CADDY_ROOT"
}

verify_mini_app_local() {
    [[ ${FAIL_MINI_APP_LOCAL:-0} != "1" ]]
}

verify_mini_app_public() {
    [[ ${FAIL_MINI_APP_PUBLIC:-0} != "1" ]]
}

select_python() {
    printf '%s\n' "$SYSTEM_PYTHON"
}

validate_environment() {
    local app_dir=$1
    local env_file=$2
    [[ -x "$app_dir/venv/bin/python" ]] || return 1
    assert_contains "$env_file" 'TELEGRAM_API_TOKEN="'
    assert_contains "$env_file" 'INSTANCE_NAME="'
    assert_contains "$env_file" 'LOG_FILE="'
}

dotenv_value() {
    local app_dir=$1
    local env_file=$2
    local key=$3
    : "$app_dir"
    sed -n \
        -e "s/^${key}=\"\\([^\"]*\\)\"$/\\1/p" \
        -e "s/^${key}=\\([^[:space:]]*\\)$/\\1/p" \
        "$env_file" | head -n 1
}

install_python_environment() {
    local app_dir=$1
    local python_bin=$2
    if [[ ${FAIL_PREPARE:-0} == "1" ]]; then
        die "Fixture application preparation failed."
    fi
    : "$python_bin"
    mkdir -p "$app_dir/venv/bin"
    ln -s "$SYSTEM_PYTHON" "$app_dir/venv/bin/python"
}

id() {
    local user=${1:-}
    if [[ "$user" == "-u" ]]; then
        printf '0\n'
        return
    fi
    grep -Fxq -- "$user" "$USERS_FILE"
}

useradd() {
    local user=${*: -1}
    grep -Fxq -- "$user" "$USERS_FILE" && return 9
    printf '%s\n' "$user" >>"$USERS_FILE"
}

userdel() {
    local user=$1
    local replacement="${USERS_FILE}.new"
    grep -Fxv -- "$user" "$USERS_FILE" >"$replacement" || true
    mv -- "$replacement" "$USERS_FILE"
}

passwd() {
    :
}

chown() {
    :
}

telegram_delivery_status() {
    printf 'Telegram delivery fixture: synchronized\n'
}

delete_telegram_webhook() {
    :
}

runuser() {
    [[ ${1:-} == "--user" && -n ${2:-} ]] ||
        fail "unexpected runuser arguments: $*"
    local user=$2
    shift 2
    [[ ${1:-} == "--" ]] || fail "runuser command separator is missing"
    shift
    printf '%s\n' "$user" >>"$RUNUSER_LOG"
    "$@"
}

install() {
    local args=()
    while (($# > 0)); do
        case "$1" in
            -o|-g)
                shift 2
                ;;
            *)
                args+=("$1")
                shift
                ;;
        esac
    done
    command install "${args[@]}"
}

stat() {
    if [[ ${1:-} == "-c" && ${2:-} == "%U:%G:%a" ]]; then
        case "$3" in
            "$ETC_ROOT"/*.env)
                printf 'root:root:600\n'
                ;;
            "$LOG_ROOT"/*.jsonl)
                local instance
                instance=$(basename "$3" .jsonl)
                printf 'gb-%s:gb-%s:640\n' "$instance" "$instance"
                ;;
            "$ETC_ROOT"/*.welcome.txt|"$ETC_ROOT"/*.help.txt)
                local instance
                instance=$(basename "$3")
                instance=${instance%%.*}
                printf 'root:gb-%s:640\n' "$instance"
                ;;
            *)
                command stat "$@"
                ;;
        esac
        return
    fi
    command stat "$@"
}

ss() {
    local instance
    local app_dir
    local env_file
    local port
    while IFS= read -r instance; do
        app_dir=$(application_dir_for "$instance")
        env_file=$(environment_file_for "$instance")
        [[ -x "$app_dir/venv/bin/python" && -f "$env_file" ]] || continue
        [[ "$(dotenv_value "$app_dir" "$env_file" "MINI_APP_ENABLED")" == "true" ]] ||
            continue
        [[ "$(<"$(service_state_file "$(service_name_for "$instance")")")" == "active" ]] ||
            continue
        port=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PORT")
        printf 'LISTEN 0 128 127.0.0.1:%s\n' "$port"
    done < <(instance_names)
}

tail() {
    local argument
    for argument in "$@"; do
        if [[ "$argument" == "-F" ]]; then
            printf '%s\n' "${*: -1}" >"$FOLLOW_MARKER"
            return
        fi
    done
    command tail "$@"
}

systemd-analyze() {
    [[ ${1:-} == "verify" ]] || return 1
}

caddy() {
    printf '%s\n' "$*" >>"$CADDY_LOG"
    [[ ${FAIL_CADDY_VALIDATE:-0} != "1" ]]
}

systemctl() {
    local command=${1:-}
    shift || true
    case "$command" in
        daemon-reload|reset-failed)
            return
            ;;
        enable)
            local start_now="false"
            if [[ ${1:-} == "--now" ]]; then
                shift
                start_now="true"
            fi
            local service=$1
            : >"$(service_enabled_file "$service")"
            if [[ "$start_now" == "true" ]]; then
                printf 'active\n' >"$(service_state_file "$service")"
            fi
            ;;
        disable)
            if [[ ${1:-} == "--now" ]]; then
                shift
            fi
            local service=$1
            rm -f -- "$(service_enabled_file "$service")"
            printf 'inactive\n' >"$(service_state_file "$service")"
            ;;
        start|restart)
            local service=$1
            if [[ ${FAIL_NEXT_START:-} == "$service" ]]; then
                unset FAIL_NEXT_START
                printf 'failed\n' >"$(service_state_file "$service")"
                return 1
            fi
            printf 'active\n' >"$(service_state_file "$service")"
            ;;
        reload)
            local service=$1
            if [[ ${FAIL_NEXT_RELOAD:-} == "$service" ]]; then
                unset FAIL_NEXT_RELOAD
                return 1
            fi
            [[ "$(<"$(service_state_file "$service")")" == "active" ]]
            ;;
        stop)
            printf 'inactive\n' >"$(service_state_file "$1")"
            ;;
        is-active)
            if [[ ${1:-} == "--quiet" ]]; then
                shift
            fi
            local state="inactive"
            [[ -f "$(service_state_file "$1")" ]] &&
                state=$(<"$(service_state_file "$1")")
            if [[ "$state" == "active" ]]; then
                [[ ${1:-} == "--quiet" ]] || printf 'active\n'
                return
            fi
            printf '%s\n' "$state"
            return 3
            ;;
        is-enabled)
            if [[ ${1:-} == "--quiet" ]]; then
                shift
            fi
            if [[ -f "$(service_enabled_file "$1")" ]]; then
                [[ ${2:-} == "--quiet" ]] || printf 'enabled\n'
                return
            fi
            [[ ${2:-} == "--quiet" ]] || printf 'disabled\n'
            return 1
            ;;
        status)
            printf 'fixture service %s\n' "$1"
            [[ "$(<"$(service_state_file "$1")")" == "active" ]]
            ;;
        show)
            printf 'ActiveState=active\n'
            printf 'SubState=running\n'
            printf 'MainPID=1234\n'
            printf 'NRestarts=0\n'
            printf 'MemoryCurrent=1024\n'
            printf 'MemoryPeak=2048\n'
            printf 'MemoryMax=67108864\n'
            printf 'TasksCurrent=1\n'
            printf 'TasksMax=64\n'
            ;;
        *)
            fail "unexpected systemctl command: $command $*"
            ;;
    esac
}

create_source_fixture() {
    mkdir -p "$SOURCE_DIR/deploy"
    cp -- "$ROOT/setup.sh" "$SOURCE_DIR/setup.sh"
    cp -- "$ROOT/deploy/getbible-robot@.service" \
        "$SOURCE_DIR/deploy/getbible-robot@.service"
    cp -- "$ROOT/deploy/welcome.txt" "$SOURCE_DIR/deploy/welcome.txt"
    cp -- "$ROOT/deploy/help.txt" "$SOURCE_DIR/deploy/help.txt"
    cp -- "$ROOT/.env.template" "$SOURCE_DIR/.env.template"
    printf 'print("fixture")\n' >"$SOURCE_DIR/bot.py"
    printf 'class Settings:\n    pass\n' >"$SOURCE_DIR/config.py"
    : >"$SOURCE_DIR/requirements.txt"
    git -C "$SOURCE_DIR" init --quiet
    git -C "$SOURCE_DIR" config user.name "Setup Lifecycle Test"
    git -C "$SOURCE_DIR" config user.email "setup-test@getbible.local"
    git -C "$SOURCE_DIR" add .
    git -C "$SOURCE_DIR" commit --quiet -m "fixture v1"
}

install_instance() {
    local instance=$1
    local token=$2
    cmd_install --source "$SOURCE_DIR" < <(
        printf '%s\n' \
            "" \
            "$instance" \
            "$token" \
            "$token" \
            "" \
            "" \
            "" \
            "" \
            "" \
            "n" \
            "0" \
            "" \
            "" \
            "" \
            "" \
            "n"
    )
}

commit_fixture_version() {
    local version=$1
    printf 'print("fixture %s")\n' "$version" >"$SOURCE_DIR/bot.py"
    git -C "$SOURCE_DIR" add bot.py
    git -C "$SOURCE_DIR" commit --quiet -m "fixture $version"
    git -C "$SOURCE_DIR" rev-parse HEAD
}

create_source_fixture
FIRST_SHA=$(git -C "$SOURCE_DIR" rev-parse HEAD)

install_instance \
    "alpha" \
    "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
install_instance \
    "beta" \
    "987654321:ZYXWVUTSRQPONMLKJIHGFEDCBAabcdefghi"

assert_file "$(metadata_file_for alpha)"
assert_file "$(metadata_file_for beta)"
assert_file "$(environment_file_for alpha)"
assert_file "$(environment_file_for beta)"
assert_file "$(welcome_file_for alpha)"
assert_file "$(help_file_for alpha)"
assert_file "$(welcome_file_for beta)"
assert_file "$(help_file_for beta)"
assert_file "$(log_file_for alpha)"
assert_file "$(log_file_for beta)"
assert_file "$MANAGER_PATH"
assert_file "$UNIT_PATH"
assert_file "$(resource_dropin_for alpha)"
assert_contains "$(resource_dropin_for alpha)" "MemoryMax=2048M"
assert_contains "$(resource_dropin_for alpha)" "CPUQuota=200%"
assert_file "$LOGROTATE_PATH"
assert_dir "$(application_dir_for alpha)"
assert_dir "$(application_dir_for beta)"
assert_mode "$(application_dir_for alpha)" "750"
assert_mode "$(application_dir_for alpha)/bot.py" "640"
assert_mode "$(application_dir_for alpha)/venv" "750"
assert_contains "$RUNUSER_LOG" "gb-alpha"
assert_contains "$RUNUSER_LOG" "gb-beta"
assert_contains "$(environment_file_for alpha)" 'INSTANCE_NAME="alpha"'
assert_contains "$(environment_file_for beta)" 'INSTANCE_NAME="beta"'
assert_contains "$(environment_file_for alpha)" 'AUDIT_LOG_MODE="metadata"'
assert_contains "$(environment_file_for alpha)" \
    "USER_PREFERENCES_FILE=\"${STATE_ROOT}/alpha/preferences.sqlite3\""
assert_contains "$(environment_file_for alpha)" 'USER_PREFERENCE_LIMIT="10000"'
assert_contains "$(environment_file_for beta)" 'HEALTH_PORT="0"'
assert_contains "$(environment_file_for alpha)" 'TELEGRAM_DELIVERY_MODE="polling"'
assert_contains "$(environment_file_for alpha)" \
    'GETBIBLE_MAX_RESPONSE_BYTES="41943040"'
assert_contains "$(environment_file_for alpha)" \
    'SEARCH_MAX_RESPONSE_BYTES="4194304"'
assert_contains "$(help_file_for alpha)" "/search"

cmd_install --source "$SOURCE_DIR" <<EOF

gamma
555555555:ABCDEFGHIJKLMNOPQRSTUVWXYZa12345678
555555555:ABCDEFGHIJKLMNOPQRSTUVWXYZa12345678





y
https://bot.example.com/getbible/gamma

0




n
EOF
assert_contains "$(environment_file_for gamma)" 'MINI_APP_ENABLED="true"'
assert_contains "$CADDY_ROUTES" "@gb_gamma_static path /getbible/gamma /getbible/gamma/"
assert_file "$(service_enabled_file "$(service_name_for gamma)")"
cmd_uninstall gamma <<EOF
gamma
y
y
EOF
assert_absent "$(metadata_file_for gamma)"
! grep -Fq "/getbible/gamma" "$CADDY_ROUTES" ||
    fail "fresh-install Mini App route survived uninstall"

assert_equal "$(wc -l <"$USERS_FILE")" "2"
! ensure_unique_token \
    "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi" \
    "beta" || fail "duplicate token was accepted"

if (
    trap cleanup EXIT
    FAIL_PREPARE=1
    export FAIL_PREPARE
    install_instance \
        "broken" \
        "111111111:ABCDEFGHIJKLMNOPQRSTUVWXabcdefghi"
)
then
    fail "an incomplete installation was reported as successful"
fi
assert_absent "$(metadata_file_for broken)"
assert_absent "$(environment_file_for broken)"
assert_absent "${INSTANCE_ROOT}/broken"
assert_absent "${CACHE_ROOT}/broken"
assert_absent "${STATE_ROOT}/broken"
! grep -Fxq -- "gb-broken" "$USERS_FILE" ||
    fail "failed installation account was retained"

LIST_OUTPUT=$(cmd_list)
[[ "$LIST_OUTPUT" == *"alpha"* && "$LIST_OUTPUT" == *"beta"* ]] ||
    fail "list did not show both instances"
STATUS_OUTPUT=$(cmd_status alpha)
[[ "$STATUS_OUTPUT" == *"Readiness:     disabled"* ]] ||
    fail "disabled readiness was not reported"
RUNTIME_OUTPUT=$(cmd_runtime alpha)
[[ "$RUNTIME_OUTPUT" == *"Dependency check:"* ]] ||
    fail "runtime diagnostics were not exercised"
printf '{"event":"fixture"}\n' >>"$(log_file_for alpha)"
assert_equal "$(cmd_logs alpha 1)" '{"event":"fixture"}'
cmd_follow alpha
assert_equal "$(<"$FOLLOW_MARKER")" "$(log_file_for alpha)"
cmd_doctor alpha
chmod 0700 "$(application_dir_for alpha)"
cmd_repair alpha
assert_mode "$(application_dir_for alpha)" "750"
assert_equal "$(<"$(service_state_file "$(service_name_for alpha)")")" "active"
cmd_stop alpha
assert_equal "$(<"$(service_state_file "$(service_name_for alpha)")")" "inactive"
cmd_start alpha
assert_equal "$(<"$(service_state_file "$(service_name_for alpha)")")" "active"
assert_file "$(service_enabled_file "$(service_name_for alpha)")"
cmd_restart alpha
assert_equal "$(<"$(service_state_file "$(service_name_for alpha)")")" "active"

SELECTED_OUTPUT=$(cmd_status <<EOF
2
EOF
)
[[ "$SELECTED_OUTPUT" == *"Instance:      beta"* ]] ||
    fail "interactive instance selection did not select beta"
MENU_OUTPUT=$(cmd_menu <<EOF
0
EOF
)
[[ "$MENU_OUTPUT" == *"GetBible Robot operations"* ]] ||
    fail "interactive operations menu did not render"

BAD_EDITOR="${TEST_ROOT}/bad-editor"
cat >"$BAD_EDITOR" <<'EOF'
#!/usr/bin/env bash
sed -i 's/^INSTANCE_NAME=.*/INSTANCE_NAME="tampered"/' "$1"
EOF
chmod 0700 "$BAD_EDITOR"
if (
    EDITOR="$BAD_EDITOR" cmd_config alpha <<EOF
n
EOF
)
then
    fail "manager-owned configuration tampering was accepted"
fi
assert_contains "$(environment_file_for alpha)" 'INSTANCE_NAME="alpha"'

GOOD_EDITOR="${TEST_ROOT}/good-editor"
cat >"$GOOD_EDITOR" <<'EOF'
#!/usr/bin/env bash
sed -i 's/^TRANSLATION=.*/TRANSLATION="asv"/' "$1"
EOF
chmod 0700 "$GOOD_EDITOR"
EDITOR="$GOOD_EDITOR" cmd_config alpha <<EOF
n
EOF
assert_contains "$(environment_file_for alpha)" 'TRANSLATION="asv"'

MINI_APP_EDITOR="${TEST_ROOT}/mini-app-editor"
cat >"$MINI_APP_EDITOR" <<'EOF'
#!/usr/bin/env bash
sed -i 's/^MINI_APP_PORT=.*/MINI_APP_PORT="9299"/' "$1"
EOF
chmod 0700 "$MINI_APP_EDITOR"
if (
    EDITOR="$MINI_APP_EDITOR" cmd_config alpha <<EOF
n
EOF
)
then
    fail "manager-owned Mini App routing tampering was accepted"
fi
assert_contains "$(environment_file_for alpha)" 'MINI_APP_PORT="9201"'

ENV_HASH=$(sha256sum "$(environment_file_for alpha)" | awk '{print $1}')
export FAIL_DNS_PREFLIGHT
FAIL_DNS_PREFLIGHT=1
if (
    cmd_miniapp alpha <<EOF
y
https://bot.example.com/getbible/alpha
9201
EOF
)
then
    fail "a failed Mini App DNS preflight was reported as successful"
fi
unset FAIL_DNS_PREFLIGHT
assert_equal "$(sha256sum "$(environment_file_for alpha)" | awk '{print $1}')" "$ENV_HASH"

cmd_miniapp alpha <<EOF
y
https://bot.example.com/getbible/alpha
9201
EOF
assert_contains "$(environment_file_for alpha)" 'MINI_APP_ENABLED="true"'
assert_contains "$(environment_file_for alpha)" \
    'MINI_APP_PUBLIC_URL="https://bot.example.com/getbible/alpha"'
assert_contains "$(environment_file_for alpha)" 'MINI_APP_PORT="9201"'
assert_file "$CADDYFILE"
assert_file "$CADDY_ROUTES"
assert_contains "$CADDYFILE" "$CADDY_IMPORT_BEGIN"
assert_contains "$CADDY_ROUTES" "bot.example.com {"
assert_contains "$CADDY_ROUTES" "@gb_alpha_static path /getbible/alpha /getbible/alpha/"
assert_contains "$CADDY_ROUTES" "/getbible/alpha/api/v1/session"
assert_contains "$CADDY_ROUTES" '/getbible/alpha/api/v1/post'
assert_contains "$CADDY_ROUTES" 'respond "" 404'
assert_contains "$CADDY_ROUTES" "reverse_proxy 127.0.0.1:9201"
assert_equal "$(grep -Fc "$CADDY_IMPORT_BEGIN" "$CADDYFILE")" "1"
assert_file "$(service_enabled_file caddy.service)"
cmd_doctor alpha

cmd_miniapp alpha <<EOF
n
EOF
assert_contains "$(environment_file_for alpha)" 'MINI_APP_ENABLED="false"'
assert_equal "$(next_mini_app_port)" "9202"

cmd_miniapp alpha <<EOF
y
https://bot.example.com/getbible/alpha
9201
EOF
assert_equal "$(grep -Fc "$CADDY_IMPORT_BEGIN" "$CADDYFILE")" "1"
assert_equal "$(grep -Fc "reverse_proxy 127.0.0.1:9201" "$CADDY_ROUTES")" "3"

CADDYFILE_HASH=$(sha256sum "$CADDYFILE" | awk '{print $1}')
CADDY_ROUTES_HASH=$(sha256sum "$CADDY_ROUTES" | awk '{print $1}')
ENV_HASH=$(sha256sum "$(environment_file_for alpha)" | awk '{print $1}')
export FAIL_MINI_APP_PUBLIC
FAIL_MINI_APP_PUBLIC=1
if (
    cmd_miniapp alpha <<EOF
y
https://bot.example.com/getbible/alpha
9201
EOF
)
then
    fail "a failed public Mini App HTTPS probe was reported as successful"
fi
unset FAIL_MINI_APP_PUBLIC
assert_equal "$(sha256sum "$CADDYFILE" | awk '{print $1}')" "$CADDYFILE_HASH"
assert_equal "$(sha256sum "$CADDY_ROUTES" | awk '{print $1}')" "$CADDY_ROUTES_HASH"
assert_equal "$(sha256sum "$(environment_file_for alpha)" | awk '{print $1}')" "$ENV_HASH"

export FAIL_NEXT_RELOAD
FAIL_NEXT_RELOAD=caddy.service
if (
    cmd_miniapp alpha <<EOF
y
https://bot.example.com/getbible/alpha
9201
EOF
)
then
    fail "a failed Caddy reload was reported as successful"
fi
unset FAIL_NEXT_RELOAD
assert_equal "$(sha256sum "$CADDYFILE" | awk '{print $1}')" "$CADDYFILE_HASH"
assert_equal "$(sha256sum "$CADDY_ROUTES" | awk '{print $1}')" "$CADDY_ROUTES_HASH"
assert_equal "$(sha256sum "$(environment_file_for alpha)" | awk '{print $1}')" "$ENV_HASH"

cmd_miniapp alpha <<EOF
n
EOF
assert_contains "$(environment_file_for alpha)" 'MINI_APP_ENABLED="false"'

CONTENT_EDITOR="${TEST_ROOT}/content-editor"
cat >"$CONTENT_EDITOR" <<'EOF'
#!/usr/bin/env bash
printf '\nFixture operator help.\n' >>"$1"
EOF
chmod 0700 "$CONTENT_EDITOR"
EDITOR="$CONTENT_EDITOR" cmd_content alpha help <<EOF
n
EOF
assert_contains "$(help_file_for alpha)" "Fixture operator help."

cmd_delivery alpha <<EOF
webhook
https://bot.example.com/telegram/alpha
127.0.0.1
9101

y
EOF
assert_contains "$(environment_file_for alpha)" \
    'TELEGRAM_DELIVERY_MODE="webhook"'
assert_contains "$(environment_file_for alpha)" \
    'TELEGRAM_WEBHOOK_PUBLIC_URL="https://bot.example.com/telegram/alpha"'
assert_contains "$(environment_file_for alpha)" \
    'TELEGRAM_WEBHOOK_LISTEN="127.0.0.1"'
assert_contains "$(environment_file_for alpha)" \
    'TELEGRAM_WEBHOOK_PORT="9101"'

export FAIL_NEXT_START
FAIL_NEXT_START=$(service_name_for alpha)
if (
    cmd_delivery alpha <<EOF
polling
EOF
)
then
    fail "a failed delivery restart was reported as successful"
fi
unset FAIL_NEXT_START
assert_contains "$(environment_file_for alpha)" \
    'TELEGRAM_DELIVERY_MODE="webhook"'

cmd_delivery alpha <<EOF
polling
EOF
assert_contains "$(environment_file_for alpha)" \
    'TELEGRAM_DELIVERY_MODE="polling"'
assert_contains "$(environment_file_for alpha)" 'MINI_APP_ENABLED="false"'
assert_contains "$(environment_file_for alpha)" \
    'MINI_APP_LISTEN="127.0.0.1"'
assert_contains "$(environment_file_for alpha)" 'MINI_APP_PORT="9201"'
assert_contains "$(environment_file_for alpha)" \
    'MINI_APP_INIT_DATA_MAX_AGE_SECONDS="300"'
assert_contains "$(environment_file_for alpha)" \
    'MINI_APP_LAUNCH_TTL_SECONDS="300"'

SECOND_SHA=$(commit_fixture_version v2)
cmd_upgrade alpha --source "$SOURCE_DIR" <<EOF

EOF
load_instance alpha
assert_equal "$ACTIVE_SHA" "$SECOND_SHA"
assert_equal "$(git -C "$(application_dir_for alpha)" rev-parse HEAD)" "$SECOND_SHA"
assert_equal \
    "$(git -C "${INSTANCE_ROOT}/alpha/app.previous" rev-parse HEAD)" \
    "$FIRST_SHA"

cmd_rollback alpha <<EOF
y
EOF
load_instance alpha
assert_equal "$ACTIVE_SHA" "$FIRST_SHA"
assert_equal "$(git -C "$(application_dir_for alpha)" rev-parse HEAD)" "$FIRST_SHA"
assert_equal \
    "$(git -C "${INSTANCE_ROOT}/alpha/app.previous" rev-parse HEAD)" \
    "$SECOND_SHA"

THIRD_SHA=$(commit_fixture_version v3)
export FAIL_NEXT_START
FAIL_NEXT_START=$(service_name_for alpha)
if (
    cmd_upgrade alpha --source "$SOURCE_DIR" <<EOF

EOF
)
then
    fail "failed upgraded service was reported as successful"
fi
unset FAIL_NEXT_START
load_instance alpha
assert_equal "$ACTIVE_SHA" "$FIRST_SHA"
assert_equal "$(git -C "$(application_dir_for alpha)" rev-parse HEAD)" "$FIRST_SHA"
[[ "$THIRD_SHA" != "$ACTIVE_SHA" ]] ||
    fail "failed upgrade metadata was retained"

cmd_miniapp alpha <<EOF
y
https://bot.example.com/getbible/alpha
9201
EOF
cmd_miniapp beta <<EOF
y
https://bot.example.com/getbible/beta
9202
EOF
assert_contains "$CADDY_ROUTES" "@gb_beta_static path /getbible/beta /getbible/beta/"
cmd_uninstall beta <<EOF
beta
y
y
EOF
assert_absent "$(metadata_file_for beta)"
assert_absent "$(environment_file_for beta)"
assert_absent "$(application_dir_for beta)"
assert_absent "$(welcome_file_for beta)"
assert_absent "$(help_file_for beta)"
assert_absent "$(log_file_for beta)"
! grep -Fq "/getbible/beta" "$CADDY_ROUTES" ||
    fail "uninstall retained the removed instance's Caddy route"
assert_contains "$CADDY_ROUTES" "@gb_alpha_static path /getbible/alpha /getbible/alpha/"
grep -Fxq -- "gb-alpha" "$USERS_FILE" || fail "alpha account was removed"
! grep -Fxq -- "gb-beta" "$USERS_FILE" || fail "beta account was retained"
assert_file "$(metadata_file_for alpha)"

assert_contains "$SETUP_LOG" "action=install"
assert_contains "$SETUP_LOG" "action=upgrade"
assert_contains "$SETUP_LOG" "result=rolled-back"
assert_contains "$SETUP_LOG" "action=rollback"
assert_contains "$SETUP_LOG" "action=uninstall"

printf 'Setup manager lifecycle test passed.\n'
