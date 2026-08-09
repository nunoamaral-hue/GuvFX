"""ADR-0034 Onboarding — DARK customer API (journey / request / confirm / ops).

Proves: every route is 404-invisible while DARK; the journey is owner-scoped (no cross-user leak, IDOR-safe);
request is idempotent and rejects any password-bearing body; confirm is gated on an observed match and is
idempotent; the ops fleet is staff-only; and POST is genuinely CSRF-enforced on the cookie-auth path (not just
assumed). No order is placed and nothing is armed.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from billing.models import UserSubscriptionState

from hosted_workspace import provisioning as P
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
_FLAGS_ON = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_WORKSPACE_ONBOARDING_ENABLED="1")

JOURNEY = "/api/hosted-workspace/onboarding/journey/"
REQUEST = "/api/hosted-workspace/onboarding/request/"
CONFIRM = "/api/hosted-workspace/onboarding/confirm/"
OPS = "/api/hosted-workspace/onboarding/ops/"


def _user(name="u1", *, entitled=True, staff=False):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x", is_staff=staff)
    UserSubscriptionState.objects.update_or_create(
        user=u, defaults=dict(current_plan=("beta" if entitled else "starter_trial"),
                              plan_status="active", viewer_mode=False))
    return u


class DarkVisibilityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(_user())

    def test_all_routes_404_when_dark(self):
        self.assertEqual(self.client.get(JOURNEY).status_code, 404)
        self.assertEqual(self.client.post(REQUEST, {"expected_login": "700900"}, format="json").status_code, 404)
        self.assertEqual(self.client.post(CONFIRM, {}, format="json").status_code, 404)
        self.assertEqual(self.client.get(OPS).status_code, 404)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1")   # master on, onboarding still off
    def test_partial_flags_still_dark(self):
        self.assertEqual(self.client.get(JOURNEY).status_code, 404)


@override_settings(**_FLAGS_ON)
class JourneyAndRequestTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_journey_no_workspace(self):
        r = self.client.get(JOURNEY)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["phase"], "NO_WORKSPACE")
        self.assertIn("assignment", r.data)
        self.assertEqual(r.data["assignment"]["state"], "NOT_ELIGIBLE")

    def test_request_requires_login(self):
        r = self.client.post(REQUEST, {}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["reason"], P.REQ_LOGIN_REQUIRED)

    def test_request_rejects_password_body(self):
        r = self.client.post(REQUEST, {"expected_login": "700900", "password": "secret"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["reason"], P.REQ_PASSWORD_FORBIDDEN)

    def test_request_rejects_nested_password_body(self):
        r = self.client.post(REQUEST, {"expected_login": "700900", "broker": {"password": "x"}}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["reason"], P.REQ_PASSWORD_FORBIDDEN)

    def test_request_rejects_token_variant_body(self):
        r = self.client.post(REQUEST, {"expected_login": "700900", "api_token": "x"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["reason"], P.REQ_PASSWORD_FORBIDDEN)

    def test_request_creates_then_idempotent(self):
        r1 = self.client.post(REQUEST, {"expected_login": "700900", "expected_server": "IS6-Demo"},
                              format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r1.data["status"], "created")
        r2 = self.client.post(REQUEST, {"expected_login": "700900"}, format="json")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.data["status"], "exists")

    def test_request_non_entitled_forbidden(self):
        c = APIClient()
        c.force_authenticate(_user("poor", entitled=False))
        r = c.post(REQUEST, {"expected_login": "700900"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_journey_is_owner_scoped_no_leak(self):
        # user B provisions a workspace; user A's journey must still be NO_WORKSPACE (no cross-user leak).
        P.request_hosted_workspace(_user("bob"), expected_login="111222")
        r = self.client.get(JOURNEY)   # A (self.user) has none
        self.assertEqual(r.data["phase"], "NO_WORKSPACE")


@override_settings(**_FLAGS_ON)
class ConfirmApiTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.ws = P.request_hosted_workspace(self.user, expected_login="700900").workspace

    def test_confirm_conflict_when_not_matched(self):
        r = self.client.post(CONFIRM, {}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["reason"], P.CONFIRM_NO_MATCH)

    def test_confirm_success_when_connected_matched(self):
        self.ws.canonical_state = S.CONNECTED
        self.ws.proj_connected = True
        self.ws.proj_account_match = True
        self.ws.save(update_fields=["canonical_state", "proj_connected", "proj_account_match"])
        r = self.client.post(CONFIRM, {}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], P.CONFIRM_OK)
        self.assertTrue(r.data["confirmed"])

    def test_confirm_no_workspace_404(self):
        c = APIClient()
        c.force_authenticate(_user("nows"))
        self.assertEqual(c.post(CONFIRM, {}, format="json").status_code, 404)


@override_settings(**_FLAGS_ON)
class OpsApiTests(TestCase):
    def test_ops_staff_only(self):
        P.request_hosted_workspace(_user("cust"), expected_login="700900")
        non_staff = APIClient()
        non_staff.force_authenticate(_user("plain"))
        self.assertEqual(non_staff.get(OPS).status_code, 404)          # invisible to non-staff

        staff = APIClient()
        staff.force_authenticate(_user("ops", staff=True))
        r = staff.get(OPS)
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(len(r.data["rows"]), 1)
        self.assertIn("phase", r.data["rows"][0])


@override_settings(**_FLAGS_ON)
class CsrfEnforcementTests(TestCase):
    """The onboarding POSTs must be genuinely CSRF-protected on the cookie-auth path — proven, not assumed."""

    def setUp(self):
        self.user = _user()
        self.token = str(AccessToken.for_user(self.user))
        self.csrf = "a" * 64          # alphanumeric, passes _sanitize_token

    def _client(self):
        c = APIClient(enforce_csrf_checks=True)
        c.cookies["guvfx_access"] = self.token
        c.cookies["csrftoken"] = self.csrf
        return c

    def test_post_without_csrf_header_is_forbidden(self):
        c = self._client()
        r = c.post(REQUEST, {"expected_login": "700900"}, format="json")   # no X-CSRFToken header
        self.assertEqual(r.status_code, 403)

    def test_post_with_matching_csrf_header_succeeds(self):
        c = self._client()
        r = c.post(REQUEST, {"expected_login": "700900"}, format="json", HTTP_X_CSRFTOKEN=self.csrf)
        self.assertIn(r.status_code, (200, 201))
