"""Managed-target configuration and the guards that protect a free tier.

CognoDB Cloud and Neo4j Aura Free are reached over the internet and are rented,
not owned. Two things follow that do not apply to a container on loopback:
a mistake costs someone else's capacity, and the round trip is part of every
measurement. These tests pin the parts of that which are checkable offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from benchmark.core.config import load_config
from benchmark.runners.runner import _levels_for

REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGED = {"cognodb-cloud", "aura-free"}


@pytest.fixture(scope="module")
def targets() -> dict:
    raw = yaml.safe_load((REPO_ROOT / "config" / "databases.yaml").read_text("utf-8"))
    return {t["name"]: t for t in raw["targets"]}


class TestManagedTargetsAreDeclared:
    @pytest.mark.parametrize("name", sorted(MANAGED))
    def test_target_exists(self, targets, name):
        assert name in targets

    @pytest.mark.parametrize("name", sorted(MANAGED))
    def test_uses_the_shared_bolt_adapter(self, targets, name):
        # Both speak Bolt, so both go through the same driver, session handling
        # and result-consumption path as self-hosted Neo4j. No vendor gets a
        # different client-side code path.
        assert targets[name]["kind"] == "bolt"

    @pytest.mark.parametrize("name", sorted(MANAGED))
    def test_credentials_come_from_the_environment(self, targets, name):
        target = targets[name]
        assert "env" in target, "credentials must be named, never inlined"
        for logical in ("uri", "password"):
            assert logical in target["env"], f"{name} does not map {logical}"
        # Both are required: a managed target reached without a password would
        # fail at connect time with a confusing auth error instead of being
        # reported as "not configured".
        assert set(target["required"]) >= {"uri", "password"}

    @pytest.mark.parametrize("name", sorted(MANAGED))
    def test_no_credential_value_is_committed(self, targets, name):
        rendered = yaml.safe_dump(targets[name])
        for marker in ("password:", "PASSWORD="):
            assert marker not in rendered or "PASSWORD" in rendered, rendered
        # The env block names variables; it must not carry values.
        for value in targets[name]["env"].values():
            assert value.isupper(), f"{value} looks like a value, not a variable name"

    def test_aura_does_not_pin_a_database_name(self, targets):
        # It used to pin `neo4j`, and the live instance rejected that as
        # nonexistent. Guessing a replacement would be the same mistake; with
        # nothing set the driver uses the account's home database, which is
        # right on every Aura tier and needs no knowledge we do not have.
        assert "database" not in targets["aura-free"]["settings"]

    def test_self_hosted_neo4j_still_pins_its_database(self, targets):
        # There the name is ours and is known, so pinning removes a failure
        # mode rather than adding one.
        assert targets["neo4j-selfhosted"]["settings"]["database"] == "neo4j"

    def test_both_are_marked_managed_free(self, targets):
        for name in MANAGED:
            assert targets[name]["tier"] == "managed-free"


class TestUnconfiguredMeansSkipped:
    def test_managed_targets_are_skipped_with_an_empty_environment(self):
        config = load_config(REPO_ROOT / "config", environ={})
        skipped = {t.name for t in config.skipped_targets()}
        assert skipped >= MANAGED

    def test_skip_reason_names_the_missing_variable(self):
        config = load_config(REPO_ROOT / "config", environ={})
        for target in config.targets:
            if target.name in MANAGED:
                # "not configured" is only actionable if it says what to set.
                assert target.missing, f"{target.name} reports no missing variables"

    def test_a_configured_managed_target_becomes_active(self):
        environ = {
            "AURA_URI": "neo4j+s://example-instance.databases.neo4j.io",
            "AURA_PASSWORD": "not-a-real-password",
        }
        config = load_config(REPO_ROOT / "config", environ=environ)
        assert "aura-free" in {t.name for t in config.active_targets()}


class TestConcurrencyCeiling:
    """More clients than requests is not a deeper test, just more connections."""

    def _config(self, iterations: int, levels: list[int]):
        from test_pipeline import make_config

        config = make_config(["point_lookup"], point_lookup={"concurrency": levels})
        config.run.measured_iterations = iterations
        return config

    def test_smoke_collapses_to_a_single_client(self):
        config = self._config(1, [1, 10, 40])
        # A smoke run measures one iteration. Opening forty connections to a
        # rented free tier to issue one query looks like abuse and measures
        # nothing the level-1 run did not.
        assert _levels_for(config.workloads[0], config) == [1]

    def test_full_benchmark_keeps_every_level(self):
        config = self._config(100, [1, 10, 40])
        assert _levels_for(config.workloads[0], config) == [1, 10, 40]

    def test_ceiling_is_the_iteration_count(self):
        config = self._config(10, [1, 10, 40])
        # 40 clients for 10 requests collapses onto 10; the distinct levels
        # below it survive.
        assert _levels_for(config.workloads[0], config) == [1, 10]

    def test_levels_are_deduplicated_after_capping(self):
        config = self._config(5, [10, 20, 40])
        assert _levels_for(config.workloads[0], config) == [5]

    def test_no_config_means_no_capping(self):
        from test_pipeline import make_config

        config = make_config(["point_lookup"], point_lookup={"concurrency": [1, 40]})
        assert _levels_for(config.workloads[0]) == [1, 40]


class TestSuiteFlags:
    @pytest.fixture(scope="class")
    def suite(self) -> str:
        return (REPO_ROOT / "scripts" / "run_suite.sh").read_text("utf-8")

    def test_managed_only_flag_exists(self, suite):
        # Starting four containers to reach two cloud instances wastes ten
        # minutes and proves nothing about either.
        assert "--managed-only" in suite

    def test_self_hosted_only_flag_exists(self, suite):
        assert "--self-hosted-only" in suite

    def test_makefile_exposes_both(self):
        makefile = (REPO_ROOT / "Makefile").read_text("utf-8")
        assert "smoke-managed:" in makefile
        assert "smoke-local:" in makefile
