#!/usr/bin/env bash
#
# Confirm the container runtime is present AND actually enforces resource
# limits. Run inside the Codespace:
#
#   bash scripts/check_runtime.sh      # or: make doctor
#
# The version checks are the obvious part. The cgroup check is the one that
# matters to this project: docker-in-docker can come up healthy and still
# silently ignore --memory and --cpus when cgroup delegation is incomplete.
# A run under a cap that was never applied looks exactly like a successful
# resource-parity benchmark and is worthless. Better to find out in thirty
# seconds than after a full suite.

set -uo pipefail

PROBE_IMAGE="busybox:1.36"
failures=0

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }
info() { printf '        %s\n' "$1"; }

echo "== container runtime =="

if command -v docker >/dev/null 2>&1; then
  pass "docker present: $(docker --version)"
else
  fail "docker not found on PATH"
  echo
  echo "The docker-in-docker feature did not install. If Codespace creation"
  echo "reported a Yarn APT key error (NO_PUBKEY), the .devcontainer/Dockerfile"
  echo "repair did not take effect - rebuild the container."
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  pass "compose present: $(docker compose version)"
else
  fail "'docker compose' not available (the v2 plugin is required, not docker-compose v1)"
fi

if docker info >/dev/null 2>&1; then
  pass "daemon reachable"
else
  fail "docker daemon not reachable"
  info "inside a Codespace the nested daemon can take a few seconds after attach"
  exit 1
fi

echo
echo "== resource limit enforcement =="

if ! docker image inspect "$PROBE_IMAGE" >/dev/null 2>&1; then
  info "pulling $PROBE_IMAGE (small, one-off)"
  if ! docker pull --quiet "$PROBE_IMAGE" >/dev/null 2>&1; then
    fail "could not pull $PROBE_IMAGE; cannot verify limit enforcement"
    info "check outbound network access from the Codespace"
    echo
    exit 1
  fi
fi

# 1 vCPU and 512 MB, then ask the kernel inside the container what it got.
# cpu.max reports "<quota> <period>"; 100000 100000 is exactly one CPU.
probe_cpu="$(docker run --rm --cpus=1 "$PROBE_IMAGE" cat /sys/fs/cgroup/cpu.max 2>/dev/null)"
probe_mem="$(docker run --rm --memory=512m "$PROBE_IMAGE" cat /sys/fs/cgroup/memory.max 2>/dev/null)"

if [ -z "$probe_cpu" ]; then
  fail "could not read cpu.max from a test container (cgroup v2 not visible?)"
elif [ "$probe_cpu" = "max" ] || [ "${probe_cpu%% *}" = "max" ]; then
  fail "--cpus was accepted but NOT enforced (cpu.max reports '$probe_cpu')"
  info "resource parity cannot be claimed on this host; do not publish a run"
else
  quota="${probe_cpu%% *}"
  period="${probe_cpu##* }"
  pass "CPU limit enforced: cpu.max='$probe_cpu' (~$((quota / period)) vCPU)"
fi

if [ -z "$probe_mem" ]; then
  fail "could not read memory.max from a test container"
elif [ "$probe_mem" = "max" ]; then
  fail "--memory was accepted but NOT enforced (memory.max reports 'max')"
  info "resource parity cannot be claimed on this host; do not publish a run"
else
  mib=$((probe_mem / 1024 / 1024))
  pass "memory limit enforced: memory.max=$probe_mem (~${mib} MiB)"
  if [ "$mib" -lt 500 ] || [ "$mib" -gt 524 ]; then
    fail "memory limit is ${mib} MiB but 512 MiB was requested"
  fi
fi

echo
echo "== host capacity =="
if command -v python >/dev/null 2>&1 && [ -f "$(dirname "$0")/probe_limits.py" ]; then
  python "$(dirname "$0")/probe_limits.py" || true
else
  info "probe_limits.py not runnable here; run 'make probe' separately"
fi

echo
if [ "$failures" -eq 0 ]; then
  echo "runtime OK: docker, compose, and enforced cgroup limits all verified"
  exit 0
fi

echo "runtime check failed with $failures problem(s)"
exit 1
