"""Phase 9 — GET /api/auth/me/ exposes the per-user `broker_accounts_ux` capability (wiring test)."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from trading.account_entitlement import grant_broker_ux
from users.views import MeView

U = get_user_model()


def _user(name):
    return U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")


class MeBrokerUx(TestCase):
    def _me(self, user):
        req = APIRequestFactory().get(reverse("auth-me"))
        force_authenticate(req, user=user)
        return MeView.as_view()(req)

    def test_me_reports_false_by_default(self):
        r = self._me(_user("me1"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("broker_accounts_ux", r.data)
        self.assertFalse(r.data["broker_accounts_ux"])   # empty allowlist ⇒ legacy for every user

    def test_me_reports_true_only_for_the_granted_user(self):
        granted, other = _user("me_grant"), _user("me_other")
        grant_broker_ux(granted)
        self.assertTrue(self._me(granted).data["broker_accounts_ux"])   # granted user
        self.assertFalse(self._me(other).data["broker_accounts_ux"])    # never leaks to another user
