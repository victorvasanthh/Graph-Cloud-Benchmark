#!/usr/bin/env python3
"""Normalise every tracked text file to LF.

    python scripts/fix_line_endings.py --check   # report only, exit 1 if dirty
    python scripts/fix_line_endings.py           # rewrite offenders in place

The repair half of what tests/test_line_endings.py enforces. `.gitattributes`
prevents new CRLF from entering the repository, but it does not clean what is
already there, and a contributor whose test run just failed deserves a command
rather than instructions.

Reads and writes in **binary** mode, deliberately. `Path.write_text()` opens in
text mode, and on Windows that translates every `\\n` into `\\r\\n` - which is
exactly how 20 files acquired CRLF here in the first place. A repair tool that
reintroduces the fault while fixing it would be worse than none.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

BINARY_SUFFIXES = {".gz", ".png", ".pdf", ".ico", ".jpg", ".jpeg"}


def tracked_text_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )
    if result.returncode != 0:
        raise SystemExit("not a git checkout, or git is unavailable")
    return [
        path
        for path in (REPO_ROOT / name for name in result.stdout.split())
        if path.is_file() and path.suffix.lower() not in BINARY_SUFFIXES
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="report offenders without rewriting them"
    )
    args = parser.parse_args()

    offenders: list[Path] = []
    for path in tracked_text_files():
        data = path.read_bytes()
        if b"\r\n" not in data:
            continue
        offenders.append(path)
        if not args.check:
            path.write_bytes(data.replace(b"\r\n", b"\n"))

    if not offenders:
        print("all tracked text files already use LF")
        return 0

    verb = "would normalise" if args.check else "normalised"
    print(f"{verb} {len(offenders)} file(s):")
    for path in sorted(offenders):
        print(f"  {path.relative_to(REPO_ROOT)}")

    if args.check:
        print(
            "\nRun `python scripts/fix_line_endings.py` to fix them.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
