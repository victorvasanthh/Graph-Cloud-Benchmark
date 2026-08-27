"""A wall-clock bound on a single query, independent of the engine.

Native timeouts are better where they exist: FalkorDB and ArangoDB will stop
working when told to, which matters when the engine has one capped vCPU and
every second spent on an abandoned query is a second stolen from the next one.
But native support is uneven - the Bolt driver offers a timeout only on an
explicit transaction, and wrapping these auto-commit statements in one would
add a round trip to every measurement, changing what is measured in order to
bound it.

So this sits on top as a backstop: run the call on a worker thread and stop
waiting when the bound expires. The engine may keep working; we simply stop
pretending we will use the answer.

The abandoned thread is the honest cost of that. It holds a connection that is
now unusable, so a caller that hits a timeout must treat its adapter as spent
and reconnect. `Deadline.expired` says whether that has happened.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from ..core.errors import QueryTimeout


class Deadline:
    """Runs callables under a wall-clock bound. Not thread-safe by design.

    One instance belongs to one (target, workload, concurrency) measurement.
    Reusing an instance across measurements would let a thread abandoned by one
    workload interfere with the next.
    """

    def __init__(self, timeout_s: float | None, grace: float = 5.0) -> None:
        #: None or <= 0 disables bounding entirely, which is what the full
        #: benchmark uses when someone deliberately wants no ceiling.
        self.timeout_s = timeout_s if timeout_s and timeout_s > 0 else None
        #: Added on top of the engine's own timeout before the watchdog fires,
        #: so a server that *is* honouring its native bound gets the chance to
        #: return its own clean error rather than being pre-empted by ours.
        self.grace = grace
        self.expired = False
        self._pool: ThreadPoolExecutor | None = None

    @property
    def enabled(self) -> bool:
        return self.timeout_s is not None

    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gcb-query")
        return self._pool

    def call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Invoke `function`, raising QueryTimeout if the bound expires."""
        if not self.enabled:
            return function(*args, **kwargs)

        future = self._executor().submit(function, *args, **kwargs)
        try:
            return future.result(timeout=self.timeout_s + self.grace)
        except FutureTimeout as exc:
            self.expired = True
            # The worker is still blocked inside the driver, so the pool is
            # abandoned rather than joined: shutdown(wait=True) would hang for
            # exactly as long as the query we just gave up on.
            self._abandon()
            raise QueryTimeout(
                f"no response within {self.timeout_s:.0f}s "
                f"(+{self.grace:.0f}s grace); query abandoned client-side"
            ) from exc

    def _abandon(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        """Release the worker thread if it is idle. Safe to call twice."""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=not self.expired, cancel_futures=True)

    def __enter__(self) -> Deadline:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
