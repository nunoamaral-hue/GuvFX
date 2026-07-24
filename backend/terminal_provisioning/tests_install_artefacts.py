"""CVM-Inc-3 B3P-2 — install artefact conformance (install-only review F1, F2, F4, F5).

**These scripts cannot be executed here.** They run PowerShell against a Windows host, and no host contact
is authorised. What CAN be checked off-host is conformance: that they target the approved per-slot
namespace, that they refuse the operator's estate, that they never take a password as a parameter, and that
they are dry-run by default. That is precisely the class of defect finding F1 was — scripts that still
described the previous architecture — so it is worth a mechanical check rather than a reading.
"""
import os
import re

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
SCRIPTS = ("install_pool.ps1", "install_service.ps1", "uninstall.ps1", "firewall.ps1")
#: The ADR-0016 launch wrapper. It is NOT an install script (no dry-run/apply, no password prompt), but it is
#: a PowerShell artefact executed on the host, so it carries the same RULE 9 ASCII/BOM/parse hazards.
WRAPPER = "slot_launch.ps1"


def _read(name):
    return open(os.path.join(_BUNDLE, name), encoding="utf-8").read()


def _code(name):
    """Script source with comment lines stripped.

    Conformance checks must test what a script DOES, not what it says: a comment explaining that the legacy
    layout was replaced legitimately names that layout, and should not read as a use of it.
    """
    return "\n".join(line for line in _read(name).splitlines()
                      if not line.lstrip().startswith("#"))


class DryRunByDefaultTests(SimpleTestCase):
    def test_every_script_is_dry_run_until_apply(self):
        for name in SCRIPTS:
            source = _read(name)
            self.assertIn("[switch]$Apply", source, name)
            self.assertNotIn("$Apply = $true", source, f"{name} defaults to applying")

    def test_every_script_stops_on_error(self):
        for name in SCRIPTS:
            self.assertIn('$ErrorActionPreference = "Stop"', _read(name), name)


class NoPasswordParametersTests(SimpleTestCase):
    """A password on a command line reaches the process listing, the shell history and any transcript."""

    def test_no_script_accepts_a_password_parameter(self):
        for name in SCRIPTS:
            source = _read(name)
            params = source.split(")", 1)[0] if "param(" in source else ""
            block = source[source.index("param("):source.index(")\n", source.index("param("))] \
                if "param(" in source else ""
            for forbidden in ("$Password", "$Pw", "$Credential", "$PlainPassword"):
                self.assertNotIn(forbidden, block, f"{name} takes {forbidden} as a parameter")

    def test_passwords_are_prompted_as_securestring(self):
        source = _read("install_pool.ps1")
        self.assertIn("Read-Host -AsSecureString", source)
        self.assertIn("Remove-Variable", source)      # the plaintext is not left lying in scope

    def test_no_literal_password_appears_anywhere(self):
        for name in SCRIPTS:
            source = _read(name)
            self.assertNotRegex(source, r'-Password\s+"[^"$]', f"{name} has a literal password")


class PoolNamespaceTests(SimpleTestCase):
    """F1: the artefacts must target the per-slot layout, not the legacy uuid-directory one."""

    def test_the_legacy_layout_is_gone(self):
        for name in ("install_service.ps1", "uninstall.ps1"):
            self.assertNotIn(r"beta\accounts", _code(name), f"{name} still targets the B2 layout")

    def test_the_pool_layout_is_present(self):
        for name in ("install_pool.ps1", "install_service.ps1", "uninstall.ps1"):
            self.assertIn(r"C:\GuvFX\beta\slots", _code(name), name)

    def test_identity_prefix_matches_the_code(self):
        import sys
        if _BUNDLE not in sys.path:
            sys.path.insert(0, _BUNDLE)
        import win_primitives as wp
        self.assertIn(wp.RUNTIME_IDENTITY_PREFIX, _read("install_pool.ps1"))
        self.assertIn(f'$IdentityPrefix -ne "{wp.RUNTIME_IDENTITY_PREFIX}"', _read("install_pool.ps1"))

    def test_task_names_match_the_code(self):
        source = _read("install_pool.ps1")
        self.assertIn('"GuvFXBetaRuntime-"', source)
        self.assertIn('"GuvFXBetaRuntimeStop-"', source)


class EstateRefusalTests(SimpleTestCase):
    """The operator's estate must be refused by construction, not avoided by convention."""

    ESTATE_TASKS = ("GuvFX_Autostart", "GuvFX_SignalBridge", "GuvFX_BridgeWatchdog",
                    "GuvFX_LaunchMT5", "GFX_LaunchIS6")

    def test_the_pool_installer_refuses_estate_paths(self):
        source = _read("install_pool.ps1")
        self.assertIn(r'"C:\GuvFX\accounts","C:\GuvFX\terminals"', source.replace(" ", ""))
        self.assertIn("refusing:", source)

    def test_the_pool_installer_refuses_estate_task_names(self):
        source = _read("install_pool.ps1")
        for task in self.ESTATE_TASKS:
            self.assertIn(task, source, task)

    def test_no_script_touches_the_reserved_ports(self):
        """8788 is the trade bridge, 8787 the backtest agent, 3389 RDP."""
        for name in ("install_pool.ps1", "install_service.ps1", "uninstall.ps1"):
            source = _code(name)
            for port in ("8788", "8787", "3389"):
                self.assertNotRegex(source, rf"LocalPort\s+{port}|-Port\s+{port}", f"{name}:{port}")

    def test_the_firewall_script_only_opens_the_agent_port(self):
        source = _read("firewall.ps1")
        self.assertIn("[int]$Port                 = 8791", source)
        self.assertIn("100.119.23.29", source)          # backend only
        self.assertIn("DefaultInboundAction", source)   # default-deny asserted


