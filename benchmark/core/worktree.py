"""Classify a dirty working tree, so nothing is discarded blindly.

`verify-config` used to report "25 uncommitted changes" and stop. That is true
and useless: it does not say whether those are smoke artifacts nobody wants,
real results that should be committed, or a leaked credential that must never
be. The safe reaction to an unexplained count is to delete everything, which is
exactly the reaction that loses work.

Each entry lands in one of five buckets, ordered by how much attention it
deserves:

  NEVER_COMMIT  a secret or a dataset blob. Loud, and fails the check.
  SOURCE        a tracked file that changed. Review, then commit.
  RESULT        a real run's output. The repository is designed to keep these.
  SMOKE         a feasibility probe's output. Disposable by construction.
  UNKNOWN       nothing matched. Treated as needing review, never as noise.

Anything unrecognised is UNKNOWN rather than assumed harmless, because the
whole point is to avoid a default that quietly throws something away.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

NEVER_COMMIT = "never-commit"
SOURCE = "source"
RESULT = "result"
SMOKE = "smoke"
UNKNOWN = "unknown"

#: Substrings that mark a path as something that must not enter the repository.
#: Deliberately broader than .gitignore: if one of these ever shows up as
#: *staged*, .gitignore has already failed and the point is to notice.
SECRET_MARKERS = (".env", ".pem", ".key", "credentials.json")

ADVICE = {
    NEVER_COMMIT: "must never be committed - check .gitignore and unstage it",
    SOURCE: "review the diff, then commit it",
    RESULT: "output of a real run; commit it if the run is one you want to keep",
    SMOKE: "feasibility-probe output; safe to delete, and now gitignored",
    UNKNOWN: "unrecognised - inspect before doing anything with it",
}


@dataclass
class Entry:
    status: str
    path: str
    category: str

    @property
    def staged(self) -> bool:
        return self.status[:1] not in {" ", "?"}

    @property
    def untracked(self) -> bool:
        return self.status == "??"


def classify(path: str) -> str:
    """Bucket one path. Order matters: the dangerous tests run first."""
    lowered = path.lower()

    # .env.example is the documented template and carries no values.
    if lowered.endswith(".env.example"):
        return SOURCE
    if any(marker in lowered for marker in SECRET_MARKERS):
        return NEVER_COMMIT
    if lowered.startswith("data/"):
        # The dataset is downloaded and checksum-verified, never vendored. Only
        # the directory's own documentation belongs in the repository.
        return SOURCE if lowered.endswith((".md", ".gitkeep")) else NEVER_COMMIT

    # Smoke before result: a smoke artifact also lives under results/.
    name = Path(path).name
    if "smoke-" in name or name.startswith("report-smoke-"):
        return SMOKE
    if lowered.startswith(("results/", "charts/")) or name.startswith("report-"):
        return RESULT

    if lowered.startswith(
        (
            "benchmark/",
            "scripts/",
            "tests/",
            "config/",
            "infra/",
            "docs/",
            ".github/",
            ".devcontainer/",
        )
    ):
        return SOURCE
    if path in {
        "Makefile",
        "README.md",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        ".gitignore",
        ".gitattributes",
        "LICENSE",
    }:
        return SOURCE
    return UNKNOWN


def read_status(repo_root: Path) -> list[Entry]:
    """Parse `git status --porcelain`. Empty when the tree is clean."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=False,
    )
    if result.returncode != 0:
        return []

    entries: list[Entry] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        status, path = line[:2], line[3:].strip()
        # Renames arrive as "old -> new"; the destination is what matters.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        entries.append(Entry(status=status, path=path, category=classify(path)))
    return entries


def group(entries: list[Entry]) -> dict[str, list[Entry]]:
    grouped: dict[str, list[Entry]] = {}
    for entry in entries:
        grouped.setdefault(entry.category, []).append(entry)
    return grouped


def blocking(entries: list[Entry]) -> list[Entry]:
    """Entries that must be resolved before a benchmark is considered clean.

    Smoke output is not blocking: it is disposable by construction and is
    gitignored, so it should never appear here at all. A RESULT is blocking
    only when staged - an uncommitted real result is a decision the operator
    has to make, not something to be swept along by a later commit.
    """
    return [
        entry
        for entry in entries
        if entry.category in {NEVER_COMMIT, SOURCE, UNKNOWN}
        or (entry.category == RESULT and entry.staged)
    ]
