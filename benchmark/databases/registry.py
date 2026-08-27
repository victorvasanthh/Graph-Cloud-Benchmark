"""Maps the `kind` field in databases.yaml onto an adapter class.

Keyed by protocol rather than by product. CognoDB, Neo4j, Aura and Memgraph
all resolve to `BoltAdapter`, which is deliberate: nothing in this file lets a
particular vendor take a different client-side code path, so no vendor can be
advantaged or disadvantaged by one.
"""

from __future__ import annotations

from ..core.config import TargetConfig
from ..core.errors import ConfigurationError
from .arangodb import ArangoDBAdapter
from .base import GraphAdapter
from .bolt import BoltAdapter
from .falkordb import FalkorDBAdapter

ADAPTERS: dict[str, type[GraphAdapter]] = {
    "bolt": BoltAdapter,
    "falkordb": FalkorDBAdapter,
    "arangodb": ArangoDBAdapter,
}


def build_adapter(target: TargetConfig) -> GraphAdapter:
    try:
        adapter_class = ADAPTERS[target.kind]
    except KeyError as exc:
        raise ConfigurationError(
            f"target {target.name!r} declares kind {target.kind!r}, "
            f"which is not one of {sorted(ADAPTERS)}"
        ) from exc
    return adapter_class(target)
