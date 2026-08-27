#!/usr/bin/env python3
"""Wait until the benchmark client can actually talk to a target.

    python scripts/wait_for_target.py arangodb --timeout 300

A thin CLI over benchmark.runners.readiness, which is where the probe itself
lives so that the runner and this script cannot disagree about what "ready"
means.

Container health checks are per-image guesswork: one needs curl, another
mgconsole, another redis-cli. The ArangoDB one was wrong for weeks because an
unauthenticated request answers 401 and `curl -f` treats that as failure. This
asks the only question the benchmark cares about instead: can the adapter the
runner uses connect and execute a statement?
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark.core.config import load_config  # noqa: E402
from benchmark.runners.readiness import wait_for  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="target name from config/databases.yaml")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--config-dir", type=Path, default=REPO_ROOT / "config")
    args = parser.parse_args()

    config = load_config(config_dir=args.config_dir)
    target = next((t for t in config.targets if t.name == args.target), None)
    if target is None:
        known = ", ".join(t.name for t in config.targets)
        print(f"unknown target {args.target!r}; known targets are: {known}", file=sys.stderr)
        return 2
    if not target.available:
        missing = ", ".join(target.missing) or "disabled in configuration"
        print(f"{args.target} is not configured ({missing})", file=sys.stderr)
        return 2

    def report(attempt: int, detail: str, remaining: int) -> None:
        print(f"  {args.target} not ready ({detail}); {remaining}s left", flush=True)

    ready, detail = wait_for(target, args.timeout, args.interval, report)
    if ready:
        print(f"{args.target}: ready - {detail}")
        return 0

    print(
        f"{args.target}: NOT ready after {args.timeout:.0f}s.\n  last error: {detail}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
