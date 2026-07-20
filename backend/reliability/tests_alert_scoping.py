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
