"""The on-disk result schema.

Two files come out of a run, and the split is deliberate:

  results/raw/<run_id>.json      every individual iteration, unaggregated
  results/summary/<run_id>.json  the percentiles the report is built from

The raw file exists so that a reader who distrusts our statistics can compute
their own. A benchmark that publishes only summary numbers asks to be taken on
faith; one that ships the sample does not.
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass
class Iteration:
    """One execution of one workload against one target."""

    index: int
    duration_ns: int
    rows: int
    ok: bool = True
    error: str | None = None

    @property
    def is_measured(self) -> bool:
        """Warmup iterations carry index < 0 and never reach the summary."""
        return self.ok and self.index >= 0


@dataclass
class WorkloadRun:
    """Every iteration of a single (target, workload, concurrency) triple."""

    target: str
    workload: str
    concurrency: int = 1
    iterations: list[Iteration] = field(default_factory=list)
    status: str = "ok"  # ok | unsupported | unavailable | failed
    note: str | None = None
    scale: dict[str, Any] = field(default_factory=dict)
    #: Wall-clock span of the measured phase. Under concurrency this is what
    #: throughput must be computed from: summing per-request latencies across
    #: parallel clients would count the same seconds several times over and
    #: report a throughput the server never delivered.
    wall_ns: int = 0

    def measured_ns(self) -> list[int]:
        return [it.duration_ns for it in self.iterations if it.is_measured]

    def failure_count(self) -> int:
        return sum(1 for it in self.iterations if it.index >= 0 and not it.ok)


@dataclass
class RunManifest:
    """Everything needed to judge whether two runs are comparable.

    Deliberately includes the host description. A benchmark whose numbers are
    quoted without saying what produced them is a marketing artifact, and the
    manifest is what makes the difference visible in the committed output.
    """

    run_id: str
    started_at: str
    schema_version: int = SCHEMA_VERSION
    finished_at: str | None = None
    dataset: str = ""
    dataset_nodes: int = 0
    dataset_edges: int = 0
    warmup_iterations: int = 0
    measured_iterations: int = 0
    seed: int = 0
    python_version: str = field(default_factory=lambda: sys.version.split()[0])
    platform: str = field(default_factory=platform.platform)
    processor: str = field(default_factory=lambda: platform.machine())
    notes: list[str] = field(default_factory=list)

    @staticmethod
    def new(run_id: str) -> RunManifest:
        return RunManifest(run_id=run_id, started_at=_utc_now())

    def close(self) -> None:
        self.finished_at = _utc_now()


@dataclass
class BenchmarkResults:
    """The complete output of one `run_benchmark` invocation."""

    manifest: RunManifest
    runs: list[WorkloadRun] = field(default_factory=list)

    def add(self, run: WorkloadRun) -> None:
        self.runs.append(run)

    def targets(self) -> list[str]:
        seen: dict[str, None] = {}
        for run in self.runs:
            seen.setdefault(run.target, None)
        return list(seen)

    def workloads(self) -> list[str]:
        seen: dict[str, None] = {}
        for run in self.runs:
            seen.setdefault(run.workload, None)
        return list(seen)

    def find(self, target: str, workload: str, concurrency: int = 1) -> WorkloadRun | None:
        for run in self.runs:
            if run.target == target and run.workload == workload and run.concurrency == concurrency:
                return run
        return None

    def concurrency_levels(self, workload: str) -> list[int]:
        levels = {run.concurrency for run in self.runs if run.workload == workload}
        return sorted(levels)

    def to_dict(self) -> dict[str, Any]:
        return {"manifest": asdict(self.manifest), "runs": [asdict(r) for r in self.runs]}

    def write_raw(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.manifest.run_id}.json"
        # Written via a .tmp sibling then moved, so an interrupted run never
        # leaves a half-written file that looks like a complete result.
        # `.tmp` is gitignored precisely for this.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return path

    @staticmethod
    def read_raw(path: Path) -> BenchmarkResults:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        version = payload.get("manifest", {}).get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"{path} was written by schema version {version!r}, "
                f"this build reads version {SCHEMA_VERSION}"
            )
        manifest = RunManifest(**payload["manifest"])
        runs = [
            WorkloadRun(
                target=r["target"],
                workload=r["workload"],
                concurrency=r.get("concurrency", 1),
                iterations=[Iteration(**i) for i in r["iterations"]],
                status=r["status"],
                note=r.get("note"),
                scale=r.get("scale", {}),
                wall_ns=r.get("wall_ns", 0),
            )
            for r in payload["runs"]
        ]
        return BenchmarkResults(manifest=manifest, runs=runs)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_run_id() -> str:
    """Timestamp-based and sortable, so `ls results/raw` is chronological."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
