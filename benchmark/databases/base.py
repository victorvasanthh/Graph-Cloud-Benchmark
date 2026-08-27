"""The adapter contract every engine implements.

The split between adapter and workload is the design decision that keeps this
benchmark defensible:

  * a **workload** owns the query text, one version per query dialect, all of
    them sitting next to each other in a single file so that a sceptical reader
    can diff the Cypher against the AQL without hunting through the tree;
  * an **adapter** owns connection setup, ingestion and result consumption, and
    knows nothing about what any particular query means.

The alternative - a method per workload on each adapter - lets the queries
drift apart engine by engine until the comparison is measuring six different
questions. Keeping the text co-located makes divergence visible in review.

Two rules bind every implementation:

1. `run` must fully consume the result set before returning. Several drivers
   stream lazily, and a `run` that returns after the first record measures
   time-to-first-row for one engine and time-to-last-row for another. That is
   not a small distortion; on aggregation workloads it is the entire result.

2. `run` must not retry. Retries belong to the runner, which records them, so
   that a target quietly retrying three times internally cannot report the
   single fast attempt as its latency.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.config import TargetConfig


@dataclass
class IngestPayload:
    """The graph handed to an adapter for loading.

    Passed as plain sequences rather than as the dataset object so that the
    adapters stay independent of which dataset is being loaded, and so that a
    unit test can drive one with a six-edge toy graph.
    """

    nodes: Sequence[int]
    edges: Sequence[tuple[int, int]]
    dates: Mapping[int, str]

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)


@dataclass
class IngestReport:
    """What actually landed in the database, as counted by the database."""

    nodes: int
    edges: int
    duration_ns: int
    batches: int

    def matches(self, expected_nodes: int, expected_edges: int) -> bool:
        return self.nodes == expected_nodes and self.edges == expected_edges


class GraphAdapter(ABC):
    """One database under measurement.

    Subclasses are constructed from a `TargetConfig` and are single-use per
    run: `connect`, then any number of `run` calls, then `close`.
    """

    #: Query dialects this engine accepts, most specific first. A workload
    #: supplying both "cypher" and "cypher_falkordb" will hand FalkorDB the
    #: specialised text and everyone else the generic one.
    dialects: tuple[str, ...] = ()

    def __init__(self, target: TargetConfig) -> None:
        self.target = target
        self.name = target.name
        self.display = target.display
        self.tier = target.tier
        self.settings = target.settings

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Open a connection and verify it. Raises ConnectionFailure."""

    @abstractmethod
    def close(self) -> None:
        """Release the connection. Must be safe to call twice."""

    def __enter__(self) -> GraphAdapter:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- introspection -----------------------------------------------------

    @abstractmethod
    def server_version(self) -> str:
        """A version string for the manifest, best effort.

        Recorded because "CognoDB was faster" is not a claim about software in
        general, it is a claim about two specific builds on two specific days.
        """

    # -- data --------------------------------------------------------------

    @abstractmethod
    def reset(self) -> None:
        """Delete all benchmark data, leaving an empty database."""

    @abstractmethod
    def prepare_schema(self) -> None:
        """Create whatever index makes lookup by paper id sane on this engine.

        Every engine gets an equivalent index, and the index creation is not
        timed. Benchmarking one database with an index against another without
        one is the oldest way to publish a fifty-fold speedup that means
        nothing, so parity here is not optional.
        """

    def diagnostics(self) -> dict[str, Any]:
        """Whatever the adapter learned that the caller could not observe.

        Used to explain an unconfirmed index. An engine whose DDL is attempted
        speculatively must report which spellings it rejected and why, or the
        operator is left with "not confirmed" and nowhere to go.
        """
        return {}

    def schema_is_ready(self) -> bool | None:
        """Whether the paper-id index verifiably exists.

        True, False, or None when the engine gives us no way to tell.

        This exists because `prepare_schema` succeeding is not the same as the
        index existing. Memgraph, for one, rejects DDL that Neo4j accepts and
        treats a repeated index creation as an error, so its flavour tolerates
        schema failures - which would let a run proceed unindexed. An engine
        measured without the index every other engine got is not slow, it is
        being asked a different question, and the resulting table would be
        wrong in the direction that looks most like a finding.
        """
        return None

    @abstractmethod
    def ingest(self, payload: IngestPayload, batch_size: int) -> IngestReport:
        """Bulk-load the citation graph and report what the server counted."""

    @abstractmethod
    def count_nodes(self) -> int:
        """Node count as reported by the server, used to verify ingestion."""

    @abstractmethod
    def count_edges(self) -> int:
        """Edge count as reported by the server, used to verify ingestion."""

    # -- measurement -------------------------------------------------------

    @abstractmethod
    def run(self, statement: str, params: dict[str, Any], timeout_s: float | None = None) -> int:
        """Execute one statement, consume every row, return the row count.

        `timeout_s` asks the *server* to stop working after that long, where
        the engine offers it as a plain client argument. It is a hint, not a
        guarantee: engines that do not support it ignore it, and the runner
        applies its own wall-clock bound on top so the harness is bounded
        whatever the server does.

        Implementations must not restructure the query or its transaction to
        obtain a timeout. Wrapping an auto-commit statement in an explicit
        transaction to get one would add a round trip and change what is being
        measured, which is a worse outcome than relying on the runner's bound.
        """

    # -- helpers -----------------------------------------------------------

    def statement_for(self, statements: dict[str, str]) -> str | None:
        """Pick the most specific dialect this engine understands."""
        for dialect in self.dialects:
            if dialect in statements:
                return statements[dialect]
        return None

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"<{type(self).__name__} {self.name}>"
