"""hosted_workspace.diagnostics_views — WHY-not-CONNECTED read API (DARK, read-only).

Proves: 404-invisible while DARK; STAFF sees the fleet + metrics (and a selected account); a NON-STAFF caller
sees ONLY their own workspace and a cross-user selector is 404 (owner-scoped / IDOR-safe); a non-owner learns
nothing; the payload is redacted; and a GET mutates nothing.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from execution.readiness import PERSISTENT_WORKSPACE
from trading.models import BrokerServer, TradingAccount

from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
URL = "/api/hosted-workspace/diagnostics/"
_ON = dict(HOSTED_PERSISTENT_MT5_ENABLED="1")
_SECRET_LOGIN = "700900123"


def _user(name, *, staff=False):
    return U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x", is_staff=staff)


def _ws(user, *, login, state=S.WAITING_FOR_LOGIN, **kw):
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo", defaults={"environment": BrokerServer.DEMO})
    acct = TradingAccount.objects.create(
        user=user, name="B", broker_name="B", account_number=login, is_demo=True,
        broker_server=srv, readiness_provider=PERSISTENT_WORKSPACE, is_active=False)
    return HostedMt5Workspace.objects.create(trading_account=acct, canonical_state=state, **kw), acct


class DarkVisibilityTests(TestCase):
    def test_404_when_dark(self):
        c = APIClient(); c.force_authenticate(_user("d1", staff=True))
        self.assertEqual(c.get(URL).status_code, 404)


@override_settings(**_ON)
class ScopingTests(TestCase):
    def setUp(self):
        self.owner = _user("owner")
        self.other = _user("other")
        self.staff = _user("staff", staff=True)
        self.ws, self.acct = _ws(self.owner, login=_SECRET_LOGIN,
                                  delivery_state=HostedMt5Workspace.DeliveryState.AUTHORIZED, proj_connected=False)

    def test_staff_sees_fleet_and_metrics(self):
        c = APIClient(); c.force_authenticate(self.staff)
        r = c.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertIn("rows", r.data)
        self.assertIn("metrics", r.data)
        self.assertEqual(r.data["rows"][0]["reason"], "no_runtime_populated")

    def test_staff_selector_account(self):
        c = APIClient(); c.force_authenticate(self.staff)
        r = c.get(URL, {"account_id": self.acct.id})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["diagnosis"]["account_id"], self.acct.id)

    def test_staff_unknown_selector_404(self):
        c = APIClient(); c.force_authenticate(self.staff)
        self.assertEqual(c.get(URL, {"account_id": 999999}).status_code, 404)

    def test_owner_sees_own_diagnosis(self):
        c = APIClient(); c.force_authenticate(self.owner)
        r = c.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["diagnosis"]["account_id"], self.acct.id)
        self.assertNotIn("rows", r.data)        # owner never gets the fleet
        self.assertNotIn("metrics", r.data)     # owner never gets fleet metrics

    def test_non_owner_sees_nothing(self):
        c = APIClient(); c.force_authenticate(self.other)
        self.assertEqual(c.get(URL).status_code, 404)

    def test_non_owner_cross_user_selector_is_404(self):
        # IDOR: a non-staff user naming the OWNER's account id learns nothing.
        c = APIClient(); c.force_authenticate(self.other)
        self.assertEqual(c.get(URL, {"account_id": self.acct.id}).status_code, 404)

    def test_owner_cross_selector_to_foreign_account_404(self):
        # The owner naming someone else's account id also 404s (scoped to their own).
        foreign, facct = _ws(self.other, login="111222")
        c = APIClient(); c.force_authenticate(self.owner)
        self.assertEqual(c.get(URL, {"account_id": facct.id}).status_code, 404)

    def test_payload_redacted(self):
        c = APIClient(); c.force_authenticate(self.staff)
        blob = str(c.get(URL).data)
        for forbidden in (_SECRET_LOGIN, "guvfx_u_", "C:\\GuvFX", "password"):
            self.assertNotIn(forbidden, blob)

    def test_get_mutates_nothing(self):
        before = (self.ws.delivery_state, self.ws.proj_connected, self.ws.observation_version)
        c = APIClient(); c.force_authenticate(self.staff)
        c.get(URL)
        self.ws.refresh_from_db()
        self.assertEqual((self.ws.delivery_state, self.ws.proj_connected, self.ws.observation_version), before)
