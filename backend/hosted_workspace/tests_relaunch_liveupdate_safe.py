"""AJ#6.4 - LiveUpdate-safe tenant relaunch: static-analysis test bar for Relaunch-GuvfxTerminal.ps1.

A PowerShell host artefact carries the same RULE 9 ASCII/BOM/parse hazards and cannot be unit-executed off-host,
so its safety contract is asserted statically (the GuvFX idiom for host .ps1 - see
terminal_provisioning.tests_install_artefacts). Behavioural proof of the update lifecycle is the on-host
certification (AJ#6.4 Phase 11-18). The packet's behavioural cases A-O plus the adversarial-review findings and
the host AppLocker reality (the tenant cannot run PowerShell, so containment runs as LocalSystem with
reparse-rejection) are mapped here to enforceable static invariants.
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
    return _raw().decode("ascii")  # decoding as ASCII is itself the no-non-ASCII assertion (RULE 9)


def _code() -> str:
    """The script with prose stripped (block <# .. #> header + '#' line comments) so 'must NOT contain' checks
    bind the executable code, never the documentation. No '#' appears inside this script's string literals."""
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

    def test_param_contract(self):
        t = _text()
        self.assertRegex(t, r"\[Parameter\(Mandatory = \$true\)\]\[string\]\$Username")
        self.assertRegex(t, r"\[Parameter\(Mandatory = \$true\)\]\[string\]\$TerminalRoot")
        self.assertRegex(t, r"\[Parameter\(Mandatory = \$true\)\]\[int\]\$AccountId")
        self.assertNotIn("$Step", t, "no tenant self-invocation switch (AppLocker blocks tenant PowerShell)")

    # ==== FINDING #1/#3 (HIGH confused-deputy): LocalSystem containment, reparse-rejection ==================
    def test_containment_reparse_safe(self):
        t = _text()
        self.assertIn("function Apply-LiveUpdateContainment", t)
        self.assertIn("function Test-ChainReparseFree", t)
        # Every target is chain-checked for reparse points before it is touched.
        self.assertRegex(t, r"if \(-not \(Test-ChainReparseFree \$tenant\.profile \$t\)\) \{ return \$false \}")
        # Reparse-point child is removed as a LINK (never recursed through).
        self.assertRegex(t, r"ReparsePoint\) -ne 0\) \{ \$c\.Delete\(\)")
        # Re-check immediately before the DACL mutate (shrink TOCTOU).
        self.assertRegex(t, r"ReparsePoint\) -ne 0\) \{ return \$false \}\s*\n\s*\$acl = Get-Acl")
        # The orchestrator body (from its confinement block onward) never Set-Acls a tenant path - only the
        # Apply-LiveUpdateContainment helper does, and that is fully reparse-guarded.
        orch = t[t.index("# ---- Confinement"):]
        self.assertNotIn("Set-Acl", orch)
        self.assertNotIn("Remove-Item -LiteralPath", orch)

    # ==== FINDING #4/#8 (MEDIUM): no NTAccount->SID name translation (host hang) =============================
    def test_no_ntaccount_name_translation(self):
        c = _code()
        self.assertNotIn("NTAccount", c, "NTAccount name->SID translation can hang on this workgroup host")
        self.assertIn("Get-CimInstance Win32_UserAccount", _text())
        self.assertIn("New-Object System.Security.Principal.SecurityIdentifier($tenant.sid)", _text())

    # ==== FINDING #6 (MEDIUM): real profile via ProfileList, not a reconstructed C:\Users path ===============
    def test_real_profile_via_profilelist(self):
        t = _text()
        self.assertIn("ProfileList", t, "must resolve the real profile via the authoritative ProfileList key")
        self.assertIn("ProfileImagePath", t)
        self.assertNotRegex(_code(), r'Join-Path\s+"C:\\Users"', "must not reconstruct the tenant profile path")
        self.assertIn("tenant_resolution_failed", t)

    # ==== FINDING #2/#5 (MEDIUM): detection by PID / canonical path, not GetOwner recompute ==================
    def test_close_detection_by_tracked_pid_not_owner(self):
        t = _text()
        self.assertRegex(t, r"foreach \(\$tp in \$tradingPids\) \{ if \(Get-Process -Id \$tp")
        self.assertNotIn("GetOwner", _code(), "must not depend on GetOwner for attribution/detection")

    def test_trading_identified_by_canonical_executable_path(self):
        t = _text()
        self.assertIn("function Get-TenantTradingPids", t)
        self.assertRegex(t, r"\$exe\.ToLower\(\) -eq \$expectedExe\.ToLower\(\)")

    # ==== FINDING #7 (MEDIUM): no over-claimed 'guarantee' ==================================================
    def test_no_overclaimed_guarantee(self):
        self.assertNotIn("guarantees terminal64", _code())

    # ---- containment content: certified WebInstall deny + read-back + kill-before-purge --------------------
    def test_containment_content(self):
        t = _text()
        self.assertIn("WebInstall", t)
        self.assertRegex(t, r"AccessControlType\]::Deny")
        self.assertIn("FileSystemRights]::Write", t)
        self.assertIn("GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])", t)
        # LiveUpdate containment completes before the relaunch task.
        self.assertLess(t.index("Apply-LiveUpdateContainment $tenant"), t.index("GuvFX_HostedRelaunch_"))

    def test_kills_stuck_updater_before_purge(self):
        t = _text()
        body = t[t.index("function Apply-LiveUpdateContainment"):t.index("function Get-TenantTradingPids")]
        self.assertLess(body.index("Stop-Process -Id"), body.index("Set-Acl -LiteralPath"),
                        "the stuck updater must be killed before the containment purge/Deny")
        self.assertRegex(body, r"Stop-Process -Id \(\[int\]\$p\.ProcessId\) -Force")

    # ---- A + G. tenant-specific process matching; NEVER a broad image-name kill ----------------------------
    def test_no_taskkill_by_image_name(self):
        c = _code()
        self.assertNotIn("/IM", c)
        for m in re.finditer(r"taskkill\.exe", c):
            self.assertNotIn("/IM", c[m.start():m.start() + 200])
        self.assertIn("/PID", c)

    def test_close_is_graceful_no_force_flag(self):
        self.assertNotRegex(_code(), r"taskkill[^\n]*\s/F\b")

    # ---- F. success only on the trading terminal; updater never accepted -----------------------------------
    def test_success_only_on_trading_terminal_never_updater(self):
        t = _text()
        self.assertRegex(t, r"Get-TenantTradingPids \$expectedExe\)\.Count -ge 1\) \{ \$result\.relaunched")
        self.assertRegex(t, r"\$result\.ok = \$true")
        self.assertIn("relaunch_hit_liveupdate", t)

    # ---- MEDIUM-mitigation: bounded single relaunch retry, fail-closed on a seen updater --------------------
    def test_relaunch_retry_bounded(self):
        t = _text()
        self.assertRegex(t, r"while \(\$attempt -lt 2 -and \(-not \$result\.relaunched\) -and \(-not \$sawUpdater\)\)")
        self.assertRegex(t, r"Any-TenantUpdater \$tenant\.profile\) \{ \$sawUpdater = \$true; break \}")

    # ---- H. SACRED identities refused: Customer Zero (1) AND the account-18 control ------------------------
    def test_reserved_identities_refused(self):
        t = _text()
        self.assertRegex(t, r"\$RESERVED_ACCOUNT_IDS\s*=\s*@\(1, 18\)",
                         "both Customer Zero (1) and account 18 must be reserved (SACRED)")
        self.assertRegex(t, r"\$RESERVED_ACCOUNT_IDS\s*-contains\s*\$AccountId")
        self.assertIn("refusing_reserved_identity", t)

    def test_account18_reserved_in_recovery_candidate_query(self):
        # The PRIMARY guard: account 18 is excluded from recovery candidacy so neither apply_autotrading_config
        # nor relaunch_terminal is ever called for it (the .ps1 refusal is only defence in depth).
        cap = os.path.join(_REPO, "backend", "hosted_workspace", "capability_recovery.py")
        with open(cap, encoding="utf-8") as fh:
            src = fh.read()
        self.assertRegex(src, r"_RESERVED_ACCOUNT_IDS\s*=\s*frozenset\(\{1, 18\}\)")

    # ---- I. account-24 invocation cannot affect account 18 (confinement) -----------------------------------
    def test_confinement_binds_identity_and_path(self):
        t = _text()
        self.assertRegex(t, r'\$Username\s*-ne\s*\("guvfx_u_"\s*\+\s*\$AccountId\)')
        self.assertIn("refusing_username_mismatch", t)
        self.assertIn("refusing_terminal_root_mismatch", t)
        self.assertIn("refusing_path_traversal", t)

    # ---- D. bounded waits only ------------------------------------------------------------------------------
    def test_bounded_waits(self):
        t = _text()
        for const in ("CLOSE_TIMEOUT_S", "RELAUNCH_TIMEOUT_S"):
            self.assertRegex(t, r"\$" + const + r"\s*=\s*\d+")
            self.assertIn("(Get-Date).AddSeconds($" + const + ")", t)
        self.assertNotRegex(t, r"while\s*\(\s*\$true\s*\)")

    # ---- E + M. stable structured failure reasons ----------------------------------------------------------
    def test_stable_failure_reasons(self):
        t = _text()
        for reason in ("relaunch_hit_liveupdate", "trading_terminal_not_restored", "containment_failed",
                       "tenant_resolution_failed", "close_timeout", "refusing_reserved_identity",
                       "relaunch_exception"):
            self.assertIn(reason, t, f"missing stable failure reason: {reason}")
        self.assertIn("function Fail(", t)
        self.assertIn("exit 1", t)
        self.assertRegex(t, r"exit 0")

    # ---- J + K. idempotent; no duplicate terminal (close-before-launch, single launch) ---------------------
    def test_close_before_launch_single_relaunch(self):
        t = _text()
        self.assertLess(t.index("GuvFX_HostedClose_"), t.index("GuvFX_HostedRelaunch_"))
        self.assertEqual(t.count('New-ScheduledTaskAction -Execute $exe -Argument "/portable"'), 1)
        self.assertIn("$acl.RemoveAccessRule($deny)", t)
        self.assertIn("$acl.AddAccessRule($deny)", t)

    # ---- L. no login / no order / no broker-credential capability; only taskkill.exe is a quoted exec ------
    def test_no_login_or_order_capability(self):
        c = _code().lower()
        for forbidden in ("order_send", "ordersend", "/login", "-password", "password=", "placeorder",
                          "order.send", "convertto-securestring", ".login("):
            self.assertNotIn(forbidden, c, f"must not carry login/order/credential capability: {forbidden}")
        for m in re.finditer(r'-Execute\s+"([^"]+)"', _code()):
            self.assertEqual(m.group(1).lower(), "c:\\windows\\system32\\taskkill.exe",
                             f"the only quoted executable must be taskkill.exe: {m.group(1)}")


if __name__ == "__main__":
    unittest.main()
