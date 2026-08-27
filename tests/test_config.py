"""Configuration loading, credential resolution and the skip semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmark.core.config import load_config
from benchmark.core.errors import ConfigurationError

BENCHMARK_YAML = """
run:
  warmup_iterations: 2
  measured_iterations: 10
  seed: 99
"""

DATABASES_YAML = """
targets:
  - name: alpha
    kind: bolt
    display: Alpha
    tier: managed-free
    env:
      uri: ALPHA_URI
      password: ALPHA_PASSWORD
    required: [uri, password]
    settings:
      flavour: neo4j
  - name: beta
    kind: falkordb
    display: Beta
    tier: self-hosted-capped
    env:
      host: BETA_HOST
    required: [host]
"""

WORKLOADS_YAML = """
workloads:
  - name: point_lookup
    params:
      distinct_parameters: 10
  - name: two_hop
    enabled: false
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "benchmark.yaml").write_text(BENCHMARK_YAML, encoding="utf-8")
    (tmp_path / "databases.yaml").write_text(DATABASES_YAML, encoding="utf-8")
    (tmp_path / "workloads.yaml").write_text(WORKLOADS_YAML, encoding="utf-8")
    return tmp_path


FULL_ENV = {
    "ALPHA_URI": "bolt+s://alpha.example",
    "ALPHA_PASSWORD": "secret",
    "BETA_HOST": "localhost",
}


class TestLoading:
    def test_run_settings_come_from_yaml(self, config_dir):
        config = load_config(config_dir, environ=dict(FULL_ENV))
        assert config.run.measured_iterations == 10
        assert config.run.warmup_iterations == 2
        assert config.run.seed == 99

    def test_credentials_are_read_from_the_environment(self, config_dir):
        config = load_config(config_dir, environ=dict(FULL_ENV))
        alpha = next(t for t in config.targets if t.name == "alpha")
        assert alpha.settings["uri"] == "bolt+s://alpha.example"
        assert alpha.settings["password"] == "secret"

    def test_inline_settings_merge_without_overriding_the_environment(self, config_dir):
        config = load_config(config_dir, environ=dict(FULL_ENV))
        alpha = next(t for t in config.targets if t.name == "alpha")
        assert alpha.settings["flavour"] == "neo4j"

    def test_disabled_workloads_are_excluded(self, config_dir):
        config = load_config(config_dir, environ=dict(FULL_ENV))
        assert [w.name for w in config.active_workloads()] == ["point_lookup"]


class TestSkipSemantics:
    def test_missing_credential_marks_the_target_unavailable(self, config_dir):
        environ = dict(FULL_ENV)
        del environ["ALPHA_PASSWORD"]
        config = load_config(config_dir, environ=environ)
        alpha = next(t for t in config.targets if t.name == "alpha")
        assert not alpha.available
        assert alpha.missing == ["ALPHA_PASSWORD"]
        # The target stays in the list so the report can say "not configured"
        # rather than leaving a blank the reader will misread as a pass.
        assert alpha in config.skipped_targets()
        assert alpha not in config.active_targets()

    def test_a_blank_required_value_counts_as_missing(self, config_dir):
        environ = dict(FULL_ENV, ALPHA_PASSWORD="   ")
        config = load_config(config_dir, environ=environ)
        alpha = next(t for t in config.targets if t.name == "alpha")
        assert alpha.missing == ["ALPHA_PASSWORD"]

    def test_optional_settings_may_be_absent(self, config_dir):
        # Beta requires only BETA_HOST; no password is configured for it and
        # that must not disqualify it.
        config = load_config(config_dir, environ=dict(FULL_ENV))
        beta = next(t for t in config.targets if t.name == "beta")
        assert beta.available


