"""Beta Readiness Stream 4 — the host provisioning engine orchestrator (prepare_hosted_slot).

The DARK host-executor seam lets us exercise EVERY host step against an in-memory fake — duplicate/idempotent,
retry, partial failure at each stage, ACL read-back mismatch + rollback, executor-incomplete, host errors,
observer-deferral, the fail-closed dark default, the Customer-Zero refusal, and the allocate-gate integration
(state is advanced ONLY on a prepared slot) — with zero host contact. Nothing here arms execution or logs a
secret.
"""
import os
from unittest import mock

from django.test import TestCase, override_settings

from execution.readiness import PERSISTENT_WORKSPACE
from trading.crypto import decrypt_password
from trading.models import TradingAccount

from hosted_workspace import slot_preparation as SP
from hosted_workspace import workspace_acl as A
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from hosted_workspace.tests_provisioning import _FLAGS_ON, _node, _user

_PREP_ON = dict(_FLAGS_ON, HOSTED_SLOT_PREP_ENABLED="1")
_DEF_USER_SID = "S-1-5-21-9-8-7-1500"

_HOST_STEPS = ["materialise_identity", "populate_runtime", "grant_rdp",
               "enforce_single_session", "verify_remoteapp", "applocker_prepare"]


class FakeExecutor:
    """In-memory host-executor. Records calls; each step returns ok unless named in ``fail``; ``raise_at``
    raises; ``drop`` removes a method (→ executor-incomplete); ACL returns a configurable read-back."""

    def __init__(self, *, fail=(), raise_at=None, drop=(), acl_rows=None,
                 acl_user_sid=_DEF_USER_SID, acl_protected=True):
        self.fail = set(fail)
        self.raise_at = raise_at
        self.calls = []
        self.received_specs = []
        self._acl_rows = acl_rows
        self._acl_user_sid = acl_user_sid
        self._acl_protected = acl_protected
        for name in drop:
            setattr(self, name, None)   # shadow the method → getattr returns None

    def _r(self, name):
        self.calls.append(name)
        if self.raise_at == name:
            raise RuntimeError("host boom")
        return {"ok": name not in self.fail, "reason": name}

    def materialise_identity(self, spec, rdp_host=None):
        self.received_specs.append(dict(spec))
        return self._r("materialise_identity")

    def apply_workspace_acl(self, plan, rdp_host=None):
        self.calls.append("apply_workspace_acl")
        if self.raise_at == "apply_workspace_acl":
            raise RuntimeError("host boom")
        if "apply_workspace_acl" in self.fail:
            return {"ok": False, "reason": "apply"}
        rows = self._acl_rows if self._acl_rows is not None else [
            {"sid": A.SYSTEM_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
            {"sid": A.ADMINISTRATORS_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
            {"sid": self._acl_user_sid, "type": "Allow", "rights": "Modify", "inherited": False},
        ]
        return {"ok": True, "rows": rows, "user_sid": self._acl_user_sid, "protected": self._acl_protected}

    def populate_runtime(self, runtime_root, rdp_host=None):
        return self._r("populate_runtime")

    def grant_rdp(self, username, rdp_host=None):
        return self._r("grant_rdp")

    def enforce_single_session(self, rdp_host=None):
        return self._r("enforce_single_session")

    def verify_remoteapp(self, username, runtime_root, rdp_host=None):
        return self._r("verify_remoteapp")

    def applocker_prepare(self, username, rdp_host=None):
        return self._r("applocker_prepare")

    def rollback_workspace_acl(self, plan, rdp_host=None):
        return self._r("rollback_workspace_acl")

    def register_observer(self, username, runtime_root, rdp_host=None):
        return self._r("register_observer")


def _account(user, login="700900"):
    return TradingAccount.objects.create(
        user=user, name="Hosted", broker_name="Hosted", account_number=login,
        is_demo=True, is_active=False, readiness_provider=PERSISTENT_WORKSPACE)


def _bound_ws(login="700900", *, node=None, uname="u1", rdp_host="10.9.9.9"):
    node = node or _node(hostname=f"node-{uname}", rdp_host=rdp_host)
    acct = _account(_user(uname), login)
    ws = HostedMt5Workspace.objects.create(trading_account=acct)
    if node is not None:
        ws.execution_node = node
        ws.save(update_fields=["execution_node"])
    return ws, acct, node


class DarkAndGuardTests(TestCase):
    def test_dark_when_master_flag_off(self):
        ws, _, _ = _bound_ws()
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor())
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_DARK)

    @override_settings(**_FLAGS_ON)   # master on, slot-prep flag OFF
    def test_dark_when_slot_prep_flag_off(self):
        ws, _, _ = _bound_ws()
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor())
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_DARK)

    @override_settings(**_PREP_ON)
    def test_refuses_reserved_customer_zero(self):
        ws, acct, _ = _bound_ws()
        with override_settings(HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS=str(acct.pk)):
            res = SP.prepare_hosted_slot(ws, executor=FakeExecutor())
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_REFUSED_RESERVED)

    @override_settings(**_PREP_ON, HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")
    def test_refuses_existing_admin_identity(self):
        from terminal_provisioning.models import AccountProvisioning
        ws, acct, _ = _bound_ws()
        AccountProvisioning.objects.create(
            trading_account=acct, windows_username=f"guvfx_u_{acct.pk}", is_admin=True,
            runtime_root=f"C:\\GuvFX\\accounts\\{acct.pk}", runtime_structure={},
            status=AccountProvisioning.Status.PENDING)
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor())
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_REFUSED_RESERVED)

    @override_settings(**_PREP_ON, HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")
    def test_not_bound_fails_closed(self):
        acct = _account(_user("nb"))
        ws = HostedMt5Workspace.objects.create(trading_account=acct)   # no execution_node
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor())
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_NOT_BOUND)

    @override_settings(**_PREP_ON, HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")
    def test_node_without_rdp_host_fails_closed(self):
        node = _node(rdp_host="")
        ws, _, _ = _bound_ws(node=node)
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor())
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_NODE_UNCONFIGURED)

    @override_settings(**_PREP_ON, HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")
    def test_dark_executor_is_fail_closed(self):
        ws, _, _ = _bound_ws()
        # No executor passed and resolve_host_executor() returns None (repository-only phase) → fail closed.
        res = SP.prepare_hosted_slot(ws)
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_NO_EXECUTOR)
        self.assertEqual(res.stage_reached, SP.ST_MATERIALISE)


