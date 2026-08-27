"""Is a target actually able to answer a query yet?

Readiness lives in the package rather than in a script because two callers
need the identical answer: `scripts/wait_for_target.py`, which gates the
suite, and `scripts/run_benchmark.py`, which must not start measuring a
container that is still booting.

The probe is deliberately the real thing: build the adapter the runner will
use, connect with it, execute a statement. Anything weaker - a TCP connect, a
port check, a container health flag - can pass while the engine is still
refusing queries, and would convert a startup race into a row of measurements
that never happened.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable

from ..core.config import TargetConfig
from ..core.errors import BenchmarkError
from ..databases import build_adapter

#: A statement every engine answers with no data loaded. Deliberately not a
#: count over the dataset: readiness must not depend on a load having happened.
PROBES = {
    "cypher": "RETURN 1 AS ok",
    "cypher_memgraph": "RETURN 1 AS ok",
    "cypher_falkordb": "RETURN 1 AS ok",
    "aql": "RETURN 1",
}

#: Appended when a target never answers. Connection-refused almost always means
#: nothing is listening, and the single most common cause is running a
#: benchmark without starting the container first.
NOT_RUNNING_HINT = (
    "connection refused usually means no container is listening on that port. "
    "`make bench TARGET=...` does not start anything - use `make bench-one "
    "SERVICE=<service> TARGET=<target>`, or `make up SERVICE=<service>` first"
)


def probe_target(target: TargetConfig) -> tuple[bool, str]:
    """One connect-and-query attempt. Returns (ready, detail). Never raises."""
    adapter = build_adapter(target)
    try:
        adapter.connect()
        statement = next((PROBES[d] for d in adapter.dialects if d in PROBES), None)
        if statement is None:
            return False, f"no readiness probe defined for dialects {adapter.dialects}"
        adapter.run(statement, {})
        return True, "connected and answered a trivial query"
    except BenchmarkError as exc:
        return False, str(exc)
    except Exception as exc:  # pragma: no cover - driver-specific surprises
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        # A close that throws must not turn a successful probe into a failure.
        with contextlib.suppress(Exception):
            adapter.close()


def wait_for(
    target: TargetConfig,
    timeout: float = 300.0,
    interval: float = 3.0,
    on_attempt: Callable[[int, str, int], None] | None = None,
) -> tuple[bool, str]:
    """Poll `probe_target` until it succeeds or `timeout` expires.

    Returns (ready, last_detail). The last detail is the useful part of a
    failure: "timed out" on its own sends people to look at the container when
    the driver's message usually names the real problem.
    """
    if timeout <= 0:
        return probe_target(target)

    deadline = time.monotonic() + timeout
    attempts = 0
    detail = "no attempt completed"

    while True:
        attempts += 1
        ready, detail = probe_target(target)
        if ready:
            return True, detail
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if on_attempt is not None:
            on_attempt(attempts, detail, int(remaining))
        time.sleep(min(interval, remaining))

    if "refused" in detail.lower():
        detail = f"{detail}\n  hint: {NOT_RUNNING_HINT}"
    return False, detail
