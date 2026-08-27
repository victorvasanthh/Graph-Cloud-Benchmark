"""End-to-end harness test against an in-memory engine.

No database is involved. `MemoryAdapter` answers the real workload statements
out of the toy graph, which is enough to exercise the parts of the harness
that would otherwise only ever be tested by a live run: parameter generation,
warmup exclusion, ingest verification, the cross-engine row-count check,
concurrency partitioning, mixed read/write scheduling, and the summary and
table layers on top of them.

The point is not to pretend the fake is a database. It is that the fairness
machinery - identical parameters, discarded warmups, verified loads, flagged
disagreements, an unbiased partition across concurrent clients - is logic, and
logic can be tested without a container.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from benchmark.core.config import BenchmarkConfig, RunConfig, TargetConfig, WorkloadConfig
from benchmark.core.errors import ConnectionFailure
from benchmark.databases import registry
from benchmark.databases.base import GraphAdapter, IngestPayload, IngestReport
from benchmark.reporting.consistency import check_row_agreement
from benchmark.reporting.summary import build_summary
from benchmark.reporting.tables import (
    render_concurrency_table,
    render_ingest_table,
    render_latency_table,
    render_status_table,
)
from benchmark.runners.runner import run_benchmark
from benchmark.workloads.base import CONTROL_PREFIX, OP_KEY
from benchmark.workloads.queries import BY_NAME

READ_WORKLOADS = [
    "point_lookup",
    "one_hop",
    "two_hop",
    "neighbourhood_3hop",
    "shortest_path",
    "top_cited",
    "date_filtered_top",
]
ALL_NAMES = [*READ_WORKLOADS, "mixed_read_write"]

# Reverse map from the exact Cypher text back to the (workload, operation) it
# belongs to, so the fake dispatches on what the runner actually sent rather
# than on a name the runner never passes.
# Variants are registered first and plain statements second, so the plain
# workload wins any collision. The mixed workload's read variant is
# deliberately the same Cypher as one_hop - that is the point of it - and
# without this ordering the fake would attribute one_hop's iterations to
# mixed_read_write. Both produce the same row count, so the collision is
# invisible until a test tries to tell the two apart.
CYPHER_TO_OP: dict[str, tuple[str, str]] = {}
for _name, _workload in BY_NAME.items():
    for _op, _mapping in _workload.variants.items():
        if "cypher" in _mapping:
            CYPHER_TO_OP[_mapping["cypher"]] = (_name, _op)
for _name, _workload in BY_NAME.items():
    if "cypher" in _workload.statements:
        CYPHER_TO_OP[_workload.statements["cypher"]] = (_name, "read")


class MemoryStore:
    """The server-side state, shared by every connection to one target.

    Modelled as a separate object on purpose. Several adapters of the same
    target are several connections to one database, so they must observe each
    other's writes; giving each adapter its own copy would let a concurrency
    test pass while the real thing raced.
    """

    def __init__(self) -> None:
        self.nodes: set[int] = set()
        self.edges: list[tuple[int, int]] = []
        self.dates: dict[int, str] = {}
        self.out: dict[int, list[int]] = {}
        self.access_count: dict[int, int] = {}
        self.lock = threading.Lock()
        #: Workload names this target should deliberately answer wrongly,
        #: used to prove the consistency checker actually fires.
        self.lie_about: set[str] = set()
        #: Edges to drop during ingest, to prove load verification fires.
        self.drop_edges = 0
        #: What schema_is_ready() should report for this target.
        self.index_state: bool | None = True
        #: Every parameter dict any connection was asked to execute.
        self.seen_params: list[dict[str, Any]] = []

    def clear(self) -> None:
        self.nodes.clear()
        self.edges.clear()
        self.dates.clear()
        self.out.clear()
        self.access_count.clear()


class MemoryAdapter(GraphAdapter):
    """Answers the real statements from a dict-of-lists graph."""

    dialects = ("cypher",)

    def __init__(self, target: TargetConfig, store: MemoryStore | None = None) -> None:
        super().__init__(target)
        self.store = store or MemoryStore()
        self.connected = False
        self.schema_ready = False

    # Convenience views, so tests can read the shared state off any adapter.
    @property
    def nodes(self) -> set[int]:
        return self.store.nodes

    @property
    def edges(self) -> list[tuple[int, int]]:
        return self.store.edges

    @property
    def access_count(self) -> dict[int, int]:
        return self.store.access_count

    @property
    def seen_params(self) -> list[dict[str, Any]]:
        return self.store.seen_params

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def server_version(self) -> str:
        return "MemoryAdapter 1.0"

    def reset(self) -> None:
        self.store.clear()

    def prepare_schema(self) -> None:
        self.schema_ready = True

    def schema_is_ready(self) -> bool | None:
        return self.store.index_state

    def ingest(self, payload: IngestPayload, batch_size: int) -> IngestReport:
        store = self.store
        store.clear()
        store.nodes.update(payload.nodes)
        store.dates.update(payload.dates)
        kept = list(payload.edges)[: len(payload.edges) - store.drop_edges]
        store.edges.extend(kept)
        for source, target in kept:
            store.out.setdefault(source, []).append(target)
        return IngestReport(
            nodes=len(store.nodes), edges=len(store.edges), duration_ns=1_000_000, batches=1
        )

    def count_nodes(self) -> int:
        return len(self.store.nodes)

    def count_edges(self) -> int:
        return len(self.store.edges)

    def run(self, statement: str, params: dict[str, Any]) -> int:
        workload, op = CYPHER_TO_OP[statement]
        store = self.store
        with store.lock:
            store.seen_params.append(dict(params))
            rows = self._answer(workload, op, params)
        return rows + 1 if workload in store.lie_about else rows

    def _answer(self, workload: str, op: str, params: dict[str, Any]) -> int:
        store = self.store
        if workload == "mixed_read_write":
            if op == "write":
                paper = params["id"]
                store.access_count[paper] = store.access_count.get(paper, 0) + 1
                return 1
            return len(store.out.get(params["id"], ()))
        if workload == "point_lookup":
            return 1 if params["id"] in store.nodes else 0
        if workload == "one_hop":
            return len(store.out.get(params["id"], ()))
        if workload == "two_hop":
            reached = {
                second
                for first in store.out.get(params["id"], ())
                for second in store.out.get(first, ())
            }
            return len(reached)
        if workload in {"neighbourhood_3hop", "shortest_path"}:
            return 1
        if workload == "top_cited":
            cited = {target for _, target in store.edges}
            return min(params["limit"], len(cited))
        if workload == "date_filtered_top":
            matching = [
                node
                for node, published in store.dates.items()
                if params["from"] <= published < params["to"]
            ]
            return min(params["limit"], len(matching))
        raise AssertionError(f"unhandled workload {workload}")


def make_config(names: list[str] | None = None, **workload_params: Any) -> BenchmarkConfig:
    chosen = names or ALL_NAMES
    return BenchmarkConfig(
        run=RunConfig(warmup_iterations=2, measured_iterations=12, seed=5, ingest_batch_size=4),
        targets=[
            TargetConfig(name="engine-a", kind="memory", display="Engine A", tier="test"),
            TargetConfig(name="engine-b", kind="memory", display="Engine B", tier="test"),
        ],
        workloads=[
            WorkloadConfig(name=name, params=dict(workload_params.get(name, {}))) for name in chosen
        ],
    )


@pytest.fixture
def memory_config() -> BenchmarkConfig:
    return make_config()


class AdapterFactory:
    """Builds MemoryAdapters, one shared store per target, recording each one."""

    def __init__(self, configure: Any = None, refuse: set[str] | None = None) -> None:
        self.instances: list[MemoryAdapter] = []
        self.by_target: dict[str, list[MemoryAdapter]] = {}
        self.stores: dict[str, MemoryStore] = {}
        self._configure = configure
        self._refuse = refuse or set()
        self._lock = threading.Lock()

    def __call__(self, target: TargetConfig) -> MemoryAdapter:
        with self._lock:
            store = self.stores.get(target.name)
            if store is None:
                store = MemoryStore()
                self.stores[target.name] = store
                if self._configure:
                    self._configure(target.name, store)
            adapter = MemoryAdapter(target, store)
            if target.name in self._refuse:

                def refuse() -> None:
                    raise ConnectionFailure(f"{target.name}: connection refused")

                adapter.connect = refuse  # type: ignore[method-assign]
            self.by_target.setdefault(target.name, []).append(adapter)
            self.instances.append(adapter)
            return adapter

    def connections_for(self, target: str) -> int:
        return len(self.by_target.get(target, []))

    def store_for(self, target: str) -> MemoryStore:
        return self.stores[target]


@pytest.fixture
def factory(monkeypatch) -> AdapterFactory:
    built = AdapterFactory()
    monkeypatch.setitem(registry.ADAPTERS, "memory", MemoryAdapter)
    monkeypatch.setattr("benchmark.runners.runner.build_adapter", built)
    return built


class TestHappyPath:
    def test_every_target_and_workload_is_measured(self, memory_config, toy_graph, factory):
        results = run_benchmark(memory_config, toy_graph)
        for target in ("engine-a", "engine-b"):
            for workload in ALL_NAMES:
                run = results.find(target, workload)
                assert run is not None, f"{target}/{workload} missing from results"
                assert run.status == "ok", run.note

    def test_warmup_iterations_are_recorded_but_excluded(self, memory_config, toy_graph, factory):
        results = run_benchmark(memory_config, toy_graph)
        run = results.find("engine-a", "point_lookup")
        assert len(run.iterations) == 14  # 2 warmup + 12 measured
        assert len(run.measured_ns()) == 12
        # The discarded ones are still on disk, with a negative index, so the
        # exclusion can be checked rather than taken on trust.
        assert sorted(it.index for it in run.iterations) == list(range(-2, 12))

    def test_both_targets_receive_identical_parameters(self, memory_config, toy_graph, factory):
        results = run_benchmark(memory_config, toy_graph)
        for workload in ALL_NAMES:
            a = [it.rows for it in results.find("engine-a", workload).iterations]
            b = [it.rows for it in results.find("engine-b", workload).iterations]
            assert a == b, f"{workload} answered differently for identical parameters"

    def test_ingest_records_verified_counts_and_throughput(self, memory_config, toy_graph, factory):
        results = run_benchmark(memory_config, toy_graph)
        run = results.find("engine-a", "ingest")
        assert run.status == "ok"
        assert run.scale["nodes_loaded"] == toy_graph.node_count
        assert run.scale["edges_loaded"] == toy_graph.edge_count
        assert run.scale["edges_per_second"] > 0

    def test_summary_reports_agreement(self, memory_config, toy_graph, factory):
        results = run_benchmark(memory_config, toy_graph)
        summary = build_summary(results)
        assert summary["consistency_issues"] == []
        assert "matching row counts" in summary["consistency_verdict"]
        assert summary["baseline"] in {"engine-a", "engine-b"}

    def test_ingest_table_renders_throughput(self, memory_config, toy_graph, factory):
        summary = build_summary(run_benchmark(memory_config, toy_graph))
        table = render_ingest_table(summary)
        assert "edges/s" in table
        assert "verified" in table


class TestMixedReadWrite:
    def test_write_operations_actually_reach_the_engine(self, toy_graph, factory):
        config = make_config(["mixed_read_write"])
        run_benchmark(config, toy_graph)
        engine = factory.store_for("engine-a")
        # 10% of 14 iterations is small, so assert on the mechanism rather than
        # an exact count: at least one counter was incremented, and the reads
        # left the graph structure untouched.
        assert sum(engine.access_count.values()) >= 1
        assert len(engine.edges) == toy_graph.edge_count

    def test_operation_sequence_is_identical_across_targets(self, toy_graph, factory):
        run_benchmark(make_config(["mixed_read_write"]), toy_graph)
        a = list(factory.store_for("engine-a").seen_params)
        b = list(factory.store_for("engine-b").seen_params)
        assert a == b

    def test_control_keys_are_stripped_before_execution(self, toy_graph, factory):
        run_benchmark(make_config(["mixed_read_write"]), toy_graph)
        for store in factory.stores.values():
            for params in store.seen_params:
                leaked = [k for k in params if k.startswith(CONTROL_PREFIX)]
                # ArangoDB rejects a query outright when a bind parameter is
                # declared but unused, so a leaked control key would fail one
                # engine and not the others.
                assert not leaked, f"control key leaked to the driver: {leaked}"

    def test_mutating_workload_is_scheduled_last(self, memory_config, toy_graph, factory):
        results = run_benchmark(memory_config, toy_graph)
        order = [run.workload for run in results.runs if run.target == "engine-a"]
        assert order[-1] == "mixed_read_write"
        assert order.index("mixed_read_write") == len(order) - 1

    def test_write_variant_has_text_for_every_read_dialect(self):
        mixed = BY_NAME["mixed_read_write"]
        for op, mapping in mixed.variants.items():
            assert "cypher" in mapping, f"{op} has no Cypher text"
            assert "aql" in mapping, f"{op} has no AQL text"

    def test_op_key_is_a_control_key(self):
        assert OP_KEY.startswith(CONTROL_PREFIX)


class TestConcurrency:
    def test_each_level_is_measured_separately(self, toy_graph, factory):
        config = make_config(["point_lookup"], point_lookup={"concurrency": [1, 4]})
        results = run_benchmark(config, toy_graph)
        assert results.concurrency_levels("point_lookup") == [1, 4]
        for level in (1, 4):
            run = results.find("engine-a", "point_lookup", concurrency=level)
            assert run is not None and run.status == "ok"
            assert len(run.measured_ns()) == 12

    def test_one_connection_is_opened_per_worker(self, toy_graph, factory):
        config = make_config(["point_lookup"], point_lookup={"concurrency": [4]})
        run_benchmark(config, toy_graph)
        # One adapter for the target itself plus one per concurrent worker.
        # Sharing a session across threads would measure driver locking.
        assert factory.connections_for("engine-a") == 5

    def test_concurrent_partition_covers_the_same_parameters(self, toy_graph, factory):
        sequential = make_config(["point_lookup"], point_lookup={"concurrency": [1]})
        results_seq = run_benchmark(sequential, toy_graph)
        rows_seq = sorted(
            it.rows for it in results_seq.find("engine-a", "point_lookup", 1).iterations
        )

        concurrent = make_config(["point_lookup"], point_lookup={"concurrency": [4]})
        results_con = run_benchmark(concurrent, toy_graph)
        rows_con = sorted(
            it.rows for it in results_con.find("engine-a", "point_lookup", 4).iterations
        )
        # The union of what the workers asked must be the same multiset the
        # sequential run used, or the levels are not comparable.
        assert rows_seq == rows_con

    def test_throughput_uses_wall_clock_not_summed_latency(self, toy_graph, factory):
        config = make_config(["point_lookup"], point_lookup={"concurrency": [4]})
        results = run_benchmark(config, toy_graph)
        run = results.find("engine-a", "point_lookup", 4)
        summary = build_summary(results)
        record = summary["workloads"]["point_lookup"]["by_concurrency"]["4"]["targets"]["engine-a"]
        expected = len(run.measured_ns()) / (run.wall_ns / 1e9)
        assert record["throughput_qps"] == pytest.approx(expected, rel=1e-6)

    def test_concurrency_table_renders_only_when_there_are_levels(self, toy_graph, factory):
        single = build_summary(
            run_benchmark(
                make_config(["point_lookup"], point_lookup={"concurrency": [1]}), toy_graph
            )
        )
        assert render_concurrency_table(single, "point_lookup") is None

        multi = build_summary(
            run_benchmark(
                make_config(["point_lookup"], point_lookup={"concurrency": [1, 4]}), toy_graph
            )
        )
        table = render_concurrency_table(multi, "point_lookup")
        assert table is not None
        assert "c=1" in table and "c=4" in table

    def test_row_agreement_is_checked_within_a_level(self, toy_graph, factory):
        config = make_config(["point_lookup"], point_lookup={"concurrency": [1, 4]})
        results = run_benchmark(config, toy_graph)
        assert check_row_agreement(results) == []


class TestIntegrityChecks:
    def test_a_missing_index_is_reported_not_measured_silently(self, toy_graph, monkeypatch):
        def configure(name: str, store: MemoryStore) -> None:
            if name == "engine-b":
                store.index_state = False

        built = AdapterFactory(configure)
        monkeypatch.setitem(registry.ADAPTERS, "memory", MemoryAdapter)
        monkeypatch.setattr("benchmark.runners.runner.build_adapter", built)

        results = run_benchmark(make_config(["point_lookup"]), toy_graph)
        # An engine measured without the index every other engine got is not
        # slow, it is answering a different question - and the difference would
        # look exactly like a performance finding.
        assert any("index could not be confirmed" in note for note in results.manifest.notes), (
            results.manifest.notes
        )
        assert results.find("engine-b", "ingest").scale["index_verified"] is False

    def test_unverifiable_index_is_recorded_as_assumed(self, toy_graph, monkeypatch):
        def configure(name: str, store: MemoryStore) -> None:
            store.index_state = None

        built = AdapterFactory(configure)
        monkeypatch.setitem(registry.ADAPTERS, "memory", MemoryAdapter)
        monkeypatch.setattr("benchmark.runners.runner.build_adapter", built)

        results = run_benchmark(make_config(["point_lookup"]), toy_graph)
        # "Could not check" and "checked and it is missing" are different
        # claims and the manifest must not conflate them.
        assert any("assumed rather than confirmed" in n for n in results.manifest.notes)

    def test_a_partial_load_fails_the_run_rather_than_scoring_well(self, toy_graph, monkeypatch):
        def configure(name: str, store: MemoryStore) -> None:
            if name == "engine-b":
                store.drop_edges = 3

        built = AdapterFactory(configure)
        monkeypatch.setitem(registry.ADAPTERS, "memory", MemoryAdapter)
        monkeypatch.setattr("benchmark.runners.runner.build_adapter", built)

        results = run_benchmark(make_config(["point_lookup"]), toy_graph)
        ingest = results.find("engine-b", "ingest")
        assert ingest.status == "failed"
        assert "load verification failed" in ingest.note
        assert any("load verification failed" in note for note in results.manifest.notes)

    def test_row_count_disagreement_is_detected(self, toy_graph, monkeypatch):
        def configure(name: str, store: MemoryStore) -> None:
            if name == "engine-b":
                store.lie_about = {"one_hop"}

        built = AdapterFactory(configure)
        monkeypatch.setitem(registry.ADAPTERS, "memory", MemoryAdapter)
        monkeypatch.setattr("benchmark.runners.runner.build_adapter", built)

        results = run_benchmark(make_config(["one_hop"], one_hop={"concurrency": [1]}), toy_graph)
        issues = check_row_agreement(results)
        assert issues, "a target returning different row counts went undetected"
        assert {issue.workload for issue in issues} == {"one_hop"}

        summary = build_summary(results)
        assert summary["workloads"]["one_hop"]["row_counts_agree"] is False
        # A workload whose engines disagreed must not carry a speedup ratio.
        table = render_latency_table(summary, "one_hop")
        assert "relative column is suppressed" in table

    def test_a_single_target_run_is_reported_as_unverifiable(self, toy_graph, factory):
        config = make_config(["point_lookup"])
        config.targets = config.targets[:1]
        results = run_benchmark(config, toy_graph)
        summary = build_summary(results)
        assert "no cross-engine check" in summary["consistency_verdict"]


class TestUnavailableTargets:
    def test_a_target_that_cannot_connect_still_appears_in_the_report(self, toy_graph, monkeypatch):
        built = AdapterFactory(refuse={"engine-b"})
        monkeypatch.setitem(registry.ADAPTERS, "memory", MemoryAdapter)
        monkeypatch.setattr("benchmark.runners.runner.build_adapter", built)

        results = run_benchmark(make_config(), toy_graph)
        summary = build_summary(results)

        # Absent from the table would read as "did not compete"; the truth is
        # "could not be reached", and the grid has to say so.
        for workload in ALL_NAMES:
            record = summary["workloads"][workload]["targets"]["engine-b"]
            assert record["status"] == "unavailable"
        assert "not reachable" in render_status_table(summary)

    def test_unavailable_target_gets_a_row_at_every_concurrency_level(self, toy_graph, monkeypatch):
        built = AdapterFactory(refuse={"engine-b"})
        monkeypatch.setitem(registry.ADAPTERS, "memory", MemoryAdapter)
        monkeypatch.setattr("benchmark.runners.runner.build_adapter", built)

        config = make_config(["point_lookup"], point_lookup={"concurrency": [1, 10]})
        results = run_benchmark(config, toy_graph)
        for level in (1, 10):
            run = results.find("engine-b", "point_lookup", concurrency=level)
            assert run is not None and run.status == "unavailable"

    def test_skipped_targets_are_named_in_the_manifest(self, toy_graph, factory):
        config = make_config(["point_lookup"])
        config.targets.append(
            TargetConfig(
                name="engine-c",
                kind="memory",
                display="Engine C",
                tier="test",
                missing=["ENGINE_C_PASSWORD"],
            )
        )
        results = run_benchmark(config, toy_graph)
        assert any("engine-c skipped" in note for note in results.manifest.notes)
