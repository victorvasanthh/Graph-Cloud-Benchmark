"""Wall-clock measurement primitives.

Everything is measured with `time.perf_counter_ns`: it is monotonic, it is the
highest-resolution clock the interpreter exposes, and it is unaffected by NTP
steps that would otherwise show up as a negative or wildly inflated latency in
the middle of a long run.

Timings are stored as integer nanoseconds all the way through the pipeline and
converted to milliseconds only at the reporting boundary. Accumulating floats
across ~10^5 measurements is not a real accuracy problem at these magnitudes,
but integers make the raw JSON byte-identical across machines, which is what
"reproducible" has to mean if it is going to mean anything.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Stopwatch:
    """Accumulates elapsed nanoseconds across repeated `lap()` blocks."""

    laps_ns: list[int] = field(default_factory=list)

    @contextmanager
    def lap(self) -> Iterator[None]:
        start = time.perf_counter_ns()
        try:
            yield
        finally:
            # Recorded in `finally` so a failing query still contributes its
            # duration to the raw log. The runner discards laps belonging to
            # failed iterations; it does not silently keep them.
            self.laps_ns.append(time.perf_counter_ns() - start)

    @property
    def last_ns(self) -> int:
        if not self.laps_ns:
            raise RuntimeError("Stopwatch has recorded no laps")
        return self.laps_ns[-1]

    @property
    def total_ns(self) -> int:
        return sum(self.laps_ns)

    def reset(self) -> None:
        self.laps_ns.clear()


@contextmanager
def timed() -> Iterator[list[int]]:
    """Time a single block; the yielded list holds one element on exit.

    A list is used rather than a scalar because the value is not known until
    the block completes, and a mutable container is the least surprising way
    to hand a not-yet-computed value back to the caller.
    """
    holder: list[int] = []
    start = time.perf_counter_ns()
    try:
        yield holder
    finally:
        holder.append(time.perf_counter_ns() - start)


def ns_to_ms(nanoseconds: float) -> float:
    """Convert to milliseconds. The reporting layer's only unit conversion."""
    return nanoseconds / 1_000_000.0
