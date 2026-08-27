#!/usr/bin/env python3
"""Diagnose a diverged checkout and, on request, snap it to the remote.

    python scripts/sync_to_remote.py            # report only
    python scripts/sync_to_remote.py --apply    # back up, then hard reset

After history is rewritten - removing an attribution trailer, say - every
commit gets a new hash. A clone that still holds the old lineage sees two
unrelated histories and `git pull` offers to *merge* them, which drags the
rewritten-away commits back in and undoes the rewrite. That is the failure this
exists to prevent, and it is not obvious from git's own message.

Reporting is the default because a hard reset is not reversible from the
working tree. `--apply` first creates a timestamped backup branch, so the
discarded lineage is still reachable by name afterwards.
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def lines(*args: str) -> list[str]:
    code, out = git(*args)
    return out.splitlines() if code == 0 and out else []


def patch_ids(revset: str) -> dict[str, str]:
    """Map patch-id -> subject for each commit in `revset`.

    A patch-id identifies a change by its *content*, not its hash. It is what
    makes it safe to say "this local commit is already upstream under a
    different hash", which is precisely the situation a rewrite creates.
    """
    result: dict[str, str] = {}
    for sha in lines("rev-list", revset):
        code, diff = git("show", "--format=", sha)
        if code != 0:
            continue
        proc = subprocess.run(
            ["git", "patch-id", "--stable"],
            input=diff,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        pid = proc.stdout.split()[0] if proc.stdout.split() else sha
        _, subject = git("log", "-1", "--format=%s", sha)
        result[pid] = subject
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="back up, then hard reset")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="master")
    args = parser.parse_args()

    upstream = f"{args.remote}/{args.branch}"
    git("fetch", args.remote)

    _, head = git("rev-parse", "--short", "HEAD")
    _, remote_head = git("rev-parse", "--short", upstream)
    ahead = lines("rev-list", f"{upstream}..HEAD")
    behind = lines("rev-list", f"HEAD..{upstream}")

    print(f"local  {args.branch}: {head}")
    print(f"remote {upstream}: {remote_head}")
    print(f"ahead {len(ahead)} / behind {len(behind)}")

    if not ahead and not behind:
        print("\nin sync; nothing to do")
        return 0

    if not ahead:
        print(f"\nfast-forward available: {len(behind)} commit(s) to pull")
        print("  git pull --ff-only")
        return 0

    print(f"\nDIVERGED: {len(ahead)} local commit(s) are not on the remote.")
    local_ids = patch_ids(f"{upstream}..HEAD")
    remote_ids = patch_ids(f"HEAD..{upstream}")

    unique = {pid: subj for pid, subj in local_ids.items() if pid not in remote_ids}
    shared = {pid: subj for pid, subj in local_ids.items() if pid in remote_ids}

    if shared:
        print(f"\n  {len(shared)} local commit(s) already exist upstream under a different")
        print("  hash - the signature of a rewritten history. Safe to discard:")
        for subject in list(shared.values())[:10]:
            print(f"    - {subject[:70]}")

    if unique:
        print(f"\n  {len(unique)} local commit(s) contain changes NOT on the remote:")
        for subject in list(unique.values())[:10]:
            print(f"    ! {subject[:70]}")
        print("\n  Resetting would discard these. Decide what to do with them first;")
        print("  `git cherry-pick` onto the remote branch is usually what you want.")

    dirty = lines("status", "--porcelain")
    tracked_dirty = [entry for entry in dirty if not entry.startswith("??")]
    if tracked_dirty:
        print(f"\n  {len(tracked_dirty)} uncommitted change(s) to tracked files would be lost.")

    if not args.apply:
        print("\nreport only. To synchronise:")
        print("  python scripts/sync_to_remote.py --apply")
        return 1

    if unique:
        print("\nrefusing to reset: local-only changes would be lost. See above.")
        return 1

    backup = f"backup-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    code, out = git("branch", backup)
    if code != 0:
        print(f"\ncould not create backup branch: {out}")
        return 1
    print(f"\nbacked up current lineage to branch {backup}")

    code, out = git("reset", "--hard", upstream)
    if code != 0:
        print(f"reset failed: {out}")
        return 1
    _, now = git("rev-parse", "--short", "HEAD")
    print(f"reset to {upstream} ({now})")
    print(f"the previous lineage is still reachable as {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
