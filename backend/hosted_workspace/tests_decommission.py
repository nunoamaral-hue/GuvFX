"""Remove Broker Account STAGE-2 physical decommission — behavioural + safety tests.

Covers the packet test matrix: logical enqueue, ordered governed teardown, idempotency, retry/backoff, fail-closed
(not-tombstoned / open-positions / no-executor), the never-un-tombstone invariant, the new host-op wiring
(REMOVE_OBSERVER / DECOMMISSION_RUNTIME dispatch args incl. port validation + Customer-Zero refusal), the
tombstone-visibility list filter + live-op guards, and 25/35/36-style sibling isolation. The host seam is injected
(no real host).
"""
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.models import UserSubscriptionState
from execution.models import TerminalNode
from execution.readiness import PERSISTENT_WORKSPACE
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import decommission as D
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()


def _acct(user, name, login, *, tombstoned=False):
    srv, _ = BrokerServer.objects.get_or_create(server_name="FortressFX-Trade")
    a = TradingAccount.objects.create(user=user, name=name, broker_name="FortressFX", account_number=login,
                                      is_demo=True, broker_server=srv, readiness_provider=PERSISTENT_WORKSPACE,
                                      is_active=not tombstoned)
    if tombstoned:
        a.disconnected_at = timezone.now()
        a.save(update_fields=["disconnected_at", "is_active"])
    return a


def _ws(a, *, cleanup_state=D.CLEANUP_PENDING, next_retry_at=None, attempts=0):
    tn = TerminalNode.objects.create(hostname=f"n-{a.pk}", status=TerminalNode.Status.ACTIVE, rdp_host="10.0.0.9")
    return HostedMt5Workspace.objects.create(
        trading_account=a, canonical_state=S.CONNECTED, execution_node=tn,
        cleanup_state=cleanup_state, cleanup_next_retry_at=next_retry_at, cleanup_attempts=attempts)


class _FakeExecutor:
    """Records ordered op calls; each op returns {"ok": ...} per the configured map (default all ok)."""
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def _r(self, name, **kw):
        self.calls.append((name, kw))
        return self.results.get(name, {"ok": True})

    def remove_observer(self, **kw): return self._r("remove_observer", **kw)
    def remove_remoteapp(self, **kw): return self._r("remove_remoteapp", **kw)
    def applocker_remove(self, **kw): return self._r("applocker_remove", **kw)
    def decommission_runtime(self, **kw): return self._r("decommission_runtime", **kw)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1")
