"""Which statement each engine actually receives.

The dialect fallback is convenient and therefore dangerous: an engine with no
specialised text silently gets the generic Cypher, which is right almost
always and catastrophically wrong for the handful of statements where the
engines genuinely differ. These tests pin the specific mappings.
"""

from __future__ import annotations

import pytest
import yaml

from benchmark.core.config import TargetConfig
from benchmark.databases.arangodb import ArangoDBAdapter
from benchmark.databases.bolt import FLAVOURS, BoltAdapter
from benchmark.databases.falkordb import FalkorDBAdapter
from benchmark.workloads.queries import ALL_WORKLOADS, BY_NAME

REPO_ROOT = pytest.importorskip("pathlib").Path(__file__).resolve().parents[1]


def bolt(flavour: str) -> BoltAdapter:
    return BoltAdapter(
        TargetConfig(
            name=f"t-{flavour}",
            kind="bolt",
            display=flavour,
            tier="test",
            settings={"uri": "bolt://localhost:7687", "flavour": flavour},
        )
    )


class TestDialectSelection:
    def test_neo4j_flavour_uses_generic_cypher(self):
        assert bolt("neo4j").dialects == ("cypher",)

    def test_memgraph_flavour_prefers_its_own_dialect(self):
        # Memgraph reads Cypher but is not Neo4j. It must be able to take
        # different text where the engines actually diverge, while sharing
        # everything else.
        assert bolt("memgraph").dialects == ("cypher_memgraph", "cypher")

    def test_falkordb_prefers_its_own_dialect_then_falls_back(self):
        adapter = FalkorDBAdapter(
            TargetConfig(name="f", kind="falkordb", display="f", tier="test", settings={})
        )
        assert adapter.dialects == ("cypher_falkordb", "cypher")

    def test_arangodb_only_speaks_aql(self):
        adapter = ArangoDBAdapter(
            TargetConfig(name="a", kind="arangodb", display="a", tier="test", settings={})
        )
        assert adapter.dialects == ("aql",)


class TestShortestPathMapping:
    """The one workload where the Cypher engines genuinely disagree."""

    @pytest.fixture
    def workload(self):
        return BY_NAME["shortest_path"]

    def test_neo4j_gets_shortest_path_function(self, workload):
        text = bolt("neo4j").statement_for(workload.statements)
        assert "shortestPath(" in text

    def test_memgraph_gets_bfs_not_shortest_path(self, workload):
        # Verified against Memgraph's documentation: it has no shortestPath()
        # function, and its equivalent is a BFS expansion returning exactly one
        # shortest path. Falling back to the generic Cypher here is what the
        # smoke test caught.
        text = bolt("memgraph").statement_for(workload.statements)
        assert "*BFS" in text
        assert "shortestPath(" not in text

    def test_memgraph_bfs_is_bounded_and_typed(self, workload):
        text = bolt("memgraph").statement_for(workload.statements)
        # An unbounded BFS over this graph is a very different question from a
        # bounded one, and the bound must match the Neo4j pattern's ..8.
        assert "..8" in text
        assert ":CITES" in text

    def test_falkordb_uses_its_native_shortest_path_procedure(self, workload):
        # FalkorDB does have shortestPath(), but not for the undirected
        # variable-length form this workload needs - the smoke run rejected it.
        # algo.SPpaths is the engine's own shortest-path procedure and takes an
        # explicit direction, so the same question can be asked natively.
        adapter = FalkorDBAdapter(
            TargetConfig(name="f", kind="falkordb", display="f", tier="test", settings={})
        )
        text = adapter.statement_for(workload.statements)
        assert "algo.SPpaths" in text
        # Endpoints still resolved in a preceding MATCH, which SPpaths requires.
        assert "MATCH (a:Paper {id: $source}), (b:Paper {id: $target})" in text

    def test_falkordb_shortest_path_asks_the_same_question(self, workload):
        adapter = FalkorDBAdapter(
            TargetConfig(name="f", kind="falkordb", display="f", tier="test", settings={})
        )
        text = adapter.statement_for(workload.statements)
        # Same relationship type, same 8-hop bound, same undirected traversal,
        # same single-row hop count as every other engine. A native procedure
        # is a fair substitute only if it is answering the identical question.
        assert "'CITES'" in text
        assert "maxLen: 8" in text
        assert "relDirection: 'both'" in text
        assert "pathCount: 1" in text
        assert "AS hops" in text

    def test_every_engine_resolves_a_statement(self, workload):
        for adapter in (
            bolt("neo4j"),
            bolt("memgraph"),
            FalkorDBAdapter(
                TargetConfig(name="f", kind="falkordb", display="f", tier="test", settings={})
            ),
            ArangoDBAdapter(
                TargetConfig(name="a", kind="arangodb", display="a", tier="test", settings={})
            ),
        ):
            assert adapter.statement_for(workload.statements) is not None, adapter.name


