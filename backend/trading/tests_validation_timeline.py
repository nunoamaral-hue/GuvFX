"""WS-D (2026-08-05) — support-grade validation TIMELINE: builder derivation + staff-only endpoint."""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from trading.models import BrokerAccountValidationAttempt, TradingAccount
from trading.validation_timeline import build_timeline
from trading.views import ValidationTimelineView

U = get_user_model()
_ON = {"BROKER_CONNECTIVITY_ENABLED": "1", "DJANGO_SECRET_KEY": "unit-test-secret"}


def _acct(user):
    return TradingAccount.objects.create(
        user=user, name="CZ", account_number="13025870", broker_name="IS6Technologies",
        is_demo=True, is_active=True, validation_status=TradingAccount.ValidationStatus.NEVER)


def _attempt(acct, *, reason, status, corr, is_demo=None):
    return BrokerAccountValidationAttempt.objects.create(
        account=acct, trigger="test", status=status, reason_code=reason, retryable=True,
        is_demo=is_demo, server="IS6Technologies-Demo", login_masked="***870", correlation_id=corr)


def _state(tl, key):
    return next(s["state"] for s in tl.stages if s["key"] == key)


class TimelineBuilderTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="t", email="t@x.invalid", password="x")
        self.acct = _acct(self.user)

    def test_unknown_correlation_is_not_found(self):
        tl = build_timeline("nope-does-not-exist")
        self.assertFalse(tl.found)
        self.assertEqual(tl.stages, [])

    def test_success_marks_every_stage_ok(self):
        _attempt(self.acct, reason="demo_ok", status="HEALTHY", corr="c-ok", is_demo=True)
        tl = build_timeline("c-ok")
        self.assertTrue(tl.found)
        self.assertTrue(all(s["state"] == "ok" for s in tl.stages))
        self.assertIn("verified", tl.customer_summary.lower())
        self.assertIn("succeeded", tl.operator_summary.lower())

    def test_ipc_failure_stops_before_broker_and_persists_result(self):
        _attempt(self.acct, reason="validation_ipc_unavailable", status="UNAVAILABLE", corr="c-ipc")
        tl = build_timeline("c-ipc")
        self.assertEqual(_state(tl, "api_received"), "ok")
        self.assertEqual(_state(tl, "credential_decrypted"), "ok")
        self.assertEqual(_state(tl, "agent_received"), "ok")
        self.assertEqual(_state(tl, "mt5_launched"), "failed")     # local IPC never came up
        self.assertEqual(_state(tl, "broker_login"), "not_reached")
        self.assertEqual(_state(tl, "broker_response"), "not_reached")
        self.assertEqual(_state(tl, "persisted"), "ok")            # the failure result IS still persisted
        self.assertEqual(_state(tl, "browser_response"), "ok")
        # customer summary: NOT a broker-outage claim, no internal leak
        self.assertNotIn("broker server is temporarily unavailable", tl.customer_summary.lower())
        for bad in ("ipc", "session 0", "mt5", "-10004", "10004"):
            self.assertNotIn(bad, tl.customer_summary.lower())
        self.assertIn("try again later", tl.customer_summary.lower())

    def test_broker_rejection_reaches_broker_stage(self):
        _attempt(self.acct, reason="invalid_password", status="NEEDS_ATTENTION", corr="c-pw")
        tl = build_timeline("c-pw")
        self.assertEqual(_state(tl, "mt5_launched"), "ok")
        self.assertEqual(_state(tl, "broker_login"), "ok")         # the broker WAS reached
        self.assertEqual(_state(tl, "broker_response"), "failed")  # ...and rejected the credential
        self.assertIn("not accepted", tl.customer_summary.lower())

    def test_credential_unsealable_is_agent_origin_not_backend_signing(self):
        # review WS-P2 finding 1: credential_unsealable is emitted ONLY by the agent (after it received the
        # signed request) → the agent WAS reached; never render request_signed as the failing stage.
        _attempt(self.acct, reason="credential_unsealable", status="UNAVAILABLE", corr="c-uns")
        tl = build_timeline("c-uns")
        self.assertEqual(_state(tl, "request_signed"), "ok")
        self.assertEqual(_state(tl, "agent_received"), "ok")
        self.assertEqual(_state(tl, "mt5_launched"), "failed")

    def test_login_timeout_is_conservative_and_not_broker_reached(self):
        # review WS-P2 finding 2: a black-box transport timeout can't confirm the agent was reached, and the
        # summary must not claim the broker responded while the stages say broker not_reached.
        _attempt(self.acct, reason="login_timeout", status="UNAVAILABLE", corr="c-to")
        tl = build_timeline("c-to")
        self.assertEqual(_state(tl, "request_signed"), "ok")
        self.assertEqual(_state(tl, "agent_received"), "failed")   # conservative — like bridge_unavailable
        self.assertEqual(_state(tl, "broker_login"), "not_reached")
        self.assertEqual(_state(tl, "broker_response"), "not_reached")
        self.assertNotIn("broker didn't respond", tl.customer_summary.lower())   # no broker-reached claim

    def test_post_probe_failure_marks_no_pipeline_stage_failed(self):
        # review WS-P2 finding 3: diagnostic_capture_failed is a POST-login fault — never render "Contacted
        # your broker" as the failing stage.
        _attempt(self.acct, reason="diagnostic_capture_failed", status="UNAVAILABLE", corr="c-diag")
        tl = build_timeline("c-diag")
        self.assertFalse(any(s["state"] == "failed" for s in tl.stages))
        self.assertEqual(_state(tl, "broker_login"), "ok")
        self.assertIn("not healthy", tl.operator_summary.lower())

    def test_output_is_secret_safe(self):
        _attempt(self.acct, reason="validation_ipc_unavailable", status="UNAVAILABLE", corr="c-sec")
        tl = build_timeline("c-sec")
        blob = str(tl).lower()
        self.assertIn("***870", tl.login_masked)                   # only the MASKED login
        for bad in ("password", "13025870", "c:\\", "accounts.dat", "pid", "session_id"):
            self.assertNotIn(bad, blob)


