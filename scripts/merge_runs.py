#!/usr/bin/env python3
"""Combine per-target raw runs into one comparable result file.

    python scripts/merge_runs.py results/raw/2026*.json -o results/raw/combined.json

Self-hosted targets are measured one container at a time - on a 2 vCPU
Codespace four capped containers cannot run at once, and even where they can,
three idle databases competing for page cache would be measuring the host
rather than the engine. Each target therefore produces its own raw file, and
this script joins them so the report can compare them.

The join is refused unless the runs are actually comparable: same dataset,
same seed, same iteration counts, same schema version. Merging runs that
differ in any of those would produce a table whose columns were produced by
different experiments, which is the failure this whole harness is built to
avoid.

The merged manifest records every source run id and start time, so the fact
that the targets were measured sequentially rather than simultaneously stays
visible in the output.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark.core.results import BenchmarkResults, RunManifest  # noqa: E402

#: Manifest fields that must agree, with the reason each one matters.
MUST_MATCH = {
    "schema_version": "written by a different version of the harness",
    "dataset": "a different dataset entirely",
    "dataset_nodes": "a different number of nodes was loaded",
    "dataset_edges": "a different number of edges was loaded",
    "seed": "different parameters, so the engines were asked different questions",
    "warmup_iterations": "a different warmup, so the caches were in different states",
    "measured_iterations": "a different sample size",
}


def merge(paths: list[Path]) -> BenchmarkResults:
    loaded = [BenchmarkResults.read_raw(path) for path in paths]
    if not loaded:
        raise SystemExit("no runs to merge")

    reference = loaded[0].manifest
    for path, results in zip(paths[1:], loaded[1:], strict=True):
        for field, why in MUST_MATCH.items():
            expected = getattr(reference, field)
            actual = getattr(results.manifest, field)
            if expected != actual:
                raise SystemExit(
                    f"refusing to merge {path.name}: {field} is {actual!r}, "
                    f"but {paths[0].name} has {expected!r} - {why}"
                )

    merged_manifest = RunManifest(
        run_id=f"merged-{reference.run_id}",
        started_at=min(r.manifest.started_at for r in loaded),
        schema_version=reference.schema_version,
        finished_at=max((r.manifest.finished_at or "") for r in loaded) or None,
        dataset=reference.dataset,
        dataset_nodes=reference.dataset_nodes,
        dataset_edges=reference.dataset_edges,
        warmup_iterations=reference.warmup_iterations,
        measured_iterations=reference.measured_iterations,
        seed=reference.seed,
        python_version=reference.python_version,
        platform=reference.platform,
        processor=reference.processor,
    )
    merged_manifest.notes.append(
        f"merged from {len(loaded)} run(s) measured sequentially, not simultaneously: "
        + ", ".join(f"{r.manifest.run_id} ({r.manifest.started_at})" for r in loaded)
    )

    merged = BenchmarkResults(manifest=merged_manifest)
    seen: set[tuple[str, str, int]] = set()
    for source, results in zip(paths, loaded, strict=True):
        merged_manifest.notes.extend(results.manifest.notes)
        for run in results.runs:
            key = (run.target, run.workload, run.concurrency)
            if key in seen:
                raise SystemExit(
                    f"refusing to merge {source.name}: it repeats {key}, which is already "
                    f"present. Two measurements of the same target and workload cannot both "
                    f"be right; pick the run you mean."
                )
            seen.add(key)
            merged.add(run)

    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path, help="raw result files to merge")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    missing = [path for path in args.runs if not path.exists()]
    if missing:
        raise SystemExit(f"no such file(s): {', '.join(str(p) for p in missing)}")

    merged = merge(args.runs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = merged.to_dict()
    import json

    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    targets = merged.targets()
    print(f"merged {len(args.runs)} run(s) covering {len(targets)} target(s): {', '.join(targets)}")
    print(f"-> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