class TestWorkloadCoverage:
    @pytest.mark.parametrize("workload", ALL_WORKLOADS, ids=lambda w: w.name)
    def test_every_workload_has_cypher_and_aql(self, workload):
        maps = list(workload.variants.values()) or [workload.statements]
        for mapping in maps:
            assert "cypher" in mapping, f"{workload.name} has no Cypher"
            assert "aql" in mapping, f"{workload.name} has no AQL"

    @pytest.mark.parametrize("workload", ALL_WORKLOADS, ids=lambda w: w.name)
    def test_every_engine_is_supported(self, workload):
        for adapter in (
            bolt("neo4j"),
            bolt("memgraph"),
            FalkorDBAdapter(
                TargetConfig(name="f", kind="falkordb", display="f", tier="test", settings={})
            ),
            ArangoDBAdapter(
                TargetConfig(name="a", kind="arangodb", display="a", tier="test", settings={})
            ),
        ):
            assert workload.supported_by(adapter.dialects), f"{workload.name} / {adapter.name}"


class TestFlavourTable:
    def test_every_flavour_declares_dialects_and_an_index_probe(self):
        for name, flavour in FLAVOURS.items():
            assert flavour.dialects, f"{name} declares no dialects"
            # Without a probe the harness cannot tell "index created" from
            # "DDL silently tolerated", which is the whole point of the check.
            assert flavour.index_probes, f"{name} has no index probe"

    def test_memgraph_tolerates_schema_errors_but_is_still_verified(self):
        memgraph = FLAVOURS["memgraph"]
        assert memgraph.tolerate_schema_errors is True
        assert memgraph.index_probes, (
            "tolerating DDL errors is only safe because the index is confirmed separately"
        )


class TestPinnedImages:
    @pytest.fixture
    def services(self) -> dict:
        raw = yaml.safe_load(
            (REPO_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        )
        return raw["services"]

    def test_no_floating_tags(self, services):
        for name, service in services.items():
            image = service["image"]
            assert ":" in image, f"{name} has no tag at all"
            tag = image.rsplit(":", 1)[1]
            # `latest` makes a run unreproducible the moment upstream pushes.
            assert tag not in {"latest", "edge"}, f"{name} uses a floating tag: {image}"

    def test_falkordb_tag_is_the_verified_one(self, services):
        # v4.4.4 was carried here for a while and has never existed; the pull
        # failed and the target silently never ran.
        assert services["falkordb"]["image"] == "falkordb/falkordb:v4.20.4"

    def test_every_service_declares_both_cap_styles(self, services):
        for name, service in services.items():
            # `deploy.resources` is honoured by Swarm and newer Compose;
            # `cpus`/`mem_limit` cover the rest. A benchmark that claims a cap
            # it did not apply is worse than one with no cap.
            assert "cpus" in service, f"{name} has no cpus limit"
            assert "mem_limit" in service, f"{name} has no mem_limit"