class InstallOnlyTests(SimpleTestCase):
    """The whole point of the gate: create objects, then stop."""

    # Every way PowerShell/Windows can start a service, so a first-start wiring attempt is caught here and
    # not on the live host: Start-Service, Restart-Service, Set-Service -Status Running, the SCM `sc start`,
    # the ServiceController .Start() method, and the WinSW wrapper's own `<exe> start`.
    START_FORMS = (r"Start-Service|Restart-Service|Set-Service\b[^\n]*-Status\s+Running|"
                   r"sc\.exe\s+start\b|\.Start\(|\$ServiceExe\s+start|service\.py.*['\"]start['\"]")

    def test_nothing_starts_the_service(self):
        for name in ("install_pool.ps1", "install_service.ps1"):
            source = _code(name)
            self.assertNotRegex(source, self.START_FORMS, name)

    def test_tasks_are_registered_disabled_and_never_enabled(self):
        source = _code("install_pool.ps1")
        self.assertIn("Disable-ScheduledTask", source)
        self.assertNotIn("Enable-ScheduledTask", source)
        self.assertNotIn("Start-ScheduledTask", source)

    def test_no_task_is_triggered_and_no_process_launched(self):
        source = _code("install_pool.ps1")
        for forbidden in ("Start-ScheduledTask", "Start-Process", "Invoke-Item"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_service_install_asserts_it_is_stopped(self):
        source = _read("install_service.ps1")
        # WinSW sets the start mode from its XML (Manual), so there is no `sc create ... start= demand`;
        # the install-only guarantee is now the post-install state assertion plus the Manual/Disabled check.
        self.assertIn('if ($svc.Status -ne "Stopped")', source)
        self.assertIn('$ci.StartMode -notin @("Manual","Disabled")', source)
        # and the WinSW config itself must declare manual start (no autostart)
        self.assertIn("<startmode>Manual</startmode>", _read("winsw/GuvFXBetaAgent.xml"))

    def test_the_service_install_refuses_localsystem(self):
        """The identity check is the review's whole position on elevation, expressed as a hard failure.

        HOST-PROVEN 2026-07-24: WinSW v2.12.0 installs LocalSystem regardless of <serviceaccount>. Identity is
        assigned post-install by `sc config obj=` (validated) + an LSA SeServiceLogonRight grant; the -Apply
        verify then THROWS unless StartName is EXACTLY the NT SERVICE virtual account (no LocalSystem fallback).
        See ADR 0013."""
        source = _read("install_service.ps1")
        self.assertIn("NT SERVICE\\GuvFXBetaAgent", source)
        self.assertIn("no LocalSystem fallback; do NOT start", source)
        self.assertIn('"$($ci.StartName)" -ne $RunAsUser', source)   # EXACT match, not substring
        # the pywin32 SERVICE-HOST mechanism must still be gone (service.py / pythonservice / postinstall);
        # sc config obj= is now a SUPPORTED post-install identity step, not the pywin32 path.
        code = _code("install_service.ps1")
        for gone in ("service.py", "pythonservice", "pywin32_postinstall"):
            self.assertNotIn(gone, code, f"install_service.ps1 still references the pywin32 host path: {gone}")

    def test_the_service_install_requires_the_pool_first(self):
        self.assertIn("run install_pool.ps1 -Apply FIRST", _read("install_service.ps1"))


class GoldenImageTests(SimpleTestCase):
    def test_per_instance_state_is_refused_in_the_golden_image(self):
        """Inheriting one runtime's broker login into every slot is the failure this prevents."""
        source = _read("install_pool.ps1")
        for leak in ("accounts.dat", "servers.dat", "MQL5\\Logs", "MQL5\\Profiles"):
            self.assertIn(leak, source, leak)

    def test_required_markers_are_asserted(self):
        source = _read("install_pool.ps1")
        self.assertIn(".guvfx_golden_manifest", source)
        self.assertIn(".guvfx_portable", source)


class ApprovedTaskEmissionTests(SimpleTestCase):
    """F3: the installer emits the definitions the agent's launch gate asserts against."""

    def test_the_installer_writes_the_approved_definitions(self):
        source = _read("install_pool.ps1")
        self.assertIn("approved_tasks.json", source)
        self.assertIn("ConvertTo-Json", source)

    def test_the_emitted_fields_match_the_code(self):
        import sys
        if _BUNDLE not in sys.path:
            sys.path.insert(0, _BUNDLE)
        import config as agent_config
        from occupancy import TASK_IDENTITY_FIELDS
        self.assertEqual(set(agent_config.APPROVED_TASK_FIELDS), set(TASK_IDENTITY_FIELDS))
        source = _read("install_pool.ps1")
        for field in TASK_IDENTITY_FIELDS:
            self.assertRegex(source, rf"{field}\s+=", field)


class UninstallCompletenessTests(SimpleTestCase):
    """F4: the B2 teardown left the terminate tasks - and their stored credentials - behind."""

    def test_both_task_families_are_removed(self):
        source = _read("uninstall.ps1")
        self.assertIn("$LaunchTaskPrefix", source)
        self.assertIn("$StopTaskPrefix", source)
        self.assertIn("foreach ($prefix in @($LaunchTaskPrefix, $StopTaskPrefix))", source)

    def test_the_logon_right_is_revoked(self):
        self.assertIn("SeBatchLogonRight", _read("uninstall.ps1"))

    def test_identities_are_disabled_not_deleted_by_default(self):
        source = _read("uninstall.ps1")
        self.assertIn("Disable-LocalUser", source)
        self.assertIn("[switch]$RemoveIdentities", source)
        self.assertIn("if ($RemoveIdentities)", source)

    def test_evidence_bearing_state_is_retained(self):
        source = _read("uninstall.ps1")
        self.assertIn("RETAINED (never deleted)", source)
        self.assertIn("evidence chain", source)

    def test_the_estate_is_declared_untouched(self):
        self.assertIn("UNTOUCHED: Nuno's terminal", _read("uninstall.ps1"))


class StopTaskContainmentTests(SimpleTestCase):
    """The single highest-risk line in the install: the task that terminates a runtime.

    The operator's production terminal has the SAME image name as a slot's, because a slot is a copy of the
    same MT5 image. The agent's own code refuses to match a process by name for exactly this reason; the
    task the agent triggers must not do what the agent is forbidden to do.
    """

    def test_the_stop_task_does_not_match_by_image_name(self):
        source = _code("install_pool.ps1")
        self.assertNotIn("taskkill", source)
        self.assertNotIn("/IM terminal64.exe", source)

    def test_the_stop_task_filters_on_the_full_image_path(self):
        source = _code("install_pool.ps1")
        self.assertIn("$_.Path -eq '$exe'", source)
        self.assertIn("Stop-Process -Force", source)

    def test_the_slot_executable_path_is_slot_scoped(self):
        source = _code("install_pool.ps1")
        self.assertIn('$exe    = Join-Path $work "terminal64.exe"', source)
        self.assertIn('$work   = Join-Path $slot "terminal"', source)


class LauncherProvisioningTests(SimpleTestCase):
    """ADR-0016: install_pool.ps1 stages the launch wrapper into an admin-only directory and rewires the
    launch task to run it AS the slot identity."""

    def test_the_launch_task_runs_powershell_against_the_wrapper_via_file(self):
        source = _code("install_pool.ps1")
        self.assertIn('New-ScheduledTaskAction -Execute "powershell.exe"', source)
        self.assertIn('-File "{0}"', source)          # the wrapper path, formatted into the args
        self.assertIn("-TerminalPath", source)
        self.assertIn("-GranteeSid", source)
        # -File only: no inline command in the launch action.
        self.assertNotIn('-Command `"$launch', source)

    def test_the_launch_task_no_longer_executes_terminal64_directly(self):
        # The whole point of ADR-0016: the launch action is powershell+wrapper, not the bare exe.
        source = _code("install_pool.ps1")
        self.assertNotIn('New-ScheduledTaskAction -Execute $exe', source)

    def test_the_grantee_service_sid_is_computed_before_the_launch_task_is_registered(self):
        # The wrapper argument needs the service SID; it must be computed (sc.exe showsid) BEFORE section-6
        # registration, not after (the value is deterministic before the service exists).
        source = _code("install_pool.ps1")
        self.assertIn("Get-GuvfxServiceSidValue", source)
        self.assertIn("sc.exe showsid", source)
        self.assertLess(source.index("$ServiceSidValue = Get-GuvfxServiceSidValue"),
                        source.index('New-ScheduledTaskAction -Execute "powershell.exe"'))

    def test_the_launcher_dir_is_admin_only_write_slots_read_execute(self):
        source = _code("install_pool.ps1")
        # Inheritance broken, Administrators + SYSTEM Full, each slot ReadAndExecute; NO slot Modify/write.
        self.assertIn('Invoke-GuvfxIcacls $LauncherDir @("/inheritance:r")', source)
        self.assertIn('*S-1-5-32-544:(OI)(CI)F', source)
        self.assertIn('{0}{1}:(OI)(CI)RX" -f $IdentityPrefix, $n', source)
        self.assertNotIn('$LauncherDir @("/grant", ("{0}{1}:(OI)(CI)M"', source)

    def test_the_wrapper_is_hash_pinned_before_and_after_staging(self):
        source = _code("install_pool.ps1")
        self.assertIn("$LaunchWrapperSha256", source)
        # Refuse a mismatched bundle wrapper BEFORE staging, and re-hash the staged copy AFTER (WinSW pattern).
        self.assertIn("refusing to stage a wrapper that is not the reviewed one", source)
        self.assertIn("staged wrapper hash mismatch after copy", source)

    def test_the_install_asserts_the_launch_args_scope_at_readback(self):
        # The approved-task read-back must assert the launch args name the fixed wrapper, this slot's
        # terminal64, and the service SID (mirroring the terminate-scope asserts).
        source = _code("install_pool.ps1")
        self.assertIn("does not invoke the fixed wrapper via -File", source)
        self.assertIn("does not target this slot's own terminal64", source)
        self.assertIn("does not carry the beta-agent service SID", source)

    def test_verify_reads_back_the_launcher_acl_and_rehashes_the_wrapper(self):
        # The launcher is executed as the slot identity every launch and is never re-hashed at runtime, so the
        # -VerifyOnly integrity gate MUST inspect it: read the launcher DACL back (protected + no slot-writable
        # ACE) and re-hash the staged wrapper against the pin. Runs in $Check (both -Apply and -VerifyOnly).
        source = _code("install_pool.ps1")
        self.assertIn("$launcherAcl = Get-Acl $LauncherDir", source)
        self.assertIn("launcher '$LauncherDir' still inherits", source)
        self.assertIn("a slot must never rewrite its own launcher", source)
        self.assertIn("staged launch wrapper hash", source)
        # It is inside the VERIFY ($Check) section, so -VerifyOnly exercises it (the sole pre-commissioning gate).
        self.assertLess(source.index('Step "VERIFY launcher'), source.index('pool VERIFIED'))
        self.assertLess(source.index('Step "VERIFY pool'), source.index('Step "VERIFY launcher'))

    def test_the_readback_rejects_inline_command_abbreviations(self):
        # The install-time inline-command regex must catch powershell.exe prefix abbreviations (-c..-command,
        # -e/-ec/-en..-encodedcommand) and NOT -ExecutionPolicy, matching the runtime launch gate.
        source = _code("install_pool.ps1")
        self.assertIn("|ec|", source)                  # the -ec alias branch
        self.assertIn("c(o(m(m(a(n(d)?)?)?)?)?)?", source)   # the -command prefix ladder


class NoAmbiguousInterpolationTests(SimpleTestCase):
    """`$Prefix1` parses as a variable named Prefix1, not as $Prefix followed by a literal 1."""

    AMBIGUOUS = ("IdentityPrefix", "LaunchPrefix", "StopPrefix", "SlotsRoot", "GoldenDir")

    def test_prefix_variables_followed_by_a_digit_are_braced(self):
        import re
        for name in SCRIPTS:
            for match in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*?)(\d)\b", _read(name)):
                self.assertNotIn(match.group(1), self.AMBIGUOUS,
                                 f"{name}: ambiguous ${match.group(1)}{match.group(2)} — use braces")


class ServiceAccountCanDoItsJobTests(SimpleTestCase):
    """The agent stages, marks and moves runtimes ITSELF — it does not delegate that to a task.

    An earlier version granted the service account nothing on golden\\ or slots\\ on the reasoning that "the
    agent only triggers tasks". It does not: win_slot_ops runs robocopy, writes the ownership marker inside
    the slot, digests the golden tree and renames the slot into the tombstone root. That install would have
    left the pool provisioned and permanently unusable.
    """

    def test_the_service_account_gets_modify_on_the_slot_root(self):
        source = _code("install_service.ps1")
        self.assertIn("$SlotsRoot", source)
        self.assertIn("foreach ($d in @($StateDir, $BetaTombstones, $SlotsRoot))", source)

    def test_the_service_account_gets_read_on_the_golden_image_only(self):
        source = _code("install_service.ps1")
        # ReadAndExecute now also covers the WinSW wrapper dir and the venv (the account runs the .exe and the
        # venv python from there).
        self.assertIn("foreach ($d in @($AgentDir, $GoldenDir, $WinSwDir, $VenvDir))", source)
        self.assertIn("-Rights ReadAndExecute -ServiceSid $ServiceSid", source)
        self.assertIn("-Rights Modify -ServiceSid $ServiceSid", source)

    def test_the_service_acls_do_not_depend_on_name_resolution(self):
        """These grants run BEFORE `sc create`, so NT SERVICE\GuvFXBetaAgent has no name mapping yet and
        icacls aborts the install at its FIRST grant with 1332 — for the name and for the raw SID alike.
        The SID is derived from the service name, so it exists as a value before the service does."""
        source = _code("install_service.ps1")
        self.assertIn("function Get-GuvfxServiceSid", source)
        self.assertIn("sc.exe showsid", source)
        self.assertIn("Set-Acl -Path $Path -AclObject $acl", source)
        # The SID is computed and the ACLs are granted BEFORE the service is registered (WinSW `install`),
        # so the account it creates inherits DACLs already keyed to its (deterministic) SID.
        self.assertLess(source.index("$ServiceSid = Get-GuvfxServiceSid"), source.index("& $ServiceExe install"))
        for line in source.splitlines():
            stmt = line.split("#", 1)[0]
            if "icacls" in stmt and "$RunAsUser" in stmt:
                self.fail(f"icacls cannot resolve the service account here: {line.strip()}")

    def test_grant_failures_are_detected(self):
        """A silently failed grant would otherwise surface as a first-MATERIALISE failure on the live host."""
        source = _code("install_service.ps1")
        self.assertIn("post-check failed: service SID", source)
        self.assertIn("the grant did not take; do NOT start", source)

    def test_the_approval_file_is_readable_by_the_service_account(self):
        """Granted via Set-Acl on the computed SID, NEVER via icacls.

        install_pool.ps1 runs BEFORE install_service.ps1, so NT SERVICE\\GuvFXBetaAgent has no name
        mapping yet. Measured on the host: icacls returns 1332 for the name AND for the raw SID (it
        reverse-resolves), and because icacls applies an invocation atomically it discarded the
        Administrators and SYSTEM grants issued in the same call - leaving the file with inheritance
        stripped and no explicit ACE, then throwing on the LAST step of a credentialed APPLY.
        """
        source = _code("install_pool.ps1")
        self.assertIn("function Grant-GuvfxServiceRead", source)
        self.assertIn("sc.exe showsid", source)
        self.assertIn("Set-Acl -Path $Path -AclObject $acl", source)
        self.assertIn('Grant-GuvfxServiceRead -Path $ApprovedTasksOut -ServiceName "GuvFXBetaAgent"', source)
        # The failing form must be gone: no icacls INVOCATION may name the service account. Both comment
        # forms are stripped first — the reason this is forbidden is written next to the code avoiding it,
        # and a check that trips on its own rationale is noise, not coverage.
        code_only = re.sub(r"<#.*?#>", "", source, flags=re.S)
        for line in code_only.splitlines():
            stmt = line.split("#", 1)[0]
            if "icacls" in stmt:
                self.assertNotIn("NT SERVICE", stmt, f"icacls cannot resolve a service account: {line}")

    def test_the_service_grant_refuses_a_non_service_sid(self):
        """sc.exe output is parsed, so the parse result is validated before it becomes an ACE."""
        source = _code("install_pool.ps1")
        self.assertIn('"^S-1-5-80-\\d+-\\d+-\\d+-\\d+-\\d+$"', source)
        self.assertIn("is not a service SID", source)
        self.assertIn("post-check failed: service SID", source)

    def test_the_service_grant_post_check_reads_the_dacl_as_sids(self):
        """Asking for NTAccount would re-introduce the name lookup that cannot succeed - a post-check
        that can never pass is worse than no post-check."""
        source = _code("install_pool.ps1")
        self.assertIn("GetAccessRules($true, $false,", source)
        self.assertIn("[System.Security.Principal.SecurityIdentifier])", source)

    def test_stripping_inheritance_is_checked_separately_from_the_grant(self):
        """If /inheritance:r succeeds and the grant then fails, the file is left with NO explicit ACE.
        One shared $LASTEXITCODE check cannot distinguish those two outcomes, so they are two calls -
        each checked by Invoke-GuvfxIcacls, which throws naming the exact arguments that failed."""
        source = _code("install_pool.ps1")
        self.assertIn('Invoke-GuvfxIcacls $ApprovedTasksOut @("/inheritance:r")', source)
        self.assertIn('Invoke-GuvfxIcacls $ApprovedTasksOut @("/grant", "*S-1-5-32-544:F"', source)

    def test_the_approval_file_is_written_without_a_bom(self):
        """Set-Content -Encoding UTF8 emits a BOM under PS 5.1; the agent would call that malformed JSON."""
        source = _code("install_pool.ps1")
        self.assertIn("UTF8Encoding $false", source)
        self.assertNotIn("Set-Content -Path $ApprovedTasksOut", source)


