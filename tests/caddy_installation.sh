#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT

FAKE_BIN="${TEST_ROOT}/bin"
TOOLS_BIN="${TEST_ROOT}/tools"
COMMAND_LOG="${TEST_ROOT}/commands.log"
APT_STATE="${TEST_ROOT}/apt-repaired"
mkdir -p "$FAKE_BIN" "$TOOLS_BIN"
: >"$COMMAND_LOG"

for tool in chmod cp dirname grep install mktemp readlink rm stat tail wc; do
    ln -s -- "$(command -v "$tool")" "${TOOLS_BIN}/${tool}"
done

write_stub() {
    local name=$1
    shift
    printf '%s\n' '#!/bin/bash' 'set -Eeuo pipefail' "$@" >"${FAKE_BIN}/${name}"
    chmod 0755 "${FAKE_BIN}/${name}"
}

write_stub dpkg \
    'printf "dpkg %s\n" "$*" >>"$COMMAND_LOG"' \
    '[[ "$*" == "--configure --pending" ]]'

write_stub apt-get \
    'printf "apt-get %s\n" "$*" >>"$COMMAND_LOG"' \
    'if [[ "$*" == "check" && ! -f "$APT_STATE" ]]; then exit 1; fi' \
    'if [[ "$*" == "--fix-broken install --yes" ]]; then : >"$APT_STATE"; fi' \
    'if [[ "$*" == "install --yes caddy" ]]; then' \
    '    printf "%s\n" "#!/usr/bin/env bash" "exit 0" >"${FAKE_BIN}/caddy"' \
    '    chmod 0755 "${FAKE_BIN}/caddy"' \
    'fi'

write_stub curl \
    'output=""' \
    'url=""' \
    'while (($# > 0)); do' \
    '    case "$1" in' \
    '        --output) output=$2; shift 2 ;;' \
    '        http*) url=$1; shift ;;' \
    '        *) shift ;;' \
    '    esac' \
    'done' \
    'printf "curl %s\n" "$url" >>"$COMMAND_LOG"' \
    'if [[ "$url" == *"/gpg.key" ]]; then' \
    '    printf "%s\n" "fake OpenPGP key" >"$output"' \
    'else' \
    '    printf "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main\n" >"$output"' \
    'fi'

write_stub gpg \
    'output=""' \
    'input=""' \
    'while (($# > 0)); do' \
    '    case "$1" in' \
    '        --output) output=$2; shift 2 ;;' \
    '        --*) shift ;;' \
    '        *) input=$1; shift ;;' \
    '    esac' \
    'done' \
    'printf "gpg dearmor\n" >>"$COMMAND_LOG"' \
    'cp -- "$input" "$output"'

write_stub systemctl \
    'printf "systemctl %s\n" "$*" >>"$COMMAND_LOG"' \
    'case "${1:-}" in' \
    '    cat|is-active) exit 0 ;;' \
    '    *) exit 0 ;;' \
    'esac'

write_stub ss 'exit 0'

export APT_STATE COMMAND_LOG FAKE_BIN
export PATH="${FAKE_BIN}:${TOOLS_BIN}"
command -v caddy >/dev/null 2>&1 &&
    {
        printf 'Caddy installation fixture requires a host without Caddy.\n' >&2
        exit 1
    }

# shellcheck source=../setup.sh
source "$ROOT/setup.sh"
trap 'rm -rf -- "$TEST_ROOT"' EXIT

CADDY_ROOT="${TEST_ROOT}/etc/caddy"
CADDYFILE="${CADDY_ROOT}/Caddyfile"
CADDY_ROUTES="${CADDY_ROOT}/getbible-robot.caddy"
CADDY_APT_KEYRING="${TEST_ROOT}/usr/share/keyrings/caddy-stable-archive-keyring.gpg"
CADDY_APT_SOURCE="${TEST_ROOT}/etc/apt/sources.list.d/caddy-stable.list"

confirm() {
    :
}

ensure_caddy_available

[[ -x "${FAKE_BIN}/caddy" ]]
[[ -s "$CADDY_APT_KEYRING" ]]
[[ -s "$CADDY_APT_SOURCE" ]]
[[ "$(stat -c '%a' "$CADDY_APT_KEYRING")" == "644" ]]
[[ "$(stat -c '%a' "$CADDY_APT_SOURCE")" == "644" ]]
grep -Fq -- "dpkg --configure --pending" "$COMMAND_LOG"
grep -Fq -- "apt-get --fix-broken install --yes" "$COMMAND_LOG"
grep -Fq -- \
    "apt-get install --yes apt-transport-https ca-certificates curl debian-archive-keyring debian-keyring gnupg" \
    "$COMMAND_LOG"
grep -Fq -- \
    "curl https://dl.cloudsmith.io/public/caddy/stable/gpg.key" \
    "$COMMAND_LOG"
grep -Fq -- \
    "curl https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" \
    "$COMMAND_LOG"
grep -Fq -- "apt-get install --yes caddy" "$COMMAND_LOG"
grep -Fq -- "systemctl cat caddy.service" "$COMMAND_LOG"

before=$(wc -l <"$COMMAND_LOG")
ensure_caddy_available
after=$(wc -l <"$COMMAND_LOG")
[[ "$after" -eq "$((before + 2))" ]]
tail -n 2 "$COMMAND_LOG" |
    grep -Fxq -- "systemctl is-active --quiet caddy.service"

printf 'Caddy installation test passed.\n'
