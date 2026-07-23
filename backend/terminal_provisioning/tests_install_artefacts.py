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

    def test_nothing_starts_the_service(self):
        for name in ("install_pool.ps1", "install_service.ps1"):
            source = _code(name)
            self.assertNotRegex(source, r"Start-Service|sc\.exe start|service\.py.*['\"]start['\"]", name)

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
        self.assertIn("start= demand", source)
        self.assertIn('if ($svc.Status -ne "Stopped")', source)

    def test_the_service_install_refuses_localsystem(self):
        """The identity check is the review's whole position on elevation, expressed as a hard failure."""
        source = _read("install_service.ps1")
        self.assertIn("NT SERVICE\\GuvFXBetaAgent", source)
        self.assertIn("LocalSystem means the obj= assignment failed", source)

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
        self.assertIn("foreach ($d in @($AgentDir, $GoldenDir))", source)
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
        self.assertLess(source.index("$ServiceSid = Get-GuvfxServiceSid"), source.index("sc.exe config"))
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
        self.assertIn("$sidBytes, $false, @($u), 1", code)

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
        for name in SCRIPTS:
            raw = open(os.path.join(_BUNDLE, name), "rb").read()
            offenders = sorted({b for b in raw if b > 127})
            self.assertEqual(offenders, [], f"{name} contains non-ASCII bytes {offenders}")

    def test_no_script_relies_on_a_bom(self):
        """A BOM would also work, but it is a subtle dependency; ASCII-only needs no such assumption."""
        for name in SCRIPTS:
            raw = open(os.path.join(_BUNDLE, name), "rb").read(3)
            self.assertNotEqual(raw, b"\xef\xbb\xbf", f"{name} starts with a UTF-8 BOM")


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
        self.assertIn("foreach ($p in @($SlotsRoot, $TombstonesRoot, $ApprovedTasksOut))", code)

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
        self.assertIn("$WRITEISH = ", code)
        for right in ("CreateFiles", "AppendData", "CreateDirectories", "WriteData",
                      "Delete", "ChangePermissions", "TakeOwnership"):
            self.assertIn(f"FileSystemRights]::{right}", code, right)
        self.assertIn("-band [int]$WRITEISH", code)

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

    def test_no_password_prompt_can_be_reached_without_mutating(self):
        """Get-SlotSecret is only ever called from inside a DoIt block, which -VerifyOnly skips."""
        code = _code("install_pool.ps1")
        body = code[code.index("function Get-SlotSecret"):]
        for i, line in enumerate(body.splitlines(), 1):
            if "Get-SlotSecret $user" in line and "function" not in line:
                self.assertNotIn("if ($Check)", body[:body.index(line)][-400:],
                                 "a password prompt is reachable from a verification-only path")
