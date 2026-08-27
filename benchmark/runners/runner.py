"""Orchestration: connect, load, warm up, measure, record.

The ordering here encodes most of the methodology, so it is worth reading in
sequence rather than as a set of helpers.

For each target, in isolation:

  reset -> prepare_schema -> ingest -> verify -> warm up -> measure -> close

`verify` is not optional and not cosmetic. It asks the server how many nodes
and relationships it actually holds and compares that with what we sent. A
partial load produces beautiful latency numbers, and without this check the
run would report them as a win.

Warmup iterations are executed and discarded. Their timings are written to the
raw file with a negative index so that the discarding is auditable rather than
merely asserted - anyone can recompute the summary including them and see what
difference the cold cache made.

Targets are measured one at a time, never concurrently. Two engines under
measurement on one host would contend for the same CPU and the same network
link, and the resulting numbers would describe the host rather than either
database.

Workloads that mutate the database are scheduled last, after every read-only
workload has been measured, so that a write cannot change what an earlier
measurement saw.

**Concurrency.** A workload may be measured at several client-concurrency
levels. Each level gets its own connections - one adapter instance per worker,
connected before the clock starts - because sharing one session across threads
would measure driver locking rather than the server. The parameter list is
partitioned round-robin across workers, so the union of what the workers ask
is exactly the same multiset every engine sees at every level.
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TextIO

from ..core.config import BenchmarkConfig, TargetConfig, WorkloadConfig
from ..core.errors import ConnectionFailure, WorkloadFailure
from ..core.results import BenchmarkResults, Iteration, RunManifest, WorkloadRun, new_run_id
from ..core.timing import ns_to_ms, timed
from ..databases import GraphAdapter, IngestPayload, build_adapter
from ..datasets.cit_hepth import CitationGraph
from ..workloads import Workload
from ..workloads.base import execution_params
from ..workloads.queries import BY_NAME

INGEST_WORKLOAD = "ingest"


@dataclass
class Progress:
    """Human-readable run progress on stderr, so stdout stays pipeable."""

    stream: TextIO = sys.stderr
    quiet: bool = False

    def say(self, message: str) -> None:
        if not self.quiet:
            print(message, file=self.stream, flush=True)


def run_benchmark(
    config: BenchmarkConfig,
    graph: CitationGraph,
    progress: Progress | None = None,
    skip_ingest: bool = False,
) -> BenchmarkResults:
    """Measure every active target against every active workload."""
    report = progress or Progress()
    manifest = RunManifest.new(new_run_id())
    manifest.dataset = "cit-HepTh"
    manifest.dataset_nodes = graph.node_count
    manifest.dataset_edges = graph.edge_count
    manifest.warmup_iterations = config.run.warmup_iterations
    manifest.measured_iterations = config.run.measured_iterations
    manifest.seed = config.run.seed
    results = BenchmarkResults(manifest=manifest)

    for skipped in config.skipped_targets():
        reason = (
            f"not configured: {', '.join(skipped.missing)} unset"
            if skipped.missing
            else "disabled in configuration"
        )
        manifest.notes.append(f"{skipped.name} skipped ({reason})")
        report.say(f"skip  {skipped.name}: {reason}")

    workloads = _resolve_workloads(config.active_workloads(), manifest)

    # Parameters are built once here, not per target, so that every engine is
    # asked the identical questions in the identical order. Every concurrency
    # level draws from this same list.
    parameters = {
        workload.name: workload.parameters_for(
            graph,
            workload_config.params,
            config.run.warmup_iterations + config.run.measured_iterations,
            config.run.seed,
        )
        for workload, workload_config in workloads
    }

    payload = IngestPayload(nodes=graph.nodes, edges=graph.edges, dates=graph.dates)

    for target in config.active_targets():
        _measure_target(
            target=target,
            config=config,
            workloads=workloads,
            parameters=parameters,
            payload=payload,
            results=results,
            report=report,
            skip_ingest=skip_ingest,
        )

    manifest.close()
    return results


def _levels_for(workload_config: WorkloadConfig) -> list[int]:
    """Client-concurrency levels this workload should be measured at."""
    raw = workload_config.params.get("concurrency", [1])
    levels = [int(level) for level in (raw if isinstance(raw, list) else [raw])]
    invalid = [level for level in levels if level < 1]
    if invalid:
        raise ValueError(
            f"workload {workload_config.name!r} requests concurrency {invalid}; "
            f"levels must be 1 or greater"
        )
    return sorted(set(levels))


def _record_unavailable(
    results: BenchmarkResults,
    target: TargetConfig,
    workloads: list[tuple[Workload, WorkloadConfig]],
    reason: str,
) -> None:
    """Write an explicit row per workload for a target that never connected.

    Emitting rows rather than omitting the target keeps the failure visible in
    the report. A target that is simply absent from a comparison table reads
    as one that was never in the running, which is a different and more
    flattering claim than the truth.
    """
    results.manifest.notes.append(f"{target.name} unavailable: {reason}")
    results.add(
        WorkloadRun(target=target.name, workload=INGEST_WORKLOAD, status="unavailable", note=reason)
    )
    for workload, workload_config in workloads:
        for level in _levels_for(workload_config):
            results.add(
                WorkloadRun(
                    target=target.name,
                    workload=workload.name,
                    concurrency=level,
                    status="unavailable",
                    note=reason,
                )
            )


def _resolve_workloads(
    configured: list[WorkloadConfig], manifest: RunManifest
) -> list[tuple[Workload, WorkloadConfig]]:
    resolved: list[tuple[Workload, WorkloadConfig]] = []
    for entry in configured:
        workload = BY_NAME.get(entry.name)
        if workload is None:
            manifest.notes.append(
                f"workloads.yaml names {entry.name!r}, which is not implemented; ignored"
            )
            continue
        resolved.append((workload, entry))
    # Mutating workloads run last so a write cannot disturb a read measurement.
    # Python's sort is stable, so the configured order survives within groups.
    resolved.sort(key=lambda pair: pair[0].mutates)
    return resolved


def _measure_target(
    target: TargetConfig,
    config: BenchmarkConfig,
    workloads: list[tuple[Workload, WorkloadConfig]],
    parameters: dict[str, list[dict[str, Any]]],
    payload: IngestPayload,
    results: BenchmarkResults,
    report: Progress,
    skip_ingest: bool,
) -> None:
    adapter = build_adapter(target)
    report.say(f"\n=== {target.display} ({target.tier}) ===")

    try:
        adapter.connect()
    except ConnectionFailure as exc:
        report.say(f"  unavailable: {exc}")
        _record_unavailable(results, target, workloads, str(exc))
        return

    try:
        version = adapter.server_version()
        results.manifest.notes.append(f"{target.name} server version: {version}")
        report.say(f"  server: {version}")

        if not skip_ingest:
            _load(adapter, payload, config, results, target, report)
        else:
            report.say("  ingest skipped (--skip-ingest); assuming the data is already loaded")
            _verify_counts(adapter, payload, results, target, report)

        for workload, workload_config in workloads:
            for level in _levels_for(workload_config):
                _measure_workload(
                    target=target,
                    adapter=adapter,
                    workload=workload,
                    params=parameters[workload.name],
                    config=config,
                    concurrency=level,
                    results=results,
                    report=report,
                )
    finally:
        adapter.close()


def _load(
    adapter: GraphAdapter,
    payload: IngestPayload,
    config: BenchmarkConfig,
    results: BenchmarkResults,
    target: TargetConfig,
    report: Progress,
) -> None:
    report.say(f"  loading {payload.node_count:,} nodes / {payload.edge_count:,} edges")
    adapter.reset()
    adapter.prepare_schema()

    # prepare_schema succeeding is not the same as the index existing. Some
    # flavours have to tolerate DDL errors to stay portable, so the index is
    # confirmed separately before anything is timed.
    index_verified = adapter.schema_is_ready()
    if index_verified is False:
        note = (
            f"{target.name}: the Paper(id) index could not be confirmed after schema "
            f"setup. Every other engine is measured with that index, so these read "
            f"numbers are not comparable and must not be published as a like-for-like."
        )
        results.manifest.notes.append(note)
        report.say(f"  WARNING {note}")
    elif index_verified is None:
        results.manifest.notes.append(
            f"{target.name}: index presence could not be verified on this engine; "
            f"schema parity is assumed rather than confirmed"
        )

    run = WorkloadRun(target=target.name, workload=INGEST_WORKLOAD)
    try:
        ingest_report = adapter.ingest(payload, config.run.ingest_batch_size)
    except (WorkloadFailure, ConnectionFailure) as exc:
        run.status = "failed"
        run.note = str(exc)
        results.add(run)
        report.say(f"  ingest FAILED: {exc}")
        return

    run.iterations.append(
        Iteration(index=0, duration_ns=ingest_report.duration_ns, rows=ingest_report.edges)
    )
    run.wall_ns = ingest_report.duration_ns
    seconds = ingest_report.duration_ns / 1e9
    run.scale = {
        "nodes_loaded": ingest_report.nodes,
        "edges_loaded": ingest_report.edges,
        "nodes_expected": payload.node_count,
        "edges_expected": payload.edge_count,
        "batches": ingest_report.batches,
        # Load throughput as a rate, so it can be compared directly rather than
        # mentally divided out of a wall time that depends on the dataset size.
        "edges_per_second": (ingest_report.edges / seconds) if seconds > 0 else 0.0,
        "nodes_per_second": (ingest_report.nodes / seconds) if seconds > 0 else 0.0,
        "index_verified": index_verified,
    }

    if not ingest_report.matches(payload.node_count, payload.edge_count):
        # Recorded as a failure of the load, not as a slow-but-valid result.
        # Every read workload that follows would be measuring a smaller graph.
        run.status = "failed"
        run.note = (
            f"load verification failed: server holds {ingest_report.nodes:,} nodes and "
            f"{ingest_report.edges:,} edges, expected {payload.node_count:,} and "
            f"{payload.edge_count:,}. Read results for this target are not comparable."
        )
        results.manifest.notes.append(f"{target.name}: {run.note}")
        report.say(f"  WARNING {run.note}")
    else:
        report.say(
            f"  loaded in {seconds:.1f}s "
            f"({run.scale['edges_per_second']:,.0f} edges/s), counts verified"
        )

    results.add(run)


def _verify_counts(
    adapter: GraphAdapter,
    payload: IngestPayload,
    results: BenchmarkResults,
    target: TargetConfig,
    report: Progress,
) -> None:
    nodes, edges = adapter.count_nodes(), adapter.count_edges()
    if nodes != payload.node_count or edges != payload.edge_count:
        note = (
            f"{target.name}: pre-loaded data does not match the dataset "
            f"({nodes:,} nodes / {edges:,} edges, expected {payload.node_count:,} / "
            f"{payload.edge_count:,})"
        )
        results.manifest.notes.append(note)
        report.say(f"  WARNING {note}")


def _statement_for(adapter: GraphAdapter, workload: Workload, params: dict[str, Any]) -> str | None:
    return adapter.statement_for(workload.dialect_map(params))


def _execute(
    adapter: GraphAdapter, workload: Workload, params: dict[str, Any], index: int
) -> Iteration:
    """One timed request. Never raises; failures come back as a failed Iteration."""
    statement = _statement_for(adapter, workload, params)
    if statement is None:
        return Iteration(
            index=index,
            duration_ns=0,
            rows=0,
            ok=False,
            error="no statement for this dialect and operation",
        )
    elapsed: list[int] = []
    try:
        with timed() as elapsed:
            rows = adapter.run(statement, execution_params(params))
        return Iteration(index=index, duration_ns=elapsed[0], rows=rows)
    except (WorkloadFailure, ConnectionFailure) as exc:
        return Iteration(
            index=index,
            duration_ns=elapsed[0] if elapsed else 0,
            rows=0,
            ok=False,
            error=str(exc),
        )


def _measure_workload(
    target: TargetConfig,
    adapter: GraphAdapter,
    workload: Workload,
    params: list[dict[str, Any]],
    config: BenchmarkConfig,
    concurrency: int,
    results: BenchmarkResults,
    report: Progress,
) -> None:
    label = workload.name if concurrency == 1 else f"{workload.name} @c{concurrency}"
    run = WorkloadRun(target=adapter.name, workload=workload.name, concurrency=concurrency)
    run.scale = {"equivalence": workload.equivalence, "mutates": workload.mutates}

    if not params:
        run.status = "unsupported"
        run.note = "no parameters could be generated from the dataset"
        results.add(run)
        report.say(f"  {label}: n/a ({run.note})")
        return

    if not workload.supported_by(adapter.dialects):
        run.status = "unsupported"
        run.note = f"no {'/'.join(adapter.dialects)} statement is defined for this workload"
        results.add(run)
        report.say(f"  {label}: n/a ({run.note})")
        return

    if concurrency == 1:
        _run_sequential(adapter, workload, params, config, run)
    else:
        failure = _run_concurrent(target, workload, params, config, concurrency, run)
        if failure is not None:
            run.status = "failed"
            run.note = failure
            results.add(run)
            report.say(f"  {label}: FAILED {failure}")
            return

    measured = run.measured_ns()
    failures = run.failure_count()
    if not measured:
        run.status = "failed"
        last_error = next(
            (it.error for it in reversed(run.iterations) if it.error), "no iteration completed"
        )
        run.note = f"every measured iteration failed; last error: {last_error}"
        report.say(f"  {label}: FAILED {run.note}")
    else:
        ordered = sorted(measured)
        median = ns_to_ms(ordered[len(ordered) // 2])
        throughput = len(measured) / (run.wall_ns / 1e9) if run.wall_ns > 0 else 0.0
        suffix = f", {failures} failed" if failures else ""
        report.say(
            f"  {label}: p50 {median:.2f} ms over {len(measured)} runs, "
            f"{throughput:,.0f} req/s{suffix}"
        )

    results.add(run)


def _run_sequential(
    adapter: GraphAdapter,
    workload: Workload,
    params: list[dict[str, Any]],
    config: BenchmarkConfig,
    run: WorkloadRun,
) -> None:
    warmup = config.run.warmup_iterations
    total = warmup + config.run.measured_iterations
    measured_started: int | None = None

    for position in range(total):
        # Warmup iterations carry a negative index; the summary layer filters
        # on index >= 0, and the raw file keeps both.
        index = position - warmup
        if index == 0:
            measured_started = time.perf_counter_ns()
        iteration = _execute(adapter, workload, params[position % len(params)], index)
        run.iterations.append(iteration)
        if not iteration.ok and config.run.stop_on_error:
            break

    if measured_started is not None:
        run.wall_ns = time.perf_counter_ns() - measured_started


def _run_concurrent(
    target: TargetConfig,
    workload: Workload,
    params: list[dict[str, Any]],
    config: BenchmarkConfig,
    concurrency: int,
    run: WorkloadRun,
) -> str | None:
    """Measure with `concurrency` independent clients. Returns an error or None.

    Each worker owns a separate adapter and therefore a separate connection.
    Connections are opened before the measured clock starts, so connection
    setup - which on a managed free tier can dominate everything else - is
    never counted as query latency.
    """
    warmup = config.run.warmup_iterations
    measured = config.run.measured_iterations
    adapters: list[GraphAdapter] = []

    try:
        for _ in range(concurrency):
            worker_adapter = build_adapter(target)
            worker_adapter.connect()
            adapters.append(worker_adapter)
    except ConnectionFailure as exc:
        for opened in adapters:
            opened.close()
        # A tier that will not grant this many connections is a real finding
        # about the tier, reported as such rather than as a slow result.
        return f"could not open {concurrency} connections: {exc}"

    def phase(count: int, first_index: int, param_offset: int) -> list[Iteration]:
        # Round-robin partition: worker w takes positions w, w+N, w+2N, ... so
        # the union across workers is the same parameter multiset, in the same
        # relative order, that the sequential run uses.
        #
        # `param_offset` matters and is easy to get wrong. The sequential path
        # walks one continuous position counter across warmup and measurement,
        # so its measured phase starts at parameter `warmup`, not at 0. A
        # concurrent phase that restarted the parameter index would hand the
        # two levels different questions and quietly make them incomparable.
        shares = _split_evenly(count, concurrency)

        def work(worker: int) -> list[Iteration]:
            adapter = adapters[worker]
            local: list[Iteration] = []
            for i in range(shares[worker]):
                position = worker + i * concurrency
                iteration_params = params[(param_offset + position) % len(params)]
                local.append(_execute(adapter, workload, iteration_params, first_index + position))
            return local

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            batches = list(pool.map(work, range(concurrency)))
        return [iteration for batch in batches for iteration in batch]

    try:
        if warmup:
            # Warmup results carry negative indices, exactly as in the
            # sequential path, so both are auditable the same way.
            run.iterations.extend(phase(warmup, -warmup, 0))

        started = time.perf_counter_ns()
        completed = phase(measured, 0, warmup)
        run.wall_ns = time.perf_counter_ns() - started
        run.iterations.extend(completed)
    finally:
        for opened in adapters:
            opened.close()

    return None


def _split_evenly(total: int, buckets: int) -> list[int]:
    """Distribute `total` items over `buckets`, largest buckets first."""
    base, remainder = divmod(total, buckets)
    return [base + (1 if i < remainder else 0) for i in range(buckets)]
