#!/usr/bin/env python3
"""Run each workload once, in isolation, and report the raw error.

    python scripts/probe_workloads.py cognodb-cloud
    python scripts/probe_workloads.py cognodb-cloud --workload top_cited

Three workloads lost the connection to CognoDB mid-execution while lighter ones
succeeded. A suite run cannot tell you why: the workloads share an adapter, so
a connection killed by one is a connection the next inherits, and the second
failure may be an echo of the first rather than a fact about that workload.

This gives every workload a **fresh connection**, runs it once, and prints the
exception type, the full message, the driver's cause chain, and how long the
call survived before dying. Duration is the discriminator that matters:

  * failed in milliseconds  -> the statement was rejected (syntax, unsupported)
  * failed after N seconds  -> the server gave up (memory, query timeout, an
                               idle proxy) and the workload is too heavy for
                               this tier rather than wrong

It changes nothing, loads nothing, and never prints a credential. It assumes
the data is already loaded; run it after a smoke run rather than instead of one.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark.core.config import load_config  # noqa: E402
from benchmark.databases import build_adapter  # noqa: E402
from benchmark.datasets import load_cit_hepth  # noqa: E402
from benchmark.workloads.base import execution_params  # noqa: E402
from benchmark.workloads.queries import ALL_WORKLOADS  # noqa: E402


def cause_chain(exc: BaseException, limit: int = 4) -> list[str]:
    """The __cause__/__context__ chain, which is where Bolt hides the real reason.

    A `defunct connection` surfaces as a generic driver error whose cause names
    what actually happened - a reset, an EOF mid-message, a closed socket.
    """
    chain: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < limit and id(current) not in seen:
        seen.add(id(current))
        chain.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return chain


def probe(target, workload, params, timeout_s: float | None) -> dict:
    """One workload, one fresh connection, one iteration."""
    adapter = build_adapter(target)
    record: dict = {"workload": workload.name, "ok": False}
    started = time.monotonic()
    try:
        adapter.connect()
        record["connect_s"] = round(time.monotonic() - started, 2)
        statement = adapter.statement_for(workload.dialect_map(params))
        if statement is None:
            record["error"] = "no statement for this engine's dialects"
            return record
        record["statement"] = statement
        query_started = time.monotonic()
        try:
            rows = adapter.run(statement, execution_params(params), timeout_s)
            record["ok"] = True
            record["rows"] = rows
            record["query_s"] = round(time.monotonic() - query_started, 2)
        except Exception as exc:
            record["query_s"] = round(time.monotonic() - query_started, 2)
            record["error"] = cause_chain(exc)
            record["traceback"] = traceback.format_exc(limit=3).strip().splitlines()[-1]
    except Exception as exc:
        record["error"] = cause_chain(exc)
    finally:
        # A close that throws on an already-dead connection must not mask the
        # error we came here to record.
        with contextlib.suppress(Exception):
            adapter.close()
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target")
    parser.add_argument("--workload", action="append", help="limit to these; repeatable")
    parser.add_argument("--timeout", type=float, default=120.0, help="0 disables")
    parser.add_argument("--config-dir", type=Path, default=REPO_ROOT / "config")
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    args = parser.parse_args()

    config = load_config(config_dir=args.config_dir)
    target = next((t for t in config.targets if t.name == args.target), None)
    if target is None:
        print(f"unknown target {args.target!r}", file=sys.stderr)
        return 2
    if not target.available:
        print(f"{args.target} is not configured ({', '.join(target.missing)})", file=sys.stderr)
        return 2

    graph = load_cit_hepth(data_dir=args.data_dir)
    chosen = [w for w in ALL_WORKLOADS if not args.workload or w.name in args.workload]
    timeout_s = args.timeout if args.timeout > 0 else None

    print(f"probing {args.target}: {len(chosen)} workload(s), a fresh connection for each")
    print(f"timeout {args.timeout:.0f}s\n")

    failures = 0
    for workload in chosen:
        # The same seeded parameters the benchmark would use, so a failure here
        # is the failure the benchmark saw and not a different question.
        params = workload.parameters_for(graph, {}, 1, config.run.seed)
        if not params:
            print(f"  {workload.name:<20} skipped: no parameters could be generated")
            continue

        record = probe(target, workload, params[0], timeout_s)
        if record["ok"]:
            print(f"  {workload.name:<20} ok    {record['query_s']:>7.2f}s  {record['rows']} rows")
            continue

        failures += 1
        print(f"  {workload.name:<20} FAIL  {record.get('query_s', 0):>7.2f}s")
        for line in record.get("error", ["no detail"]):
            print(f"      {line}")
        if record.get("statement"):
            print(f"      sent: {record['statement'][:150]}")
        print()

    print(f"\n{len(chosen) - failures} ok, {failures} failed")
    if failures:
        print(
            "\nRead the durations. A failure in milliseconds is the statement being\n"
            "rejected; a failure after seconds is the server giving up, which is a\n"
            "property of the tier rather than of the query."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