@override_settings(**_PREP_ON, HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")
class HappyPathTests(TestCase):
    def test_prepares_slot_and_marks_provisioned(self):
        from terminal_provisioning.models import AccountProvisioning
        ws, acct, _ = _bound_ws()
        ex = FakeExecutor()
        res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertTrue(res.prepared, res.reason)
        self.assertEqual(res.reason, SP.PREP_OK)
        # All host steps ran in order.
        for step in ["materialise_identity", "apply_workspace_acl", "populate_runtime", "grant_rdp",
                     "enforce_single_session", "verify_remoteapp", "applocker_prepare"]:
            self.assertIn(step, ex.calls)
        # Identity record materialised → PROVISIONED (delivery precondition), only after host read-back.
        prov = AccountProvisioning.objects.get(trading_account=acct)
        self.assertEqual(prov.status, AccountProvisioning.Status.PROVISIONED)
        self.assertTrue(prov.identity_materialized and prov.runtime_materialized)
        self.assertFalse(prov.is_admin)

    def test_idempotent_second_run(self):
        ws, _, _ = _bound_ws()
        self.assertTrue(SP.prepare_hosted_slot(ws, executor=FakeExecutor()).prepared)
        res2 = SP.prepare_hosted_slot(ws, executor=FakeExecutor())
        self.assertTrue(res2.prepared, res2.reason)

    def test_never_leaks_the_windows_password(self):
        from terminal_provisioning.models import AccountProvisioning
        ws, acct, _ = _bound_ws()
        ex = FakeExecutor()
        res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertTrue(res.prepared)
        prov = AccountProvisioning.objects.get(trading_account=acct)
        pw = decrypt_password(prov.password_enc)
        self.assertTrue(pw)
        # The password flows INTO the executor spec (intended)…
        self.assertEqual(ex.received_specs[0]["password"], pw)
        # …but never into the result surface.
        self.assertNotIn(pw, str(res))
        self.assertNotIn(pw, repr(res))

    def test_observer_deferred_when_executor_lacks_it(self):
        ws, _, _ = _bound_ws()
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor(drop=["register_observer"]))
        self.assertTrue(res.prepared, res.reason)
        self.assertTrue(res.observer_deferred)

    def test_observer_not_deferred_when_executor_registers_it(self):
        ws, _, _ = _bound_ws()
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor())
        self.assertTrue(res.prepared)
        self.assertFalse(res.observer_deferred)


