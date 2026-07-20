"""GFX-BETA-PHASE0 (C15) — reliability alert/recommendation lists must be user-scoped for non-staff.
A beta user must see ONLY their own trading-account alerts, never GLOBAL/operator or other users'."""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, force_authenticate

from reliability.models import AlertEvent, RecoveryRecommendation
from reliability.views import AlertListView, RecommendationListView
from trading.models import TradingAccount

U = get_user_model()


class ReliabilityScopingTests(TestCase):
    def setUp(self):
        self.u1 = U.objects.create_user(username="u1", email="u1@x.invalid", password="x")
        self.u2 = U.objects.create_user(username="u2", email="u2@x.invalid", password="x")
        self.staff = U.objects.create_user(
            username="s", email="s@x.invalid", password="x", is_staff=True)
        self.a1 = TradingAccount.objects.create(
            user=self.u1, name="A1", account_number="A1", is_demo=True)
        self.a2 = TradingAccount.objects.create(
            user=self.u2, name="A2", account_number="A2", is_demo=True)
        AlertEvent.objects.create(component="X", title="u1", dedup_key="k1",
                                  status="OPEN", trading_account=self.a1)
        AlertEvent.objects.create(component="X", title="u2", dedup_key="k2",
                                  status="OPEN", trading_account=self.a2)
        AlertEvent.objects.create(component="X", title="global", dedup_key="kg",
                                  status="OPEN", trading_account=None)  # operator/global
        RecoveryRecommendation.objects.create(component="X", recommended_action="r1",
                                              dedup_key="r1", trading_account=self.a1)
        RecoveryRecommendation.objects.create(component="X", recommended_action="rg",
                                              dedup_key="rg", trading_account=None)
        self.factory = APIRequestFactory()

    def _alerts(self, user):
        req = self.factory.get("/api/reliability/alerts/")
        force_authenticate(req, user=user)
        return AlertListView.as_view()(req).data["alerts"]

    def _recs(self, user):
        req = self.factory.get("/api/reliability/recommendations/")
        force_authenticate(req, user=user)
        return RecommendationListView.as_view()(req).data["recommendations"]

    def test_non_staff_sees_only_own_alerts(self):
        self.assertEqual(len(self._alerts(self.u1)), 1)   # only k1 (not k2, not global)
        self.assertEqual(len(self._alerts(self.u2)), 1)   # only k2

    def test_staff_sees_all_alerts(self):
        self.assertEqual(len(self._alerts(self.staff)), 3)  # k1 + k2 + global

    def test_non_staff_sees_only_own_recommendations(self):
        self.assertEqual(len(self._recs(self.u1)), 1)   # only r1 (not global rg)

    def test_staff_sees_all_recommendations(self):
        self.assertEqual(len(self._recs(self.staff)), 2)


class TradingHealthScopingTests(TestCase):
    """GFX-BETA-PHASE0 (C14 + IDOR) — trading-health is per-tenant for non-staff; GLOBAL/other-account
    is staff-only; the 4 global operational endpoints are admin-only."""

    def setUp(self):
        from reliability.views import (TradingHealthView, HealthMatrixView,
                                       RecoveryAttemptListView, RecoveryStatusView, CircuitResetView)
        self.views = dict(th=TradingHealthView, hm=HealthMatrixView, ra=RecoveryAttemptListView,
                          rs=RecoveryStatusView, cr=CircuitResetView)
        self.u1 = U.objects.create_user(username="u1", email="u1@x.invalid", password="x")
        self.u2 = U.objects.create_user(username="u2", email="u2@x.invalid", password="x")
        self.staff = U.objects.create_user(
            username="s", email="s@x.invalid", password="x", is_staff=True)
        self.a1 = TradingAccount.objects.create(
            user=self.u1, name="A1", account_number="A1", is_demo=True)
        self.a2 = TradingAccount.objects.create(
            user=self.u2, name="A2", account_number="A2", is_demo=True)
        self.factory = APIRequestFactory()

    def _th(self, user, qs=""):
        req = self.factory.get("/api/reliability/trading-health/" + qs)
        force_authenticate(req, user=user)
        return self.views["th"].as_view()(req)

    def test_non_staff_cannot_read_other_users_account(self):
        r = self._th(self.u1, "?account_id=%d" % self.a2.id)
        self.assertEqual(r.status_code, 404)

    def test_non_staff_can_read_own_account(self):
        r = self._th(self.u1, "?account_id=%d" % self.a1.id)
        self.assertEqual(r.status_code, 200)  # allowed (UNKNOWN if no snapshot, but not 404)

    def test_non_staff_no_account_id_is_not_global(self):
        r = self._th(self.u1)
        self.assertIn("account_id", " ".join(r.data.get("reasons", [])).lower())

    def test_staff_no_account_id_uses_global(self):
        r = self._th(self.staff)
        # staff GLOBAL path — not the "provide your own account_id" refusal
        self.assertNotIn("provide your own account_id", " ".join(r.data.get("reasons", [])).lower())

    def _hit(self, key, user, method="get"):
        req = getattr(self.factory, method)("/x")
        force_authenticate(req, user=user)
        return self.views[key].as_view()(req)

    def test_global_operational_endpoints_are_admin_only(self):
        for key, method in (("hm", "get"), ("ra", "get"), ("rs", "get"), ("cr", "post")):
            self.assertEqual(self._hit(key, self.u1, method).status_code, 403,
                             "%s must be admin-only for non-staff" % key)

    def test_global_operational_endpoints_allow_staff(self):
        for key, method in (("hm", "get"), ("ra", "get"), ("rs", "get")):
            self.assertEqual(self._hit(key, self.staff, method).status_code, 200,
                             "%s must allow staff" % key)
