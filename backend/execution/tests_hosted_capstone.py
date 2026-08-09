"""ADR-0034 Execution Engine CAPSTONE — durable workspace->node binding + provisioning contract + routing
invariants (PARTS 2/3/5/6/12).

Proves: the provisioning contract (assign/clear, versioned, idempotent, fail-closed); a workspace resolves
to exactly ONE authorised node (NULL/mismatch ⇒ not routable); the claim seam rejects a job whose node
drifted from the account's node; arming refuses an unbound/mismatched workspace; the expected broker
identity is SERVER-derived (a forged payload cannot authorise); and each job re-resolves from its own
durable server-side truth (no stale identity leaks from a previous job).
"""
from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from trading.models import BrokerServer, TradingAccount

from execution import hosted_provisioning as P
from execution import hosted_routing as HR
from execution import readiness as R
from execution.models import TerminalNode
from execution.readiness import PERSISTENT_WORKSPACE
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()


def _account(*, login="700900", server="IS6-Demo", node=True, ws=True, armed=True, bind=True):
    user = U.objects.create_user(username=f"c{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name=server)
    tn = TerminalNode.objects.create(hostname=f"node-{login}") if node else None
    acct = TradingAccount.objects.create(
        user=user, name="a", broker_name="B", account_number=login, is_demo=True,
        broker_server=srv, readiness_provider=PERSISTENT_WORKSPACE, terminal_node=tn,
        workspace_confirmed_at=timezone.now())  # ADR-0034 Onboarding — a ready account is a CONFIRMED account
    if ws:
        HostedMt5Workspace.objects.create(
            trading_account=acct, canonical_state=S.EXECUTION_READY, proj_connected=True,
            proj_trade_allowed=True, proj_account_match=True, proj_execution_ready=True,
            last_decision_at=timezone.now(), execution_enabled=armed,
            execution_node=(tn if bind else None))
    return acct


class ProvisioningContractTests(TestCase):
    def test_assign_sets_node_and_versions(self):
        acct = _account(bind=False)
        node = acct.terminal_node
        P.assign_workspace_execution_node(acct, node, actor="admin")
        ws = acct.hosted_workspace
        ws.refresh_from_db()
        self.assertEqual(ws.execution_node_id, node.pk)
        self.assertEqual(ws.execution_binding_generation, 1)

    def test_assign_is_idempotent_no_extra_generation(self):
        acct = _account(bind=False)
        P.assign_workspace_execution_node(acct, acct.terminal_node)
        P.assign_workspace_execution_node(acct, acct.terminal_node)  # idempotent
        acct.hosted_workspace.refresh_from_db()
        self.assertEqual(acct.hosted_workspace.execution_binding_generation, 1)

    def test_reassign_bumps_generation(self):
        acct = _account(bind=True)  # generation 0 (created with node directly)
        other = TerminalNode.objects.create(hostname="node-other")
        P.assign_workspace_execution_node(acct, other)
        acct.hosted_workspace.refresh_from_db()
        self.assertEqual(acct.hosted_workspace.execution_node_id, other.pk)
        self.assertEqual(acct.hosted_workspace.execution_binding_generation, 1)

    def test_clear_unbinds_and_versions(self):
        acct = _account(bind=True)
        P.clear_workspace_execution_node(acct)
        acct.hosted_workspace.refresh_from_db()
        self.assertIsNone(acct.hosted_workspace.execution_node_id)
        self.assertEqual(acct.hosted_workspace.execution_binding_generation, 1)

    def test_assign_none_node_fails_closed(self):
        acct = _account(bind=False)
        self.assertIsNone(P.assign_workspace_execution_node(acct, None))
        acct.hosted_workspace.refresh_from_db()
        self.assertIsNone(acct.hosted_workspace.execution_node_id)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class RoutingBindingTests(TestCase):
    def test_bound_armed_agree_routes_ok(self):
        acct = _account()  # armed + bound + agree
        r = HR.resolve_hosted_route(acct)
        self.assertTrue(r.ok, r.reason_code)
        self.assertEqual(r.expected_login, "700900")

    def test_armed_but_binding_cleared_not_routable(self):
        acct = _account()
        P.clear_workspace_execution_node(acct)   # cleared after arming (clear does not disarm)
        acct = TradingAccount.objects.get(pk=acct.pk)
        self.assertEqual(HR.resolve_hosted_route(acct).reason_code, HR.ER_NODE_UNBOUND)

    def test_binding_mismatch_not_routable(self):
        acct = _account()
        other = TerminalNode.objects.create(hostname="node-elsewhere")
        ws = acct.hosted_workspace
        ws.execution_node = other       # binding disagrees with account.terminal_node
        ws.save(update_fields=["execution_node"])
        acct = TradingAccount.objects.get(pk=acct.pk)
        self.assertEqual(HR.resolve_hosted_route(acct).reason_code, HR.ER_NODE_MISMATCH)

    def test_claim_rejects_job_node_drift(self):
        acct = _account()
        other = TerminalNode.objects.create(hostname="node-drift")

        class _J:
            def __init__(s):
                s.account = acct
                s.terminal_node_id = other.pk      # job node != account node
                s.job_type = "PLACE_ORDER"
        self.assertEqual(HR.authorize_hosted_claim(_J(), worker_is_node_aware=True).reason_code,
                         HR.ER_NODE_MISMATCH)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class ArmBindingPreconditionTests(TestCase):
    def test_arm_refuses_unbound(self):
        acct = _account(armed=False, bind=False)
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, P.ARM_NODE_UNBOUND)

    def test_arm_refuses_mismatch(self):
        acct = _account(armed=False, bind=True)
        other = TerminalNode.objects.create(hostname="node-mismatch")
        ws = acct.hosted_workspace
        ws.execution_node = other
        ws.save(update_fields=["execution_node"])
        acct = TradingAccount.objects.get(pk=acct.pk)
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, P.ARM_NODE_MISMATCH)

    def test_arm_ok_when_bound_and_agree(self):
        acct = _account(armed=False, bind=True)
        self.assertTrue(P.arm_hosted_workspace_execution(acct).ok)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class ServerSideIdentityTests(TestCase):
    def test_expected_identity_is_server_derived_not_payload(self):
        """PART 6: the route's expected login/server come from the account's durable bindings, never from a
        client payload. A forged payload cannot change the authorised identity."""
        acct = _account(login="700900", server="IS6-Demo")
        r = HR.resolve_hosted_route(acct)  # takes only the account — no payload input exists to forge
        self.assertEqual(r.expected_login, "700900")
        self.assertEqual(r.expected_server, "IS6-Demo")

    def test_each_account_resolves_its_own_identity_no_stale_leak(self):
        """PART 5: two different accounts resolve independently from their OWN durable truth; account A's
        identity can never authorise account B (resolve is stateless per account)."""
        a = _account(login="700111", server="Srv-A")
        b = _account(login="800222", server="Srv-B")
        ra = HR.resolve_hosted_route(a)
        rb = HR.resolve_hosted_route(b)
        self.assertEqual((ra.expected_login, ra.expected_server), ("700111", "Srv-A"))
        self.assertEqual((rb.expected_login, rb.expected_server), ("800222", "Srv-B"))
        # B's route never inherits A's node/login even if evaluated right after A.
        self.assertNotEqual(rb.expected_login, ra.expected_login)


