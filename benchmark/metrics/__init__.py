"""Summary statistics over raw iteration timings."""

from .latency import LatencySummary, percentile_ns, summarise

__all__ = ["LatencySummary", "percentile_ns", "summarise"]
