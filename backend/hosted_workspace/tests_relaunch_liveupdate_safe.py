"""AJ#6.4 - LiveUpdate-safe tenant relaunch: static-analysis test bar for Relaunch-GuvfxTerminal.ps1.

A PowerShell host artefact carries the same RULE 9 ASCII/BOM/parse hazards and cannot be unit-executed off-host,
so its safety contract is asserted statically (the GuvFX idiom for host .ps1 - see
terminal_provisioning.tests_install_artefacts). Behavioural proof of the update lifecycle is the on-host
certification (AJ#6.4 Phase 11-18). The packet's behavioural cases A-O plus the 8 confirmed adversarial-review
findings are mapped here to enforceable static invariants of the corrected script.
"""
import os
import re
import unittest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PS1 = os.path.join(_REPO, "backend", "terminal_provisioning", "windows", "Relaunch-GuvfxTerminal.ps1")


def _raw() -> bytes:
    with open(_PS1, "rb") as fh:
        return fh.read()


def _text() -> str:
    # Decoding as ASCII is itself the no-non-ASCII assertion (RULE 9 corollary): a stray byte > 127 raises.
    return _raw().decode("ascii")


def _code() -> str:
    """The script with prose stripped (the block <# .. #> header + '#' line comments), so 'must NOT contain'
    checks bind the executable code, never the documentation. No '#' appears inside this script's string
    literals, so first-'#'-to-EOL stripping is exact."""
    t = re.sub(r"<#.*?#>", "", _text(), flags=re.S)
    out = []
    for ln in t.splitlines():
        h = ln.find("#")
        out.append(ln[:h] if h != -1 else ln)
    return "\n".join(out)


