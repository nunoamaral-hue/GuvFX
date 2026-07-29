"""ADR-0021 — the ONE canonical TradingAccount creation contract (``trading.account_service``).

Proves both customer-facing entry points share a single behaviour:
  * ``TradingAccountViewSet.perform_create``   (``POST /api/trading/accounts/``)
  * ``AddAccountWithMt5LoginView``             (``POST /api/trading/accounts/add-with-mt5-login/`` — UI)

Creation records CUSTOMER INTENT ONLY (``mt5_instance=None``; broker-login deferred to provisioning); a
customer with no shared MT5 instance is never ``409``'d (the Customer Zero regression); idempotent; the
per-user cap is enforced; provisioning is enqueued; staff keep the unchanged admin create.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from billing.beta import grant_beta_entitlement
from billing.models import UserSubscriptionState
from trading.models import TradingAccount
from trading.views import TradingAccountViewSet
from trading.views_account_add import AddAccountWithMt5LoginView

U = get_user_model()
VS = TradingAccount.ValidationStatus


def _add_via_endpoint(user, number="900001", **extra):
    body = {"name": "A", "account_number": number, "password": "pw",
            "broker_name": "DemoBroker", "is_demo": True, **extra}
    req = APIRequestFactory().post("/api/trading/accounts/add-with-mt5-login/", body, format="json")
    force_authenticate(req, user=user)
    return AddAccountWithMt5LoginView.as_view()(req)


def _add_via_viewset(user, number="900002", **extra):
    body = {"name": "A", "account_number": number, "password": "pw",
            "broker_name": "DemoBroker", "is_demo": True, **extra}
    req = APIRequestFactory().post("/api/trading/accounts/", body, format="json")
    force_authenticate(req, user=user)
    return TradingAccountViewSet.as_view({"post": "create"})(req)


class CanonicalCreateContractTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="cz", email="cz@x.invalid", password="x")
        grant_beta_entitlement(self.user)

    def test_no_shared_instance_never_409(self):
        # THE Customer Zero regression: a customer with no leased MT5 instance used to hit
        # "No MT5 instance/windows user assigned" (409) at add-with-mt5-login. A STANDARD-plan customer
        # (exactly CZ's shape) now records intent instead — the canonical contract requires no instance.
        std = U.objects.create_user(username="std", email="std@x.invalid", password="x")
        UserSubscriptionState.objects.update_or_create(
            user=std, defaults={"current_plan": UserSubscriptionState.Plan.STANDARD,
                                "plan_status": UserSubscriptionState.PlanStatus.ACTIVE, "viewer_mode": False})
        resp = _add_via_endpoint(std, number="900001")
        self.assertEqual(resp.status_code, 201, resp.data)
        acct = TradingAccount.objects.get(user=std, account_number="900001")
        self.assertIsNone(acct.mt5_instance)

    def test_both_entry_points_produce_the_same_shape(self):
        r1 = _add_via_endpoint(self.user, number="900010")
        r2 = _add_via_viewset(self.user, number="900011")
        self.assertIn(r1.status_code, (200, 201), r1.data)
        self.assertIn(r2.status_code, (200, 201), r2.data)
        for n in ("900010", "900011"):
            a = TradingAccount.objects.get(user=self.user, account_number=n)
            self.assertIsNone(a.mt5_instance)      # dedicated-runtime model — never the shared instance
            self.assertFalse(a.is_active)          # inactive until its runtime is ready
            self.assertEqual(a.validation_status, VS.NEVER)   # broker-login validation deferred

    def test_provisioning_enqueued_for_both_paths(self):
        with mock.patch("trading.views._maybe_enqueue_beta_provisioning") as m:
            _add_via_endpoint(self.user, number="900020")
            _add_via_viewset(self.user, number="900021")
        self.assertEqual(m.call_count, 2)          # owned-runtime provisioning triggered on both paths

    def test_idempotent_resubmit_across_endpoint(self):
        r1 = _add_via_endpoint(self.user, number="900030")
        r2 = _add_via_endpoint(self.user, number="900030")
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertEqual(r2.status_code, 200)      # existing account returned, never a duplicate
        self.assertFalse(r2.data["created"])
        self.assertEqual(
            TradingAccount.objects.filter(user=self.user, account_number="900030").count(), 1)

    def test_cap_enforced_for_user_without_plan(self):
        viewer = U.objects.create_user(username="v0", email="v0@x.invalid", password="x")  # no plan
        resp = _add_via_endpoint(viewer, number="900040")
        self.assertEqual(resp.status_code, 400)    # max_trading_accounts=0 → "limit reached"
        self.assertFalse(TradingAccount.objects.filter(account_number="900040").exists())

    def test_staff_create_takes_admin_path_without_provisioning(self):
        staff = U.objects.create_user(username="s0", email="s0@x.invalid", password="x", is_staff=True)
        with mock.patch("trading.views._maybe_enqueue_beta_provisioning") as m:
            resp = _add_via_endpoint(staff, number="900050")
        self.assertIn(resp.status_code, (200, 201), resp.data)
        m.assert_not_called()                      # staff keep the unchanged admin create (no provisioning)
