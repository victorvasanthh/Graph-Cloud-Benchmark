"""Confirming an index when the catalogue will not say.

CognoDB reported `CREATE CONSTRAINT ... REQUIRE p.id IS UNIQUE` as successful
and then answered `SHOW INDEXES` with zero rows. Both can be true at once: a
unique constraint may be listed under constraints rather than indexes, or its
backing index may not surface in the catalogue at all.

The catalogue is therefore no longer the only witness. The planner is asked
directly whether a point lookup will use an index, which tests the property the
benchmark depends on rather than the bookkeeping around it - and answers even
on an engine whose introspection does not exist.

Note what these tests do NOT do: they never let an unverified index pass. An
engine measured without the index every other engine has is answering an easier
question, and a fast number from it would be the most misleading kind of
result.
"""

from __future__ import annotations

import pytest

from benchmark.core.config import TargetConfig
from benchmark.databases.bolt import FLAVOURS, BoltAdapter, _is_connection_loss, _plan_operators


def adapter(flavour: str = "cognodb") -> BoltAdapter:
    return BoltAdapter(
        TargetConfig(
            name=f"t-{flavour}",
            kind="bolt",
            display=flavour,
            tier="test",
            settings={"uri": "bolt://localhost:7687", "flavour": flavour},
        )
    )


class PlanSession:
    """A session whose catalogue is empty but whose planner answers."""

    def __init__(self, plan: dict | None, log: list[str], catalogue_rows: list | None = None):
        self._plan = plan
        self._log = log
        self._rows = catalogue_rows or []

    def run(self, statement, *args, **kwargs):
        self._log.append(statement)
        self._statement = statement
        return self

    def consume(self):
        return type("Summary", (), {"plan": self._plan})()

    def single(self):
        return None

    def __iter__(self):
        return iter(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


INDEXED_PLAN = {
    "operatorType": "ProduceResults",
    "children": [{"operatorType": "NodeUniqueIndexSeek", "children": []}],
}
SCAN_PLAN = {
    "operatorType": "ProduceResults",
    "children": [
        {
            "operatorType": "Filter",
            "children": [{"operatorType": "NodeByLabelScan", "children": []}],
        }
    ],
}


class TestPlanOperators:
    def test_flattens_a_nested_plan(self):
        assert "NodeUniqueIndexSeek" in _plan_operators(INDEXED_PLAN)

    def test_finds_a_label_scan(self):
        operators = _plan_operators(SCAN_PLAN)
        assert "NodeByLabelScan" in operators
        assert not any("IndexSeek" in op for op in operators)

    def test_tolerates_an_empty_or_odd_plan(self):
        assert _plan_operators({}) == []
        assert _plan_operators({"children": [None, "nonsense"]}) == []


class TestPlanBasedVerification:
    def test_index_seek_in_the_plan_confirms_the_index(self, monkeypatch):
        target = adapter()
        log: list[str] = []
        monkeypatch.setattr(target, "_session", lambda: PlanSession(INDEXED_PLAN, log))
        # The catalogue returns nothing, exactly as CognoDB did. The plan is
        # what settles it.
        assert target.schema_is_ready() is True
        assert any("EXPLAIN" in statement for statement in log)

    def test_a_label_scan_means_the_index_is_absent(self, monkeypatch):
        target = adapter()
        log: list[str] = []
        monkeypatch.setattr(target, "_session", lambda: PlanSession(SCAN_PLAN, log))
        # This is the case that must never be published: the engine will scan.
        assert target.schema_is_ready() is False

    def test_no_plan_available_yields_unknown(self, monkeypatch):
        target = adapter()
        log: list[str] = []
        monkeypatch.setattr(target, "_session", lambda: PlanSession(None, log))
        # Not "no index" - "we could not tell", which still blocks publication.
        assert target.schema_is_ready() is None

    def test_the_outcome_is_recorded_with_the_operators(self, monkeypatch):
        target = adapter()
        monkeypatch.setattr(target, "_session", lambda: PlanSession(SCAN_PLAN, []))
        target.schema_is_ready()
        plan_attempts = [o for s, o in target.probe_attempts if "EXPLAIN" in s]
        assert plan_attempts
        # The operator list has to travel with the verdict, or a wrong verdict
        # is unarguable.
        assert "NodeByLabelScan" in plan_attempts[0]

    def test_catalogue_wins_when_it_answers(self, monkeypatch):
        target = adapter()
        rows = [type("R", (), {"values": lambda self: ["Paper", "id"]})()]
        monkeypatch.setattr(target, "_session", lambda: PlanSession(SCAN_PLAN, [], rows))
        # A catalogue that positively lists the index short-circuits, so a
        # planner quirk cannot override direct evidence.
        assert target.schema_is_ready() is True


class TestFlavourWiring:
    def test_cognodb_probes_constraints_as_well_as_indexes(self):
        probes = " ".join(FLAVOURS["cognodb"].index_probes)
        # A unique constraint may be catalogued only under constraints.
        assert "SHOW CONSTRAINTS" in probes
        assert "SHOW INDEXES" in probes

    def test_cognodb_awaits_indexes_before_looking(self):
        # Index creation is asynchronous on several engines; a catalogue read
        # microseconds after CREATE can honestly return nothing.
        assert any("awaitIndexes" in s for s in FLAVOURS["cognodb"].await_statements)

    def test_cognodb_has_a_plan_probe(self):
        assert "EXPLAIN" in FLAVOURS["cognodb"].plan_probe

    def test_neo4j_also_gained_the_plan_probe(self):
        # The same verification is stronger everywhere, and applying it only to
        # the engine that failed would make the check itself vendor-specific.
        assert "EXPLAIN" in FLAVOURS["neo4j"].plan_probe


class TestConnectionLossClassification:
    @pytest.mark.parametrize(
        "message",
        [
            "defunct connection",
            "Failed to read from defunct connection",
            "ServiceUnavailable: connection to the server was lost",
            "SessionExpired",
            "connection reset by peer",
            "broken pipe",
        ],
    )
    def test_transport_failures_are_recognised(self, message):
        assert _is_connection_loss(Exception(message))

    @pytest.mark.parametrize(
        "message",
        [
            "CypherSyntaxError: unexpected token IN",
            "ClientError: unknown function algo.bfs",
            "Constraint already exists",
        ],
    )
    def test_query_rejections_are_not_connection_loss(self, message):
        # Conflating the two is what made five workloads look unsupported when
        # their connection had simply been killed by an earlier one.
        assert not _is_connection_loss(Exception(message))

    def test_the_whole_cause_chain_is_searched(self):
        try:
            try:
                raise OSError("failed to read from defunct connection")
            except OSError as inner:
                raise RuntimeError("query failed") from inner
        except RuntimeError as outer:
            # The driver wraps the real cause; matching only the outermost
            # message would miss every one of these.
            assert _is_connection_loss(outer)


class TestProberWaitsForRecovery:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        return (root / "scripts" / "probe_workloads.py").read_text("utf-8")

    def test_waits_before_each_workload(self, source):
        assert "wait_for(" in source
        assert "recover-timeout" in source

    def test_says_a_skipped_workload_is_not_evidence(self, source):
        # The report must not let "not attempted" be read as "unsupported".
        assert "NOT evidence" in source
