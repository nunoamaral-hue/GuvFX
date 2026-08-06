"""Supervised-installer tests (WORKSTREAM G) — validate the SINGLE sanctioned installer supports BOTH the
DARK and SUPERVISED deployment profiles safely, without ever bypassing the identity assignment.

PowerShell cannot be executed on CI (no pwsh), so — consistent with the project's machine-readable-artefact +
static-validation pattern — this suite: (a) validates both WinSW XML profiles' invariants; (b) static-analyses
install_service.ps1 for the required gates and the ABSENCE of dangerous patterns; (c) checks the installer is
ASCII-only (RULE 9 corollary) and structurally balanced; (d) validates the installer-contract JSON. The real
PowerShell parse gate + behavioural run happen on-host in PLAN mode first (documented, RULE 9).
"""
import json
import os
import re
import xml.etree.ElementTree as ET

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
_INSTALLER = os.path.join(_BUNDLE, "install_service.ps1")
_DARK_XML = os.path.join(_BUNDLE, "winsw", "GuvFXBetaAgent.xml")
_SUP_XML = os.path.join(_BUNDLE, "winsw", "GuvFXBetaAgent.supervised.xml")
_CONTRACT = os.path.join(_REPO, "docs", "operations", "validation-agent", "installer-contract.json")


def _installer_text():
    return open(_INSTALLER, encoding="utf-8").read()


class DarkXmlInvariantsTests(SimpleTestCase):
    def setUp(self):
        self.r = ET.parse(_DARK_XML).getroot()

    def test_manual_no_recovery(self):
        self.assertEqual(self.r.findtext("startmode"), "Manual")
        onf = self.r.findall("onfailure")
        self.assertEqual(len(onf), 1)
        self.assertEqual(onf[0].get("action"), "none")

    def test_runs_agent_under_venv(self):
        self.assertIn("agent.py", self.r.findtext("arguments"))
        self.assertIn("python.exe", self.r.findtext("executable"))


class SupervisedXmlInvariantsTests(SimpleTestCase):
    def setUp(self):
        self.r = ET.parse(_SUP_XML).getroot()

    def test_automatic_delayed_with_restart_floor(self):
        self.assertEqual(self.r.findtext("startmode"), "Automatic")
        self.assertEqual(self.r.findtext("delayedAutoStart"), "true")
        onf = self.r.findall("onfailure")
        self.assertGreaterEqual(len(onf), 3)
        self.assertTrue(all(o.get("action") == "restart" for o in onf))
        self.assertTrue(self.r.findtext("resetfailure"))

    def test_launch_markers_present(self):
        names = {e.get("name") for e in self.r.findall("env")}
        for n in ("BETA_AGENT_SERVICE_IDENTITY", "BETA_AGENT_SUPERVISED_TOKEN",
                  "BETA_AGENT_REFUSE_UNSUPERVISED_LAUNCH"):
            self.assertIn(n, names)

    def test_refuse_ships_off_and_token_is_placeholder_or_set(self):
        envs = {e.get("name"): e.get("value") for e in self.r.findall("env")}
        self.assertEqual(envs["BETA_AGENT_REFUSE_UNSUPERVISED_LAUNCH"], "0")  # must not brick manual recovery pre-proof
        # source ships the placeholder; the installer substitutes a non-secret value at stage
        self.assertTrue(envs["BETA_AGENT_SUPERVISED_TOKEN"])

    def test_identity_and_stoptimeout_shared(self):
        self.assertEqual(self.r.findtext("serviceaccount/username"), "NT SERVICE\\GuvFXBetaAgent")
        self.assertTrue(self.r.findtext("stoptimeout"))

    def test_supervised_xml_is_ascii(self):
        b = open(_SUP_XML, "rb").read()
        self.assertEqual([c for c in b if c > 127], [])


