"""A tolerated failure must still be recorded.

CognoDB loaded 27,770 nodes and 352,768 edges successfully, then reported its
Paper(id) index as "not confirmed" - with nothing to say why. The adapter had
attempted three DDL spellings and two introspection queries and discarded every
error, so the operator could not tell rejected DDL from unimplemented
introspection, and those need different fixes.

Tolerating an error is a decision about control flow. Discarding it is a
decision to be undiagnosable, and the two do not have to travel together.
"""

from __future__ import annotations

import pytest

from benchmark.core.config import TargetConfig
from benchmark.databases.bolt import BoltAdapter
from tests.test_cognodb_flavour import FakeSession


def adapter(flavour: str) -> BoltAdapter:
    return BoltAdapter(
        TargetConfig(
            name=f"t-{flavour}",
            kind="bolt",
            display=flavour,
            tier="test",
            settings={"uri": "bolt://localhost:7687", "flavour": flavour},
        )
    )


def wire(monkeypatch, target, script=None, reject=()):
    log: list[str] = []
    monkeypatch.setattr(target, "_session", lambda: FakeSession(list(script or []), log, reject))
    return log


class TestSchemaAttemptsAreRecorded:
    def test_successful_ddl_is_recorded(self, monkeypatch):
        target = adapter("cognodb")
        wire(monkeypatch, target)
        target.prepare_schema()
        assert len(target.schema_attempts) == 3
        assert all(outcome == "ok" for _, outcome in target.schema_attempts)

    def test_rejected_ddl_keeps_the_error_text(self, monkeypatch):
        target = adapter("cognodb")
        # Neo4j 5 syntax rejected, older spellings accepted - the shape we
        # expect from an engine that stops at CALL { } IN TRANSACTIONS.
        wire(monkeypatch, target, reject=("REQUIRE",))
        target.prepare_schema()

        outcomes = dict(target.schema_attempts)
        rejected = [s for s, o in outcomes.items() if o != "ok"]
        assert len(rejected) == 1
        assert "REQUIRE" in rejected[0]
        # The message must survive, not just the fact of failure.
        assert "syntax error" in outcomes[rejected[0]]

    def test_every_spelling_failing_is_still_recorded(self, monkeypatch):
        target = adapter("cognodb")
        wire(monkeypatch, target, reject=("CREATE",))
        target.prepare_schema()
        assert len(target.schema_attempts) == 3
        assert not any(outcome == "ok" for _, outcome in target.schema_attempts)

    def test_attempts_reset_between_runs(self, monkeypatch):
        target = adapter("cognodb")
        wire(monkeypatch, target)
        target.prepare_schema()
        target.prepare_schema()
        # Accumulating across calls would misreport a second target's setup.
        assert len(target.schema_attempts) == 3

    def test_intolerant_flavour_still_raises(self, monkeypatch):
        from benchmark.core.errors import WorkloadFailure

        target = adapter("neo4j")
        wire(monkeypatch, target, reject=("CREATE CONSTRAINT",))
        # neo4j does not tolerate schema errors, and recording must not have
        # quietly turned a hard failure into a soft one.
        with pytest.raises(WorkloadFailure):
            target.prepare_schema()
        assert target.schema_attempts and target.schema_attempts[0][1] != "ok"


class TestProbeAttemptsAreRecorded:
    def test_successful_probe_is_recorded(self, monkeypatch):
        target = adapter("cognodb")
        wire(monkeypatch, target)
        target.schema_is_ready()
        assert len(target.probe_attempts) == 1
        assert target.probe_attempts[0][1].startswith("ok")

    def test_first_probe_rejected_second_tried(self, monkeypatch):
        target = adapter("cognodb")
        wire(monkeypatch, target, reject=("SHOW INDEXES",))
        target.schema_is_ready()
        assert len(target.probe_attempts) == 2
        assert target.probe_attempts[0][1] != "ok"
        assert target.probe_attempts[1][1].startswith("ok")

    def test_all_probes_rejected_yields_unknown_with_reasons(self, monkeypatch):
        target = adapter("cognodb")
        wire(monkeypatch, target, reject=("SHOW INDEXES", "db.indexes"))
        # "Could not ask" stays distinct from "asked, and it is missing".
        assert target.schema_is_ready() is None
        assert len(target.probe_attempts) == 2
        assert all(outcome != "ok" for _, outcome in target.probe_attempts)


class TestDiagnosticsSurface:
    def test_diagnostics_expose_both_lists(self, monkeypatch):
        target = adapter("cognodb")
        wire(monkeypatch, target, reject=("REQUIRE", "SHOW INDEXES"))
        target.prepare_schema()
        target.schema_is_ready()

        diagnostics = target.diagnostics()
        assert diagnostics["schema_attempts"], "DDL attempts must be reportable"
        assert diagnostics["index_probe_attempts"], "probe attempts must be reportable"
        for entry in diagnostics["schema_attempts"]:
            assert "statement" in entry and "outcome" in entry

    def test_base_adapter_returns_an_empty_mapping(self):
        # An adapter with nothing to add must not break the runner's reporting.
        from benchmark.databases.arangodb import ArangoDBAdapter

        other = ArangoDBAdapter(
            TargetConfig(name="a", kind="arangodb", display="a", tier="t", settings={})
        )
        assert other.diagnostics() == {}
