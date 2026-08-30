#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

CONTROL="${GETBIBLE_ROBOT_CONTROL:-/usr/local/bin/getbible-robot-container}"
CONTRIBUTION_APP_ROOT="${ROBOT_APP_ROOT:-/app}"
CONTRIBUTION_DATA_ROOT="${ROBOT_DATA_DIR:-/data}"
CONTRIBUTION_CONFIG_ROOT="${ROBOT_CONFIG_DIR:-/config/instances}"
CONTRIBUTION_PYTHON="${ROBOT_PYTHON:-python}"
CONTRIBUTION_INSTANCE=""
CONTRIBUTION_STORE=""
CONTRIBUTION_TRANSLATION=""
CONTRIBUTION_TOPICS=""
CONTRIBUTION_ASSOCIATIONS=""
CONTRIBUTION_SCRIPT=""

usage() {
    cat <<'EOF'
GetBible Robot container setup and operations.

Usage:
  setup.sh [menu]
  setup.sh list
  setup.sh status [instance]
  setup.sh doctor [instance]
  setup.sh start INSTANCE
  setup.sh stop INSTANCE
  setup.sh restart INSTANCE
  setup.sh reload
  setup.sh contributions [INSTANCE]
  setup.sh contributions INSTANCE status|export
  setup.sh shell

Configuration is supplied by the container environment in single mode or by
/config/instances/*.env in multi mode. Configuration errors are written to
standard output/error and are visible through docker logs.

Contribution review and live publication are supported inside the container.
The container can also write a privacy-safe JSON export, but automated Git
branch publication from that export is not supported in this release.
EOF
}

require_control() {
    [[ -x "$CONTROL" ]] || {
        printf 'ERROR: Container control command is unavailable: %s\n' "$CONTROL" >&2
        exit 1
    }
}

run_control() {
    require_control
    "$CONTROL" "$@"
}

prompt_instance() {
    local instance
    read -r -p "Instance name: " instance
    [[ "$instance" =~ ^[a-z][a-z0-9-]{0,22}[a-z0-9]$ &&
        "$instance" != *--* ]] || {
        printf 'ERROR: Invalid instance name.\n' >&2
        return 1
    }
    printf '%s\n' "$instance"
}

validate_instance() {
    local instance=$1
    [[ "$instance" =~ ^[a-z][a-z0-9-]{0,22}[a-z0-9]$ &&
        "$instance" != *--* ]] || {
        printf 'ERROR: Invalid instance name.\n' >&2
        return 1
    }
}

require_interactive() {
    [[ -t 0 && -t 1 ]] || {
        printf 'ERROR: This contribution review action requires an interactive terminal.\n' >&2
        return 1
    }
}

contribution_translation() {
    local instance=$1
    "$CONTRIBUTION_PYTHON" - "$instance" "$CONTRIBUTION_CONFIG_ROOT" <<'PY'
import os
import re
import sys
from pathlib import Path

instance = sys.argv[1]
config_root = Path(sys.argv[2])
mode = os.environ.get("ROBOT_MODE", "multi").casefold()
if mode == "single":
    configured_instance = os.environ.get("INSTANCE_NAME", "production").strip()
    if configured_instance != instance:
        raise SystemExit("The requested single-bot instance is not configured.")
    translation = os.environ.get("TRANSLATION", "kjv")
else:
    from dotenv import dotenv_values

    path = config_root / f"{instance}.env"
    if path.is_symlink() or not path.is_file():
        raise SystemExit("The requested multi-bot instance is not configured.")
    values = dotenv_values(path, interpolate=False)
    translation = values.get("TRANSLATION", "kjv")
if not isinstance(translation, str):
    raise SystemExit("The instance translation is invalid.")
translation = translation.strip().casefold()
if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,29}", translation) is None:
    raise SystemExit("The instance translation is invalid.")
print(translation)
PY
}

load_contribution_context() {
    local instance=$1
    validate_instance "$instance" || return 1
    [[ "$CONTRIBUTION_APP_ROOT" == /* && "$CONTRIBUTION_DATA_ROOT" == /* &&
        "$CONTRIBUTION_CONFIG_ROOT" == /* ]] || {
        printf 'ERROR: Container contribution roots must be absolute paths.\n' >&2
        return 1
    }
    CONTRIBUTION_SCRIPT="${CONTRIBUTION_APP_ROOT}/scripts/contribution_review.py"
    CONTRIBUTION_TOPICS="${CONTRIBUTION_APP_ROOT}/data/global-bookmarks/topics.json"
    CONTRIBUTION_ASSOCIATIONS="${CONTRIBUTION_APP_ROOT}/data/global-bookmarks/tag-verse.csv"
    local instance_root="${CONTRIBUTION_DATA_ROOT}/${instance}"
    local state_root="${instance_root}/state"
    CONTRIBUTION_STORE="${state_root}/contributions.sqlite3"
    for required in "$CONTRIBUTION_SCRIPT" "$CONTRIBUTION_TOPICS" \
        "$CONTRIBUTION_ASSOCIATIONS"; do
        [[ -f "$required" && ! -L "$required" ]] || {
            printf 'ERROR: Container contribution review asset is unavailable: %s\n' \
                "$required" >&2
            return 1
        }
    done
    [[ -d "$CONTRIBUTION_DATA_ROOT" && ! -L "$CONTRIBUTION_DATA_ROOT" &&
        -d "$instance_root" && ! -L "$instance_root" &&
        -d "$state_root" && ! -L "$state_root" ]] || {
        printf 'ERROR: Start the instance once before reviewing contributions.\n' >&2
        return 1
    }
    [[ -f "$CONTRIBUTION_STORE" && ! -L "$CONTRIBUTION_STORE" ]] || {
        printf 'ERROR: The private contribution store is unavailable for this instance.\n' >&2
        return 1
    }
    CONTRIBUTION_TRANSLATION=$(contribution_translation "$instance") || return 1
    CONTRIBUTION_INSTANCE=$instance
}

run_contribution_review() {
    local command=$1
    shift
    (
        cd "$CONTRIBUTION_APP_ROOT"
        "$CONTRIBUTION_PYTHON" -m scripts.contribution_review "$command" \
            --store "$CONTRIBUTION_STORE" \
            --actor "container:${CONTRIBUTION_INSTANCE}" \
            --topics-file "$CONTRIBUTION_TOPICS" \
            --associations-file "$CONTRIBUTION_ASSOCIATIONS" "$@"
    )
}

export_contributions() {
    local export_root="${CONTRIBUTION_DATA_ROOT}/${CONTRIBUTION_INSTANCE}/state/contribution-exports"
    local stamp
    local destination
    # Multiple operators may export at once. `mkdir -p` makes directory
    # creation idempotent; validate the resulting object before using it so an
    # existing symlink or non-directory is still rejected.
    mkdir -p -- "$export_root" || {
        printf 'ERROR: The private contribution export directory could not be created.\n' >&2
        return 1
    }
    [[ -d "$export_root" && ! -L "$export_root" ]] || {
        printf 'ERROR: The private contribution export directory is unsafe.\n' >&2
        return 1
    }
    chmod 0700 -- "$export_root"
    stamp=$(date --utc +'%Y%m%d-%H%M%S')
    destination=$(mktemp \
        --tmpdir="$export_root" \
        "reviewed-catalog-${stamp}-XXXXXXXX.json") || {
        printf 'ERROR: A private contribution export could not be reserved.\n' >&2
        return 1
    }
    chmod 0600 -- "$destination"
    if ! (
        cd "$CONTRIBUTION_APP_ROOT"
        "$CONTRIBUTION_PYTHON" -m scripts.contribution_review export \
            --store "$CONTRIBUTION_STORE" \
            --actor "container:${CONTRIBUTION_INSTANCE}" \
            --output "$destination"
    ); then
        rm -f -- "$destination"
        return 1
    fi
    printf 'Privacy-safe repository export: %s\n' "$destination"
    printf 'Automated Git branch publication from a container export is not supported in this release.\n'
    printf 'Retain the JSON only for a separately reviewed manual repository import.\n'
}

contribution_menu() {
    require_interactive || return 1
    local selection
    while true; do
        cat <<'EOF'

Container contribution review
  1) Review contributor applications / revoke access
  2) Resolve and merge contributor topics
  3) Review verse additions and removals
  4) Show review status
  5) Publish approved changes to this live instance
  6) Write a privacy-safe repository export
  0) Return

Automated repository branch publication is unavailable for container instances in this release.
Use a native deployment for the guarded one-command Git publication workflow.
EOF
        read -r -p "Selection: " selection
        case "$selection" in
            1) run_contribution_review applications || true ;;
            2) run_contribution_review topics || true ;;
            3)
                run_contribution_review verses \
                    --translation "$CONTRIBUTION_TRANSLATION" || true
                ;;
            4) run_contribution_review status || true ;;
            5) run_contribution_review publish-live || true ;;
            6) export_contributions || true ;;
            0) return ;;
            *) printf 'WARNING: Unknown selection.\n' >&2 ;;
        esac
    done
}

contributions_command() {
    local instance=${1:-}
    local action=${2:-}
    if [[ -z "$instance" ]]; then
        require_interactive || return 1
        instance=$(prompt_instance) || return 1
    fi
    load_contribution_context "$instance" || return 1
    case "$action" in
        "") contribution_menu ;;
        status) run_contribution_review status ;;
        export) export_contributions ;;
        applications|topics|verses|publish-live)
            require_interactive || return 1
            if [[ "$action" == "verses" ]]; then
                run_contribution_review verses \
                    --translation "$CONTRIBUTION_TRANSLATION"
            else
                run_contribution_review "$action"
            fi
            ;;
        *)
            printf 'ERROR: Unknown contribution action: %s\n' "$action" >&2
            return 1
            ;;
    esac
}

instance_command() {
    local command=$1
    local instance=${2:-}
    [[ -n "$instance" ]] || instance=$(prompt_instance)
    run_control "$command" "$instance"
}

menu() {
    [[ -t 0 && -t 1 ]] || {
        printf 'ERROR: The container menu requires an interactive terminal.\n' >&2
        return 1
    }
    local selection
    local instance
    while true; do
        cat <<'EOF'

GetBible Robot container operations
  1) List bot instances
  2) Show instance status
  3) Run instance diagnostics
  4) Start an instance
  5) Stop an instance
  6) Restart an instance
  7) Reload mounted instance configuration
  8) Review and publish trusted contributions
  9) Open a shell in this container
  0) Exit
EOF
        read -r -p "Selection: " selection
        case "$selection" in
            1) run_control list ;;
            2)
                instance=$(prompt_instance) || continue
                run_control status "$instance"
                ;;
            3)
                instance=$(prompt_instance) || continue
                run_control doctor "$instance"
                ;;
            4)
                instance=$(prompt_instance) || continue
                run_control start "$instance"
                ;;
            5)
                instance=$(prompt_instance) || continue
                run_control stop "$instance"
                ;;
            6)
                instance=$(prompt_instance) || continue
                run_control restart "$instance"
                ;;
            7) run_control reload ;;
            8)
                instance=$(prompt_instance) || continue
                contributions_command "$instance"
                ;;
            9) exec /bin/bash ;;
            0) return ;;
            *) printf 'WARNING: Unknown selection.\n' >&2 ;;
        esac
    done
}

main() {
    local command=${1:-menu}
    [[ $# -eq 0 ]] || shift
    case "$command" in
        menu) menu ;;
        list) run_control list ;;
        status|doctor) run_control "$command" "$@" ;;
        start|stop|restart) instance_command "$command" "${1:-}" ;;
        reload) run_control reload ;;
        contributions) contributions_command "${1:-}" "${2:-}" ;;
        shell) exec /bin/bash ;;
        help|-h|--help) usage ;;
        *) usage >&2; printf 'ERROR: Unknown command: %s\n' "$command" >&2; return 1 ;;
    esac
}

main "$@"
