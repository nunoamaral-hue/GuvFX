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
_OPWRAP = _BUNDLE / "operator" / "Invoke-GuvfxCopierOneShotAttestation.ps1"
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


class ParityValidatorPs51FixTests(SimpleTestCase):
    """GFX-COPIER-PARITY-VALIDATOR-FIX-013 — the host-parity validator must complete on Windows PowerShell 5.1:

    Defect 1 — native schtasks non-zero exits are handled through an ErrorActionPreference='Continue'-scoped helper
    (never a terminating error on 5.1 or 7.4+), and cleanup of a missing temp task is a no-op.
    Defect 2 — the principal is an explicit parameter, SID-verified BEFORE any task is created, with NO fallback to
    the current user's domain/name.

    These are source-level regression (mutation-killing) checks; the live schtasks sequence is exercised on a
    Windows runner, not here (no Windows in CI)."""

    def _text(self):
        return _PARITY.read_text()

    # --- Defect 1: native-exit handling; missing-task cleanup must not terminate the validator ---
    def test_native_schtasks_routed_through_continue_scoped_helper(self):
        t = self._text()
        self.assertIn("function Invoke-Schtasks", t, "missing the EAP-scoped schtasks helper")
        self.assertIn("$ErrorActionPreference = 'Continue'", t, "helper must scope EAP to Continue for native exits")
        self.assertIn("$LASTEXITCODE", t, "native exit codes must be read, not thrown")
        self.assertIn("function Remove-TempTask", t, "missing-task cleanup must be a dedicated no-op wrapper")
        # A 2>$null redirect under EAP=Stop is exactly what threw on PS 5.1 (Defect 1); it must not return.
        self.assertNotIn("2>$null", t, "2>$null under EAP=Stop reintroduces the PS 5.1 terminating-error (Defect 1)")

    def test_does_not_depend_on_ps73_native_guard(self):
        # Setting $PSNativeCommandUseErrorActionPreference is inert on PS 5.1; the fix must not RELY on it as the
        # guard (a comment explaining why the EAP-scoped helper is needed is fine). Kill the old assignment form.
        t = self._text()
        self.assertNotIn("$PSNativeCommandUseErrorActionPreference =", t,
                         "fix must not depend on assigning the PS7.3+-only native guard (inert on 5.1)")

    # --- Defect 2: explicit, SID-verified principal; no current-user fallback ---
    def test_principal_is_explicit_parameter(self):
        self.assertRegex(self._text(), r"\[string\]\$Principal", "an explicit -Principal parameter is required")

    def test_no_current_user_principal_fallback(self):
        t = self._text()
        self.assertNotIn("$env:USERDOMAIN", t, "must not fall back to the current user's domain (Defect 2)")
        self.assertNotIn("$env:USERNAME", t, "must not fall back to the current user's name (Defect 2)")

    def test_principal_sid_verified_and_fail_closed_before_create(self):
        t = self._text()
        self.assertIn("SecurityIdentifier", t, "principal must be translated to a SID")
        self.assertIn("Translate", t)
        self.assertIn("exit 2", t, "omitted/unresolvable principal must fail closed (exit 2)")
        # the fail-closed principal guard must run before the first task-creating call
        self.assertLess(t.index("exit 2"), t.index("Create-And-Readback $tplPath"),
                        "principal SID check + fail-closed must precede any task creation")

    # --- preserved semantics ---
    def test_preserves_disabled_triggerless_highest_negctl_and_cleanup(self):
        t = self._text()
        self.assertIn("<Enabled>false</Enabled>", t)                 # disabled
        self.assertIn("<Trigger>", t)                                # triggerless readback assertion present
        self.assertIn("HighestAvailable", t)                         # no-HighestAvailable readback assertion present
        self.assertIn("RestartOnFailure", t)                         # no-RestartOnFailure readback assertion present
        self.assertIn("'PT' + '0M'", t)                              # PT0M negative control (constructed, not literal)
        self.assertNotIn("PT0M", t)                                  # the literal never appears in the asset
        self.assertIn("finally", t)                                  # self-clean on success and failure
        self.assertIn("PARITYTEST", t)                               # non-production task name