class TimelineEndpointTests(TestCase):
    def setUp(self):
        self.staff = U.objects.create_user(username="s", email="s@x.invalid", password="x", is_staff=True)
        self.user = U.objects.create_user(username="u", email="u@x.invalid", password="x")
        self.acct = _acct(self.staff)
        _attempt(self.acct, reason="validation_ipc_unavailable", status="UNAVAILABLE", corr="c-ep")

    def _get(self, user, qs=""):
        req = APIRequestFactory().get(f"/api/trading/validation-timeline/{qs}")
        force_authenticate(req, user=user)
        return ValidationTimelineView.as_view()(req)

    def test_non_staff_forbidden(self):
        with mock.patch.dict(os.environ, _ON):
            resp = self._get(self.user, "?correlation_id=c-ep")
        self.assertEqual(resp.status_code, 403)

    def test_dark_when_flag_off_is_404(self):
        with mock.patch.dict(os.environ, _ON, clear=False):
            os.environ.pop("BROKER_CONNECTIVITY_ENABLED", None)
            resp = self._get(self.staff, "?correlation_id=c-ep")
        self.assertEqual(resp.status_code, 404)

    def test_missing_correlation_id_is_400(self):
        with mock.patch.dict(os.environ, _ON):
            resp = self._get(self.staff)
        self.assertEqual(resp.status_code, 400)

    def test_staff_gets_timeline(self):
        with mock.patch.dict(os.environ, _ON):
            resp = self._get(self.staff, "?correlation_id=c-ep")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["found"])
        self.assertEqual(resp.data["correlation_id"], "c-ep")
        self.assertTrue(any(s["state"] == "failed" for s in resp.data["stages"]))
