#!/usr/bin/env python3
"""Turn a recorded run into a Markdown report and charts.

    python scripts/make_report.py                  # most recent run
    python scripts/make_report.py --run 20260827T101500Z
    python scripts/make_report.py --no-charts

Reads results/raw, recomputes the summary from the raw iterations rather than
trusting the stored one, and writes docs/report-<run_id>.md plus PNGs under
charts/. Recomputing is the point: the raw file is the record, and every
derived number in the report can be regenerated from it by anyone.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark.core.results import BenchmarkResults  # noqa: E402
from benchmark.reporting.consistency import summarise_issues  # noqa: E402
from benchmark.reporting.summary import build_summary, write_summary  # noqa: E402
from benchmark.reporting.tables import (  # noqa: E402
    render_conclusion,
    render_concurrency_table,
    render_footnotes,
    render_footprint,
    render_ingest_table,
    render_latency_table,
    render_limitations,
    render_status_table,
)


def latest_run(raw_dir: Path) -> Path:
    candidates = sorted(raw_dir.glob("*.json"))
    if not candidates:
        raise SystemExit(
            f"no runs found in {raw_dir}. Run `python scripts/run_benchmark.py` first."
        )
    return candidates[-1]


def build_markdown(summary: dict, chart_paths: list[Path]) -> str:
    manifest = summary["manifest"]
    sections = [
        f"# Benchmark report - {manifest['run_id']}",
        "",
        "Generated from `results/raw/{run}.json`. Every number below can be "
        "recomputed from that file with `scripts/make_report.py`.".format(run=manifest["run_id"]),
        "",
        "## Coverage",
        "",
        render_status_table(summary),
        "",
    ]

    if chart_paths:
        sections += ["## Charts", ""]
        for path in chart_paths:
            sections += [f"![{path.stem}]({path.as_posix()})", ""]

    sections += ["## Results", ""]
    ordered = [name for name in sorted(summary["workloads"]) if name != "ingest"]
    for workload in ordered:
        sections += [render_latency_table(summary, workload), ""]

    scaling = [table for table in (render_concurrency_table(summary, w) for w in ordered) if table]
    if scaling:
        sections += ["## Concurrency scaling", ""]
        for table in scaling:
            sections += [table, ""]

    ingest_table = render_ingest_table(summary)
    if ingest_table:
        sections += ["## Bulk load", "", ingest_table, ""]

    issues = summary.get("consistency_issues") or []
    if issues:
        from benchmark.reporting.consistency import ConsistencyIssue

        restored = [ConsistencyIssue(**issue) for issue in issues]
        sections += [
            "## Consistency findings",
            "",
            "These workloads returned different row counts on different engines. "
            "Until that is explained, their latencies are not measuring the same "
            "question and should not be compared.",
            "",
        ]
        sections += [f"- {line}" for line in summarise_issues(restored)]
        sections += [""]

    # Limitations before the conclusion, and both before the run conditions.
    # A reader who stops early should meet the caveats before the numbers have
    # had time to harden into an opinion.
    sections += [render_footprint(summary), ""]
    sections += [render_limitations(summary), ""]
    sections += [render_conclusion(summary), ""]
    sections += [render_footnotes(summary), ""]
    return "\n".join(sections)


def _link_from(chart: Path, docs_dir: Path) -> Path:
    """Path to `chart` as written inside a Markdown file living in `docs_dir`.

    Relative when the two share a root, absolute otherwise. `--charts-dir` and
    `--docs-dir` can point anywhere, including different Windows drives, and a
    relative path is a convenience rather than a requirement - failing to
    compute one must not discard a report that has already been generated.
    """
    try:
        return Path(os.path.relpath(chart, start=docs_dir))
    except ValueError:
        return chart.resolve()


def _echo(text: str) -> None:
    """Print the report, surviving a console that cannot encode it.

    The dagger used to mark unresolvable percentiles is not representable in
    every legacy Windows code page. The file on disk is always UTF-8; only
    this convenience echo degrades, and it degrades visibly rather than
    aborting a run that has already produced its results.
    """
    encoding = sys.stdout.encoding or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--run", help="run id; defaults to the most recent")
    parser.add_argument("--results-dir", type=Path, default=REPO_ROOT / "results")
    parser.add_argument("--charts-dir", type=Path, default=REPO_ROOT / "charts")
    parser.add_argument("--docs-dir", type=Path, default=REPO_ROOT / "docs")
    parser.add_argument("--baseline", help="target that relative numbers are expressed against")
    parser.add_argument("--no-charts", action="store_true")
    args = parser.parse_args()

    raw_dir = args.results_dir / "raw"
    path = raw_dir / f"{args.run}.json" if args.run else latest_run(raw_dir)
    if not path.exists():
        raise SystemExit(f"no such run: {path}")

    results = BenchmarkResults.read_raw(path)
    summary = build_summary(results, baseline=args.baseline)
    write_summary(summary, args.results_dir / "summary", results.manifest.run_id)

    chart_paths: list[Path] = []
    if not args.no_charts:
        try:
            from benchmark.reporting.charts import render_ingest_chart, render_latency_panels

            run_id = results.manifest.run_id
            chart_paths.append(
                render_latency_panels(summary, args.charts_dir / f"latency-{run_id}.png")
            )
            ingest_chart = render_ingest_chart(summary, args.charts_dir / f"ingest-{run_id}.png")
            if ingest_chart is not None:
                chart_paths.append(ingest_chart)
        except ImportError as exc:
            print(f"charts skipped: {exc}", file=sys.stderr)
        except ValueError as exc:
            print(f"charts skipped: {exc}", file=sys.stderr)

    args.docs_dir.mkdir(parents=True, exist_ok=True)
    doc_relative = [_link_from(chart, args.docs_dir) for chart in chart_paths]

    markdown = build_markdown(summary, doc_relative)
    report_path = args.docs_dir / f"report-{results.manifest.run_id}.md"
    # newline="" so Python does not translate line endings on Windows. The
    # report is a tracked file and .gitattributes requires LF; text mode would
    # write CRLF and fail the line-ending gate on every regeneration.
    with report_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(markdown)

    _echo(markdown)
    print(f"\nreport -> {report_path}", file=sys.stderr)
    for chart in chart_paths:
        print(f"chart  -> {chart}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
