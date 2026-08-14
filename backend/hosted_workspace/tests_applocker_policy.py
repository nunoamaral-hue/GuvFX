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


class WxIsolationTests(SimpleTestCase):
    """STREAM 10D — the W^X (write-xor-execute) native-code-elimination guards (ADR-0043). TENANT-WRITABLE =>
    NON-EXECUTABLE; TENANT-EXECUTABLE => NON-WRITABLE."""

    SID = "S-1-5-21-11-22-33-1099"

    def test_metaeditor_denied_via_exe_binaryname_pin(self):
        exe = [c for c in ET.fromstring(A.generate_base_policy()).findall("RuleCollection")
               if c.get("Type") == "Exe"][0]
        mq = [r for r in exe if r.tag == "FilePublisherRule"]
        self.assertTrue(mq)
        for r in mq:
            for c in r.findall("Conditions/FilePublisherCondition"):
                self.assertNotEqual(c.get("BinaryName"), "*", "Exe MetaQuotes rule must be BinaryName-pinned")
                self.assertIn(c.get("BinaryName").upper(), {b.upper() for b in A.HOSTED_METAQUOTES_EXE_BINARIES})

    def test_unpinned_metaquotes_exe_publisher_is_caught(self):
        root = ET.fromstring(A.generate_base_policy())
        exe = [c for c in root.findall("RuleCollection") if c.get("Type") == "Exe"][0]
        bad = ET.SubElement(exe, "FilePublisherRule", {"Id": "dead0000-0000-a11e-0000-00000000097f",
                            "Name": "unpinned", "Action": "Allow", "UserOrGroupSid": A.EVERYONE_SID})
        pc = ET.SubElement(ET.SubElement(bad, "Conditions"), "FilePublisherCondition",
                           {"PublisherName": A.METAQUOTES_PUBLISHER_NAME, "ProductName": "*", "BinaryName": "*"})
        ET.SubElement(pc, "BinaryVersionRange", {"LowSection": "*", "HighSection": "*"})
        with self.assertRaisesRegex(A.AppLockerPolicyError, "metaquotes_exe_not_binaryname_pinned"):
            A.assert_allow_model_invariants(ET.tostring(root, encoding="unicode"))

    def test_wx_fragment_is_single_exe_deny_all_with_exec_allowlist_exceptions(self):
        frag = A.tenant_wx_deny_fragment(7, self.SID)
        self.assertTrue(A.assert_wx_deny_invariants(frag, 7, self.SID))
        root = ET.fromstring(frag)
        colls = root.findall("RuleCollection")
        self.assertEqual([c.get("Type") for c in colls], ["Exe"])   # Exe only (Dll/Script closed by the base)
        rule = list(colls[0])[0]
        self.assertEqual(rule.get("Action"), "Deny")
        self.assertEqual([c.get("Path") for c in rule.findall("Conditions/FilePathCondition")], ["*"])
        exc = {c.get("Path").upper() for c in rule.findall("Exceptions/FilePathCondition")}
        self.assertEqual(exc, {p.upper() for p in A.hosted_tenant_exec_allowlist(7)})

    def test_wx_exec_allowlist_is_terminal64_plus_session_binaries(self):
        al = [p.upper() for p in A.hosted_tenant_exec_allowlist(7)]
        self.assertIn(r"%OSDRIVE%\GUVFX\ACCOUNTS\7\TERMINAL\TERMINAL64.EXE", al)
        for b in A.HOSTED_SESSION_ALLOW:
            self.assertIn(f"%SYSTEM32%\\{b.upper()}", al)
        self.assertNotIn(r"%OSDRIVE%\GUVFX\ACCOUNTS\7\TERMINAL\METAEDITOR64.EXE", al)   # MetaEditor not runnable

    def test_wx_extra_exception_is_caught(self):
        root = ET.fromstring(A.tenant_wx_deny_fragment(7, self.SID))
        rule = list([c for c in root.findall("RuleCollection")][0])[0]
        ET.SubElement(rule.find("Exceptions"), "FilePathCondition", {"Path": r"%OSDRIVE%\Users\Public\*"})
        with self.assertRaisesRegex(A.AppLockerPolicyError, "wx_exceptions_mismatch"):
            A.assert_wx_deny_invariants(ET.tostring(root, encoding="unicode"), 7, self.SID)

    def test_wx_missing_exception_is_caught(self):
        root = ET.fromstring(A.tenant_wx_deny_fragment(7, self.SID))
        exc = list([c for c in root.findall("RuleCollection")][0])[0].find("Exceptions")
        exc.remove(list(exc)[0])
        with self.assertRaisesRegex(A.AppLockerPolicyError, "wx_exceptions_mismatch"):
            A.assert_wx_deny_invariants(ET.tostring(root, encoding="unicode"), 7, self.SID)

    def _exec_denied(self, account, sid, target):
        # Model AppLocker Deny(*)-with-exceptions: a path is DENIED unless it matches an exception (location-agnostic).
        import fnmatch
        rule = list([c for c in ET.fromstring(A.tenant_wx_deny_fragment(account, sid)).findall("RuleCollection")][0])[0]
        exc = [c.get("Path").upper() for c in rule.findall("Exceptions/FilePathCondition")]
        return not any(fnmatch.fnmatch(target.upper(), e) for e in exc)

    def test_copied_terminal64_denied_from_any_location_legit_allowed(self):
        rx = r"%OSDRIVE%\GUVFX\ACCOUNTS\7\TERMINAL\TERMINAL64.EXE"
        for loc in (r"%OSDRIVE%\USERS\PUBLIC\DOCUMENTS\TERMINAL64.EXE",   # the review's Public gap
                    r"%OSDRIVE%\PROGRAMDATA\Z\TERMINAL64.EXE",            # ProgramData gap
                    r"%OSDRIVE%\USERS\GUVFX_U_7.HOST\TERMINAL64.EXE",     # suffixed RDS profile
                    r"D:\ANYWHERE\TERMINAL64.EXE",                        # another drive
                    r"%OSDRIVE%\GUVFX\ACCOUNTS\7\TERMINAL\CONFIG\TERMINAL64.EXE"):  # a writable subdir
            self.assertTrue(self._exec_denied(7, self.SID, loc), loc)
        self.assertFalse(self._exec_denied(7, self.SID, rx))              # legit RX terminal64 runs
        self.assertFalse(self._exec_denied(7, self.SID, r"%SYSTEM32%\RDPSHELL.EXE"))   # session binary runs

    def test_wx_no_writable_executable_intersection(self):
        self.assertTrue(A.assert_wx_no_writable_executable_intersection(7))

    def test_wx_per_tenant_sid_and_account_isolation(self):
        f7 = A.tenant_wx_deny_fragment(7, "S-1-5-21-11-22-33-1007")
        f9 = A.tenant_wx_deny_fragment(9, "S-1-5-21-11-22-33-1009")
        self.assertEqual(A.tenant_account_ids(f7), {7})
        self.assertEqual(A.tenant_account_ids(f9), {9})
        ids7 = {r.get("Id") for c in ET.fromstring(f7).findall("RuleCollection") for r in c}
        ids9 = {r.get("Id") for c in ET.fromstring(f9).findall("RuleCollection") for r in c}
        self.assertFalse(ids7 & ids9)                    # no rule-id collision across accounts
        self.assertIn(r"ACCOUNTS\7\TERMINAL", " ".join(A.hosted_tenant_exec_allowlist(7)).upper())  # 7's own path

    def test_wx_deny_refuses_shared_principal(self):
        for shared in ("S-1-1-0", "S-1-5-32-545", "S-1-5-32-544", "S-1-5-18"):
            with self.assertRaises(A.AppLockerPolicyError):
                A.tenant_wx_deny_fragment(7, shared)

    def test_wx_single_list_couples_ntfs_and_applocker(self):
        # Decision 2: ONE canonical constant drives BOTH NTFS (workspace_acl) and AppLocker. Same object identity.
        from hosted_workspace import workspace_acl as W
        self.assertIs(W.HOSTED_WRITABLE_SUBDIRS, A.HOSTED_WRITABLE_SUBDIRS)
        self.assertIs(W.HOSTED_CODE_SUBDIRS, A.HOSTED_CODE_SUBDIRS)

    def test_wx_fragment_grants_nothing(self):
        root = ET.fromstring(A.tenant_wx_deny_fragment(7, self.SID))
        self.assertFalse([r for c in root.findall("RuleCollection") for r in c if r.get("Action") == "Allow"])

    def test_golden_mql_gate_default_matches_canonical_code_subdirs(self):
        # Test-GuvfxGoldenMql.ps1's default -CodeSubdirs must equal HOSTED_CODE_SUBDIRS (the golden #import scanner
        # must cover EVERY code dir the ACL locks — esp. MQL5\Include where .mqh headers live). Drift fails CI.
        import json as _json
        import re as _re
        ps = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "terminal_provisioning", "windows", "Test-GuvfxGoldenMql.ps1")
        txt = open(ps).read()
        m = _re.search(r"\$CodeSubdirs\s*=\s*'(\[.*?\])'", txt)
        self.assertIsNotNone(m, "could not find the -CodeSubdirs default in Test-GuvfxGoldenMql.ps1")
        # The captured group is valid JSON already (\\ escapes one backslash); json.loads collapses it.
        default = {p.lower() for p in _json.loads(m.group(1))}
        self.assertEqual(default, {c.lower() for c in A.HOSTED_CODE_SUBDIRS})

    # ── STREAM 10D review fix — the W^X Deny(*) must be COMPOSED into an effective policy, not inert ──────────
    def _effective_deny_exceptions(self, eff_xml, account):
        """The exception paths of the per-tenant W^X Deny(*) as it sits in a COMPOSED effective policy (proves the
        composer actually placed the load-bearing rule — not just that the standalone fragment is well-formed)."""
        root = ET.fromstring(eff_xml)
        denies = [r for c in root.findall("RuleCollection") if c.get("Type") == "Exe"
                  for r in c if (r.get("Action") or "").lower() == "deny" and A._is_tenant_rule(r.get("Id", ""), account)]
        self.assertEqual(len(denies), 1, "composed effective policy must carry exactly one W^X Deny for the tenant")
        self.assertEqual([c.get("Path") for c in denies[0].findall("Conditions/FilePathCondition")], ["*"])
        return {c.get("Path").upper() for c in denies[0].findall("Exceptions/FilePathCondition")}

    def test_wx_effective_policy_composes_and_denies_copied_terminal64(self):
        # The V3/V5 closure is only real if the W^X Deny(*) enters an EFFECTIVE policy. Prove the composer does it:
        # a copied SIGNED terminal64 (which would match the base MetaQuotes publisher Allow) is denied everywhere.
        import fnmatch
        base = A.generate_base_policy("Enabled")
        eff = A.compile_effective_wx_policy(base, [(7, self.SID)])
        exc = self._effective_deny_exceptions(eff, 7)
        denied = lambda t: not any(fnmatch.fnmatch(t.upper(), e) for e in exc)
        # the base MetaQuotes Exe publisher Allow is still present (so ABSENT the Deny, the copy WOULD run)
        base_pubs = [r for c in ET.fromstring(eff).findall("RuleCollection") if c.get("Type") == "Exe"
                     for r in c if r.tag == "FilePublisherRule" and (r.get("Action") or "") == "Allow"]
        self.assertTrue(base_pubs, "base MetaQuotes publisher Allow must remain — proves the Deny is load-bearing")
        for loc in (r"%OSDRIVE%\USERS\PUBLIC\TERMINAL64.EXE", r"%OSDRIVE%\PROGRAMDATA\Z\TERMINAL64.EXE",
                    r"D:\X\TERMINAL64.EXE", r"%OSDRIVE%\GUVFX\ACCOUNTS\7\TERMINAL\CONFIG\TERMINAL64.EXE"):
            self.assertTrue(denied(loc), f"copied terminal64 must be denied from {loc}")
        self.assertFalse(denied(r"%OSDRIVE%\GUVFX\ACCOUNTS\7\TERMINAL\TERMINAL64.EXE"))   # legit RX runs
        # composition must not break the base allow model, and the W^X Deny supersedes the legacy shell-deny:
        self.assertTrue(A.assert_allow_model_invariants(eff))
        self.assertTrue(denied(r"%SYSTEM32%\CMD.EXE"), "W^X Deny(*) must deny cmd.exe (supersedes legacy shell-deny)")

    def test_wx_effective_composer_is_deterministic_dedup_idempotent(self):
        base = A.generate_base_policy("Enabled")
        a = A.compile_effective_wx_policy(base, [(7, self.SID), (7, self.SID), (3, "S-1-5-21-11-22-33-1003")])
        b = A.compile_effective_wx_policy(base, [(3, "S-1-5-21-11-22-33-1003"), (7, self.SID)])
        self.assertEqual(A._canonical(a), A._canonical(b))                       # order-independent + dedup
        self.assertEqual(A.tenant_account_ids(a), {3, 7})
        # merge_tenant_wx is idempotent and strips only the target account
        m = A.merge_tenant_wx(a, 7, self.SID)
        self.assertEqual(A._canonical(m), A._canonical(a))
        removed, n = A.remove_tenant(m, 7)
        self.assertEqual(A.tenant_account_ids(removed), {3})

    def test_wx_deny_rules_single_definition_shared_by_fragment_and_composer(self):
        # tenant_wx_deny_rules is the ONE definition; the standalone fragment and the composed policy carry the
        # byte-identical rule (they cannot drift).
        rule = A.tenant_wx_deny_rules(7, self.SID)[0]
        frag_rule = list([c for c in ET.fromstring(A.tenant_wx_deny_fragment(7, self.SID)).findall("RuleCollection")][0])[0]
        self.assertEqual(ET.tostring(rule, encoding="unicode"), ET.tostring(frag_rule, encoding="unicode"))

    def test_wx_subdir_lists_are_disjoint(self):
        # ADR-0043 MINIMALITY, previously prose-only: no writable subdir may nest a code dir (or vice-versa).
        self.assertTrue(A.assert_wx_subdir_lists_disjoint())

    def test_wx_subdir_overlap_would_be_caught(self):
        real = A.HOSTED_WRITABLE_SUBDIRS
        try:
            A.HOSTED_WRITABLE_SUBDIRS = real + (r"terminal\MQL5\Experts\sub",)   # nested under a code dir
            with self.assertRaisesRegex(A.AppLockerPolicyError, "wx_writable_code_subdir_overlap"):
                A.assert_wx_subdir_lists_disjoint()
        finally:
            A.HOSTED_WRITABLE_SUBDIRS = real

    def test_golden_mql_gate_is_fail_closed(self):
        # STREAM 10D review (rated HIGH): the golden gate must NOT emit a false "vetted_empty". Statically assert
        # the .ps1 carries the RULE-11 fail-closed guards (CI has no PowerShell, so guard the source shape so a
        # future edit that removes any of them trips CI). The runtime positive control itself proves the parser.
        ps = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "terminal_provisioning", "windows", "Test-GuvfxGoldenMql.ps1")
        txt = open(ps).read()
        for token in ("positive_control_failed",           # RULE-11 runtime positive control aborts on failure
                      "expected_code_dir_absent",          # coverage guard: a missing code dir is an offender
                      "code_dir_unscannable",               # captured enumeration errors -> offender (no fail-open)
                      "-ErrorVariable ev",                  # enumeration errors are captured, not swallowed
                      "code_dir_is_reparse_point",          # reparse-point code dir rejected
                      "poscontrol.mq5", 'poscontrol.ex5'):  # the seeded known-bad positive control inputs
            self.assertIn(token, txt, f"golden gate missing fail-closed guard: {token}")

    # ── STREAM 10E — per-tenant Dll W^X Deny (reducible half of the signed-DLL residual) ─────────────────────
    # A representative host-soak-derived NON-writable RX set. NOTE it must NOT contain `...\TERMINAL\*` — that
    # wildcard would cover the tenant-WRITABLE `terminal\MQL5\Files` and re-open the hole (the guard rejects it);
    # MT5's own DLLs are excepted via the specific non-writable RX code dirs (Libraries, Include).
    NW = (r"%SYSTEM32%\*", r"%WINDIR%\WinSxS\*", r"%PROGRAMFILES%\*",
          r"%OSDRIVE%\GUVFX\ACCOUNTS\7\TERMINAL\MQL5\Libraries\*")   # a NON-writable (RX) code dir where MT5 libs load

    def test_wx_dll_deny_is_single_dll_deny_all_with_soak_exceptions(self):
        frag = A.tenant_wx_dll_deny_fragment(7, self.SID, self.NW)
        self.assertTrue(A.assert_wx_dll_deny_invariants(frag, 7, self.SID, self.NW))
        root = ET.fromstring(frag)
        colls = root.findall("RuleCollection")
        self.assertEqual([c.get("Type") for c in colls], ["Dll"])     # Dll only
        rule = list(colls[0])[0]
        self.assertEqual(rule.get("Action"), "Deny")
        self.assertEqual([c.get("Path") for c in rule.findall("Conditions/FilePathCondition")], ["*"])
        exc = {c.get("Path").upper() for c in rule.findall("Exceptions/FilePathCondition")}
        self.assertEqual(exc, {p.upper() for p in self.NW})

    def test_wx_dll_deny_denies_signed_dll_from_writable_allows_os_dll(self):
        # The reducible-half closure: a planted DLL in a tenant-writable location is NOT an exception -> denied
        # (regardless of signature); an OS DLL under %SYSTEM32% IS excepted -> loads.
        import fnmatch
        rule = list(ET.fromstring(A.tenant_wx_dll_deny_fragment(7, self.SID, self.NW)).findall("RuleCollection")[0])[0]
        exc = [c.get("Path").upper() for c in rule.findall("Exceptions/FilePathCondition")]
        denied = lambda t: not any(fnmatch.fnmatch(t.upper(), e) for e in exc)
        for planted in (r"%OSDRIVE%\GUVFX\ACCOUNTS\7\TERMINAL\MQL5\FILES\EVIL.DLL",   # tenant-writable data dir
                        r"%OSDRIVE%\USERS\PUBLIC\EVIL.DLL", r"%TEMP%\EVIL.DLL"):
            self.assertTrue(denied(planted), f"signed DLL planted at {planted} must be denied")
        self.assertFalse(denied(r"%SYSTEM32%\KERNEL32.DLL"))          # legit OS DLL still loads
        self.assertFalse(denied(r"%OSDRIVE%\GUVFX\ACCOUNTS\7\TERMINAL\MQL5\LIBRARIES\LIB.DLL"))  # MT5's RX-dir lib loads

    def test_wx_dll_deny_empty_exceptions_refused(self):
        with self.assertRaisesRegex(A.AppLockerPolicyError, "wx_dll_deny_requires_exceptions"):
            A.tenant_wx_dll_deny_fragment(7, self.SID, [])            # would deny EVERY DLL incl. the OS

    def test_wx_dll_deny_wildcard_exception_refused(self):
        with self.assertRaisesRegex(A.AppLockerPolicyError, "wx_dll_exception_is_wildcard"):
            A.tenant_wx_dll_deny_rules(7, self.SID, ["*"])            # would allow everything (fail-open)

    def test_wx_dll_deny_exception_under_writable_tree_refused(self):
        with self.assertRaisesRegex(A.AppLockerPolicyError, "wx_dll_exception_covers_writable"):
            A.tenant_wx_dll_deny_rules(7, self.SID, [r"%OSDRIVE%\GUVFX\ACCOUNTS\7\TERMINAL\MQL5\Files"])

    def test_wx_dll_deny_exception_covering_writable_subdir_refused(self):
        # A wildcard exception whose coverage CONTAINS a tenant-writable subdir (terminal\* covers terminal\MQL5\Files)
        # must be rejected — the exact %WINDIR%-writable-subdir trap that keeps the base Dll rule publisher-only.
        with self.assertRaisesRegex(A.AppLockerPolicyError, "wx_dll_exception_covers_writable"):
            A.tenant_wx_dll_deny_rules(7, self.SID, [r"%OSDRIVE%\GUVFX\ACCOUNTS\7\TERMINAL\*"])

    def test_wx_dll_deny_rejects_LITERAL_drive_covers_writable(self):
        # STREAM 10E review HIGH (RULE 11): the soak enumerates LITERAL C:\ paths; a literal-drive exception that
        # covers a tenant-writable dir must be rejected EXACTLY like its %OSDRIVE% form (drive-canonical guard).
        for bad in (r"C:\GuvFX\accounts\7\terminal\*",              # covers terminal\MQL5\Files (writable)
                    r"C:\GuvFX\accounts\7\terminal\config",         # IS the writable common.ini dir
                    r"C:\*", r"%SYSTEMDRIVE%\*",                     # whole OS drive
                    r"%SYSTEMDRIVE%\GuvFX\accounts\7\terminal\*",    # %SYSTEMDRIVE% variant
                    r"c:/guvfx/accounts/7/terminal/*"):             # forward-slash + case variant
            with self.assertRaisesRegex(A.AppLockerPolicyError, "wx_dll_exception_covers_writable", msg=bad):
                A.tenant_wx_dll_deny_rules(7, self.SID, [bad])

    def test_wx_dll_deny_accepts_legit_literal_drive_nonwritable(self):
        # A genuinely non-writable literal-drive exception (OS + an RX code dir) must still be accepted.
        good = [r"C:\Windows\System32\*", r"C:\GuvFX\accounts\7\terminal\MQL5\Libraries\*", r"%SYSTEM32%\*"]
        self.assertTrue(A.assert_wx_dll_deny_invariants(A.tenant_wx_dll_deny_fragment(7, self.SID, good), 7, self.SID, good))

    def test_wx_dll_assert_independently_rejects_covers_writable(self):
        # assert must RE-DERIVE the closure (not just exceptions==expected): a fragment carrying a covers-writable
        # exception fails assert even when the passed expected set contains the same (bad) exception.
        frag = ET.fromstring(A.tenant_wx_dll_deny_fragment(7, self.SID, self.NW))
        rule = list(frag.findall("RuleCollection")[0])[0]
        ET.SubElement(rule.find("Exceptions"), "FilePathCondition", {"Path": r"C:\GuvFX\accounts\7\terminal\*"})
        with self.assertRaisesRegex(A.AppLockerPolicyError, "wx_dll_exception_covers_writable"):
            A.assert_wx_dll_deny_invariants(ET.tostring(frag, encoding="unicode"), 7, self.SID,
                                            tuple(self.NW) + (r"C:\GuvFX\accounts\7\terminal\*",))

    def test_wx_dll_deny_refuses_shared_principal(self):
        for shared in ("S-1-1-0", "S-1-5-32-544", "S-1-5-18"):
            with self.assertRaises(A.AppLockerPolicyError):
                A.tenant_wx_dll_deny_fragment(7, shared, self.NW)

    def test_wx_dll_deny_exception_mismatch_caught(self):
        frag = A.tenant_wx_dll_deny_fragment(7, self.SID, self.NW)
        with self.assertRaisesRegex(A.AppLockerPolicyError, "wx_dll_exceptions_mismatch"):
            A.assert_wx_dll_deny_invariants(frag, 7, self.SID, tuple(self.NW) + (r"%TEMP%\*",))

    def test_wx_dll_deny_composer_reuses_single_definition(self):
        # tenant_wx_dll_deny_rules is the ONE definition; the fragment carries the byte-identical rule.
        rule = A.tenant_wx_dll_deny_rules(7, self.SID, self.NW)[0]
        frag_rule = list(ET.fromstring(A.tenant_wx_dll_deny_fragment(7, self.SID, self.NW)).findall("RuleCollection")[0])[0]
        self.assertEqual(ET.tostring(rule, encoding="unicode"), ET.tostring(frag_rule, encoding="unicode"))


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