class ApprovalReflectsRealityTests(SimpleTestCase):
    """The approval must pin what IS registered, not what we intended to register."""

    def test_the_approval_is_read_back_through_the_agents_own_interface(self):
        source = _code("install_pool.ps1")
        self.assertIn("Schedule.Service", source)
        self.assertIn("$reg.Definition.Principal", source)
        self.assertIn("[string]$p.UserId", source)

    def test_the_approval_carries_the_arguments_and_asserts_portable(self):
        source = _code("install_pool.ps1")
        self.assertIn("arguments         = [string]$act.Arguments", source)
        self.assertIn("does not carry /portable - refusing to approve it", source)

    def test_registration_is_re_runnable(self):
        source = _code("install_pool.ps1")
        self.assertEqual(source.count("-RunLevel Limited -Force"), 2)

    def test_the_password_is_prompted_once_and_confirmed(self):
        source = _code("install_pool.ps1")
        self.assertIn("Confirm password for $user", source)
        self.assertIn("function Get-SlotSecret", source)


class UserRightManagementTests(SimpleTestCase):
    """User rights are managed with the LSA policy API, never secedit.

    The 2026-07-22 baseline recorded SeBatchLogonRight as ABSENT from local policy. That reading was a
    false negative in the capture — the right is explicitly assigned to the three Windows defaults
    (Administrators, Backup Operators, Performance Log Users). The correction does not weaken the case for
    the LSA API; it strengthens it. secedit writes a COMPLETE assignment line, so adding our four SIDs
    means reconstructing those three default principals from a template exactly, and a reconstruction that
    is wrong revokes batch logon machine-wide from whoever held it. LsaAddAccountRights adds one right to
    one account and touches nothing else, so there is no line to rewrite and the installer never has to
    know, infer or recreate the defaults.

    These are source-conformance checks. The LSA calls themselves cannot be exercised off Windows; the
    read-only half runs on the host during PLAN, before any APPLY, which is where the interop is proven.
    """

    def test_secedit_is_gone_from_both_scripts(self):
        for name in ("install_pool.ps1", "uninstall.ps1"):
            code = _code(name)
            for call in ("secedit /export", "secedit /configure", "local.sdb"):
                self.assertNotIn(call, code, f"{name} still calls {call}")

    def test_no_secedit_fallback_remains(self):
        """A fallback would reintroduce exactly the failure mode this change removes."""
        for name in ("install_pool.ps1", "uninstall.ps1"):
            self.assertNotIn("secedit", _code(name), name)

    def test_install_adds_a_right_and_never_rewrites_an_assignment(self):
        code = _code("install_pool.ps1")
        self.assertIn("LsaAddAccountRights", code)
        self.assertNotIn("SeBatchLogonRight = ", code)      # the secedit line-rewrite form

    def test_uninstall_removes_only_the_named_right(self):
        """allRights=$false is the difference between removing one right and removing every right."""
        code = _code("uninstall.ps1")
        self.assertIn("LsaRemoveAccountRights", code)
        self.assertIn("$slotSidBytes, $false, @($u), 1", code)

    def test_only_one_right_name_is_ever_used(self):
        for name in ("install_pool.ps1", "uninstall.ps1"):
            code = _code(name)
            self.assertIn('$GuvfxRight = "SeBatchLogonRight"', code)
            for other in ("SeInteractiveLogonRight", "SeServiceLogonRight", "SeDebugPrivilege",
                          "SeTcbPrivilege", "SeAssignPrimaryTokenPrivilege"):
                self.assertNotIn(other, code, f"{name} references {other}")

    def test_the_sid_namespace_guard_exists_in_both_scripts(self):
        """Taking a NAME and validating it — rather than accepting a SID — is what makes it impossible for
        these functions to act on Administrators, SYSTEM, or any principal outside the beta namespace."""
        for name in ("install_pool.ps1", "uninstall.ps1"):
            code = _code(name)
            self.assertIn("guvfx_b_slot[1-9][0-9]*$", code, name)
            self.assertIn("outside the beta-slot identity namespace", code, name)

    def test_every_lsa_status_is_checked(self):
        """NTSTATUS is a return value, not an exception: an unchecked call fails silently."""
        for name in ("install_pool.ps1", "uninstall.ps1"):
            code = _code(name)
            self.assertIn("LsaOpenPolicy failed", code, name)
            self.assertIn("LsaEnumerateAccountRights failed", code, name)
        self.assertIn("LsaAddAccountRights failed", _code("install_pool.ps1"))
        self.assertIn("LsaRemoveAccountRights failed", _code("uninstall.ps1"))

    def test_the_add_is_idempotent(self):
        code = _code("install_pool.ps1")
        self.assertIn("result=already_present", code)
        self.assertIn("if ($before -contains $GuvfxRight)", code)

    def test_the_remove_is_safe_to_repeat(self):
        code = _code("uninstall.ps1")
        self.assertIn("result=not_held", code)
        self.assertIn("if ($before -notcontains $GuvfxRight)", code)

    def test_other_rights_are_asserted_to_survive(self):
        """The assertion secedit could never make: enumerate before, enumerate after, compare."""
        for name in ("install_pool.ps1", "uninstall.ps1"):
            self.assertIn("user-right regression", _code(name), name)

    def test_the_operation_is_recorded_as_evidence(self):
        for name in ("install_pool.ps1", "uninstall.ps1"):
            code = _code(name)
            self.assertIn("evidence right=$GuvfxRight sid=", code, name)
            self.assertIn("op=", code, name)
            self.assertIn("result=", code, name)

    def test_uninstall_captures_sids_before_any_account_is_removed(self):
        code = _code("uninstall.ps1")
        self.assertLess(code.index("$SlotIdentities = @()"), code.index("Remove-LocalUser"))
        self.assertLess(code.index("$SlotIdentities = @()"), code.index("Disable-LocalUser"))

    def test_plan_mode_exercises_the_read_path_only(self):
        """PLAN proves the LSA interop works on the real host and prints the exact delta, changing nothing."""
        code = _code("install_pool.ps1")
        self.assertIn("WOULD ADD", code)
        self.assertIn("Get-GuvfxAccountRights -AccountName $user", code)

    def test_no_default_memberships_are_inferred(self):
        """The directive is explicit: do not infer or recreate Windows default account-right memberships."""
        for name in ("install_pool.ps1", "uninstall.ps1"):
            code = _code(name)
            for well_known in ("S-1-5-32-551", "S-1-5-32-559", "Backup Operators:"):
                self.assertNotIn(well_known, code, f"{name} references default holder {well_known}")


class RestoredOrderingInvariantsTests(SimpleTestCase):
    """Invariants that predate the LSA change and must survive it.

    They were covered by the old SeceditSafetyTests class, which the LSA rewrite replaced wholesale — the
    tests went with it. Restored here, decoupled from the mechanism they used to be attached to.
    """

    def test_service_acls_are_revoked_before_the_service_is_deleted(self):
        """Once the SCM registration is gone, NT SERVICE\\<name> may no longer resolve."""
        code = _code("uninstall.ps1")
        self.assertLess(code.index("/remove:g"), code.index("sc.exe delete"))

    def test_sids_are_collected_before_identities_are_removed(self):
        code = _code("uninstall.ps1")
        self.assertLess(code.index("$SlotIdentities = @()"), code.index("Remove-LocalUser"))
        self.assertLess(code.index("$SlotIdentities = @()"), code.index("Disable-LocalUser"))


class LsaLanguageTrapTests(SimpleTestCase):
    """PowerShell parses an 8-hex-digit literal as Int32, so 0xC0000034 wraps to -1073741772 while the LSA
    return value is UInt32 3221225524. Written as hex, the comparison is False for ever — turning the
    benign 'this account holds no rights' status into a hard failure that aborts every -Apply after the
    accounts have been created. [uint32]0xC0000034 does not rescue it: the literal has already wrapped and
    the cast throws.
    """

    def test_the_ntstatus_constant_is_not_an_eight_digit_hex_literal(self):
        for name in ("install_pool.ps1", "uninstall.ps1"):
            code = _code(name)
            self.assertNotIn("= 0xC0000034", code, name)
            self.assertIn("[uint32]3221225524", code, name)

    def test_the_wrap_is_documented_where_the_constant_is_defined(self):
        for name in ("install_pool.ps1", "uninstall.ps1"):
            self.assertIn("WRAPS", _read(name), name)


