#!/usr/bin/env python3
"""Wait until the benchmark client can actually talk to a target.

    python scripts/wait_for_target.py arangodb --timeout 300

Container health checks are per-image guesswork: one needs curl, another
mgconsole, another redis-cli, and each has its own idea of what "up" means.
The ArangoDB check shipped here was wrong for weeks because an unauthenticated
GET answers 401 and `curl -f` treats that as failure - the server was ready and
the gate said otherwise.

This asks the only question the benchmark cares about: can the adapter the
runner will use connect to this target and execute a trivial statement? That is
engine-agnostic, it is the same code path the measurement uses, and it cannot
drift from the harness because it *is* the harness.

The container health check is still useful - `docker ps` should tell the truth
- but this is what gates a run.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark.core.config import load_config  # noqa: E402
from benchmark.core.errors import BenchmarkError  # noqa: E402
from benchmark.databases import build_adapter  # noqa: E402

#: A statement every engine answers without any data loaded. Deliberately not a
#: count over the dataset: readiness must not depend on a load having happened.
PROBES = {
    "cypher": "RETURN 1 AS ok",
    "cypher_memgraph": "RETURN 1 AS ok",
    "cypher_falkordb": "RETURN 1 AS ok",
    "aql": "RETURN 1",
}


def probe_once(target_name: str, config_dir: Path) -> tuple[bool, str]:
    """One connect-and-query attempt. Returns (ready, detail)."""
    config = load_config(config_dir=config_dir)
    target = next((t for t in config.targets if t.name == target_name), None)
    if target is None:
        known = ", ".join(t.name for t in config.targets)
        raise SystemExit(f"unknown target {target_name!r}; known targets are: {known}")
    if not target.available:
        missing = ", ".join(target.missing) or "disabled in configuration"
        raise SystemExit(f"{target_name} is not configured ({missing})")

    adapter = build_adapter(target)
    try:
        adapter.connect()
        statement = next(
            (PROBES[d] for d in adapter.dialects if d in PROBES),
            None,
        )
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="target name from config/databases.yaml")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--config-dir", type=Path, default=REPO_ROOT / "config")
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    attempts = 0
    last = "no attempt completed"

    while time.monotonic() < deadline:
        attempts += 1
        ready, last = probe_once(args.target, args.config_dir)
        if ready:
            print(f"{args.target}: ready after {attempts} attempt(s) - {last}")
            return 0
        remaining = int(deadline - time.monotonic())
        print(f"  {args.target} not ready ({last}); {remaining}s left", flush=True)
        time.sleep(args.interval)

    # The last error is the useful part. "Timed out" alone sends people to look
    # at the container when the real answer is usually in the driver message.
    print(
        f"{args.target}: NOT ready after {args.timeout:.0f}s and {attempts} attempt(s).\n"
        f"  last error: {last}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
