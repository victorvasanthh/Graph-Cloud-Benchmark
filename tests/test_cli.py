"""Every command-line parser must be constructible.

A duplicate `add_argument` raises `argparse.ArgumentError` at *parser
construction*, which means the script dies before it can print help, before it
validates anything, and before it can say what it was going to do. Nothing else
in the suite exercises that path: the modules import cleanly, the functions are
individually fine, and the failure only appears when somebody runs the command.

That is exactly how `--wait-seconds` shipped twice. It was added by a patch,
verified with a grep for `wait_seconds`, and the underscore form never matches
`add_argument("--wait-seconds", ...)` - so the check reported absence and a
second copy went in on top of the first.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

#: Scripts exposing a reusable parser factory. Kept explicit rather than
#: globbed, so adding a CLI is a deliberate act that includes wiring it here.
PARSER_SCRIPTS = ["run_benchmark"]


def load(module_name: str):
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def option_strings(parser: argparse.ArgumentParser) -> list[str]:
    return [opt for action in parser._actions for opt in action.option_strings]


class TestRunBenchmarkParser:
    @pytest.fixture(scope="class")
    def parser(self) -> argparse.ArgumentParser:
        # The regression: this call raised ArgumentError, so no benchmark ran.
        return load("run_benchmark").build_parser()

    def test_parser_constructs(self, parser):
        assert isinstance(parser, argparse.ArgumentParser)

    def test_no_option_string_is_defined_twice(self, parser):
        seen: set[str] = set()
        duplicates = sorted({opt for opt in option_strings(parser) if opt in seen or seen.add(opt)})
        assert not duplicates, f"option string(s) defined more than once: {duplicates}"

    def test_wait_seconds_is_accepted_exactly_once(self, parser):
        assert option_strings(parser).count("--wait-seconds") == 1

    def test_wait_seconds_parses_and_has_one_authoritative_default(self, parser):
        assert parser.parse_args([]).wait_seconds == 60.0
        assert parser.parse_args(["--wait-seconds", "5"]).wait_seconds == 5.0
        # Zero is the documented way to skip waiting entirely.
        assert parser.parse_args(["--wait-seconds", "0"]).wait_seconds == 0.0

    @pytest.mark.parametrize(
        "flag",
        ["--target", "--workload", "--iterations", "--warmup", "--dry-run", "--quiet", "--run-id"],
    )
    def test_documented_flags_still_exist(self, parser, flag):
        # The README and run_suite.sh both invoke these by name; losing one
        # would break the suite at run time rather than here.
        assert flag in option_strings(parser)

    def test_dry_run_does_not_require_a_configured_target(self, parser):
        args = parser.parse_args(["--dry-run"])
        assert args.dry_run is True


class TestEveryScriptParses:
    """A script whose parser cannot be built is a script that cannot run."""

    @pytest.mark.parametrize("name", PARSER_SCRIPTS)
    def test_parser_factory_is_callable(self, name):
        module = load(name)
        assert hasattr(module, "build_parser"), f"{name}.py has no build_parser()"
        parser = module.build_parser()
        seen: set[str] = set()
        duplicates = sorted({opt for opt in option_strings(parser) if opt in seen or seen.add(opt)})
        assert not duplicates, f"{name}.py defines {duplicates} more than once"

    @pytest.mark.parametrize(
        "name",
        [
            "download_data",
            "init_env",
            "make_report",
            "merge_runs",
            "probe_limits",
            "scan_secrets",
            "show_effective_config",
            "wait_for_target",
            "fix_line_endings",
        ],
    )
    def test_script_imports_without_side_effects(self, name):
        # Importing must not parse arguments or touch the network: these
        # modules are imported by tests and by each other.
        path = SCRIPTS / f"{name}.py"
        if not path.is_file():
            pytest.skip(f"{name}.py not present")
        load(name)
