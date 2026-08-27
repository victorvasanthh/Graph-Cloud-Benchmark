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
    def run(self, statement: str, params: dict[str, Any]) -> int:
        """Execute one statement, consume every row, return the row count."""

    # -- helpers -----------------------------------------------------------

    def statement_for(self, statements: dict[str, str]) -> str | None:
        """Pick the most specific dialect this engine understands."""
        for dialect in self.dialects:
            if dialect in statements:
                return statements[dialect]
        return None

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"<{type(self).__name__} {self.name}>"
