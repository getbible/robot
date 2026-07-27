#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)

# shellcheck source=../setup.sh
source "$ROOT/setup.sh"

probe_calls=0
sleeps=0
ready_after=4

probe_mini_app_url() {
    ((probe_calls += 1))
    ((probe_calls >= ready_after))
}

sleep() {
    ((sleeps += 1))
}

mini_app_local_url() {
    printf 'http://127.0.0.1:9201/\n'
}

verify_mini_app_local ignored-app ignored-env 5
[[ "$probe_calls" == "4" ]]
[[ "$sleeps" == "3" ]]

probe_calls=0
sleeps=0
ready_after=99
if verify_mini_app_local ignored-app ignored-env 3; then
    printf 'Mini App readiness unexpectedly succeeded.\n' >&2
    exit 1
fi
[[ "$probe_calls" == "3" ]]
[[ "$sleeps" == "2" ]]

printf 'Mini App readiness test passed.\n'
