"""The devcontainer contract.

A broken devcontainer does not fail loudly in CI - it fails when somebody
tries to create a Codespace, which is the worst possible moment and the
hardest place to debug. These tests are cheap and pin the specific things that
have already gone wrong once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEVCONTAINER = REPO_ROOT / ".devcontainer"


def load_jsonc(path: Path) -> dict:
    """Parse devcontainer.json, which permits // comments.

    Whole-line comments only. Stripping every `//` would also mangle any URL
    in the file, and the feature identifiers are URLs.
    """
    raw = path.read_text(encoding="utf-8")
    stripped = "\n".join(
        "" if line.lstrip().startswith("//") else line for line in raw.splitlines()
    )
    return json.loads(stripped)


@pytest.fixture
def config() -> dict:
    return load_jsonc(DEVCONTAINER / "devcontainer.json")


class TestDevcontainerJson:
    def test_parses(self, config):
        assert config["name"] == "graph-cloud-benchmark"

    def test_builds_from_the_local_dockerfile(self, config):
        # The Dockerfile carries the APT repair that has to run before any
        # feature installs. Reverting to a bare `image` key would reintroduce
        # the Yarn key failure that broke Codespace creation.
        assert "build" in config, "must build from the local Dockerfile"
        assert "image" not in config, "`image` and `build` are mutually exclusive"
        dockerfile = DEVCONTAINER / config["build"]["dockerfile"]
        assert dockerfile.is_file(), f"{dockerfile} does not exist"

    def test_docker_in_docker_not_outside_of_docker(self, config):
        features = set(config.get("features", {}))
        assert any("docker-in-docker" in f for f in features), (
            "the harness connects to bolt://localhost; with docker-outside-of-docker "
            "published ports land on the host instead and every connection is refused"
        )
        assert not any("docker-outside-of-docker" in f for f in features)

    def test_lifecycle_script_exists(self, config):
        command = config["postCreateCommand"]
        referenced = [word for word in command.split() if word.endswith(".sh")]
        assert referenced, "postCreateCommand should call a script, not inline a pipeline"
        for name in referenced:
            assert (REPO_ROOT / name).is_file(), f"{name} is referenced but missing"

    def test_host_requirements_fit_one_database_at_a_time(self, config):
        import yaml

        raw = yaml.safe_load((REPO_ROOT / "config" / "databases.yaml").read_text(encoding="utf-8"))
        capped = [t for t in raw["targets"] if t["tier"] == "self-hosted-capped"]
        per_target_cpu = max(t["resources"]["cpus"] for t in capped)
        per_target_mem = max(t["resources"]["memory_gb"] for t in capped)

        host_cpu = config["hostRequirements"]["cpus"]
        host_mem = int(config["hostRequirements"]["memory"].rstrip("gb"))

        # One database plus the client and the nested daemon. This is the
        # whole justification for measuring sequentially; if a future edit
        # raises the per-target cap past what the machine can hold, the plan
        # silently stops working.
        assert host_cpu >= per_target_cpu + 1, (
            f"{host_cpu} vCPU cannot host a {per_target_cpu} vCPU database and a client"
        )
        assert host_mem >= per_target_mem + 2, (
            f"{host_mem} GB cannot host a {per_target_mem} GB database with headroom"
        )


class TestDockerfile:
    @pytest.fixture
    def dockerfile(self) -> str:
        return (DEVCONTAINER / "Dockerfile").read_text(encoding="utf-8")

    def test_removes_the_yarn_repository(self, dockerfile):
        # The specific failure: NO_PUBKEY 62D54FD4003F6525 from dl.yarnpkg.com
        # makes apt-get update fatal, which kills the feature install.
        assert "yarnpkg" in dockerfile

    def test_runs_apt_get_update_to_prove_the_repair(self, dockerfile):
        # Proving it at build time is the point. Without this the image builds
        # happily and the failure resurfaces during the feature install.
        assert "apt-get update" in dockerfile

    def test_protects_the_main_sources_list(self, dockerfile):
        # sources.list carries Debian itself. It must be edited, never deleted.
        assert "sed -i" in dockerfile
        assert "rm -f /etc/apt/sources.list\n" not in dockerfile
        assert "rm -rf /etc/apt/sources.list " not in dockerfile

    def test_drops_back_to_the_unprivileged_user(self, dockerfile):
        # Features and lifecycle commands expect to run as vscode; leaving the
        # image as root changes file ownership in ways that surface much later.
        assert dockerfile.rstrip().endswith("USER vscode")


class TestShellScripts:
    @pytest.mark.parametrize(
        "relative",
        [".devcontainer/post-create.sh", "scripts/check_runtime.sh", "scripts/run_suite.sh"],
    )
    def test_script_exists_and_has_a_shebang(self, relative):
        path = REPO_ROOT / relative
        assert path.is_file(), f"{relative} is missing"
        first = path.read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("#!"), f"{relative} has no shebang"

    def test_post_create_does_not_abort_the_codespace(self):
        text = (DEVCONTAINER / "post-create.sh").read_text(encoding="utf-8")
        # Comments are stripped before matching: the script explains at length
        # why it deliberately omits `set -e`, and a naive substring search
        # finds that explanation and fails on it.
        code = [
            line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
        ]
        errexit = [
            line for line in code if line.strip().startswith(("set -e", "set -eu", "set -o"))
        ]
        # A half-created Codespace you can debug beats a failed creation you
        # cannot. Every step reports its own outcome and the script exits 0.
        assert not errexit, f"post-create must not abort on a single failed step: {errexit}"
        assert text.rstrip().endswith("exit 0")

    def test_runtime_check_verifies_enforcement_not_just_presence(self):
        text = (REPO_ROOT / "scripts" / "check_runtime.sh").read_text(encoding="utf-8")
        # docker-in-docker can report healthy and still ignore --memory and
        # --cpus. A parity benchmark under caps that were never applied is
        # worthless, so presence alone is not enough.
        assert "cpu.max" in text
        assert "memory.max" in text
        assert "docker compose version" in text
