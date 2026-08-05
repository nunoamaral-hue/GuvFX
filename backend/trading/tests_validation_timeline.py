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

    def test_validation_busy_reaches_agent_but_not_broker(self):
        # Phase-4 WS-B (S3): validation_busy is the AGENT's single-flight lock — the agent WAS reached, but the
        # MT5 probe never launched and the broker was never contacted. Explicit mapping (not the default).
        _attempt(self.acct, reason="validation_busy", status="UNAVAILABLE", corr="c-busy")
        tl = build_timeline("c-busy")
        self.assertEqual(_state(tl, "agent_received"), "ok")       # agent reached (its lock refused)
        self.assertEqual(_state(tl, "mt5_launched"), "failed")     # probe never launched
        self.assertEqual(_state(tl, "broker_login"), "not_reached")
        self.assertEqual(_state(tl, "broker_response"), "not_reached")
        self.assertEqual(_state(tl, "persisted"), "ok")
        self.assertIn("busy", tl.customer_summary.lower())
        self.assertNotIn("broker server is temporarily unavailable", tl.customer_summary.lower())

    def test_browser_response_label_claims_only_return_not_render(self):
        # Phase-4 WS-B (S7): the backend can only evidence that it RETURNED the response, not that the browser
        # rendered it. The customer label must not over-claim ("Showed you the result").
        _attempt(self.acct, reason="demo_ok", status="HEALTHY", corr="c-br", is_demo=True)
        tl = build_timeline("c-br")
        br = next(s for s in tl.stages if s["key"] == "browser_response")
        self.assertNotIn("showed you", br["customer_label"].lower())
        self.assertIn("returned the result", br["customer_label"].lower())

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

    def test_broker_server_missing_fails_at_api_received(self):
        # review WS-P3 finding 1: broker_server_missing fails BEFORE credential prep (broker-server
        # resolution), so the failing stage is api_received itself — never blame credential_decrypted,
        # which never ran. ok_idx = -1 → the op summary must say the request could not be started.
        _attempt(self.acct, reason="broker_server_missing", status="UNAVAILABLE", corr="c-nosrv")
        tl = build_timeline("c-nosrv")
        self.assertEqual(_state(tl, "api_received"), "failed")
        self.assertEqual(_state(tl, "credential_decrypted"), "not_reached")
        self.assertEqual(_state(tl, "broker_login"), "not_reached")
        self.assertEqual(_state(tl, "persisted"), "ok")           # result still recorded + shown
        self.assertEqual(_state(tl, "browser_response"), "ok")
        self.assertIn("could not be started", tl.operator_summary.lower())

    def _credential_audit_at(self, when):
        """Create a CREDENTIAL_ACCESSED audit for the account with a specific created_at. The model is
        append-only (immutable save + auto_now_add), so the timestamp is backdated via raw SQL."""
        from django.db import connection
        from core.models import AuditEvent
        ev = AuditEvent.objects.create(
            event_type="CREDENTIAL_ACCESSED", entity_type="TradingAccount", entity_id=str(self.acct.id))
        with connection.cursor() as cur:
            cur.execute(f"UPDATE {AuditEvent._meta.db_table} SET created_at=%s WHERE id=%s", [when, str(ev.id)])
        return ev

    def test_stale_credential_audit_is_ignored_no_duration(self):
        # review WS-P3 finding 2: a CREDENTIAL_ACCESSED audit older than the ~300s validation window belongs
        # to a PRIOR attempt; using it as the start marker would report a wildly-stale duration. It must be
        # ignored (no start marker → no duration), NOT fall back to the prior attempt's decrypt time.
        from datetime import timedelta
        att = _attempt(self.acct, reason="broker_server_missing", status="UNAVAILABLE", corr="c-stale")
        self._credential_audit_at(att.created_at - timedelta(seconds=400))   # older than the 300s floor
        tl = build_timeline("c-stale")
        self.assertEqual(tl.started_at, "")                        # stale audit ignored
        self.assertIsNone(tl.duration_ms)                          # → no spurious duration

    def test_recent_credential_audit_yields_duration(self):
        # positive control for the 300s bound: an audit WITHIN the window IS used as the start marker.
        from datetime import timedelta
        att = _attempt(self.acct, reason="invalid_password", status="NEEDS_ATTENTION", corr="c-dur")
        self._credential_audit_at(att.created_at - timedelta(seconds=5))
        tl = build_timeline("c-dur")
        self.assertNotEqual(tl.started_at, "")
        self.assertIsNotNone(tl.duration_ms)
        self.assertGreaterEqual(tl.duration_ms, 4000)

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

    def test_search_by_attempt_id(self):
        att = self.acct.validation_attempts.first()
        with mock.patch.dict(os.environ, _ON):
            resp = self._get(self.staff, f"?attempt_id={att.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["correlation_id"], "c-ep")
        self.assertEqual(resp.data["attempt_id"], att.id)

    def test_search_by_account_id_uses_latest(self):
        _attempt(self.acct, reason="demo_ok", status="HEALTHY", corr="c-newer", is_demo=True)
        with mock.patch.dict(os.environ, _ON):
            resp = self._get(self.staff, f"?account_id={self.acct.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["correlation_id"], "c-newer")   # latest attempt for the account

    def test_no_search_key_is_400(self):
        with mock.patch.dict(os.environ, _ON):
            resp = self._get(self.staff, "")
        self.assertEqual(resp.status_code, 400)