@override_settings(**_PREP_ON, HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")
class FailureAndRollbackTests(TestCase):
    def test_partial_failure_at_each_host_step(self):
        expect = {
            "materialise_identity": (SP.PREP_IDENTITY_FAILED, SP.ST_MATERIALISE),
            "populate_runtime": (SP.PREP_POPULATE_FAILED, SP.ST_POPULATE),
            "grant_rdp": (SP.PREP_RDP_FAILED, SP.ST_RDP),
            "enforce_single_session": (SP.PREP_SESSION_FAILED, SP.ST_SESSION),
            "verify_remoteapp": (SP.PREP_REMOTEAPP_FAILED, SP.ST_REMOTEAPP),
            "applocker_prepare": (SP.PREP_APPLOCKER_FAILED, SP.ST_APPLOCKER),
        }
        for step, (reason, stage) in expect.items():
            ws, _, _ = _bound_ws(uname=f"f_{step}")
            res = SP.prepare_hosted_slot(ws, executor=FakeExecutor(fail=[step]))
            self.assertFalse(res.prepared, step)
            self.assertEqual(res.reason, reason, step)
            self.assertEqual(res.stage_reached, stage, step)

    def test_acl_readback_mismatch_fails_and_does_not_mark_provisioned(self):
        from terminal_provisioning.models import AccountProvisioning
        ws, acct, _ = _bound_ws()
        leak = [
            {"sid": A.SYSTEM_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
            {"sid": A.ADMINISTRATORS_SID, "type": "Allow", "rights": "FullControl", "inherited": False},
            {"sid": _DEF_USER_SID, "type": "Allow", "rights": "Modify", "inherited": False},
            {"sid": A.BUILTIN_USERS_SID, "type": "Allow", "rights": "ReadAndExecute", "inherited": False},
        ]
        ex = FakeExecutor(acl_rows=leak)
        res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_ACL_FAILED)
        self.assertEqual(res.stage_reached, SP.ST_ACL)
        # A detected leak instructs the executor to roll the host DACL back to the snapshot (not additive).
        self.assertIn("rollback_workspace_acl", ex.calls)
        # mark_materialized must NOT have run — status stays PENDING (state never got ahead of the host).
        prov = AccountProvisioning.objects.get(trading_account=acct)
        self.assertEqual(prov.status, AccountProvisioning.Status.PENDING)

    def test_acl_not_protected_fails(self):
        ws, _, _ = _bound_ws()
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor(acl_protected=False))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_ACL_FAILED)

    def test_executor_incomplete_when_required_method_missing(self):
        ws, _, _ = _bound_ws()
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor(drop=["grant_rdp"]))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_EXECUTOR_INCOMPLETE)
        self.assertEqual(res.stage_reached, SP.ST_RDP)

    def test_host_exception_is_sanitised(self):
        ws, _, _ = _bound_ws()
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor(raise_at="populate_runtime"))
        self.assertFalse(res.prepared)
        # sanitised: a host error surfaces as the step's failure reason, never the exception text
        self.assertEqual(res.stage_reached, SP.ST_POPULATE)
        self.assertNotIn("boom", str(res))


