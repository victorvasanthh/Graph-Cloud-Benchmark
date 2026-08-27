"""Console and Markdown tables.

Formatting rules that exist for honesty rather than for looks:

  * a target that failed, was unsupported or was never configured shows the
    reason in its cell. Blank cells invite the reader to assume a zero.
  * percentiles that cannot be distinguished from the maximum at the sample
    size used are marked with a dagger rather than printed bare.
  * relative-to-baseline numbers are omitted for workloads whose row counts
    disagreed across engines, because a ratio between two different questions
    is not a speedup.
"""

from __future__ import annotations

from typing import Any

from tabulate import tabulate

STATUS_LABEL = {
    "unavailable": "not reachable",
    "unsupported": "n/a",
    "failed": "failed",
    # Kept distinct from "failed" on purpose: the engine accepted the query and
    # was still working when the bound expired. That is a statement about the
    # engine at this resource cap, not about the query being wrong.
    "timeout": "TIMEOUT",
    "skipped": "not configured",
}

DAGGER = "†"


def render_latency_table(
    summary: dict[str, Any], workload: str, table_format: str = "github"
) -> str:
    """One table per workload: a row per target."""
    entry = summary["workloads"][workload]
    baseline = summary.get("baseline")
    comparable = entry.get("row_counts_agree", True)

    rows: list[list[str]] = []
    for target, record in sorted(entry["targets"].items()):
        if record.get("status") != "ok":
            reason = STATUS_LABEL.get(record.get("status", ""), record.get("status", "?"))
            note = record.get("note") or ""
            detail = f"{reason} - {note}" if note else reason
            # Dashes rather than blanks in the numeric columns. An empty cell in
            # a latency table is read as a zero often enough to be worth ruling
            # out typographically.
            rows.append([target, "-", "-", "-", "-", "-", "-", detail[:70]])
            continue

        caveats = record.get("caveats") or []
        marked = {c.split()[0] for c in caveats if c.split()[0].startswith("p")}
        relative = record.get("relative_p50")
        if relative is None or not comparable:
            relative_text = "-"
        elif target == baseline:
            relative_text = "1.00x (baseline)"
        else:
            relative_text = f"{relative:.2f}x"

        rows.append(
            [
                target,
                f"{record['n']}",
                f"{record['p50_ms']:.2f}",
                f"{record['p95_ms']:.2f}" + (DAGGER if "p95" in marked else ""),
                f"{record['p99_ms']:.2f}" + (DAGGER if "p99" in marked else ""),
                relative_text,
                f"{record.get('rows_returned', '?')}",
                "ok" if not record.get("failures") else f"{record['failures']} failed",
            ]
        )

    headers = ["target", "n", "p50 ms", "p95 ms", "p99 ms", "vs baseline", "rows", "status"]
    table = tabulate(rows, headers=headers, tablefmt=table_format, disable_numparse=True)

    lines = [f"### {workload}", "", entry.get("description", ""), "", table]
    if not comparable:
        lines += [
            "",
            "> Row counts disagreed across engines for this workload, so the "
            "relative column is suppressed. See the consistency section.",
        ]
    if entry.get("equivalence") == "loose":
        lines += ["", f"> Equivalence: loose. {entry.get('equivalence_note', '')}"]
    return "\n".join(line for line in lines if line is not None)


def render_status_table(summary: dict[str, Any], table_format: str = "github") -> str:
    """A single grid of statuses, so gaps in coverage are impossible to miss."""
    workloads = sorted(summary["workloads"])
    targets = sorted(
        {target for entry in summary["workloads"].values() for target in entry["targets"]}
    )

    rows: list[list[str]] = []
    for target in targets:
        row = [target]
        for workload in workloads:
            record = summary["workloads"][workload]["targets"].get(target)
            if record is None:
                row.append("-")
            elif record.get("status") == "ok":
                row.append("ok")
            else:
                row.append(STATUS_LABEL.get(record.get("status", ""), "?"))
        rows.append(row)

    return tabulate(rows, headers=["target", *workloads], tablefmt=table_format)


