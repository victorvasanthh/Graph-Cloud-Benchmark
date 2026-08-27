"""Percentile semantics and the honesty flags that come with them."""

from __future__ import annotations

import pytest

from benchmark.metrics.latency import (
    percentile_ns,
    relative_to_baseline,
    summarise,
    throughput_per_second,
)


class TestPercentile:
    def test_nearest_rank_returns_an_observed_value(self):
        samples = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        # Every result must be a member of the sample - never an interpolation
        # between two neighbours, which would report a latency nothing had.
        for pct in (1, 25, 50, 75, 90, 95, 99, 100):
            assert percentile_ns(samples, pct) in samples

    def test_known_ranks(self):
        samples = list(range(1, 101))  # 1..100
        assert percentile_ns(samples, 50) == 50
        assert percentile_ns(samples, 90) == 90
        assert percentile_ns(samples, 99) == 99
        assert percentile_ns(samples, 100) == 100

    def test_p50_of_an_even_sample_does_not_average(self):
        # statistics.median would return 2.5 here. Nearest-rank returns 2.
        assert percentile_ns([1, 2, 3, 4], 50) == 2

    def test_order_does_not_matter(self):
        assert percentile_ns([90, 10, 50, 30, 70], 50) == percentile_ns([10, 30, 50, 70, 90], 50)

    def test_empty_sample_is_an_error_not_a_zero(self):
        with pytest.raises(ValueError):
            percentile_ns([], 50)

    @pytest.mark.parametrize("pct", [0, -1, 101])
    def test_out_of_range_percentile_rejected(self, pct):
        with pytest.raises(ValueError):
            percentile_ns([1, 2, 3], pct)


class TestSummarise:
    def test_small_samples_flag_indistinguishable_percentiles(self):
        summary = summarise([5_000_000] * 30)
        # 30 samples cannot separate p99 from the maximum, and the report must
        # say so rather than printing a number that is really a max.
        assert not summary.has_resolvable_p99
        assert any(c.startswith("p99") for c in summary.caveats)
        # p95 needs only 20 samples, so at n=30 it is genuinely distinct and
        # must not be flagged. Over-flagging would be its own kind of dishonesty.
        assert not any(c.startswith("p95") for c in summary.caveats)

    def test_hundred_samples_resolve_p99(self):
        summary = summarise(list(range(1, 101)))
        assert summary.has_resolvable_p99
        assert not any(c.startswith("p9") for c in summary.caveats)

    def test_conversion_to_milliseconds(self):
        summary = summarise([1_000_000, 2_000_000, 3_000_000])
        assert summary.p50_ms == pytest.approx(2.0)
        assert summary.min_ms == pytest.approx(1.0)
        assert summary.max_ms == pytest.approx(3.0)
        assert summary.mean_ms == pytest.approx(2.0)

    def test_failures_are_carried_into_the_caveats(self):
        summary = summarise(list(range(1, 101)), failures=3)
        assert summary.failures == 3
        assert any("failed" in c for c in summary.caveats)

    def test_single_sample_has_zero_stdev_rather_than_raising(self):
        summary = summarise([1_000_000])
        assert summary.n == 1
        assert summary.stdev_ms == 0.0

    def test_empty_sample_is_an_error(self):
        with pytest.raises(ValueError):
            summarise([])


class TestDerivedNumbers:
    def test_throughput_is_the_reciprocal_of_the_mean(self):
        # 2 ms mean -> 500 sequential queries per second.
        assert throughput_per_second([2_000_000] * 10) == pytest.approx(500.0)

    def test_throughput_of_nothing_is_zero_not_infinite(self):
        assert throughput_per_second([]) == 0.0

    def test_relative_to_baseline(self):
        assert relative_to_baseline(4.0, 2.0) == pytest.approx(2.0)

    def test_relative_to_a_zero_baseline_is_undefined(self):
        # Returning None rather than inf keeps an impossible ratio out of the
        # report instead of printing "infx faster".
        assert relative_to_baseline(4.0, 0.0) is None
