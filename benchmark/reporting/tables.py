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


def _configured_targets() -> list[str]:
    """Every target declared in config, whether or not it produced results."""
    try:
        from ..core.config import load_config

        return [t.name for t in load_config().targets]
    except Exception:
        # Reporting must not fail because config is unreadable from wherever
        # this is being rendered.
        return []


def render_limitations(summary: dict[str, Any]) -> str:
    """What this run does not support, derived from the run itself.

    Written from the summary rather than by hand, on purpose. A limitations
    section composed from memory drifts away from the data it describes, and
    the drift always runs in the flattering direction. Every statement below is
    generated from what the manifest and the per-target records actually say,
    so it cannot claim a target was measured when it was not.
    """
    workloads = summary["workloads"]
    targets = sorted({t for entry in workloads.values() for t in entry["targets"]})

    unavailable: list[str] = []
    unverified_index: list[str] = []
    timed_out: dict[str, list[str]] = {}
    failed: dict[str, list[str]] = {}
    unresolved_p99: set[str] = set()

    for target in targets:
        statuses = {
            name: entry["targets"].get(target, {}).get("status")
            for name, entry in workloads.items()
        }
        if statuses and all(s in {"unavailable", None} for s in statuses.values()):
            unavailable.append(target)
            continue
        for name, entry in workloads.items():
            record = entry["targets"].get(target, {})
            status = record.get("status")
            if status == "timeout":
                timed_out.setdefault(target, []).append(name)
            elif status in {"failed", "unsupported"}:
                failed.setdefault(target, []).append(name)
            if any(c.startswith("p99") for c in record.get("caveats", ())):
                unresolved_p99.add(target)

        ingest = workloads.get("ingest", {}).get("targets", {}).get(target, {})
        if ingest.get("index_verified") is not True:
            unverified_index.append(target)

    lines = [
        "## Limitations",
        "",
        "Generated from this run's own record, not written from memory.",
        "",
    ]

    # A target that produced no result file is invisible in every table above,
    # and an absent row reads as "not part of the comparison" when the truth is
    # "we failed to measure it". Naming it here is the only place that can be
    # said, because there is no row to carry the caveat.
    absent = [name for name in _configured_targets() if name not in targets]
    if absent:
        lines += [
            f"**No results at all: {', '.join(absent)}.** These targets are configured "
            "but produced no usable result file, so they appear in no table above. "
            "Their absence is a gap in this benchmark, not a judgement about them: "
            "nothing here says whether they would have been faster or slower. Any "
            "comparison drawn from this report covers only the targets that "
            "actually ran.",
            "",
        ]

    if unavailable:
        lines += [
            f"**Not measured at all: {', '.join(unavailable)}.** These targets appear in "
            "every table as `not reachable`, which is different from and less "
            "flattering than being absent. Nothing about their performance is "
            "claimed or implied here.",
            "",
        ]

    if unverified_index:
        lines += [
            f"**Index unconfirmed: {', '.join(unverified_index)}.** The Paper(id) index "
            "could not be verified by catalogue introspection or by query plan. "
            "An engine reading without the index every other engine has is "
            "answering an easier question, so **its read latencies are not "
            "comparable and must not be quoted against the others**, however "
            "favourable they look.",
            "",
        ]

    if timed_out:
        detail = "; ".join(f"{t}: {', '.join(sorted(w))}" for t, w in sorted(timed_out.items()))
        lines += [
            f"**Timed out: {detail}.** The engine accepted the query and was still "
            "working when the bound expired. That is a statement about the engine "
            "at this resource cap, not about the query being wrong, and no latency "
            "from a timed-out workload reaches any statistic.",
            "",
        ]

    if failed:
        detail = "; ".join(f"{t}: {', '.join(sorted(w))}" for t, w in sorted(failed.items()))
        lines += [
            f"**Failed or unsupported: {detail}.** Recorded as failures rather than "
            "omitted, so a gap in the comparison is visible rather than silent.",
            "",
        ]

    if unresolved_p99:
        lines += [
            f"**p99 not resolvable for {', '.join(sorted(unresolved_p99))}** at this "
            "sample size: nearest-rank p99 needs 100 observations before it is "
            "distinguishable from the maximum. Those cells are marked and should "
            "be read as the maximum.",
            "",
        ]

    loose = [n for n, e in workloads.items() if e.get("equivalence") == "loose"]
    if loose:
        lines += [
            f"**Loose equivalence: {', '.join(sorted(loose))}.** The engines are asked "
            "for the same answer but reach it by materially different means, so "
            "this row compares engine-plus-optimiser rather than raw traversal "
            "speed. Do not quote its ratio on its own.",
            "",
        ]

    lines += [
        "**Structural limits that apply to every number here**, regardless of which targets ran:",
        "",
        "- One client, one connection, at each stated concurrency level. This is "
        "a latency benchmark; it does not measure capacity.",
        "- One dataset at one size. An engine that wins on 27,770 nodes may lose "
        "at a hundred times that, and nothing here predicts which.",
        "- Read-heavy. Only the bulk load and the mixed workload write at all.",
        "- Nothing about durability, replication, failover, backup or operability.",
        "- A single run. Free-tier instances share hardware and drift; a gap "
        "smaller than the spread between repeat runs is not a finding.",
        "",
    ]
    return "\n".join(lines)


def render_conclusion(summary: dict[str, Any]) -> str:
    """What may and may not be concluded, given which targets actually ran."""
    workloads = summary["workloads"]
    targets = sorted({t for entry in workloads.values() for t in entry["targets"]})

    measured = []
    for target in targets:
        ok = any(
            entry["targets"].get(target, {}).get("status") == "ok" for entry in workloads.values()
        )
        if ok:
            measured.append(target)

    ingest_targets = workloads.get("ingest", {}).get("targets", {})
    comparable = [t for t in measured if ingest_targets.get(t, {}).get("index_verified") is True]
    non_comparable = [t for t in measured if t not in comparable]

    lines = [
        "## Conclusion",
        "",
        f"**Measured: {', '.join(measured) if measured else 'nothing'}.**",
        "",
    ]

    if comparable:
        lines += [
            f"**Directly comparable: {', '.join(comparable)}.** These ran the same "
            "workloads, with the same seeded parameters in the same order, under "
            "verified equivalent indexes, one target at a time so none contended "
            "with another. Differences between them are attributable to the "
            "engines and their configuration.",
            "",
        ]
    if non_comparable:
        lines += [
            f"**Present but not directly comparable: {', '.join(non_comparable)}.** "
            "See the limitations above for why in each case. Their rows are "
            "published because omitting them would hide the gap, not because "
            "they support a ranking.",
            "",
        ]

    lines += [
        "The honest summary of any single run of this kind is narrow: it says "
        "how these builds behaved on this dataset, at this size, under these "
        "caps, from this client, on this day. It does not establish that one "
        "engine is faster than another in general, and the raw per-iteration "
        "timings are committed alongside precisely so that anyone who "
        "disagrees with the statistics can compute their own from the same "
        "observations.",
        "",
    ]
    return "\n".join(lines)
