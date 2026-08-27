"""CognoDB speaks Bolt, but not Neo4j 5's Cypher.

A live instance completed DNS, TCP, TLS, the Bolt handshake, driver connect and
`RETURN 1`, then rejected the reset with:

    syntax error at position 42: unexpected token IN ("IN")

Position 42 is exactly where `CALL { ... } IN TRANSACTIONS` begins. That
construct arrived in Neo4j 4.4, so a working Bolt connection says nothing about
which Cypher level an engine implements - which is the lesson these tests pin.
"""

from __future__ import annotations

import pytest

from benchmark.core.config import TargetConfig
from benchmark.core.errors import WorkloadFailure
from benchmark.databases.bolt import FLAVOURS, BoltAdapter


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


class FakeSession:
    """Records statements and replays scripted single() results."""

    def __init__(self, script: list, log: list[str], reject: tuple[str, ...] = ()) -> None:
        self._script = script
        self._log = log
        self._reject = reject

    def run(self, statement, *args, **kwargs):
        self._log.append(statement)
        for fragment in self._reject:
            if fragment in statement:
                raise RuntimeError(f"syntax error: unexpected token near {fragment!r}")
        return self

    def single(self):
        return self._script.pop(0) if self._script else None

    def consume(self):
        return None

    def __iter__(self):
        return iter(())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def wire(monkeypatch, target: BoltAdapter, script=None, reject=()) -> list[str]:
    log: list[str] = []
    monkeypatch.setattr(target, "_session", lambda: FakeSession(list(script or []), log, reject))
    return log


class TestFlavourTable:
    def test_cognodb_flavour_exists(self):
        assert "cognodb" in FLAVOURS

    def test_cognodb_does_not_use_call_in_transactions(self):
        flavour = FLAVOURS["cognodb"]
        combined = " ".join(flavour.reset) + " " + flavour.reset_batch
        # The exact construct the live engine rejected.
        assert "IN TRANSACTIONS" not in combined

    def test_cognodb_uses_a_client_side_batch(self):
        flavour = FLAVOURS["cognodb"]
        assert flavour.reset_batch, "cognodb must delete in client-driven batches"
        assert "{batch}" in flavour.reset_batch
        assert "count(" in flavour.reset_batch, "the loop needs a count to know when to stop"

    def test_batch_size_is_substituted_not_parameterised(self):
        flavour = FLAVOURS["cognodb"]
        rendered = flavour.reset_batch.format(batch=flavour.reset_batch_size)
        # No engine here accepts a parameter in a LIMIT clause, so the size has
        # to be a literal in the text.
        assert "$" not in rendered
        assert str(flavour.reset_batch_size) in rendered

    def test_cognodb_tolerates_ddl_failure_but_still_verifies(self):
        flavour = FLAVOURS["cognodb"]
        # Trying several DDL spellings is only defensible because the result is
        # confirmed afterwards.
        assert flavour.tolerate_schema_errors is True
        assert flavour.index_probes, "guessing at DDL requires verifying the outcome"

    def test_cognodb_offers_several_schema_spellings(self):
        # We do not know which DDL generation it accepts, so all three are tried.
        assert len(FLAVOURS["cognodb"].schema) >= 2


class TestNeo4jAndAuraUnchanged:
    """The fix must not touch the engines that already worked."""

    def test_neo4j_still_uses_call_in_transactions(self):
        assert "IN TRANSACTIONS" in FLAVOURS["neo4j"].reset[0]

    def test_neo4j_has_no_client_side_batch(self):
        assert FLAVOURS["neo4j"].reset_batch == ""

    def test_memgraph_reset_unchanged(self):
        assert FLAVOURS["memgraph"].reset == ("MATCH (n) DETACH DELETE n",)
        assert FLAVOURS["memgraph"].reset_batch == ""

    def test_neo4j_still_uses_the_generic_cypher_dialect(self):
        assert adapter("neo4j").dialects == ("cypher",)

    def test_aura_resolves_to_the_neo4j_flavour(self):
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parents[1]
        raw = yaml.safe_load((root / "config" / "databases.yaml").read_text("utf-8"))
        by_name = {t["name"]: t for t in raw["targets"]}
        # Aura is the calibration anchor: it must stay byte-identical to the
        # self-hosted Neo4j target, or the gap between them stops measuring the
        # platform and starts measuring our configuration.
        assert by_name["aura-free"]["settings"]["flavour"] == "neo4j"
        assert by_name["neo4j-selfhosted"]["settings"]["flavour"] == "neo4j"
        assert by_name["cognodb-cloud"]["settings"]["flavour"] == "cognodb"


class TestBatchedReset:
    def test_loops_until_nothing_is_deleted(self, monkeypatch):
        target = adapter("cognodb")
        # Three passes returning 5000, 5000, 0 - then it must stop.
        log = wire(monkeypatch, target, script=[[5000], [5000], [0]])
        target.reset()
        assert len(log) == 3
        assert all("LIMIT 5000" in statement for statement in log)

    def test_stops_immediately_on_an_empty_database(self, monkeypatch):
        target = adapter("cognodb")
        log = wire(monkeypatch, target, script=[[0]])
        target.reset()
        assert len(log) == 1

    def test_a_missing_row_is_treated_as_done(self, monkeypatch):
        # single() returning None must terminate rather than loop forever.
        target = adapter("cognodb")
        log = wire(monkeypatch, target, script=[])
        target.reset()
        assert len(log) == 1

    def test_refuses_to_loop_forever(self, monkeypatch):
        target = adapter("cognodb")
        # An engine that always claims progress would otherwise hang setup with
        # no diagnosis at all.
        monkeypatch.setattr(target, "_session", lambda: FakeSession([[1]] * 100_000, [], ()))
        with pytest.raises(WorkloadFailure, match="looping forever"):
            target.reset()

    def test_neo4j_reset_runs_its_statement_once(self, monkeypatch):
        target = adapter("neo4j")
        log = wire(monkeypatch, target)
        target.reset()
        assert len(log) == 1
        assert "IN TRANSACTIONS" in log[0]


class TestSchemaProbeFallback:
    def test_falls_through_to_the_next_probe(self, monkeypatch):
        target = adapter("cognodb")
        # SHOW INDEXES is rejected; CALL db.indexes() must still be tried.
        log = wire(monkeypatch, target, reject=("SHOW INDEXES",))
        target.schema_is_ready()
        assert any("db.indexes" in statement for statement in log)

    def test_unknown_when_every_probe_is_rejected(self, monkeypatch):
        target = adapter("cognodb")
        wire(monkeypatch, target, reject=("SHOW INDEXES", "db.indexes"))
        # "Could not ask" is not the same claim as "checked, it is missing".
        assert target.schema_is_ready() is None
