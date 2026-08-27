"""Classification of a dirty working tree.

`verify-config` reported "25 uncommitted changes" and stopped. A bare count is
true and useless: it does not distinguish a smoke artifact nobody wants from a
leaked credential that must never be committed. The safe-looking reaction to an
unexplained count is to delete everything, and that is the reaction that loses
work - so the classification has to be right.
"""

from __future__ import annotations

import pytest

from benchmark.core.worktree import (
    NEVER_COMMIT,
    RESULT,
    SMOKE,
    SOURCE,
    UNKNOWN,
    Entry,
    blocking,
    classify,
    group,
)


class TestNeverCommit:
    @pytest.mark.parametrize(
        "path",
        [
            ".env",
            ".env.local",
            "server.pem",
            "id_rsa.key",
            "credentials.json",
            "data/cit-HepTh.txt.gz",
            "data/cit-HepTh-dates.txt.gz",
        ],
    )
    def test_secrets_and_dataset_blobs(self, path):
        assert classify(path) == NEVER_COMMIT

    def test_env_example_is_a_template_not_a_secret(self):
        # It ships with every value blank and is the documented starting point.
        assert classify(".env.example") == SOURCE

    def test_data_directory_documentation_is_source(self):
        assert classify("data/README.md") == SOURCE
        assert classify("data/.gitkeep") == SOURCE


class TestSmokeVersusResult:
    @pytest.mark.parametrize(
        "path",
        [
            "results/raw/smoke-20260828T101500Z-arangodb.json",
            "results/summary/smoke-20260828T101500Z-combined.json",
            "charts/latency-smoke-20260828T101500Z-combined.png",
            "docs/report-smoke-20260828T101500Z-combined.md",
        ],
    )
    def test_smoke_artifacts(self, path):
        # A smoke run is 1 iteration with no warmup. Filing it as a result
        # would put a non-measurement where measurements live.
        assert classify(path) == SMOKE

    @pytest.mark.parametrize(
        "path",
        [
            "results/raw/20260828T101500Z-combined.json",
            "results/summary/20260828T101500Z-neo4j-selfhosted.json",
            "charts/latency-20260828T101500Z-combined.png",
            "docs/report-20260828T101500Z-combined.md",
        ],
    )
    def test_real_run_artifacts(self, path):
        assert classify(path) == RESULT

    def test_smoke_is_checked_before_result(self):
        # Both live under results/. Order matters, and getting it backwards
        # would silently reclassify every smoke artifact as data.
        assert classify("results/raw/smoke-x.json") == SMOKE


class TestSource:
    @pytest.mark.parametrize(
        "path",
        [
            "benchmark/core/worktree.py",
            "scripts/run_benchmark.py",
            "tests/test_cli.py",
            "config/databases.yaml",
            "infra/docker-compose.yml",
            "docs/methodology.md",
            ".github/workflows/ci.yml",
            ".devcontainer/Dockerfile",
            "Makefile",
            "README.md",
            ".gitignore",
            ".gitattributes",
        ],
    )
    def test_tracked_source_files(self, path):
        assert classify(path) == SOURCE


class TestUnknown:
    @pytest.mark.parametrize("path", ["wat.tmp", "notes.txt", "some/other/thing"])
    def test_unrecognised_paths_are_flagged_not_ignored(self, path):
        # Defaulting to "harmless" is how something gets thrown away. Anything
        # unmatched demands a human look.
        assert classify(path) == UNKNOWN


class TestBlocking:
    def make(self, status: str, path: str) -> Entry:
        return Entry(status=status, path=path, category=classify(path))

    def test_secrets_block(self):
        assert blocking([self.make("??", ".env")])

    def test_source_changes_block(self):
        assert blocking([self.make(" M", "benchmark/core/config.py")])

    def test_unknown_blocks(self):
        assert blocking([self.make("??", "mystery.bin")])

    def test_smoke_artifacts_do_not_block(self):
        # Disposable by construction, and gitignored, so they should not even
        # reach this function.
        assert not blocking([self.make("??", "results/raw/smoke-x.json")])

    def test_untracked_real_results_do_not_block(self):
        # Keeping a real run is the operator's decision, not a precondition.
        assert not blocking([self.make("??", "results/raw/20260828-combined.json")])

    def test_staged_real_results_do_block(self):
        # Already staged means it is about to be committed by the next commit,
        # which should be deliberate rather than incidental.
        assert blocking([self.make("A ", "results/raw/20260828-combined.json")])

    def test_clean_tree_blocks_nothing(self):
        assert blocking([]) == []


class TestEntry:
    def test_untracked_detection(self):
        assert Entry("??", "x", SOURCE).untracked
        assert not Entry(" M", "x", SOURCE).untracked

    def test_staged_detection(self):
        assert Entry("M ", "x", SOURCE).staged
        assert Entry("A ", "x", SOURCE).staged
        assert not Entry(" M", "x", SOURCE).staged
        assert not Entry("??", "x", SOURCE).staged


class TestGrouping:
    def test_groups_by_category(self):
        entries = [
            Entry("??", "results/raw/smoke-a.json", SMOKE),
            Entry("??", "results/raw/smoke-b.json", SMOKE),
            Entry(" M", "README.md", SOURCE),
        ]
        grouped = group(entries)
        assert len(grouped[SMOKE]) == 2
        assert len(grouped[SOURCE]) == 1


class TestGitignoreCoversSmoke:
    def test_smoke_patterns_are_ignored(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / ".gitignore").read_text("utf-8")
        # Without these the artifacts reappear on every smoke run and the tree
        # is never clean, which is the state that blocked the last benchmark.
        for pattern in (
            "results/raw/smoke-*",
            "results/summary/smoke-*",
            "charts/*smoke-*",
            "docs/report-smoke-*",
        ):
            assert pattern in text, f"{pattern} missing from .gitignore"
