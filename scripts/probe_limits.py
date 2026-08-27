#!/usr/bin/env python3
"""Measure the resources actually available, and compare them with the config.

    python scripts/probe_limits.py              # host + any running containers
    python scripts/probe_limits.py --json       # machine-readable

Run this before the benchmark and keep the output. `config/databases.yaml`
records the parity target the harness was *written* against; this script
reports what the kernel is *enforcing*. A resource-parity benchmark whose
parity was never verified is a benchmark with an unchecked assumption at the
centre of it, and the two numbers disagreeing is exactly the kind of thing
that invalidates a published comparison after the fact.

Everything here is read-only: cgroup files, /proc, and `docker inspect`.
Nothing is started, stopped or modified.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

CGROUP_V2 = Path("/sys/fs/cgroup")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def host_limits() -> dict[str, Any]:
    """What this machine actually offers, cgroup caps included.

    A Codespace is itself a container, so `nproc` and /proc/meminfo report the
    underlying node rather than the slice we are allowed to use. The cgroup
    values are the ones that bind, and where they disagree the cgroup wins.
    """
    info: dict[str, Any] = {}

    try:
        import os

        info["cpu_count_visible"] = os.cpu_count()
        info["cpu_affinity"] = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        info["cpu_affinity"] = None

    cpu_max = _read(CGROUP_V2 / "cpu.max")
    if cpu_max:
        quota, _, period = cpu_max.partition(" ")
        if quota == "max":
            info["cgroup_cpu_limit"] = None
        else:
            info["cgroup_cpu_limit"] = round(int(quota) / int(period or 100000), 3)

    mem_max = _read(CGROUP_V2 / "memory.max")
    if mem_max and mem_max != "max":
        info["cgroup_memory_gb"] = round(int(mem_max) / 1024**3, 2)

    meminfo = _read(Path("/proc/meminfo")) or ""
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            info["meminfo_total_gb"] = round(int(line.split()[1]) / 1024**2, 2)
            break

    usage = shutil.disk_usage(str(REPO_ROOT))
    info["disk_total_gb"] = round(usage.total / 1024**3, 2)
    info["disk_free_gb"] = round(usage.free / 1024**3, 2)

    info["effective_cpus"] = info.get("cgroup_cpu_limit") or info.get("cpu_affinity")
    info["effective_memory_gb"] = info.get("cgroup_memory_gb") or info.get("meminfo_total_gb")
    return info


def _docker(*args: str) -> str | None:
    if shutil.which("docker") is None:
        return None
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def container_limits() -> dict[str, dict[str, Any]]:
    """Enforced CPU and memory caps for every running benchmark container."""
    names = _docker("ps", "--filter", "name=gcb-", "--format", "{{.Names}}")
    if not names:
        return {}

    found: dict[str, dict[str, Any]] = {}
    for name in names.splitlines():
        name = name.strip()
        if not name:
            continue
        raw = _docker("inspect", name, "--format", "{{json .HostConfig}}")
        entry: dict[str, Any] = {"running": True}
        if raw:
            host_config = json.loads(raw)
            nano = host_config.get("NanoCpus") or 0
            quota = host_config.get("CpuQuota") or 0
            period = host_config.get("CpuPeriod") or 100000
            if nano:
                entry["cpus"] = round(nano / 1e9, 3)
            elif quota:
                entry["cpus"] = round(quota / period, 3)
            else:
                entry["cpus"] = None
            memory = host_config.get("Memory") or 0
            entry["memory_gb"] = round(memory / 1024**3, 2) if memory else None

        # The cgroup file inside the container is the authority: it is what the
        # kernel enforces, whatever the daemon was asked for.
        cgroup_cpu = _docker("exec", name, "cat", "/sys/fs/cgroup/cpu.max")
        if cgroup_cpu:
            quota_s, _, period_s = cgroup_cpu.partition(" ")
            entry["enforced_cpus"] = (
                None if quota_s == "max" else round(int(quota_s) / int(period_s or 100000), 3)
            )
        cgroup_mem = _docker("exec", name, "cat", "/sys/fs/cgroup/memory.max")
        if cgroup_mem:
            entry["enforced_memory_gb"] = (
                None if cgroup_mem == "max" else round(int(cgroup_mem) / 1024**3, 2)
            )
        found[name] = entry
    return found


def configured_targets() -> list[dict[str, Any]]:
    raw = yaml.safe_load((REPO_ROOT / "config" / "databases.yaml").read_text(encoding="utf-8"))
    return [t for t in raw.get("targets", []) if t.get("tier") == "self-hosted-capped"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = parser.parse_args()

    host = host_limits()
    containers = container_limits()
    targets = configured_targets()

    total_cpu = sum(t.get("resources", {}).get("cpus", 0) for t in targets)
    total_mem = sum(t.get("resources", {}).get("memory_gb", 0) for t in targets)

    findings: list[str] = []
    effective_cpus = host.get("effective_cpus")
    effective_mem = host.get("effective_memory_gb")

    if effective_cpus and total_cpu > effective_cpus:
        findings.append(
            f"Running all {len(targets)} self-hosted targets at once needs {total_cpu} vCPU "
            f"but this machine provides {effective_cpus}. They must be measured one at a "
            f"time, which is also the fairer arrangement."
        )
    if effective_mem and total_mem >= effective_mem * 0.9:
        findings.append(
            f"The configured memory caps total {total_mem} GB against {effective_mem} GB "
            f"available, leaving nothing for the OS, the client, or the dataset in memory. "
            f"Measure one target at a time."
        )

    for name, actual in containers.items():
        target = next((t for t in targets if name.endswith(t["name"].split("-")[0])), None)
        if target is None:
            continue
        want_cpu = target.get("resources", {}).get("cpus")
        want_mem = target.get("resources", {}).get("memory_gb")
        got_cpu = actual.get("enforced_cpus") or actual.get("cpus")
        got_mem = actual.get("enforced_memory_gb") or actual.get("memory_gb")
        if got_cpu is None:
            findings.append(f"{name}: no CPU limit is being enforced (config asks for {want_cpu})")
        elif want_cpu and abs(got_cpu - want_cpu) > 0.05:
            findings.append(f"{name}: CPU limit is {got_cpu}, config declares {want_cpu}")
        if got_mem is None:
            findings.append(
                f"{name}: no memory limit is being enforced (config asks for {want_mem} GB)"
            )
        elif want_mem and abs(got_mem - want_mem) > 0.15:
            findings.append(f"{name}: memory limit is {got_mem} GB, config declares {want_mem} GB")

    payload = {
        "host": host,
        "containers": containers,
        "configured_total_cpus": total_cpu,
        "configured_total_memory_gb": total_mem,
        "findings": findings,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if findings else 0

    print("== host ==")
    print(f"  effective vCPU:    {host.get('effective_cpus')}")
    print(f"  effective memory:  {host.get('effective_memory_gb')} GB")
    print(f"  disk free:         {host.get('disk_free_gb')} GB of {host.get('disk_total_gb')} GB")
    print()
    print("== configured self-hosted caps ==")
    for target in targets:
        resources = target.get("resources", {})
        print(
            f"  {target['name']:<20} {resources.get('cpus')} vCPU, {resources.get('memory_gb')} GB"
        )
    print(f"  {'TOTAL':<20} {total_cpu} vCPU, {total_mem} GB")
    print()
    print("== running containers ==")
    if not containers:
        print("  none (start one with: docker compose -f infra/docker-compose.yml up -d <service>)")
    for name, actual in sorted(containers.items()):
        print(
            f"  {name:<20} enforced {actual.get('enforced_cpus')} vCPU, "
            f"{actual.get('enforced_memory_gb')} GB"
        )
    print()
    if findings:
        print("== findings ==")
        for finding in findings:
            print(f"  ! {finding}")
        return 1
    print("== findings ==")
    print("  none: enforced limits match the configuration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
