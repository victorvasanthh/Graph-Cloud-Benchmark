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
from ..core.errors import ConnectionFailure, ConnectionLost, WorkloadFailure
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
    #: Statements that list indexes, tried in order until one is accepted, used
    #: to prove the index really exists. A tuple rather than one string because
    #: an engine whose DDL we had to guess at is also an engine whose
    #: introspection we cannot assume.
    index_probes: tuple[str, ...] = ()
    #: Memgraph rejects `CREATE CONSTRAINT ... IF NOT EXISTS`, and re-running a
    #: constraint that already exists is an error there rather than a no-op.
    #: Schema DDL is therefore allowed to fail without failing the run.
    tolerate_schema_errors: bool = False
    #: Set when the engine cannot chunk a delete server-side. The statement is
    #: run repeatedly, each pass deleting at most `reset_batch_size` nodes and
    #: returning how many it removed, until it removes none. `{batch}` is
    #: substituted with the size, because no engine here accepts a parameter in
    #: a LIMIT clause.
    reset_batch: str = ""
    reset_batch_size: int = 10_000
    #: Run after DDL, best effort. Index creation is asynchronous on several
    #: engines, so a catalogue queried microseconds after CREATE can honestly
    #: report nothing.
    await_statements: tuple[str, ...] = ()
    #: EXPLAIN of the point lookup. Asking the planner whether it will use an
    #: index is a stronger check than asking the catalogue whether one is
    #: listed: it tests the property the benchmark depends on rather than the
    #: bookkeeping around it, and it works even when introspection does not.
    plan_probe: str = ""
    #: Plan operators that mean the lookup resolved through an index.
    plan_index_markers: tuple[str, ...] = ("IndexSeek", "NodeUniqueIndexSeek", "NodeIndexSeek")


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
    index_probes=("SHOW INDEXES YIELD labelsOrTypes, properties RETURN labelsOrTypes, properties",),
    # Neo4j's catalogue is reliable, so these are belt and braces here rather
    # than the primary evidence. They are still declared: a verification that
    # only runs against the engine which failed is a vendor-specific check
    # pretending to be a general one, and it would never catch the same fault
    # arriving somewhere else.
    await_statements=("CALL db.awaitIndexes(60)",),
    plan_probe="EXPLAIN MATCH (p:Paper {id: 1001}) RETURN p",
)

_MEMGRAPH = _Flavour(
    schema=(
        "CREATE INDEX ON :Paper(id)",
        "CREATE CONSTRAINT ON (p:Paper) ASSERT p.id IS UNIQUE",
    ),
    reset=("MATCH (n) DETACH DELETE n",),
    version_probes=("SHOW VERSION",),
    dialects=("cypher_memgraph", "cypher"),
    index_probes=("SHOW INDEX INFO",),
    tolerate_schema_errors=True,
)

_COGNODB = _Flavour(
    # CognoDB speaks Bolt and Cypher, but a narrower Cypher than Neo4j 5. The
    # neo4j flavour's reset failed here with `unexpected token IN` at position
    # 42 - exactly where `CALL { ... } IN TRANSACTIONS` begins. That construct
    # arrived in Neo4j 4.4, so connectivity working says nothing about which
    # language level is implemented.
    #
    # Every alternative spelling of the schema is attempted and failures are
    # tolerated, because we do not know which DDL generation this engine
    # accepts. That would be reckless on its own, so schema_is_ready() confirms
    # afterwards that an index actually exists: guessing is only acceptable
    # when the guess is verified.
    schema=(
        "CREATE CONSTRAINT paper_id IF NOT EXISTS FOR (p:Paper) REQUIRE p.id IS UNIQUE",
        "CREATE CONSTRAINT ON (p:Paper) ASSERT p.id IS UNIQUE",
        "CREATE INDEX ON :Paper(id)",
    ),
    # No server-side chunking available, so the delete is driven from the
    # client. Slower than IN TRANSACTIONS, and it is only setup - never timed,
    # never part of a measurement.
    reset=(),
    reset_batch="MATCH (n) WITH n LIMIT {batch} DETACH DELETE n RETURN count(n) AS deleted",
    reset_batch_size=5_000,
    version_probes=(
        "CALL dbms.components() YIELD name, versions, edition "
        "RETURN name + ' ' + versions[0] + ' (' + edition + ')' AS version",
        "SHOW VERSION",
    ),
    dialects=("cypher_cognodb", "cypher"),
    # SHOW INDEXES came back accepted-but-empty while the constraint DDL
    # reported success, so the catalogue alone cannot settle this. A unique
    # constraint may be listed under constraints rather than indexes, and the
    # backing index may not surface at all, so every spelling is tried.
    index_probes=(
        "SHOW INDEXES YIELD labelsOrTypes, properties RETURN labelsOrTypes, properties",
        "SHOW CONSTRAINTS",
        "SHOW INDEXES",
        "CALL db.indexes()",
        "CALL db.constraints()",
    ),
    await_statements=("CALL db.awaitIndexes(60)", "CALL db.awaitIndexes()"),
    plan_probe="EXPLAIN MATCH (p:Paper {id: 1001}) RETURN p",
    tolerate_schema_errors=True,
)

