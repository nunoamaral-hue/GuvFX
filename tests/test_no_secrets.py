"""Tests for the GuvFX secret scanner (standard library only).

Run with:
    python3 -m unittest discover -s tests -p 'test_no_secrets.py' -v

Secret-like strings are constructed at runtime via concatenation so that this
source file never contains a literal high-confidence secret. Where a literal is
unavoidable for a fixture-marker test, the line carries the fixture ignore
marker, which is honoured here because this is the dedicated test file.
"""

import importlib.util
import os
import tempfile
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
_SCANNER_PATH = os.path.join(_REPO_ROOT, "scripts", "check_no_secrets.py")

_spec = importlib.util.spec_from_file_location("check_no_secrets", _SCANNER_PATH)
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)


# Built at runtime so no literal secret appears in this file's source.
def _aws_key():
    return "AKIA" + "ABCDEFGHIJKLMNOP"


def _github_token():
    return "ghp_" + ("a" * 36)


def _private_key_header():
    return "-----BEGIN " + "RSA PRIVATE KEY-----"


def _guvfx_token():
    # A synthetic 20-char bare token shaped like a real GuvFX agent token (built at runtime).
    return "Aa0" + ("b" * 17)


def _marker():
    # Avoid a literal marker token in non-test contexts.
    return scanner.IGNORE_MARKER


class TestSecretDetection(unittest.TestCase):
    def test_detects_private_key_header(self):
        findings = scanner.scan_text(_private_key_header(), "backend/app/keys.py")
        self.assertTrue(any(c == "private-key-header" for _, c in findings))

    def test_detects_aws_access_key(self):
        findings = scanner.scan_text(f"key = '{_aws_key()}'", "backend/app/conf.py")
        self.assertTrue(any(c == "aws-access-key-id" for _, c in findings))

    def test_detects_github_token(self):
        findings = scanner.scan_text(f"token={_github_token()}", "backend/app/conf.py")
        self.assertTrue(any(c == "github-token" for _, c in findings))

    def test_detects_guvfx_agent_token_in_header(self):
        # Regression: a live bridge token was committed in the runbook as a literal header value.
        line = "curl -H \"X-GuvFX-Agent-Token: " + _guvfx_token() + "\" http://host:8788/health"
        findings = scanner.scan_text(line, "docs/OPERATIONS_RUNBOOK.md")
        self.assertTrue(any(c == "guvfx-agent-token-header" for _, c in findings))

    def test_detects_guvfx_token_assignment(self):
        for name in ("GUVFX_AGENT_TOKEN", "GUVFX_WINDOWS_AGENT_TOKEN", "GUVFX_WORKER_TOKEN",
                     "WINDOWS_AGENT_TOKEN"):
            line = f"{name}={_guvfx_token()}"
            findings = scanner.scan_text(line, "deploy/some.env")
            self.assertTrue(any(c == "guvfx-token-assignment" for _, c in findings), name)

    def test_guvfx_env_references_are_not_flagged(self):
        """Env-var references and real source must NOT trip the new patterns (else CI breaks on clean code)."""
        clean = "\n".join([
            'curl -H "X-GuvFX-Agent-Token: $GUVFX_AGENT_TOKEN" http://host:8788/health',
            'curl -H "X-GuvFX-Agent-Token: ${GUVFX_AGENT_TOKEN}" http://host:8788/health',
            'AGENT_TOKEN = os.getenv("GUVFX_AGENT_TOKEN", "").strip()',
            'GUVFX_WINDOWS_AGENT_TOKEN = env("GUVFX_WINDOWS_AGENT_TOKEN", "")',
            'headers["X-GuvFX-Agent-Token"] = token',
            '- GUVFX_AGENT_TOKEN: Token for OHLC endpoint auth (separate from WORKER_TOKEN)',
            'a 401 usually means the backend env lacks a valid `GUVFX_WINDOWS_AGENT_TOKEN`.',
        ])
        self.assertEqual(scanner.scan_text(clean, "backend/app/conf.py"), [])

    def test_clean_fixture_passes(self):
        text = "def add(a, b):\n    return a + b\n# nothing secret here\n"
        self.assertEqual(scanner.scan_text(text, "backend/app/math.py"), [])


class TestIgnoreMarker(unittest.TestCase):
    def test_marker_suppresses_in_fixture_dir(self):
        line = f"key = '{_aws_key()}'  # {_marker()}"
        findings = scanner.scan_text(line, "tests/fixtures/planted.txt")
        self.assertEqual(findings, [])

    def test_marker_suppresses_in_dedicated_test_file(self):
        line = f"key = '{_aws_key()}'  # {_marker()}"
        findings = scanner.scan_text(line, scanner.DEDICATED_TEST_FILE)
        self.assertEqual(findings, [])

    def test_marker_outside_fixture_is_flagged(self):
        line = f"# {_marker()}"
        findings = scanner.scan_text(line, "backend/app/conf.py")
        self.assertTrue(any(c == "ignore-marker-misuse" for _, c in findings))

    def test_marker_outside_fixture_does_not_suppress_secret(self):
        # A real secret plus a marker in a non-fixture file must still be caught.
        line = f"key = '{_aws_key()}'  # {_marker()}"
        findings = scanner.scan_text(line, "backend/app/conf.py")
        categories = {c for _, c in findings}
        self.assertIn("ignore-marker-misuse", categories)
        # The line is reported (not silently passed).
        self.assertTrue(findings)


class TestNoGeneralBypass(unittest.TestCase):
    def test_secret_detected_regardless_of_environment(self):
        # There is no env var / flag that disables scanning.
        os.environ["GUVFX_DISABLE_SECRET_SCAN"] = "1"
        try:
            findings = scanner.scan_text(
                f"key = '{_aws_key()}'", "backend/app/conf.py"
            )
        finally:
            del os.environ["GUVFX_DISABLE_SECRET_SCAN"]
        self.assertTrue(any(c == "aws-access-key-id" for _, c in findings))

    def test_marker_does_not_create_repo_wide_bypass(self):
        # Same marker, ordinary source path -> not suppressed, but flagged.
        secret_line = f"x = '{_github_token()}'  # {_marker()}"
        findings = scanner.scan_text(secret_line, "frontend/src/util.ts")
        self.assertIn("ignore-marker-misuse", {c for _, c in findings})


class TestSafeHandling(unittest.TestCase):
    def test_binary_input_skipped(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"\x00\x01\x02" + _aws_key().encode() + b"\x00")
            path = fh.name
        try:
            self.assertEqual(scanner.scan_file(path, "data/blob.bin"), [])
        finally:
            os.unlink(path)

    def test_oversized_input_skipped(self):
        with tempfile.NamedTemporaryFile(delete=False, mode="w") as fh:
            fh.write("padding\n" * 5)
            fh.write(_aws_key() + "\n")
            fh.write("x" * (scanner.MAX_BYTES + 10))
            path = fh.name
        try:
            self.assertEqual(scanner.scan_file(path, "data/big.txt"), [])
        finally:
            os.unlink(path)

    def test_missing_file_safe(self):
        self.assertEqual(
            scanner.scan_file("/nonexistent/path/xyz", "ghost.txt"), []
        )


if __name__ == "__main__":
    unittest.main()