class AllocateGateTests(TestCase):
    """The allocate_workspace_node gate: DARK default preserves current behaviour; armed, it advances state
    ONLY on a prepared slot."""

    def _requested(self, uname="g1", login="700900"):
        from hosted_workspace import provisioning as P
        with override_settings(**_FLAGS_ON):
            res = P.request_hosted_workspace(_user(uname), expected_login=login)
        self.assertTrue(res.ok, res.reason)
        return res.workspace

    @override_settings(**_FLAGS_ON)   # slot-prep flag OFF → gate not taken (regression check)
    def test_dark_default_advances_exactly_as_before(self):
        from hosted_workspace import provisioning as P
        _node()
        ws = self._requested()
        res = P.allocate_workspace_node(ws)
        self.assertTrue(res.ok, res.reason)
        ws.refresh_from_db()
        self.assertEqual(str(ws.canonical_state), S.WAITING_FOR_LOGIN)

    @override_settings(**_PREP_ON, HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")
    def test_armed_dark_executor_does_not_advance(self):
        from hosted_workspace import provisioning as P
        _node()
        ws = self._requested("g2")
        res = P.allocate_workspace_node(ws)   # resolve_host_executor() is None → prepare fails closed
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, SP.PREP_NO_EXECUTOR)
        ws.refresh_from_db()
        self.assertEqual(str(ws.canonical_state), S.PROVISIONING)   # NOT advanced — no slot behind it

    @override_settings(**_PREP_ON, HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")
    def test_armed_prepared_slot_advances(self):
        from hosted_workspace import provisioning as P
        _node()
        ws = self._requested("g3")
        with mock.patch.object(SP, "resolve_host_executor", return_value=FakeExecutor()):
            res = P.allocate_workspace_node(ws)
        self.assertTrue(res.ok, res.reason)
        ws.refresh_from_db()
        self.assertEqual(str(ws.canonical_state), S.WAITING_FOR_LOGIN)


class ReservedAccountSetTests(TestCase):
    """The Customer-Zero reserved-id guard must fail SAFE: unset or garbled → protect account #1; only an
    explicit empty string disables it."""

    @override_settings(HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")
    def test_explicit_empty_disables(self):
        self.assertEqual(SP._reserved_account_ids(), set())

    @override_settings(HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="none")
    def test_garbled_value_falls_back_to_customer_zero(self):
        self.assertEqual(SP._reserved_account_ids(), {1})

    @override_settings(HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="1, 2 5")
    def test_explicit_set_is_parsed(self):
        self.assertEqual(SP._reserved_account_ids(), {1, 2, 5})


class EngineScriptHygieneTests(TestCase):
    """RULE 9: every host script the engine composes must be ASCII-only so it parses identically under Windows
    PowerShell 5.1 with or without a BOM."""

    def test_engine_windows_scripts_are_ascii_only(self):
        import hosted_workspace
        base = os.path.join(os.path.dirname(os.path.dirname(hosted_workspace.__file__)),
                            "terminal_provisioning", "windows")
        scripts = ["Set-GuvfxWorkspaceAcl.ps1", "Provision-GuvfxAccount.ps1", "Grant-GuvfxRdpAccess.ps1",
                   "Populate-GuvfxViewerRuntime.ps1", "Set-GuvfxSingleSession.ps1", "Set-GuvfxAppLocker.ps1"]
        for name in scripts:
            path = os.path.join(base, name)
            data = open(path, "rb").read()
            offenders = [i for i, b in enumerate(data) if b > 127]
            self.assertEqual(offenders, [], f"{name}: non-ASCII bytes at {offenders[:5]}")