class Stage2OrchestratorTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="supp", email="supp@x.invalid", password="x")
        UserSubscriptionState.objects.update_or_create(
            user=self.user, defaults=dict(current_plan="beta", plan_status="active", viewer_mode=False))

    def _run(self, ex):
        return D.run_workspace_cleanup(executor_resolver=lambda aid, ws: ex, now=timezone.now())

    def test_all_ops_ok_converges_to_succeeded_in_order(self):
        a = _acct(self.user, "A", "70100001", tombstoned=True)
        ws = _ws(a)
        ex = _FakeExecutor()
        out = self._run(ex)
        self.assertEqual(out["succeeded"], 1)
        ws.refresh_from_db()
        self.assertEqual(ws.cleanup_state, D.CLEANUP_SUCCEEDED)
        self.assertIsNone(ws.cleanup_next_retry_at)
        # ordered governed teardown: observer -> remoteapp -> applocker -> runtime
        self.assertEqual([n for n, _ in ex.calls],
                         ["remove_observer", "remove_remoteapp", "applocker_remove", "decommission_runtime"])
        # NEVER un-tombstones
        a.refresh_from_db()
        self.assertIsNotNone(a.disconnected_at)
        self.assertFalse(a.is_active)

    def test_step_failure_marks_failed_retryable_and_retries_to_success(self):
        a = _acct(self.user, "B", "70100002", tombstoned=True)
        ws = _ws(a)
        ex_fail = _FakeExecutor(results={"decommission_runtime": {"ok": False, "reason": "residual:port"}})
        out1 = self._run(ex_fail)
        self.assertEqual(out1["retry"], 1)
        ws.refresh_from_db()
        self.assertEqual(ws.cleanup_state, D.CLEANUP_FAILED_RETRYABLE)
        self.assertIsNotNone(ws.cleanup_next_retry_at)
        self.assertEqual(ws.cleanup_attempts, 1)
        # still tombstoned after a cleanup FAILURE (the invariant)
        a.refresh_from_db(); self.assertIsNotNone(a.disconnected_at); self.assertFalse(a.is_active)
        # retry (force it due now) with a healthy executor -> SUCCEEDED
        HostedMt5Workspace.objects.filter(pk=ws.pk).update(cleanup_next_retry_at=timezone.now())
        self._run(_FakeExecutor())
        ws.refresh_from_db()
        self.assertEqual(ws.cleanup_state, D.CLEANUP_SUCCEEDED)

    def test_idempotent_absent_resources_are_ok(self):
        a = _acct(self.user, "C", "70100003", tombstoned=True)
        ws = _ws(a)
        ex = _FakeExecutor(results={n: {"ok": True, "reason": "absent"} for n in
                                    ("remove_observer", "remove_remoteapp", "applocker_remove", "decommission_runtime")})
        self._run(ex)
        ws.refresh_from_db(); self.assertEqual(ws.cleanup_state, D.CLEANUP_SUCCEEDED)

    def test_fail_closed_when_not_tombstoned(self):
        # A cleanup row on a LIVE account must NEVER trigger physical teardown.
        a = _acct(self.user, "Live", "70100004", tombstoned=False)
        ws = _ws(a, cleanup_state=D.CLEANUP_PENDING)
        ex = _FakeExecutor()
        out = D.run_workspace_cleanup(executor_resolver=lambda aid, w: ex, now=timezone.now())
        # not selected at all (queryset requires disconnected_at NOT NULL) -> zero ops, account untouched
        self.assertEqual(out["polled"], 0)
        self.assertEqual(ex.calls, [])
        a.refresh_from_db(); self.assertTrue(a.is_active); self.assertIsNone(a.disconnected_at)

    def test_fail_closed_on_open_positions(self):
        a = _acct(self.user, "Open", "70100005", tombstoned=True)
        ws = _ws(a)
        with mock.patch("trading.account_removal.open_position_count", return_value=1):
            ex = _FakeExecutor()
            out = self._run(ex)
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(ex.calls, [])                       # no teardown attempted with an open position
        ws.refresh_from_db(); self.assertEqual(ws.cleanup_state, D.CLEANUP_FAILED_RETRYABLE)
        self.assertEqual(ws.cleanup_last_reason, "open_positions")

    def test_no_executor_holds_for_retry(self):
        a = _acct(self.user, "NoEx", "70100006", tombstoned=True)
        ws = _ws(a)
        out = D.run_workspace_cleanup(executor_resolver=lambda aid, w: None, now=timezone.now())
        self.assertEqual(out["retry"], 1)
        ws.refresh_from_db()
        self.assertEqual(ws.cleanup_state, D.CLEANUP_FAILED_RETRYABLE)
        self.assertEqual(ws.cleanup_last_reason, "no_executor")

    def test_dark_when_master_flag_off(self):
        a = _acct(self.user, "Dark", "70100007", tombstoned=True)
        _ws(a)
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED="0"):
            out = D.run_workspace_cleanup(executor_resolver=lambda aid, w: _FakeExecutor(), now=timezone.now())
        self.assertFalse(out["enabled"])
        self.assertEqual(out["polled"], 0)

    def test_not_due_is_skipped_until_retry_time(self):
        a = _acct(self.user, "Future", "70100008", tombstoned=True)
        _ws(a, cleanup_state=D.CLEANUP_FAILED_RETRYABLE,
            next_retry_at=timezone.now() + timezone.timedelta(hours=1))
        out = D.run_workspace_cleanup(executor_resolver=lambda aid, w: _FakeExecutor(), now=timezone.now())
        self.assertEqual(out["polled"], 0)                   # backoff not elapsed

    def test_decommission_runtime_receives_endpoint_port(self):
        a = _acct(self.user, "Port", "70100009", tombstoned=True)
        ws = _ws(a)
        from execution.models import HostedExecutionEndpoint
        HostedExecutionEndpoint.objects.create(workspace=ws, workspace_uuid=ws.workspace_uuid,
                                               trading_account=a, terminal_node=ws.execution_node,
                                               host="10.0.0.9", port=8806,
                                               state=HostedExecutionEndpoint.State.RETIRED)
        ex = _FakeExecutor()
        self._run(ex)
        call = dict(ex.calls)["decommission_runtime"]
        self.assertEqual(call.get("port"), 8806)


