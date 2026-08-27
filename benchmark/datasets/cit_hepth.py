"""The SNAP cit-HepTh citation network.

Source: https://snap.stanford.edu/data/cit-HepTh.html
27,770 papers (nodes), 352,807 citations (directed edges).

Chosen because it is small enough to load into a free-tier managed instance in
minutes rather than hours, but dense enough that two-hop expansion is real work
rather than a round-trip latency measurement. It is also public and static, so
anyone can reproduce the numbers against the identical input.

Two quirks of the raw files are handled here rather than being pushed into
every workload, because both are easy to get subtly wrong:

1. Node ids are arXiv identifiers with leading zeros stripped by SNAP. Paper
   0001001 (January 2000) appears in the edge list as `1001`. The dates file
   keeps the zero padding, so joining the two requires parsing both sides as
   integers rather than as strings.

2. The dates file marks cross-listed papers with a `11` prefix on the real id,
   as its own header comment says. Those are folded back onto the true id.

Even after both fixes, only about 41% of the graph nodes carry a publication
date: the two files cover overlapping but different paper sets. That is a
property of the source data, not a parsing bug, and it is measured rather than
assumed - see `CitationGraph.date_coverage`. Any workload that filters on date
must therefore restrict itself to nodes that have one, or it will silently
compare engines on an almost-empty result set.
"""

from __future__ import annotations

import gzip
import hashlib
import random
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

EDGES_FILE = "cit-HepTh.txt.gz"
DATES_FILE = "cit-HepTh-dates.txt.gz"

# SHA-256 of the files as published by SNAP, recorded so that a re-download
# that silently returns a different revision is caught before it becomes a
# result nobody can reproduce.
EXPECTED_SHA256 = {
    EDGES_FILE: "7df944b502bd7b687b0e7e94ba9b0a87f202693254558ae5f8ea45657697eb32",
    DATES_FILE: "4b5f2259b0feb848a2a93ab3345d344a4b1c3c9c773ddebcab95f2f6e0bf6225",
}

SOURCE_URLS = {
    EDGES_FILE: "https://snap.stanford.edu/data/cit-HepTh.txt.gz",
    DATES_FILE: "https://snap.stanford.edu/data/cit-HepTh-dates.txt.gz",
}


@dataclass
class DatasetFiles:
    edges: Path
    dates: Path

    @staticmethod
    def in_dir(data_dir: Path | None = None) -> DatasetFiles:
        directory = data_dir or DEFAULT_DATA_DIR
        return DatasetFiles(directory / EDGES_FILE, directory / DATES_FILE)

    def missing(self) -> list[Path]:
        return [p for p in (self.edges, self.dates) if not p.exists()]


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksums(files: DatasetFiles) -> list[str]:
    """Return a list of human-readable mismatch descriptions, empty when clean."""
    problems: list[str] = []
    for path in (files.edges, files.dates):
        expected = EXPECTED_SHA256.get(path.name)
        if expected is None:
            continue
        actual = sha256_of(path)
        if actual != expected:
            problems.append(f"{path.name}: expected sha256 {expected}, found {actual}")
    return problems


def normalise_paper_id(raw: str) -> int:
    """Map a dates-file identifier onto the integer id used in the edge list."""
    if len(raw) > 7 and raw.startswith("11"):
        # Cross-listed marker, per the dates file header.
        return int(raw[2:])
    return int(raw)


