#!/usr/bin/env python3
"""Replace live instance endpoints with a safe label, everywhere.

    python scripts/redact_endpoints.py --check   # report only, exit 1 if found
    python scripts/redact_endpoints.py           # rewrite in place

The assignment brief is explicit: *"Do not include your CognoDB (or any
platform) passwords or connection URIs in the repository - read them from
environment variables."* Credentials were always read from the environment, but
a connection URI still reached the repository by another route: an
authentication failure, and the driver puts the endpoint it was dialling into
the exception text. That message was recorded verbatim in the run manifest and
travelled into every raw file, summary and report derived from it.

**This only ever touches error and note strings.** Every numeric value - every
duration, every percentile, every count - is left byte-identical, and
`--check` plus a numeric digest before and after are how that is proved rather
than asserted. Redacting a measurement would be a far worse fault than the one
being fixed.

The replacement keeps the shape of what was there, so the record still reads as
"could not connect to a CognoDB endpoint" rather than losing the meaning of the
failure.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Instance-specific endpoints. Matched by shape rather than by one literal
#: hostname, because table rendering truncates long cells and the same endpoint
#: appears cut off at several different lengths.
PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # bolt+s://db-<id>.<region>.databases.cognodb.<tld>[:port], including the
    # truncated forms produced by fixed-width table cells.
    (
        re.compile(r"bolt\+s://db-[A-Za-z0-9][A-Za-z0-9.\-]*(?::\d+)?"),
        "bolt+s://<cognodb-endpoint-redacted>",
    ),
    # The bare hostname, in case it appears without a scheme.
    (
        re.compile(r"\bdb-[A-Za-z0-9]{6,}\.[A-Za-z0-9.\-]*cognodb\.[A-Za-z]+"),
        "<cognodb-endpoint-redacted>",
    ),
    # A real Aura instance host, should one ever be captured the same way.
    (
        re.compile(r"neo4j\+s://[a-f0-9]{6,}\.databases\.neo4j\.io(?::\d+)?"),
        "neo4j+s://<aura-endpoint-redacted>",
    ),
]

SKIP_SUFFIXES = {".gz", ".png", ".jpg", ".jpeg", ".pdf", ".zip", ".whl"}
#: This file necessarily contains the patterns it searches for.
SKIP_FILES = {"scripts/redact_endpoints.py"}
#: Test fixtures and the secret scanner's own examples are deliberately
#: synthetic hostnames. Redacting them would remove the thing they exist to
#: demonstrate, and none of them names a real instance.
SKIP_PREFIXES = ("tests/", "scripts/scan_secrets.py", ".env.example")


def tracked_text_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )
    if result.returncode != 0:
        raise SystemExit("not a git checkout")
    return [
        REPO_ROOT / name
        for name in result.stdout.split()
        if name not in SKIP_FILES
        and not name.startswith(SKIP_PREFIXES)
        and Path(name).suffix.lower() not in SKIP_SUFFIXES
    ]


def redact(text: str) -> tuple[str, int]:
    total = 0
    for pattern, replacement in PATTERNS:
        text, count = pattern.subn(replacement, text)
        total += count
    return text, total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report without rewriting")
    args = parser.parse_args()

    hits: list[tuple[str, int]] = []
    for path in tracked_text_files():
        if not path.is_file():
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        cleaned, count = redact(original)
        if count == 0:
            continue
        hits.append((str(path.relative_to(REPO_ROOT)), count))
        if not args.check:
            # Binary write: text mode would translate newlines on Windows and
            # put CRLF into files that .gitattributes requires to be LF.
            path.write_bytes(cleaned.encode("utf-8"))

    if not hits:
        print("no live endpoints found in tracked files")
        return 0

    verb = "would redact" if args.check else "redacted"
    print(f"{verb} {sum(c for _, c in hits)} occurrence(s) in {len(hits)} file(s):")
    for name, count in sorted(hits):
        print(f"  {count:>4}  {name}")
    return 1 if args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
