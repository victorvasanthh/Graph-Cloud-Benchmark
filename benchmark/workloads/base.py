"""The workload contract.

A workload is a question, expressed once per query dialect, plus a rule for
generating the parameters it is asked with.

Parameters are generated **once**, up front, from a seeded random number
generator, and the identical list is replayed against every target in the same
order. This is the single most important fairness property in the harness: if
each engine drew its own random paper ids, one of them would eventually draw
an easier sample and the difference would show up as a performance result.

`equivalence` records how closely the dialect variants really match. It is
carried into the report rather than left as a comment, because "these two
queries do the same thing" is a claim that deserves to travel next to the
number it produced.

Most workloads issue one statement. A mixed read/write workload issues a
different statement depending on the iteration, so a workload may instead
declare `variants`: a mapping of operation name to per-dialect text. The
operation for each iteration is chosen when the parameters are generated, so
every engine performs the same operation in the same order - a mix that
differed per engine would not be a mix, it would be a different benchmark per
target.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..datasets.cit_hepth import CitationGraph

#: How comparable the per-dialect statements are to one another.
#:   identical  - the same text runs on every engine that reached this workload
#:   idiomatic  - a faithful translation using each engine's natural construct
#:   loose      - the engines are asked for the same answer but are free to
#:                reach it by materially different means; read with care
Equivalence = str

ParamBuilder = Callable[[CitationGraph, dict[str, Any], int, int], list[dict[str, Any]]]

#: Parameter keys beginning with this prefix steer the harness and are stripped
#: before the statement is executed. ArangoDB rejects a query outright when a
#: bind parameter is declared but unused, so passing control keys through to the
#: driver would fail one engine and not the others.
CONTROL_PREFIX = "__"

#: Key naming the operation for a workload that declares `variants`.
OP_KEY = "__op__"


def execution_params(params: dict[str, Any]) -> dict[str, Any]:
    """The parameters actually sent to the driver, control keys removed."""
    return {k: v for k, v in params.items() if not k.startswith(CONTROL_PREFIX)}


@dataclass(frozen=True)
class Workload:
    """One measured question."""

    name: str
    description: str
    build_params: ParamBuilder
    statements: dict[str, str] = field(default_factory=dict)
    variants: dict[str, dict[str, str]] = field(default_factory=dict)
    equivalence: Equivalence = "identical"
    equivalence_note: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)
    #: True when iterations mutate the database. Such a workload is scheduled
    #: last and excluded from the cross-engine row-count check, because its
    #: result depends on how many writes have already landed.
    mutates: bool = False

    def __post_init__(self) -> None:
        if not self.statements and not self.variants:
            raise ValueError(f"workload {self.name!r} declares neither statements nor variants")

    def dialect_map(self, params: dict[str, Any]) -> dict[str, str]:
        """The dialect-to-text mapping this iteration should draw from."""
        if not self.variants:
            return self.statements
        op = params.get(OP_KEY)
        try:
            return self.variants[op]
        except KeyError as exc:
            raise KeyError(
                f"workload {self.name!r} has no variant for operation {op!r}; "
                f"known operations are {sorted(self.variants)}"
            ) from exc

    def supported_by(self, dialects: Sequence[str]) -> bool:
        """True when every variant has text for at least one of `dialects`."""
        maps = list(self.variants.values()) or [self.statements]
        return all(any(d in mapping for d in dialects) for mapping in maps)

    def parameters_for(
        self,
        graph: CitationGraph,
        overrides: dict[str, Any],
        count: int,
        seed: int,
    ) -> list[dict[str, Any]]:
        """Build the parameter list replayed against every target."""
        params = {**self.defaults, **overrides}
        return self.build_params(graph, params, count, seed)


def cycle_to_length(items: Sequence[Any], count: int) -> list[Any]:
    """Repeat `items` until there are `count` of them.

    Iteration counts routinely exceed the number of distinct parameters worth
    sampling. Cycling rather than resampling keeps every engine on exactly the
    same repetition pattern, so a cache that warms on the eleventh repeat warms
    at the same point for all of them.
    """
    if not items:
        return []
    repeated: list[Any] = []
    while len(repeated) < count:
        repeated.extend(items)
    return repeated[:count]