class LsaReentryTests(SimpleTestCase):
    """PLAN then APPLY in one console is the mandated workflow, so Add-Type must not be a second-run
    terminating error."""

    def test_add_type_is_guarded_by_a_type_existence_check(self):
        self.assertIn("if (-not ('GuvfxLsa' -as [type]))", _code("install_pool.ps1"))
        self.assertIn("if (-not ('GuvfxLsaU' -as [type]))", _code("uninstall.ps1"))

    def test_the_interop_loads_before_any_account_is_created(self):
        """A broken interop must abort while the host is still untouched, not after four accounts exist.

        Compares EXECUTION order, so comments are stripped first: a comment that merely names
        New-LocalUser (explaining why group membership is reconciled separately, for instance) is not a
        call to it, and an ordering test that trips on prose is measuring the wrong thing.
        """
        raw = _code("install_pool.ps1")
        code = re.sub(r"<#.*?#>", "", raw, flags=re.S)
        code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
        # Both operands must index the SAME string — offsets from differently-stripped copies are not
        # comparable, and comparing them is how this assertion first reported a false failure.
        self.assertLess(code.index("Add-Type -TypeDefinition"), code.index("New-LocalUser"))
        self.assertLess(code.index("LSA interop self-test"), code.index("New-LocalUser"))

    def test_the_self_test_actually_enters_advapi32(self):
        code = _code("install_pool.ps1")
        self.assertIn("$probe = Open-GuvfxLsaPolicy -Access $LSA_READ", code)
        self.assertIn("LsaClose($probe)", code)

    def test_the_plan_claim_matches_what_plan_can_prove(self):
        """On a fresh host the accounts do not exist, so the enumerate path is NOT exercised by PLAN. The
        comment must not claim otherwise."""
        source = _read("install_pool.ps1")
        self.assertIn("enumerate path cannot be exercised", source)
        self.assertNotIn("it proves the LSA interop works and prints the", source)


class EvidenceOnFailureTests(SimpleTestCase):
    """A failed user-right operation is as much an installation fact as a successful one."""

    def test_failure_paths_emit_evidence_before_throwing(self):
        for name, ops in (("install_pool.ps1", ("result=failed", "result=postcheck_failed", "result=regression")),
                          ("uninstall.ps1", ("result=failed",))):
            code = _code(name)
            for op in ops:
                self.assertIn(op, code, f"{name}:{op}")

    def test_evidence_uses_the_host_output_stream(self):
        """A bare string is a return value; inside a function it flows to the caller, not the transcript."""
        for name in ("install_pool.ps1", "uninstall.ps1"):
            self.assertIn('Write-Host "evidence right=$GuvfxRight', _code(name), name)


class UninstallSilentNoOpTests(SimpleTestCase):
    """A revoke that resolves no identity is indistinguishable from a clean teardown while four SIDs keep
    the right for ever — inheritable by a future account with the same RID."""

    def test_unresolvable_identities_are_reported_loudly(self):
        code = _code("uninstall.ps1")
        self.assertIn("could not be resolved", code)
        self.assertIn("CANNOT be verified as revoked", code)
        self.assertIn("the grant is orphaned on its SID", code)

    def test_the_identity_prefix_is_refused_up_front(self):
        code = _code("uninstall.ps1")
        self.assertIn('if ($IdentityPrefix -ne "guvfx_b_slot")', code)
        self.assertLess(code.index('$IdentityPrefix -ne "guvfx_b_slot"'), code.index("Disable-LocalUser"))


class AsciiOnlyScriptTests(SimpleTestCase):
    """Installation scripts must be pure ASCII.

    Windows PowerShell 5.1 reads a BOM-less file as ANSI (Windows-1252), so a UTF-8 em-dash (E2 80 94)
    decodes to three characters, the last of which is a double-quote that TERMINATES the enclosing string.
    Parsing install_pool.ps1 on the host produced 20 syntax errors from nothing but punctuation - and
    firewall.ps1 and install_service.ps1 had carried the same latent defect since B2/B3P-1, through two
    adversarial reviews and a full install-only review, because nobody had ever parsed them on Windows.

    ASCII-only makes the scripts parse identically under any encoding, with or without a BOM.
    """

    def test_every_install_script_is_pure_ascii(self):
        for name in SCRIPTS + (WRAPPER,):
            raw = open(os.path.join(_BUNDLE, name), "rb").read()
            offenders = sorted({b for b in raw if b > 127})
            self.assertEqual(offenders, [], f"{name} contains non-ASCII bytes {offenders}")

    def test_no_script_relies_on_a_bom(self):
        """A BOM would also work, but it is a subtle dependency; ASCII-only needs no such assumption."""
        for name in SCRIPTS + (WRAPPER,):
            raw = open(os.path.join(_BUNDLE, name), "rb").read(3)
            self.assertNotEqual(raw, b"\xef\xbb\xbf", f"{name} starts with a UTF-8 BOM")


class LaunchWrapperTests(SimpleTestCase):
    """ADR-0016: the per-slot launch wrapper launches terminal64 suspended, grants the beta-agent service
    query access to that ONE process object (read-modify-write, never a DACL replace), verifies it, and
    resumes; on any failure it terminates the child by handle and exits non-zero. These checks pin the
    safety-critical properties a review must never let regress."""

    def _wrapper(self):
        return _read(WRAPPER)

    def _wrapper_code(self):
        # Comment-stripped view: full-line PS '#' comments and C# '//' inline comments removed, so a check for
        # the ABSENCE of a token tests what the wrapper DOES, not what its comments explain (RULE 5).
        out = []
        for ln in _read(WRAPPER).splitlines():
            if ln.strip().startswith("#"):
                continue
            if "//" in ln:
                ln = ln[:ln.index("//")]
            out.append(ln)
        return "\n".join(out)

    def test_the_pinned_hash_in_install_pool_matches_the_wrapper_file(self):
        # The install refuses to stage a wrapper whose hash differs from $LaunchWrapperSha256. If that
        # constant ever drifts from the file, the install would refuse the real wrapper on the host. Bind
        # them here so the pin can never silently rot (RULE 9: ASCII-only keeps the hash encoding-stable).
        import hashlib
        digest = hashlib.sha256(open(os.path.join(_BUNDLE, WRAPPER), "rb").read()).hexdigest()
        pool = _read("install_pool.ps1")
        self.assertIn('$LaunchWrapperSha256 = "%s"' % digest, pool,
                      "install_pool.ps1 $LaunchWrapperSha256 does not match slot_launch.ps1's actual hash")

    def test_the_ace_is_applied_read_modify_write_never_a_replace(self):
        # A wholesale DACL replace would strip the owner's default PROCESS_TERMINATE and silently disable the
        # slot's STOP task. The wrapper must READ the existing DACL and INSERT one ACE.
        w = self._wrapper()
        self.assertIn("GetKernelObjectSecurity", w)
        self.assertIn("SetKernelObjectSecurity", w)
        self.assertIn("InsertAce", w)
        # A NULL DACL must fail closed, never be synthesised into a restrictive one.
        self.assertIn("DiscretionaryAcl == null", w)

    def test_the_grant_is_exactly_query_limited_plus_read_control(self):
        # 0x21000 == PROCESS_QUERY_LIMITED_INFORMATION (0x1000) | READ_CONTROL (0x20000). Nothing broader.
        w = self._wrapper()
        code = self._wrapper_code()
        # ONE source of truth for the mask, used by BOTH the ACE and its read-back.
        self.assertIn("const int GRANT_MASK = 0x21000;", w)
        # The read-back must assert EQUALITY (== GRANT_MASK), never 'contains at least' (& mask == mask), so a
        # broader grant (e.g. one that also carries PROCESS_VM_READ) fails verification. This kills the
        # over-grant regression the substring check alone would miss.
        self.assertIn("ca.AccessMask == GRANT_MASK", w)
        self.assertNotIn("& mask) == mask", w)
        # No hard-coded broad masks anywhere in the CODE (the concept may be named in comments).
        for forbidden in ("PROCESS_ALL_ACCESS", "0x1F0FFF", "GENERIC_ALL", "SeDebugPrivilege"):
            self.assertNotIn(forbidden, code)
        # 0x21000 appears in CODE ONLY as the single GRANT_MASK const, not scattered as literals.
        self.assertEqual(code.count("0x21000"), 1, "the mask must be a single const, not repeated literals")

    def test_it_launches_suspended_and_fails_closed_by_terminating_the_child(self):
        # CREATE_SUSPENDED + verify + ResumeThread means terminal64 executes ZERO instructions (never reaches
        # a broker) until the grant is confirmed; a failure terminates the child by the handle it created.
        w = self._wrapper()
        self.assertIn("CREATE_SUSPENDED", w)
        self.assertIn("ResumeThread", w)
        self.assertIn("TerminateProcess", w)
        # Never by image name (the production terminal shares terminal64.exe).
        self.assertNotIn("Stop-Process -Name", w)
        self.assertNotIn("/IM terminal64", w)

    def test_it_hard_codes_portable_and_never_forwards_a_free_arg_to_terminal64(self):
        # /portable is a wrapper constant, not taken from the task argument -> no injection surface. The task
        # argument's inert /portable is swallowed by ValueFromRemainingArguments.
        w = self._wrapper()
        self.assertIn("/portable", w)
        self.assertIn("ValueFromRemainingArguments", w)

    def test_it_refuses_a_grantee_that_is_not_the_service_account(self):
        # The grantee SID must be a service SID (S-1-5-80-) that translates back to NT SERVICE\GuvFXBetaAgent.
        w = self._wrapper()
        self.assertIn("S-1-5-80-", w)
        self.assertIn("NT SERVICE\\GuvFXBetaAgent", w)
        self.assertIn("Translate", w)

    def test_it_confines_the_terminal_path_to_the_beta_slots_root(self):
        w = self._wrapper()
        self.assertIn(r"C:\GuvFX\beta\slots", w)
        self.assertIn("terminal64.exe", w)

    def test_it_validates_the_working_directory_beneath_the_slots_root(self):
        # The working dir becomes terminal64's CWD, so it is validated symmetrically with TerminalPath.
        w = self._wrapper()
        self.assertIn("WorkingDirectory is not beneath the beta slots root", w)

    def test_fail_emits_exit_code_2_not_dead_code_under_stop(self):
        # Under $ErrorActionPreference='Stop', Write-Error would throw and make 'exit 2' unreachable (emitting
        # exit code 1). Fail must write to stderr directly so the intended non-zero exit code is actually set.
        self.assertIn("[Console]::Error.WriteLine", self._wrapper())
        self.assertNotIn("Write-Error", self._wrapper_code())   # not CALLED (a comment may name it)

    def test_a_thrown_failure_still_terminates_the_suspended_child(self):
        # A thrown exception after CreateProcessW must not slip past to the finally (which only closes handles)
        # leaving a suspended terminal64 the wrapper can no longer kill by handle. A catch terminates first.
        w = self._wrapper()
        self.assertIn("catch", w)
        # The catch terminates the child before the finally closes the handle.
        self.assertIn("try { TerminateProcess(pi.hProcess, 1); } catch { }", w)


