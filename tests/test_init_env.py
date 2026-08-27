"""The generated .env must configure exactly the self-hosted targets.

These assertions are about the *contract* between three files that are easy to
drift apart: .env.example declares the variable names, config/databases.yaml
maps them onto targets, and init_env.py writes them. A rename in one without
the others produces a run that silently skips a database.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from init_env import MANAGED, SELF_HOSTED, generate_password  # noqa: E402

from benchmark.core.config import load_config  # noqa: E402

SELF_HOSTED_TARGETS = {"neo4j-selfhosted", "memgraph", "falkordb", "arangodb"}
MANAGED_TARGETS = {"cognodb-cloud", "aura-free"}


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


@pytest.fixture
def generated() -> dict[str, str]:
    return parse_env(SELF_HOSTED.format(neo4j=generate_password(), arango=generate_password()))


class TestGeneratedEnv:
    def test_activates_exactly_the_self_hosted_targets(self, generated):
        config = load_config(REPO_ROOT / "config", environ=generated)
        assert {t.name for t in config.active_targets()} == SELF_HOSTED_TARGETS

    def test_managed_targets_are_skipped_with_named_variables(self, generated):
        config = load_config(REPO_ROOT / "config", environ=generated)
        skipped = {t.name: t.missing for t in config.skipped_targets()}
        assert set(skipped) == MANAGED_TARGETS
        # The report names the variable that was missing, so "not configured"
        # is actionable rather than merely true.
        for missing in skipped.values():
            assert missing

    def test_unauthenticated_engines_have_empty_credentials(self, generated):
        # Memgraph and FalkorDB ship without auth. An empty username makes the
        # Bolt adapter pass auth=None, which is not the same as sending a pair
        # of empty strings.
        assert generated["MEMGRAPH_USERNAME"] == ""
        assert generated["MEMGRAPH_PASSWORD"] == ""
        assert generated["FALKORDB_PASSWORD"] == ""

    def test_authenticated_engines_get_real_passwords(self, generated):
        for key in ("NEO4J_PASSWORD", "ARANGO_PASSWORD"):
            assert len(generated[key]) >= 16, f"{key} is too short to be worth generating"

    def test_passwords_are_shell_and_uri_safe(self, generated):
        # These values land in .env, in docker-compose interpolation and in a
        # Bolt URI. A quote or an @ in any of them breaks one of the three.
        for key in ("NEO4J_PASSWORD", "ARANGO_PASSWORD"):
            assert not set(generated[key]) & set("\"'@:/\\ $`")

    def test_passwords_are_not_reused_between_engines(self, generated):
        assert generated["NEO4J_PASSWORD"] != generated["ARANGO_PASSWORD"]

    def test_each_call_generates_fresh_passwords(self):
        assert generate_password() != generate_password()

    def test_resource_caps_match_the_configured_parity_target(self, generated):
        import yaml

        raw = yaml.safe_load((REPO_ROOT / "config" / "databases.yaml").read_text(encoding="utf-8"))
        capped = [t for t in raw["targets"] if t["tier"] == "self-hosted-capped"]
        # The .env drives docker-compose; databases.yaml is what the report
        # claims. They must not disagree, or the run enforces one thing and
        # publishes another.
        for target in capped:
            assert str(target["resources"]["cpus"]) == generated["CPU_LIMIT"]
            assert f"{target['resources']['memory_gb']}g" == generated["MEMORY_LIMIT"]


class TestManagedBlock:
    def test_managed_block_declares_keys_without_values(self):
        values = parse_env(MANAGED)
        for key, value in values.items():
            if key.endswith(("_PASSWORD", "_URI", "_REGION")):
                assert value == "", f"{key} must ship empty"

    def test_managed_block_covers_every_managed_target_variable(self):
        config = load_config(REPO_ROOT / "config", environ={})
        declared = set(parse_env(MANAGED))
        for target in config.targets:
            if target.name not in MANAGED_TARGETS:
                continue
            for variable in target.missing:
                assert variable in declared, f"{variable} missing from the managed block"


class TestEnvExampleParity:
    def test_every_generated_variable_is_documented_in_env_example(self, generated):
        example = parse_env((REPO_ROOT / ".env.example").read_text(encoding="utf-8"))
        # CPU_LIMIT / MEMORY_LIMIT are container knobs rather than credentials
        # and are documented in infra/docker-compose.yml instead.
        infra_only = {"CPU_LIMIT", "MEMORY_LIMIT"}
        undocumented = set(generated) - set(example) - infra_only
        assert not undocumented, (
            f"init_env writes variables .env.example never mentions: {undocumented}"
        )
