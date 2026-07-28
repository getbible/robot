#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

CONTROL="${GETBIBLE_ROBOT_CONTROL:-/usr/local/bin/getbible-robot-container}"

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
  setup.sh shell

Configuration is supplied by the container environment in single mode or by
/config/instances/*.env in multi mode. Configuration errors are written to
standard output/error and are visible through docker logs.
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
  8) Open a shell in this container
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
            8) exec /bin/bash ;;
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
        shell) exec /bin/bash ;;
        help|-h|--help) usage ;;
        *) usage >&2; printf 'ERROR: Unknown command: %s\n' "$command" >&2; return 1 ;;
    esac
}

main "$@"
