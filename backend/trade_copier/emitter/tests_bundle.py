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
_ONESHOT_INSTALLER = _BUNDLE / "Install-GuvfxCopierEmitterOneShotTask.ps1"
_KEY_PROV = _BUNDLE / "operator" / "Provision-GuvfxCopierOneShotKey.ps1"
_KEY_PROV_TEST = _BUNDLE / "operator" / "tests" / "Test-OneShotKeyProvisioner.Tests.ps1"
_KEY_PROV_EVD = _ROOT / "evidence" / "manifests" / "GFX-EVD-COPIER-ONESHOT-KEY-PROVISIONER.json"
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


class OneShotInstallerNativeSchtasksFixTests(SimpleTestCase):
    """GFX-COPIER-ONESHOT-INSTALLER-NATIVE-SCHTASKS-FIX-103 - the governed one-shot task installer must survive the
    Windows PowerShell 5.1 native-stderr trap. The absent-task /query that every first install performs writes to
    stderr; under $ErrorActionPreference='Stop' with a `2>$null` redirect, PS 5.1 promotes that stderr into a
    TERMINATING error before $LASTEXITCODE is read - which failed the real rollout's PLAN (packet 100). The fix routes
    every real schtasks.exe call through one EAP-scoped helper that reads the native exit code and restores the prior
    EAP in finally. These are source-level (mutation-killing) regressions; the live absent-task sequence + a RULE 11
    positive control are exercised on a Windows runner (no Windows in CI)."""

    def _t(self):
        return _ONESHOT_INSTALLER.read_text()

    def test_present_ascii_no_bom_ps51(self):
        self.assertTrue(_ONESHOT_INSTALLER.exists(), "one-shot installer missing")
        data = _ONESHOT_INSTALLER.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "BOM in one-shot installer")
        self.assertTrue(all(b <= 0x7F for b in data), "non-ASCII byte in one-shot installer")
        self.assertIn("#Requires -Version 5.1", self._t())

    def test_native_schtasks_routed_through_one_eap_scoped_helper(self):
        t = self._t()
        self.assertIn("function Invoke-GuvfxSchtasksNative", t, "missing the single EAP-scoped native helper")
        self.assertIn("$ErrorActionPreference = 'Continue'", t, "helper must scope EAP to Continue for the native call")
        self.assertIn("$LASTEXITCODE", t, "native exit code must be READ, not thrown")
        # the raw native invocation exists exactly ONCE (inside the helper); every other site uses the injected seam
        self.assertEqual(t.count("& schtasks"), 1, "the real schtasks.exe must be invoked from exactly one helper site")
        self.assertIn("$InvokeSchtasks = { param($argList) return (Invoke-GuvfxSchtasksNative -ArgList $argList) }", t,
                      "the default schtasks invoker must route through the safe helper")

    def test_no_terminating_redirect_of_native_stderr_to_null(self):
        # `2>$null` under EAP=Stop is the exact PS 5.1 terminating-error defect (packet 100). It must not appear
        # anywhere in the installer (not even in a comment) so a naive scan cannot be fooled.
        self.assertNotIn("2>$null", self._t(), "2>$null reintroduces the PS 5.1 native-stderr terminating trap")

    def test_helper_restores_prior_eap_in_finally(self):
        t = self._t()
        self.assertIn("$prevEap = $ErrorActionPreference", t, "the helper must snapshot the caller's prior EAP")
        self.assertIn("$ErrorActionPreference = $prevEap", t, "the helper must restore the prior EAP")
        self.assertIn("finally", t, "EAP restore + temp-file cleanup must live in a finally")
        # the restore happens inside the finally (after the final 'finally {' in the helper region)
        helper = t[t.index("function Invoke-GuvfxSchtasksNative"):t.index("function Resolve-GuvfxPrincipalSid")]
        self.assertLess(helper.index("finally"), helper.index("$ErrorActionPreference = $prevEap"),
                        "the EAP restore must be in the finally block, not the try body")

    def test_absent_is_not_a_blanket_catch(self):
        # the fix must NOT paper over every failure as "task absent". The native helper never returns a sentinel from a
        # catch (its only catch guards reading the stderr temp file and assigns a default); absence is inferred by the
        # CALLER from a non-zero /query exit code. (The 3 `catch { return (Fail ...)` sites are JSON-parse fail-closed
        # guards OUTSIDE the helper, which is why this check is scoped to the helper region.)
        t = self._t()
        helper = t[t.index("function Invoke-GuvfxSchtasksNative"):t.index("function Resolve-GuvfxPrincipalSid")]
        self.assertNotIn("catch { return", helper, "the native helper must not relabel a caught failure as a soft pass")
        self.assertIn("finally", helper, "the helper uses try/finally for EAP restore + temp-file cleanup")
        self.assertIn("if ($null -eq $res -or $res.ExitCode -ne 0 -or -not $res.Out) { return $null }", t,
                      "absence must be inferred from the query exit code, not from swallowing an exception")

    def test_preserves_governed_install_invariants(self):
        t = self._t()
        self.assertIn("$PINNED = 46", t)                                   # account-46 pin
        self.assertIn("$EXPLICIT_REFUSE = @(48)", t)                       # account-48 refused
        self.assertIn("$RESERVED = @(0, 1, 18)", t)                        # Customer Zero + reserved refused
        self.assertIn("if (-not $Apply)", t)                               # PLAN dry-run default
        self.assertIn("'GuvFX_CopierEmitterOneShot_'", t)                  # fixed task name
        self.assertIn("rendered_not_disabled", t)                          # disabled render enforced
        self.assertIn("rendered_has_triggers", t)                          # triggerless enforced
        self.assertIn("readback_verified", t)                              # authoritative read-back
        self.assertFalse(_SECRET_RE.search(t), "secret-looking literal in the one-shot installer")

    def test_real_schtasks_regression_asset_present_ascii_and_safe(self):
        asset = _BUNDLE / "tests" / "Install-GuvfxCopierEmitterOneShotTask.RealSchtasks.Tests.ps1"
        self.assertTrue(asset.exists(), "real-schtasks absent-path regression asset missing")
        data = asset.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "BOM in real-schtasks regression asset")
        self.assertTrue(all(b <= 0x7F for b in data), "non-ASCII byte in real-schtasks regression asset")
        text = asset.read_text()
        low = text.lower()
        self.assertIn("invoke-guvfxschtasksnative", low)          # exercises the real helper
        self.assertIn("$erroractionpreference = 'stop'", low)     # reproduces the trap condition
        self.assertIn("/query", low)                              # the absent-task query path
        self.assertIn("poscontrol", low)                          # RULE 11 positive control
        self.assertIn("finally", low)                             # defensive cleanup
        # a clearly non-production, collision-resistant test task name (never a real GuvFX task)
        self.assertIn("GuvFxPesterAbsent", text)
        self.assertNotIn("GuvFX_CopierEmitterOneShot_46", text, "must not reference the real production task name")
        for forbidden in ("COPIER_INGEST_HMAC_KEY", "GUVFX_AGENT_TOKEN", "api.guvfx.com", "100.79.101.19"):
            self.assertNotIn(forbidden, text, f"regression asset must not reference {forbidden}")
        self.assertFalse(_SECRET_RE.search(text), "secret-looking literal in real-schtasks regression asset")


