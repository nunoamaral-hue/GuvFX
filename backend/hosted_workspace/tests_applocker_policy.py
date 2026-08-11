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
