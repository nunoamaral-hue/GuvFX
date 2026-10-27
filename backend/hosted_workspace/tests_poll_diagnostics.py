"""Per-account observation-unavailability breadcrumb — behavioural + invariance tests (ADD-ONLY diagnostic).

Proves the account-#52 systemic gap is closed WITHOUT changing recognition/promotion logic:
  * a poll that cannot reach the bridge records WHY on the row (``last_reason``), so a network-blocked new account
    is no longer indistinguishable from one merely waiting for the member to log in;
  * recording touches ONLY ``last_reason`` — never ``observation_version`` / ``canonical_state`` / ``delivery_state``
    / ``proj_*`` / ``broker_ever_matched`` (so it can neither advance a state machine nor forge a connection);
  * it is idempotent (change-only, no per-cycle churn) and fail-open (never raises into the scheduler);
  * the bounded observation cycle records the precise host reason on an unavailable observe;
  * the NEXT successful observation CLEARS the breadcrumb and promotes normally — i.e. once the bridge becomes
    reachable the account converges to CONNECTED with no manual intervention and no stale diagnostic.

No monitoring/execution/ingestion against a real host; no order; no production record is touched.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.models import UserSubscriptionState
from execution.models import TerminalNode
from execution.readiness import PERSISTENT_WORKSPACE
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import bounded_observation as BO
from hosted_workspace.manager import WorkspaceObservation, derive_workspace_decision
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.persistence import PersistStatus, persist_workspace_decision
from hosted_workspace.poll_diagnostics import record_poll_unavailable
from hosted_workspace.state_machine import WorkspaceLifecycleState as S, WorkspaceReason

U = get_user_model()


def _mk(name, *, state=S.WAITING_FOR_LOGIN, login="700900", connected=None, matched=None,
        broker_ever_matched=False):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.update_or_create(
        user=u, defaults=dict(current_plan="beta", plan_status="active", viewer_mode=False))
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    acct = TradingAccount.objects.create(user=u, name="B", broker_name="B", account_number=login,
                                         is_demo=True, broker_server=srv,
                                         readiness_provider=PERSISTENT_WORKSPACE, is_active=False)
    tn = TerminalNode.objects.create(hostname=f"n-{u.pk}", status=TerminalNode.Status.ACTIVE, rdp_host="10.0.0.1")
    ws = HostedMt5Workspace.objects.create(
        trading_account=acct, canonical_state=str(state), proj_connected=connected, proj_account_match=matched,
        broker_ever_matched=broker_ever_matched, execution_node=tn)
    return ws, acct


def _obs(**kw):
    base = dict(process_running=True, ipc_available=True, connected=True, account_match=True,
                trade_allowed=True, fresh=True, previous_state=str(S.WAITING_FOR_LOGIN),
                previous_reason=str(WorkspaceReason.NONE), observed_at=None)
    base.update(kw)
    return WorkspaceObservation(**base)


class RecordPollUnavailableUnitTests(TestCase):
    def test_records_reason_and_touches_nothing_else(self):
        ws, _ = _mk("u1", connected=False, matched=False, broker_ever_matched=False)
        before = dict(obs_ver=ws.observation_version, canon=ws.canonical_state, delivery=ws.delivery_state,
                      pconn=ws.proj_connected, pmatch=ws.proj_account_match, latch=ws.broker_ever_matched)
        self.assertTrue(record_poll_unavailable(ws, "host_unavailable"))
        ws.refresh_from_db()
        self.assertEqual(ws.last_reason, "host_unavailable")
        # EVERY other field that could influence recognition/promotion is untouched.
        self.assertEqual(ws.observation_version, before["obs_ver"])
        self.assertEqual(ws.canonical_state, before["canon"])
        self.assertEqual(ws.delivery_state, before["delivery"])
        self.assertEqual(ws.proj_connected, before["pconn"])
        self.assertEqual(ws.proj_account_match, before["pmatch"])
        self.assertEqual(ws.broker_ever_matched, before["latch"])

    def test_change_only_is_idempotent(self):
        ws, _ = _mk("u2")
        self.assertTrue(record_poll_unavailable(ws, "host_unavailable"))
        self.assertFalse(record_poll_unavailable(ws, "host_unavailable"))   # no change → no write
        self.assertTrue(record_poll_unavailable(ws, "observation_timeout"))  # a different reason → writes
        ws.refresh_from_db()
        self.assertEqual(ws.last_reason, "observation_timeout")

    def test_blank_reason_defaults_and_is_capped(self):
        ws, _ = _mk("u3")
        self.assertTrue(record_poll_unavailable(ws, ""))
        ws.refresh_from_db()
        self.assertEqual(ws.last_reason, "host_unavailable")
        # Over-long reason is capped to the column width (64) and never raises.
        ws2, _ = _mk("u3b")
        self.assertTrue(record_poll_unavailable(ws2, "x" * 500))
        ws2.refresh_from_db()
        self.assertLessEqual(len(ws2.last_reason), 64)

    def test_unsaved_workspace_is_fail_open(self):
        self.assertFalse(record_poll_unavailable(HostedMt5Workspace(), "host_unavailable"))


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_DELIVERY_LIFECYCLE_ENABLED="0")
class BoundedCycleBreadcrumbTests(TestCase):
    def test_unavailable_observe_records_reason_without_promoting(self):
        ws, _ = _mk("b1", state=S.WAITING_FOR_LOGIN)
        combined = lambda _ws: (None, None, "host_unavailable")  # bridge unreachable this cycle
        out = BO.run_bounded_observation_cycle(combined_fn=combined)
        self.assertEqual(out["unavailable"], 1)
        self.assertEqual(out["applied"], 0)
        ws.refresh_from_db()
        self.assertEqual(ws.last_reason, "host_unavailable")        # the stuck reason is now visible on the row
        self.assertEqual(ws.canonical_state, str(S.WAITING_FOR_LOGIN))  # NOT promoted
        self.assertEqual(int(ws.observation_version or 0), 0)       # version NOT advanced by a failed poll


class ClearOnSuccessTests(TestCase):
    def setUp(self):
        self.ws, self.account = _mk("c1", state=S.CONNECTED, connected=True, matched=True,
                                    broker_ever_matched=True)
        self.ws.last_reason = "host_unavailable"
        self.ws.save(update_fields=["last_reason"])

    def test_successful_observation_clears_stale_breadcrumb(self):
        decision = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED)))
        r = persist_workspace_decision(self.ws, _obs(previous_state=str(S.CONNECTED)), decision,
                                       observation_version=1)
        self.assertIn(r.status, (PersistStatus.APPLIED, PersistStatus.IDEMPOTENT))
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.last_reason, "")                   # self-healed on reachable success
        self.assertEqual(self.ws.observation_version, 1)


class RecoveryConvergenceTests(TestCase):
    def test_stuck_unreachable_account_self_heals_and_latches_on_first_reachable_observation(self):
        """A new account that was stuck unreachable (last_reason set, never matched) — once its bridge becomes
        reachable, the FIRST connected+matched observation both CLEARS the stale "unreachable" breadcrumb and sets
        the set-once first-launch latch, so the member-facing "log in in MT5" guidance stops without any manual
        intervention. (The WAITING_FOR_LOGIN→CONNECTED canonical/delivery promotion itself is the existing
        pipeline's job, corroborated separately, and is covered by its own tests.)"""
        ws, _ = _mk("r1", state=S.WAITING_FOR_LOGIN, connected=None, matched=None, broker_ever_matched=False)
        ws.last_reason = "host_unavailable"
        ws.save(update_fields=["last_reason"])
        obs = _obs(previous_state=str(S.WAITING_FOR_LOGIN))         # now reachable, connected + matched
        decision = derive_workspace_decision(obs)
        r = persist_workspace_decision(ws, obs, decision, observation_version=1)
        self.assertIn(r.status, (PersistStatus.APPLIED, PersistStatus.IDEMPOTENT))
        ws.refresh_from_db()
        self.assertEqual(ws.observation_version, 1)                # a real observation was applied
        self.assertTrue(ws.broker_ever_matched)                    # first-launch latch set (connected + matched)
        self.assertEqual(ws.last_reason, "")                       # stale "unreachable" breadcrumb cleared
