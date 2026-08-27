"""The measured queries, all dialects side by side.

Everything a reviewer needs in order to accuse this benchmark of asking
ArangoDB a harder question than Neo4j is in this one file, deliberately. Each
workload lists its Cypher and its AQL next to each other, and states how close
the two really are.

Bind parameters use the same names in both dialects (`$id` in Cypher, `@id` in
AQL), so a single parameter dictionary drives every engine unchanged.

Two AQL conventions are followed throughout, both because the obvious shorter
form is invalid rather than merely unidiomatic:

  * a traversal or shortest-path start vertex is bound with LET before use.
    An inline function call in that position is not accepted everywhere a
    document handle is.
  * a subquery is always parenthesised - `LENGTH((FOR ... RETURN 1))`, never
    `LENGTH(FOR ... RETURN 1)`, which does not parse.
"""

from __future__ import annotations

import random
from typing import Any

from ..datasets.cit_hepth import CitationGraph
from .base import OP_KEY, Workload, cycle_to_length

# ---------------------------------------------------------------------------
# parameter builders
# ---------------------------------------------------------------------------


def _random_paper_ids(
    graph: CitationGraph, params: dict[str, Any], count: int, seed: int
) -> list[dict[str, Any]]:
    distinct = int(params.get("distinct_parameters", 100))
    ids = graph.sample_nodes(distinct, seed=seed)
    return [{"id": paper_id} for paper_id in cycle_to_length(ids, count)]


def _high_degree_paper_ids(
    graph: CitationGraph, params: dict[str, Any], count: int, seed: int
) -> list[dict[str, Any]]:
    """Sources chosen from the most-citing papers.

    Expansion workloads run from high out-degree nodes on purpose: a uniformly
    random paper in this graph cites about a dozen others, and timing that
    measures the network round trip rather than the traversal. The choice is
    applied identically to every engine and is stated in the report, which is
    what separates a deliberate workload from a rigged one.
    """
    top = int(params.get("top_n", 200))
    ids = graph.top_out_degree(top)
    return [{"id": paper_id} for paper_id in cycle_to_length(ids, count)]


def _connected_pairs(
    graph: CitationGraph, params: dict[str, Any], count: int, seed: int
) -> list[dict[str, Any]]:
    distinct = int(params.get("distinct_parameters", 25))
    pairs = graph.sample_connected_pairs(
        distinct,
        seed=seed,
        min_hops=int(params.get("min_hops", 3)),
        max_hops=int(params.get("max_hops", 6)),
    )
    return [
        {"source": source, "target": target} for source, target in cycle_to_length(pairs, count)
    ]


def _limit_only(
    graph: CitationGraph, params: dict[str, Any], count: int, seed: int
) -> list[dict[str, Any]]:
    limit = int(params.get("limit", 25))
    return [{"limit": limit} for _ in range(count)]


def _date_windows(
    graph: CitationGraph, params: dict[str, Any], count: int, seed: int
) -> list[dict[str, Any]]:
    """Fixed date windows, one per year of the corpus.

    Not randomised: the windows are a property of the dataset rather than of
    the run, and varying them per iteration would fold the differing size of
    each year into the latency spread for no analytical gain.
    """
    limit = int(params.get("limit", 25))
    windows = params.get("windows") or [
        ("1995-01-01", "1996-01-01"),
        ("1997-01-01", "1998-01-01"),
        ("1999-01-01", "2000-01-01"),
        ("2001-01-01", "2002-01-01"),
    ]
    prepared = [
        {"from": start, "to": end, "limit": limit} for start, end in (tuple(w) for w in windows)
    ]
    return cycle_to_length(prepared, count)


def _mixed_operations(
    graph: CitationGraph, params: dict[str, Any], count: int, seed: int
) -> list[dict[str, Any]]:
    """A deterministic read/write interleaving, identical on every engine.

    The operation sequence is drawn once from a seeded RNG and then replayed,
    so target A and target B perform the same operation at the same position.
    A mix re-randomised per engine would give one of them a different ratio of
    cheap reads to expensive writes and call the difference performance.
    """
    write_ratio = float(params.get("write_ratio", 0.1))
    if not 0.0 <= write_ratio <= 1.0:
        raise ValueError(f"write_ratio must be between 0 and 1, got {write_ratio}")
    distinct = int(params.get("distinct_parameters", 100))
    ids = graph.sample_nodes(distinct, seed=seed)
    if not ids:
        return []

    rng = random.Random(seed)
    prepared: list[dict[str, Any]] = []
    for index in range(count):
        paper_id = ids[index % len(ids)]
        op = "write" if rng.random() < write_ratio else "read"
        prepared.append({"id": paper_id, OP_KEY: op})
    return prepared


# ---------------------------------------------------------------------------
# workloads
# ---------------------------------------------------------------------------

