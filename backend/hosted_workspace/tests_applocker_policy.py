"""Beta Readiness Stream 6 (M1) — the multi-tenant AppLocker policy compiler negative controls.

Proves the model is additive, isolated, idempotent and reversible, that Customer Zero can never be removed by a
tenant op, that the hardened publisher-based posture (no writable-path executable Allow) is preserved, and that
capacity scales with no cross-tenant collision. Pure — no host, no DB.
"""
import os
import xml.etree.ElementTree as ET

from django.test import SimpleTestCase

from hosted_workspace import applocker_policy as A

_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "terminal_provisioning", "windows", "applocker", "guvfx-hosted-auditonly.xml")


def _sid(n):
    return f"S-1-5-21-11-22-33-{1000 + n}"


def _base():
    return A.load_base_policy(open(_TEMPLATE).read())


def _exe_rule_ids(xml, account_id):
    root = ET.fromstring(xml)
    out = []
    for coll in root.findall("RuleCollection"):
        for r in coll:
            rid = r.get("Id", "")
            if A._is_tenant_rule(rid, account_id):
                out.append(rid)
    return out


class BaseTests(SimpleTestCase):
    def test_base_strips_all_hosted_denies(self):
        self.assertEqual(A.tenant_account_ids(_base()), set())
        self.assertNotIn(A.HOSTED_SID_TOKEN, _base())

    def test_base_invariants_hold(self):
        self.assertTrue(A.assert_base_invariants(_base()))

    def test_no_writable_tree_exe_allow_in_base(self):
        # The canonical bypass: an Everyone/Allow EXE path rule over the writable C:\GuvFX\accounts tree. The
        # certified base must NOT have one (only the MetaQuotes publisher rule) — so a renamed cmd.exe dropped in
        # the tree matches no Allow and is denied by default.
        root = ET.fromstring(_base())
        exe = [c for c in root.findall("RuleCollection") if c.get("Type") == "Exe"][0]
        for r in exe:
            if r.tag == "FilePathRule" and r.get("Action") == "Allow":
                for cond in r.findall("Conditions/FilePathCondition"):
                    self.assertNotIn("accounts", (cond.get("Path") or "").lower())


class MergeIsolationTests(SimpleTestCase):
    def test_effective_is_base_plus_tenants(self):
        eff = A.compile_effective_policy(_base(), [(1, _sid(1)), (2, _sid(2))])
        self.assertEqual(A.tenant_account_ids(eff), {1, 2})
        self.assertTrue(A.assert_base_invariants(eff))    # tenants never weaken the base

    def test_merge_account_two_does_not_touch_account_one(self):
        eff1 = A.compile_effective_policy(_base(), [(1, _sid(1))])
        ids1_before = _exe_rule_ids(eff1, 1)
        eff2 = A.merge_tenant(eff1, 2, _sid(2))
        self.assertEqual(_exe_rule_ids(eff2, 1), ids1_before)   # account 1 rules byte-identical
        self.assertEqual(len(_exe_rule_ids(eff2, 2)), len(A.DENY_BINARIES))

    def test_merge_third_does_not_alter_second(self):
        eff = A.compile_effective_policy(_base(), [(1, _sid(1)), (2, _sid(2))])
        ids2 = _exe_rule_ids(eff, 2)
        eff3 = A.merge_tenant(eff, 3, _sid(3))
        self.assertEqual(_exe_rule_ids(eff3, 2), ids2)

    def test_merge_is_idempotent(self):
        eff = A.compile_effective_policy(_base(), [(2, _sid(2))])
        again = A.merge_tenant(eff, 2, _sid(2))
        self.assertFalse(A.policy_changed(eff, again))

    def test_compile_dedupes_duplicate_accounts(self):
        eff = A.compile_effective_policy(_base(), [(2, _sid(2)), (2, _sid(2))])
        self.assertEqual(len(_exe_rule_ids(eff, 2)), len(A.DENY_BINARIES))

    def test_merge_fragment_is_not_configured_so_it_never_downgrades_enforcement(self):
        # The mergeable fragment's Exe collection is NotConfigured, so Set-AppLockerPolicy -Merge adds the rules
        # without changing the target collection's enforcement mode (can't downgrade CZ's Enforced collection).
        frag = A.tenant_fragment(2, _sid(2))
        coll = ET.fromstring(frag).find("RuleCollection")
        self.assertEqual(coll.get("Type"), "Exe")
        self.assertEqual(coll.get("EnforcementMode"), "NotConfigured")
        self.assertEqual(len(list(coll)), len(A.DENY_BINARIES))


