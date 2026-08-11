"""Stream 7C - packaging + drift guards for the hosted executor bundle.

Asserts: the vendored envelope crypto is byte-identical to the beta-agent copy; the stage manifest's sources all
exist; the WinSW XML + installer are ASCII-only (RULE 9 corollary) with the required contract elements; and the
runner's primitive contract covers exactly the dispatch's primitive names (no drift between the two).
"""
import hashlib
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "hosted-executor")
_LIB = os.path.join(_BUNDLE, "lib")
for _p in (_BUNDLE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import primitive_runner as pr  # noqa: E402
from hosted_workspace.host_agent_dispatch import OP_PRIMITIVES  # noqa: E402


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _is_ascii(path):
    with open(path, "rb") as fh:
        return all(b <= 0x7F for b in fh.read())


class DriftGuardTests(unittest.TestCase):
    def test_vendored_envelope_matches_beta_agent(self):
        mine = os.path.join(_LIB, "broker_cred_envelope.py")
        beta = os.path.join(_REPO, "deploy", "beta-agent", "broker_cred_envelope.py")
        self.assertTrue(os.path.isfile(mine) and os.path.isfile(beta))
        self.assertEqual(_sha256(mine), _sha256(beta),
                         "vendored broker_cred_envelope.py drifted from the beta-agent copy")

    def test_contract_covers_exactly_dispatch_primitives(self):
        dispatch_primitives = {v["primitive"] for v in OP_PRIMITIVES.values()}
        self.assertEqual(set(pr.CONTRACT), dispatch_primitives,
                         "primitive_runner.CONTRACT drifted from host_agent_dispatch.OP_PRIMITIVES")


class StageManifestTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_BUNDLE, "stage-manifest.json")) as fh:
            self.manifest = json.load(fh)

    def test_all_staged_sources_exist(self):
        for entry in self.manifest["stage"]:
            src = os.path.join(_REPO, entry["src"])
            self.assertTrue(os.path.isfile(src), f"stage source missing: {entry['src']}")

    def test_all_scripts_exist_and_match_runner(self):
        scripts_from = os.path.join(_REPO, self.manifest["scripts_from"])
        for name in self.manifest["scripts"]:
            self.assertTrue(os.path.isfile(os.path.join(scripts_from, name)), f"primitive missing: {name}")
        # the manifest's script list must equal the runner's required scripts (no drift)
        runner = pr.PrimitiveRunner(scripts_dir=scripts_from)
        self.assertEqual(set(self.manifest["scripts"]), set(runner.required_scripts()))

    def test_backend_single_source_modules_are_staged(self):
        staged = {e["src"] for e in self.manifest["stage"]}
        for need in ("backend/hosted_workspace/host_protocol.py",
                     "backend/hosted_workspace/host_agent_dispatch.py",
                     "backend/hosted_workspace/__init__.py"):
            self.assertIn(need, staged, f"{need} must be staged to the host (single source of truth)")


class AsciiArtefactTests(unittest.TestCase):
    def test_installer_is_ascii(self):
        self.assertTrue(_is_ascii(os.path.join(_BUNDLE, "install_service.ps1")))

    def test_winsw_xmls_are_ascii(self):
        for name in ("GuvFXHostedExecutor.xml", "GuvFXHostedExecutor.supervised.xml"):
            self.assertTrue(_is_ascii(os.path.join(_BUNDLE, "winsw", name)), f"{name} is not ASCII")


class WinSwContractTests(unittest.TestCase):
    def _read(self, name):
        with open(os.path.join(_BUNDLE, "winsw", name), encoding="ascii") as fh:
            return fh.read()

    def test_dark_profile_invariants(self):
        xml = self._read("GuvFXHostedExecutor.xml")
        self.assertIn("<id>GuvFXHostedExecutor</id>", xml)
        self.assertIn("daemon.py", xml)
        self.assertIn("<startmode>Manual</startmode>", xml)
        self.assertIn('<onfailure action="none" />', xml)
        # ADR-0040 privilege model: the daemon runs as LocalSystem (the signed protocol + allow-listed primitives
        # are the security boundary). WinSW ignores <serviceaccount>; the installer's `sc config obj=` is authoritative.
        self.assertIn("<username>LocalSystem</username>", xml)

    def test_supervised_profile_invariants(self):
        xml = self._read("GuvFXHostedExecutor.supervised.xml")
        self.assertIn("<startmode>Automatic</startmode>", xml)
        self.assertIn("<delayedAutoStart>true</delayedAutoStart>", xml)
        self.assertEqual(xml.count('<onfailure action="restart"'), 3)   # bounded 3-tier restart floor
        self.assertIn("<resetfailure>", xml)
        self.assertIn("__SET_AT_INSTALL__", xml)                         # token substituted at install
        # secrets must NEVER be INJECTED as env in the XML (the doc comment may name them; an <env> must not).
        self.assertNotIn('<env name="HOSTED_EXECUTOR_KEYRING"', xml)
        self.assertNotIn('<env name="HOSTED_EXECUTOR_ENC_PRIVKEYS"', xml)
        self.assertNotIn('<env name="HOSTED_EXECUTOR_KEY_ID"', xml)


if __name__ == "__main__":
    unittest.main()