POINT_LOOKUP = Workload(
    name="point_lookup",
    description="Fetch a single paper by id.",
    equivalence="idiomatic",
    equivalence_note=(
        "Cypher resolves the unique constraint on :Paper(id); AQL resolves the "
        "primary index via DOCUMENT(). Both are the engine's fastest single-key "
        "access path, which is the property being held equal."
    ),
    defaults={"distinct_parameters": 100},
    build_params=_random_paper_ids,
    statements={
        "cypher": "MATCH (p:Paper {id: $id}) RETURN p.id AS id, p.published AS published",
        "aql": (
            'LET p = DOCUMENT(CONCAT("papers/", @id)) '
            "FILTER p != null "
            "RETURN {id: p.pid, published: p.published}"
        ),
    },
)

ONE_HOP = Workload(
    name="one_hop",
    description="Papers directly cited by a given paper.",
    equivalence="idiomatic",
    equivalence_note="A single OUTBOUND edge step in both dialects.",
    defaults={"top_n": 200},
    build_params=_high_degree_paper_ids,
    statements={
        "cypher": "MATCH (:Paper {id: $id})-[:CITES]->(c:Paper) RETURN c.id AS id",
        "aql": (
            'LET start = CONCAT("papers/", @id) '
            "FOR c IN 1..1 OUTBOUND start cites "
            "RETURN {id: c.pid}"
        ),
    },
)

TWO_HOP = Workload(
    name="two_hop",
    description="Distinct papers exactly two citation hops downstream.",
    equivalence="idiomatic",
    equivalence_note=(
        "Cypher variable-length patterns are relationship-isomorphic, so no "
        "edge repeats within a path. The AQL traversal is pinned to "
        "uniqueEdges:'path' to match that exactly rather than relying on the "
        "default, which has changed between ArangoDB versions."
    ),
    defaults={"top_n": 200},
    build_params=_high_degree_paper_ids,
    statements={
        "cypher": "MATCH (:Paper {id: $id})-[:CITES*2]->(c:Paper) RETURN DISTINCT c.id AS id",
        "aql": (
            'LET start = CONCAT("papers/", @id) '
            "FOR c IN 2..2 OUTBOUND start cites "
            "OPTIONS {uniqueEdges: 'path', uniqueVertices: 'none'} "
            "RETURN DISTINCT {id: c.pid}"
        ),
    },
)

NEIGHBOURHOOD = Workload(
    name="neighbourhood_3hop",
    description="Size of the undirected three-hop neighbourhood of a paper.",
    equivalence="loose",
    equivalence_note=(
        "The two engines are asked for the same number and allowed to reach it "
        "differently. Cypher enumerates paths and deduplicates with "
        "count(DISTINCT ...); AQL uses uniqueVertices:'global', which prunes "
        "during the walk. That is a genuine difference in work done, so this "
        "row compares engine-plus-optimiser rather than raw traversal speed, "
        "and it is the one workload here whose ratio should not be quoted on "
        "its own."
    ),
    defaults={"top_n": 200},
    build_params=_high_degree_paper_ids,
    statements={
        "cypher": (
            "MATCH (:Paper {id: $id})-[:CITES*1..3]-(c:Paper) RETURN count(DISTINCT c) AS reachable"
        ),
        "aql": (
            'LET start = CONCAT("papers/", @id) '
            "LET reached = ("
            "FOR c IN 1..3 ANY start cites "
            "OPTIONS {uniqueVertices: 'global', bfs: true} "
            "RETURN 1) "
            "RETURN {reachable: LENGTH(reached)}"
        ),
    },
)

SHORTEST_PATH = Workload(
    name="shortest_path",
    description="Hop count of the shortest undirected path between two papers.",
    equivalence="idiomatic",
    equivalence_note=(
        "Both sides run the engine's built-in shortest-path operator and "
        "return a single row holding the hop count. Pairs are pre-validated as "
        "reachable, so no engine is credited for failing fast on a "
        "disconnected pair."
    ),
    defaults={"distinct_parameters": 25, "min_hops": 3, "max_hops": 6},
    build_params=_connected_pairs,
    statements={
        # Neo4j, Aura, CognoDB and FalkorDB all provide shortestPath(). The
        # endpoints are resolved in a separate MATCH and the pattern carries no
        # property filter, which FalkorDB requires and Neo4j is happy with.
        "cypher": (
            "MATCH (a:Paper {id: $source}), (b:Paper {id: $target}) "
            "MATCH path = shortestPath((a)-[:CITES*..8]-(b)) "
            "RETURN length(path) AS hops"
        ),
        # FalkorDB has shortestPath(), but its planner rejects the undirected
        # variable-length form this workload needs. Its native equivalent is
        # algo.SPpaths with relDirection 'both' - the same question asked of
        # the engine's own shortest-path procedure, filtered to the same
        # relationship type and bounded at the same 8 hops, returning the same
        # single row. `pathCount: 1` asks for one shortest path, matching what
        # shortestPath() returns everywhere else.
        "cypher_falkordb": (
            "MATCH (a:Paper {id: $source}), (b:Paper {id: $target}) "
            "CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['CITES'], "
            "relDirection: 'both', maxLen: 8, pathCount: 1}) "
            "YIELD path "
            "RETURN length(path) AS hops"
        ),
        # Memgraph has no shortestPath(). Its equivalent is a BFS expansion,
        # which returns exactly one shortest path - the same answer by the same
        # definition, reached through the engine's own operator rather than
        # through an emulation we wrote. `size(relationships(path))` is the
        # portable spelling of `length(path)` here.
        "cypher_memgraph": (
            "MATCH path = (a:Paper {id: $source})-[:CITES *BFS ..8]-(b:Paper {id: $target}) "
            "RETURN size(relationships(path)) AS hops"
        ),
        "aql": (
            'LET source = CONCAT("papers/", @source) '
            'LET target = CONCAT("papers/", @target) '
            "LET vertices = (FOR v IN ANY SHORTEST_PATH source TO target cites RETURN 1) "
            "RETURN {hops: LENGTH(vertices) - 1}"
        ),
    },
)

