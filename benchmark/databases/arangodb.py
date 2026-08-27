"""Adapter for ArangoDB.

ArangoDB is the one target that is not a Cypher engine at all: it speaks AQL
over HTTP. Workloads therefore carry a hand-written `aql` variant, and the
honest caveat that comes with it is recorded here rather than buried in a
footnote - a translated query is a different query, and no amount of care
makes the comparison as tight as running identical Cypher on two Cypher
engines.

Two modelling choices, both made to avoid handing ArangoDB an artificial
handicap:

  * papers use the paper id as `_key`, so lookups hit the primary index rather
    than a secondary one. That is the idiomatic access path here, and it is
    the fair counterpart to a unique constraint on :Paper(id) in Cypher.
  * an integer `pid` field carries a persistent index alongside it, because
    `_key` is a string and range or ordering predicates on a stringified
    integer would be a modelling error rather than a measurement.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from ..core.config import TargetConfig
from ..core.errors import ConnectionFailure, WorkloadFailure
from .base import GraphAdapter, IngestPayload, IngestReport

PAPERS = "papers"
CITES = "cites"


class ArangoDBAdapter(GraphAdapter):
    """AQL over HTTP, via python-arango."""

    dialects = ("aql",)

    def __init__(self, target: TargetConfig) -> None:
        super().__init__(target)
        self.database_name = self.settings.get("database") or "benchmark"
        self._client: Any = None
        self._db: Any = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        try:
            from arango import ArangoClient
        except ImportError as exc:  # pragma: no cover - install-time failure
            raise ConnectionFailure(
                "python-arango is not installed; run `pip install -r requirements.txt`"
            ) from exc

        url = self.settings.get("url") or "http://localhost:8529"
        username = self.settings.get("username") or "root"
        password = self.settings.get("password") or ""

        try:
            self._client = ArangoClient(hosts=url)
            # The benchmark database is created on demand through _system, so
            # a fresh container needs no manual preparation step that someone
            # reproducing the run could forget to perform.
            system = self._client.db("_system", username=username, password=password)
            if not system.has_database(self.database_name):
                system.create_database(self.database_name)
            self._db = self._client.db(self.database_name, username=username, password=password)
            self._db.properties()
        except Exception as exc:
            self._client = None
            self._db = None
            raise ConnectionFailure(f"{self.name}: could not connect to {url}: {exc}") from exc

    def close(self) -> None:
        self._db = None
        if self._client is not None:
            with contextlib.suppress(Exception):  # closing is best effort
                self._client.close()
            self._client = None

    def _require_db(self) -> Any:
        if self._db is None:
            raise ConnectionFailure(f"{self.name}: connect() has not been called")
        return self._db

    # -- introspection -----------------------------------------------------

    def server_version(self) -> str:
        try:
            return f"ArangoDB {self._require_db().version()}"
        except Exception:
            return "unknown"

    # -- data --------------------------------------------------------------

    def reset(self) -> None:
        db = self._require_db()
        for name in (CITES, PAPERS):
            if db.has_collection(name):
                # Truncated rather than dropped, so the indexes created by
                # prepare_schema survive a reset and every measured run starts
                # from the same schema state.
                db.collection(name).truncate()

    def prepare_schema(self) -> None:
        db = self._require_db()
        if not db.has_collection(PAPERS):
            db.create_collection(PAPERS)
        if not db.has_collection(CITES):
            db.create_collection(CITES, edge=True)
        papers = db.collection(PAPERS)
        existing = {tuple(index.get("fields", ())) for index in papers.indexes()}
        if ("pid",) not in existing:
            papers.add_persistent_index(fields=["pid"], name="paper_pid")

    def schema_is_ready(self) -> bool | None:
        try:
            papers = self._require_db().collection(PAPERS)
            fields = {tuple(index.get("fields", ())) for index in papers.indexes()}
        except Exception:
            return None
        # `_key` carries the primary index and needs no declaration; `pid` is
        # the one this harness has to create.
        return ("pid",) in fields

    def ingest(self, payload: IngestPayload, batch_size: int) -> IngestReport:
        db = self._require_db()
        papers = db.collection(PAPERS)
        cites = db.collection(CITES)
        started = time.perf_counter_ns()
        batches = 0

        for start in range(0, len(payload.nodes), batch_size):
            chunk = payload.nodes[start : start + batch_size]
            papers.import_bulk(
                [
                    {"_key": str(node), "pid": node, "published": payload.dates.get(node)}
                    for node in chunk
                ],
                on_duplicate="error",
            )
            batches += 1

        for start in range(0, len(payload.edges), batch_size):
            chunk = payload.edges[start : start + batch_size]
            cites.import_bulk(
                [
                    {"_from": f"{PAPERS}/{source}", "_to": f"{PAPERS}/{target}"}
                    for source, target in chunk
                ],
                on_duplicate="error",
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
        return int(self._require_db().collection(PAPERS).count())

    def count_edges(self) -> int:
        return int(self._require_db().collection(CITES).count())

    # -- measurement -------------------------------------------------------

    def run(self, statement: str, params: dict[str, Any]) -> int:
        db = self._require_db()
        try:
            cursor = db.aql.execute(statement, bind_vars=params)
            rows = 0
            # The cursor pages lazily over HTTP, so iterating it is what
            # actually fetches the whole result. Reading `cursor.count()`
            # instead would time one page and report it as the full query.
            for _ in cursor:
                rows += 1
            return rows
        except Exception as exc:
            raise WorkloadFailure(f"{self.name}: {type(exc).__name__}: {exc}") from exc
