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


# ── STREAM 10D — G5v2 (inverted / W^X) ACL contract tests (ADR-0043) ─────────────────────────────────────────
def _v2_good_readback(user_sid, plan):
    """A known-good W^X read-back: root {SYSTEM/Admins Full, user RX}; writable subdirs +user Modify; code dirs
    + common.ini +user Deny-write."""
    root = [
        {"sid": A.SYSTEM_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
        {"sid": A.ADMINISTRATORS_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
        {"sid": user_sid, "type": "Allow", "rights": "ReadAndExecute", "inherited": False},
    ]
    dacls = {A.V2_ROOT: list(root)}
    for rel in plan.writable_subdirs:
        dacls[rel] = root + [{"sid": user_sid, "type": "Allow", "rights": "Modify", "inherited": False}]
    for rel in plan.deny_write_paths:
        dacls[rel] = root + [{"sid": user_sid, "type": "Deny", "rights": "Write, Delete", "inherited": False}]
    return dacls


class G5v2WxAclTests(SimpleTestCase):
    SID = "S-1-5-21-9-9-9-1042"

    def setUp(self):
        self.plan = A.build_workspace_acl_plan_v2(r"C:\GuvFX\accounts\42", "guvfx_u_42")

    def test_plan_carries_inverted_contract(self):
        self.assertEqual(self.plan.user_root_right, "readexecute")
        self.assertEqual(self.plan.writable_subdirs, A.HOSTED_WRITABLE_SUBDIRS)
        self.assertIn(A.COMMON_INI_RELPATH, self.plan.deny_write_paths)
        for code in A.HOSTED_CODE_SUBDIRS:
            self.assertIn(code, self.plan.deny_write_paths)

    def test_plan_refuses_foreign_and_mismatched_targets(self):
        for root, user in ((r"C:\GuvFX\accounts\42", "guvfx_u_43"),      # id/tree mismatch
                           (r"C:\Windows", "guvfx_u_42"),                 # out of base
                           (r"C:\GuvFX\accounts\42\..\9", "guvfx_u_42"),  # traversal
                           (r"C:\GuvFX\accounts\42", "Administrator")):   # not a hosted identity
            with self.assertRaises(A.AclError):
                A.build_workspace_acl_plan_v2(root, user)

    def test_verify_accepts_known_good_wx_readback(self):
        self.assertTrue(A.verify_workspace_acl_v2(_v2_good_readback(self.SID, self.plan),
                                                  user_sid=self.SID, plan=self.plan).ok)

    def test_verify_rejects_tenant_writable_root(self):
        dacls = _v2_good_readback(self.SID, self.plan)
        dacls[A.V2_ROOT].append({"sid": self.SID, "type": "Allow", "rights": "Modify", "inherited": False})
        v = A.verify_workspace_acl_v2(dacls, user_sid=self.SID, plan=self.plan)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, A.V2_ROOT_NOT_RX_ONLY)

    def test_verify_rejects_missing_deny_write_on_common_ini(self):
        dacls = _v2_good_readback(self.SID, self.plan)
        dacls[A.COMMON_INI_RELPATH] = [r for r in dacls[A.COMMON_INI_RELPATH] if r.get("type") != "Deny"]
        v = A.verify_workspace_acl_v2(dacls, user_sid=self.SID, plan=self.plan)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, A.V2_CODE_NOT_DENIED)

    def test_verify_rejects_writable_subdir_without_modify(self):
        dacls = _v2_good_readback(self.SID, self.plan)
        rel = self.plan.writable_subdirs[0]
        dacls[rel] = [r for r in dacls[rel] if not (r.get("type") == "Allow" and r.get("sid") == self.SID)]
        v = A.verify_workspace_acl_v2(dacls, user_sid=self.SID, plan=self.plan)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, A.V2_WRITABLE_NOT_MODIFY)

    def test_verify_rejects_missing_path(self):
        dacls = _v2_good_readback(self.SID, self.plan)
        del dacls[self.plan.deny_write_paths[0]]
        self.assertEqual(A.verify_workspace_acl_v2(dacls, user_sid=self.SID, plan=self.plan).reason,
                         A.V2_MISSING_PATH)

    def test_verify_rejects_extra_principal_at_root(self):
        dacls = _v2_good_readback(self.SID, self.plan)
        dacls[A.V2_ROOT].append({"sid": A.BUILTIN_USERS_SID, "type": "Allow", "rights": "ReadAndExecute",
                                 "inherited": False})
        self.assertFalse(A.verify_workspace_acl_v2(dacls, user_sid=self.SID, plan=self.plan).ok)

    def test_self_control_passes_on_a_good_call_v2(self):
        # RULE 11 happy path: the verifier proves its detector live (pos + neg) before any verdict.
        self.assertTrue(A.verify_workspace_acl_v2(_v2_good_readback(self.SID, self.plan),
                                                  user_sid=self.SID, plan=self.plan).ok)

    def test_self_control_actually_fires_v2(self):
        # STREAM 10D review fix — the REAL proof (not a tautology): if _classify_v2 were broken to ACCEPT a
        # leaking DACL, the self-control's NEGATIVE probe must catch it so verify_workspace_acl_v2 RAISES. A test
        # that only asserts a good read-back verifies .ok would still pass if the _self_check_v2 call were DELETED;
        # this one fails unless the self-control genuinely runs on every call.
        from unittest.mock import patch
        import types
        with patch.object(A, "_classify_v2", lambda *a, **k: types.SimpleNamespace(ok=True, reason="")):
            with self.assertRaises(A.AclSelfCheckError):
                A.verify_workspace_acl_v2(_v2_good_readback(self.SID, self.plan), user_sid=self.SID, plan=self.plan)

    def test_self_control_actually_fires_v1(self):
        # Same real proof for the G5v1 verifier: a broken _classify (accepts a leak) must be caught by _self_check.
        from unittest.mock import patch
        import types
        with patch.object(A, "_classify", lambda *a, **k: types.SimpleNamespace(ok=True, reason="")):
            with self.assertRaises(A.AclSelfCheckError):
                A.verify_workspace_acl(_rows(self.SID), user_sid=self.SID)

    def test_verify_rejects_partial_deny_write_on_common_ini(self):
        # A Deny covering only Delete (or only Write) leaves an effective write path via the inherited tenant
        # Modify on config\ — the AllowDllImport ceiling stays flippable. It must NOT pass verification.
        for partial in ("Delete", "Write", "AppendData"):
            dacls = _v2_good_readback(self.SID, self.plan)
            dacls[A.COMMON_INI_RELPATH] = [r for r in dacls[A.COMMON_INI_RELPATH] if r.get("type") != "Deny"]
            dacls[A.COMMON_INI_RELPATH].append({"sid": self.SID, "type": "Deny", "rights": partial, "inherited": False})
            v = A.verify_workspace_acl_v2(dacls, user_sid=self.SID, plan=self.plan)
            self.assertFalse(v.ok, partial)
            self.assertEqual(v.reason, A.V2_CODE_NOT_DENIED, partial)

    def test_verify_rejects_foreign_allow_principal_on_code_dir(self):
        dacls = _v2_good_readback(self.SID, self.plan)
        rel = self.plan.deny_write_paths[0]
        dacls[rel].append({"sid": A.BUILTIN_USERS_SID, "type": "Allow", "rights": "Modify", "inherited": True})
        v = A.verify_workspace_acl_v2(dacls, user_sid=self.SID, plan=self.plan)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, A.V2_FOREIGN_PRINCIPAL)

    def test_v1_certified_contract_left_untouched(self):
        # G5v1 (the live certified path) must still build + verify exactly as before.
        plan1 = A.build_workspace_acl_plan(r"C:\GuvFX\accounts\42", "guvfx_u_42")
        self.assertEqual(plan1.user_min_right, "modify")
        rows = [
            {"sid": A.SYSTEM_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
            {"sid": A.ADMINISTRATORS_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
            {"sid": self.SID, "type": "Allow", "rights": "Modify", "inherited": False},
        ]
        self.assertTrue(A.verify_workspace_acl(rows, user_sid=self.SID).ok)
