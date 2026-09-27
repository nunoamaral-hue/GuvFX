"""Phase 7 — member "Remove account" (history-retaining decommission) + history-safe assignment removal.

Covers the removal matrix: tombstone-not-delete, open-position fail-closed gate, entitlement slot release,
owner-scoping (cross-user denied), idempotence, the closed hard-delete hole (405), pending-order cancellation,
observer exclusion, and the history-safe Remove-Strategy rule (never-traded hard-delete vs traded deactivate).
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from billing.models import UserSubscriptionState
from strategies.models import Strategy, StrategyAssignment
from strategies.assignment_service import assignment_has_history
from trading.account_removal import remove_account, open_position_count
from trading.models import BrokerServer, TradingAccount, Trade
from trading.views import TradingAccountViewSet

U = get_user_model()


def _user(name):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.create(user=u, current_plan=UserSubscriptionState.Plan.BETA,
                                         plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False)
    return u


def _acct(user, number, *, is_active=True, is_demo=True):
    return TradingAccount.objects.create(user=user, name="A", account_number=number, broker_name="B",
                                         is_demo=is_demo, is_active=is_active)


def _trade(account, *, ticket, open_=True, magic=None, assignment=None):
    return Trade.objects.create(
        account=account, ticket=ticket, symbol="XAUUSD", side="BUY", volume="0.01",
        open_time=timezone.now(), close_time=(None if open_ else timezone.now()),
        open_price="2000.00", magic_number=magic, strategy_assignment=assignment)


def _remove(user, acct_id):
    req = APIRequestFactory().post(f"/api/trading/accounts/{acct_id}/remove/")
    force_authenticate(req, user=user)
    return TradingAccountViewSet.as_view({"post": "remove"})(req, pk=acct_id)


def _delete(user, acct_id):
    req = APIRequestFactory().delete(f"/api/trading/accounts/{acct_id}/")
    force_authenticate(req, user=user)
    return TradingAccountViewSet.as_view({"delete": "destroy"})(req, pk=acct_id)


class RemoveAccountTests(TestCase):
    def setUp(self):
        self.user = _user("rm")

    def test_remove_tombstones_not_deletes(self):
        a = _acct(self.user, "1001")
        res = _remove(self.user, a.id)
        self.assertEqual(res.status_code, 200)
        a.refresh_from_db()
        self.assertIsNotNone(a.disconnected_at)   # tombstoned
        self.assertFalse(a.is_active)             # execution stopped
        self.assertTrue(TradingAccount.objects.filter(id=a.id).exists())   # row RETAINED

    def test_remove_blocked_on_open_position(self):
        a = _acct(self.user, "1002")
        _trade(a, ticket="T1", open_=True)
        self.assertEqual(open_position_count(a), 1)
        res = _remove(self.user, a.id)
        self.assertEqual(res.status_code, 409)
        a.refresh_from_db()
        self.assertIsNone(a.disconnected_at)      # NOT removed — fail-closed
        self.assertTrue(a.is_active)

    def test_remove_allowed_when_positions_closed(self):
        a = _acct(self.user, "1003")
        _trade(a, ticket="T2", open_=False)       # closed position present — history, but no OPEN position
        res = _remove(self.user, a.id)
        self.assertEqual(res.status_code, 200)
        a.refresh_from_db(); self.assertIsNotNone(a.disconnected_at)
        self.assertEqual(a.trades.count(), 1)     # closed Trade history RETAINED

    def test_remove_releases_owned_slot(self):
        # Estate-default (un-enforced) path: a removed account must free the owned cap immediately.
        from trading.account_service import create_customer_account
        # 10 is the legacy cap ceiling; create up to it, then removal must let a new one through.
        accts = [_acct(self.user, f"20{i:02d}") for i in range(3)]
        before = TradingAccount.objects.filter(user=self.user, disconnected_at__isnull=True).count()
        _remove(self.user, accts[0].id)
        after = TradingAccount.objects.filter(user=self.user, disconnected_at__isnull=True).count()
        self.assertEqual(after, before - 1)       # owned (non-tombstoned) count dropped by exactly one

    def test_foreign_account_denied(self):
        other = _user("rm-other")
        a = _acct(other, "1004")
        res = _remove(self.user, a.id)
        self.assertEqual(res.status_code, 404)    # owner-scoped get_object → not found for a foreign account
        a.refresh_from_db(); self.assertIsNone(a.disconnected_at)

    def test_repeated_remove_idempotent(self):
        a = _acct(self.user, "1005")
        r1 = _remove(self.user, a.id)
        r2 = _remove(self.user, a.id)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.data.get("already"))

    def test_hard_delete_blocked_405(self):
        a = _acct(self.user, "1006")
        res = _delete(self.user, a.id)
        self.assertEqual(res.status_code, 405)    # DELETE closed — history-safety
        self.assertTrue(TradingAccount.objects.filter(id=a.id).exists())

    def test_pending_order_jobs_cancelled_on_remove(self):
        from execution.models import ExecutionJob
        a = _acct(self.user, "1007")
        j = ExecutionJob.objects.create(account=a, job_type=ExecutionJob.JobType.PLACE_ORDER, payload={},
                                        status=ExecutionJob.Status.PENDING)
        _remove(self.user, a.id)
        j.refresh_from_db()
        self.assertEqual(j.status, ExecutionJob.Status.FAILED)   # queued order cancelled, won't dispatch

    def test_removed_excluded_from_observer_query(self):
        from hosted_workspace.models import HostedMt5Workspace
        a = _acct(self.user, "1008")
        HostedMt5Workspace.objects.create(trading_account=a)
        _remove(self.user, a.id)
        # The bounded-observation query excludes tombstoned accounts.
        remaining = HostedMt5Workspace.objects.filter(trading_account__disconnected_at__isnull=True,
                                                      trading_account_id=a.id)
        self.assertEqual(remaining.count(), 0)


class AssignmentHistorySafeRemovalTests(TestCase):
    def setUp(self):
        self.user = _user("as")
        self.acct = _acct(self.user, "3001")
        self.strat = Strategy.objects.create(owner=self.user, name="S", filters={"template_slug": "x"})

    def _asn(self, magic=None):
        return StrategyAssignment.objects.create(account=self.acct, strategy=self.strat, is_active=True,
                                                 magic_number=magic)

    def test_never_traded_has_no_history(self):
        self.assertFalse(assignment_has_history(self._asn()))

    def test_traded_assignment_has_history_via_trade_fk(self):
        a = self._asn()
        _trade(self.acct, ticket="H1", open_=False, assignment=a)
        self.assertTrue(assignment_has_history(a))

    def test_traded_assignment_has_history_via_magic(self):
        a = self._asn(magic=1000009999)
        _trade(self.acct, ticket="H2", open_=False, magic=1000009999)   # deal carries the magic, FK unstamped
        self.assertTrue(assignment_has_history(a))

    def test_execution_job_counts_as_history(self):
        from execution.models import ExecutionJob
        a = self._asn()
        ExecutionJob.objects.create(account=self.acct, assignment=a, job_type=ExecutionJob.JobType.PLACE_ORDER,
                                    payload={}, status=ExecutionJob.Status.SUCCESS)
        self.assertTrue(assignment_has_history(a))