class RemoveTests(SimpleTestCase):
    def test_remove_target_only(self):
        eff = A.compile_effective_policy(_base(), [(1, _sid(1)), (2, _sid(2)), (3, _sid(3))])
        out, removed = A.remove_tenant(eff, 3)
        self.assertEqual(removed, len(A.DENY_BINARIES))
        self.assertEqual(A.tenant_account_ids(out), {1, 2})
        self.assertTrue(A.assert_base_invariants(out))

    def test_customer_zero_removal_forbidden(self):
        eff = A.compile_effective_policy(_base(), [(1, _sid(1)), (2, _sid(2))])
        with self.assertRaises(A.AppLockerPolicyError) as cm:
            A.remove_tenant(eff, 1)
        self.assertEqual(str(cm.exception), "customer_zero_removal_forbidden")


class ValidationTests(SimpleTestCase):
    def test_malformed_account_rejected(self):
        for bad in (0, -1, "x", 0x10000000):
            with self.assertRaises(A.AppLockerPolicyError):
                A.tenant_fragment(bad, _sid(2))

    def test_malformed_sid_rejected(self):
        for bad in ("", "  ", A.HOSTED_SID_TOKEN, "notasid", "S-1"):
            with self.assertRaises(A.AppLockerPolicyError):
                A.tenant_fragment(2, bad)

    def test_shared_principal_sid_refused(self):
        for shared in ("S-1-1-0", "S-1-5-32-544"):
            with self.assertRaises(A.AppLockerPolicyError):
                A.tenant_fragment(2, shared)

    def test_writable_path_bypass_is_a_permanent_regression(self):
        # Inject an Everyone Allow over the writable accounts tree into the Exe collection → invariants MUST fail.
        root = ET.fromstring(_base())
        exe = [c for c in root.findall("RuleCollection") if c.get("Type") == "Exe"][0]
        bad = ET.SubElement(exe, "FilePathRule",
                            {"Id": "dead0000-0000-0000-0000-000000000099", "Name": "bad", "Action": "Allow",
                             "UserOrGroupSid": "S-1-1-0"})
        cond = ET.SubElement(bad, "Conditions")
        ET.SubElement(cond, "FilePathCondition", {"Path": r"C:\GuvFX\accounts\*"})
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_base_invariants(ET.tostring(root, encoding="unicode"))

    def test_metaquotes_publisher_and_admin_recovery_preserved(self):
        eff = A.compile_effective_policy(_base(), [(2, _sid(2))])
        root = ET.fromstring(eff)
        exe = [c for c in root.findall("RuleCollection") if c.get("Type") == "Exe"][0]
        self.assertTrue(any(r.tag == "FilePublisherRule" and r.get("Action") == "Allow" for r in exe))
        self.assertTrue(any(r.get("UserOrGroupSid") == "S-1-5-32-544" and r.get("Action") == "Allow" for r in exe))


def _struct(xml):
    """Structural fingerprint of a policy (ignores whitespace/comments/rule order) for drift comparison. Captures
    path, publisher (name/product/binary + version range), file-hash conditions AND any Exceptions block — so a
    hand-edit that only alters a version range or slips in an <Exceptions> element cannot pass the drift guard."""
    root = ET.fromstring(xml)
    out = []
    for coll in root.findall("RuleCollection"):
        rules = []
        for r in coll:
            conds = []
            for c in r.findall("Conditions/FilePathCondition"):
                conds.append(("path", c.get("Path") or ""))
            for c in r.findall("Conditions/FilePublisherCondition"):
                vr = c.find("BinaryVersionRange")
                conds.append(("pub", c.get("PublisherName") or "", c.get("ProductName") or "",
                              c.get("BinaryName") or "",
                              (vr.get("LowSection") or "") if vr is not None else "",
                              (vr.get("HighSection") or "") if vr is not None else ""))
            for c in r.findall("Conditions/FileHashCondition/FileHash"):
                conds.append(("hash", c.get("Type") or "", c.get("Data") or ""))
            excs = []
            for e in r.findall("Exceptions/*"):
                excs.append((e.tag, tuple(sorted((k, v or "") for k, v in e.attrib.items()))))
            rules.append((r.get("Id"), r.tag, r.get("UserOrGroupSid"), r.get("Action"),
                          tuple(sorted(conds)), tuple(sorted(excs))))
        out.append((coll.get("Type"), coll.get("EnforcementMode"), tuple(sorted(rules))))
    return tuple(sorted(out))


