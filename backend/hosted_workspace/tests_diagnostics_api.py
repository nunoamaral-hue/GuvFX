"""hosted_workspace.diagnostics_views — WHY-not-CONNECTED read API (DARK, read-only, STAFF-ONLY).

Discriminating tests for the two independent gates + the safety properties. Each asserts a behaviour that
FAILS if its guard is removed:
  (a) master/observation flags ON but the DEDICATED diagnostics flag OFF ⇒ 404 (the dedicated gate is required);
  (b) diagnostics flag ON + non-staff ⇒ 404 (staff-only posture; no member exposure);
  (c) diagnostics flag ON + staff ⇒ 200 (fleet + metrics);
  (d) cross-owner / non-staff owner ⇒ 404 (no member surface at all);
  (e) a GET mutates nothing;
  (f) the payload leaks no login / username / runtime-path / secret.
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
# The DEDICATED diagnostics flag is the operative gate. Master flag ON is included to prove the dedicated gate
# is still required on top of it (production already runs the master/observation flags on).
_DIAG_ON = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_CONNECTION_DIAGNOSTICS_ENABLED="1")
_SECRET_LOGIN = "700900123"


def _user(name, *, staff=False):
    return U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x", is_staff=staff)


def _ws(user, *, login, state=S.WAITING_FOR_LOGIN, **kw):
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo", defaults={"environment": BrokerServer.DEMO})
    acct = TradingAccount.objects.create(
        user=user, name="B", broker_name="B", account_number=login, is_demo=True,
        broker_server=srv, readiness_provider=PERSISTENT_WORKSPACE, is_active=False)
    return HostedMt5Workspace.objects.create(trading_account=acct, canonical_state=state, **kw), acct


class GateTests(TestCase):
    def test_fully_dark_404(self):
        c = APIClient(); c.force_authenticate(_user("g0", staff=True))
        self.assertEqual(c.get(URL).status_code, 404)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1")   # master ON, dedicated diagnostics flag OFF
    def test_master_on_but_diagnostics_flag_off_is_404(self):
        # (a) The dedicated flag is an INDEPENDENT additional gate: with it OFF the route 404s even though the
        # master flag (and, in prod, the observation flags) are ON. Removing the dedicated-flag check breaks this.
        c = APIClient(); c.force_authenticate(_user("g1", staff=True))
        self.assertEqual(c.get(URL).status_code, 404)


@override_settings(**_DIAG_ON)
class StaffOnlyTests(TestCase):
    def setUp(self):
        self.owner = _user("owner")
        self.other = _user("other")
        self.staff = _user("staff", staff=True)
        self.ws, self.acct = _ws(self.owner, login=_SECRET_LOGIN,
                                  delivery_state=HostedMt5Workspace.DeliveryState.AUTHORIZED, proj_connected=False)

    def test_staff_sees_fleet_and_metrics(self):
        # (c)
        c = APIClient(); c.force_authenticate(self.staff)
        r = c.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertIn("rows", r.data)
        self.assertIn("metrics", r.data)
        self.assertEqual(r.data["rows"][0]["reason"], "no_runtime_populated")
        # bounded-metrics shape
        self.assertIn("scanned", r.data["metrics"])
        self.assertEqual(r.data["metrics"]["workspace_total"], 1)

    def test_staff_selector_account(self):
        c = APIClient(); c.force_authenticate(self.staff)
        r = c.get(URL, {"account_id": self.acct.id})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["diagnosis"]["account_id"], self.acct.id)

    def test_staff_unknown_selector_404(self):
        c = APIClient(); c.force_authenticate(self.staff)
        self.assertEqual(c.get(URL, {"account_id": 999999}).status_code, 404)

    def test_staff_malformed_uuid_is_404_not_500(self):
        # A non-UUID workspace_uuid must 404 (matches nothing), never raise an unhandled 500.
        c = APIClient(); c.force_authenticate(self.staff)
        self.assertEqual(c.get(URL, {"workspace_uuid": "not-a-uuid"}).status_code, 404)

    def test_non_staff_owner_is_404(self):
        # (b)/(d) The workspace OWNER, being non-staff, still gets 404 — detailed diagnostics are not a member
        # surface. Removing the staff check would turn this into a 200 (information disclosure).
        c = APIClient(); c.force_authenticate(self.owner)
        self.assertEqual(c.get(URL).status_code, 404)
        self.assertEqual(c.get(URL, {"account_id": self.acct.id}).status_code, 404)

    def test_non_staff_non_owner_is_404(self):
        # (d) cross-owner: a different non-staff user naming the owner's account id learns nothing.
        c = APIClient(); c.force_authenticate(self.other)
        self.assertEqual(c.get(URL).status_code, 404)
        self.assertEqual(c.get(URL, {"account_id": self.acct.id}).status_code, 404)

    def test_payload_redacted(self):
        # (f)
        c = APIClient(); c.force_authenticate(self.staff)
        blob = str(c.get(URL).data)
        for forbidden in (_SECRET_LOGIN, "guvfx_u_", "C:\\GuvFX", "password"):
            self.assertNotIn(forbidden, blob)

    def test_get_mutates_nothing(self):
        # (e)
        before = (self.ws.delivery_state, self.ws.proj_connected, self.ws.observation_version)
        c = APIClient(); c.force_authenticate(self.staff)
        c.get(URL)
        self.ws.refresh_from_db()
        self.assertEqual((self.ws.delivery_state, self.ws.proj_connected, self.ws.observation_version), before)
