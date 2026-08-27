#!/usr/bin/env python3
"""Run the benchmark and write results/raw + results/summary.

    python scripts/run_benchmark.py                       # everything configured
    python scripts/run_benchmark.py --target memgraph     # one target
    python scripts/run_benchmark.py --workload point_lookup --iterations 200
    python scripts/run_benchmark.py --dry-run             # show the plan, measure nothing

Targets with no credentials in the environment are skipped and reported as
skipped. That is not the same as passing, and the report says so.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark.core.config import load_config  # noqa: E402
from benchmark.core.errors import ConfigurationError  # noqa: E402
from benchmark.datasets import load_cit_hepth  # noqa: E402
from benchmark.reporting.summary import build_summary, write_summary  # noqa: E402
from benchmark.reporting.tables import render_status_table  # noqa: E402
from benchmark.runners import Progress, run_benchmark  # noqa: E402
from benchmark.runners.readiness import wait_for  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--target", action="append", help="limit to this target; repeatable")
    parser.add_argument("--workload", action="append", help="limit to this workload; repeatable")
    parser.add_argument("--iterations", type=int, help="override measured_iterations")
    parser.add_argument("--warmup", type=int, help="override warmup_iterations")
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="do not reload the data; assumes every target already holds it, and verifies that",
    )
    parser.add_argument(
        "--baseline",
        help="target that relative numbers are expressed against (default: first with results)",
    )
    parser.add_argument("--config-dir", type=Path, default=REPO_ROOT / "config")
    parser.add_argument("--results-dir", type=Path, default=REPO_ROOT / "results")
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument(
        "--no-verify-checksum",
        action="store_true",
        help="skip dataset checksum verification (only for a deliberately modified dataset)",
    )
    parser.add_argument(
        "--run-id",
        help=(
            "name this run instead of using a timestamp; lets an orchestrating script "
            "know the output path in advance rather than guessing at the newest file"
        ),
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=60.0,
        help=(
            "wait this long for each target to answer a query before measuring "
            "(0 disables). Guards against measuring a container that is still booting."
        ),
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=60.0,
        help=(
            "wait this long for each target to answer a query before measuring "
            "(0 disables). Guards against measuring a container that is still booting."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        config = load_config(
            config_dir=args.config_dir,
            only_targets=args.target,
            only_workloads=args.workload,
        )
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.iterations is not None:
        config.run.measured_iterations = args.iterations
    if args.warmup is not None:
        config.run.warmup_iterations = args.warmup
    try:
        config.run.validate()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    active = config.active_targets()
    skipped = config.skipped_targets()

    print(f"targets:   {', '.join(t.name for t in active) or '(none)'}")
    if skipped:
        for target in skipped:
            reason = ", ".join(target.missing) + " unset" if target.missing else "disabled"
            print(f"  skipping {target.name}: {reason}")
    print(f"workloads: {', '.join(w.name for w in config.active_workloads()) or '(none)'}")
    print(
        f"iterations: {config.run.measured_iterations} measured "
        f"+ {config.run.warmup_iterations} warmup, seed {config.run.seed}"
    )

    if not active:
        print(
            "\nNothing to measure: no target has its credentials in the environment.\n"
            "Copy .env.example to .env and fill in at least one target.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print("\ndry run: nothing was measured")
        return 0

    if args.wait_seconds > 0:
        # Probed before the dataset is parsed: reading 350k edges takes seconds
        # we would rather not spend discovering that nothing is listening.
        #
        # The probe is the real adapter executing a real statement. A weaker
        # check - a TCP connect, a port scan, the container health flag - can
        # pass while the engine is still refusing queries, which would turn a
        # startup race into a row of measurements that never happened.
        print("\nwaiting for targets to accept queries...")
        for target in active:

            def report(attempt: int, detail: str, remaining: int, name=target.name) -> None:
                print(f"  {name}: not ready ({detail}); {remaining}s left", flush=True)

            ready, detail = wait_for(target, args.wait_seconds, on_attempt=report)
            if ready:
                print(f"  {target.name}: ready")
            else:
                # Not fatal. The runner records an unreachable target as
                # unavailable in every table, which is the honest outcome. A
                # hard exit here would discard the targets that *are* up.
                print(f"  {target.name}: NOT ready - {detail}", file=sys.stderr)

    print("\nloading dataset...")
    graph = load_cit_hepth(data_dir=args.data_dir, verify=not args.no_verify_checksum)
    print(
        f"  {graph.node_count:,} nodes, {graph.edge_count:,} edges "
        f"({graph.self_loops} self-loops dropped), "
        f"{graph.date_coverage:.1%} of nodes carry a publication date"
    )

    results = run_benchmark(config, graph, progress=Progress(quiet=args.quiet))
    if args.run_id:
        results.manifest.run_id = args.run_id

    raw_path = results.write_raw(args.results_dir / "raw")
    summary = build_summary(results, baseline=args.baseline)
    summary_path = write_summary(summary, args.results_dir / "summary", results.manifest.run_id)

    print("\n" + render_status_table(summary))
    print(f"\nconsistency: {summary['consistency_verdict']}")
    print(f"\nraw     -> {raw_path}")
    print(f"summary -> {summary_path}")
    print(f"\nNext: python scripts/make_report.py --run {results.manifest.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
