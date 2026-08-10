"""Beta Readiness Stream 4 — G5: the reusable per-user NTFS ACL engine (pure brain).

Proves the plan builder refuses any non-hosted / out-of-base / traversal target, and that the SID-typed
read-back verifier is EXACT (fails closed on any extra Allow principal — the cross-tenant leak), enforces the
inheritance break, checks minimum rights, and runs its positive/negative self-control every call (RULE 11).
No host, no DB — pure functions.
"""
from django.test import SimpleTestCase

from hosted_workspace import workspace_acl as A


def _rows(user_sid, *, extra=None, protected=True, system_rights="FullControl",
          admin_rights="FullControl", user_rights="Modify", inherited=False):
    rows = [
        {"sid": A.SYSTEM_SID, "type": "Allow", "rights": system_rights, "inherited": False},
        {"sid": A.ADMINISTRATORS_SID, "type": "Allow", "rights": admin_rights, "inherited": False},
        {"sid": user_sid, "type": "Allow", "rights": user_rights, "inherited": inherited},
    ]
    if extra:
        rows.extend(extra)
    return rows


USER_SID = "S-1-5-21-11-22-33-1014"


class BuildPlanTests(SimpleTestCase):
    def test_valid_plan(self):
        p = A.build_workspace_acl_plan(r"C:\GuvFX\accounts\14", "guvfx_u_14")
        self.assertTrue(p.break_inheritance)
        self.assertEqual(p.windows_username, "guvfx_u_14")
        self.assertEqual(p.user_min_right, "modify")
        # The two fixed principals are SID-typed in the contract.
        sids = {sid for sid, _ in p.required}
        self.assertEqual(sids, {A.SYSTEM_SID, A.ADMINISTRATORS_SID})

    def test_forward_slashes_normalised(self):
        p = A.build_workspace_acl_plan("C:/GuvFX/accounts/9", "guvfx_u_9")
        self.assertEqual(p.runtime_root, r"C:\GuvFX\accounts\9")

    def test_refuses_non_hosted_identity(self):
        for bad in ["Administrator", "guvfx_b_slot1", "guvfx_u_", "guvfx_u_0", "", "  ", "svc_x"]:
            with self.assertRaises(A.AclError):
                A.build_workspace_acl_plan(r"C:\GuvFX\accounts\14", bad)

    def test_refuses_out_of_base_and_traversal(self):
        for bad_root in [r"C:\GuvFX\beta\slots\1", r"C:\Windows\System32", r"C:\GuvFX\accounts",
                         r"C:\GuvFX\accounts\14\..\..\Windows", ""]:
            with self.assertRaises(A.AclError):
                A.build_workspace_acl_plan(bad_root, "guvfx_u_14")

    def test_refuses_identity_tree_mismatch(self):
        # guvfx_u_5 must never be granted rights on account 9's tree (identity bound to its own tree).
        with self.assertRaises(A.AclError):
            A.build_workspace_acl_plan(r"C:\GuvFX\accounts\9", "guvfx_u_5")
        # A trailing backslash on the correct tree is tolerated.
        p = A.build_workspace_acl_plan("C:\\GuvFX\\accounts\\5\\", "guvfx_u_5")
        self.assertEqual(p.runtime_root, r"C:\GuvFX\accounts\5")


class VerifyTests(SimpleTestCase):
    def test_exact_three_principals_ok(self):
        v = A.verify_workspace_acl(_rows(USER_SID), user_sid=USER_SID, protected=True)
        self.assertTrue(v.ok, v.reason)
        self.assertEqual(v.reason, A.V_OK)

    def test_extra_allow_principal_is_the_leak_and_fails(self):
        extra = [{"sid": A.BUILTIN_USERS_SID, "type": "Allow", "rights": "ReadAndExecute", "inherited": False}]
        v = A.verify_workspace_acl(_rows(USER_SID, extra=extra), user_sid=USER_SID, protected=True)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, A.V_UNEXPECTED)
        self.assertIn(A.BUILTIN_USERS_SID, v.offenders)

    def test_missing_user_principal_fails(self):
        rows = [
            {"sid": A.SYSTEM_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
            {"sid": A.ADMINISTRATORS_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
        ]
        v = A.verify_workspace_acl(rows, user_sid=USER_SID, protected=True)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, A.V_MISSING)

    def test_inheritance_not_broken_fails(self):
        v = A.verify_workspace_acl(_rows(USER_SID), user_sid=USER_SID, protected=False)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, A.V_INHERITANCE)

    def test_inherited_allow_ace_fails_even_if_protected_claimed(self):
        v = A.verify_workspace_acl(_rows(USER_SID, inherited=True), user_sid=USER_SID, protected=True)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, A.V_INHERITED_ACE)

    def test_insufficient_user_rights_fails(self):
        v = A.verify_workspace_acl(_rows(USER_SID, user_rights="ReadAndExecute"),
                                   user_sid=USER_SID, protected=True)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, A.V_RIGHTS)

    def test_system_must_be_full_not_modify(self):
        v = A.verify_workspace_acl(_rows(USER_SID, system_rights="Modify"),
                                   user_sid=USER_SID, protected=True)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, A.V_RIGHTS)

    def test_user_fullcontrol_satisfies_modify(self):
        v = A.verify_workspace_acl(_rows(USER_SID, user_rights="FullControl"),
                                   user_sid=USER_SID, protected=True)
        self.assertTrue(v.ok, v.reason)

    def test_composite_modify_label_satisfies(self):
        v = A.verify_workspace_acl(_rows(USER_SID, user_rights="Modify, Synchronize"),
                                   user_sid=USER_SID, protected=True)
        self.assertTrue(v.ok, v.reason)

    def test_deny_ace_does_not_break_a_clean_allow_set(self):
        extra = [{"sid": A.BUILTIN_USERS_SID, "type": "Deny", "rights": "FullControl", "inherited": False}]
        v = A.verify_workspace_acl(_rows(USER_SID, extra=extra), user_sid=USER_SID, protected=True)
        self.assertTrue(v.ok, v.reason)   # Deny cannot widen access → not a leak

    def test_self_control_runs_and_passes_on_normal_call(self):
        # verify_workspace_acl runs the positive+negative control internally; a normal call must not raise.
        try:
            A.verify_workspace_acl(_rows(USER_SID), user_sid=USER_SID, protected=True)
        except A.AclSelfCheckError:  # pragma: no cover
            self.fail("self-control raised on a healthy classifier")
