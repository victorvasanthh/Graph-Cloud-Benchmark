"""Latency statistics.

Percentiles use the nearest-rank method: the p-th percentile of n sorted
samples is the element at index ceil(p/100 * n) - 1. No interpolation.

That choice is worth stating explicitly because it is the main reason two
honest tools report different p99s for identical data. Nearest-rank always
returns a value that was actually observed, which is the property that matters
when the number is going to be quoted as "the slowest request in a hundred".
Interpolating between two observations invents a latency that no query ever
had.

The corollary is that a p99 needs at least 100 samples to mean anything: with
30 samples, nearest-rank p99 and p95 are both just the maximum. `summarise`
flags that rather than hiding it, so the report can mark the column instead of
presenting a number that is really a max wearing a percentile label.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..core.timing import ns_to_ms


def percentile_ns(samples: Sequence[int], pct: float) -> int:
    """Nearest-rank percentile. `pct` is 0-100."""
    if not samples:
        raise ValueError("cannot take a percentile of an empty sample")
    if not 0 < pct <= 100:
        raise ValueError(f"percentile must be in (0, 100], got {pct}")
    ordered = sorted(samples)
    rank = math.ceil(pct / 100.0 * len(ordered))
    return ordered[max(rank - 1, 0)]


@dataclass
class LatencySummary:
    """Aggregated timings for one (target, workload) pair, in milliseconds."""

    n: int
    min_ms: float
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    mean_ms: float
    stdev_ms: float
    failures: int = 0
    caveats: list[str] = field(default_factory=list)

    @property
    def has_resolvable_p99(self) -> bool:
        return not any(c.startswith("p99") for c in self.caveats)

    def as_row(self) -> dict[str, float | int]:
        return {
            "n": self.n,
            "min_ms": self.min_ms,
            "p50_ms": self.p50_ms,
            "p90_ms": self.p90_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "max_ms": self.max_ms,
            "mean_ms": self.mean_ms,
            "stdev_ms": self.stdev_ms,
            "failures": self.failures,
        }


def summarise(samples_ns: Sequence[int], failures: int = 0) -> LatencySummary:
    """Reduce raw nanosecond timings to the numbers the report prints."""
    if not samples_ns:
        raise ValueError("cannot summarise an empty sample")

    ordered = sorted(samples_ns)
    n = len(ordered)
    caveats: list[str] = []

    # A percentile is only distinguishable from the maximum once the sample is
    # large enough that at least one observation sits above it.
    for label, pct in (("p99", 99.0), ("p95", 95.0), ("p90", 90.0)):
        required = math.ceil(100.0 / (100.0 - pct))
        if n < required:
            caveats.append(
                f"{label} equals the maximum at n={n}; "
                f"{required} or more samples are needed for it to be distinct"
            )

    if failures:
        caveats.append(f"{failures} iteration(s) failed and are excluded from these statistics")

    return LatencySummary(
        n=n,
        min_ms=ns_to_ms(ordered[0]),
        p50_ms=ns_to_ms(percentile_ns(ordered, 50)),
        p90_ms=ns_to_ms(percentile_ns(ordered, 90)),
        p95_ms=ns_to_ms(percentile_ns(ordered, 95)),
        p99_ms=ns_to_ms(percentile_ns(ordered, 99)),
        max_ms=ns_to_ms(ordered[-1]),
        mean_ms=ns_to_ms(statistics.fmean(ordered)),
        stdev_ms=ns_to_ms(statistics.stdev(ordered)) if n > 1 else 0.0,
        failures=failures,
        caveats=caveats,
    )


def throughput_per_second(samples_ns: Sequence[int]) -> float:
    """Sequential throughput implied by the measured latencies.

    This is 1 / mean_latency, not a concurrent throughput measurement. The
    harness issues one query at a time on one connection, so calling this
    number "queries per second" without that qualification would overstate what
    was measured by whatever concurrency factor a reader assumes.
    """
    if not samples_ns:
        return 0.0
    mean_ns = statistics.fmean(samples_ns)
    if mean_ns <= 0:
        return 0.0
    return 1_000_000_000.0 / mean_ns


def relative_to_baseline(value_ms: float, baseline_ms: float) -> float | None:
    """`value` expressed as a multiple of `baseline`; None when undefined."""
    if baseline_ms <= 0:
        return None
    return value_ms / baseline_ms
