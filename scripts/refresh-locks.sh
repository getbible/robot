#!/usr/bin/env bash
set -euo pipefail

PYTHON=${PYTHON:-python3.14}
TOOL_VENV=${TOOL_VENV:-.lock-venv}
VERIFY_VENV=${VERIFY_VENV:-.lock-verify-venv}

version=$(
  "$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
)
if [[ "$version" != "3.14" ]]; then
  echo "Lock generation requires Python 3.14; found ${version}." >&2
  exit 2
fi

upgrade_args=()
if [[ "${UPGRADE:-0}" == "1" ]]; then
  upgrade_args+=(--upgrade)
fi

rm -rf "$TOOL_VENV" "$VERIFY_VENV"
"$PYTHON" -m venv "$TOOL_VENV"
"$TOOL_VENV/bin/python" -m pip install \
  pip==26.1.2 \
  pip-tools==7.6.0

"$TOOL_VENV/bin/python" -m piptools compile \
  --resolver=backtracking \
  --generate-hashes \
  --allow-unsafe \
  "${upgrade_args[@]}" \
  --output-file=requirements.txt \
  requirements.in

"$TOOL_VENV/bin/python" -m piptools compile \
  --resolver=backtracking \
  --generate-hashes \
  --allow-unsafe \
  "${upgrade_args[@]}" \
  --output-file=requirements-dev.txt \
  requirements-dev.in

"$PYTHON" -m venv "$VERIFY_VENV"
"$VERIFY_VENV/bin/python" -m pip install --require-hashes -r requirements-dev.txt
"$VERIFY_VENV/bin/python" -m pip check

cat <<'EOF'
Locks regenerated and verified on Python 3.14.

Next steps:
  1. Review the complete requirements.txt and requirements-dev.txt diff.
  2. Install requirements.txt with --require-hashes on Python 3.10 through 3.14.
  3. Run: bash scripts/run-checks.sh
  4. Commit both input files and both generated locks together.

Set UPGRADE=1 when intentionally selecting the newest versions allowed by the inputs.
EOF
