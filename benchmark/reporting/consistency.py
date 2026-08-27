"""Cross-engine answer checking.

A latency table is worthless if the engines were not answering the same
question, and the cheapest way for a benchmark to be quietly wrong is for one
target to return an empty result very quickly.

Every workload here is written so that all dialects return the same number of
rows for the same parameters. That makes row count a usable invariant: for a
given workload, concurrency level and iteration, every healthy target should
agree. Where they do not, something is wrong with the query translation, the
data model or the load - and that is a finding about the benchmark, not about
the database.

Comparisons are scoped to a single concurrency level. Iteration indices mean
the same thing at every level, but comparing a level-1 iteration against a
level-40 one would compare different runs, and any disagreement found that way
would say nothing about either engine.

This deliberately checks row *counts* rather than row *contents*. Contents
would be stronger, but pulling every result back to compare it would change
what the timed path does, and a check that alters the measurement is worse
than a slightly weaker check that does not.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.results import BenchmarkResults


@dataclass
class ConsistencyIssue:
    workload: str
    iteration: int
    counts: dict[str, int]
    concurrency: int = 1

    def describe(self) -> str:
        parts = ", ".join(f"{target}={rows}" for target, rows in sorted(self.counts.items()))
        level = "" if self.concurrency == 1 else f" at concurrency {self.concurrency}"
        return f"{self.workload}{level} iteration {self.iteration}: row counts disagree ({parts})"


def check_row_agreement(
    results: BenchmarkResults, ignore_workloads: tuple[str, ...] = ("ingest",)
) -> list[ConsistencyIssue]:
    """Find iterations where healthy targets returned different row counts."""
    issues: list[ConsistencyIssue] = []

    groups: dict[tuple[str, int], dict[str, dict[int, int]]] = {}
    for run in results.runs:
        if run.workload in ignore_workloads or run.status != "ok":
            continue
        key = (run.workload, run.concurrency)
        groups.setdefault(key, {})[run.target] = {
            it.index: it.rows for it in run.iterations if it.is_measured
        }

    for (workload, concurrency), by_target in sorted(groups.items()):
        if len(by_target) < 2:
            # Nothing to cross-check against. Not an issue, just an
            # unverifiable row - reported by the summary as such.
            continue

        shared_iterations = set.intersection(*(set(v) for v in by_target.values()))
        for index in sorted(shared_iterations):
            counts = {target: rows[index] for target, rows in by_target.items()}
            if len(set(counts.values())) > 1:
                issues.append(
                    ConsistencyIssue(
                        workload=workload,
                        iteration=index,
                        counts=counts,
                        concurrency=concurrency,
                    )
                )

    return issues


def summarise_issues(issues: list[ConsistencyIssue], max_shown: int = 10) -> list[str]:
    """Collapse per-iteration issues into one line per workload and level.

    A translation error usually breaks every iteration of a workload, so
    printing all of them buries the finding in repetition.
    """
    grouped: dict[tuple[str, int], list[ConsistencyIssue]] = {}
    for issue in issues:
        grouped.setdefault((issue.workload, issue.concurrency), []).append(issue)

    lines: list[str] = []
    for (workload, concurrency), group in sorted(grouped.items()):
        example = group[0]
        parts = ", ".join(f"{t}={r}" for t, r in sorted(example.counts.items()))
        level = "" if concurrency == 1 else f" at concurrency {concurrency}"
        lines.append(
            f"{workload}{level}: {len(group)} iteration(s) disagree on row count; "
            f"first at iteration {example.iteration} ({parts})"
        )
    return lines[:max_shown]