TOP_CITED = Workload(
    name="top_cited",
    description="The most-cited papers in the corpus, by in-degree.",
    equivalence="idiomatic",
    equivalence_note=(
        "A full scan and grouping of every edge in both dialects. This is the "
        "workload least sensitive to modelling choices and the most sensitive "
        "to how much memory the tier allows."
    ),
    defaults={"limit": 25},
    build_params=_limit_only,
    statements={
        "cypher": (
            "MATCH (:Paper)-[:CITES]->(p:Paper) "
            "RETURN p.id AS id, count(*) AS citations "
            "ORDER BY citations DESC, id ASC LIMIT $limit"
        ),
        "aql": (
            "FOR e IN cites COLLECT target = e._to WITH COUNT INTO citations "
            "SORT citations DESC, target ASC LIMIT @limit "
            "RETURN {id: TO_NUMBER(SUBSTRING(target, 7)), citations: citations}"
        ),
    },
)

DATE_FILTERED = Workload(
    name="date_filtered_top",
    description="Most-cited papers published within a given year.",
    equivalence="idiomatic",
    equivalence_note=(
        "Filter on an ISO date string, then count incoming citations. Only "
        "about 41% of nodes carry a publication date in the source data, so "
        "this workload deliberately touches a minority of the graph; the "
        "absolute row counts are small by construction, not by accident."
    ),
    defaults={"limit": 25},
    build_params=_date_windows,
    statements={
        "cypher": (
            "MATCH (p:Paper) WHERE p.published >= $from AND p.published < $to "
            "OPTIONAL MATCH (c:Paper)-[:CITES]->(p) "
            "WITH p, count(c) AS citations "
            "RETURN p.id AS id, citations ORDER BY citations DESC, id ASC LIMIT $limit"
        ),
        "aql": (
            "FOR p IN papers "
            "FILTER p.published >= @from AND p.published < @to "
            "LET citations = LENGTH((FOR c IN 1..1 INBOUND p cites RETURN 1)) "
            "SORT citations DESC, p.pid ASC LIMIT @limit "
            "RETURN {id: p.pid, citations: citations}"
        ),
    },
)

MIXED = Workload(
    name="mixed_read_write",
    description="Read-heavy mix: one-hop expansion with interleaved counter updates.",
    equivalence="idiomatic",
    equivalence_note=(
        "The read is the one_hop expansion; the write increments a counter "
        "property on a single paper. The write deliberately touches only a "
        "property and never the graph structure, so it cannot change what any "
        "other workload measures. Read and write positions in the sequence are "
        "fixed by the seed and identical on every engine."
    ),
    defaults={"write_ratio": 0.1, "distinct_parameters": 100},
    build_params=_mixed_operations,
    mutates=True,
    variants={
        "read": {
            "cypher": "MATCH (:Paper {id: $id})-[:CITES]->(c:Paper) RETURN c.id AS id",
            "aql": (
                'LET start = CONCAT("papers/", @id) '
                "FOR c IN 1..1 OUTBOUND start cites "
                "RETURN {id: c.pid}"
            ),
        },
        "write": {
            "cypher": (
                "MATCH (p:Paper {id: $id}) "
                "SET p.access_count = coalesce(p.access_count, 0) + 1 "
                "RETURN p.access_count AS access_count"
            ),
            "aql": (
                'LET doc = DOCUMENT(CONCAT("papers/", @id)) '
                "UPDATE doc WITH {access_count: (doc.access_count OR 0) + 1} IN papers "
                "RETURN {access_count: NEW.access_count}"
            ),
        },
    },
)


ALL_WORKLOADS: list[Workload] = [
    POINT_LOOKUP,
    ONE_HOP,
    TWO_HOP,
    NEIGHBOURHOOD,
    SHORTEST_PATH,
    TOP_CITED,
    DATE_FILTERED,
    MIXED,
]

BY_NAME: dict[str, Workload] = {w.name: w for w in ALL_WORKLOADS}
