"""ADR-0077 — Windows emitter bundle integrity + safety (repository checks; no host).

Validates: manifest hashes match (reproducible), every artefact is ASCII/no-BOM (RULE 9), PowerShell parses
(ParseFile when pwsh is present, else a structural balance mirror), the service defaults disabled/not-scheduled/
no-auto-restart, config gates default OFF with no secret values, no order/mutation verb in the scripts, no secret
literal anywhere, and the pinned lib subset exists.
"""
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from django.test import SimpleTestCase

_ROOT = Path(__file__).resolve().parents[3]
_BUNDLE = _ROOT / "deploy" / "copier-emitter"
_PS1 = sorted(_BUNDLE.glob("*.ps1"))
_ORDER_VERBS = ("order_send", "place_order", "ordersend", "broker_order", "close_position", "modify_position")
_SECRET_RE = re.compile(r"\b[0-9a-fA-F]{40,}\b")


class BundleManifestTests(SimpleTestCase):
    def test_manifest_hashes_match(self):
        manifest = json.loads((_BUNDLE / "MANIFEST.json").read_text())
        self.assertEqual(manifest["defaults"]["service_state"], "disabled_stopped")
        self.assertFalse(manifest["defaults"]["scheduled"])
        self.assertFalse(manifest["defaults"]["auto_restart"])
        for name, expected in manifest["files"].items():
            data = (_BUNDLE / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected, f"hash drift: {name}")

    def test_all_artefacts_ascii_no_bom(self):
        for f in list(_BUNDLE.glob("*")):
            if f.is_file():
                data = f.read_bytes()
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"), f"BOM in {f.name}")
                self.assertTrue(all(b <= 0x7F for b in data), f"non-ASCII byte in {f.name}")

    def test_lib_subset_exists(self):
        manifest = json.loads((_BUNDLE / "MANIFEST.json").read_text())
        for rel in manifest["lib_subset"]:
            self.assertTrue((_ROOT / rel).exists(), f"missing lib subset source: {rel}")


class BundleSafetyTests(SimpleTestCase):
    def test_config_template_gates_off_and_no_secret(self):
        cfg = json.loads((_BUNDLE / "config.template.json").read_text())
        self.assertFalse(cfg["attestation_allowed"])
        self.assertFalse(cfg["observation_allowed"])
        self.assertFalse(cfg["delivery_allowed"])
        self.assertTrue(str(cfg["bridge_base_url"]).startswith(("http://127.0.0.1", "http://localhost")))
        # secrets are referenced by env-var NAME only; no secret value present
        self.assertEqual(cfg["ingest_key_env"], "COPIER_INGEST_HMAC_KEY")
        self.assertFalse(_SECRET_RE.search(json.dumps(cfg)))

    def test_task_template_disabled_no_trigger_no_autorestart(self):
        xml = (_BUNDLE / "emitter_task_template.xml").read_text()
        self.assertIn("<Enabled>false</Enabled>", xml)
        self.assertIn("<Triggers />", xml)                       # never scheduled
        self.assertNotIn("<Trigger>", xml)
        self.assertIn("<Count>0</Count>", xml)                   # no auto-restart into active emission

    def test_scripts_have_no_order_or_secret(self):
        for f in list(_BUNDLE.glob("*.ps1")) + [_BUNDLE / "run_emitter.py"]:
            text = f.read_text().lower()
            for verb in _ORDER_VERBS:
                self.assertNotIn(verb, text, f"{f.name} references a broker-order verb: {verb}")
            self.assertFalse(_SECRET_RE.search(f.read_text()), f"secret-looking literal in {f.name}")

    def test_install_disables_the_task(self):
        install = (_BUNDLE / "Install-GuvfxCopierEmitter.ps1").read_text()
        self.assertIn("/disable", install)
        self.assertIn("<Enabled>false</Enabled>", install)
        self.assertIn("account_id_reserved", install)            # reserved id refusal
        self.assertIn("-not $Apply", install)                    # dry-run default


class PowerShellParseTests(SimpleTestCase):
    def test_powershell_parses_or_is_structurally_balanced(self):
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        for f in _PS1:
            if pwsh:
                cmd = ("$e=$null;"
                       "[System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$null,[ref]$e)|Out-Null;"
                       "if($e -and $e.Count -gt 0){exit 3}else{Write-Output 'PARSE_OK'}" % str(f))
                r = subprocess.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True)
                self.assertIn("PARSE_OK", r.stdout, f"ParseFile failed for {f.name}: {r.stdout}{r.stderr}")
            else:
                text = f.read_text()
                for op, cl in (("{", "}"), ("(", ")"), ("[", "]")):
                    self.assertEqual(text.count(op), text.count(cl), f"unbalanced {op}{cl} in {f.name}")
                self.assertEqual(text.count('"') % 2, 0, f"odd double-quote count in {f.name}")
                self.assertIn("param(", text, f"{f.name} missing a param() block")
