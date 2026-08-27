"""Adapter for FalkorDB.

FalkorDB speaks Cypher, but it is not a Bolt server: queries go over RESP via
its own client, results arrive fully materialised in `result_set`, and the
supported Cypher surface is a subset. Workloads that need different text here
supply it under the `cypher_falkordb` dialect key; everything else falls
through to the shared `cypher` version, which keeps the diff between engines
small and visible.

Ingestion uses parallel scalar arrays rather than the list-of-maps parameter
the Bolt adapter uses. That is a client capability difference, not a tuning
advantage: both formulations issue the same number of batched writes doing the
same work, and scalar list parameters are the shape this client serialises
most reliably.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from ..core.config import TargetConfig
from ..core.errors import ConnectionFailure, WorkloadFailure
from .base import GraphAdapter, IngestPayload, IngestReport

_NODE_INGEST = """
UNWIND range(0, size($ids) - 1) AS i
CREATE (:Paper {id: $ids[i], published: $published[i]})
""".strip()

_EDGE_INGEST = """
UNWIND range(0, size($sources) - 1) AS i
MATCH (a:Paper {id: $sources[i]})
MATCH (b:Paper {id: $targets[i]})
CREATE (a)-[:CITES]->(b)
""".strip()


class FalkorDBAdapter(GraphAdapter):
    """Cypher over RESP, via the FalkorDB client."""

    dialects = ("cypher_falkordb", "cypher")

    def __init__(self, target: TargetConfig) -> None:
        super().__init__(target)
        self.graph_name = self.settings.get("graph") or "benchmark"
        self._db: Any = None
        self._graph: Any = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        try:
            from falkordb import FalkorDB
        except ImportError as exc:  # pragma: no cover - install-time failure
            raise ConnectionFailure(
                "the falkordb client is not installed; run `pip install -r requirements.txt`"
            ) from exc

        host = self.settings.get("host") or "localhost"
        port = int(self.settings.get("port") or 6379)
        password = self.settings.get("password") or None

        try:
            self._db = FalkorDB(host=host, port=port, password=password)
            self._graph = self._db.select_graph(self.graph_name)
            # select_graph is lazy, so it proves nothing on its own. A trivial
            # query is what actually establishes that the server is reachable
            # and that the module is loaded.
            self._graph.query("RETURN 1")
        except Exception as exc:
            self._db = None
            self._graph = None
            raise ConnectionFailure(
                f"{self.name}: could not connect to {host}:{port}: {exc}"
            ) from exc

    def close(self) -> None:
        self._graph = None
        if self._db is not None:
            with contextlib.suppress(Exception):  # closing is best effort
                self._db.connection.close()
            self._db = None

    def _require_graph(self) -> Any:
        if self._graph is None:
            raise ConnectionFailure(f"{self.name}: connect() has not been called")
        return self._graph

    # -- introspection -----------------------------------------------------

    def server_version(self) -> str:
        if self._db is None:
            return "unknown"
        try:
            modules = self._db.connection.execute_command("MODULE", "LIST")
            for module in modules or ():
                fields = {_decode(module[i]): module[i + 1] for i in range(0, len(module) - 1, 2)}
                name = _decode(fields.get("name", ""))
                if "graph" in name.lower():
                    return f"FalkorDB module {name} v{fields.get('ver', '?')}"
        except Exception:
            pass
        return "unknown"

    # -- data --------------------------------------------------------------

    def reset(self) -> None:
        graph = self._require_graph()
        # `delete` raises when the key does not exist yet, which is the
        # normal state on a first run rather than an error.
        with contextlib.suppress(Exception):
            graph.delete()
        self._graph = self._db.select_graph(self.graph_name)

    def prepare_schema(self) -> None:
        graph = self._require_graph()
        # FalkorDB accepted the terse form first and the Neo4j-style form
        # later. Trying both, and requiring only that one succeed, keeps the
        # adapter working across the versions people actually have installed.
        errors = []
        for statement in (
            "CREATE INDEX FOR (p:Paper) ON (p.id)",
            "CREATE INDEX ON :Paper(id)",
        ):
            try:
                graph.query(statement)
                return
            except Exception as exc:
                errors.append(f"{statement}: {exc}")
        raise WorkloadFailure(f"{self.name}: could not create the Paper(id) index: {errors}")

    def ingest(self, payload: IngestPayload, batch_size: int) -> IngestReport:
        graph = self._require_graph()
        started = time.perf_counter_ns()
        batches = 0

        for start in range(0, len(payload.nodes), batch_size):
            chunk = payload.nodes[start : start + batch_size]
            graph.query(
                _NODE_INGEST,
                {
                    "ids": list(chunk),
                    "published": [payload.dates.get(node) for node in chunk],
                },
            )
            batches += 1

        for start in range(0, len(payload.edges), batch_size):
            chunk = payload.edges[start : start + batch_size]
            graph.query(
                _EDGE_INGEST,
                {
                    "sources": [source for source, _ in chunk],
                    "targets": [target for _, target in chunk],
                },
            )
            batches += 1

        duration_ns = time.perf_counter_ns() - started
        return IngestReport(
            nodes=self.count_nodes(),
            edges=self.count_edges(),
            duration_ns=duration_ns,
            batches=batches,
        )

    def count_nodes(self) -> int:
        return self._scalar("MATCH (p:Paper) RETURN count(p)")

    def count_edges(self) -> int:
        return self._scalar("MATCH ()-[r:CITES]->() RETURN count(r)")

    def _scalar(self, statement: str) -> int:
        graph = self._require_graph()
        result = graph.query(statement)
        rows = result.result_set or []
        return int(rows[0][0]) if rows else 0

    # -- measurement -------------------------------------------------------

    def run(self, statement: str, params: dict[str, Any]) -> int:
        graph = self._require_graph()
        try:
            result = graph.query(statement, params)
        except Exception as exc:
            raise WorkloadFailure(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        # This client materialises the whole result set before returning, so
        # the rows are already paid for by the time the timer stops. Nothing
        # further needs to be drained, unlike the streaming Bolt driver.
        return len(result.result_set or [])


def _decode(value: Any) -> str:
    """RESP replies arrive as bytes on some client versions and str on others."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
