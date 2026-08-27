"""Dataset parsing, id normalisation and parameter sampling."""

from __future__ import annotations

import pytest

from benchmark.datasets.cit_hepth import (
    DatasetFiles,
    load_cit_hepth,
    normalise_paper_id,
)


class TestIdNormalisation:
    def test_plain_id_passes_through(self):
        assert normalise_paper_id("9203201") == 9203201

    def test_zero_padding_is_stripped(self):
        # SNAP strips leading zeros in the edge list but not in the dates file,
        # so paper 0001001 has to reach the same integer from both sides.
        assert normalise_paper_id("0001001") == 1001

    def test_cross_listed_prefix_is_removed(self):
        # The dates file header documents the 11<true_id> marker.
        assert normalise_paper_id("119203001") == 9203001
        assert normalise_paper_id("11101013") == 101013

    def test_seven_char_id_starting_with_eleven_is_not_a_cross_listing(self):
        # A genuine 7-character id beginning "11" must survive intact; only
        # longer ids carry the prefix.
        assert normalise_paper_id("1101013") == 1101013


class TestToyGraph:
    def test_counts(self, toy_graph):
        assert toy_graph.node_count == 10
        assert toy_graph.edge_count == 9

    def test_date_coverage_is_measured_not_assumed(self, toy_graph):
        assert toy_graph.date_coverage == pytest.approx(0.6)
        assert toy_graph.dated_nodes() == [1, 2, 3, 5, 6, 9]

    def test_top_out_degree_is_deterministic_under_ties(self, toy_graph):
        # Nodes 2, 3, 5, 6, 7 and 9 all have out-degree 1. Ties break on id, so
        # repeated calls cannot reorder the parameter list between targets.
        assert toy_graph.top_out_degree(3) == [1, 2, 3]
        assert toy_graph.top_out_degree(3) == toy_graph.top_out_degree(3)

    def test_sample_nodes_is_reproducible_for_a_seed(self, toy_graph):
        first = toy_graph.sample_nodes(4, seed=7)
        second = toy_graph.sample_nodes(4, seed=7)
        assert first == second
        assert len(set(first)) == 4

    def test_sample_nodes_can_restrict_to_dated_nodes(self, toy_graph):
        sample = toy_graph.sample_nodes(3, seed=7, dated_only=True)
        assert set(sample) <= set(toy_graph.dated_nodes())

    def test_sample_nodes_caps_at_population_size(self, toy_graph):
        assert len(toy_graph.sample_nodes(500, seed=1)) == toy_graph.node_count

    def test_connected_pairs_are_actually_connected(self, toy_graph):
        pairs = toy_graph.sample_connected_pairs(5, seed=3, min_hops=2, max_hops=4)
        adjacency = toy_graph._undirected_adjacency()
        for source, target in pairs:
            assert source != target
            reachable = toy_graph._bfs_at_depth(source, adjacency, 2, 4)
            assert target in reachable

    def test_connected_pairs_never_cross_components(self, toy_graph):
        # 9 and 10 form an isolated pair. Nothing in the main component may be
        # paired with them, or the shortest-path workload would be timing a
        # search that cannot succeed.
        pairs = toy_graph.sample_connected_pairs(20, seed=11, min_hops=1, max_hops=6)
        main = {1, 2, 3, 4, 5, 6, 7, 8}
        for source, target in pairs:
            assert (source in main) == (target in main)


@pytest.mark.integration
class TestRealDataset:
    """Runs only when the downloaded files are present."""

    def test_loads_with_the_documented_shape(self):
        if DatasetFiles.in_dir().missing():
            pytest.skip("dataset not downloaded; run scripts/download_data.py")
        graph = load_cit_hepth()
        # The figures SNAP publishes for this dump, minus the self-loops we drop.
        assert graph.node_count == 27_770
        assert graph.self_loops == 39
        assert graph.edge_count == 352_807 - graph.self_loops
        # Recorded as a regression guard: the two source files cover
        # overlapping but different paper sets, and a change here would mean
        # the join broke rather than that the data improved.
        assert 0.40 < graph.date_coverage < 0.42