class GoldenImageValidationTests(SimpleTestCase):
    """RULE 10: the golden runtime must come from a dedicated clean install, never the production terminal.

    The production MT5 install carries the operator's broker credentials in config\\accounts.dat and its
    whole trading history in bases\\ — promoting it would copy a live login into every beta slot. These
    checks are written to REFUSE such an image, not to trust that nobody would do it.
    """

    REQUIRED_EVIDENCE = {
        "config\\accounts.dat": "a saved broker account",
        "config\\servers.dat": "a downloaded broker server list",
        "config\\common.ini": "settings from a previous run",
        "config\\terminal.ini": "settings from a previous run",
        "bases": "market data / trade history",
        "logs": "terminal logs",
        "MQL5\\Logs": "MQL5 logs",
        "MQL5\\Profiles": "chart profiles carrying attached-EA configuration",
        "MQL5\\Presets": "saved EA input presets",
    }

    def test_every_required_usage_artefact_is_refused(self):
        code = _code("install_pool.ps1")
        for rel in self.REQUIRED_EVIDENCE:
            self.assertIn(rel, code, f"golden validation does not refuse '{rel}'")

    def test_the_mt5_build_is_pinned_by_the_manifest(self):
        """The same string the agent compares against BETA_AGENT_GOLDEN_MANIFEST_VERSION."""
        code = _code("install_pool.ps1")
        self.assertIn("VersionInfo.FileVersion", code)
        self.assertIn("the manifest pins", code)

    def test_an_empty_manifest_is_refused(self):
        self.assertIn("it must pin the MT5 build", _code("install_pool.ps1"))

    def test_compiled_eas_are_refused(self):
        """An Experts directory with compiled EAs is attached-strategy configuration even with no profile."""
        code = _code("install_pool.ps1")
        self.assertIn("MQL5\\Experts", code)
        self.assertIn("the golden image must carry no strategy", code)

    def test_the_expected_structure_is_asserted(self):
        """terminal64.exe is the ONLY hard structural requirement. MQL5 is deliberately optional: a fresh
        non-portable install keeps its data under %APPDATA%\\MetaQuotes\\Terminal\\<hash>, so the tree
        legitimately has none, and /portable creates one in the slot at first run. Requiring it rejected a
        genuine MetaQuotes installer output."""
        code = _code("install_pool.ps1")
        self.assertIn('if (Test-Path (Join-Path $Path "terminal64.exe"))', code)
        self.assertIn("non-portable install; /portable creates it in the slot", code)
        self.assertIn(".guvfx_golden_manifest", code)
        self.assertIn(".guvfx_portable", code)

    def test_bases_is_judged_by_broker_directory_not_file_count(self):
        """bases\\ SHIPS POPULATED - Bases\\Default carries demo history, 527 welcome messages and symbol
        definitions, 537 files written within two seconds of install. Only a BROKER-NAMED subdirectory
        proves the terminal ever connected."""
        code = _code("install_pool.ps1")
        self.assertIn('Where-Object { $_.Name -ne "Default" }', code)
        self.assertIn("is a broker-named data directory", code)

    def test_provenance_is_scanned_in_file_contents(self):
        """The check that caught a tree copied from a live per-account runtime: MQL5\\experts.dat held 66
        absolute paths rooted at another runtime's directory while every filename-based check passed."""
        code = _code("install_pool.ps1")
        self.assertIn("C:\\GuvFX\\terminals", code)
        self.assertIn("this tree was COPIED from an existing runtime", code)
        self.assertIn("[Text.Encoding]::Unicode.GetString($b)", code)

    def test_validation_failure_aborts_before_plan(self):
        """Abort, never warn: a dirty golden image must not reach the identity or task stages."""
        code = _code("install_pool.ps1")
        self.assertIn("golden image validation FAILED", code)
        self.assertIn("aborting before PLAN", code)
        self.assertLess(code.index("golden image validation FAILED"), code.index("New-LocalUser"))

    def test_the_rule_is_stated_where_the_check_lives(self):
        source = _read("install_pool.ps1")
        self.assertIn("RULE 10", source)
        self.assertIn("never promote the production MT5 installation", source)

    def test_each_failure_names_what_was_found(self):
        """'validation failed' is not actionable; the operator must be told which artefact was present."""
        self.assertIn("previous use: '$rel' present", _code("install_pool.ps1"))


class PermanentRulesTests(SimpleTestCase):
    def test_rules_9_and_10_are_recorded(self):
        rules = open(os.path.join(_REPO, ".claude", "rules", "security.md"), encoding="utf-8").read()
        self.assertIn("RULE 9", rules)
        self.assertIn("RULE 10", rules)
        self.assertIn("ParseFile", rules)
        self.assertIn("never be promoted to the golden image", rules)


class ApplyReadinessReviewTests(SimpleTestCase):
    """Findings from the adversarial review of the APPLY package, before it ran on the live host.

    Every one of these is a defect that PLAN could not surface, because PLAN never reaches the step.
    """

    def test_every_icacls_call_goes_through_the_checked_wrapper(self):
        """icacls is native, so $ErrorActionPreference does not apply and a failed ACL is silent. Six of
        eight calls were unchecked: a slot could be left with the wrong access while the run printed ok."""
        code = _code("install_pool.ps1")
        self.assertIn("function Invoke-GuvfxIcacls", code)
        body = re.sub(r"<#.*?#>", "", code, flags=re.S)   # block comments explain WHY, and name icacls
        wrapper, callers = body.split("function Invoke-GuvfxIcacls", 1)[1].split("\n}\n", 1)
        self.assertIn("$LASTEXITCODE -ne 0", wrapper)     # the wrapper itself checks
        for line in callers.splitlines():
            stmt = line.split("#", 1)[0]
            if "icacls" in stmt and "Invoke-GuvfxIcacls" not in stmt:
                self.fail(f"unchecked icacls call: {line.strip()}")

    def test_the_golden_image_inheritance_is_broken(self):
        """MEASURED on the host: the golden tree inherits BUILTIN\\Users ReadAndExecute + AppendData +
        CreateFiles and an inherit-only CREATOR OWNER GENERIC_ALL. Slot identities are in Users, so without
        breaking inheritance they could CREATE files there and own them - and every future MATERIALISE
        would copy that into every future slot. icacls /grant is ADDITIVE; granting RX removes nothing."""
        code = _code("install_pool.ps1")
        self.assertIn('Invoke-GuvfxIcacls $GoldenDir @("/inheritance:r")', code)
        self.assertIn("AreAccessRulesProtected", code)

    def test_both_task_families_are_pinned_in_the_approval_file(self):
        """The terminate task is the one that can reach a process. Pinning only the launch task inverted
        the risk: the approvals file is written once at install, so adding it later would mean re-prompting
        four passwords on the live host."""
        code = _code("install_pool.ps1")
        self.assertIn("foreach ($t in @($launch, $stop)) {", code)
        self.assertIn("$Approved[$t] = [ordered]@{", code)
        self.assertIn("does not scope termination to", code)
        self.assertIn("pipes Get-Process straight into Stop-Process", code)

    def test_the_non_admin_assertion_fails_closed(self):
        """-ErrorAction SilentlyContinue turned an enumeration failure into 'not a member', so the one
        check standing between the sponsor's guarantee and a privileged runtime identity passed loudest
        exactly when it could see least."""
        code = _code("install_pool.ps1")
        self.assertIn("Get-LocalGroupMember -Group $g -ErrorAction Stop", code)
        self.assertIn("non-admin is UNPROVEN", code)
        self.assertIn("is not a member of 'Users' - refusing to continue", code)

    def test_password_buffers_are_zeroed_and_freed(self):
        """SecureStringToBSTR allocates unmanaged memory holding the plaintext; nothing freed it."""
        code = _code("install_pool.ps1")
        self.assertIn("ZeroFreeBSTR", code)
        self.assertIn("empty password rejected", code)

    def test_the_approvals_path_is_namespace_refused(self):
        """It is the target of the script's most destructive file primitive - inheritance stripped and the
        DACL rewritten - and it had no refusal at all."""
        code = _code("install_pool.ps1")
        self.assertIn("foreach ($p in @($SlotsRoot, $TombstonesRoot, $ApprovedTasksOut, $LauncherDir))", code)

    def test_the_estate_check_can_actually_fail(self):
        """It printed ok when a task was present and nothing when it was missing, so 'estate untouched'
        was asserted by silence (RULE 11)."""
        code = _code("install_pool.ps1")
        self.assertIn("$EstateBefore", code)
        self.assertIn("was present before the install and is GONE - STOP", code)
        self.assertIn("principal changed from", code)

    def test_the_follow_on_scripts_default_to_the_approved_golden_path(self):
        """install_service.ps1 runs minutes after APPLY and hard-fails on a missing golden path."""
        for name in ("install_service.ps1", "uninstall.ps1"):
            source = _read(name)
            self.assertIn(r'"C:\GuvFX\golden\newMT5"', source, name)
            self.assertNotIn(r'$GoldenDir   = "C:\GuvFX\beta\golden"', source, name)

    def test_verify_reads_acls_back_from_the_filesystem(self):
        """ACLs were the one authorised object class VERIFY never inspected - applied, then trusted."""
        code = _code("install_pool.ps1")
        self.assertIn("cross-slot access - STOP", code)
        self.assertIn("Read+Execute only was authorised - STOP", code)


class GoldenAclVerificationTests(SimpleTestCase):
    """Decision 3: the golden image must not inherit writable permissions, and VERIFY must prove it."""

    def test_write_class_rights_are_checked_as_bits_not_as_a_name_substring(self):
        """A substring match on 'Write|Modify|FullControl' misses AppendData and CreateFiles — which is
        exactly how an earlier check reported the tree clean while BUILTIN\\Users could create files in it
        and own them Full Control via inherit-only CREATOR OWNER (RULE 11)."""
        code = _code("install_pool.ps1")
        self.assertIn("$WriteMask = ", code)
        for right in ("CreateFiles", "AppendData", "CreateDirectories", "WriteData",
                      "Delete", "ChangePermissions", "TakeOwnership"):
            self.assertIn(f"FileSystemRights]::{right}", code, right)
        self.assertIn("-band [int]$WriteMask", code)

    def test_only_administrators_and_system_may_write_the_golden_image(self):
        code = _code("install_pool.ps1")
        self.assertIn('$GOLDEN_WRITERS = @("S-1-5-32-544", "S-1-5-18")', code)
        self.assertIn("unexpected writable principal - STOP", code)

    def test_creator_owner_is_explicitly_checked(self):
        """Anything a slot identity created would otherwise be owned by it with full control."""
        code = _code("install_pool.ps1")
        self.assertIn('$who -eq "S-1-3-0"', code)
        self.assertIn("CREATOR OWNER still grants write-class rights - STOP", code)

    def test_administrators_and_system_must_survive_the_inheritance_break(self):
        """Breaking inheritance without re-granting would lock the operator out of their own image."""
        code = _code("install_pool.ps1")
        self.assertIn("has NO ACE after the inheritance break - STOP", code)

    def test_the_golden_digest_is_recomputed_after_the_acl_work(self):
        """ACL changes must not have touched content."""
        code = _code("install_pool.ps1")
        self.assertIn("tree digest", code)
        self.assertIn("BETA_AGENT_GOLDEN_DIGEST", code)