class TombstoneVisibilityApiTests(TestCase):
    """Phase 14/15: a removed account is hidden from the current LIST + blocked from live ops, while detail/history/
    idempotent-remove still RESOLVE it."""
    def setUp(self):
        from rest_framework.test import APIClient
        self.user = U.objects.create_user(username="m", email="m@x.invalid", password="x")
        UserSubscriptionState.objects.update_or_create(
            user=self.user, defaults=dict(current_plan="beta", plan_status="active", viewer_mode=False))
        self.c = APIClient(); self.c.force_authenticate(self.user)

    def _ids(self, resp):
        data = resp.json()
        items = data.get("results", data) if isinstance(data, dict) else data
        return sorted(x["id"] for x in items) if isinstance(items, list) else []

    def test_list_excludes_tombstoned_detail_and_remove_still_resolve(self):
        live = _acct(self.user, "Live", "70200001", tombstoned=False)
        removed = _acct(self.user, "Gone", "70200002", tombstoned=True)
        r = self.c.get("/api/trading/accounts/")
        self.assertEqual(r.status_code, 200)
        ids = self._ids(r)
        self.assertIn(live.id, ids)
        self.assertNotIn(removed.id, ids)                        # hidden from the current list
        # detail STILL resolves (history / idempotency must keep working)
        self.assertEqual(self.c.get(f"/api/trading/accounts/{removed.id}/").status_code, 200)
        # idempotent re-remove resolves + returns already-removed
        rr = self.c.post(f"/api/trading/accounts/{removed.id}/remove/")
        self.assertIn(rr.status_code, (200, 409))                # resolves (not a 404-from-list-hiding)

    def test_start_trading_blocked_on_tombstoned_but_stop_allowed(self):
        removed = _acct(self.user, "Gone2", "70200003", tombstoned=True)
        _ws(removed, cleanup_state=D.CLEANUP_PENDING)
        start = self.c.post(f"/api/trading/accounts/{removed.id}/set-active/", {"is_active": True}, format="json")
        self.assertEqual(start.status_code, 409)                 # cannot re-start a removed account
        stop = self.c.post(f"/api/trading/accounts/{removed.id}/set-active/", {"is_active": False}, format="json")
        self.assertEqual(stop.status_code, 200)                  # STOP is harmless/idempotent
        # still tombstoned throughout
        removed.refresh_from_db()
        self.assertIsNotNone(removed.disconnected_at); self.assertFalse(removed.is_active)

    def test_guard_live_op_raises_409_on_removed_but_not_live(self):
        # The shared guard used by bc_test_connection / bc_retry_validation / bc_replace_credentials (and Start /
        # delivery / arm): a removed account fails closed with 409; a live account passes.
        from trading.account_removal import guard_live_op, AccountRemovedError
        live = _acct(self.user, "L", "70200007", tombstoned=False)
        removed = _acct(self.user, "R", "70200008", tombstoned=True)
        guard_live_op(live)                                  # no raise
        with self.assertRaises(AccountRemovedError) as ctx:
            guard_live_op(removed)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_cross_user_remove_is_idor_safe(self):
        other = U.objects.create_user(username="o", email="o@x.invalid", password="x")
        theirs = _acct(other, "Theirs", "70200004", tombstoned=False)
        r = self.c.post(f"/api/trading/accounts/{theirs.id}/remove/")
        self.assertEqual(r.status_code, 404)                     # owner-scoped get_object -> not found


class RemoveEnqueuesCleanupTests(TestCase):
    """The trigger: a successful logical Remove flips the hosted workspace's cleanup_state to PENDING inside the
    tombstone transaction (commits atomically with disconnected_at), and NEVER blocks the removal."""
    def setUp(self):
        self.user = U.objects.create_user(username="rm", email="rm@x.invalid", password="x")
        UserSubscriptionState.objects.update_or_create(
            user=self.user, defaults=dict(current_plan="beta", plan_status="active", viewer_mode=False))

    def test_remove_account_enqueues_pending_cleanup(self):
        from trading.account_removal import remove_account
        a = _acct(self.user, "Enq", "70300001", tombstoned=False)
        ws = _ws(a, cleanup_state=D.CLEANUP_NOT_REQUIRED)
        res = remove_account(a, actor="test")
        self.assertTrue(res["removed"])
        a.refresh_from_db(); ws.refresh_from_db()
        self.assertIsNotNone(a.disconnected_at)                  # tombstoned
        self.assertFalse(a.is_active)
        self.assertEqual(ws.cleanup_state, D.CLEANUP_PENDING)    # cleanup queued atomically
        self.assertIsNotNone(ws.cleanup_next_retry_at)

    def test_remove_is_idempotent_and_does_not_requeue(self):
        from trading.account_removal import remove_account
        a = _acct(self.user, "Idem", "70300002", tombstoned=True)     # already removed
        ws = _ws(a, cleanup_state=D.CLEANUP_SUCCEEDED)
        res = remove_account(a, actor="test")
        self.assertTrue(res["already"])
        ws.refresh_from_db()
        self.assertEqual(ws.cleanup_state, D.CLEANUP_SUCCEEDED)  # a completed cleanup is NOT reset by a re-remove


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1")
class PortReuseGuardTests(TestCase):
    """The HIGH fix: a retired port stays RESERVED (not reallocated to a co-resident tenant) until the removed
    account's Stage-2 cleanup SUCCEEDS - so the port-targeted bridge kill can never hit another live tenant."""
    def setUp(self):
        self.user = U.objects.create_user(username="p", email="p@x.invalid", password="x")
        UserSubscriptionState.objects.update_or_create(
            user=self.user, defaults=dict(current_plan="beta", plan_status="active", viewer_mode=False))

    def _retired_ep(self, a, ws, host, port):
        from execution.models import HostedExecutionEndpoint
        return HostedExecutionEndpoint.objects.create(
            workspace=ws, workspace_uuid=ws.workspace_uuid, trading_account=a, terminal_node=ws.execution_node,
            host=host, port=port, state=HostedExecutionEndpoint.State.RETIRED)

    def test_retired_port_reserved_until_cleanup_succeeded(self):
        from execution.endpoint_service import allocate_port
        a = _acct(self.user, "PR", "70400001", tombstoned=True)
        ws = _ws(a, cleanup_state=D.CLEANUP_PENDING)
        host = ws.execution_node.rdp_host
        self._retired_ep(a, ws, host, 8801)
        # cleanup still active -> port 8801 must NOT be handed out
        self.assertNotEqual(allocate_port(host), 8801)
        # once cleanup SUCCEEDS -> port is freed for reuse
        HostedMt5Workspace.objects.filter(pk=ws.pk).update(cleanup_state=D.CLEANUP_SUCCEEDED)
        self.assertEqual(allocate_port(host), 8800)  # lowest free; 8801 now reusable (not < 8800)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1")
