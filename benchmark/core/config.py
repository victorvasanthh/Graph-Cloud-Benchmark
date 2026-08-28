"""Configuration loading.

Layout is deliberately three files rather than one, because they change on
different schedules and for different reasons:

  config/benchmark.yaml  how the run is executed (iterations, warmup, seed)
  config/databases.yaml  what is being measured
  config/workloads.yaml  what is being asked of it

No credential ever appears in any of them. The YAML names an environment
variable; the value is read from the process environment (populated from a
gitignored .env). That indirection is why config/ is safe to commit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigurationError

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def load_dotenv_if_present(path: Path | None = None) -> None:
    """Populate os.environ from .env, if python-dotenv and the file are there.

    Kept tolerant of a missing python-dotenv so that the config module stays
    importable in a bare interpreter. The unit tests exercise parsing without
    installing driver dependencies, and a hard import would make that
    impossible for no benefit.
    """
    env_path = path or (REPO_ROOT / ".env")
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - depends on install extras
        return
    load_dotenv(env_path, override=False)


@dataclass
class TargetConfig:
    """One database under measurement."""

    name: str
    kind: str
    display: str
    tier: str
    settings: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    resources: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    #: Why this target has no results, when the reason is not something the
    #: harness can observe. A run filtered with --target reports every other
    #: target as "disabled in configuration", which is true of that invocation
    #: and false about the target; this is where the real reason is recorded.
    absence_note: str = ""

    @property
    def available(self) -> bool:
        """False when a required environment variable was not set.

        An unavailable target is skipped and reported as skipped. It is never
        quietly dropped, because a missing row in a comparison table reads as
        "did not compete" when the truth is "was not configured".
        """
        return self.enabled and not self.missing


@dataclass
class WorkloadConfig:
    name: str
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunConfig:
    warmup_iterations: int = 5
    measured_iterations: int = 30
    seed: int = 20260827
    query_timeout_s: float = 120.0
    ingest_batch_size: int = 5_000
    stop_on_error: bool = False

    def validate(self) -> None:
        if self.measured_iterations < 1:
            raise ConfigurationError("measured_iterations must be at least 1")
        if self.warmup_iterations < 0:
            raise ConfigurationError("warmup_iterations cannot be negative")


@dataclass
class BenchmarkConfig:
    run: RunConfig
    targets: list[TargetConfig]
    workloads: list[WorkloadConfig]
    dataset: dict[str, Any] = field(default_factory=dict)

    def active_targets(self) -> list[TargetConfig]:
        return [t for t in self.targets if t.available]

    def skipped_targets(self) -> list[TargetConfig]:
        return [t for t in self.targets if not t.available]

    def active_workloads(self) -> list[WorkloadConfig]:
        return [w for w in self.workloads if w.enabled]


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"missing configuration file: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path} is not valid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"{path} must contain a mapping at the top level")
    return loaded


def _resolve_target(raw: dict[str, Any], environ: dict[str, str]) -> TargetConfig:
    try:
        name = raw["name"]
        kind = raw["kind"]
    except KeyError as exc:
        raise ConfigurationError(f"target entry is missing required key {exc}") from exc

    env_map: dict[str, str] = raw.get("env", {}) or {}
    required: list[str] = raw.get("required", []) or []
    unknown_required = [key for key in required if key not in env_map]
    if unknown_required:
        raise ConfigurationError(
            f"target {name!r} marks {unknown_required} as required "
            f"but does not map them to environment variables"
        )

    settings: dict[str, str] = {}
    missing: list[str] = []
    for logical, env_var in env_map.items():
        value = environ.get(env_var, "")
        # A required variable that is set-but-empty counts as missing. An empty
        # password is far more likely to be an unfinished .env than a genuine
        # blank credential, and failing here beats a confusing auth error later.
        if logical in required and not value.strip():
            missing.append(env_var)
        settings[logical] = value

    # Non-secret literals declared inline in YAML (ports, database names) are
    # merged after the environment so that config stays readable, while any key
    # also present in the env map keeps whatever the environment supplied.
    for key, value in (raw.get("settings", {}) or {}).items():
        settings.setdefault(key, str(value))

    return TargetConfig(
        name=name,
        kind=kind,
        display=raw.get("display", name),
        tier=raw.get("tier", "unspecified"),
        settings=settings,
        enabled=bool(raw.get("enabled", True)),
        resources=raw.get("resources", {}) or {},
        missing=missing,
        absence_note=str(raw.get("absence_note", "") or ""),
    )


def load_config(
    config_dir: Path | None = None,
    environ: dict[str, str] | None = None,
    only_targets: list[str] | None = None,
    only_workloads: list[str] | None = None,
) -> BenchmarkConfig:
    """Read the three config files and resolve credentials from the environment."""
    directory = config_dir or CONFIG_DIR
    if environ is None:
        load_dotenv_if_present()
        environ = dict(os.environ)

    run_raw = _read_yaml(directory / "benchmark.yaml")
    db_raw = _read_yaml(directory / "databases.yaml")
    wl_raw = _read_yaml(directory / "workloads.yaml")

    known_run_fields = set(RunConfig.__dataclass_fields__)
    run_section = run_raw.get("run", {}) or {}
    unknown = set(run_section) - known_run_fields
    if unknown:
        raise ConfigurationError(
            f"benchmark.yaml: unknown run setting(s) {sorted(unknown)}; "
            f"known settings are {sorted(known_run_fields)}"
        )
    run = RunConfig(**run_section)
    run.validate()

    targets = [_resolve_target(entry, environ) for entry in db_raw.get("targets", []) or []]
    names = [t.name for t in targets]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigurationError(f"databases.yaml declares duplicate target name(s): {duplicates}")

    workloads = [
        WorkloadConfig(
            name=entry["name"],
            enabled=bool(entry.get("enabled", True)),
            params=entry.get("params", {}) or {},
        )
        for entry in wl_raw.get("workloads", []) or []
    ]

    if only_targets:
        requested = set(only_targets)
        unknown_targets = requested - set(names)
        if unknown_targets:
            raise ConfigurationError(
                f"--target named {sorted(unknown_targets)}, which is not in databases.yaml "
                f"(known: {sorted(names)})"
            )
        for target in targets:
            target.enabled = target.enabled and target.name in requested

    if only_workloads:
        requested = set(only_workloads)
        known_workloads = {w.name for w in workloads}
        unknown_workloads = requested - known_workloads
        if unknown_workloads:
            raise ConfigurationError(
                f"--workload named {sorted(unknown_workloads)}, which is not in workloads.yaml "
                f"(known: {sorted(known_workloads)})"
            )
        for workload in workloads:
            workload.enabled = workload.enabled and workload.name in requested

    return BenchmarkConfig(
        run=run,
        targets=targets,
        workloads=workloads,
        dataset=run_raw.get("dataset", {}) or {},
    )
