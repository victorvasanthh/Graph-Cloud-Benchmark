#!/usr/bin/env bash
#
# Runs once, after the devcontainer features are installed.
#
# Deliberately NOT `set -e`. A Codespace that half-created is far more useful
# than one that refused to create: if the dataset download fails because SNAP
# is briefly unreachable, you want a shell with the tooling installed and a
# clear message, not a failed creation and no way to investigate. Each step
# reports its own outcome and the script always exits 0.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 0

failures=0

step() {
  echo
  echo "--- $1 ---"
}

step "installing pinned dependencies"
if pip install --no-cache-dir -r requirements-dev.txt; then
  echo "ok"
else
  echo "FAILED: run 'make install' by hand once the Codespace is up"
  failures=$((failures + 1))
fi

step "downloading the dataset"
if python scripts/download_data.py; then
  echo "ok"
else
  echo "FAILED: run 'make data' by hand; the benchmark cannot start without it"
  failures=$((failures + 1))
fi

step "verifying the container runtime"
# The whole reason this repository needs a devcontainer at all. Reported here
# so a broken Docker install is visible at creation time rather than twenty
# minutes into a benchmark run.
if bash scripts/check_runtime.sh; then
  echo
else
  echo
  echo "FAILED: see above. 'make doctor' re-runs this check."
  failures=$((failures + 1))
fi

echo
if [ "$failures" -eq 0 ]; then
  echo "=============================================="
  echo " Codespace ready."
  echo
  echo "   make env     generate .env for the four containers"
  echo "   make smoke   feasibility check"
  echo "   make suite   the real run"
  echo "=============================================="
else
  echo "=============================================="
  echo " Codespace created with $failures failed step(s) - see above."
  echo " The environment is usable; fix the steps that failed before running"
  echo " the benchmark. 'make doctor' re-checks the runtime."
  echo "=============================================="
fi

exit 0