class SinglePathProofTests(TestCase):
    """PART 15 (STRUCTURAL): the certified bridge (``scripts/mt5_signal_bridge.py``) is the SOLE order path.

    Proven by SWEEPING the hosted backend TREE — a structural glob, not a hand-maintained module list — so a
    NEW hosted backend module cannot silently open a second order path and still pass. Two invariants hold
    over the whole swept set: (1) no module calls the broker order-mutation surface (``order_send`` /
    ``order_check``); (2) the ONLY broker-API (``MetaTrader5``) importers are a small SANCTIONED, host-only,
    READ-ONLY allow-list, each separately proven to touch no order surface. The earlier proof asserted over a
    curated 15-module subset, whose blanket ``no MetaTrader5 import`` predicate was already false for the
    read-only observer command — this replaces it with the true, tree-wide invariant."""

    # backend/ root, from backend/execution/tests_hosted_capstone.py
    _BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    # MAY import MetaTrader5: sanctioned, HOST-ONLY, READ-ONLY observers (lazy import; guarded_initialize +
    # terminal/account/positions observation only, never order_send/order_check/login). Each is re-proven
    # order-free by invariant (1). An importer NOT on this list fails the sweep — it must be justified here.
    _SANCTIONED_MT5_IMPORTERS = {
        "hosted_workspace/management/commands/certify_workspace_observation.py",
    }

    def _hosted_backend_py_files(self):
        """Structural glob of the hosted backend surface: the whole ``hosted_workspace`` app + the
        ``execution`` hosted seam modules + the readiness/gate authorities. Excludes tests and migrations.
        Returns a set of repo-relative POSIX paths."""
        import glob
        pats = ["execution/hosted_*.py", "execution/readiness.py", "execution/broker_gate.py",
                "hosted_workspace/**/*.py"]
        files = set()
        for pat in pats:
            for f in glob.glob(os.path.join(self._BACKEND, pat), recursive=True):
                rel = os.path.relpath(f, self._BACKEND).replace(os.sep, "/")
                base = rel.rsplit("/", 1)[-1]
                if "/migrations/" in rel or "/tests/" in rel or base.startswith(("test_", "tests_")):
                    continue
                files.add(rel)
        return files

    def test_bridge_is_sole_order_path_structural_sweep(self):
        import re
        files = self._hosted_backend_py_files()
        # sanity: the glob actually resolved the seam (guards against an empty/mis-scoped sweep, RULE 11)
        self.assertIn("execution/hosted_routing.py", files)
        self.assertIn("hosted_workspace/manager.py", files)
        self.assertIn("hosted_workspace/management/commands/certify_workspace_observation.py", files)

        import_re = re.compile(r"^[ \t]*(?:import|from)[ \t]+MetaTrader5\b", re.M)
        order_re = re.compile(r"\border_send[ \t]*\(|\border_check[ \t]*\(")
        importers, order_callers = [], []
        for rel in sorted(files):
            with open(os.path.join(self._BACKEND, rel), encoding="utf-8") as fh:
                src = fh.read()
            if import_re.search(src):
                importers.append(rel)
            if order_re.search(src):
                order_callers.append(rel)

        # (1) NO hosted backend module invokes the order-mutation surface — the bridge alone does.
        self.assertEqual(order_callers, [],
                         f"hosted backend must not call order_send/order_check: {order_callers}")
        # RULE 11 — prove the import detector is NON-VACUOUS: the known sanctioned importer MUST be found, so
        # `unexpected == []` below means "searched and verified", not "the regex matched nothing" (a broken
        # `import_re` or a relocated import would otherwise make invariant (2) pass for the wrong reason).
        self.assertIn("hosted_workspace/management/commands/certify_workspace_observation.py", importers,
                      "import_re failed to detect the known MetaTrader5 importer — the sweep would be vacuous")
        # (2) The ONLY broker-API importers are the sanctioned read-only observers.
        unexpected = sorted(set(importers) - self._SANCTIONED_MT5_IMPORTERS)
        self.assertEqual(unexpected, [],
                         f"unsanctioned hosted-backend broker-API import(s) — a possible 2nd order path: "
                         f"{unexpected}")

    def test_positive_control_the_sweep_can_detect_an_order_caller(self):
        """RULE 11 positive controls for BOTH measurement surfaces: the order-surface regex DOES fire on a
        real call form, and the import-surface regex DOES fire on a real import — so an empty result means
        'searched and found none', not 'the search is broken'."""
        import re
        order_re = re.compile(r"\border_send[ \t]*\(|\border_check[ \t]*\(")
        self.assertTrue(order_re.search("result = mt5.order_send(request)"))
        self.assertTrue(order_re.search("mt5.order_check (req)"))
        self.assertFalse(order_re.search("# this module performs no order and never sends one"))
        import_re = re.compile(r"^[ \t]*(?:import|from)[ \t]+MetaTrader5\b", re.M)
        self.assertTrue(import_re.search("    import MetaTrader5 as mt5"))
        self.assertTrue(import_re.search("from MetaTrader5 import order_send"))
        self.assertFalse(import_re.search("# imports MetaTrader5 lazily on the host"))