def render_footnotes(summary: dict[str, Any]) -> str:
    """Everything a reader needs in order to discount the numbers correctly."""
    manifest = summary["manifest"]
    lines = [
        "### Run conditions",
        "",
        f"- run id: `{manifest['run_id']}`",
        f"- started: {manifest['started_at']}, finished: {manifest.get('finished_at')}",
        f"- dataset: {manifest['dataset']} "
        f"({manifest['dataset_nodes']:,} nodes, {manifest['dataset_edges']:,} edges)",
        f"- iterations: {manifest['measured_iterations']} measured "
        f"after {manifest['warmup_iterations']} warmup, seed {manifest['seed']}",
        f"- client: Python {manifest['python_version']} on {manifest['platform']}",
        "",
        f"{DAGGER} at this sample size the percentile equals the observed maximum.",
        "",
        "### Consistency",
        "",
        f"{summary.get('consistency_verdict', 'not evaluated')}",
    ]
    if manifest.get("notes"):
        lines += ["", "### Notes", ""]
        lines += [f"- {note}" for note in manifest["notes"]]
    return "\n".join(lines)


def render_concurrency_table(
    summary: dict[str, Any], workload: str, table_format: str = "github"
) -> str | None:
    """Latency and achieved throughput as client concurrency rises.

    Returns None when the workload was only measured at a single level, so the
    report does not carry a scaling table that shows no scaling.

    Both numbers are needed together. Latency alone rewards an engine that
    queues requests until they time out; throughput alone hides an engine that
    doubled its p95 to get there.
    """
    entry = summary["workloads"][workload]
    levels = entry.get("concurrency_levels") or []
    if len(levels) < 2:
        return None

    targets = sorted(
        {target for level in levels for target in entry["by_concurrency"][str(level)]["targets"]}
    )

    rows: list[list[str]] = []
    for target in targets:
        row = [target]
        for level in levels:
            record = entry["by_concurrency"][str(level)]["targets"].get(target)
            if record is None:
                row.append("-")
            elif record.get("status") != "ok":
                row.append(STATUS_LABEL.get(record.get("status", ""), "?"))
            else:
                row.append(
                    f"{record['p50_ms']:.2f} / {record['p95_ms']:.2f} / "
                    f"{record['throughput_qps']:,.0f}"
                )
        rows.append(row)

    headers = ["target", *(f"c={level}" for level in levels)]
    table = tabulate(rows, headers=headers, tablefmt=table_format, disable_numparse=True)
    return "\n".join(
        [
            f"### {workload} - concurrency scaling",
            "",
            "Cells are `p50 ms / p95 ms / requests per second`. Throughput is "
            "measured from the wall clock of the concurrent phase, not from "
            "1/mean-latency, which would overstate it by roughly the client "
            "count.",
            "",
            table,
        ]
    )


def render_ingest_table(summary: dict[str, Any], table_format: str = "github") -> str | None:
    """Bulk load: wall time, throughput, and what the server actually holds."""
    entry = summary["workloads"].get("ingest")
    if not entry:
        return None

    rows: list[list[str]] = []
    for target, record in sorted(entry["targets"].items()):
        if record.get("status") != "ok":
            reason = STATUS_LABEL.get(record.get("status", ""), record.get("status", "?"))
            note = (record.get("note") or "")[:70]
            rows.append([target, "-", "-", "-", "-", f"{reason} - {note}" if note else reason])
            continue
        index = record.get("index_verified")
        index_text = {True: "yes", False: "NO", None: "unknown"}.get(index, "unknown")
        rows.append(
            [
                target,
                f"{record['p50_ms'] / 1000:,.1f}",
                f"{record.get('edges_per_second', 0):,.0f}",
                f"{record.get('nodes_loaded', 0):,} / {record.get('edges_loaded', 0):,}",
                index_text,
                "verified",
            ]
        )

    headers = ["target", "load s", "edges/s", "nodes / edges held", "indexed", "status"]
    table = tabulate(rows, headers=headers, tablefmt=table_format, disable_numparse=True)
    return "\n".join(
        [
            "### ingest (bulk load)",
            "",
            "Wall time for the batched load, the implied edge throughput, and "
            "the counts the server itself reported afterwards. A target whose "
            "counts did not match the dataset is marked failed: its read "
            "numbers would describe a smaller graph. `indexed` is the "
            "separately confirmed presence of the Paper(id) index - a `NO` "
            "there means that target was measured without the index every "
            "other target had, and its read rows are not comparable.",
            "",
            table,
        ]
    )
