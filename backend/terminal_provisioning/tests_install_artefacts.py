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
        self.assertIn("(OI)(CI)RX", source)

    def test_grant_failures_are_detected(self):
        """icacls is a native command: $ErrorActionPreference does not apply, so a failed grant is silent."""
        source = _code("install_service.ps1")
        self.assertIn("$LASTEXITCODE -ne 0", source)
        self.assertIn("the grant did not take; do NOT start", source)

    def test_the_approval_file_is_readable_by_the_service_account(self):
        source = _code("install_pool.ps1")
        self.assertIn("NT SERVICE\\GuvFXBetaAgent:(R)", source)

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
        self.assertIn("does not carry /portable — refusing to approve it", source)

    def test_registration_is_re_runnable(self):
        source = _code("install_pool.ps1")
        self.assertEqual(source.count("-RunLevel Limited -Force"), 2)

    def test_the_password_is_prompted_once_and_confirmed(self):
        source = _code("install_pool.ps1")
        self.assertIn("Confirm password for $user", source)
        self.assertIn("function Get-SlotSecret", source)


class UserRightManagementTests(SimpleTestCase):
    """User rights are managed with the LSA policy API, never secedit.

    The install-only baseline found SeBatchLogonRight ABSENT from local policy on the target host, so
    Windows' effective defaults were in force. secedit writes a COMPLETE assignment line — creating one
    containing only our four SIDs would have replaced those defaults machine-wide. LsaAddAccountRights adds
    one right to one account and touches nothing else, so there is no line to rewrite and no need for the
    installer to know what the defaults are.

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
        """A broken interop must abort while the host is still untouched, not after four accounts exist."""
        code = _code("install_pool.ps1")
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
