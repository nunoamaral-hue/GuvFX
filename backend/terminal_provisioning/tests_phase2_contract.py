"""ADR-0027 Phase 2 — timeout contract, isolation and manifest-consistency tests.

Covers Workstream B (the timeout invariant + fail-closed validators + runtime-override protection) and the
Workstream A governance surface (the committed image manifest/provenance stay in lock-step with the pinned
source hashes, and the deployed isolation primitive accepts the governed build-5833 path while rejecting every
forbidden root). Pure logic — no host/MT5/network.
"""
import json
import os
import sys

from django.test import SimpleTestCase, override_settings

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

import config as agent_config          # noqa: E402
import validation_image as vi          # noqa: E402
import validate_login as vl            # noqa: E402
from terminal_provisioning import beta_worker as bw   # noqa: E402

_MANIFEST = os.path.join(_BUNDLE, "validation_image_manifest.json")
_PROVENANCE = os.path.join(_BUNDLE, "validation_image_provenance.json")


# ── B1: timeout invariant + fail-closed validators ──────────────────────────────────────────────────────
class TimeoutContractTests(SimpleTestCase):
    def test_canonical_values_and_invariant(self):
        login_s = agent_config.DEFAULT_LOGIN_TIMEOUT_MS / 1000.0
        grace = agent_config.DEFAULT_CLEANUP_GRACE_S
        agent_wait = login_s + grace
        backend_op = bw.OP_TRANSPORT_TIMEOUTS["VALIDATE_LOGIN"]
        self.assertEqual((login_s, grace, agent_wait, backend_op), (120.0, 45, 165.0, 175))
        # backend > agent_wait > login + mandatory cleanup
        self.assertGreater(backend_op, agent_wait)
        self.assertGreater(agent_wait, login_s + agent_config.MIN_CLEANUP_GRACE_S)
        self.assertEqual(agent_config.MIN_CLEANUP_GRACE_S, 30)   # 45 = 30 mandatory + 15 margin

    def test_agent_contract_fails_closed_on_low_grace(self):
        with self.assertRaises(agent_config.ConfigError):
            agent_config.assert_validation_timeout_contract(120000, 10)   # 10 < 30 min
        agent_config.assert_validation_timeout_contract(120000, 45)       # canonical: no raise

    def test_backend_contract_fails_closed_below_floor(self):
        with self.assertRaises(ValueError):
            bw.assert_backend_timeout_contract(165)     # == floor, not >
        with self.assertRaises(ValueError):
            bw.assert_backend_timeout_contract(100)
        bw.assert_backend_timeout_contract(175)         # canonical: no raise

    def test_runtime_override_cannot_weaken_contract(self):
        with override_settings(BETA_AGENT_OP_TIMEOUTS={"VALIDATE_LOGIN": 30}):
            self.assertEqual(bw._op_read_timeout("VALIDATE_LOGIN"), 175)   # clamped up to canonical
        with override_settings(BETA_AGENT_OP_TIMEOUTS={"VALIDATE_LOGIN": 300}):
            self.assertEqual(bw._op_read_timeout("VALIDATE_LOGIN"), 300)   # a SAFE higher override is honoured

    def test_load_config_applies_and_validates_contract(self):
        env = {"BETA_AGENT_BIND_HOST": agent_config.DEFAULT_EXPECTED_BIND_HOST,
               "BETA_AGENT_EXPECTED_BIND_HOST": agent_config.DEFAULT_EXPECTED_BIND_HOST,
               "BETA_AGENT_BIND_PORT": "8791", "BETA_AGENT_KEYRING": '{"k":"v"}', "BETA_AGENT_KEY_ID": "k"}
        cfg = agent_config.load_config(dict(env))
        self.assertEqual(cfg["login_timeout_ms"], 120000)
        self.assertEqual(cfg["cleanup_grace_s"], 45)
        # an unsafe configured grace fails CLOSED at load (startup), not merely warns
        with self.assertRaises(agent_config.ConfigError):
            agent_config.load_config(dict(env, BETA_AGENT_CLEANUP_GRACE_S="5"))