class TestFilters:
    def test_only_targets_narrows_the_run(self, config_dir):
        config = load_config(config_dir, environ=dict(FULL_ENV), only_targets=["beta"])
        assert [t.name for t in config.active_targets()] == ["beta"]

    def test_unknown_target_is_rejected_rather_than_ignored(self, config_dir):
        # Silently running nothing because of a typo is how a run gets reported
        # as clean when it never happened.
        with pytest.raises(ConfigurationError, match="not in databases.yaml"):
            load_config(config_dir, environ=dict(FULL_ENV), only_targets=["gamma"])

    def test_unknown_workload_is_rejected(self, config_dir):
        with pytest.raises(ConfigurationError, match="not in workloads.yaml"):
            load_config(config_dir, environ=dict(FULL_ENV), only_workloads=["nope"])


class TestValidation:
    def test_missing_file_is_reported_by_path(self, tmp_path):
        with pytest.raises(ConfigurationError, match="missing configuration file"):
            load_config(tmp_path, environ={})

    def test_unknown_run_setting_is_rejected(self, tmp_path):
        (tmp_path / "benchmark.yaml").write_text("run:\n  iterations: 10\n", encoding="utf-8")
        (tmp_path / "databases.yaml").write_text(DATABASES_YAML, encoding="utf-8")
        (tmp_path / "workloads.yaml").write_text(WORKLOADS_YAML, encoding="utf-8")
        # A misspelled knob that silently does nothing would mean running a
        # different experiment than the one written down.
        with pytest.raises(ConfigurationError, match="unknown run setting"):
            load_config(tmp_path, environ=dict(FULL_ENV))

    def test_duplicate_target_names_are_rejected(self, tmp_path):
        (tmp_path / "benchmark.yaml").write_text(BENCHMARK_YAML, encoding="utf-8")
        (tmp_path / "databases.yaml").write_text(
            DATABASES_YAML + "\n  - name: alpha\n    kind: bolt\n", encoding="utf-8"
        )
        (tmp_path / "workloads.yaml").write_text(WORKLOADS_YAML, encoding="utf-8")
        with pytest.raises(ConfigurationError, match="duplicate target"):
            load_config(tmp_path, environ=dict(FULL_ENV))

    def test_required_key_must_be_mapped_to_an_env_var(self, tmp_path):
        (tmp_path / "benchmark.yaml").write_text(BENCHMARK_YAML, encoding="utf-8")
        (tmp_path / "databases.yaml").write_text(
            "targets:\n"
            "  - name: alpha\n"
            "    kind: bolt\n"
            "    env:\n"
            "      uri: ALPHA_URI\n"
            "    required: [uri, password]\n",
            encoding="utf-8",
        )
        (tmp_path / "workloads.yaml").write_text(WORKLOADS_YAML, encoding="utf-8")
        with pytest.raises(ConfigurationError, match="does not map them"):
            load_config(tmp_path, environ=dict(FULL_ENV))


class TestShippedConfig:
    """The committed config must load and stay in step with the code."""

    def test_repository_config_is_valid(self, repo_root):
        config = load_config(repo_root / "config", environ={})
        assert config.targets, "databases.yaml declares no targets"
        assert config.workloads, "workloads.yaml declares no workloads"

    def test_every_configured_workload_is_implemented(self, repo_root):
        from benchmark.workloads.queries import BY_NAME

        config = load_config(repo_root / "config", environ={})
        unknown = [w.name for w in config.workloads if w.name not in BY_NAME]
        assert not unknown, f"workloads.yaml names unimplemented workloads: {unknown}"

    def test_every_target_kind_has_an_adapter(self, repo_root):
        from benchmark.databases import ADAPTERS

        config = load_config(repo_root / "config", environ={})
        unknown = [t.kind for t in config.targets if t.kind not in ADAPTERS]
        assert not unknown, f"databases.yaml names unknown adapter kinds: {unknown}"

    def test_no_target_is_runnable_without_an_environment(self, repo_root):
        # Guards against a credential being accidentally committed as an
        # inline default: with an empty environment, nothing may be available.
        config = load_config(repo_root / "config", environ={})
        assert config.active_targets() == []
