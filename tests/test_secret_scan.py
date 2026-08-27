"""The secret scanner must catch real leaks and ignore the config design.

Worth testing properly rather than trusting: an earlier revision of the
scanner had its word-boundary escapes silently corrupted, which left it
reporting "clean" while matching almost nothing. A scanner that cannot fail is
worse than no scanner, because it is believed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scan_secrets import PATTERNS, _looks_like_a_value, scan_text  # noqa: E402


class TestCatchesRealLeaks:
    @pytest.mark.parametrize(
        "line",
        [
            "COGNODB_PASSWORD=hunter2correcthorse",
            'password: "s3cr3t-value-here"',
            "api_key = 'abcdef1234567890abcdef'",
            "token: ghp_0123456789abcdefghijABCDEFGHIJ",
        ],
    )
    def test_literal_credentials_are_reported(self, line):
        assert scan_text(line), f"missed a credential in: {line}"

    def test_uri_with_inline_credentials(self):
        assert scan_text("uri = bolt+s://neo4j:realpassword@instance.example.com:7687")

    def test_aws_key(self):
        assert scan_text("aws = AKIAIOSFODNN7EXAMPLE")

    def test_private_key_block(self):
        assert scan_text("-----BEGIN RSA PRIVATE KEY-----")


class TestIgnoresTheConfigDesign:
    @pytest.mark.parametrize(
        "line",
        [
            # The whole point of config/databases.yaml: names, not values.
            "      password: COGNODB_PASSWORD",
            "      password: ALPHA_PASSWORD",
            # Ordinary code reading a setting.
            '        password = self.settings.get("password") or ""',
            '        password = self.settings.get("password") or None',
            # Compose interpolation with a required-variable guard.
            "      ARANGO_ROOT_PASSWORD: ${ARANGO_PASSWORD:?set ARANGO_PASSWORD in .env}",
            "      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:?set NEO4J_PASSWORD in .env}",
            # Empty keys, as .env.example declares them.
            "COGNODB_PASSWORD=",
            "MEMGRAPH_PASSWORD=",
            # Angle-bracket placeholders.
            "COGNODB_URI=bolt+s://<your-instance-id>.databases.cognodb.cloud",
            # A str.format field, as scripts/init_env.py templates its output.
            "ARANGO_PASSWORD={arango}",
            "NEO4J_PASSWORD={neo4j}",
            # Prose that happens to contain a credential word and a colon.
            '    """A URL-safe token: no quoting problems in .env or a Bolt URI."""',
            "# The password is displayed EXACTLY ONCE at creation time.",
            "    #: Workload names this target should answer wrongly, token: none",
        ],
    )
    def test_no_false_positive(self, line):
        assert scan_text(line) == [], f"false positive on: {line}"


class TestValueHeuristic:
    def test_env_var_name_is_a_reference(self):
        assert not _looks_like_a_value("COGNODB_PASSWORD")

    def test_short_values_are_ignored(self):
        assert not _looks_like_a_value("abc")

    def test_placeholder_words_are_ignored(self):
        assert not _looks_like_a_value("changeme")
        assert not _looks_like_a_value("placeholder")

    def test_a_real_looking_literal_is_a_value(self):
        assert _looks_like_a_value("Hs83ndkeJJ20fmzz")

    def test_quotes_are_stripped_before_judging(self):
        assert _looks_like_a_value('"Hs83ndkeJJ20fmzz"')

    def test_a_quoted_value_may_contain_spaces(self):
        # Rare but legal, and the quotes are what make it unambiguous.
        assert _looks_like_a_value('"correct horse battery staple"')

    def test_unquoted_prose_is_not_a_value(self):
        assert not _looks_like_a_value("no quoting problems in .env or a Bolt URI.")

    def test_format_placeholder_is_not_a_value(self):
        assert not _looks_like_a_value("{arango}")


class TestPatternIntegrity:
    """Guards against the escape-corruption that motivated these tests."""

    def test_patterns_contain_no_control_characters(self):
        for name, pattern in PATTERNS:
            assert "\x08" not in pattern.pattern, f"{name} contains a literal backspace"
            assert all(ord(c) >= 32 or c in "\t" for c in pattern.pattern), name

    def test_scanner_source_has_no_control_characters(self):
        raw = (REPO_ROOT / "scripts" / "scan_secrets.py").read_bytes()
        stray = [i for i, byte in enumerate(raw) if byte < 9 or 13 < byte < 32]
        assert not stray, f"control bytes in scan_secrets.py at offsets {stray[:5]}"


class TestRepository:
    def test_the_committed_tree_is_clean(self):
        """The scanner must pass on this repository as it stands."""
        import scan_secrets

        problems: list[str] = []
        scan_secrets.check_env_example(problems)
        for path in scan_secrets.candidate_files(staged=False):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative in scan_secrets.ALLOWLIST:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            problems.extend(scan_secrets.scan_text(text, relative))
        assert problems == []
