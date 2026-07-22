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
            self.assertIn(f"{field} =", source, field)


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
