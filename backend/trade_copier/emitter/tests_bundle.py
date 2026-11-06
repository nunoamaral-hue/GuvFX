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
_PARITY = _BUNDLE / "tests" / "Test-TaskXmlSchtasksParity.ps1"
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
        # "never auto-restart" is expressed by OMITTING <RestartOnFailure> entirely. A zero-duration
        # <Interval>PT0M</Interval> is REJECTED by schtasks (out of range), so neither the element nor PT0M
        # may appear (GFX-COPIER-HOST-EMITTER-TASKXML-FIX-009).
        self.assertNotIn("<RestartOnFailure", xml)
        self.assertNotIn("PT0M", xml)
        self.assertIn("<RunLevel>LeastPrivilege</RunLevel>", xml)

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


class TaskXmlRestartOnFailureFixTests(SimpleTestCase):
    """GFX-COPIER-HOST-EMITTER-TASKXML-FIX-009 — the no-auto-restart intent is expressed by OMITTING
    <RestartOnFailure>. A zero-duration <Interval>PT0M</Interval> is rejected by schtasks (out of range) and
    must never reappear in the vendored bundle (template OR the installer's generated XML).
    """

    def test_generated_install_xml_omits_restartonfailure(self):
        install = (_BUNDLE / "Install-GuvfxCopierEmitter.ps1").read_text()
        self.assertNotIn("<RestartOnFailure", install)
        self.assertNotIn("PT0M", install)
        # the disabled / triggerless / least-privilege shape is preserved
        self.assertIn("<Enabled>false</Enabled>", install)
        self.assertIn("<Triggers />", install)
        self.assertIn("<RunLevel>LeastPrivilege</RunLevel>", install)

    def test_no_pt0m_anywhere_in_vendored_bundle(self):
        # Scope = the vendored payload (top-level bundle files). The host-parity asset under tests/ is tooling,
        # not the bundle, and legitimately carries a PT0M negative control.
        for f in _BUNDLE.glob("*"):
            if f.is_file():
                self.assertNotIn("PT0M", f.read_text(errors="ignore"), f"PT0M reappeared in {f.name}")

    def test_no_restartonfailure_anywhere_in_vendored_bundle(self):
        for f in _BUNDLE.glob("*"):
            if f.is_file():
                self.assertNotIn("<RestartOnFailure", f.read_text(errors="ignore"),
                                  f"RestartOnFailure reappeared in {f.name}")

    def test_template_and_generated_restart_semantics_equivalent(self):
        tpl = (_BUNDLE / "emitter_task_template.xml").read_text()
        ins = (_BUNDLE / "Install-GuvfxCopierEmitter.ps1").read_text()
        for blob, label in ((tpl, "template"), (ins, "generated")):
            self.assertIn("<Enabled>false</Enabled>", blob, label)
            self.assertIn("<Triggers />", blob, label)
            self.assertNotIn("<Trigger>", blob, label)
            self.assertNotIn("<RestartOnFailure", blob, label)
            self.assertNotIn("PT0M", blob, label)
            self.assertIn("<RunLevel>LeastPrivilege</RunLevel>", blob, label)

    def test_account_id_plan_and_rollback_behaviour_preserved(self):
        ins = (_BUNDLE / "Install-GuvfxCopierEmitter.ps1").read_text()
        # account id fail-closed (reserved <= 1)
        self.assertIn("if ($AccountId -le 1)", ins)
        self.assertIn("account_id_reserved", ins)
        # PLAN is the dry-run default and is non-mutating: the plan emit precedes any schtasks /create
        self.assertIn("$planOnly = (-not $Apply)", ins)
        self.assertIn("would_register_disabled", ins)
        self.assertLess(ins.index("would_register_disabled"), ins.index("schtasks /create"),
                        "PLAN branch must emit and exit before any schtasks /create")
        # existing-task snapshot (overwrite protection) + rollback restore remain intact
        self.assertIn("$existingXml", ins)
        self.assertIn("install_failed_rolled_back", ins)


class BundlePackagingHygieneTests(SimpleTestCase):
    """Fail-closed packaging hygiene: a host bundle must never carry macOS metadata, trailing-dot filenames, or
    files absent from the manifest (hash mismatches are covered by BundleManifestTests)."""

    def test_no_appledouble_trailing_dot_or_unlisted_top_level_files(self):
        manifest = json.loads((_BUNDLE / "MANIFEST.json").read_text())
        listed = set(manifest["files"]) | {"MANIFEST.json"}
        for f in _BUNDLE.iterdir():
            if f.is_dir():
                continue
            name = f.name
            self.assertFalse(name.startswith("._"), f"AppleDouble metadata file in bundle: {name}")
            self.assertFalse(name.endswith("."), f"trailing-dot filename in bundle: {name}")
            self.assertIn(name, listed, f"bundle file absent from manifest: {name}")

    def test_no_appledouble_or_trailing_dot_recursively(self):
        for f in _BUNDLE.rglob("*"):
            self.assertFalse(f.name.startswith("._"), f"AppleDouble metadata under bundle: {f}")
            self.assertFalse(f.name.endswith("."), f"trailing-dot name under bundle: {f}")


class HostParityAssetTests(SimpleTestCase):
    """The Windows schtasks parity validator must exist, be ASCII, prove its parser with a positive control,
    exercise schtasks create/read-back/delete, use a non-production task name, and reference no emitter/secret/
    broker/bridge/backend. It is NOT run here (needs a Windows host); these are static safety assertions."""

    def test_parity_asset_present_ascii_and_safe(self):
        self.assertTrue(_PARITY.exists(), "host-parity schtasks validation asset missing")
        data = _PARITY.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "BOM in parity asset")
        self.assertTrue(all(b <= 0x7F for b in data), "non-ASCII byte in parity asset")
        text = _PARITY.read_text()
        low = text.lower()
        self.assertIn("parsefile", low)          # parses the PowerShell
        self.assertIn("posctrl", low)            # positive control proving the parser
        self.assertIn("schtasks", low)
        self.assertIn("/create", low)
        self.assertIn("/delete", low)
        self.assertIn("PARITYTEST", text)        # clearly non-production task name
        for forbidden in ("run_emitter", "order_send", "place_order",
                          "COPIER_INGEST_HMAC_KEY", "GUVFX_AGENT_TOKEN", "api.guvfx.com"):
            self.assertNotIn(forbidden, text, f"parity asset must not reference {forbidden}")
        self.assertFalse(_SECRET_RE.search(text), "secret-looking literal in parity asset")
