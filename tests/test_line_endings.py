"""No tracked text file may contain CRLF.

A shell script checked out with CRLF fails as `$'\\r': command not found` and
`set: pipefail: invalid option name` - bash reads the carriage return as part
of the command name. That is not a lint preference; it broke a smoke run, and
the error message points nowhere near the cause.

The failure arrived through a Windows editor, so a check that only runs on
Linux would not have caught it at the point it was introduced. These tests run
everywhere the suite runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Normalising these would corrupt them. The dataset is checksum-verified, so
#: corruption would surface as a confusing integrity failure rather than as a
#: line-ending mistake.
BINARY_SUFFIXES = {".gz", ".png", ".pdf", ".ico", ".jpg", ".jpeg"}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    return [REPO_ROOT / name for name in result.stdout.split()]


def text_files() -> list[Path]:
    return [
        path
        for path in tracked_files()
        if path.is_file() and path.suffix.lower() not in BINARY_SUFFIXES
    ]


class TestLineEndings:
    def test_no_tracked_text_file_contains_crlf(self):
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for path in text_files()
            if b"\r\n" in path.read_bytes()
        ]
        assert not offenders, (
            "these files contain CRLF and will misbehave on Linux: "
            f"{offenders}. Run `python scripts/fix_line_endings.py` to normalise them."
        )

    def test_no_tracked_text_file_contains_a_bare_carriage_return(self):
        # Old-Mac line endings are rarer but break bash identically.
        offenders = []
        for path in text_files():
            data = path.read_bytes()
            if b"\r" in data.replace(b"\r\n", b""):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        assert not offenders, f"bare CR found in: {offenders}"

    @pytest.mark.parametrize(
        "relative",
        [
            "scripts/run_suite.sh",
            "scripts/check_runtime.sh",
            ".devcontainer/post-create.sh",
            "Makefile",
        ],
    )
    def test_executed_files_are_lf(self, relative):
        # Named individually as well as covered by the sweep above, so a
        # failure says which executed file broke rather than only that some
        # file did.
        path = REPO_ROOT / relative
        assert path.is_file(), f"{relative} is missing"
        assert b"\r\n" not in path.read_bytes(), f"{relative} has CRLF and will not run on Linux"


class TestGitattributes:
    @pytest.fixture(scope="class")
    def attributes(self) -> str:
        path = REPO_ROOT / ".gitattributes"
        assert path.is_file(), (
            ".gitattributes is missing; without it line endings depend on each "
            "machine's git configuration, which is how this broke the first time"
        )
        return path.read_text(encoding="utf-8")

    def test_forces_lf_repository_wide(self, attributes):
        assert "* text=auto eol=lf" in attributes

    def test_pins_shell_scripts_explicitly(self, attributes):
        # The wildcard should cover it, but shell scripts are the case where
        # being wrong is a hard failure rather than a nuisance, so the rule is
        # stated rather than inferred.
        assert "*.sh" in attributes
        assert "Makefile" in attributes

    def test_marks_the_dataset_as_binary(self, attributes):
        assert "*.gz" in attributes and "binary" in attributes

    def test_gitattributes_itself_is_lf(self):
        assert b"\r\n" not in (REPO_ROOT / ".gitattributes").read_bytes()