class AllowModelTests(SimpleTestCase):
    """STREAM 10B — the canonical deny-by-default allow model (ADR-0042). These are the permanent regression
    guards the packet requires: no broad Everyone Windows/Program Files allow, no future widening of the surface,
    no interpreter/LOLBIN ever granted to a hosted tenant, and the committed templates match the generator."""

    def test_generator_invariants_hold_both_modes(self):
        for mode in ("AuditOnly", "Enabled"):
            xml = A.generate_base_policy(mode)
            self.assertTrue(A.assert_allow_model_invariants(xml))
            self.assertTrue(A.assert_base_invariants(xml))
            self.assertEqual(ET.fromstring(xml).find("RuleCollection").get("EnforcementMode"), mode)

    def test_bad_enforcement_rejected(self):
        with self.assertRaises(A.AppLockerPolicyError):
            A.generate_base_policy("Nonsense")

    def test_no_broad_everyone_windows_or_pf_allow(self):
        exe = [c for c in ET.fromstring(A.generate_base_policy()).findall("RuleCollection")
               if c.get("Type") == "Exe"][0]
        for r in exe:
            if r.tag == "FilePathRule" and r.get("Action") == "Allow" and r.get("UserOrGroupSid") == A.EVERYONE_SID:
                for cond in r.findall("Conditions/FilePathCondition"):
                    p = (cond.get("Path") or "").upper().rstrip("\\")
                    self.assertNotIn(p, ("%WINDIR%", "%WINDIR%\\*", "%PROGRAMFILES%", "%PROGRAMFILES%\\*",
                                         "%SYSTEM32%\\*", "%OSDRIVE%\\*", "*"))

    def test_reintroducing_broad_everyone_allow_is_caught(self):
        root = ET.fromstring(A.generate_base_policy())
        exe = [c for c in root.findall("RuleCollection") if c.get("Type") == "Exe"][0]
        bad = ET.SubElement(exe, "FilePathRule", {"Id": "dead0000-0000-a11e-0000-000000000998",
                            "Name": "widen", "Action": "Allow", "UserOrGroupSid": A.EVERYONE_SID})
        ET.SubElement(ET.SubElement(bad, "Conditions"), "FilePathCondition", {"Path": r"%WINDIR%\*"})
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def test_forbidden_interpreter_allow_is_caught(self):
        root = ET.fromstring(A.generate_base_policy())
        exe = [c for c in root.findall("RuleCollection") if c.get("Type") == "Exe"][0]
        bad = ET.SubElement(exe, "FilePathRule", {"Id": "dead0000-0000-a11e-0000-000000000999",
                            "Name": "py", "Action": "Allow", "UserOrGroupSid": A.EVERYONE_SID})
        ET.SubElement(ET.SubElement(bad, "Conditions"), "FilePathCondition", {"Path": r"%SYSTEM32%\python.exe"})
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def test_hosted_session_allow_contains_no_interpreter_or_lolbin(self):
        forbidden = {f.lower() for f in A.FORBIDDEN_HOSTED_ALLOW}
        for b in A.HOSTED_SESSION_ALLOW:
            self.assertNotIn(b.lower(), forbidden, f"{b} is a forbidden primitive")

    def test_system_and_virtual_account_sids_kept_windows_exec(self):
        allowed = {r.get("UserOrGroupSid") for r in ET.fromstring(A.generate_base_policy()).find("RuleCollection")
                   if r.get("Action") == "Allow"}
        for sid, _ in A.SYSTEM_EXEC_SIDS:
            self.assertIn(sid, allowed, f"missing Windows-exec allow for {sid}")

    def test_missing_system_exec_allow_is_caught(self):
        # removing a system/virtual-account allow (would break the OS/compositor) MUST fail the invariant.
        root = ET.fromstring(A.generate_base_policy())
        exe = [c for c in root.findall("RuleCollection") if c.get("Type") == "Exe"][0]
        for r in list(exe):
            if r.get("UserOrGroupSid") == "S-1-5-90-0":     # Window Manager Group
                exe.remove(r)
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def test_committed_templates_match_generator(self):
        base = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "terminal_provisioning", "windows", "applocker")
        for mode, fname in (("AuditOnly", "guvfx-hosted-auditonly.xml"), ("Enabled", "guvfx-hosted-enforce.xml")):
            committed = open(os.path.join(base, fname)).read()
            self.assertEqual(_struct(committed), _struct(A.generate_base_policy(mode)),
                             f"{fname} drifted from generate_base_policy('{mode}') - regenerate, do not hand-edit")

    # ── STREAM 10B adversarial-review closures (positive-allowlist guard over EVERY collection) ──────────────

    def test_dll_collection_present_and_required(self):
        # The DLL-sideload / HKCU-COM-hijack native-code path (STREAM 10 allow-surface review, HIGH): the model
        # MUST carry an enforced Dll collection; removing it MUST fail the invariant.
        root = ET.fromstring(A.generate_base_policy())
        self.assertIn("Dll", {c.get("Type") for c in root.findall("RuleCollection")})
        for c in list(root):
            if c.get("Type") == "Dll":
                root.remove(c)
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def test_dll_everyone_allows_are_publisher_only(self):
        # Re-verify HIGH: a tenant-reachable %WINDIR%\* DLL PATH allow is bypassable via user-writable %WINDIR%
        # subdirs (Temp, System32\spool\drivers\color, ...). The tenant (Everyone) DLL surface must be PUBLISHER
        # rules only (Microsoft OS signer + MetaQuotes) — a planted unsigned DLL then matches nothing, anywhere.
        dll = [c for c in ET.fromstring(A.generate_base_policy()).findall("RuleCollection")
               if c.get("Type") == "Dll"][0]
        everyone = [r for r in dll if r.get("Action") == "Allow" and r.get("UserOrGroupSid") == A.EVERYONE_SID]
        self.assertTrue(everyone)
        for r in everyone:
            self.assertEqual(r.tag, "FilePublisherRule", "Everyone DLL allow must be publisher-only, not a path")
            pubs = {c.get("PublisherName") for c in r.findall("Conditions/FilePublisherCondition")}
            self.assertTrue(pubs <= {A.MICROSOFT_WINDOWS_PUBLISHER_NAME, A.METAQUOTES_PUBLISHER_NAME}, pubs)

    def test_tenant_windir_dll_path_allow_is_caught(self):
        # The exact regression the re-verify caught: an Everyone %WINDIR%\* PATH allow in the Dll collection.
        root = ET.fromstring(A.generate_base_policy())
        dll = [c for c in root.findall("RuleCollection") if c.get("Type") == "Dll"][0]
        bad = ET.SubElement(dll, "FilePathRule", {"Id": "dead0000-0000-a11e-0000-000000000992",
                            "Name": "windir-dll", "Action": "Allow", "UserOrGroupSid": A.EVERYONE_SID})
        ET.SubElement(ET.SubElement(bad, "Conditions"), "FilePathCondition", {"Path": r"%WINDIR%\*"})
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def test_microsoft_publisher_allowed_in_dll_but_not_exe(self):
        # A Microsoft-signed DLL is trusted code (OK for Dll); a Microsoft-signed EXE is a signed-LOLBIN ACE
        # (rundll32/regsvr32/...) so MS publisher must be REJECTED in the Exe collection.
        def inject(ctype):
            root = ET.fromstring(A.generate_base_policy())
            coll = [c for c in root.findall("RuleCollection") if c.get("Type") == ctype][0]
            bad = ET.SubElement(coll, "FilePublisherRule", {"Id": "dead0000-0000-a11e-0000-000000000991",
                                "Name": "ms", "Action": "Allow", "UserOrGroupSid": A.EVERYONE_SID})
            pc = ET.SubElement(ET.SubElement(bad, "Conditions"), "FilePublisherCondition",
                               {"PublisherName": A.MICROSOFT_WINDOWS_PUBLISHER_NAME, "ProductName": "*",
                                "BinaryName": "*"})
            ET.SubElement(pc, "BinaryVersionRange", {"LowSection": "*", "HighSection": "*"})
            return ET.tostring(root, encoding="unicode")
        self.assertTrue(A.assert_allow_model_invariants(inject("Dll")))          # allowed in Dll
        with self.assertRaises(A.AppLockerPolicyError):                          # rejected in Exe
            A.assert_allow_model_invariants(inject("Exe"))

    def test_duplicate_collection_type_does_not_hide_widening(self):
        # A SECOND Exe collection carrying a broad Everyone allow must NOT be shadowed by a dict-by-Type analysis.
        # The dupe's EnforcementMode MATCHES the generator default (AuditOnly) so the mixed-mode guard does NOT
        # fire first — this test must exercise the LIST-iteration path, not raise for an unrelated reason.
        root = ET.fromstring(A.generate_base_policy("AuditOnly"))
        dupe = ET.SubElement(root, "RuleCollection", {"Type": "Exe", "EnforcementMode": "AuditOnly"})
        bad = ET.SubElement(dupe, "FilePathRule", {"Id": "dead0000-0000-a11e-0000-000000000990",
                            "Name": "widen", "Action": "Allow", "UserOrGroupSid": A.EVERYONE_SID})
        ET.SubElement(ET.SubElement(bad, "Conditions"), "FilePathCondition", {"Path": r"%WINDIR%\*"})
        with self.assertRaisesRegex(A.AppLockerPolicyError, "tenant_reachable"):
            A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def test_uniform_notconfigured_mode_is_caught(self):
        # A policy with EVERY collection uniformly NotConfigured passes the mixed-mode check but enforces nothing.
        root = ET.fromstring(A.generate_base_policy("Enabled"))
        for c in root.findall("RuleCollection"):
            c.set("EnforcementMode", "NotConfigured")
        with self.assertRaisesRegex(A.AppLockerPolicyError, "non_enforcing_mode"):
            A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def test_tenant_path_rule_with_no_conditions_is_caught(self):
        # A tenant-reachable FilePathRule with zero FilePathCondition children blesses nothing and must be rejected.
        root = ET.fromstring(A.generate_base_policy())
        exe = [c for c in root.findall("RuleCollection") if c.get("Type") == "Exe"][0]
        bad = ET.SubElement(exe, "FilePathRule", {"Id": "dead0000-0000-a11e-0000-00000000098e",
                            "Name": "empty", "Action": "Allow", "UserOrGroupSid": A.EVERYONE_SID})
        ET.SubElement(bad, "Conditions")     # present but empty — no FilePathCondition
        with self.assertRaisesRegex(A.AppLockerPolicyError, "empty_path_rule"):
            A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def test_mixed_enforcement_modes_is_caught(self):
        root = ET.fromstring(A.generate_base_policy("Enabled"))
        [c for c in root.findall("RuleCollection") if c.get("Type") == "Dll"][0].set("EnforcementMode",
                                                                                     "NotConfigured")
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def test_lowercase_action_allow_is_not_evaded(self):
        # Action="allow" (wrong case) must still be analyzed as an Allow, not skipped as non-Allow.
        root = ET.fromstring(A.generate_base_policy())
        exe = [c for c in root.findall("RuleCollection") if c.get("Type") == "Exe"][0]
        bad = ET.SubElement(exe, "FilePathRule", {"Id": "dead0000-0000-a11e-0000-00000000098f",
                            "Name": "widen", "Action": "allow", "UserOrGroupSid": A.EVERYONE_SID})
        ET.SubElement(ET.SubElement(bad, "Conditions"), "FilePathCondition", {"Path": r"%WINDIR%\*"})
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def _inject_exe_allow(self, sid, path, tag="FilePathRule"):
        root = ET.fromstring(A.generate_base_policy())
        exe = [c for c in root.findall("RuleCollection") if c.get("Type") == "Exe"][0]
        bad = ET.SubElement(exe, tag, {"Id": "dead0000-0000-a11e-0000-000000000997", "Name": "widen",
                                       "Action": "Allow", "UserOrGroupSid": sid})
        cond = ET.SubElement(bad, "Conditions")
        if tag == "FilePathRule":
            ET.SubElement(cond, "FilePathCondition", {"Path": path})
        else:
            pc = ET.SubElement(cond, "FilePublisherCondition",
                               {"PublisherName": path, "ProductName": "*", "BinaryName": "*"})
            ET.SubElement(pc, "BinaryVersionRange", {"LowSection": "*", "HighSection": "*"})
        return ET.tostring(root, encoding="unicode")

    def test_broad_allow_to_users_group_is_caught(self):            # GAP-1(b): tenant-inclusive group != Everyone
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_allow_model_invariants(self._inject_exe_allow("S-1-5-32-545", r"%WINDIR%\*"))

    def test_broad_allow_to_authenticated_users_is_caught(self):
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_allow_model_invariants(self._inject_exe_allow("S-1-5-11", r"%PROGRAMFILES%\*"))

    def test_system32_subdir_alias_to_everyone_is_caught(self):     # GAP-1(a): %WINDIR%\System32\* alias
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_allow_model_invariants(self._inject_exe_allow(A.EVERYONE_SID, r"%WINDIR%\System32\*"))

    def test_non_metaquotes_publisher_to_everyone_is_caught(self):  # GAP-2: publisher rules now inspected
        with self.assertRaises(A.AppLockerPolicyError):
            A.assert_allow_model_invariants(self._inject_exe_allow(
                A.EVERYONE_SID, "O=MICROSOFT CORPORATION, L=REDMOND, S=WASHINGTON, C=US", tag="FilePublisherRule"))

    def test_broad_allow_in_script_or_msi_collection_is_caught(self):   # GAP-4: all collections inspected
        for ctype in ("Script", "Msi", "Dll"):
            root = ET.fromstring(A.generate_base_policy())
            coll = [c for c in root.findall("RuleCollection") if c.get("Type") == ctype][0]
            bad = ET.SubElement(coll, "FilePathRule", {"Id": "dead0000-0000-a11e-0000-000000000993",
                                "Name": "widen", "Action": "Allow", "UserOrGroupSid": A.EVERYONE_SID})
            ET.SubElement(ET.SubElement(bad, "Conditions"), "FilePathCondition", {"Path": "*"})
            with self.assertRaises(A.AppLockerPolicyError, msg=f"{ctype} widening not caught"):
                A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def test_committed_templates_pass_invariants_on_file_contents(self):   # GAP-5: assert on the deployed artifact
        base = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "terminal_provisioning", "windows", "applocker")
        for fname in ("guvfx-hosted-auditonly.xml", "guvfx-hosted-enforce.xml"):
            committed = open(os.path.join(base, fname)).read()
            self.assertTrue(A.assert_allow_model_invariants(committed), fname)
            self.assertTrue(A.assert_base_invariants(committed), fname)

    def test_hosted_session_allow_is_frozen(self):   # GAP-3: any curated-list change is a visible, reviewed edit
        self.assertEqual(set(A.HOSTED_SESSION_ALLOW), {
            "rdpinit.exe", "rdpshell.exe", "rdpclip.exe", "tstheme.exe", "userinit.exe", "sihost.exe",
            "ctfmon.exe", "taskhostw.exe", "conhost.exe", "shellappruntime.exe", "shellhost.exe", "wlrmdr.exe"},
            "HOSTED_SESSION_ALLOW changed - update this expected set in the SAME commit and re-certify the soak")

    def test_expanded_lolbin_in_session_allow_would_be_caught(self):
        # A future primitive (wsl/odbcconf/scriptrunner) added to the session allow-list is caught by the
        # forbidden-leaf tripwire the moment it appears as a %SYSTEM32%\<leaf> Everyone allow.
        for lolbin in ("wsl.exe", "odbcconf.exe", "scriptrunner.exe"):
            self.assertIn(lolbin, {f.lower() for f in A.FORBIDDEN_HOSTED_ALLOW})
            with self.assertRaises(A.AppLockerPolicyError):
                A.assert_allow_model_invariants(self._inject_exe_allow(A.EVERYONE_SID, rf"%SYSTEM32%\{lolbin}"))


class CapacityTests(SimpleTestCase):
    def test_no_collision_across_accounts(self):
        accounts = [2, 3, 10, 100]
        eff = A.compile_effective_policy(_base(), [(n, _sid(n)) for n in accounts])
        self.assertEqual(A.tenant_account_ids(eff), set(accounts))
        # every tenant rule id is globally unique (no cross-tenant collision) and there is no 5-user cap.
        all_ids = []
        for n in accounts:
            all_ids.extend(_exe_rule_ids(eff, n))
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertEqual(len(all_ids), len(accounts) * len(A.DENY_BINARIES))

    def test_rule_id_is_deterministic_and_account_tagged(self):
        self.assertEqual(A.tenant_rule_id(14, 16), A.tenant_rule_id(14, 16))
        self.assertNotEqual(A.tenant_rule_id(14, 16), A.tenant_rule_id(15, 16))
        self.assertTrue(A._is_tenant_rule(A.tenant_rule_id(14, 16), 14))
        self.assertFalse(A._is_tenant_rule(A.tenant_rule_id(14, 16), 15))