FLAVOURS: dict[str, _Flavour] = {
    "neo4j": _NEO4J,
    "memgraph": _MEMGRAPH,
    "cognodb": _COGNODB,
}

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


#: Substrings that mean the transport died rather than the query being wrong.
#: Matched against the whole exception chain, because the driver wraps the real
#: cause - a reset, an EOF mid-message - inside a generic failure.
_CONNECTION_LOSS_MARKERS = (
    "defunct",
    "connection reset",
    "broken pipe",
    "serviceunavailable",
    "sessionexpired",
    "connection closed",
    "failed to read",
    "no data",
    "eof",
)


def _is_connection_loss(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = f"{type(current).__name__}: {current}".lower()
        if any(marker in text for marker in _CONNECTION_LOSS_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _plan_operators(plan: dict) -> list[str]:
    """Every operator type in a query plan tree, flattened.

    A label scan followed by a property filter means the lookup is not using an
    index, however cheerfully the catalogue reports one.
    """
    found: list[str] = []
    stack = [plan]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        operator = node.get("operatorType") or node.get("operator_type")
        if operator:
            found.append(str(operator))
        children = node.get("children") or node.get("args", {}).get("children") or []
        if isinstance(children, list):
            stack.extend(children)
    return found


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
        #: (statement, "ok" | error text) for every DDL attempt, kept because a
        #: tolerated failure that is not recorded is indistinguishable from one
        #: that never happened - which is exactly why an unconfirmed index was
        #: undiagnosable.
        self.schema_attempts: list[tuple[str, str]] = []
        #: The same for index introspection.
        self.probe_attempts: list[tuple[str, str]] = []

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
        if self.flavour.reset_batch:
            self._reset_in_batches()
            return
        with self._session() as session:
            for statement in self.flavour.reset:
                session.run(statement).consume()

    def _reset_in_batches(self) -> None:
        """Delete everything from the client, for engines that cannot chunk.

        Neo4j chunks server-side with CALL { ... } IN TRANSACTIONS. CognoDB
        does not implement that construct, so the loop lives here instead:
        delete a bounded slice, ask how many went, repeat until none do.

        The iteration ceiling is a guard against a delete that reports progress
        forever - a bug in the engine or in this statement would otherwise hang
        setup with no diagnosis. It is generous enough that a legitimate reset
        of this dataset never approaches it.
        """
        statement = self.flavour.reset_batch.format(batch=self.flavour.reset_batch_size)
        max_passes = 10_000
        with self._session() as session:
            for _ in range(max_passes):
                record = session.run(statement).single()
                deleted = int(record[0]) if record is not None else 0
                if deleted == 0:
                    return
        raise WorkloadFailure(
            f"{self.name}: batched reset still deleting after {max_passes} passes; "
            f"aborting rather than looping forever"
        )

    def prepare_schema(self) -> None:
        self.schema_attempts = []
        with self._session() as session:
            for statement in self.flavour.schema:
                try:
                    session.run(statement).consume()
                    self.schema_attempts.append((statement, "ok"))
                except Exception as exc:
                    self.schema_attempts.append((statement, f"{type(exc).__name__}: {exc}"))
                    if not self.flavour.tolerate_schema_errors:
                        raise WorkloadFailure(
                            f"{self.name}: schema statement failed: {statement}: {exc}"
                        ) from exc

    def schema_is_ready(self) -> bool | None:
        """Confirm the Paper(id) index, by catalogue and then by query plan.

        CognoDB reported `CREATE CONSTRAINT ... REQUIRE` as successful while
        `SHOW INDEXES` returned zero rows. Both can be true: the constraint may
        be listed under constraints rather than indexes, or its backing index
        may not surface in the catalogue at all.

        So the catalogue is asked in several spellings, and if that is
        inconclusive the planner is asked directly whether a point lookup will
        use an index. The plan is the better evidence: it tests the property
        the benchmark actually depends on rather than the bookkeeping around
        it, and it answers even when introspection does not exist.
        """
        self.probe_attempts = []
        self._await_indexes()

        # Index creation is asynchronous on several engines, so a single
        # immediate query can honestly return nothing. A couple of short
        # retries costs nothing and removes a whole class of false negative.
        for attempt in range(3):
            found = self._probe_catalogue(record=attempt == 0)
            if found is True:
                return True
            if attempt < 2:
                time.sleep(1.0)

        plan_result = self._probe_plan()
        if plan_result is not None:
            return plan_result
        # Catalogue said no or could not answer, and the plan could not be
        # read. "We could not tell" is the honest answer, and it keeps the
        # read numbers marked non-comparable.
        return None

    def _await_indexes(self) -> None:
        for statement in self.flavour.await_statements:
            try:
                with self._session() as session:
                    session.run(statement).consume()
                self.probe_attempts.append((statement, "ok"))
                return
            except Exception as exc:
                self.probe_attempts.append((statement, f"{type(exc).__name__}: {exc}"))

    def _probe_catalogue(self, record: bool) -> bool | None:
        answered = False
        for probe in self.flavour.index_probes:
            try:
                with self._session() as session:
                    rows = [str(row.values()) for row in session.run(probe)]
            except Exception as exc:
                # This spelling was rejected; try the next. The reason is kept,
                # because "we could not ask" and "we asked wrongly" need
                # different fixes.
                if record:
                    self.probe_attempts.append((probe, f"{type(exc).__name__}: {exc}"))
                continue
            answered = True
            if record:
                self.probe_attempts.append((probe, f"ok, {len(rows)} row(s)"))
            # Every catalogue here puts the label and the property somewhere in
            # the row, so matching on both together avoids depending on a
            # column layout that differs between engines and versions.
            if any("Paper" in row and "id" in row for row in rows):
                return True
        return False if answered else None

    def _probe_plan(self) -> bool | None:
        probe = self.flavour.plan_probe
        if not probe:
            return None
        try:
            with self._session() as session:
                plan = session.run(probe).consume().plan
        except Exception as exc:
            self.probe_attempts.append((probe, f"{type(exc).__name__}: {exc}"))
            return None
        if not plan:
            self.probe_attempts.append((probe, "accepted but returned no plan"))
            return None

        operators = _plan_operators(plan)
        indexed = any(
            marker.lower() in operator.lower()
            for operator in operators
            for marker in self.flavour.plan_index_markers
        )
        self.probe_attempts.append(
            (probe, f"plan operators {sorted(set(operators))} -> indexed={indexed}")
        )
        return indexed

    def diagnostics(self) -> dict[str, Any]:
        return {
            "schema_attempts": [
                {"statement": stmt, "outcome": outcome} for stmt, outcome in self.schema_attempts
            ],
            "index_probe_attempts": [
                {"statement": stmt, "outcome": outcome} for stmt, outcome in self.probe_attempts
            ],
        }

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

    def run(self, statement: str, params: dict[str, Any], timeout_s: float | None = None) -> int:
        # `timeout_s` is deliberately ignored here. The driver exposes a
        # timeout only on an explicit transaction, and wrapping these
        # auto-commit statements in one would add a round trip to every
        # measurement - changing what is measured in order to bound it. The
        # runner's wall-clock watchdog bounds this engine instead.
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
            if _is_connection_loss(exc):
                raise ConnectionLost(f"{self.name}: {type(exc).__name__}: {exc}") from exc
            raise WorkloadFailure(f"{self.name}: {type(exc).__name__}: {exc}") from exc


def _chunked(items: Any, size: int) -> Any:
    """Yield successive slices of `items`, each at most `size` long."""
    if size < 1:
        raise ValueError("batch size must be at least 1")
    for start in range(0, len(items), size):
        yield items[start : start + size]