class OperatorWrapperTests(SimpleTestCase):
    """GFX-COPIER-ATTESTATION-OPERATOR-WRAPPER-021 - the human-operated one-shot attestation wrapper must enter
    secrets interactively (never as arguments), expose them to the child emitter process ONLY, submit exactly one
    envelope with no retry, never print the envelope/signature, always clean up, and never touch the scheduled
    task. These are source-level (mutation-killing) assertions; the behavioural mock test runs on a Windows runner."""

    def _t(self):
        return _OPWRAP.read_text()

    def test_present_ascii_no_bom_ps51(self):
        self.assertTrue(_OPWRAP.exists(), "operator wrapper missing")
        data = _OPWRAP.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "BOM in operator wrapper")
        self.assertTrue(all(b <= 0x7F for b in data), "non-ASCII byte in operator wrapper")
        self.assertIn("#Requires -Version 5.1", self._t())
        self.assertFalse(_SECRET_RE.search(self._t()), "secret-looking literal in operator wrapper")

    def test_no_secret_parameters(self):
        t = self._t()
        # the param() block must not accept a secret/key/token/password as an argument
        head = t.split("$ErrorActionPreference", 1)[0]
        for bad in ("$IngestKey", "$HmacKey", "$Secret", "$Token", "$AgentToken", "$Password", "$ApiKey"):
            self.assertNotIn(bad, head, f"operator wrapper must not take {bad} as a parameter")

    def test_secrets_entered_non_echoing(self):
        t = self._t()
        self.assertGreaterEqual(t.count("Read-Host -AsSecureString"), 2, "both secrets must use non-echoing prompts")
        self.assertIn("SecureStringToBSTR", t)   # SecureString -> plaintext only for child env injection
        self.assertIn("ZeroFreeBSTR", t)         # BSTR zeroed after use

    def test_secret_injected_into_child_env_only_not_persisted(self):
        t = self._t()
        self.assertIn("$psi.EnvironmentVariables.Add($ingestEnvName, $plainKey)", t)
        self.assertIn("$psi.EnvironmentVariables.Add($agentEnvName, $plainToken)", t)
        self.assertIn("$psi.UseShellExecute = $false", t)
        # must NOT persist a secret anywhere
        self.assertNotIn("[Environment]::SetEnvironmentVariable", t, "must not write persistent env")
        self.assertNotIn("Set-ItemProperty", t, "must not write the registry")
        self.assertNotIn("Out-File", t)
        self.assertNotIn("Set-Content", t)
        self.assertNotIn("Export-Clixml", t)

    def test_secret_never_in_process_arguments(self):
        t = self._t()
        # the child Arguments line carries only the entrypoint + config path, never a secret variable
        arg_lines = [ln for ln in t.splitlines() if "$psi.Arguments" in ln]
        self.assertTrue(arg_lines, "no Arguments assignment found")
        for ln in arg_lines:
            self.assertNotIn("$plainKey", ln)
            self.assertNotIn("$plainToken", ln)

    def test_exactly_one_emitter_invocation_and_one_post_no_retry(self):
        t = self._t()
        self.assertEqual(t.count("[System.Diagnostics.Process]::Start("), 1, "exactly one emitter invocation")
        self.assertEqual(t.count("Invoke-WebRequest"), 1, "exactly one HTTP submission")
        # the actual emitter invocation (the $psi.Arguments assignment) uses attest mode ONLY - checked on the
        # invocation line, not the whole file (the header comment legitimately names the forbidden modes)
        arg_lines = [ln for ln in t.splitlines() if "$psi.Arguments" in ln]
        self.assertTrue(arg_lines, "no $psi.Arguments assignment found")
        for ln in arg_lines:
            self.assertIn("attest --config", ln)
            self.assertNotIn("observe", ln)
            self.assertNotIn("run --config", ln)
            self.assertNotIn("delivery", ln)
        # no retry loop around the submission
        post_idx = t.index("Invoke-WebRequest")
        window = t[max(0, post_idx - 400):post_idx]
        self.assertNotRegex(window, r"\b(for|foreach|while|do)\b", "no retry loop around the single POST")

    def test_envelope_and_secrets_never_printed(self):
        t = self._t()
        self.assertNotIn("Write-Output $envelopeJson", t)
        self.assertNotIn("Write-Host", t)
        self.assertNotIn("Write-Output $stdout", t)
        # the envelope is explicitly kept in-memory only
        self.assertIn("NEVER printed", t)
        # the only structured result is the redacted ordered dict
        self.assertIn("Write-RedactedResult", t)
        self.assertIn("reason_code", t)
        # the signature is read only to validate presence, never emitted to output
        self.assertNotIn("Write-RedactedResult $result", t)
        self.assertNotIn("$envelopeJson }", t.split("Write-RedactedResult", 1)[0])

    def test_transcription_and_tracing_guards(self):
        t = self._t()
        self.assertIn("EnableTranscripting", t)
        self.assertIn("Stop-Transcript", t)
        self.assertIn("Set-PSDebug -Off", t)
        self.assertIn("VerbosePreference", t)
        self.assertIn("DebugPreference", t)

    def test_finally_cleanup_and_task_untouched(self):
        t = self._t()
        self.assertIn("finally", t)
        tail = t.rsplit("finally", 1)[1]
        self.assertIn("$plainKey = $null", tail)
        self.assertIn("$plainToken = $null", tail)
        self.assertIn("Dispose", tail)
        # the wrapper never invokes schtasks or any scheduled-task cmdlet (real calls, not comment mentions)
        self.assertNotIn("schtasks", t, "wrapper must not invoke schtasks")
        for cmd in ("Register-ScheduledTask", "Start-ScheduledTask", "Enable-ScheduledTask", "Set-ScheduledTask"):
            self.assertNotIn(cmd, t, f"wrapper must not touch the scheduled task ({cmd})")

    def test_mock_assets_present_ascii_and_synthetic(self):
        me = _BUNDLE / "operator" / "tests" / "mock_emitter.py"
        pe = _BUNDLE / "operator" / "tests" / "Test-OperatorWrapper.Tests.ps1"
        for f in (me, pe):
            self.assertTrue(f.exists(), f"mock asset missing: {f.name}")
            data = f.read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), f"BOM in {f.name}")
            self.assertTrue(all(b <= 0x7F for b in data), f"non-ASCII in {f.name}")
            text = f.read_text()
            self.assertFalse(_SECRET_RE.search(text), f"secret-looking literal in {f.name}")
            self.assertIn("SYNTHETIC", text.upper())                 # explicitly synthetic
            self.assertNotIn("api.guvfx.com", text, f"{f.name} must not reference a real endpoint")
            self.assertNotIn("100.79.101.19", text)                  # no real host
        # the mock verifier is loopback-only
        self.assertIn("127.0.0.1", pe.read_text())