class InstallerStaticAnalysisTests(SimpleTestCase):
    def setUp(self):
        self.t = _installer_text()

    def test_profile_is_mandatory_and_explicit(self):
        self.assertIn('[Parameter(Mandatory=$true)][ValidateSet("Dark","Supervised")][string]$InstallProfile', self.t)
        # no silent default / auto-detection of the profile
        self.assertNotRegex(self.t, r'\$InstallProfile\s*=\s*["\']')

    def test_both_profiles_route_through_identity_assignment(self):
        # the LocalSystem fix must be unconditional (not inside a Dark-only branch)
        self.assertIn("Assign-GuvfxIdentity", self.t)
        self.assertIn('sc.exe config $ServiceName obj= "$RunAsUser"', self.t)
        self.assertIn("ChangeServiceConfig SUCCESS", self.t)
        self.assertIn("Grant-GuvfxServiceLogonRight", self.t)

    def test_verify_rejects_localsystem(self):
        # StartName must be verified == RunAsUser before success, for both profiles
        self.assertIn('if ("$($ci.StartName)" -ne $RunAsUser)', self.t)
        self.assertIn("no LocalSystem fallback", self.t)

    def test_rollback_exists_and_is_invoked_on_failure(self):
        self.assertIn("function Restore-GuvfxServiceFromSnapshot", self.t)
        self.assertIn("function Backup-GuvfxServiceState", self.t)
        # the install/verify catch (the LAST catch in the file) must invoke rollback then re-throw
        tail = self.t[self.t.rindex("catch {"):]
        self.assertIn("Restore-GuvfxServiceFromSnapshot -Snapshot $Snapshot", tail)
        self.assertLess(tail.index("Restore-GuvfxServiceFromSnapshot"), tail.rindex("throw"))
        # rollback fails loud if identity not restored
        self.assertIn("ROLLBACK INCOMPLETE", self.t)

    def test_verify_is_profile_aware(self):
        self.assertIn('DARK service StartMode', self.t)
        self.assertIn('SUPERVISED service StartMode', self.t)
        # dark => recovery none; supervised => restart tiers required
        self.assertIn("DARK service has SCM recovery actions", self.t)
        self.assertIn("SUPERVISED service has NO SCM restart actions", self.t)

    def test_xml_contract_is_profile_aware(self):
        self.assertIn('if ($InstallProfile -eq "Dark")', self.t)
        self.assertIn("SUPERVISED XML <startmode>", self.t)
        self.assertIn("bounded-backoff restart FLOOR", self.t)

    def test_install_only_no_start(self):
        # the installer never starts the service (both profiles install STOPPED)
        self.assertNotRegex(self.t, r"Start-Service\s")
        self.assertIn('expected Stopped (install-only, both profiles)', self.t)

    def test_secret_safe_logging(self):
        # the supervised token VALUE is never emitted; log helper forbids secrets by contract
        self.assertIn("value never logged", self.t)
        self.assertIn("NON-SECRET", self.t)
        # no obvious credential echoing
        self.assertNotRegex(self.t, r"Write-Host.*\$SupervisedToken\b")
        self.assertNotRegex(self.t, r"(?i)Write-Host.*(keyring|password|BETA_AGENT_KEYRING)\b.*=\s*\$")

    def test_ascii_only(self):
        b = open(_INSTALLER, "rb").read()
        self.assertEqual([i for i, c in enumerate(b) if c > 127], [])
        self.assertNotEqual(b[:3], b"\xef\xbb\xbf")

    def test_structurally_balanced(self):
        self.assertEqual(self.t.count("{"), self.t.count("}"))
        self.assertEqual(self.t.count("("), self.t.count(")"))
        self.assertEqual(self.t.count("@'\n"), self.t.count("\n'@"))  # LSA here-string closed


class InstallerReviewHardeningTests(SimpleTestCase):
    """Regression guards for the 6 confirmed adversarial-review findings (2026-08-06)."""
    def setUp(self):
        self.t = _installer_text()

    def test_runas_identity_is_pinned(self):
        self.assertIn('if ($RunAsUser -ne "NT SERVICE\\GuvFXBetaAgent")', self.t)

    def test_reinstall_uninstalls_first(self):
        # WinSW v2.12 install does not update in place; a re-install must uninstall-first so the new XML applies
        self.assertIn("if ($Snapshot.Existed) { Uninstall-GuvfxServiceVerified", self.t)
        self.assertIn("function Uninstall-GuvfxServiceVerified", self.t)
        self.assertIn("function Wait-GuvfxServiceRemoved", self.t)

    def test_uninstall_is_verified_not_swallowed(self):
        # removal must be confirmed via SCM re-query; a failure is loud, never caught-and-ignored
        self.assertIn("still registered after uninstall", self.t)
        self.assertNotIn("try { & $ServiceExe uninstall 2>&1 | Out-Null } catch {}", self.t)

    def test_rollback_no_prior_branch_verifies_removal(self):
        # the no-prior branch must throw ROLLBACK INCOMPLETE if removal is unconfirmed (not log ok)
        self.assertIn("could not confirm removal of the freshly-created", self.t)
        self.assertNotIn('detail="no_prior_service_removed"', self.t)  # the old unconditional-ok message is gone

    def test_early_guard_refuses_unknown_baseline_xml(self):
        self.assertIn("cannot guarantee a safe rollback", self.t)

    def test_rollback_reinstall_checks_exit_code(self):
        self.assertIn("reinstall of the baseline service failed", self.t)

    def test_plan_mode_gates_all_service_mutations(self):
        # The backup + whole install/identity/rollback flow only runs under -Apply (PLAN mutates nothing).
        self.assertIn("if ($Apply) {\n  $Snapshot = Backup-GuvfxServiceState", self.t)
        # rollback (Restore) is invoked only inside the catch, under an -Apply guard
        tail = self.t[self.t.rindex("catch {"):]
        self.assertIn("if ($Apply) {", tail)
        self.assertLess(tail.index("if ($Apply)"), tail.index("Restore-GuvfxServiceFromSnapshot"))
        # the register-time WinSW install is wrapped in DoIt (gated), never a bare top-level statement
        self.assertNotRegex(self.t, r"(?m)^& \$ServiceExe install\b")


class InstallerContractTests(SimpleTestCase):
    def setUp(self):
        self.c = json.load(open(_CONTRACT, encoding="utf-8"))

    def test_single_mechanism_two_profiles(self):
        self.assertTrue(self.c["single_sanctioned_mechanism"])
        self.assertTrue(self.c["profile_parameter"]["mandatory"])
        self.assertFalse(self.c["profile_parameter"]["inference"])
        self.assertEqual(set(self.c["profiles"]), {"Dark", "Supervised"})

    def test_contract_matches_xml(self):
        self.assertEqual(ET.parse(_DARK_XML).getroot().findtext("startmode"),
                         self.c["profiles"]["Dark"]["startmode"])
        self.assertEqual(ET.parse(_SUP_XML).getroot().findtext("startmode"),
                         self.c["profiles"]["Supervised"]["startmode"])

    def test_identity_guarantee_documented(self):
        self.assertIn("LocalSystem", self.c["identity_guarantee"])
        self.assertIn("FAILS CLOSED", self.c["identity_guarantee"])
        self.assertIn("token", " ".join(self.c["logging"]["never_logs"]).lower())