class GoldenDigestCanonicalisationTests(SimpleTestCase):
    """The installer's digest must reproduce win_slot_ops.tree_digest() byte for byte.

    It did not. The installer used forward slashes and a culture-aware Sort-Object; the agent uses
    backslashes (normalise() forces "/" -> "\\") and an ordinal sort. Over the real 584-file image the two
    produced DIFFERENT hex strings, and the wrong one was what got recorded as BETA_AGENT_GOLDEN_DIGEST —
    so stage_copy's source_digest_matches would have blocked every MATERIALISE on a clean, unmodified
    image, after the install and after the passwords.

        installer (corrected) 3a7fa6638e9eb9a0989edcaaff5b0c9ec93b15a6c62b9ee9b5f5f420d6313f10
        agent                 3a7fa6638e9eb9a0989edcaaff5b0c9ec93b15a6c62b9ee9b5f5f420d6313f10
    """

    def test_the_installer_uses_backslash_separators_like_normalise(self):
        code = _code("install_pool.ps1")
        self.assertIn('.Replace("/","\\").TrimEnd("\\").ToLower()', code)
        self.assertNotIn('.Replace("\\","/").ToLower()', code)

    def test_the_installer_sorts_ordinally_not_by_culture(self):
        """Sort-Object is culture-aware even with -CaseSensitive, so ordering is done explicitly."""
        code = _code("install_pool.ps1")
        self.assertIn("[System.StringComparer]::Ordinal", code)
        self.assertNotIn("Sort-Object FullName", code)

    def test_the_manifest_line_shape_matches_the_agent(self):
        import sys
        if _BUNDLE not in sys.path:
            sys.path.insert(0, _BUNDLE)
        from win_slot_ops import manifest_line, normalise
        self.assertEqual(manifest_line("Bases\\Default\\x.dat", 7, "ab"), "bases\\default\\x.dat|7|ab\n")
        self.assertEqual(normalise("Bases/Default/"), "bases\\default")
        # the PowerShell builds "{0}|{1}|{2}`n" over the same normalised key
        self.assertIn('("{0}|{1}|{2}`n" -f $rel, $f.Length,', _code("install_pool.ps1"))

    def test_the_recorded_digest_is_the_agent_computed_one(self):
        """Guards the specific value that ships in config.example.json."""
        import json
        cfg = json.load(open(os.path.join(_BUNDLE, "config.example.json"), encoding="utf-8"))
        self.assertEqual(cfg["BETA_AGENT_GOLDEN_DIGEST"],
                         "3a7fa6638e9eb9a0989edcaaff5b0c9ec93b15a6c62b9ee9b5f5f420d6313f10")
        self.assertEqual(cfg["BETA_AGENT_GOLDEN_MANIFEST_VERSION"], "5.0.0.6036")


class NullCountTests(SimpleTestCase):
    """`@($null).Count` is 1 in PowerShell, so `@($x).Count` reads ABSENT as ONE.

    This aborted the credentialed APPLY on 2026-07-23:

        if (@($task.Triggers).Count -gt 0) { throw "task '$t' has a trigger; expected on-demand only" }

    Get-ScheduledTask returns $null for .Triggers on a trigger-less task, so the check fired on every
    task that was correct. COM reported Triggers.Count 0, the registered XML held `<Triggers />`, and
    schtasks said "On demand only" — the tasks were right and the check could never pass.
    """

    def test_no_script_counts_a_possibly_null_value_with_the_at_paren_idiom(self):
        for name in SCRIPTS:
            code = re.sub(r"<#.*?#>", "", _code(name), flags=re.S)
            # Get-GuvfxCount is the ONE sanctioned use: it returns 0 for $null before reaching @().Count.
            code = re.sub(r"function Get-GuvfxCount.*?\n}\n", "", code, flags=re.S)
            for i, line in enumerate(code.splitlines(), 1):
                stmt = line.split("#", 1)[0]
                # An inline `if ($null -eq $x) { 0 } else { @($x).Count }` is null-safe by construction.
                if re.search(r"\$null -eq \$[A-Za-z_][\w.]*", stmt):
                    continue
                m = re.search(r"@\(\$[A-Za-z_][\w.]*\)\.Count", stmt)
                if m:
                    self.fail(f"{name}:{i} counts with {m.group(0)} — @($null).Count is 1, "
                              f"use Get-GuvfxCount / an explicit null test: {line.strip()}")

    def test_the_null_safe_counter_exists_and_returns_zero_for_null(self):
        code = _code("install_pool.ps1")
        self.assertIn("function Get-GuvfxCount", code)
        self.assertIn("if ($null -eq $Value) { return 0 }", code)

    def test_the_trigger_check_is_corroborated_by_a_second_source(self):
        """One library's null convention must not decide this alone."""
        code = _code("install_pool.ps1")
        self.assertIn("$trigPs  = Get-GuvfxCount $task.Triggers", code)
        self.assertIn("Definition.Triggers.Count", code)
        self.assertIn("trigger count disagrees between sources", code)
        self.assertIn("if ($trigCom -gt 0)", code)

    def test_uninstall_counts_identities_null_safely(self):
        code = _code("uninstall.ps1")
        self.assertIn("$slotIdentityCount = if ($null -eq $SlotIdentities) { 0 }", code)


class VerifyOnlyTests(SimpleTestCase):
    """-VerifyOnly re-runs verification against an already-provisioned pool.

    The mutating half of -Apply can succeed while VERIFY aborts — it did. Re-running -Apply to re-verify
    would re-register all eight tasks and re-prompt the operator for four passwords to redo work that was
    already correct, so verification is separable from mutation.
    """

    def test_the_switch_exists_and_excludes_apply(self):
        code = _code("install_pool.ps1")
        self.assertIn("[switch]$VerifyOnly", code)
        self.assertIn("-Apply and -VerifyOnly are mutually exclusive", code)

    def test_mutation_and_verification_are_separate_gates(self):
        code = _code("install_pool.ps1")
        self.assertIn("$Mutate = [bool]$Apply", code)
        self.assertIn("$Check  = ([bool]$Apply) -or ([bool]$VerifyOnly)", code)
        # DoIt — the only thing that changes the host — is gated on $Mutate, never on $Check.
        doit = code[code.index("function DoIt"):code.index("function Get-GuvfxCount")]
        self.assertIn("if ($Mutate)", doit)
        self.assertNotIn("$Check", doit)

    def test_every_mutating_step_is_gated_on_mutate_not_check(self):
        """The two mutations outside DoIt: the Users membership repair and the user-right grant."""
        code = _code("install_pool.ps1")
        self.assertIn("if ($Mutate) { Add-GuvfxUsersMembership -AccountName $user }", code)
        self.assertIn("if ($Mutate) {\n    Grant-GuvfxBatchLogonRight -AccountName $user", code)
        self.assertNotIn("if ($Check) {\n    Grant-GuvfxBatchLogonRight", code)

    #: Every block that must run under BOTH -Apply and -VerifyOnly. Reverting any of these to `if ($Apply)`
    #: silently turns -VerifyOnly into a no-op that still prints a green epilogue. Mutation testing during
    #: review proved the suite stayed green for each one individually and for all three at once.
    CHECK_GATED = (
        # the group-membership assertions (non-admin, and positively in Users)
        'if ($Check) {\n  for ($n = 1; $n -le $PoolSize; $n++) {\n    $user = "$IdentityPrefix$n"\n'
        '    foreach ($g in @("Administrators","Remote Desktop Users","Backup Operators")) {',
        # the approvals read-back, which carries the /portable and terminate-scope assertions
        'if ($Check) {\n    $svc = New-Object -ComObject Schedule.Service',
        # the VERIFY block itself
        'if ($Check) {\n  Step "VERIFY pool',
    )

    def test_every_verification_block_is_gated_on_check_not_apply(self):
        """The positive half of the gating, absent until review mutation-tested it.

        The dangerous revert is the approvals read-back: with it on $Apply, a -VerifyOnly run skips the
        assertion that the terminate task filters on this slot's own executable rather than piping
        Get-Process straight into Stop-Process — the script's own comment calls that the only thing
        standing between `Stop-Process -Force` and the operator's live terminal — and still prints
        'ok pool VERIFIED'.
        """
        code = _code("install_pool.ps1")
        for gate in self.CHECK_GATED:
            self.assertIn(gate, code,
                          f"a verification block is no longer gated on $Check:\n{gate}")

    def test_no_bare_apply_gate_survives_outside_the_exclusion_guard(self):
        """A NEW block must be classified as $Mutate or $Check, not default to invisible under -VerifyOnly."""
        code = _code("install_pool.ps1")
        for i, line in enumerate(code.splitlines(), 1):
            stmt = line.split("#", 1)[0]
            if re.search(r"if\s*\(\$Apply\)", stmt):
                self.fail(f"install_pool.ps1:{i} gates on $Apply directly — use $Mutate (changes the host) "
                          f"or $Check (asserts): {line.strip()}")
        self.assertIn("if ($Apply -and $VerifyOnly)", code)      # the one sanctioned use

    def test_verifyonly_asserts_the_user_right_rather_than_printing_a_plan_line(self):
        code = _code("install_pool.ps1")
        self.assertIn("does NOT hold $GuvfxRight - the pool cannot launch - STOP", code)
        self.assertIn("VERIFY: identity '$user' does not exist - the pool is not provisioned - STOP", code)

    def test_verifyonly_does_not_print_the_provisioning_epilogue(self):
        """'Next: install_service.ps1 -Apply' after a verify run would misreport what just happened."""
        code = _code("install_pool.ps1")
        self.assertIn("ok   pool VERIFIED. Nothing was created, changed or started by this run.", code)

    def test_no_password_prompt_can_be_reached_without_mutating(self):
        """EVERY Get-SlotSecret call site must sit inside a DoIt scriptblock, which -VerifyOnly skips.

        The first version of this test used ``body.index(line)`` to locate each call site. Two of the three
        call sites are byte-identical, so ``index`` returned the FIRST match every time and the test
        asserted the same offset three times over — an unconditional prompt at the top of the script would
        have passed it. Brace-match instead: find the enclosing block for real.
        """
        code = _code("install_pool.ps1")
        lines = code.splitlines()
        sites = [i for i, ln in enumerate(lines)
                 if "Get-SlotSecret $user" in ln and not ln.lstrip().startswith("function")]
        self.assertGreaterEqual(len(sites), 3, "expected the creation + two task-registration call sites")
        for idx in sites:
            # Walk backwards tracking brace depth; the innermost enclosing opener must be a DoIt block.
            depth, opener = 0, None
            for j in range(idx, -1, -1):
                depth += lines[j].count("}") - lines[j].count("{")
                if depth < 0:                      # this line opened the block we are inside
                    opener = lines[j]
                    break
            self.assertIsNotNone(opener, f"line {idx + 1} is not inside any block")
            self.assertIn("DoIt ", opener,
                          f"Get-SlotSecret at line {idx + 1} is not inside a DoIt block, so -VerifyOnly "
                          f"could reach a password prompt: enclosing opener was {opener.strip()!r}")