@dataclass
class CitationGraph:
    """The parsed graph, plus everything the workloads need to build parameters."""

    edges: list[tuple[int, int]]
    nodes: list[int]
    dates: dict[int, str]
    self_loops: int = 0
    _out: dict[int, list[int]] = field(default_factory=dict, repr=False)
    _undirected: dict[int, list[int]] = field(default_factory=dict, repr=False)

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    @property
    def date_coverage(self) -> float:
        """Fraction of graph nodes carrying a publication date, in [0, 1]."""
        if not self.nodes:
            return 0.0
        return len(self.dated_nodes()) / len(self.nodes)

    def dated_nodes(self) -> list[int]:
        node_set = set(self.nodes)
        return sorted(node_set & set(self.dates))

    def out_degree(self, node: int) -> int:
        return len(self._out.get(node, ()))

    def top_out_degree(self, count: int) -> list[int]:
        """The `count` most-citing papers, ties broken by id for determinism."""
        return [
            node
            for node, _ in sorted(
                ((n, len(adj)) for n, adj in self._out.items()),
                key=lambda pair: (-pair[1], pair[0]),
            )[:count]
        ]

    def sample_nodes(self, count: int, seed: int, dated_only: bool = False) -> list[int]:
        """A reproducible sample of node ids, used as query parameters.

        Sampled from a sorted population with an explicitly seeded Random, so
        the same seed produces the same parameters on every machine and every
        run. Every engine is therefore asked exactly the same questions, which
        is the whole point of controlling the parameter set rather than letting
        each adapter pick its own.
        """
        population = self.dated_nodes() if dated_only else self.nodes
        if not population:
            return []
        rng = random.Random(seed)
        if count >= len(population):
            return list(population)
        return rng.sample(population, count)

    def sample_connected_pairs(
        self,
        count: int,
        seed: int,
        min_hops: int = 3,
        max_hops: int = 6,
        search_budget: int = 4_000,
    ) -> list[tuple[int, int]]:
        """Source/target pairs known to be `min_hops`..`max_hops` apart.

        Shortest-path timings are meaningless if half the pairs are
        unreachable: an engine that gives up quickly on a disconnected pair
        would look fast for doing no work. Pairs are therefore pre-validated
        here with a breadth-first search over the undirected projection, and
        only reachable ones are handed to the workload.

        The undirected projection is deliberate. Citation edges point backwards
        in time, so a directed path between two random papers almost never
        exists, and a directed-only sampler would spend its whole budget
        failing.
        """
        rng = random.Random(seed)
        adjacency = self._undirected_adjacency()
        candidates = [n for n in self.nodes if adjacency.get(n)]
        if not candidates:
            return []

        pairs: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for _ in range(search_budget):
            if len(pairs) >= count:
                break
            source = rng.choice(candidates)
            reachable = self._bfs_at_depth(source, adjacency, min_hops, max_hops)
            if not reachable:
                continue
            target = rng.choice(reachable)
            key = (source, target)
            if source == target or key in seen:
                continue
            seen.add(key)
            pairs.append(key)
        return pairs

    def _undirected_adjacency(self) -> dict[int, list[int]]:
        if not self._undirected:
            adjacency: dict[int, set[int]] = {}
            for source, target in self.edges:
                if source == target:
                    continue
                adjacency.setdefault(source, set()).add(target)
                adjacency.setdefault(target, set()).add(source)
            self._undirected = {node: sorted(peers) for node, peers in adjacency.items()}
        return self._undirected

    @staticmethod
    def _bfs_at_depth(
        source: int,
        adjacency: dict[int, list[int]],
        min_hops: int,
        max_hops: int,
    ) -> list[int]:
        """Nodes whose shortest-path distance from `source` falls in the band."""
        visited = {source}
        frontier = deque([(source, 0)])
        found: list[int] = []
        while frontier:
            node, depth = frontier.popleft()
            if depth >= max_hops:
                continue
            for peer in adjacency.get(node, ()):
                if peer in visited:
                    continue
                visited.add(peer)
                if depth + 1 >= min_hops:
                    found.append(peer)
                frontier.append((peer, depth + 1))
        return found


def load_cit_hepth(
    data_dir: Path | None = None,
    verify: bool = True,
    drop_self_loops: bool = True,
) -> CitationGraph:
    """Parse both gzip files into a `CitationGraph`.

    Self-loops (39 papers cite themselves in this dump, an artefact of the
    arXiv id remapping) are dropped by default. They are counted and reported
    rather than discarded silently, because the edge total is part of what
    makes a run comparable and an unexplained 352,768 would look like a bug.
    """
    files = DatasetFiles.in_dir(data_dir)
    absent = files.missing()
    if absent:
        names = ", ".join(p.name for p in absent)
        raise FileNotFoundError(
            f"dataset file(s) not found: {names}. Run `python scripts/download_data.py` first."
        )

    if verify:
        problems = verify_checksums(files)
        if problems:
            raise ValueError(
                "dataset checksum mismatch, refusing to benchmark against unknown input:\n  "
                + "\n  ".join(problems)
            )

    edges: list[tuple[int, int]] = []
    out_adjacency: dict[int, list[int]] = {}
    node_set: set[int] = set()
    self_loops = 0

    with gzip.open(files.edges, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2:
                continue
            source, target = int(parts[0]), int(parts[1])
            node_set.add(source)
            node_set.add(target)
            if source == target:
                self_loops += 1
                if drop_self_loops:
                    continue
            edges.append((source, target))
            out_adjacency.setdefault(source, []).append(target)

    dates: dict[int, str] = {}
    with gzip.open(files.dates, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2:
                continue
            # Prefixed cross-listings can collide with a plain id. First value
            # wins, which keeps the mapping deterministic under file order.
            dates.setdefault(normalise_paper_id(parts[0]), parts[1])

    return CitationGraph(
        edges=edges,
        nodes=sorted(node_set),
        dates=dates,
        self_loops=self_loops,
        _out=out_adjacency,
    )