class Stream10eEscapeBatteryTests(SimpleTestCase):
    """STREAM 10E — static guards over the host-certification package (CI has no PowerShell, so pin the scripts'
    ASCII cleanliness + the required escape cases + fail-closed controls, and the runbook references). The real
    behavioural proof runs on the disposable cert host per STREAM_10E_HOST_CERTIFICATION_RUNBOOK.md."""

    WIN = os.path.join(os.path.dirname(os.path.dirname(__file__)), "terminal_provisioning", "windows")
    DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                        "docs", "operations", "hosted-workspace")

    def _read_ascii(self, *parts):
        # PowerShell artefacts MUST be ASCII (RULE 9) — opening as ASCII fails loudly on any non-ASCII byte.
        p = os.path.join(*parts)
        self.assertTrue(os.path.exists(p), f"missing artefact: {p}")
        return open(p, encoding="ascii").read()

    def _read(self, *parts):
        # Docs (Markdown) may contain UTF-8 (em-dashes, arrows, box-drawing); read as UTF-8.
        p = os.path.join(*parts)
        self.assertTrue(os.path.exists(p), f"missing artefact: {p}")
        return open(p, encoding="utf-8").read()

    def test_escape_battery_scripts_exist_and_are_ascii(self):
        for f in ("Invoke-GuvfxEscapeBattery.ps1", "Get-GuvfxCertEvidence.ps1", "Get-GuvfxIsolationFingerprint.ps1"):
            txt = self._read_ascii(self.WIN, "escape_battery", f)   # RULE 9: fails if any non-ASCII byte present
            self.assertTrue(txt.strip())

    def test_escape_battery_covers_every_required_case(self):
        txt = self._read(self.WIN, "escape_battery", "Invoke-GuvfxEscapeBattery.ps1")
        for case in ("portable_copy_v5", "metaeditor", "writable_exe", "writable_script", "unsigned_dll_sideload",
                     "signed_dll_comhijack_from_writable", "common_ini_mutation", "import_native_exec",
                     "mt5_normal_positive_control"):
            self.assertIn(case, txt, f"escape battery missing required case: {case}")
        # it must NOT perform a broker login (credential boundary) and must clean up planted artefacts
        self.assertIn("operator_required", txt)
        self.assertIn("finally", txt)

    def test_evidence_collector_is_fail_closed_rule11(self):
        txt = self._read(self.WIN, "escape_battery", "Get-GuvfxCertEvidence.ps1")
        for token in ("measurement_proven",            # RULE-11: channel must show a real allow before a negative is trusted
                      "MEASUREMENT_UNPROVEN",           # fail-closed overall when the channel is dead
                      "8004", "8007",                   # authoritative block events
                      "FAIL_ESCAPED", "INCONCLUSIVE",   # an allow on an escape artefact / no decisive event both fail
                      "allowdllimport",                 # ceiling checked
                      "NO_BATTERY",                     # absent/empty attempts file = hard fail (not "0 escapes -> PASS")
                      "REQUIRED_CASES", "INCOMPLETE_BATTERY", "missingRequired",   # full-roster completeness gate
                      "PLANT_FAILED", "undecidedRequired",       # plant failure / not-decisively-blocked = hard fail
                      "ToUpperInvariant"):              # exact full-path correlation (not a leaf substring)
            self.assertIn(token, txt, f"evidence collector missing fail-closed control: {token}")
        # the loose leaf-substring correlation must be GONE (it cross-attributed the golden terminal64 allow)
        self.assertNotIn('-like "*$leaf*"', txt)

    def test_escape_runner_uses_robust_com_trigger_and_no_orphan(self):
        txt = self._read(self.WIN, "escape_battery", "Invoke-GuvfxEscapeBattery.ps1")
        self.assertIn("GetTypeFromCLSID", txt)                    # deterministic CoCreateInstance load trigger
        self.assertNotIn("BindToMoniker", txt)                    # the fragile class-moniker form is gone
        # the HKCU CLSID key is registered for cleanup BEFORE it is created (no orphan if New-ItemProperty throws)
        i_plant = txt.find('$planted += "REGKEY::HKCU:\\Software\\Classes\\CLSID')
        i_newitem = txt.find("New-Item -Path $key -Force")
        self.assertTrue(0 < i_plant < i_newitem, "reg key must be registered for cleanup before creation")

    def test_fingerprint_hashes_isolation_state_for_before_after(self):
        txt = self._read(self.WIN, "escape_battery", "Get-GuvfxIsolationFingerprint.ps1")
        for token in ("effective_policy_sha256", "runtime_root_dacl_sha256", "allowdllimport",
                      "terminal64_sha256", "fingerprint_sha256"):
            self.assertIn(token, txt)

    def test_runbook_exists_and_references_the_package(self):
        rb = self._read(self.DOCS, "STREAM_10E_HOST_CERTIFICATION_RUNBOOK.md")
        for ref in ("Invoke-GuvfxEscapeBattery.ps1", "Get-GuvfxCertEvidence.ps1", "Get-GuvfxIsolationFingerprint.ps1",
                    "Set-GuvfxAppLockerTenant.ps1", "Set-GuvfxWorkspaceAclV2.ps1", "Test-GuvfxGoldenMql.ps1",
                    "tenant_wx_dll_deny_fragment",                       # the reducible-half closure mechanism
                    "disposable", "Customer Zero", "before/after", "ParseFile", "REMOTEAPP_ISOLATION_CERTIFIED",
                    "signed_dll_comhijack_from_writable"):               # the hard precondition case
            self.assertIn(ref, rb, f"runbook missing reference: {ref}")
        # the runbook must forbid running the battery against the production host and must forbid weakening it
        self.assertIn("never", rb.lower())
        self.assertIn("disposable", rb.lower())