class OneShotInstallerPrincipalSidFixTests(SimpleTestCase):
    """GFX-COPIER-ONESHOT-INSTALLER-PRINCIPAL-NORMALIZATION-FIX-108 - the APPLY read-back must compare the task
    principal by CANONICAL SID, not by comparing a returned SID with the bare 'guvfx_u_46' string. The live rollout
    registered a correct DISABLED/triggerless account-46 task, but schtasks returned the principal as a SID
    (S-1-5-21-...-1065); the old read-back string-compared it to 'guvfx_u_46', reported readback_mismatch, and
    fail-closed-deleted the task. These are source-level (mutation-killing) regressions; the live Windows SID
    normalization is exercised by the real-schtasks asset on a Windows runner (no Windows in Linux CI)."""

    def _t(self):
        return _ONESHOT_INSTALLER.read_text()

    def _resolver_region(self, t):
        return t[t.index("function Resolve-GuvfxPrincipalSid"):t.index("function Install-GuvfxCopierEmitterOneShotTask")]

    def test_canonical_sid_resolver_present_and_authoritative(self):
        t = self._t()
        self.assertIn("function Resolve-GuvfxPrincipalSid", t, "missing the canonical-SID resolver")
        self.assertIn("function Test-PrincipalCanonical", t, "missing the SID-to-SID comparison helper")
        region = self._resolver_region(t)
        # authoritative Windows identity translation, both directions (name->SID and SID->account round-trip)
        self.assertIn("System.Security.Principal.NTAccount", region)
        self.assertIn("System.Security.Principal.SecurityIdentifier", region)
        self.assertIn(".Translate(", region)
        # fail closed: the resolver returns $null on any failure (catch) and is injectable for tests
        self.assertIn("catch { return $null }", region)
        self.assertIn("[scriptblock]$ResolvePrincipalSid = $null", t, "resolver must be injectable for tests")

    def test_resolver_is_not_a_string_or_substring_match(self):
        region = self._resolver_region(self._t())
        # check the CODE only (the header comment legitimately names guvfx_u_46 as an example); the resolver must
        # NEVER lowercase/substring/suffix-match the account name -- translation is authoritative Windows identity.
        code = "\n".join(re.sub(r"#.*$", "", ln) for ln in region.splitlines())
        self.assertNotIn(".ToLower()", code, "principal resolution must be authoritative, not a lowercase compare")
        self.assertNotIn("-like", code, "principal resolution must not use a wildcard/substring -like match")
        self.assertNotIn("guvfx_u_46", code, "the resolver must not special-case the account name as a string")

    def test_readback_uses_canonical_sid_not_raw_string(self):
        t = self._t()
        # both read-back sites (authoritative read-back + idempotency compliant check) use the canonical comparison
        self.assertIn("$rbUid = (Test-PrincipalCanonical (", t, "APPLY read-back must use the canonical SID comparison")
        self.assertGreaterEqual(t.count("Test-PrincipalCanonical ("), 2, "both read-back + compliant checks must canonicalize")
        # the raw 'returned UserId -eq $PRINCIPAL' string equality must be GONE
        self.assertNotIn("Principal.UserId) -eq $PRINCIPAL", t, "raw string compare of the read-back principal must be removed")
        # the RENDER check (what we rendered == the bare name) legitimately stays a -ne string check
        self.assertIn("Principal.UserId) -ne $PRINCIPAL", t, "the render-validation principal check must be preserved")

    def test_account_pin_and_disabled_triggerless_preserved(self):
        t = self._t()
        self.assertIn("$PINNED = 46", t)
        self.assertIn("$EXPLICIT_REFUSE = @(48)", t)
        self.assertIn("$RESERVED = @(0, 1, 18)", t)
        self.assertIn("rendered_not_disabled", t)
        self.assertIn("rendered_has_triggers", t)
        self.assertIn("readback_mismatch", t)
        self.assertFalse(_SECRET_RE.search(t), "secret-looking literal in the installer")

    def test_real_schtasks_sid_regression_present(self):
        asset = _BUNDLE / "tests" / "Install-GuvfxCopierEmitterOneShotTask.RealSchtasks.Tests.ps1"
        self.assertTrue(asset.exists(), "real-schtasks regression asset missing")
        text = asset.read_text()
        low = text.lower()
        self.assertIn("real windows sid normalization", low, "missing the real-schtasks SID normalization test")
        self.assertIn("resolve-guvfxprincipalsid", low)
        self.assertIn("windowsidentity", low)          # resolves/compares against the runner's real current-user SID
        self.assertIn("schtasks", low)
        # the real test must assert the returned (SID) read-back canonicalizes to the submitted principal
        self.assertIn("rx.Task.Principals.Principal.UserId", text)
        data = asset.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "BOM in real-schtasks asset")
        self.assertTrue(all(b <= 0x7F for b in data), "non-ASCII byte in real-schtasks asset")
        for forbidden in ("COPIER_INGEST_HMAC_KEY", "GUVFX_AGENT_TOKEN", "api.guvfx.com", "100.79.101.19", "guvfx_u_46"):
            self.assertNotIn(forbidden, text, f"real-schtasks asset must not reference {forbidden}")
        self.assertFalse(_SECRET_RE.search(text), "secret-looking literal in real-schtasks asset")


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
        # actual loop SYNTAX (not the English word "do" in a comment) around the single POST
        self.assertNotRegex(window, r"(for|foreach|while)\s*\(|\bdo\s*\{", "no retry loop around the single POST")

    def test_envelope_and_secrets_never_printed(self):
        t = self._t()
        self.assertNotIn("Write-Host", t)
        self.assertNotIn("Write-Output $stdout", t)
        self.assertNotIn("Write-Output $envelopeRaw", t)
        self.assertNotIn("Write-Output $result", t)
        # the only structured output is the redacted ordered dict (no sensitive field in it)
        self.assertIn("Write-RedactedResult", t)
        self.assertIn("reason_code", t)
        self.assertNotIn("Write-RedactedResult $result", t)

    def test_verbatim_envelope_post_no_reserialize(self):
        t = self._t()
        # body = the emitter's ORIGINAL envelope bytes, extracted verbatim - never a deserialize/re-serialize
        self.assertIn("function Get-EnvelopeSubstring", t)
        self.assertIn("$envelopeRaw = Get-EnvelopeSubstring $stdout", t)
        self.assertIn("[System.Text.Encoding]::UTF8.GetBytes($envelopeRaw)", t)
        self.assertIn("-Body $bodyBytes", t)
        self.assertNotIn("$result.envelope | ConvertTo-Json", t, "must not re-serialize the envelope for the body")
        self.assertNotIn("$envelopeJson", t, "the re-serialized body variable must be gone")
        # validation parses a SEPARATE copy (the trimmed stdout), not the submitted body
        self.assertIn("$trimmed | ConvertFrom-Json", t)
        # single-object / no-extra-content guard: reject multiple / prefixed / suffixed stdout before POST
        self.assertIn("single JSON object", t)
        self.assertIn("-is [array]", t)

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


