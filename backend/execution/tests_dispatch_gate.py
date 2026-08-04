"""WP1B/WP2 (ADR-0029) — FINAL-DISPATCH gate tests.

The creation gate proves eligibility when a job is created; the final-dispatch gate re-evaluates FRESH
immediately before the live order_send, never trusting the enqueue-time snapshot. Covers: flag-OFF
transparency, fresh eligibility refusal, health-contract consumption (both flags), fail-closed on a
health read error, per-job resolution + durable audit, and the TOCTOU race (eligible at enqueue →
ineligible at dispatch).
"""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import AuditEvent
from execution import broker_gate as bg
from execution.models import ExecutionJob
from reliability.models import BrokerAccountHealth
from trading.models import TradingAccount

U = get_user_model()
_VS = TradingAccount.ValidationStatus
_ON = {"BROKER_CONNECTIVITY_EXECUTION_GATE": "1"}
_ON_BOTH = {"BROKER_CONNECTIVITY_EXECUTION_GATE": "1", "BROKER_CONNECTIVITY_HEALTH_ENABLED": "true"}


def _acct(user, *, number="1302575", active=True, validated=_VS.VALIDATED, cred="x", disconnected=None):
    a = TradingAccount.objects.create(
        user=user, name="A", account_number=number, broker_name="IS6Technologies",
        is_demo=True, is_active=active, validation_status=validated, password_enc=cred)
    if disconnected is not None:
        a.disconnected_at = disconnected
        a.save(update_fields=["disconnected_at"])
    return a


def _health(account, state):
    return BrokerAccountHealth.objects.create(account=account, state=state)


def _job(account, job_type="PLACE_ORDER"):
    # Create with the gate OFF so the creation gate never refuses; the dispatch gate is what we test.
    with mock.patch.dict(os.environ, {"BROKER_CONNECTIVITY_EXECUTION_GATE": "0"}):
        return ExecutionJob.objects.create(account=account, job_type=job_type, payload={}, status="PENDING")


class DispatchDecisionTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="d1", email="d1@x.invalid", password="x")

    def test_flag_off_is_transparent(self):
        acct = _acct(self.user, active=False, validated=_VS.NEVER, cred="")
        d = bg.evaluate_dispatch_gate(acct)
        self.assertTrue(d.allowed)
        self.assertEqual(d.reason_code, bg.GATE_DISABLED)

    def test_validated_no_health_allows(self):
        with mock.patch.dict(os.environ, _ON):
            d = bg.evaluate_dispatch_gate(_acct(self.user))
        self.assertEqual((d.allowed, d.reason_code), (True, bg.DISPATCH_OK))

    def test_eligibility_refusals_use_shared_vocabulary(self):
        cases = [
            (dict(active=False), bg.SR_ACCOUNT_INACTIVE),
            (dict(cred=""), bg.SR_CREDENTIAL_MISSING),
            (dict(validated=_VS.NEVER), bg.SR_VALIDATION_REQUIRED),
            (dict(validated=_VS.CONNECTION_FAILED), bg.SR_VALIDATION_FAILED),
            (dict(validated=_VS.TECHNICAL_ERROR), bg.SR_VALIDATION_UNAVAILABLE),
        ]
        for i, (kw, code) in enumerate(cases):
            acct = _acct(self.user, number=f"100{i}", **kw)
            with mock.patch.dict(os.environ, _ON):
                d = bg.evaluate_dispatch_gate(acct)
            self.assertEqual((d.allowed, d.reason_code), (False, code), kw)

    def test_disconnected_before_dispatch_refused(self):
        from django.utils import timezone
        acct = _acct(self.user, disconnected=timezone.now())
        with mock.patch.dict(os.environ, _ON):
            d = bg.evaluate_dispatch_gate(acct)
        self.assertEqual((d.allowed, d.reason_code), (False, bg.SR_ACCOUNT_DISCONNECTED))

    def test_health_states_map_to_shared_codes(self):
        cases = [
            ("HEALTHY", True, bg.DISPATCH_OK),
            ("DEGRADED", False, bg.SR_HEALTH_DEGRADED),
            ("STALE", False, bg.SR_HEALTH_STALE),
            ("DISCONNECTED", False, bg.SR_HEALTH_DISCONNECTED),
            ("TOMBSTONED", False, bg.SR_ACCOUNT_TOMBSTONED),
            ("UNKNOWN", False, bg.SR_VALIDATION_REQUIRED),
        ]
        for i, (state, allowed, code) in enumerate(cases):
            acct = _acct(self.user, number=f"200{i}")
            _health(acct, state)
            with mock.patch.dict(os.environ, _ON_BOTH):
                d = bg.evaluate_dispatch_gate(acct)
            self.assertEqual((d.allowed, d.reason_code), (allowed, code), state)

    def test_health_not_consulted_when_health_flag_off(self):
        acct = _acct(self.user)
        _health(acct, "DEGRADED")
        with mock.patch.dict(os.environ, _ON):  # exec gate ON, health flag OFF
            d = bg.evaluate_dispatch_gate(acct)
        self.assertEqual((d.allowed, d.reason_code), (True, bg.DISPATCH_OK))  # health ignored

    def test_no_health_row_adds_no_constraint(self):
        acct = _acct(self.user)  # VALIDATED, no health row
        with mock.patch.dict(os.environ, _ON_BOTH):
            d = bg.evaluate_dispatch_gate(acct)
        self.assertTrue(d.allowed)

    def test_health_read_error_fails_closed(self):
        acct = _acct(self.user)
        _health(acct, "HEALTHY")
        with mock.patch.dict(os.environ, _ON_BOTH), \
             mock.patch("execution.broker_gate._get_health_contract", side_effect=RuntimeError("boom")):
            d = bg.evaluate_dispatch_gate(acct)
        self.assertEqual((d.allowed, d.reason_code), (False, bg.SR_HEALTH_STATE_CHANGED))


class JobDispatchTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="d2", email="d2@x.invalid", password="x")

    def test_flag_off_transparent_no_query(self):
        d = bg.evaluate_job_dispatch(999999)  # non-existent id, but flag OFF short-circuits before any read
        self.assertEqual((d.allowed, d.reason_code), (True, bg.GATE_DISABLED))

    def test_missing_job_refused(self):
        with mock.patch.dict(os.environ, _ON):
            d = bg.evaluate_job_dispatch(999999)
        self.assertEqual((d.allowed, d.reason_code), (False, bg.SR_ACCOUNT_MISSING))

    def test_toctou_eligible_at_enqueue_ineligible_at_dispatch(self):
        acct = _acct(self.user)
        job = _job(acct)  # created while eligible
        # Account degrades after enqueue.
        _health(acct, "DEGRADED")
        with mock.patch.dict(os.environ, _ON_BOTH):
            d = bg.evaluate_job_dispatch(job.pk)
        self.assertEqual((d.allowed, d.reason_code), (False, bg.SR_HEALTH_DEGRADED))

    def test_refusal_is_audited(self):
        acct = _acct(self.user, validated=_VS.NEVER)
        job = _job(acct)
        with mock.patch.dict(os.environ, _ON):
            bg.evaluate_job_dispatch(job.pk)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="EXECUTION_DISPATCH_REFUSED", entity_id=str(acct.pk)).exists())

    def test_replay_same_job_same_decision(self):
        acct = _acct(self.user)
        job = _job(acct)
        with mock.patch.dict(os.environ, _ON):
            d1 = bg.evaluate_job_dispatch(job.pk)
            d2 = bg.evaluate_job_dispatch(job.pk)
        self.assertEqual((d1.allowed, d1.reason_code), (d2.allowed, d2.reason_code))

    def test_require_dispatch_gate_raises_on_refusal(self):
        acct = _acct(self.user, validated=_VS.NEVER)
        with mock.patch.dict(os.environ, _ON):
            with self.assertRaises(bg.ExecutionGateRefused) as ctx:
                bg.require_dispatch_gate(acct)
        self.assertEqual(ctx.exception.reason_code, bg.SR_VALIDATION_REQUIRED)