class PowerShellCaseCollisionTests(SimpleTestCase):
    """PowerShell variable names are CASE-INSENSITIVE: `$Foo` and `$foo` are one variable.

    This broke the golden-ACL check. The mask was `$WRITEISH` and the per-ACE result was `$writeish`:

        $WRITEISH = (...Write -bor ...Delete -bor ...)          # 0xD0156
        $writeish = (($raw -band [int]$WRITEISH) -ne 0) -or ...  # SAME VARIABLE

    The first ACE (SYSTEM, FullControl) set the result to $true, overwriting the mask, and `[int]$true` is
    1. Every later ACE was then tested against bit 0x1 — ReadData, which `ReadAndExecute` contains — so the
    check threw on the first slot identity and could never pass on a correctly configured image. Measured
    on the host: the mask read `0x1`; the same expression in isolation gives `0xD0156`.

    Using case to distinguish a constant from a local is not a style question here, it is a bug.
    """

    def test_no_script_has_two_variables_differing_only_by_case(self):
        import collections
        for name in SCRIPTS:
            code = re.sub(r"<#.*?#>", "", _read(name), flags=re.S)
            code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
            groups = collections.defaultdict(set)
            for var in re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", code):
                groups[var.lower()].add(var)
            collisions = {k: sorted(v) for k, v in groups.items() if len(v) > 1}
            self.assertEqual(collisions, {},
                             f"{name}: names differing only by case are the SAME variable in PowerShell — "
                             f"{collisions}")

    def test_the_mask_and_the_result_are_distinct_words(self):
        code = _code("install_pool.ps1")
        self.assertIn("$WriteMask = ", code)
        self.assertIn("$hasWriteRight = ", code)
        self.assertNotIn("$WRITEISH", code)
        self.assertNotIn("$writeish", code)


class InterpreterValidationTests(SimpleTestCase):
    """PLAN must never execute a candidate interpreter, and an installer must be rejected by identity.

    The service preflight previously ran `& $Python -c "import ..."` unconditionally. Pointed at
    C:\\GuvFX\\python311.exe — which is the Python INSTALLER (OriginalFilename python-3.11.9-amd64.exe),
    not an interpreter — a dry run launched an installer on the production host. `Test-Path` cannot tell
    the two apart and neither can an exit code (a detaching bootstrapper leaves $LASTEXITCODE $null).
    """

    def test_plan_does_not_execute_the_candidate_interpreter(self):
        code = _code("install_service.ps1")
        # identity is static; runtime execution is gated on $Apply
        self.assertIn("function Test-GuvfxInterpreterIdentity", code)
        self.assertIn("function Test-GuvfxInterpreterRuntime", code)
        self.assertIn("if ($Apply) { Test-GuvfxInterpreterRuntime -Path $Python -AgentDir $AgentDir }", code)
        # the identity function must NOT execute the file
        ident = code[code.index("function Test-GuvfxInterpreterIdentity"):
                     code.index("function Test-GuvfxInterpreterRuntime")]
        self.assertNotIn("& $Path", ident)
        self.assertNotIn("--version", ident)

    def test_the_installer_binary_is_rejected_by_metadata(self):
        code = _code("install_service.ps1")
        # Both guards must live INSIDE the identity function, each with its own throw — a stronger check
        # than "the literals appear somewhere", so restructuring that disabled a guard would be caught.
        ident = code[code.index("function Test-GuvfxInterpreterIdentity"):
                     code.index("function Test-GuvfxInterpreterRuntime")]
        self.assertIn("OriginalFilename", ident)
        # negative guard: reject the installer name / .msi, then the positive guard — both in the function
        self.assertIn(r"$orig -match '(?i)^python-.*\.exe$'", ident)
        self.assertIn("is the Python INSTALLER", ident)
        # positive guard: accept only interpreter/venv-shim names, with a throw
        self.assertIn(r"'(?i)^(python|pythonw|py|pyw)\.exe$'", ident)   # accepts venv 'py.exe' shim
        after_pos = ident[ident.index(r"'(?i)^(python|pythonw|py|pyw)\.exe$'"):]
        self.assertIn("throw", after_pos)

    def test_the_default_interpreter_is_the_beta_venv_not_the_installer(self):
        source = _read("install_service.ps1")
        self.assertIn(r'$Python      = "C:\GuvFX\beta\agent-venv\Scripts\python.exe"', source)

    def test_no_script_executes_the_installer_path(self):
        """The teardown had the same bug: & C:\\GuvFX\\python311.exe service.py remove."""
        for name in SCRIPTS:
            for line in _code(name).splitlines():
                stmt = line.split("#", 1)[0]
                if "python311.exe" in stmt and ("&" in stmt or "Start-Process" in stmt):
                    self.fail(f"{name} executes the installer path: {line.strip()}")


class FirewallScopedBlockTests(SimpleTestCase):
    """Report C / Workstream F: restrict :8791 to the backend with a scoped BLOCK + ALLOW, WITHOUT
    changing the machine-wide profile default.

    The bridge (:8788) is admitted by the broad Tailscale-In allow; that same rule over-exposes :8791 to
    the whole tailnet. A Windows Block rule wins over an Allow, so a block scoped to 'everything except the
    backend' denies non-backend peers while leaving the backend (and the bridge) reachable.
    """

    def test_it_does_not_require_or_change_the_machine_default(self):
        code = _code("firewall.ps1")
        # the old hard failure on DefaultInboundAction != Block is gone
        self.assertNotIn("expected 'Block' - the scoped allow is not safe", code)
        self.assertNotIn('DefaultInboundAction -ne "Block"', code)
        # and nothing sets a profile default
        self.assertNotIn("Set-NetFirewallProfile", code)

    def test_it_adds_a_scoped_block_and_a_scoped_allow(self):
        code = _code("firewall.ps1")
        self.assertIn("-Action Block -Protocol TCP", code)
        self.assertIn("-Action Allow -Protocol TCP", code)
        self.assertIn("$BlockRuleName", code)
        # both scoped to the agent port and the bind interface
        self.assertIn("-LocalPort $Port -LocalAddress $Interface -RemoteAddress $BlockRemoteRanges", code)
        self.assertIn("-LocalPort $Port -LocalAddress $Interface -RemoteAddress $AllowFrom", code)

    def test_the_block_scope_is_verified_numerically_not_by_string_membership(self):
        """The old guard was `$bkRemote -contains $AllowFrom` — exact list-element equality, which can never
        detect the backend sitting INSIDE a range string like '0.0.0.0-100.119.23.29', nor an under-covering
        block that leaves a non-backend host reachable. It is replaced by a merge-and-compare over integers.
        Proven on the host: correct complement PASSES; backend-in-range, a coverage gap, a bare IP, and the
        whole space all FAIL."""
        code = _code("firewall.ps1")
        self.assertIn("function Assert-BlockScopeExcludesOnly", code)
        self.assertIn("function IpToUInt", code)
        # the weak string guard is gone
        self.assertNotIn("$bkRemote -contains $AllowFrom", code)
        # the numeric verifier runs on BOTH the derived ranges and the INSTALLED rule's ranges
        self.assertIn("Assert-BlockScopeExcludesOnly -Ranges $BlockRemoteRanges -Backend $AllowFrom", code)
        self.assertIn("Assert-BlockScopeExcludesOnly -Ranges $bkRemote -Backend $AllowFrom", code)
        # the complement is derived from the backend IP (single source of truth)
        self.assertIn("[System.Net.IPAddress]::Parse($AllowFrom).GetAddressBytes()", code)

    def test_the_complement_of_the_reference_backend_is_the_expected_two_ranges(self):
        """Locks the contract the host-verified derivation must produce for the shipped backend."""
        import ipaddress
        bk = int(ipaddress.IPv4Address("100.119.23.29"))
        expect = [f"0.0.0.0-{ipaddress.IPv4Address(bk-1)}", f"{ipaddress.IPv4Address(bk+1)}-255.255.255.255"]
        self.assertEqual(expect, ["0.0.0.0-100.119.23.28", "100.119.23.30-255.255.255.255"])

    def test_broad_allows_are_neutralised_not_fatal(self):
        code = _code("firewall.ps1")
        self.assertIn("NEUTRALISED by the scoped block", code)
        # the old hard Fail on a pre-existing broad allow is gone
        self.assertNotIn("BEFORE adding the agent rule or starting the service", code)

    def test_it_never_touches_the_bridge_port(self):
        code = _code("firewall.ps1")
        # 8788 may appear only in an untouched-assurance message, NEVER in a mutating position
        for line in code.splitlines():
            if "8788" in line:
                for mutating in ("New-NetFirewallRule", "Remove-NetFirewallRule", "Set-NetFirewallRule",
                                 "-LocalPort", "-RemotePort"):
                    self.assertNotIn(mutating, line, f"firewall.ps1 acts on 8788: {line.strip()}")
        self.assertIn("bridge rules (:8788) and all unrelated rules untouched", _read("firewall.ps1"))

    def test_the_default_agent_port_is_8791_from_the_backend_only(self):
        source = _read("firewall.ps1")
        self.assertIn("[int]$Port                 = 8791", source)
        self.assertIn('$AllowFrom         = "100.119.23.29"', source)


