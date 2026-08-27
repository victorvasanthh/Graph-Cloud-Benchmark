"""Reduce a raw run to the summary the report and charts are built from.

The summary carries a concurrency dimension. `targets` holds the single-client
numbers, which is what the headline tables show; `by_concurrency` holds every
level that was measured, including level 1, so the scaling tables can be built
without special-casing the baseline.

Throughput is computed from the measured phase's wall clock, never by summing
per-request latencies. Under N parallel clients those latencies overlap in
time, and adding them up would report a throughput the server never delivered.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..core.results import BenchmarkResults, WorkloadRun
from ..metrics.latency import relative_to_baseline, summarise
from ..workloads.queries import BY_NAME
from .consistency import check_row_agreement

UNVERIFIABLE = "only one target produced results, so no cross-engine check was possible"


def build_summary(results: BenchmarkResults, baseline: str | None = None) -> dict[str, Any]:
    """Aggregate percentiles per (workload, concurrency, target), plus findings.

    `baseline` names the target that relative numbers are expressed against.
    It defaults to the first target that produced a usable result rather than
    to the product being promoted, because a table whose ratios are all
    computed against the vendor is a table that flatters the vendor by
    construction.
    """
    issues = check_row_agreement(results)
    flagged = {(issue.workload, issue.concurrency) for issue in issues}

    ok_targets = [run.target for run in results.runs if run.status == "ok" and run.measured_ns()]
    resolved_baseline = (
        baseline if baseline in ok_targets else (ok_targets[0] if ok_targets else None)
    )

    workloads: dict[str, Any] = {}
    for workload_name in results.workloads():
        spec = BY_NAME.get(workload_name)
        levels = results.concurrency_levels(workload_name)
        entry: dict[str, Any] = {
            "equivalence": spec.equivalence if spec else "n/a",
            "equivalence_note": spec.equivalence_note if spec else "",
            "description": spec.description if spec else "",
            "mutates": bool(spec.mutates) if spec else False,
            "concurrency_levels": levels,
            "row_counts_agree": not any((workload_name, lvl) in flagged for lvl in levels),
            "by_concurrency": {},
        }

        for level in levels:
            entry["by_concurrency"][str(level)] = {
                "row_counts_agree": (workload_name, level) not in flagged,
                "targets": _targets_at_level(results, workload_name, level, resolved_baseline),
            }

        # The headline tables read `targets`, which is the lowest level that was
        # actually measured - normally 1. Falling back to the lowest available
        # keeps a concurrency-only workload from rendering as an empty table.
        primary = str(levels[0]) if levels else "1"
        entry["targets"] = entry["by_concurrency"].get(primary, {}).get("targets", {})
        workloads[workload_name] = entry

    return {
        "manifest": asdict(results.manifest),
        "baseline": resolved_baseline,
        "workloads": workloads,
        "consistency_issues": [asdict(issue) for issue in issues],
        "consistency_verdict": _verdict(results, issues),
    }


def _targets_at_level(
    results: BenchmarkResults, workload: str, level: int, baseline: str | None
) -> dict[str, Any]:
    targets: dict[str, Any] = {}
    baseline_p50: float | None = None

    for run in results.runs:
        if run.workload != workload or run.concurrency != level:
            continue
        measured = run.measured_ns()
        if run.status != "ok" or not measured:
            targets[run.target] = {
                "status": run.status if run.status != "ok" else "failed",
                "note": run.note,
                "concurrency": level,
            }
            continue

        stats = summarise(measured, failures=run.failure_count())
        record = stats.as_row()
        record["status"] = "ok"
        record["concurrency"] = level
        record["wall_s"] = run.wall_ns / 1e9
        record["throughput_qps"] = _throughput(run)
        record["caveats"] = stats.caveats
        record["rows_returned"] = _modal_rows(run)
        record.update(run.scale)
        targets[run.target] = record
        if run.target == baseline:
            baseline_p50 = stats.p50_ms

    if baseline_p50 is not None:
        for record in targets.values():
            if record.get("status") != "ok":
                continue
            record["relative_p50"] = relative_to_baseline(float(record["p50_ms"]), baseline_p50)

    return targets


def _throughput(run: WorkloadRun) -> float:
    """Completed requests per second over the measured phase's wall clock.

    Wall clock rather than the reciprocal of mean latency: with N clients in
    flight those latencies overlap, and 1/mean would understate real
    throughput by roughly a factor of N.
    """
    completed = len(run.measured_ns())
    seconds = run.wall_ns / 1e9
    if completed == 0 or seconds <= 0:
        return 0.0
    return completed / seconds


def _modal_rows(run: WorkloadRun) -> int | None:
    """The row count these iterations most commonly returned.

    Reported so a reader can see at a glance that a fast target was not fast
    because it returned nothing.
    """
    counts: dict[int, int] = {}
    for iteration in run.iterations:
        if iteration.is_measured:
            counts[iteration.rows] = counts.get(iteration.rows, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda pair: pair[1])[0]


def _verdict(results: BenchmarkResults, issues: list[Any]) -> str:
    distinct_targets = {
        run.target for run in results.runs if run.status == "ok" and run.measured_ns()
    }
    if len(distinct_targets) < 2:
        return UNVERIFIABLE
    if issues:
        return (
            f"{len(issues)} iteration(s) across "
            f"{len({i.workload for i in issues})} workload(s) returned differing row counts; "
            "those workloads are not directly comparable until the cause is understood"
        )
    return "all cross-checked workloads returned matching row counts on every target"


def write_summary(summary: dict[str, Any], directory: Path, run_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return path
