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
CONTRIBUTION_CATALOG=""
CONTRIBUTION_EXPORT_ROOT=""
CONTRIBUTION_SCRIPT=""
CONTRIBUTION_CREDENTIALS_FILE=""

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
  setup.sh contributions INSTANCE status|inspect|export|commit|tokens
  setup.sh contributions INSTANCE inspect --revision NUMBER
  setup.sh contributions INSTANCE applications|topics|verses|accept
  setup.sh shell

Configuration is supplied by the container environment in single mode or by
/config/instances/*.env in multi mode. Configuration errors are written to
standard output/error and are visible through docker logs.

Contribution acceptance is preserved in the private instance database. With a
CONTRIBUTION_GITHUB_TOKEN, acceptance commits directly to the bookmark builder.
Without it, work remains queued for `contributions INSTANCE commit`. Optional
CONTRIBUTION_OPENAI_API_KEY translates new topics.
Use `contributions INSTANCE tokens` to add, replace or clear publication tokens.
They are saved privately in the instance's persistent /data state directory,
overriding the environment or mounted instance file for subsequent commits.
No Git executable, working copy, contribution branch or pull request is needed.
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
    local action=${2:-}
    validate_instance "$instance" || return 1
    [[ "$CONTRIBUTION_APP_ROOT" == /* && "$CONTRIBUTION_DATA_ROOT" == /* &&
        "$CONTRIBUTION_CONFIG_ROOT" == /* ]] || {
        printf 'ERROR: Container contribution roots must be absolute paths.\n' >&2
        return 1
    }
    CONTRIBUTION_SCRIPT="${CONTRIBUTION_APP_ROOT}/scripts/contribution_review.py"
    local instance_root="${CONTRIBUTION_DATA_ROOT}/${instance}"
    local state_root="${instance_root}/state"
    CONTRIBUTION_STORE="${state_root}/contributions.sqlite3"
    CONTRIBUTION_CREDENTIALS_FILE="${state_root}/contribution-credentials.env"
    CONTRIBUTION_EXPORT_ROOT="${state_root}/contribution-exports"
    CONTRIBUTION_CATALOG="${CONTRIBUTION_EXPORT_ROOT}/bookmarks-catalog.json"
    [[ -f "$CONTRIBUTION_SCRIPT" && ! -L "$CONTRIBUTION_SCRIPT" ]] || {
        printf 'ERROR: Container contribution review asset is unavailable: %s\n' \
            "$CONTRIBUTION_SCRIPT" >&2
        return 1
    }
    [[ -d "$CONTRIBUTION_DATA_ROOT" && ! -L "$CONTRIBUTION_DATA_ROOT" &&
        -d "$instance_root" && ! -L "$instance_root" &&
        -d "$state_root" && ! -L "$state_root" ]] || {
        printf 'ERROR: Start the instance once before reviewing contributions.\n' >&2
        return 1
    }
    [[ "$action" == "tokens" || ( -f "$CONTRIBUTION_STORE" && ! -L "$CONTRIBUTION_STORE" ) ]] || {
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
            --actor "container:${CONTRIBUTION_INSTANCE}" "$@"
    )
}

run_catalogue_review() {
    # Topic, verse, and acceptance review compare against the shared
    # catalogue.  It is fetched fresh from the public Bookmarks API first and
    # the command is not started when that verified download is unavailable.
    local command=$1
    shift
    fetch_contribution_catalog || return 1
    run_contribution_review "$command" --catalog-file "$CONTRIBUTION_CATALOG" "$@"
}

ensure_export_root() {
    # Multiple operators may export at once. `mkdir -p` makes directory
    # creation idempotent; validate the resulting object before using it so an
    # existing symlink or non-directory is still rejected.
    mkdir -p -- "$CONTRIBUTION_EXPORT_ROOT" || {
        printf 'ERROR: The private contribution export directory could not be created.\n' >&2
        return 1
    }
    [[ -d "$CONTRIBUTION_EXPORT_ROOT" && ! -L "$CONTRIBUTION_EXPORT_ROOT" ]] || {
        printf 'ERROR: The private contribution export directory is unsafe.\n' >&2
        return 1
    }
    chmod 0700 -- "$CONTRIBUTION_EXPORT_ROOT"
}

fetch_contribution_catalog() {
    ensure_export_root || return 1
    if ! (
        cd "$CONTRIBUTION_APP_ROOT"
        "$CONTRIBUTION_PYTHON" -m scripts.contribution_review fetch-catalog \
            --output "$CONTRIBUTION_CATALOG"
    ); then
        printf 'ERROR: The shared bookmark catalogue could not be fetched from bookmarks.getbible.net; this review step needs it and was not started.\n' >&2
        return 1
    fi
}

export_contributions() {
    local export_root=$CONTRIBUTION_EXPORT_ROOT
    local stamp
    local destination
    ensure_export_root || return 1
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
    printf 'The export is optional; contributions INSTANCE commit publishes the accepted ledger directly through the GitHub API.\n'

}

run_contribution_publication() {
    local command=$1
    shift
    local -a config_arguments=()
    local mode=${ROBOT_MODE:-multi}
    if [[ "${mode,,}" != "single" ]]; then
        config_arguments+=(--env-file "$CONTRIBUTION_CONFIG_ROOT/$CONTRIBUTION_INSTANCE.env")
    fi
    (
        cd "$CONTRIBUTION_APP_ROOT"
        "$CONTRIBUTION_PYTHON" -m scripts.contribution_publish "$command" \
            --store "$CONTRIBUTION_STORE" \
            --credentials-file "$CONTRIBUTION_CREDENTIALS_FILE" \
            "${config_arguments[@]}" "$@"
    )
}

configure_contribution_tokens() {
    require_interactive || return 1
    run_contribution_publication configure --replace
}

accept_and_commit_contributions() {
    run_catalogue_review accept || return 1
    run_contribution_publication commit
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
  5) Accept approved changes and commit to the builder
  6) Write a privacy-safe repository export
  7) Commit previously accepted contributions / retry
  8) Add, replace or clear GitHub / OpenAI credentials
  9) Inspect submitted changes and accepted revisions
  0) Return

Topic, verse, and acceptance review fetch the shared catalogue from bookmarks.getbible.net first.
Publication uses optional GitHub / OpenAI tokens from the instance configuration.
EOF
        read -r -p "Selection: " selection
        case "$selection" in
            1) run_contribution_review applications || true ;;
            2) run_catalogue_review topics || true ;;
            3)
                run_catalogue_review verses \
                    --translation "$CONTRIBUTION_TRANSLATION" || true
                ;;
            4)
                run_contribution_review status || true
                run_contribution_publication status || true
                ;;
            5) accept_and_commit_contributions || true ;;
            6) export_contributions || true ;;
            7) run_contribution_publication commit || true ;;
            8) configure_contribution_tokens || true ;;
            9) run_contribution_review inspect || true ;;
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
    load_contribution_context "$instance" "$action" || return 1
    case "$action" in
        "") contribution_menu ;;
        status)
            run_contribution_review status
            run_contribution_publication status
            ;;
        commit) run_contribution_publication commit ;;
        tokens) configure_contribution_tokens ;;
        inspect) run_contribution_review inspect "${@:3}" ;;
        export) export_contributions ;;
        applications)
            require_interactive || return 1
            run_contribution_review applications
            ;;
        topics)
            require_interactive || return 1
            run_catalogue_review topics
            ;;
        accept)
            require_interactive || return 1
            accept_and_commit_contributions
            ;;
        verses)
            require_interactive || return 1
            run_catalogue_review verses \
                --translation "$CONTRIBUTION_TRANSLATION"
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
        contributions) contributions_command "$@" ;;
        shell) exec /bin/bash ;;
        help|-h|--help) usage ;;
        *) usage >&2; printf 'ERROR: Unknown command: %s\n' "$command" >&2; return 1 ;;
    esac
}

main "$@"
