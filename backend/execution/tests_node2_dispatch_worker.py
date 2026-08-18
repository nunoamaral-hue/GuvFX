"""Contract test for the Node-2 dispatch-worker deploy artifact (`deploy/node2-dispatch-worker/`).

The worker is not deployed by this repo (it is a supervised host service), but its deploy definition
is safety-critical: it must carry NO secret literals, must pin the node-2 identity, must route to the
node-2 bridge (never Customer Zero's :8788), and must never re-enable node-1 / legacy / shared-ingest
identities. These invariants are locked here so a future edit that weakens them fails CI.
"""
import os
import re
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEPLOY_DIR = os.path.join(_REPO_ROOT, "deploy", "node2-dispatch-worker")
_COMPOSE = os.path.join(_DEPLOY_DIR, "docker-compose.node2-worker.yml")
_ENV_TPL = os.path.join(_DEPLOY_DIR, "node2-dispatch-worker.env.template")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _active(text):
    """The file with comments stripped — the ACTIVE configuration. Comments legitimately
    document what the file must NOT do (node-1/legacy identities, legacy-auth), so the
    forbidden-content checks run against active config only, never the explanatory comments."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


# A secret KEY-NAME (matches the sibling scanner in tests/test_no_secrets, incl. bare KEY).
_SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASSWORD|PASS|FERNET|PRIVATE|KEY)", re.I)
# A VALUE that looks like a real credential regardless of key name (long opaque string, DSN with
# embedded creds, base64/hex blob). Placeholders (<FILL...>), empty, and plain hostnames/paths are ok.
_SECRET_VALUE = re.compile(r"://[^/\s:]+:[^/\s@]+@|[A-Za-z0-9+/_-]{24,}={0,2}$")


def _assignments(active_text):
    """Yield (key, value) for BOTH yaml-mapping form `KEY: value` and compose/env list form
    `- KEY=value` / `KEY=value`, so a secret cannot hide behind a different syntax."""
    for raw in active_text.splitlines():
        s = raw.strip().lstrip("-").strip()
        m = re.match(r"([A-Za-z0-9_]+)\s*[:=]\s*(.*)$", s)
        if m:
            yield m.group(1), m.group(2).strip().strip('"').strip("'")


def _assert_no_inline_secret(testcase, path):
    """No committed file may carry a real secret. A secret-NAMED key may hold only a placeholder or an
    explicit empty/false pin — ANY other inline value fails (closes the short-secret gap, independent of
    value length). Additionally, a credential-bearing DSN under ANY key name fails. Comments are
    stripped by _active; a value's own inline '#...' is NOT treated as a comment for this check."""
    ALLOWED = ("", '""', "0", "false", "no", "off")   # explicit off-pins for flags like GUVFX_USE_LEGACY_AUTH
    for key, val in _assignments(_active(_read(path))):
        if not val or val.startswith("<FILL") or val.lower() in ALLOWED:
            continue
        if _SECRET_KEY.search(key):                      # secret-named key with a real value → never inline
            testcase.fail(f"{os.path.basename(path)}: inline value under secret-named key {key}")
        if "://" in val and _SECRET_VALUE.search(val):   # DSN with embedded creds under any key name
            testcase.fail(f"{os.path.basename(path)}: credential-bearing URL under {key}")


class Node2DispatchWorkerContractTests(unittest.TestCase):
    def test_artifacts_exist(self):
        self.assertTrue(os.path.isfile(_COMPOSE), "compose file missing")
        self.assertTrue(os.path.isfile(_ENV_TPL), "env template missing")

    def test_node2_identity_pinned(self):
        c = _read(_COMPOSE)
        self.assertIn("MT5_WORKER_ID: mt5-node2-order-1", c)
        self.assertIn('command: ["python", "/app/mt5_trade_ingest_worker.py"]', c)

    def test_routes_to_node2_bridge_not_customer_zero(self):
        active = _active(_read(_COMPOSE))
        self.assertIn(":8789", active)                  # node-2 order bridge
        self.assertNotIn(":8788", active)               # Customer Zero's bridge must never be configured here

    def test_never_reuses_node1_or_legacy_identities(self):
        # Applies to BOTH committed files (compose AND the env template that becomes the host env-file).
        for path in (_COMPOSE, _ENV_TPL):
            active = _active(_read(path))
            for forbidden in ("mt5-trade-ingest-1", "legacy-worker", "guvfx-windows-mt5", ":8788"):
                self.assertNotIn(forbidden, active,
                                 f"{os.path.basename(path)} must not configure {forbidden}")

    def test_legacy_auth_can_never_be_enabled(self):
        # The compose PINS GUVFX_USE_LEGACY_AUTH to empty (highest precedence), and neither committed
        # file may set it to a truthy value. So a poisoned env-file cannot re-enable the legacy path.
        self.assertIn('GUVFX_USE_LEGACY_AUTH: ""', _read(_COMPOSE))   # explicit off-pin present
        for path in (_COMPOSE, _ENV_TPL):
            for key, val in _assignments(_active(_read(path))):
                if key == "GUVFX_USE_LEGACY_AUTH":
                    self.assertIn(val.lower(), ("", '""', "0", "false", "no", "off"),
                                  f"{os.path.basename(path)} must not enable legacy auth")

    def test_supervised_and_bounded_logs(self):
        c = _read(_COMPOSE)
        self.assertIn("restart: unless-stopped", c)
        self.assertIn("max-size", c)                    # bounded logs

    def test_no_secret_literals_in_either_committed_file(self):
        # Both the compose AND the committed env-template must carry no real secret — detected by a
        # secret-looking KEY *or* a credential-looking VALUE (DSN/opaque blob), in mapping OR list form.
        _assert_no_inline_secret(self, _COMPOSE)
        _assert_no_inline_secret(self, _ENV_TPL)
        self.assertIn("env_file:", _read(_COMPOSE))     # secrets are sourced from env-files, not inline

    def test_template_has_placeholders_not_secrets(self):
        t = _read(_ENV_TPL)
        self.assertIn("MT5_WORKER_ID=mt5-node2-order-1", t)
        self.assertIn("<FILL_NODE2_WORKER_SECRET>", t)  # placeholder, not a real secret
        # every *_TOKEN / *_PASSWORD / *_KEY line must be a placeholder or a well-known non-secret
        allow = {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DJANGO_ALLOWED_HOSTS"}
        for line in t.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            if re.search(r"(TOKEN|SECRET|PASSWORD|FERNET|KEY)", key, re.I) and key not in allow:
                self.assertTrue(val.startswith("<FILL") or val == "",
                                f"template {key} must be a placeholder, got a value")


if __name__ == "__main__":
    unittest.main()
