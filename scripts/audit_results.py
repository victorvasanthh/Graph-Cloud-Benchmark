#!/usr/bin/env python3
"""What is already measured, and what still has to run.

    python scripts/audit_results.py

A killed run leaves a directory of raw files whose completeness is not obvious
from their names. This reads each one and reports, per target, which workloads
have a full measured sample, which recorded an honest failure or timeout, and
which are simply absent because the process died before reaching them.

The distinction that matters for a submission:

  complete   every configured workload was attempted and recorded an outcome
  partial    the run stopped early; workloads after the cut were never tried
  usable     complete OR partial-but-every-attempted-workload-finished

A partial run is not worthless. Workloads it did finish carry their full
sample, and a workload it honestly recorded as TIMEOUT is a result. What must
never happen is a half-filled workload being averaged as though it were whole,
and the summary layer already refuses that: a run whose status is not `ok`
contributes no latency to any statistic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark.core.config import load_config  # noqa: E402


def main() -> int:
    raw_dir = REPO_ROOT / "results" / "raw"
    files = sorted(raw_dir.glob("*.json"))
    if not files:
        print(f"no raw result files in {raw_dir}")
        return 1

    config = load_config()
    expected = {w.name for w in config.active_workloads()}
    wanted_iterations = config.run.measured_iterations

    # target -> {workload -> (status, measured_count, file)}
    by_target: dict[str, dict[str, tuple[str, int, str]]] = {}

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  UNREADABLE {path.name}: {exc}")
            continue
        manifest = payload.get("manifest", {})
        if manifest.get("measured_iterations", 0) < wanted_iterations:
            continue  # a smoke file, not a measurement
        for run in payload.get("runs", []):
            measured = sum(
                1 for it in run.get("iterations", []) if it.get("index", -1) >= 0 and it.get("ok")
            )
            slot = by_target.setdefault(run["target"], {})
            previous = slot.get(run["workload"])
            # Keep the most complete record if a target appears twice.
            if previous is None or measured > previous[1]:
                slot[run["workload"]] = (run["status"], measured, path.name)

    print(f"expecting {len(expected)} workload(s) at {wanted_iterations} measured iterations\n")

    finished: list[str] = []
    needs_run: list[str] = []

    for target in sorted(by_target):
        runs = by_target[target]
        attempted = {name for name in runs if name != "ingest"}
        missing = sorted(expected - attempted)
        full = [n for n, (s, c, _) in runs.items() if s == "ok" and c >= wanted_iterations]
        honest = [n for n, (s, _, _) in runs.items() if s in {"timeout", "failed", "unsupported"}]
        # `unavailable` is not an outcome about the engine - it means we never
        # reached it. A file full of these is a record of a failed attempt, not
        # a measurement, and letting it into a merge would put an empty target
        # in the comparison.
        unreachable = [n for n, (s, _, _) in runs.items() if s == "unavailable"]
        short = [
            n
            for n, (s, c, _) in runs.items()
            if s == "ok" and n != "ingest" and c < wanted_iterations
        ]

        source = next(iter(runs.values()))[2]
        print(f"  {target}   ({source})")
        print(f"     full sample     : {len(full)}  {sorted(full)}")
        if honest:
            print(f"     recorded failure: {len(honest)}  {sorted(honest)}")
        if short:
            print(f"     SHORT SAMPLE    : {len(short)}  {sorted(short)}  <- excluded from stats")
        if missing:
            print(f"     never attempted : {len(missing)}  {missing}")

        if unreachable:
            print(f"     never reached   : {len(unreachable)}  (target was unavailable)")

        if not full:
            needs_run.append(target)
            print("     -> NOT USABLE: no workload produced a measured sample")
        elif missing:
            needs_run.append(target)
            print("     -> INCOMPLETE: workloads after the cut were never tried")
        else:
            finished.append(target)
            print("     -> usable: every workload reached an outcome")
        print()

    configured = {t.name for t in config.active_targets()}
    never_ran = sorted(configured - set(by_target))
    if never_ran:
        print(f"  no result file at all: {never_ran}\n")
        needs_run.extend(never_ran)

    print(f"usable now      : {sorted(finished) or 'none'}")
    print(f"still to run    : {sorted(set(needs_run)) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
