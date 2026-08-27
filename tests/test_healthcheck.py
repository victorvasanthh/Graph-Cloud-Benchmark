"""Regression tests for container readiness.

The ArangoDB health check was wrong in a way that cost a whole smoke run: it
used `curl -sf` against an endpoint that answers 401 without credentials, so
`-f` failed the check forever while the server was up and serving. These tests
pin the specific mistake and the shape of the fix.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def services() -> dict:
    raw = yaml.safe_load((REPO_ROOT / "infra" / "docker-compose.yml").read_text("utf-8"))
    return raw["services"]


def healthcheck_command(service: dict) -> str:
    test = service["healthcheck"]["test"]
    # Compose accepts a string or a list whose first element is CMD/CMD-SHELL.
    return test if isinstance(test, str) else " ".join(test[1:])


class TestArangoHealthcheck:
    @pytest.fixture
    def command(self, services) -> str:
        return healthcheck_command(services["arangodb"])

    def test_does_not_use_curl_fail_flag(self, command):
        # THE BUG. ArangoDB requires auth on every database API, so an
        # unauthenticated GET /_api/version answers 401. `-f` exits non-zero on
        # any status >= 400, so the check could never pass no matter how
        # healthy the server was.
        tokens = shlex.split(command)
        curl_flags = [t for t in tokens if t.startswith("-") and not t.startswith("--")]
        assert "-sf" not in tokens, "curl -sf treats the expected 401 as a failure"
        for flag in curl_flags:
            if flag.startswith("-") and "f" in flag.lstrip("-") and "curl" in command:
                # -f in any combined short flag is the same trap.
                assert flag in {"-s", "-o"}, f"curl flag {flag} reintroduces --fail behaviour"

    def test_has_a_fallback_that_needs_no_external_tool(self, command):
        # If the image ships without curl the check would exit 127 forever.
        # arangosh is part of the distribution, so it is always present.
        assert "arangosh" in command

    def test_password_is_not_interpolated_by_compose(self, command):
        # `$VAR` would be substituted by Compose at parse time, baking the
        # password into the container definition and leaking it to
        # `docker inspect`. `$$VAR` reaches the container shell literally.
        if "ARANGO_ROOT_PASSWORD" in command:
            assert "$$ARANGO_ROOT_PASSWORD" in command, (
                "use $$ so Compose passes the variable through instead of expanding it"
            )

    def test_start_period_allows_for_first_boot(self, services):
        # ArangoDB builds its system collections on first boot, which under a
        # 1 vCPU cap takes materially longer than the other three engines.
        period = services["arangodb"]["healthcheck"]["start_period"]
        assert period.endswith("s")
        assert int(period.rstrip("s")) >= 60


class TestAllHealthchecks:
    def test_every_service_declares_one(self, services):
        for name, service in services.items():
            assert "healthcheck" in service, f"{name} has no healthcheck"

    @pytest.mark.parametrize("name", ["neo4j", "memgraph", "falkordb", "arangodb"])
    def test_no_healthcheck_uses_curl_fail_against_an_authenticated_api(self, services, name):
        command = healthcheck_command(services[name])
        assert "curl -sf" not in command, (
            f"{name}: -f fails on 401/403, which authenticated engines return by default"
        )

    def test_retries_and_interval_cover_the_documented_wait(self, services):
        # run_suite.sh waits 300s. A health check that gives up sooner would
        # mark the container unhealthy while the suite was still waiting.
        for name, service in services.items():
            check = service["healthcheck"]
            interval = int(check["interval"].rstrip("s"))
            retries = int(check["retries"])
            assert interval * retries >= 300, f"{name} gives up before the suite does"


class TestReadinessGate:
    """The suite must not gate on the container health state alone."""

    @pytest.fixture(scope="class")
    def suite(self) -> str:
        return (REPO_ROOT / "scripts" / "run_suite.sh").read_text("utf-8")

    def test_suite_waits_on_the_harness_probe(self, suite):
        assert "wait_for_target.py" in suite, (
            "readiness must be decided by the adapter the runner uses, not by a "
            "per-image health probe that can be wrong in either direction"
        )

    def test_suite_no_longer_gates_on_health_state(self, suite):
        code = [
            line
            for line in suite.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        gating = [
            line
            for line in code
            if "{{.Health}}" in line and ("case" in line or "return 0" in line)
        ]
        assert not gating, f"health state is still deciding whether the run proceeds: {gating}"

    def test_probe_covers_every_dialect_an_adapter_can_declare(self):
        import sys

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from wait_for_target import PROBES

        from benchmark.databases.bolt import FLAVOURS

        declared = {d for flavour in FLAVOURS.values() for d in flavour.dialects}
        declared |= {"cypher_falkordb", "aql"}
        missing = declared - set(PROBES)
        # A dialect with no probe would make the gate fail closed on an engine
        # that is perfectly healthy.
        assert not missing, f"no readiness probe for dialect(s): {missing}"