class WinSwServiceHarnessTests(SimpleTestCase):
    """The service host is a hash-pinned WinSW WRAPPER, not a pywin32 service.

    Decision (2026-07-24): prefer a wrapper over native pywin32 unless a wrapper demonstrably cannot meet a
    requirement. The pywin32 host caused the 2026-07-24 STOP by (a) writing helper DLLs to System32 and the
    base interpreter and (b) failing the `sc config obj=` virtual-account assignment. WinSW runs the venv
    python as a child, writes nothing global, and takes its account from a reviewed XML.
    """

    XML = "winsw/GuvFXBetaAgent.xml"

    def test_the_winsw_binary_is_hash_pinned_and_refused_on_mismatch(self):
        code = _code("install_service.ps1")
        self.assertIn("function Test-GuvfxWinSw", code)
        self.assertIn('$WinSwSha256 = "923111c7142b3dc783a3c722b19b8a21bcb78222d7a136ac33f0ca8a29f4cb66"',
                      _read("install_service.ps1"))
        self.assertIn("Get-FileHash $Path -Algorithm SHA256", code)
        self.assertIn("REFUSING an unverified executable", code)
        # absence is a hard refusal too, not a skip
        self.assertIn("place the pinned WinSW.NET4.exe there first", code)

    def test_the_service_is_registered_through_winsw_not_pywin32(self):
        code = _code("install_service.ps1")
        self.assertIn("& $ServiceExe install", code)
        # the staged wrapper is re-hashed after copy (a swap between verify and register is caught)
        self.assertIn("staged WinSW exe hash changed after copy", code)
        # none of the pywin32 SERVICE-HOST machinery survives on the service path. (`sc.exe config obj=` is
        # NOT pywin32 machinery - it is the supported post-install identity assignment WinSW v2.12.0 needs.)
        for gone in ("service.py install", "service.py remove", "pythonservice", "win32serviceutil",
                     "pywin32_postinstall"):
            self.assertNotIn(gone, code, f"pywin32 service-host machinery still present: {gone}")

    def test_the_xml_declares_manual_start_virtual_account_and_no_recovery(self):
        xml = _read(self.XML)
        self.assertIn("<startmode>Manual</startmode>", xml)                       # no autostart
        self.assertIn("<username>NT SERVICE\\GuvFXBetaAgent</username>", xml)      # least-privilege virtual account
        self.assertIn('<onfailure action="none" />', xml)                          # nothing auto-restarts pre-approval
        # ABSENCE of any auto-restart is asserted too, not just presence of the 'none' entry: a SECOND
        # <onfailure action="restart"> would re-enable recovery while the 'none' entry stays untouched.
        self.assertEqual(1, xml.count("<onfailure"), "exactly one <onfailure> entry expected")
        for act in ('action="restart"', 'action="reboot"', 'action="run"'):
            self.assertNotIn(act, xml, f"XML re-enables auto-recovery: {act}")
        # runs the VENV python (never the base interpreter / installer) on agent.py
        self.assertIn("<executable>C:\\GuvFX\\beta\\agent-venv\\Scripts\\python.exe</executable>", xml)
        self.assertIn("agent.py", xml)
        self.assertNotIn("C:\\GuvFX\\python311.exe", xml)                          # never the installer
        self.assertNotIn("Program Files\\Python311", xml)                          # never the bridge's base interpreter
        # NT SERVICE virtual account: <allowservicelogon>true</> IS required. HOST-PROVEN 2026-07-24 that
        # WinSW v2.12.0 WITHOUT it ignores <username> and installs LocalSystem (this reverses finding F2).
        # Assert on the ACTIVE config, not the explanatory comment.
        xml_no_comments = re.sub(r"<!--.*?-->", "", xml, flags=re.S)
        self.assertIn("<allowservicelogon>true</allowservicelogon>", xml_no_comments)

    def test_the_apply_verify_fails_closed_on_identity_startmode_and_binary(self):
        source = _read("install_service.ps1")
        # identity must be EXACTLY the virtual account, else throw and do NOT start
        self.assertIn('"$($ci.StartName)" -ne $RunAsUser', source)
        self.assertIn("no LocalSystem fallback; do NOT start", source)
        # ProcessId must be 0 (not running)
        self.assertIn('$ci.ProcessId -ne 0', source)
        # start mode must be Manual/Disabled, else throw
        self.assertIn('$ci.StartMode -notin @("Manual","Disabled")', source)
        # the service binary must be the WinSW wrapper we staged
        self.assertIn("expected the WinSW wrapper $ServiceExe", source)
        # and it must be Stopped
        self.assertIn('if ($svc.Status -ne "Stopped")', source)

    def test_identity_is_assigned_post_install_and_logon_right_granted(self):
        """Option 1 (Nuno, 2026-07-24): WinSW installs, then sc config obj= assigns the virtual account and an
        LSA grant gives it SeServiceLogonRight; both are host-proven and both results are validated."""
        code = _code("install_service.ps1")
        # sc config obj= to the virtual account, result CAPTURED + VALIDATED (not piped to Out-Null)
        self.assertIn('& sc.exe config $ServiceName obj= "$RunAsUser"', code)
        self.assertIn("ChangeServiceConfig SUCCESS", code)
        self.assertIn("sc config obj= failed", code)
        # no `sc.exe config ... | Out-Null` (unvalidated) anywhere
        for line in code.splitlines():
            if "sc.exe config" in line:
                self.assertNotIn("Out-Null", line, f"sc.exe config result unvalidated: {line.strip()}")
        # LSA grant of SeServiceLogonRight to the derived service SID, with post-check + regression guard
        self.assertIn("function Grant-GuvfxServiceLogonRight", code)
        self.assertIn('$SvcLogonRight = "SeServiceLogonRight"', code)
        self.assertIn("LsaAddAccountRights", code)
        self.assertIn("still lacks $SvcLogonRight", code)          # post-check
        self.assertIn("user-right regression", code)               # other rights preserved
        # verify asserts the right is present before declaring success
        self.assertIn("$svcRights -notcontains 'SeServiceLogonRight'", code)

    def test_no_service_start_anywhere_in_the_installer(self):
        code = _code("install_service.ps1")
        for forbidden in ("Start-Service", "$ServiceExe start", "sc.exe start", "Start-Process"):
            self.assertNotIn(forbidden, code, f"installer starts the service: {forbidden}")
        # and the forms the earlier matcher missed (mutation (d): .Start()/Restart-Service/Set-Service running)
        self.assertNotRegex(code, InstallOnlyTests.START_FORMS, "installer starts the service via a missed form")

    def test_the_xml_executable_is_bound_to_the_validated_interpreter(self):
        """(F4/F8) The identity guard validates $Python, but the service runs the XML's <executable>. The
        installer must refuse unless they are the same interpreter, else a relocated -Python validates one
        binary while WinSW launches another."""
        code = _code("install_service.ps1")
        self.assertIn("function Test-GuvfxWinSwXmlContract", code)
        self.assertIn('"$($svc.executable)" -ne $Python', code)
        self.assertIn("Test-GuvfxWinSwXmlContract -XmlPath $XmlSource -Python $Python -AgentDir $AgentDir", code)
        # and the arguments must be tied to -AgentDir\agent.py, not merely contain the literal 'agent.py'
        self.assertIn('[regex]::Escape($agentPy)', code)

    def test_the_service_account_gets_read_on_the_venv(self):
        """(F3) WinSW runs the venv python and the agent loads pywin32 DLLs from the venv; the least-privilege
        account must have RX there, granted AND verified like every other dir - never assumed inherited."""
        code = _code("install_service.ps1")
        self.assertIn("$VenvDir    = Split-Path (Split-Path $Python)", code)
        self.assertIn("foreach ($d in @($AgentDir, $GoldenDir, $WinSwDir, $VenvDir))", code)      # granted
        self.assertIn("$AgentDir, $GoldenDir, $WinSwDir, $VenvDir))", code)                        # verified in the ACE loop

    def test_recovery_is_parsed_not_just_printed(self):
        """(F9) VERIFY claims 'recovery none'; it must PARSE sc.exe qfailure and throw on a restart action,
        not merely print the table."""
        code = _code("install_service.ps1")
        self.assertIn("$qf = (& sc.exe qfailure $ServiceName) -join", code)
        self.assertIn("RESTART|RUN PROCESS|REBOOT", code)
        self.assertIn("service has SCM recovery actions configured; expected none", code)

    def test_stoptimeout_exceeds_the_configured_drain(self):
        """(F7) A stop that force-kills a mutation mid-drain is the exact B-6 failure. The installer must
        assert the XML stop timeout exceeds BETA_AGENT_DRAIN_TIMEOUT_S, and the XML must ship a generous value."""
        code = _code("install_service.ps1")
        self.assertIn('BETA_AGENT_DRAIN_TIMEOUT_S", "Machine"', code)
        self.assertIn("$stopS -le $drainS", code)
        self.assertIn("must EXCEED BETA_AGENT_DRAIN_TIMEOUT_S", code)
        self.assertIn("<stoptimeout>300 sec</stoptimeout>", _read(self.XML))

    def test_global_dll_writes_are_measured_not_asserted(self):
        """(F10 / RULE 11) The 'writes nothing global' claim must be MEASURED (before/after), never a bare
        unconditional Write-Host that prints PASS whether or not a write happened."""
        code = _code("install_service.ps1")
        self.assertIn("function Get-GuvfxGlobalDllState", code)
        self.assertIn("$GlobalDllBaseline = Get-GuvfxGlobalDllState", code)
        self.assertIn("GLOBAL WRITE: this install created", code)
        self.assertIn("GLOBAL WRITE: this install modified", code)
        # the old unconditional claim must be gone
        self.assertNotIn("WinSW install writes nothing to System32 or the base interpreter (wrapper runs", code)


class WinSwUninstallTests(SimpleTestCase):
    """(F6) Teardown must match the WinSW harness: revoke the WinSW/venv ACLs, WinSW-uninstall the service,
    and remove the staged wrapper dir so no orphaned binary or ACE for the deleted virtual-account SID remains.
    """

    def test_uninstall_revokes_the_winsw_and_venv_grants(self):
        code = _code("uninstall.ps1")
        self.assertIn("$AgentDir, $StateDir, $BetaTombstones, $SlotsRoot, $GoldenDir, $WinSwDir, $VenvDir", code)

    def test_uninstall_uses_the_winsw_wrapper_and_removes_its_dir(self):
        code = _code("uninstall.ps1")
        self.assertIn("& $svcExe uninstall", code)
        self.assertIn("Remove-Item -Recurse -Force $WinSwDir", code)
        # sc.exe delete remains as the fallback that removes the registration if the wrapper is gone
        self.assertIn("sc.exe delete $ServiceName", code)

    def test_uninstall_drops_the_stale_pywin32_removal(self):
        """The pywin32 'service.py remove' branch targets the retired host and must not survive the switch."""
        code = _code("uninstall.ps1")
        self.assertNotIn("service.py", code)
