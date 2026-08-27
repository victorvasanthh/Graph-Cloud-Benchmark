#!/usr/bin/env python3
"""Print what this checkout will actually run, and what is actually running.

    python scripts/show_effective_config.py      # or: make verify-config

Written after a smoke run reported failures that had already been fixed. The
fixes were in a working tree that had never been committed, so the Codespace
was faithfully executing older code while the report described newer code.
Nothing in the benchmark output could have revealed that.

So this reads the three things that can silently disagree:

  1. the commit this checkout is on, and whether it matches the remote;
  2. the image tag each service is pinned to in docker-compose.yml, and the
     tag of the container actually running, if any;
  3. the query text each engine will really be handed, resolved through the
     same dialect chain the runner uses.

It changes nothing and starts nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from benchmark.core.config import TargetConfig  # noqa: E402
from benchmark.core.worktree import ADVICE, blocking, group, read_status  # noqa: E402
from benchmark.databases.arangodb import ArangoDBAdapter  # noqa: E402
from benchmark.databases.bolt import BoltAdapter  # noqa: E402
from benchmark.databases.falkordb import FalkorDBAdapter  # noqa: E402
from benchmark.workloads.queries import BY_NAME  # noqa: E402

#: compose service -> the adapter the runner will build for it.
ENGINES = {
    "neo4j": ("neo4j-selfhosted", BoltAdapter, {"flavour": "neo4j", "uri": "bolt://x"}),
    "memgraph": ("memgraph", BoltAdapter, {"flavour": "memgraph", "uri": "bolt://x"}),
    "falkordb": ("falkordb", FalkorDBAdapter, {}),
    "arangodb": ("arangodb", ArangoDBAdapter, {}),
}


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, cwd=REPO_ROOT, check=False
        )
    except OSError:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _docker(*args: str) -> str:
    if shutil.which("docker") is None:
        return ""
    try:
        out = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def report_commit() -> list[str]:
    findings: list[str] = []
    head = _git("rev-parse", "--short", "HEAD")
    subject = _git("log", "-1", "--pretty=%s")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    print("== checkout ==")
    print(f"  branch:  {branch or 'unknown'}")
    print(f"  commit:  {head or 'unknown'}  {subject}")

    entries = read_status(REPO_ROOT)
    if entries:
        print(f"  working tree: {len(entries)} uncommitted change(s)")
        # A bare count invites the reaction that loses work: delete everything.
        # Say what each one is instead.
        for category, items in sorted(group(entries).items()):
            print()
            print(f"    {category}: {len(items)} - {ADVICE[category]}")
            for entry in items[:12]:
                print(f"      {entry.status} {entry.path}")
            if len(items) > 12:
                print(f"      ... and {len(items) - 12} more")

        blockers = blocking(entries)
        if blockers:
            findings.append(
                f"{len(blockers)} change(s) need a decision before a benchmark is "
                f"reproducible; see the breakdown above"
            )
        else:
            print()
            print("    none of these block a run")
    else:
        print("  working tree: clean")

    _git("fetch", "--quiet", "origin")
    behind = _git("rev-list", "--count", "HEAD..@{upstream}")
    ahead = _git("rev-list", "--count", "@{upstream}..HEAD")
    if behind and behind != "0":
        print(f"  behind remote by {behind} commit(s)")
        findings.append(f"this checkout is {behind} commit(s) behind the remote; run `git pull`")
    elif ahead and ahead != "0":
        print(f"  ahead of remote by {ahead} commit(s)")
        findings.append(f"{ahead} commit(s) are not pushed; another machine will not see them")
    elif behind == "0":
        print("  in sync with remote")
    return findings


def report_images() -> list[str]:
    findings: list[str] = []
    compose = yaml.safe_load((REPO_ROOT / "infra" / "docker-compose.yml").read_text("utf-8"))
    print("\n== images ==")
    for service, spec in compose["services"].items():
        pinned = spec["image"]
        container = f"gcb-{service}"
        running = _docker("inspect", container, "--format", "{{json .Config.Image}}")
        if running:
            actual = json.loads(running)
            state = _docker("inspect", container, "--format", "{{.State.Status}}")
            marker = "  <-- MISMATCH" if actual != pinned else ""
            print(f"  {service:<10} pinned {pinned}")
            print(f"  {'':<10} running {actual} ({state}){marker}")
            if actual != pinned:
                findings.append(
                    f"{container} is running {actual} but the compose file pins {pinned}; "
                    f"remove the container so it is recreated from the pinned image"
                )
        else:
            print(f"  {service:<10} pinned {pinned}  (no container running)")
    return findings


def report_queries() -> list[str]:
    print("\n== resolved query text, per engine ==")
    workload = BY_NAME["shortest_path"]
    for service, (target_name, cls, settings) in ENGINES.items():
        adapter = cls(
            TargetConfig(
                name=target_name, kind=service, display=target_name, tier="t", settings=settings
            )
        )
        text = adapter.statement_for(workload.statements)
        print(f"\n  {target_name}  dialects={adapter.dialects}")
        print(f"    {text}")
    return []


def main() -> int:
    findings = report_commit() + report_images() + report_queries()

    print("\n== findings ==")
    if not findings:
        print("  none: this checkout, its pins, and the running containers agree")
        return 0
    for finding in findings:
        print(f"  ! {finding}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