class IdempotencyKeyCollisionTests(TestCase):
    """Mutation-adequacy for the pure ``hosted_idempotency_key``: it is deterministic AND every intended
    component (workspace / login / server / job / operation / strategy) genuinely participates — changing any
    one yields a different key, so the same logical order can never collide across users/workspaces/jobs/ops."""

    def test_deterministic_and_every_component_matters(self):
        from execution.hosted_idempotency import hosted_idempotency_key as K
        base = dict(workspace_uuid="ws-1", expected_login="700111", expected_server="Srv-A",
                    job_id=1, operation="PLACE_ORDER", strategy_id="5")
        baseline = K(**base)
        self.assertTrue(baseline.startswith("HWX-"))
        self.assertEqual(K(**base), baseline)  # deterministic
        for field, alt in [("workspace_uuid", "ws-2"), ("expected_login", "800222"),
                           ("expected_server", "Srv-B"), ("job_id", 2),
                           ("operation", "CLOSE_TRADE"), ("strategy_id", "6")]:
            mut = dict(base); mut[field] = alt
            self.assertNotEqual(K(**mut), baseline, f"key MUST change when {field} changes (collision risk)")


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class FailClosedBranchTests(TestCase):
    def test_readiness_not_ready_when_trade_not_allowed(self):
        from execution.readiness import RW_WORKSPACE_NOT_READY, PersistentWorkspaceProvider
        acct = _account()  # armed + bound + connected + matched
        ws = acct.hosted_workspace
        ws.proj_trade_allowed = False       # connected+matched but trading halted → not ready
        ws.canonical_state = S.CONNECTED    # so canonical_execution_ready is False too
        ws.save(update_fields=["proj_trade_allowed", "canonical_state"])
        d = PersistentWorkspaceProvider().evaluate(TradingAccount.objects.get(pk=acct.pk))
        self.assertFalse(d.eligible)
        self.assertEqual(d.reason_code, RW_WORKSPACE_NOT_READY)

    def test_route_binding_mismatch_on_empty_login(self):
        # Armed + node-bound + agree, but the account carries no bound login → the route cannot be safely
        # pinned → ER_BINDING_MISMATCH (fail closed rather than route an unpinnable order).
        acct = _account(login="700900")
        acct.account_number = ""            # no bound broker login
        acct.save(update_fields=["account_number"])
        r = HR.resolve_hosted_route(TradingAccount.objects.get(pk=acct.pk))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, HR.ER_BINDING_MISMATCH)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class ArmFailClosedBranchTests(TestCase):
    """Decision D: EVERY arm precondition fails closed with a SPECIFIC reason code (most-specific-first).
    ``_arm_preconditions`` is a standalone conjunction; each branch is asserted ACROSS THIS SUITE (this class
    + ``ArmBindingPreconditionTests`` + ``tests_hosted_provisioning``) — a mutation that dropped one (silently
    arming when it should refuse) must fail a test, not slip through. The compound ``RW_WORKSPACE_NOT_READY``
    branch is split into its two independent disjuncts below so a mutant dropping either sub-check dies."""

    def _ready_unarmed(self):
        return _account(armed=False, bind=True)  # passes every precondition; each test breaks exactly one

    def test_account_missing(self):
        self.assertEqual(P.arm_hosted_workspace_execution(None).reason_code, "broker_account_missing")

    def test_subsystem_disabled_for_non_persistent_provider(self):
        acct = self._ready_unarmed()
        acct.readiness_provider = "mt5_native"
        acct.save(update_fields=["readiness_provider"])
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, R.RW_SUBSYSTEM_DISABLED)

    def test_account_inactive(self):
        acct = self._ready_unarmed()
        acct.is_active = False
        acct.save(update_fields=["is_active"])
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, "broker_account_inactive")

    def test_account_disconnected(self):
        acct = self._ready_unarmed()
        acct.disconnected_at = timezone.now()
        acct.save(update_fields=["disconnected_at"])
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, "broker_account_disconnected")

    def test_real_account_not_enabled(self):
        acct = self._ready_unarmed()
        acct.is_demo = False
        acct.save(update_fields=["is_demo"])
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, R.RW_REAL_ACCOUNT_NOT_ENABLED)

    def test_no_workspace(self):
        acct = _account(armed=False, ws=False)
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, P.ARM_NO_WORKSPACE)

    def test_workspace_owner_mismatch(self):
        # Defensive branch unreachable via the reverse OneToOne (a workspace always belongs to its account),
        # so it is asserted with a duck-typed account whose workspace claims a different owner.
        class _WS:
            trading_account_id = 999999
        class _Acct:
            pk = 4242
            readiness_provider = PERSISTENT_WORKSPACE
            is_active = True
            disconnected_at = None
            is_demo = True
            hosted_workspace = _WS()
        self.assertEqual(P.arm_hosted_workspace_execution(_Acct()).reason_code, "workspace_owner_mismatch")

    def test_workspace_not_connected(self):
        acct = self._ready_unarmed()
        ws = acct.hosted_workspace
        ws.proj_connected = False
        ws.save(update_fields=["proj_connected"])
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, R.RW_WORKSPACE_NOT_CONNECTED)

    def test_workspace_not_ready_trade_disjunct_only(self):
        # disjunct 1 ONLY: broker trading halted, but canonical still EXECUTION_READY. Kills a mutant that
        # dropped the `proj_trade_allowed is not True` sub-check (the fresher, safety-relevant one).
        acct = self._ready_unarmed()
        ws = acct.hosted_workspace
        ws.proj_trade_allowed = False           # canonical_state stays EXECUTION_READY
        ws.save(update_fields=["proj_trade_allowed"])
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, R.RW_WORKSPACE_NOT_READY)

    def test_workspace_not_ready_canonical_disjunct_only(self):
        # disjunct 2 ONLY: canonical not EXECUTION_READY, but trading still allowed. Kills a mutant that
        # dropped the `not canonical_execution_ready` sub-check.
        acct = self._ready_unarmed()
        ws = acct.hosted_workspace
        ws.canonical_state = S.CONNECTED        # proj_trade_allowed stays True
        ws.save(update_fields=["canonical_state"])
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, R.RW_WORKSPACE_NOT_READY)


class DisarmFailClosedTests(TestCase):
    def test_disarm_without_workspace_reports_no_workspace(self):
        acct = _account(armed=False, ws=False)
        self.assertEqual(P.disarm_hosted_workspace_execution(acct).reason_code, P.ARM_NO_WORKSPACE)
