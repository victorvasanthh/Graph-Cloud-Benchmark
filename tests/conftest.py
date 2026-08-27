"""Shared fixtures.

The test suite runs without any database and without the driver packages
installed: every adapter imports its driver lazily inside `connect`, so the
modules are importable and the harness itself can be tested on its own.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark.datasets.cit_hepth import CitationGraph  # noqa: E402


@pytest.fixture
def toy_graph() -> CitationGraph:
    """A ten-node citation graph small enough to reason about by hand.

    Shaped so that the interesting cases are all present: a hub with several
    outgoing citations, a chain long enough for a three-hop path, an isolated
    pair, and a node with no date.
    """
    edges = [
        (1, 2),
        (1, 3),
        (1, 4),
        (2, 5),
        (3, 5),
        (5, 6),
        (6, 7),
        (7, 8),
        (9, 10),
    ]
    nodes = sorted({n for edge in edges for n in edge})
    dates = {
        1: "1995-01-10",
        2: "1995-06-02",
        3: "1996-02-20",
        5: "1997-03-15",
        6: "1998-08-01",
        9: "2001-05-05",
    }
    out: dict[int, list[int]] = {}
    for source, target in edges:
        out.setdefault(source, []).append(target)
    return CitationGraph(edges=edges, nodes=nodes, dates=dates, self_loops=0, _out=out)


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