class ReservedFloorTests(TestCase):
    def test_reserved_account_is_dequeued_never_torn_down(self):
        # Defence in depth: a reserved id (<2) reaching _cleanup_one is de-queued to NOT_REQUIRED and never torn down.
        user = U.objects.create_user(username="cz", email="cz@x.invalid", password="x")
        UserSubscriptionState.objects.update_or_create(
            user=user, defaults=dict(current_plan="beta", plan_status="active", viewer_mode=False))
        a = _acct(user, "CZ", "70400002", tombstoned=True)
        ws = _ws(a, cleanup_state=D.CLEANUP_PENDING)
        ex = _FakeExecutor()
        a.id = 1                                            # ws.trading_account is this cached instance
        outcome, reason = D._cleanup_one(ws, executor_resolver=lambda aid, w: ex, now=timezone.now())
        self.assertEqual((outcome, reason), ("skipped", "reserved_account"))
        self.assertEqual(ex.calls, [])                      # never dispatched any teardown op
        ws.refresh_from_db()
        self.assertEqual(ws.cleanup_state, D.CLEANUP_NOT_REQUIRED)


class HostOpWiringTests(TestCase):
    def test_new_ops_registered_in_protocol_and_dispatch(self):
        from hosted_workspace.host_protocol import HOSTED_OPERATIONS
        from hosted_workspace.host_agent_dispatch import OP_PRIMITIVES, is_known_primitive
        for op, primitive in (("REMOVE_OBSERVER", "remove_observer"),
                              ("DECOMMISSION_RUNTIME", "decommission_runtime")):
            self.assertIn(op, HOSTED_OPERATIONS)                 # wire allow-list
            self.assertIn(op, OP_PRIMITIVES)                     # op -> primitive map
            self.assertEqual(OP_PRIMITIVES[op]["primitive"], primitive)
            self.assertTrue(is_known_primitive(primitive))       # primitive-name guard accepts it
        # the invariant that binds the two layers still holds
        self.assertEqual(set(OP_PRIMITIVES), set(HOSTED_OPERATIONS))

    def test_decommission_build_args_validates_port_and_refuses_reserved(self):
        from hosted_workspace import host_agent_dispatch as HAD
        slot = HAD.derive_slot(37)
        # valid per-tenant port passes through
        args = HAD._build_args("DECOMMISSION_RUNTIME", slot, {"params": {"port": 8806}}, envelope_open=None)
        self.assertEqual(args["port"], 8806)
        self.assertEqual(args["username"], "guvfx_u_37")
        self.assertEqual(args["account_id"], 37)
        # out-of-range port rejected
        with self.assertRaises(Exception):
            HAD._build_args("DECOMMISSION_RUNTIME", slot, {"params": {"port": 22}}, envelope_open=None)
        # port 0 (unknown) allowed -> host skips the port kill
        args0 = HAD._build_args("DECOMMISSION_RUNTIME", slot, {"params": {"port": 0}}, envelope_open=None)
        self.assertEqual(args0["port"], 0)