class RelaunchLiveUpdateSafeStatic(unittest.TestCase):
    # ---- N. ASCII / no-BOM / LF (RULE 9) --------------------------------------------------------------------
    def test_ascii_no_bom_lf(self):
        raw = _raw()
        self.assertNotEqual(raw[:3], b"\xef\xbb\xbf", "PS1 must not carry a UTF-8 BOM (RULE 9)")
        self.assertEqual(sum(1 for b in raw if b > 127), 0, "PS1 must be ASCII-only (RULE 9 corollary)")
        self.assertNotIn(b"\r\n", raw, "PS1 must use LF line endings")
        _text()

    # ---- O. ParseFile-shaped structural sanity on CODE (real ParseFile runs on the host) --------------------
    def test_code_braces_and_parens_balanced(self):
        c = _code()
        self.assertEqual(c.count("{"), c.count("}"), "unbalanced braces would fail ParseFile")
        self.assertEqual(c.count("("), c.count(")"), "unbalanced parens would fail ParseFile")
        self.assertIn("param(", c)
        self.assertIn("try {", c)
        self.assertIn("catch {", c)

    # ---- server-derived args unchanged (+ the internal -Step switch; no dispatch/runner change) -------------
    def test_param_contract(self):
        t = _text()
        self.assertRegex(t, r"\[Parameter\(Mandatory = \$true\)\]\[string\]\$Username")
        self.assertRegex(t, r"\[Parameter\(Mandatory = \$true\)\]\[string\]\$TerminalRoot")
        self.assertRegex(t, r"\[Parameter\(Mandatory = \$true\)\]\[int\]\$AccountId")
        self.assertRegex(t, r'\[string\]\$Step = ""')

    # ==== CONFIRMED FINDING #1 / #3 (HIGH): reparse/confused-deputy - containment runs as the TENANT ==========
    def test_containment_runs_as_tenant_not_localsystem_inline(self):
        t = _text()
        # There is a tenant-STEP branch, and the orchestrator dispatches containment through a tenant-principal
        # scheduled task (Limited token) rather than purging/Set-Acl-ing a tenant-writable path as LocalSystem.
        self.assertRegex(t, r'if \(\$Step -eq "Contain"\)')
        self.assertIn("GuvFX_HostedContain_", t)
        self.assertRegex(t, r"-Argument \$argLine")
        self.assertRegex(t, r"-Step Contain", "containment must be dispatched via the tenant self-invocation")
        # The Deny/purge/Set-Acl live ONLY inside the Contain step, never in the orchestrator body.
        step_start = t.index('if ($Step -eq "Contain")')
        orch_start = t.index("ORCHESTRATOR")
        step_block = t[step_start:orch_start]
        orch_block = t[orch_start:]
        self.assertIn("Set-Acl -LiteralPath", step_block)
        self.assertNotIn("Set-Acl", orch_block, "the LocalSystem orchestrator must never Set-Acl a tenant path")
        self.assertNotIn("Remove-Item -LiteralPath", orch_block, "orchestrator must never recursively delete a tenant path")

    def test_reparse_point_rejected_fail_closed(self):
        t = _text()
        self.assertIn("[System.IO.FileAttributes]::ReparsePoint", t, "must refuse a reparse point / junction")
        # A reparse-point child is deleted as a LINK, never recursed through.
        self.assertRegex(t, r"ReparsePoint\) -ne 0\) \{ \$c\.Delete\(\)")

    # ==== CONFIRMED FINDING #4 / #8 (MEDIUM): no NTAccount->SID name translation (host hang) =================
    def test_no_ntaccount_name_translation(self):
        c = _code()
        self.assertNotIn("NTAccount", c, "NTAccount name->SID translation can hang on this workgroup host")
        # SID comes from the tenant's own token inside the tenant step.
        self.assertIn("[System.Security.Principal.WindowsIdentity]::GetCurrent()).User", _text())

    # ==== CONFIRMED FINDING #6 (MEDIUM): real profile via $env:APPDATA, not a reconstructed C:\Users path ====
    def test_real_profile_via_appdata_not_reconstructed(self):
        t = _text()
        self.assertIn("$env:APPDATA", t, "containment must use the tenant's real roaming profile")
        self.assertNotRegex(_code(), r'Join-Path "C:\\Users"', "must not reconstruct the tenant profile path")

    # ==== CONFIRMED FINDING #2 / #5 (MEDIUM): detection by PID / canonical path, not GetOwner recompute ======
    def test_close_detection_by_tracked_pid_not_owner(self):
        t = _text()
        # Close is confirmed by the tracked PIDs disappearing (Get-Process -Id), never by recomputing owner.
        self.assertRegex(t, r"foreach \(\$tp in \$tradingPids\) \{ if \(Get-Process -Id \$tp")
        self.assertNotIn("GetOwner", _code(), "must not depend on GetOwner for attribution/detection")

    def test_trading_identified_by_canonical_executable_path(self):
        t = _text()
        self.assertIn("function Get-TenantTradingPids", t)
        self.assertRegex(t, r"\$exe\.ToLower\(\) -eq \$expectedExe\.ToLower\(\)",
                         "trading terminal is the canonical (tenant-specific) exe path")

    # ==== CONFIRMED FINDING #7 (MEDIUM): no over-claimed 'guarantee'; final trading check is the gate ========
    def test_no_overclaimed_guarantee(self):
        c = _code()
        self.assertNotIn("guarantees terminal64", c)
        self.assertNotIn("guarantee", c.lower().replace("guaranteed", ""))  # comments in _text() only

    # ---- the corrective: certified Variant-A LiveUpdate containment (WebInstall deny + read-back) -----------
    def test_liveupdate_containment_present_and_verified(self):
        t = _text()
        self.assertIn("WebInstall", t, "must deny the load-bearing WebInstall staging path")
        self.assertRegex(t, r"AccessControlType\]::Deny", "must apply a Deny ACE")
        self.assertIn("FileSystemRights]::Write", t, "Deny must carry the Write right")
        self.assertIn("GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])", t)
        # Containment verdict is the tenant task's own exit code, checked fail-closed before relaunch.
        self.assertIn("LastTaskResult -ne 0) { Fail \"containment_failed\"", t)
        self.assertIn("containment_task_did_not_run", t)
        self.assertLess(t.index("GuvFX_HostedContain_"), t.index("GuvFX_HostedRelaunch_"),
                        "containment must complete before relaunch")

    def test_kills_stuck_updater_before_purge(self):
        t = _text()
        step = t[t.index('if ($Step -eq "Contain")'):t.index("ORCHESTRATOR")]
        self.assertLess(step.index("Stop-Process -Id"), step.index("Set-Acl -LiteralPath"),
                        "the stuck updater must be killed before the containment purge/Deny")
        self.assertRegex(step, r"Stop-Process -Id \(\[int\]\$p\.ProcessId\) -Force")

    # ---- A + G. tenant-specific process matching; NEVER a broad image-name kill (checked on CODE) -----------
    def test_no_taskkill_by_image_name(self):
        c = _code()
        self.assertNotIn("/IM", c, "taskkill /IM is a shared kill - forbidden; matching must be tenant-specific")
        for m in re.finditer(r"taskkill\.exe", c):
            self.assertNotIn("/IM", c[m.start():m.start() + 200])
        self.assertIn("/PID", c, "close targets explicit PIDs")

    def test_close_is_graceful_no_force_flag(self):
        self.assertNotRegex(_code(), r"taskkill[^\n]*\s/F\b", "close must be graceful (no /F)")

    # ---- F + L. success only on the trading terminal; updater never accepted --------------------------------
    def test_success_only_on_trading_terminal_never_updater(self):
        t = _text()
        self.assertRegex(t, r"Get-TenantTradingPids \$expectedExe\)\.Count -ge 1\) \{ \$result\.relaunched")
        self.assertRegex(t, r"\$result\.ok = \$true")
        self.assertIn("relaunch_hit_liveupdate", t)

    # ---- H. Customer Zero refused ---------------------------------------------------------------------------
    def test_customer_zero_refused(self):
        t = _text()
        self.assertRegex(t, r"\$RESERVED_ACCOUNT_IDS\s*=\s*@\(1\)")
        self.assertRegex(t, r"\$RESERVED_ACCOUNT_IDS\s*-contains\s*\$AccountId")
        self.assertIn("refusing_reserved_identity", t)

    # ---- I. an account-24 invocation cannot affect account 18 (confinement) --------------------------------
    def test_confinement_binds_identity_and_path(self):
        t = _text()
        self.assertRegex(t, r'\$Username\s*-ne\s*\("guvfx_u_"\s*\+\s*\$AccountId\)')
        self.assertIn("refusing_username_mismatch", t)
        self.assertIn("refusing_terminal_root_mismatch", t)
        self.assertIn("refusing_path_traversal", t)

    # ---- D. bounded waits only ------------------------------------------------------------------------------
    def test_bounded_waits(self):
        t = _text()
        for const in ("CLOSE_TIMEOUT_S", "RELAUNCH_TIMEOUT_S", "CONTAIN_TIMEOUT_S"):
            self.assertRegex(t, r"\$" + const + r"\s*=\s*\d+")
            self.assertIn("(Get-Date).AddSeconds($" + const + ")", t)
        self.assertNotRegex(t, r"while\s*\(\s*\$true\s*\)")

    # ---- E + M. stable structured failure reasons ----------------------------------------------------------
    def test_stable_failure_reasons(self):
        t = _text()
        for reason in ("relaunch_hit_liveupdate", "trading_terminal_not_restored", "containment_failed",
                       "containment_task_did_not_run", "close_timeout", "refusing_reserved_identity",
                       "self_path_unresolved", "relaunch_exception"):
            self.assertIn(reason, t, f"missing stable failure reason: {reason}")
        self.assertIn("function Fail(", t)
        self.assertIn("exit 1", t)
        self.assertRegex(t, r"exit 0")

    # ---- J + K. idempotent; no duplicate terminal (close-before-launch, single launch) ---------------------
    def test_close_before_launch_single_relaunch(self):
        t = _text()
        self.assertLess(t.index("GuvFX_HostedClose_"), t.index("GuvFX_HostedRelaunch_"),
                        "must close before relaunch so no duplicate trading terminal is created")
        self.assertEqual(t.count('New-ScheduledTaskAction -Execute $exe -Argument "/portable"'), 1)
        self.assertIn("$acl.RemoveAccessRule($deny)", t)
        self.assertIn("$acl.AddAccessRule($deny)", t)

    # ---- L. no login / no order / no broker-credential capability (checked on CODE) ------------------------
    def test_no_login_or_order_capability(self):
        c = _code().lower()
        for forbidden in ("order_send", "ordersend", "/login", "-password", "password=", "placeorder",
                          "order.send", "convertto-securestring", ".login("):
            self.assertNotIn(forbidden, c, f"relaunch must not carry login/order/credential capability: {forbidden}")
        # The only quoted executables it runs are taskkill.exe and the FULL-PATH Windows PowerShell (the tenant
        # self-invocation, full path to defeat search-order hijack); the relaunch runs terminal64 via $exe.
        allowed = ("c:\\windows\\system32\\taskkill.exe",
                   "c:\\windows\\system32\\windowspowershell\\v1.0\\powershell.exe")
        for m in re.finditer(r'-Execute\s+"([^"]+)"', _code()):
            self.assertIn(m.group(1).lower(), allowed, f"unexpected quoted executable: {m.group(1)}")

    def test_powershell_invoked_by_full_path(self):
        # LOW-hardening: the contain task must not invoke a bare 'powershell.exe' (binary search-order hijack).
        self.assertNotRegex(_code(), r'-Execute\s+"powershell\.exe"')
        self.assertIn(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", _text())

    def test_relaunch_retry_bounded(self):
        # MEDIUM-mitigation: the relaunch is retried at most once (transient launch no-op), never an unbounded
        # loop, and a seen updater breaks out fail-closed (no retry on a containment breach).
        t = _text()
        self.assertRegex(t, r"while \(\$attempt -lt 2 -and \(-not \$result\.relaunched\) -and \(-not \$sawUpdater\)\)")
        self.assertRegex(t, r"Any-TenantUpdater \$Username\) \{ \$sawUpdater = \$true; break \}")


if __name__ == "__main__":
    unittest.main()
