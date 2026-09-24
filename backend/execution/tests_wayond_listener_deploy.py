"""Config-check: the committed Wayond-listener deploy definition must faithfully supply the
full certified listener environment, so a standard ``docker compose ... up`` recreate can
never silently drop a functional key (the RULE 5/8 regression that would disable
multi-account routing / the drawdown gate).

Dependency-free by design: PyYAML is NOT installed in the backend venv, so the committed
``wayond-listener.env.example`` (key NAMES only, no secret values) is the enforced contract
and the overlay is scanned as text. Runs headless in CI under ``make check`` — no DB, no
network, no access to the prod secret ``*.env`` files.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

_DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "wayond-listener"
_EXAMPLE = _DEPLOY / "wayond-listener.env.example"
_OVERLAY = _DEPLOY / "docker-compose.wayond-listener.yml"

# The functional environment the certified running listener carries. Dropping any of these
# on a recreate is exactly the regression this test guards. Keep in sync with
# wayond-listener.env.example (the test fails if the example omits any of these).
REQUIRED_KEYS = {
    "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT", "DJANGO_SECRET_KEY",
    "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_STRING_SESSION",
    "TELEGRAM_DEVICE_MODEL", "TELEGRAM_SYSTEM_VERSION", "TELEGRAM_APP_VERSION",
    "MULTI_ACCOUNT_ROUTING_ENABLED", "RISK_MAX_DAILY_DRAWDOWN_ABS",
    "HOSTED_PERSISTENT_MT5_ENABLED", "GUVFX_AGENT_URL", "GUVFX_WINDOWS_AGENT_BASE_URL",
}


def _example_pairs():
    """(key, sep, value) for each non-comment, non-blank line of the .example file."""
    pairs = []
    for raw in _EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        pairs.append((key.strip(), sep, value.strip()))
    return pairs


class WayondListenerDeployContractTests(SimpleTestCase):
    def test_example_lists_all_required_keys(self):
        keys = {k for k, _sep, _v in _example_pairs()}
        missing = REQUIRED_KEYS - keys
        self.assertFalse(
            missing, f"wayond-listener.env.example is missing required keys: {sorted(missing)}")

    def test_example_never_commits_a_value(self):
        for key, sep, value in _example_pairs():
            self.assertEqual(sep, "=", f"malformed example line for {key!r} (expected KEY=)")
            self.assertEqual(
                value, "", f"wayond-listener.env.example must not commit a value for {key!r}")

    def test_overlay_loads_both_env_files(self):
        text = _OVERLAY.read_text()
        for name in ("wayond-listener.env", "bridge-agent.env"):
            self.assertRegex(
                text, rf"(?m)^\s*-\s*{re.escape(name)}\s*$",
                f"overlay must load {name} via env_file (guards the dropped-key regression)")

    def test_overlay_has_no_environment_block(self):
        # env_file loads the whole file; a hand-enumerated `environment:` block is how the five
        # functional keys were dropped, and (compose precedence environment > env_file) it could
        # shadow the file. Forbid ANY environment: key — block- or inline-flow-mapping form
        # (`environment: {..}`) — so the contract can't silently regress. (Every overlay line that
        # mentions "environment:" is a comment starting with '#', which `^\s*environment:` can't match.)
        text = _OVERLAY.read_text()
        self.assertNotRegex(
            text, r"(?m)^\s*environment:",
            "overlay must not declare an environment: block; supply env via env_file only")
