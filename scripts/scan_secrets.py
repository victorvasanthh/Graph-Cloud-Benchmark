#!/usr/bin/env python3
"""Fail if anything credential-shaped is about to be committed.

    python scripts/scan_secrets.py            # scan the working tree
    python scripts/scan_secrets.py --staged   # scan only what is staged

This is a guardrail, not a security product. It catches the specific mistakes
this repository invites - a filled-in .env committed by accident, a password
pasted into a YAML file, a connection URI with credentials inline - and it
errs towards false positives, because a false positive costs a moment and a
false negative publishes a password.

The interesting part is `_looks_like_a_value`. A regex alone cannot tell
`password: COGNODB_PASSWORD`, which is an environment variable *name* and the
entire point of this repository's config design, from a pasted credential. So
the regex only locates candidates and a separate rule decides.

.env.example is checked for the opposite property: that its credential keys
are all empty.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Locates `key: value` / `key = value` for credential-ish keys. Whether the
#: right-hand side is really a secret is decided by `_looks_like_a_value`.
#:
#: There is deliberately no word boundary before the keyword. The single most
#: important thing this scanner has to catch is a filled-in .env line such as
#: `COGNODB_PASSWORD=...`, and a leading \b never matches there because the
#: underscore preceding PASSWORD is itself a word character. The trailing \b
#: stays, so `password_hash = ...` is not mistaken for a credential.
ASSIGNMENT = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*(?P<value>\S.*)$"
)

#: Patterns that are conclusive on their own - no value heuristic needed.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "connection URI with inline credentials",
        re.compile(
            r"(?i)\b(bolt|neo4j|redis|rediss|http|https|mongodb)(\+s|\+ssc)?://[^\s:/]+:[^\s@]+@"
        ),
    ),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
]

#: Values that are obviously stand-ins rather than credentials.
PLACEHOLDERS = {
    "changeme",
    "example",
    "none",
    "null",
    "password",
    "placeholder",
    "secret",
    "todo",
    "xxx",
}

#: Markers that mean "this is a reference or an expression, not a literal".
EXPRESSION_MARKERS = ("${", "$(", "(", ")", "os.environ", "getenv", "=>", "!=", "==")


def _looks_like_a_value(raw: str) -> bool:
    """True when the right-hand side looks like a literal credential.

    Everything this repository does on purpose has to survive here:

      * `password: COGNODB_PASSWORD` - a YAML mapping to an env var name;
      * `password = self.settings.get("password") or ""` - ordinary code;
      * `ARANGO_ROOT_PASSWORD: ${ARANGO_PASSWORD:?...}` - compose interpolation;
      * `COGNODB_PASSWORD=` - an empty key in .env.example.

    A real leak is a short literal with no structure, so the test is for the
    absence of every marker meaning "this is a reference" rather than for the
    presence of secret-ness.
    """
    candidate = raw.strip()
    if candidate[:1] in {'"', "'"}:
        # Quoted: the credential is whatever sits inside the quotes, spaces
        # included, so `password: "two words here"` is still a value.
        quote = candidate[0]
        closing = candidate.find(quote, 1)
        value = candidate[1:closing] if closing > 0 else candidate[1:]
    else:
        # Unquoted: a credential never contains whitespace, so everything from
        # the first space onwards is prose. This is what stops a docstring or
        # a comment - "A URL-safe token: no quoting problems in .env" - from
        # reading as an assignment.
        value = candidate.split("#", 1)[0].split()[0].rstrip(",") if candidate.split() else ""

    if len(value) < 8:
        return False
    if value.lower() in PLACEHOLDERS:
        return False
    # An environment variable name: a reference, not a value.
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        return False
    # A str.format field such as `{arango}`, as scripts/init_env.py templates
    # its output. The real value is substituted at run time and never appears
    # in the source.
    if re.fullmatch(r"\{[A-Za-z_][A-Za-z0-9_]*\}", value):
        return False
    if any(marker in value for marker in EXPRESSION_MARKERS):
        return False
    # Attribute access or a call chain: `self.settings.get(...)`, `cfg.password`.
    if re.match(r"^[A-Za-z_][\w.]*\.[A-Za-z_]", value):
        return False
    # Angle-bracket or `your-...` placeholders, as .env.example uses.
    return not (value.startswith("<") or value.lower().startswith("your"))


#: Files that legitimately discuss credentials without holding any.
ALLOWLIST = {
    ".env.example",
    "scripts/scan_secrets.py",
    "tests/test_secret_scan.py",
    "docs/methodology.md",
    "README.md",
}

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv", "data"}
SKIP_SUFFIXES = {".gz", ".png", ".jpg", ".jpeg", ".pdf", ".zip", ".whl"}


def scan_text(text: str, label: str = "<input>") -> list[str]:
    """Findings for one file's contents. Exposed so the tests can drive it."""
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        conclusive = next((name for name, pattern in PATTERNS if pattern.search(line)), None)
        if conclusive:
            problems.append(f"{label}:{number}: {conclusive}")
            continue
        if "${" in line:
            # Shell or Compose interpolation. The credential comes from the
            # environment at run time, so the line holds a reference however
            # much of it looks like an assignment. Checked on the whole line
            # rather than on the captured value, because a guard such as
            # `${NEO4J_PASSWORD:?set NEO4J_PASSWORD in .env}` puts the keyword
            # inside the interpolation and the message after it.
            continue
        assignment = ASSIGNMENT.search(line)
        if assignment and _looks_like_a_value(assignment.group("value")):
            problems.append(f"{label}:{number}: credential assigned a literal value")
    return problems


def candidate_files(staged: bool) -> list[Path]:
    if staged:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        return [REPO_ROOT / line for line in out.stdout.split() if (REPO_ROOT / line).is_file()]

    found: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        found.append(path)
    return found


def check_env_example(problems: list[str]) -> None:
    """.env.example must declare credential keys and never carry values."""
    example = REPO_ROOT / ".env.example"
    if not example.exists():
        return
    for number, line in enumerate(example.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if not any(word in key.upper() for word in ("PASSWORD", "SECRET", "TOKEN", "KEY")):
            continue
        if value.strip():
            problems.append(
                f".env.example:{number}: {key.strip()} has a value; the example file must "
                f"declare empty credential keys only"
            )


def check_git_tracking(problems: list[str]) -> None:
    """The one file guaranteed to hold real credentials must not be tracked."""
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )
    if tracked.returncode != 0:
        return
    for name in tracked.stdout.split():
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            # .gitignore covers this; the check catches a `git add -f` that
            # deliberately bypassed the ignore rule.
            problems.append(f"{name} is tracked by git and must not be")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="scan only staged changes")
    args = parser.parse_args()

    problems: list[str] = []
    check_git_tracking(problems)
    check_env_example(problems)

    for path in candidate_files(args.staged):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        problems.extend(scan_text(text, relative))

    if problems:
        print("SECRET SCAN FAILED", file=sys.stderr)
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
        print(
            "\nIf one of these is a false positive, confirm it really is, then add the "
            "file to ALLOWLIST in scripts/scan_secrets.py with a comment saying why.",
            file=sys.stderr,
        )
        return 1

    print("secret scan clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
