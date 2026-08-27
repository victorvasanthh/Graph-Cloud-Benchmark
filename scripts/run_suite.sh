#!/usr/bin/env bash
#
# Measure every configured target, one self-hosted container at a time.
#
#   scripts/run_suite.sh --smoke     # 1 iteration each, proves the plumbing
#   scripts/run_suite.sh             # the full run
#
# Why sequential rather than all containers at once:
#
#   * the configured caps total 4 vCPU and 8 GB, which does not fit on a 2-core
#     Codespace at all; and
#   * even where it fits, three idle databases holding page cache while a
#     fourth is measured means measuring the host, not the engine.
#
# Each target writes its own raw file; scripts/merge_runs.py joins them and
# refuses to do so unless the runs are genuinely comparable. The merged
# manifest records that the targets were measured sequentially.
#
# Intended to run inside the Codespace. It does not install anything.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE=(docker compose -f infra/docker-compose.yml --env-file .env)
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RAW_DIR="results/raw"
SMOKE=0
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --smoke) SMOKE=1; shift ;;
    --) shift; EXTRA_ARGS+=("$@"); break ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

if [[ $SMOKE -eq 1 ]]; then
  # Deliberately tiny. A smoke run proves connectivity, schema, ingest
  # verification and every query dialect parse; it is not a measurement and
  # its output must never be quoted as one.
  EXTRA_ARGS+=(--iterations 1 --warmup 0)
  STAMP="smoke-${STAMP}"
  echo "SMOKE RUN: 1 iteration, no warmup. Results prove the plumbing, not performance."
fi

if [[ ! -f .env ]]; then
  echo "no .env found. Copy .env.example to .env and fill in the targets you have." >&2
  exit 1
fi

# service name in docker-compose.yml : target name in config/databases.yaml
SELF_HOSTED=(
  "neo4j:neo4j-selfhosted"
  "memgraph:memgraph"
  "falkordb:falkordb"
  "arangodb:arangodb"
)
MANAGED=(cognodb-cloud aura-free)

PRODUCED=()

wait_healthy() {
  local service="$1" deadline=$((SECONDS + 300))
  echo "  waiting for ${service} to report healthy..."
  while (( SECONDS < deadline )); do
    local state
    state="$("${COMPOSE[@]}" ps --format '{{.Health}}' "$service" 2>/dev/null | head -1 || true)"
    case "$state" in
      healthy) echo "  ${service} healthy"; return 0 ;;
      unhealthy) echo "  ${service} reported unhealthy" >&2; return 1 ;;
    esac
    sleep 5
  done
  echo "  ${service} did not become healthy within 300s" >&2
  "${COMPOSE[@]}" logs --tail=40 "$service" >&2 || true
  return 1
}

teardown() {
  local service="${1:-}"
  if [[ -n "$service" ]]; then
    "${COMPOSE[@]}" rm -sf "$service" >/dev/null 2>&1 || true
  fi
}

for pair in "${SELF_HOSTED[@]}"; do
  service="${pair%%:*}"
  target="${pair##*:}"
  run_id="${STAMP}-${target}"
  echo
  echo "=============================================================="
  echo "  ${target}  (container: ${service})"
  echo "=============================================================="

  "${COMPOSE[@]}" up -d "$service"

  if ! wait_healthy "$service"; then
    # A container that will not start under its cap is a finding about that
    # engine at that cap, not a reason to abandon the suite.
    echo "  SKIPPED: ${target} never became healthy under its resource cap" >&2
    teardown "$service"
    continue
  fi

  echo "  --- enforced limits ---"
  python scripts/probe_limits.py || echo "  (limit findings above; recorded, continuing)"

  if python scripts/run_benchmark.py \
      --target "$target" \
      --run-id "$run_id" \
      "${EXTRA_ARGS[@]}"; then
    PRODUCED+=("${RAW_DIR}/${run_id}.json")
  else
    echo "  ${target} run exited non-zero; see output above" >&2
  fi

  teardown "$service"
done

# Managed targets need no container and can share one invocation: they are
# separate services on separate hardware, so they do not contend with
# each other the way two local containers would.
configured_managed=()
for target in "${MANAGED[@]}"; do
  if python scripts/run_benchmark.py --target "$target" --dry-run >/dev/null 2>&1; then
    configured_managed+=(--target "$target")
  else
    echo "skipping ${target}: not configured in .env"
  fi
done

if [[ ${#configured_managed[@]} -gt 0 ]]; then
  echo
  echo "=============================================================="
  echo "  managed targets"
  echo "=============================================================="
  run_id="${STAMP}-managed"
  if python scripts/run_benchmark.py "${configured_managed[@]}" \
      --run-id "$run_id" "${EXTRA_ARGS[@]}"; then
    PRODUCED+=("${RAW_DIR}/${run_id}.json")
  fi
fi

echo
if [[ ${#PRODUCED[@]} -eq 0 ]]; then
  echo "no target produced results; nothing to merge" >&2
  exit 1
fi

MERGED="${RAW_DIR}/${STAMP}-combined.json"
python scripts/merge_runs.py "${PRODUCED[@]}" -o "$MERGED"
python scripts/make_report.py --run "$(basename "$MERGED" .json)"

echo
echo "suite complete: ${#PRODUCED[@]} run(s) merged into ${MERGED}"