class OneShotKeyProvisionerTests(SimpleTestCase):
    """GFX-COPIER-ONESHOT-KEY-PROVISIONER-112 - the host-local one-shot ingest-key provisioner is the ONLY reviewed
    writer of C:\\GuvFX\\copier-emitter\\oneshot\\ingest.key. It must be: account-46 pinned (#48/Customer Zero/reserved
    refused), human hidden-input ONLY (never argv/env/log/output), an atomic ACL-restricted write (tenant Read + SYSTEM
    + Administrators, inheritance broken, no Users/Everyone/Authenticated-Users), reparse-refusing, min-length-32
    fail-closed, with idempotent -Remove cleanup and a read-only -Verify, and it must perform NO scheduled-task
    mutation. Source-level (mutation-killing) assertions; the behavioural Pester suite runs on a Windows runner."""

    def _t(self):
        return _KEY_PROV.read_text()

    def _param_block(self, t):
        return t[t.index("param("):t.index("$ErrorActionPreference")]

    def test_present_ascii_no_bom_ps51(self):
        self.assertTrue(_KEY_PROV.exists(), "provisioner missing")
        data = _KEY_PROV.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "BOM in provisioner")
        self.assertTrue(all(b <= 0x7F for b in data), "non-ASCII byte in provisioner")
        self.assertIn("#Requires -Version 5.1", self._t())
        self.assertFalse(_SECRET_RE.search(self._t()), "secret-looking literal in provisioner")

    def test_fixed_scope_not_overridable(self):
        t = self._t()
        self.assertIn("$PINNED_ACCOUNT = 46", t)
        self.assertIn("$EXPLICIT_REFUSE = @(48)", t)
        self.assertIn("$RESERVED_IDS = @(0, 1, 18)", t)
        self.assertIn("$TASK_NAME = 'GuvFX_CopierEmitterOneShot_' + $PINNED_ACCOUNT", t)
        self.assertIn("$PRINCIPAL_NAME = 'guvfx_u_' + $PINNED_ACCOUNT", t)
        self.assertIn(r"$KEY_RELATIVE = 'copier-emitter\oneshot\ingest.key'", t)
        self.assertIn(r"$FIXED_ROOT = 'C:\GuvFX'", t)
        # no caller-supplied alternate account(name) / task / path / principal / secret parameter
        head = self._param_block(t)
        for bad in ("$KeyPath", "$KeyFile", "$Path ", "$Destination", "$TaskName", "$Task ",
                    "$Principal", "$PrincipalName", "$UserId", "$Sid ", "$Secret ", "$IngestKey",
                    "$HmacKey", "$Token", "$Password", "$ApiKey"):
            self.assertNotIn(bad, head, f"provisioner must not take {bad} as a parameter")

    def test_account_pin_refusals_present(self):
        t = self._t()
        self.assertIn("Assert-AccountPinned", t)
        for reason in ("account_refused_reserved", "account_refused_explicit", "account_not_pinned"):
            self.assertIn(reason, t)

    def test_hidden_input_only_never_argv_env(self):
        t = self._t()
        self.assertIn("Read-Host -AsSecureString", t)
        self.assertIn("SecureStringToBSTR", t)
        self.assertIn("ZeroFreeBSTR", t)
        self.assertIn("[scriptblock]$ReadSecret = $null", t)       # the only non-interactive source is the test seam
        self.assertNotIn("[Environment]::GetEnvironmentVariable", t)
        self.assertNotIn("$env:COPIER_INGEST_HMAC_KEY", t)

    def test_no_secret_persisted_or_printed(self):
        t = self._t()
        self.assertNotIn("Write-Host", t)
        self.assertNotIn("[Environment]::SetEnvironmentVariable", t)   # no persistent env write
        self.assertNotIn("Set-ItemProperty", t)                       # no registry write
        self.assertNotIn("Export-Clixml", t)
        self.assertNotIn("Start-Transcript", t)
        self.assertNotIn("Set-Content", t)                            # the ONLY writer is the atomic WriteAllText temp
        self.assertNotIn("Out-File", t)
        self.assertIn("Write-Redacted", t)
        for field in ("key_file_present", "length_policy_satisfied", "acl_verified", "reason", "correlation_id"):
            self.assertIn(field, t)
        for leaky in ("key_value", "key_hash", "key_sha", "key_prefix", "key_suffix", "entropy", "byte_dump"):
            self.assertNotIn(leaky, t, f"redacted result must never carry {leaky}")

    def test_atomic_acl_restricted_write(self):
        t = self._t()
        self.assertIn("SetAccessRuleProtection($true, $false)", t)     # break inheritance, do NOT copy inherited ACEs
        self.assertIn("S-1-5-18", t)                                   # SYSTEM
        self.assertIn("S-1-5-32-544", t)                              # Administrators
        self.assertIn("[System.IO.File]::Move(", t)                   # atomic same-volume rename
        # the tenant gets Modify (read + write + delete) so the one-use consumer can shred the key; NOT a bare Read
        self.assertIn("[System.Security.AccessControl.FileSystemRights]::Modify", t)
        # the temp is ACL-restricted BEFORE the secret bytes are written
        acl_idx = t.index("Set-ProtectedAcl $tmp")
        secret_idx = t.index("WriteAllText($tmp, $plain")
        self.assertLess(acl_idx, secret_idx, "temp must be restricted before the secret is written")
        # a directory the wrapper creates is admin-owned with the tenant limited to traverse (ReadAndExecute)
        self.assertIn("Set-ProtectedDirAcl", t)
        self.assertIn("[System.Security.AccessControl.FileSystemRights]::ReadAndExecute", t)
        # no broad principal is ever granted
        for broad in ("'Everyone'", r"'BUILTIN\Users'", "'Authenticated Users'", "S-1-1-0", "S-1-5-11", "S-1-5-32-545"):
            self.assertNotIn(broad, t, f"must not grant a broad principal ({broad})")

    def test_reparse_rechecked_before_write_and_acl_failure_shreds(self):
        t = self._t()
        # the reparse chain is re-verified AFTER the human-input window / dir creation, immediately before the write
        self.assertGreaterEqual(t.count("Test-NoReparseChain"), 3, "reparse chain must be re-checked before the write")
        # a post-write ACL-verification failure SHREDS the secret, never a bare delete
        self.assertIn("Remove-SecretFile $keyPath", t)
        self.assertIn("if (-not $ok) { Remove-SecretFile $keyPath }", t)

    def test_reparse_refusal_and_min_length(self):
        t = self._t()
        self.assertIn("Test-NoReparseChain", t)
        self.assertIn("ReparsePoint", t)
        self.assertIn("reparse_point_detected", t)
        self.assertIn("$MIN_SECRET_LEN = 32", t)
        self.assertIn("secret_too_short", t)
        self.assertIn("key_file_exists", t)                           # existing-file policy: fail closed, no overwrite

    def test_no_scheduled_task_mutation(self):
        t = self._t()
        self.assertIn("/query", t)                                    # the task is READ (schtasks /query /xml) only
        for verb in ("Register-ScheduledTask", "Set-ScheduledTask", "Enable-ScheduledTask",
                     "Unregister-ScheduledTask", "Start-ScheduledTask", "/create", "/change", "/run", "/delete"):
            self.assertNotIn(verb, t, f"provisioner must not mutate the scheduled task ({verb})")

    def test_remove_and_verify_modes(self):
        t = self._t()
        self.assertIn("[switch]$Remove", t)
        self.assertIn("[switch]$Verify", t)
        self.assertIn("'removed'", t)
        self.assertIn("'verified'", t)
        self.assertIn("remove_target_reparse", t)
        self.assertIn("Test-ProtectedAcl", t)
        self.assertIn("$item.Length -ge $MIN_SECRET_LEN", t)          # verify length by SIZE; content never read

    def test_test_seams_default_null_and_gated_in_production(self):
        t = self._t()
        for seam in ("[scriptblock]$ReadTaskXml = $null", "[scriptblock]$ResolvePrincipalSid = $null",
                     "[scriptblock]$ReadSecret = $null", "[scriptblock]$AfterTempForTest = $null",
                     "[string]$FixedRootForTest = ''"):
            self.assertIn(seam, t, f"test seam must default to a no-op: {seam}")
        # the sandbox seam only prepends a root to the FIXED relative suffix (never a full arbitrary path)
        self.assertIn("if ($FixedRootForTest) { $FixedRootForTest } else { $FIXED_ROOT }", t)
        self.assertIn("Join-Path $root $KEY_RELATIVE", t)
        # EVERY seam is refused in production: a seam is honoured only when the self-test signal is set, so the
        # fail-closed contract cannot be bypassed from the command line (defaults alone are not enough).
        self.assertIn("$env:GUVFX_PROVISIONER_SELFTEST -ne '1'", t)
        self.assertIn("test_seam_in_production", t)
        self.assertIn("$seamUsed", t)

    def test_pester_suite_present_and_synthetic(self):
        self.assertTrue(_KEY_PROV_TEST.exists(), "provisioner Pester suite missing")
        data = _KEY_PROV_TEST.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "BOM in provisioner Pester suite")
        self.assertTrue(all(b <= 0x7F for b in data), "non-ASCII in provisioner Pester suite")
        text = _KEY_PROV_TEST.read_text()
        self.assertIn("SYNTHETIC", text.upper())
        self.assertNotIn("api.guvfx.com", text, "the Pester suite must not reference a real endpoint")
        self.assertNotIn("100.79.101.19", text)
        self.assertFalse(_SECRET_RE.search(text), "secret-looking literal in the provisioner Pester suite")

    def test_included_in_governed_evidence_manifest(self):
        self.assertTrue(_KEY_PROV_EVD.exists(), "evidence manifest missing")
        manifest = json.loads(_KEY_PROV_EVD.read_text())
        checks = manifest.get("checksums", {})
        rel = "deploy/copier-emitter/operator/Provision-GuvfxCopierOneShotKey.ps1"
        self.assertIn(rel, checks, "provisioner must be recorded in the governed evidence manifest")
        actual = "sha256:" + hashlib.sha256(_KEY_PROV.read_bytes()).hexdigest()
        self.assertEqual(checks[rel], actual, "provisioner checksum drift vs the evidence manifest")
