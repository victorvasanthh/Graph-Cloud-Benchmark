#!/usr/bin/env python3
"""Fetch the cit-HepTh dataset from SNAP and verify it.

Run before the first benchmark:

    python scripts/download_data.py

Files already present are checksum-verified and left alone, so re-running is
cheap and safe. A file whose digest does not match the one recorded in
benchmark/datasets/cit_hepth.py is reported and *not* overwritten - a silent
re-download of a changed upstream file is exactly how a benchmark stops being
reproducible without anybody noticing.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.datasets.cit_hepth import (  # noqa: E402
    DEFAULT_DATA_DIR,
    EXPECTED_SHA256,
    SOURCE_URLS,
    sha256_of,
)

USER_AGENT = "graph-cloud-benchmark/1.0 (dataset fetch)"


def download(url: str, destination: Path, timeout: float) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    partial = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        partial.write_bytes(response.read())
    # Moved into place only once the whole body has arrived, so an interrupted
    # download cannot leave a truncated file that passes an existence check.
    partial.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="where to write the files"
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download even if the file is present and its checksum matches",
    )
    args = parser.parse_args()

    args.data_dir.mkdir(parents=True, exist_ok=True)
    failures = 0

    for filename, url in SOURCE_URLS.items():
        destination = args.data_dir / filename
        expected = EXPECTED_SHA256[filename]

        if destination.exists() and not args.force:
            actual = sha256_of(destination)
            if actual == expected:
                print(f"ok    {filename} (already present, checksum matches)")
                continue
            print(
                f"STALE {filename}: on-disk sha256 {actual}\n"
                f"      does not match the expected {expected}.\n"
                f"      Not overwriting. Delete it and re-run, or pass --force, "
                f"but first work out why it changed.",
                file=sys.stderr,
            )
            failures += 1
            continue

        print(f"fetch {filename} from {url}")
        try:
            download(url, destination, args.timeout)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"FAIL  {filename}: {exc}", file=sys.stderr)
            failures += 1
            continue

        actual = sha256_of(destination)
        if actual != expected:
            print(
                f"FAIL  {filename}: downloaded file has sha256 {actual}, expected {expected}",
                file=sys.stderr,
            )
            failures += 1
        else:
            size_mb = destination.stat().st_size / (1024 * 1024)
            print(f"ok    {filename} ({size_mb:.1f} MiB, checksum verified)")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
