"""Adapter for every engine that speaks Bolt: CognoDB, Neo4j, Aura, Memgraph.

One class serves all four. That is not laziness - it is the point. The same
driver, the same session handling and the same result-consumption code path
run against each of them, so any difference in the reported latency is a
difference in the server rather than in our client.

Where the engines genuinely diverge (index DDL, bulk delete, version probing)
the difference is isolated in a `_Flavour` table below and nowhere else. The
flavour is a config setting, not a hardcoded mapping from target name, so an
engine whose dialect turns out to differ from what we assumed can be corrected
in YAML without a code change.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..core.config import TargetConfig
from ..core.errors import ConnectionFailure, WorkloadFailure
from .base import GraphAdapter, IngestPayload, IngestReport


@dataclass(frozen=True)
class _Flavour:
    """The handful of statements that are not portable across Bolt engines."""

    schema: tuple[str, ...]
    reset: tuple[str, ...]
    version_probes: tuple[str, ...]
    #: Query dialects this flavour accepts, most specific first. Memgraph reads
    #: Cypher but is not Neo4j: it has no shortestPath() and expresses the same
    #: idea with a BFS expansion, so it needs a way to opt into different text
    #: for the statements that actually differ while sharing the rest.
    dialects: tuple[str, ...] = ("cypher",)
    #: Statement that lists indexes, used to prove the index really exists.
    index_probe: str = ""
    #: Memgraph rejects `CREATE CONSTRAINT ... IF NOT EXISTS`, and re-running a
    #: constraint that already exists is an error there rather than a no-op.
    #: Schema DDL is therefore allowed to fail without failing the run.
    tolerate_schema_errors: bool = False


_NEO4J = _Flavour(
    schema=("CREATE CONSTRAINT paper_id IF NOT EXISTS FOR (p:Paper) REQUIRE p.id IS UNIQUE",),
    # Deleting 350k relationships in one transaction is what actually kills a
    # 1 GB free-tier instance, so the delete is chunked server-side. The row
    # count has to be a literal; Cypher does not accept a parameter there.
    reset=("MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS",),
    version_probes=(
        "CALL dbms.components() YIELD name, versions, edition "
        "RETURN name + ' ' + versions[0] + ' (' + edition + ')' AS version",
    ),
    dialects=("cypher",),
    index_probe="SHOW INDEXES YIELD labelsOrTypes, properties RETURN labelsOrTypes, properties",
)

_MEMGRAPH = _Flavour(
    schema=(
        "CREATE INDEX ON :Paper(id)",
        "CREATE CONSTRAINT ON (p:Paper) ASSERT p.id IS UNIQUE",
    ),
    reset=("MATCH (n) DETACH DELETE n",),
    version_probes=("SHOW VERSION",),
    dialects=("cypher_memgraph", "cypher"),
    index_probe="SHOW INDEX INFO",
    tolerate_schema_errors=True,
)

FLAVOURS: dict[str, _Flavour] = {"neo4j": _NEO4J, "memgraph": _MEMGRAPH}

_NODE_INGEST = """
UNWIND $rows AS row
CREATE (p:Paper {id: row.id})
SET p.published = row.published
""".strip()

# MATCH-then-CREATE rather than MERGE: the source edge list is deduplicated at
# parse time, so a MERGE on the relationship would pay for an index probe on
# every one of 352,768 edges to prevent a duplicate that cannot occur.
_EDGE_INGEST = """
UNWIND $rows AS row
MATCH (a:Paper {id: row.f})
MATCH (b:Paper {id: row.t})
CREATE (a)-[:CITES]->(b)
""".strip()


class BoltAdapter(GraphAdapter):
    """Cypher over Bolt, via the official Neo4j driver."""

    dialects = ("cypher",)

    def __init__(self, target: TargetConfig) -> None:
        super().__init__(target)
        flavour_name = self.settings.get("flavour") or self.settings.get("flavor") or "neo4j"
        if flavour_name not in FLAVOURS:
            raise ValueError(
                f"target {self.name!r} requests unknown Bolt flavour {flavour_name!r}; "
                f"known flavours are {sorted(FLAVOURS)}"
            )
        self.flavour_name = flavour_name
        self.flavour = FLAVOURS[flavour_name]
        # Instance-level, so one adapter class can serve engines whose Cypher
        # diverges without the registry needing a class per product.
        self.dialects = self.flavour.dialects
        self.database = self.settings.get("database") or None
        self._driver: Any = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:  # pragma: no cover - install-time failure
            raise ConnectionFailure(
                "the neo4j driver is not installed; run `pip install -r requirements.txt`"
            ) from exc

        uri = self.settings.get("uri", "")
        if not uri:
            raise ConnectionFailure(f"target {self.name!r} has no URI configured")

        username = self.settings.get("username", "")
        password = self.settings.get("password", "")
        # Memgraph runs unauthenticated by default. Passing ("", "") to the
        # driver is not the same as passing None, and the difference is a
        # confusing handshake error rather than a clear one.
        auth = (username, password) if username else None

        try:
            self._driver = GraphDatabase.driver(uri, auth=auth)
            self._driver.verify_connectivity()
        except Exception as exc:
            self._driver = None
            raise ConnectionFailure(f"{self.name}: could not connect to {uri}: {exc}") from exc

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def _session(self) -> Any:
        if self._driver is None:
            raise ConnectionFailure(f"{self.name}: connect() has not been called")
        if self.database:
            return self._driver.session(database=self.database)
        return self._driver.session()

    # -- introspection -----------------------------------------------------

    def server_version(self) -> str:
        for probe in self.flavour.version_probes:
            try:
                with self._session() as session:
                    record = session.run(probe).single()
                if record is not None:
                    return str(record[0])
            except Exception:
                # Version reporting is best effort. A managed instance that
                # withholds dbms.components() is not a reason to abort a run.
                continue
        return "unknown"

    # -- data --------------------------------------------------------------

    def reset(self) -> None:
        with self._session() as session:
            for statement in self.flavour.reset:
                session.run(statement).consume()

    def prepare_schema(self) -> None:
        with self._session() as session:
            for statement in self.flavour.schema:
                try:
                    session.run(statement).consume()
                except Exception as exc:
                    if not self.flavour.tolerate_schema_errors:
                        raise WorkloadFailure(
                            f"{self.name}: schema statement failed: {statement}: {exc}"
                        ) from exc

    def schema_is_ready(self) -> bool | None:
        probe = self.flavour.index_probe
        if not probe:
            return None
        try:
            with self._session() as session:
                rows = [str(record.values()) for record in session.run(probe)]
        except Exception:
            # Not being able to ask is different from the answer being no.
            return None
        # Both SHOW INDEXES (Neo4j) and SHOW INDEX INFO (Memgraph) return the
        # label and property somewhere in the row; matching on both together
        # avoids depending on a column layout that differs between them and
        # has changed between versions of each.
        return any("Paper" in row and "id" in row for row in rows)

    def ingest(self, payload: IngestPayload, batch_size: int) -> IngestReport:
        started = time.perf_counter_ns()
        batches = 0

        with self._session() as session:
            for chunk in _chunked(payload.nodes, batch_size):
                rows = [{"id": node, "published": payload.dates.get(node)} for node in chunk]
                session.run(_NODE_INGEST, {"rows": rows}).consume()
                batches += 1

            for chunk in _chunked(payload.edges, batch_size):
                rows = [{"f": source, "t": target} for source, target in chunk]
                session.run(_EDGE_INGEST, {"rows": rows}).consume()
                batches += 1

        duration_ns = time.perf_counter_ns() - started
        return IngestReport(
            nodes=self.count_nodes(),
            edges=self.count_edges(),
            duration_ns=duration_ns,
            batches=batches,
        )

    def count_nodes(self) -> int:
        return self._scalar("MATCH (p:Paper) RETURN count(p) AS c")

    def count_edges(self) -> int:
        return self._scalar("MATCH ()-[r:CITES]->() RETURN count(r) AS c")

    def _scalar(self, statement: str) -> int:
        with self._session() as session:
            record = session.run(statement).single()
        return int(record[0]) if record is not None else 0

    # -- measurement -------------------------------------------------------

    def run(self, statement: str, params: dict[str, Any]) -> int:
        try:
            with self._session() as session:
                # Parameters go in positionally as a mapping, never as
                # **kwargs: a workload parameter named `timeout` would
                # otherwise be swallowed by the driver as a call option.
                result = session.run(statement, params)
                # Counted by iteration rather than by len(list(result)) so that
                # the rows are consumed without being retained. Materialising a
                # large result would add allocation time to the measurement and
                # would put free-tier memory pressure on the client instead of
                # the server.
                rows = 0
                for _ in result:
                    rows += 1
                return rows
        except Exception as exc:
            raise WorkloadFailure(f"{self.name}: {type(exc).__name__}: {exc}") from exc


def _chunked(items: Any, size: int) -> Any:
    """Yield successive slices of `items`, each at most `size` long."""
    if size < 1:
        raise ValueError("batch size must be at least 1")
    for start in range(0, len(items), size):
        yield items[start : start + size]
