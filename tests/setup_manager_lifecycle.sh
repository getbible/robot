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
MINI_APP_VERIFY_LOG="${TEST_ROOT}/mini-app-verify.log"
CONTRIBUTION_STORE_VERIFY_LOG="${TEST_ROOT}/contribution-store-verify.log"
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
: >"$MINI_APP_VERIFY_LOG"
: >"$CONTRIBUTION_STORE_VERIFY_LOG"

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

# The lifecycle sources the manager so it can replace host primitives with
# fixtures. A real installed manager execs the reviewed checkout here; keep the
# in-process harness on the same function definitions instead.
handoff_upgrade_to_target_manager() {
    :
}

install_host_prerequisites() {
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
    local app_dir=$1
    local env_file=$2
    local base_url
    local public_url
    local public_path
    local listen
    local port
    public_url=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PUBLIC_URL")
    public_path=${public_url#https://}
    if [[ "$public_path" == */* ]]; then
        public_path=/${public_path#*/}
    else
        public_path=""
    fi
    listen=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_LISTEN")
    [[ "$listen" != "0.0.0.0" ]] || listen="127.0.0.1"
    port=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PORT")
    base_url="http://${listen}:${port}${public_path%/}/"
    printf 'local GET %sapi/v1/bookmarks/catalog\n' \
        "$base_url" >>"$MINI_APP_VERIFY_LOG"
    printf 'local GET %sapi/v1/contributions/status\n' \
        "$base_url" >>"$MINI_APP_VERIFY_LOG"
    printf 'local POST %sapi/v1/contributions/events\n' \
        "$base_url" >>"$MINI_APP_VERIFY_LOG"
    [[ ${FAIL_MINI_APP_LOCAL:-0} != "1" ]]
}

verify_mini_app_public() {
    local app_dir=$1
    local env_file=$2
    local base_url
    base_url=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PUBLIC_URL")
    base_url="${base_url%/}/"
    printf 'public GET %sapi/v1/bookmarks/catalog\n' \
        "$base_url" >>"$MINI_APP_VERIFY_LOG"
    printf 'public GET %sapi/v1/contributions/status\n' \
        "$base_url" >>"$MINI_APP_VERIFY_LOG"
    printf 'public POST %sapi/v1/contributions/events\n' \
        "$base_url" >>"$MINI_APP_VERIFY_LOG"
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

verify_contribution_store_access() {
    printf '%s\t%s\t%s\n' "$1" "$2" "$3" >>"$CONTRIBUTION_STORE_VERIFY_LOG"
    [[ ${FAIL_CONTRIBUTION_STORE_VERIFY:-0} != "1" ]]
}

verify_contribution_store_readonly() {
    verify_contribution_store_access "$@"
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
    local listen
    local port
    while IFS= read -r instance; do
        app_dir=$(application_dir_for "$instance")
        env_file=$(environment_file_for "$instance")
        [[ -x "$app_dir/venv/bin/python" && -f "$env_file" ]] || continue
        [[ "$(dotenv_value "$app_dir" "$env_file" "MINI_APP_ENABLED")" == "true" ]] ||
            continue
        [[ "$(<"$(service_state_file "$(service_name_for "$instance")")")" == "active" ]] ||
            continue
        listen=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_LISTEN")
        port=$(dotenv_value "$app_dir" "$env_file" "MINI_APP_PORT")
        printf 'LISTEN 0 128 %s:%s\n' "${listen:-127.0.0.1}" "$port"
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
            printf 'systemctl reload %s\n' "$service" >>"$CADDY_LOG"
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
    mkdir -p \
        "$SOURCE_DIR/deploy" \
        "$SOURCE_DIR/scripts/lib" \
        "$SOURCE_DIR/data/global-bookmarks" \
        "$SOURCE_DIR/miniapp/lib"
    cp -- "$ROOT/setup.sh" "$SOURCE_DIR/setup.sh"
    cp -- "$ROOT/deploy/getbible-robot@.service" \
        "$SOURCE_DIR/deploy/getbible-robot@.service"
    cp -- "$ROOT/deploy/welcome.txt" "$SOURCE_DIR/deploy/welcome.txt"
    cp -- "$ROOT/deploy/help.txt" "$SOURCE_DIR/deploy/help.txt"
    cp -- "$ROOT/.env.template" "$SOURCE_DIR/.env.template"
    cp -- "$ROOT/scripts/contribution_review.py" \
        "$SOURCE_DIR/scripts/contribution_review.py"
    cp -- "$ROOT/scripts/import_contribution_bundle.mjs" \
        "$SOURCE_DIR/scripts/import_contribution_bundle.mjs"
    cp -- "$ROOT/scripts/lib/global_bookmark_sources.mjs" \
        "$SOURCE_DIR/scripts/lib/global_bookmark_sources.mjs"
    cp -- "$ROOT/data/global-bookmarks/topics.json" \
        "$SOURCE_DIR/data/global-bookmarks/topics.json"
    cp -- "$ROOT/data/global-bookmarks/tag-verse.csv" \
        "$SOURCE_DIR/data/global-bookmarks/tag-verse.csv"
    cp -- "$ROOT/miniapp/lib/bible-canon.js" \
        "$SOURCE_DIR/miniapp/lib/bible-canon.js"
    printf 'print("fixture")\n' >"$SOURCE_DIR/bot.py"
    printf 'class Settings:\n    pass\n' >"$SOURCE_DIR/config.py"
    : >"$SOURCE_DIR/requirements.txt"
    git -C "$SOURCE_DIR" init --quiet
    git -C "$SOURCE_DIR" config user.name "Setup Lifecycle Test"
    git -C "$SOURCE_DIR" config user.email "setup-test@getbible.local"
    git -C "$SOURCE_DIR" add .
    git -C "$SOURCE_DIR" commit --quiet -m "fixture v1"
}

assert_contribution_assets() {
    local app_dir=$1
    assert_file "$app_dir/scripts/contribution_review.py"
    assert_file "$app_dir/scripts/import_contribution_bundle.mjs"
    assert_file "$app_dir/scripts/lib/global_bookmark_sources.mjs"
    assert_file "$app_dir/data/global-bookmarks/topics.json"
    assert_file "$app_dir/data/global-bookmarks/tag-verse.csv"
    assert_file "$app_dir/miniapp/lib/bible-canon.js"
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
assert_contribution_assets "$(application_dir_for alpha)"
assert_contribution_assets "$(application_dir_for beta)"
assert_mode "$(application_dir_for alpha)" "750"
assert_mode "$(application_dir_for alpha)/bot.py" "640"
assert_mode "$(application_dir_for alpha)/scripts/contribution_review.py" "640"
assert_mode "$(application_dir_for alpha)/data/global-bookmarks/topics.json" "640"
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
assert_contains "$(environment_file_for alpha)" \
    'MINI_APP_SESSION_TTL_SECONDS="7776000"'

# A legacy file may validly omit the setting and rely on the old code default.
# Fill that case with the old-safe raw value so either application tree boots.
MISSING_TTL_ENV="${TEST_ROOT}/missing-session-ttl.env"
cp -- "$(environment_file_for alpha)" "$MISSING_TTL_ENV"
sed -i '/^MINI_APP_SESSION_TTL_SECONDS=/d' "$MISSING_TTL_ENV"
migrate_instance_configuration \
    "$SOURCE_DIR" "$SYSTEM_PYTHON" "$MISSING_TTL_ENV" \
    "gb-alpha" "alpha"
assert_contains "$MISSING_TTL_ENV" 'MINI_APP_SESSION_TTL_SECONDS="900"'

# Released managers persisted the former session range in the instance file.
# Migration must leave those bytes rollback-compatible; the new runtime maps
# them to its ninety-day effective lifetime when it loads the environment.
replace_env_value "$SYSTEM_PYTHON" "$(environment_file_for alpha)" \
    "MINI_APP_SESSION_TTL_SECONDS" "900"
replace_env_value "$SYSTEM_PYTHON" "$(environment_file_for beta)" \
    "MINI_APP_SESSION_TTL_SECONDS" "1800"
migrate_instance_configuration \
    "$SOURCE_DIR" "$SYSTEM_PYTHON" "$(environment_file_for alpha)" \
    "gb-alpha" "alpha"
migrate_instance_configuration \
    "$SOURCE_DIR" "$SYSTEM_PYTHON" "$(environment_file_for beta)" \
    "gb-beta" "beta"
assert_contains "$(environment_file_for alpha)" \
    'MINI_APP_SESSION_TTL_SECONDS="900"'
assert_contains "$(environment_file_for beta)" \
    'MINI_APP_SESSION_TTL_SECONDS="1800"'

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
assert_contains "$CADDY_ROUTES" 'path /getbible/alpha/api/v1/*'
assert_contains "$CADDY_ROUTES" '/getbible/alpha/api/v1/bookmarks/backup'
if grep -Fq '/getbible/alpha/api/v1/contributions/sync' "$CADDY_ROUTES"; then
    fail "managed Caddy routes still carry the retired contribution sync matcher"
fi
if grep -Fq 'max_size 1MiB' "$CADDY_ROUTES"; then
    fail "managed Caddy routes still carry the retired 1MiB body budget"
fi
assert_contains "$CADDY_ROUTES" 'max_size 5MB'
assert_contains "$CADDY_ROUTES" 'respond "" 404'
assert_contains "$CADDY_ROUTES" "reverse_proxy 127.0.0.1:9201"
assert_equal "$(grep -Fc "$CADDY_IMPORT_BEGIN" "$CADDYFILE")" "1"
assert_file "$(service_enabled_file caddy.service)"
cmd_doctor alpha

# A manually selected Mini App port remains reserved even when the owning bot
# is stopped. Initial install must reject it without relying on a live socket.
cmd_stop alpha
if (
    cmd_install \
        --source "$SOURCE_DIR" \
        --reverse-proxy external \
        --mini-app-port 9201 <<EOF

delta
246813579:ABCDEFGHIJKLMNOPQRSTUVWXYZabcde1234
246813579:ABCDEFGHIJKLMNOPQRSTUVWXYZabcde1234





y
https://bot.example.com/getbible/delta
EOF
)
then
    fail "an already assigned Mini App port was accepted during install"
fi
assert_absent "$(metadata_file_for delta)"
assert_absent "$(environment_file_for delta)"
cmd_start alpha

CADDY_ROUTES_HASH=$(sha256sum "$CADDY_ROUTES" | awk '{print $1}')
cmd_install \
    --source "$SOURCE_DIR" \
    --reverse-proxy external \
    --mini-app-port 9250 <<EOF

delta
246813579:ABCDEFGHIJKLMNOPQRSTUVWXYZabcde1234
246813579:ABCDEFGHIJKLMNOPQRSTUVWXYZabcde1234





y
https://bot.example.com/getbible/delta
0


n
y
n
EOF
assert_contains "$(environment_file_for delta)" 'MINI_APP_ENABLED="true"'
assert_contains "$(environment_file_for delta)" 'REVERSE_PROXY_MODE="external"'
assert_contains "$(environment_file_for delta)" 'MINI_APP_LISTEN="0.0.0.0"'
assert_contains "$(environment_file_for delta)" 'MINI_APP_PORT="9250"'
assert_equal "$(sha256sum "$CADDY_ROUTES" | awk '{print $1}')" \
    "$CADDY_ROUTES_HASH"
cmd_start delta
cmd_doctor delta
cmd_uninstall delta <<EOF
delta
y
y
EOF
assert_absent "$(metadata_file_for delta)"
assert_equal "$(sha256sum "$CADDY_ROUTES" | awk '{print $1}')" \
    "$CADDY_ROUTES_HASH"

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

# A failed public self-probe with a healthy local surface is a hairpin-NAT
# explanation, not a diagnostic failure: outside devices are unaffected and
# the operator is told how to verify from one.
export FAIL_MINI_APP_PUBLIC
FAIL_MINI_APP_PUBLIC=1
HAIRPIN_DOCTOR_OUTPUT=$(cmd_doctor alpha 2>&1) ||
    fail "doctor treated a hairpin-only public probe failure as a problem"
unset FAIL_MINI_APP_PUBLIC
[[ "$HAIRPIN_DOCTOR_OUTPUT" == *"could not reach its own public Mini App URL"* ]] || {
    printf 'DOCTOR OUTPUT:\n%s\n' "$HAIRPIN_DOCTOR_OUTPUT" >&2
    fail "doctor did not explain the failed public self-probe"
}
[[ "$HAIRPIN_DOCTOR_OUTPUT" == *"hairpin NAT"* ]] ||
    fail "doctor did not name the hairpin cause"
[[ "$HAIRPIN_DOCTOR_OUTPUT" == *"All deployment diagnostics passed."* ]] ||
    fail "doctor did not report overall success for a hairpin-only warning"

# A broken local surface remains a hard diagnostic failure even when the
# public probe fails for the same reason.
export FAIL_MINI_APP_LOCAL FAIL_MINI_APP_PUBLIC
FAIL_MINI_APP_LOCAL=1
FAIL_MINI_APP_PUBLIC=1
if (cmd_doctor alpha > /dev/null 2>&1); then
    fail "doctor ignored a failed local Mini App surface"
fi
unset FAIL_MINI_APP_LOCAL FAIL_MINI_APP_PUBLIC

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

cmd_miniapp alpha <<EOF
y
https://bot.example.com/getbible/alpha
9201
EOF

# Routine restart is also a repair boundary for generated managed routes.
sed -i \
    -e 's#path /getbible/alpha/api/v1/[*]#path /getbible/alpha/api/v0/*#' \
    "$CADDY_ROUTES"
RESTART_CADDY_RELOADS=$(
    grep -Fc 'systemctl reload caddy.service' "$CADDY_LOG" || true
)
cmd_restart alpha
assert_contains "$CADDY_ROUTES" 'path /getbible/alpha/api/v1/*'
assert_equal \
    "$(grep -Fc 'systemctl reload caddy.service' "$CADDY_LOG" || true)" \
    "$((RESTART_CADDY_RELOADS + 1))"

# Model the generated route left by the previous release. Its catch-all
# remains valid, but its API prefix does not reach the current backend routes.
sed -i \
    -e 's#path /getbible/alpha/api/v1/[*]#path /getbible/alpha/api/v0/*#' \
    "$CADDY_ROUTES"
! grep -Fq 'path /getbible/alpha/api/v1/*' "$CADDY_ROUTES" ||
    fail "pre-release Caddy fixture retained the current API prefix"
SUCCESS_UPGRADE_CADDY_RELOADS=$(
    grep -Fc 'systemctl reload caddy.service' "$CADDY_LOG" || true
)

SECOND_SHA=$(commit_fixture_version v2)
cmd_upgrade alpha --source "$SOURCE_DIR" <<EOF

EOF
load_instance alpha
assert_equal "$ACTIVE_SHA" "$SECOND_SHA"
assert_equal "$(git -C "$(application_dir_for alpha)" rev-parse HEAD)" "$SECOND_SHA"
assert_contribution_assets "$(application_dir_for alpha)"
assert_equal \
    "$(git -C "${INSTANCE_ROOT}/alpha/app.previous" rev-parse HEAD)" \
    "$FIRST_SHA"
assert_contribution_assets "${INSTANCE_ROOT}/alpha/app.previous"
assert_contains "$CADDY_ROUTES" 'path /getbible/alpha/api/v1/*'
verify_managed_caddy_routes ||
    fail "successful upgrade did not install the regenerated managed Caddy routes"
assert_equal \
    "$(grep -Fc 'systemctl reload caddy.service' "$CADDY_LOG" || true)" \
    "$((SUCCESS_UPGRADE_CADDY_RELOADS + 1))"

# Re-running update at the deployed commit is a repair operation. It must use
# the target manager's migration/route logic without rotating app.previous.
sed -i \
    -e 's#path /getbible/alpha/api/v1/[*]#path /getbible/alpha/api/v0/*#' \
    "$CADDY_ROUTES"
SAME_SHA_PREVIOUS=$(
    git -C "${INSTANCE_ROOT}/alpha/app.previous" rev-parse HEAD
)
SAME_SHA_CADDY_RELOADS=$(
    grep -Fc 'systemctl reload caddy.service' "$CADDY_LOG" || true
)
: >"$MINI_APP_VERIFY_LOG"
REFRESH_OUTPUT=$(cmd_upgrade alpha --source "$SOURCE_DIR" <<EOF

EOF
)
assert_contains "$CADDY_ROUTES" 'path /getbible/alpha/api/v1/*'
assert_equal \
    "$(git -C "${INSTANCE_ROOT}/alpha/app.previous" rev-parse HEAD)" \
    "$SAME_SHA_PREVIOUS"
assert_equal \
    "$(grep -Fc 'systemctl reload caddy.service' "$CADDY_LOG" || true)" \
    "$((SAME_SHA_CADDY_RELOADS + 1))"
[[ "$REFRESH_OUTPUT" == *"Deployment refresh succeeded"* ]] ||
    fail "same-commit update did not report a deployment refresh"
assert_contains "$MINI_APP_VERIFY_LOG" \
    'local GET http://127.0.0.1:9201/getbible/alpha/api/v1/bookmarks/catalog'
assert_contains "$MINI_APP_VERIFY_LOG" \
    'local GET http://127.0.0.1:9201/getbible/alpha/api/v1/contributions/status'
assert_contains "$MINI_APP_VERIFY_LOG" \
    'local POST http://127.0.0.1:9201/getbible/alpha/api/v1/contributions/events'
assert_contains "$MINI_APP_VERIFY_LOG" \
    'public GET https://bot.example.com/getbible/alpha/api/v1/bookmarks/catalog'
assert_contains "$MINI_APP_VERIFY_LOG" \
    'public GET https://bot.example.com/getbible/alpha/api/v1/contributions/status'
assert_contains "$MINI_APP_VERIFY_LOG" \
    'public POST https://bot.example.com/getbible/alpha/api/v1/contributions/events'

# A same-commit repair is an artifact transaction, not merely a Caddy
# transaction. Force the candidate restart to fail after every managed file has
# been refreshed, then require the exact pre-attempt bytes and service state.
sed -i '/^PREWARM_DEFAULT_TRANSLATION=/d' "$(environment_file_for alpha)"
printf '# installed manager sentinel\n' >>"$MANAGER_PATH"
printf '# installed unit sentinel\n' >>"$UNIT_PATH"
printf '# installed logrotate sentinel\n' >>"$LOGROTATE_PATH"
printf '# installed resource sentinel\n' >>"$(resource_dropin_for alpha)"
sed -i \
    -e 's#path /getbible/alpha/api/v1/[*]#path /getbible/alpha/api/v0/*#' \
    "$CADDY_ROUTES"
FAILED_REFRESH_ENV_HASH=$(sha256sum "$(environment_file_for alpha)" | awk '{print $1}')
FAILED_REFRESH_MANAGER_HASH=$(sha256sum "$MANAGER_PATH" | awk '{print $1}')
FAILED_REFRESH_UNIT_HASH=$(sha256sum "$UNIT_PATH" | awk '{print $1}')
FAILED_REFRESH_LOGROTATE_HASH=$(sha256sum "$LOGROTATE_PATH" | awk '{print $1}')
FAILED_REFRESH_RESOURCE_HASH=$(sha256sum "$(resource_dropin_for alpha)" | awk '{print $1}')
FAILED_REFRESH_CADDYFILE_HASH=$(sha256sum "$CADDYFILE" | awk '{print $1}')
FAILED_REFRESH_ROUTES_HASH=$(sha256sum "$CADDY_ROUTES" | awk '{print $1}')
FAILED_REFRESH_APP_SHA=$(git -C "$(application_dir_for alpha)" rev-parse HEAD)
FAILED_REFRESH_PREVIOUS_SHA=$(
    git -C "${INSTANCE_ROOT}/alpha/app.previous" rev-parse HEAD
)
export FAIL_NEXT_START
FAIL_NEXT_START=$(service_name_for alpha)
if (
    cmd_upgrade alpha --source "$SOURCE_DIR" <<EOF

EOF
)
then
    fail "failed same-commit refresh was reported as successful"
fi
unset FAIL_NEXT_START
assert_equal "$(sha256sum "$(environment_file_for alpha)" | awk '{print $1}')" \
    "$FAILED_REFRESH_ENV_HASH"
assert_equal "$(sha256sum "$MANAGER_PATH" | awk '{print $1}')" \
    "$FAILED_REFRESH_MANAGER_HASH"
assert_equal "$(sha256sum "$UNIT_PATH" | awk '{print $1}')" \
    "$FAILED_REFRESH_UNIT_HASH"
assert_equal "$(sha256sum "$LOGROTATE_PATH" | awk '{print $1}')" \
    "$FAILED_REFRESH_LOGROTATE_HASH"
assert_equal "$(sha256sum "$(resource_dropin_for alpha)" | awk '{print $1}')" \
    "$FAILED_REFRESH_RESOURCE_HASH"
assert_equal "$(sha256sum "$CADDYFILE" | awk '{print $1}')" \
    "$FAILED_REFRESH_CADDYFILE_HASH"
assert_equal "$(sha256sum "$CADDY_ROUTES" | awk '{print $1}')" \
    "$FAILED_REFRESH_ROUTES_HASH"
assert_equal "$(git -C "$(application_dir_for alpha)" rev-parse HEAD)" \
    "$FAILED_REFRESH_APP_SHA"
assert_equal "$(git -C "${INSTANCE_ROOT}/alpha/app.previous" rev-parse HEAD)" \
    "$FAILED_REFRESH_PREVIOUS_SHA"
assert_equal "$(systemctl is-active "$(service_name_for alpha)")" "active"
assert_equal "$(systemctl is-enabled "$(service_name_for alpha)")" "enabled"
if compgen -G "${ETC_ROOT}/.upgrade-alpha.*" >/dev/null; then
    fail "failed same-commit refresh retained a completed transaction snapshot"
fi

export FAIL_MINI_APP_PUBLIC
FAIL_MINI_APP_PUBLIC=1
if (
    cmd_upgrade alpha --source "$SOURCE_DIR" <<EOF

EOF
)
then
    fail "same-commit refresh ignored a failed public API postflight"
fi
unset FAIL_MINI_APP_PUBLIC
assert_equal "$(sha256sum "$(environment_file_for alpha)" | awk '{print $1}')" \
    "$FAILED_REFRESH_ENV_HASH"
assert_equal "$(sha256sum "$MANAGER_PATH" | awk '{print $1}')" \
    "$FAILED_REFRESH_MANAGER_HASH"
assert_equal "$(sha256sum "$CADDY_ROUTES" | awk '{print $1}')" \
    "$FAILED_REFRESH_ROUTES_HASH"
assert_equal "$(systemctl is-active "$(service_name_for alpha)")" "active"
assert_equal "$(systemctl is-enabled "$(service_name_for alpha)")" "enabled"
if compgen -G "${ETC_ROOT}/.upgrade-alpha.*" >/dev/null; then
    fail "failed API postflight retained a completed transaction snapshot"
fi

cmd_rollback alpha <<EOF
y
EOF
load_instance alpha
assert_equal "$ACTIVE_SHA" "$FIRST_SHA"
assert_equal "$(git -C "$(application_dir_for alpha)" rev-parse HEAD)" "$FIRST_SHA"
assert_contribution_assets "$(application_dir_for alpha)"
assert_equal \
    "$(git -C "${INSTANCE_ROOT}/alpha/app.previous" rev-parse HEAD)" \
    "$SECOND_SHA"
assert_contribution_assets "${INSTANCE_ROOT}/alpha/app.previous"

# A failed upgraded service must restore the exact pre-upgrade Caddy bytes and
# reload them after the candidate API prefix was already installed.
sed -i \
    -e 's#path /getbible/alpha/api/v1/[*]#path /getbible/alpha/api/v0/*#' \
    "$CADDY_ROUTES"
FAILED_UPGRADE_CADDYFILE_HASH=$(sha256sum "$CADDYFILE" | awk '{print $1}')
FAILED_UPGRADE_ROUTES_HASH=$(sha256sum "$CADDY_ROUTES" | awk '{print $1}')
FAILED_UPGRADE_ENV_HASH=$(sha256sum "$(environment_file_for alpha)" | awk '{print $1}')
FAILED_UPGRADE_MANAGER_HASH=$(sha256sum "$MANAGER_PATH" | awk '{print $1}')
FAILED_UPGRADE_UNIT_HASH=$(sha256sum "$UNIT_PATH" | awk '{print $1}')
FAILED_UPGRADE_LOGROTATE_HASH=$(sha256sum "$LOGROTATE_PATH" | awk '{print $1}')
FAILED_UPGRADE_RESOURCE_HASH=$(sha256sum "$(resource_dropin_for alpha)" | awk '{print $1}')
FAILED_UPGRADE_CADDY_RELOADS=$(
    grep -Fc 'systemctl reload caddy.service' "$CADDY_LOG" || true
)
THIRD_SHA=$(commit_fixture_version v3)
export FAIL_CONTRIBUTION_STORE_VERIFY
FAIL_CONTRIBUTION_STORE_VERIFY=1
if (
    cmd_upgrade alpha --source "$SOURCE_DIR" <<EOF

EOF
)
then
    fail "an unavailable target contribution database was reported as upgrade-ready"
fi
unset FAIL_CONTRIBUTION_STORE_VERIFY
load_instance alpha
assert_equal "$ACTIVE_SHA" "$FIRST_SHA"
assert_equal "$(git -C "$(application_dir_for alpha)" rev-parse HEAD)" "$FIRST_SHA"
assert_absent "${INSTANCE_ROOT}/alpha/app.next"
assert_equal "$(sha256sum "$(environment_file_for alpha)" | awk '{print $1}')" \
    "$FAILED_UPGRADE_ENV_HASH"

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
assert_equal "$(sha256sum "$CADDYFILE" | awk '{print $1}')" \
    "$FAILED_UPGRADE_CADDYFILE_HASH"
assert_equal "$(sha256sum "$CADDY_ROUTES" | awk '{print $1}')" \
    "$FAILED_UPGRADE_ROUTES_HASH"
assert_equal "$(sha256sum "$(environment_file_for alpha)" | awk '{print $1}')" \
    "$FAILED_UPGRADE_ENV_HASH"
assert_equal "$(sha256sum "$MANAGER_PATH" | awk '{print $1}')" \
    "$FAILED_UPGRADE_MANAGER_HASH"
assert_equal "$(sha256sum "$UNIT_PATH" | awk '{print $1}')" \
    "$FAILED_UPGRADE_UNIT_HASH"
assert_equal "$(sha256sum "$LOGROTATE_PATH" | awk '{print $1}')" \
    "$FAILED_UPGRADE_LOGROTATE_HASH"
assert_equal "$(sha256sum "$(resource_dropin_for alpha)" | awk '{print $1}')" \
    "$FAILED_UPGRADE_RESOURCE_HASH"
assert_equal \
    "$(grep -Fc 'systemctl reload caddy.service' "$CADDY_LOG" || true)" \
    "$((FAILED_UPGRADE_CADDY_RELOADS + 2))"
assert_equal "$(systemctl is-active caddy.service)" "active"
assert_equal "$(systemctl is-enabled caddy.service)" "enabled"

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
