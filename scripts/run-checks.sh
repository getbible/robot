#!/usr/bin/env bash
set -euo pipefail

VENV=${VENV:-venv}
PYTHON="$VENV/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing ${PYTHON}. Install requirements-dev.txt in a virtual environment first." >&2
  exit 2
fi

bash -n setup.sh
bash -n container/setup.sh
bash -n tests/docker_manager.sh
bash setup.sh self-test
"$PYTHON" -m pip check
"$PYTHON" -m compileall -q bot.py config.py modules container scripts tests
"$PYTHON" -m unittest discover -s tests -v
"$VENV/bin/ruff" check .
"$VENV/bin/mypy"
if ! command -v npm >/dev/null 2>&1; then
  echo "Missing npm. Node.js is required for Mini App quality checks." >&2
  exit 2
fi
npm --prefix miniapp run check
npm --prefix miniapp run test:browser

librarian_path=$(
  "$PYTHON" - <<'PY'
from pathlib import Path

import getbible

print(Path(getbible.__file__).resolve().parent)
PY
)
"$VENV/bin/bandit" -q -r bot.py config.py modules container scripts "$librarian_path" -ll
"$PYTHON" scripts/audit_runtime.py

report=$(mktemp)
trap 'rm -f "$report"' EXIT
venv_exclude=$(
  "$PYTHON" - "$VENV" <<'PY'
import pathlib
import re
import sys

repository = pathlib.Path.cwd().resolve()
environment = pathlib.Path(sys.argv[1]).resolve()
try:
    relative = environment.relative_to(repository)
except ValueError:
    print("")
else:
    print(rf"(^|/){re.escape(relative.as_posix())}/")
PY
)

secret_args=(
  --all-files
  --exclude-files '(^|/)\.git/'
  --exclude-files '(^|/)\.(mypy|ruff)_cache/'
  --exclude-files '(^|/)(venv|\.venv|env|ENV|\.lock-venv|\.lock-verify-venv)/'
  --exclude-files '(^|/)node_modules/'
  --exclude-files '(^|/)\.env\.template$'
  --exclude-files '(^|/)requirements(-dev)?\.txt$'
)
if [[ -n "$venv_exclude" ]]; then
  secret_args+=(--exclude-files "$venv_exclude")
fi
"$VENV/bin/detect-secrets" scan "${secret_args[@]}" > "$report"

"$PYTHON" - "$report" <<'PY'
import json
import pathlib
import sys

report = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
findings = report.get("results", {})
if findings:
    print(json.dumps(findings, indent=2))
    raise SystemExit("Potential secrets detected.")
PY

if [[ "${VERIFY_SYSTEMD:-0}" == "1" ]]; then
  if ! command -v systemd-analyze >/dev/null 2>&1; then
    echo "VERIFY_SYSTEMD=1 but systemd-analyze is unavailable." >&2
    exit 2
  fi
  if [[ ! "${VERIFY_SYSTEMD_INSTANCE:-}" =~ ^[a-z][a-z0-9-]{0,22}[a-z0-9]$ ]]; then
    echo "Set VERIFY_SYSTEMD_INSTANCE to an installed instance name." >&2
    exit 2
  fi
  systemd-analyze verify "getbible-robot@${VERIFY_SYSTEMD_INSTANCE}.service"
fi

echo "All local deterministic, quality, dependency, and secret checks passed."