# ── A5: isolation of the governed build-5833 image via the DEPLOYED primitive ────────────────────────────
class ImageIsolationTests(SimpleTestCase):
    ROOT = r"C:\GuvFX\beta\validation-5833"
    DIR = r"C:\GuvFX\beta\validation-5833\terminal"
    FORBIDDEN = (r"C:\GuvFX\beta\slots", r"C:\GuvFX\golden", r"C:\GuvFX\beta\accounts",
                 r"C:\GuvFX\beta\validation", r"C:\Program Files\IS6 Technologies MT5 Terminal")

    def test_governed_image_accepted(self):
        p = vl.assert_isolated_validation_terminal(
            self.DIR, validation_root=self.ROOT, forbidden_roots=self.FORBIDDEN, path_exists=lambda _p: True)
        self.assertTrue(p.endswith("terminal64.exe"))

    def test_forbidden_roots_rejected(self):
        negs = {
            "cz_slot2": r"C:\GuvFX\beta\slots\2\terminal",
            "prod_is6": r"C:\Program Files\IS6 Technologies MT5 Terminal",
            "golden": r"C:\GuvFX\golden\newMT5",
            "canonical6073": r"C:\GuvFX\beta\validation\terminal",
            "traversal": r"C:\GuvFX\beta\validation-5833\..\slots\2",
            "bare_drive": "C:\\",
        }
        for name, path in negs.items():
            with self.assertRaises(vl.IsolationError, msg=name):
                root = self.ROOT if name != "bare_drive" else "C:\\"
                vl.assert_isolated_validation_terminal(
                    path, validation_root=root, forbidden_roots=self.FORBIDDEN, path_exists=lambda _p: True)

    def test_missing_executable_rejected(self):
        with self.assertRaises(vl.IsolationError):
            vl.assert_isolated_validation_terminal(
                self.DIR, validation_root=self.ROOT, forbidden_roots=self.FORBIDDEN, path_exists=lambda _p: False)


# ── A4: the committed manifest + provenance stay in lock-step with the pinned source hashes ──────────────
class ManifestConsistencyTests(SimpleTestCase):
    def setUp(self):
        with open(_MANIFEST, encoding="utf-8") as fh:
            self.m = json.load(fh)
        with open(_PROVENANCE, encoding="utf-8") as fh:
            self.p = json.load(fh)

    def test_manifest_source_hashes_match_module(self):
        self.assertEqual(self.m["source_hashes"], vi.SOURCE_HASHES)
        self.assertEqual(sorted(self.m["allow_listed_files"]), sorted(vi.ALLOW_LIST))
        self.assertEqual(self.m["expected_ex5_count"], 131)
        self.assertEqual(self.m["account_artefact_count"], 0)
        self.assertEqual(self.m["attached_ea_count"], 0)
        self.assertEqual(self.m["terminal_build"], vi.SOURCE_BUILD_TERMINAL)
        self.assertEqual(self.m["metaeditor_build"], vi.SOURCE_BUILD_METAEDITOR)

    def test_provenance_hashes_match_module(self):
        self.assertEqual(self.p["source"]["terminal64.exe"]["sha256"], vi.SOURCE_HASHES["terminal64.exe"])
        self.assertEqual(self.p["source"]["MetaEditor64.exe"]["sha256"], vi.SOURCE_HASHES["metaeditor64.exe"])
        self.assertFalse(self.p["source"]["appdata_accessed"])
        self.assertFalse(self.p["source"]["production_account_state_copied"])

    def test_build_script_source_hashes_match_module(self):
        with open(os.path.join(_BUNDLE, "build_validation_image.ps1"), encoding="utf-8") as fh:
            script = fh.read().lower()
        for h in vi.SOURCE_HASHES.values():                 # every pinned hash appears in the builder
            self.assertIn(h, script)
        self.assertTrue(script.isascii())                   # RULE 9: ASCII-only installer artefact
