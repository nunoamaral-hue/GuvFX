"""WP1B/WP2 (ADR-0029) — broker-validation execution-gate tests.

Covers the central decision service (every validation/eligibility state, flag-OFF transparency),
enforcement (`require_execution_gate` raises + audits), and the `create_open_trade_job` service funnel
(gate ON + unvalidated ⇒ ExecutionGateRefused, zero jobs). The auto-execution promotion funnel is covered
in tests_e3_demo_promotion.DemoGateInheritanceTests.
"""
import os
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import AuditEvent
from execution import broker_gate as bg
from execution.broker_gate import ExecutionGateRefused, GateDecision, evaluate_execution_gate
from execution.models import ExecutionJob
from trading.models import TradingAccount

U = get_user_model()
_VS = TradingAccount.ValidationStatus
_ON = {"BROKER_CONNECTIVITY_EXECUTION_GATE": "1"}


def _acct(user, *, number="1302575", active=True, validated=_VS.VALIDATED, cred="x", disconnected=None):
    a = TradingAccount.objects.create(
        user=user, name="A", account_number=number, broker_name="IS6Technologies",
        is_demo=True, is_active=active, validation_status=validated, password_enc=cred)
    if disconnected is not None:
        a.disconnected_at = disconnected
        a.save(update_fields=["disconnected_at"])
    return a


class DecisionMatrixTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="g1", email="g1@x.invalid", password="x")

    def test_flag_off_is_transparent(self):
        # even a NEVER-validated, inactive, credential-less account is allowed while the flag is OFF
        acct = _acct(self.user, active=False, validated=_VS.NEVER, cred="")
        d = evaluate_execution_gate(acct)
        self.assertTrue(d.allowed)
        self.assertEqual(d.reason_code, bg.GATE_DISABLED)

    def test_validated_eligible_allows(self):
        with mock.patch.dict(os.environ, _ON):
            d = evaluate_execution_gate(_acct(self.user))
        self.assertEqual((d.allowed, d.reason_code), (True, bg.GATE_OK))

    def test_none_account_refused(self):
        with mock.patch.dict(os.environ, _ON):
            self.assertEqual(evaluate_execution_gate(None).reason_code, bg.R_ACCOUNT_MISSING)

    def test_inactive_refused(self):
        with mock.patch.dict(os.environ, _ON):
            d = evaluate_execution_gate(_acct(self.user, active=False))
        self.assertEqual((d.allowed, d.reason_code), (False, bg.R_ACCOUNT_INACTIVE))

    def test_disconnected_refused(self):
        from django.utils import timezone
        with mock.patch.dict(os.environ, _ON):
            d = evaluate_execution_gate(_acct(self.user, disconnected=timezone.now()))
        self.assertEqual((d.allowed, d.reason_code), (False, bg.R_ACCOUNT_DISCONNECTED))

    def test_missing_credential_refused(self):
        with mock.patch.dict(os.environ, _ON):
            d = evaluate_execution_gate(_acct(self.user, cred=""))
        self.assertEqual((d.allowed, d.reason_code), (False, bg.R_CREDENTIAL_MISSING))

    def test_validation_states_refused(self):
        cases = {
            _VS.NEVER: bg.R_NOT_VALIDATED_NEVER,
            _VS.CONNECTION_FAILED: bg.R_NOT_VALIDATED_CONNECTION_FAILED,
            _VS.TECHNICAL_ERROR: bg.R_NOT_VALIDATED_TECHNICAL_ERROR,
        }
        with mock.patch.dict(os.environ, _ON):
            for i, (state, reason) in enumerate(cases.items()):
                d = evaluate_execution_gate(_acct(self.user, number=f"acc{i}", validated=state))
                self.assertEqual((d.allowed, d.reason_code), (False, reason), state)

    def test_unknown_validation_status_is_fail_closed(self):
        # A validation_status outside the enum (data corruption / future value) must fail closed, not allow.
        acct = _acct(self.user)
        TradingAccount.objects.filter(pk=acct.pk).update(validation_status="WEIRD_UNEXPECTED")
        acct.refresh_from_db()
        with mock.patch.dict(os.environ, _ON):
            d = evaluate_execution_gate(acct)
        self.assertEqual((d.allowed, d.reason_code), (False, bg.R_VALIDATION_STATE_UNKNOWN))

    def test_first_disqualifying_condition_wins(self):
        # An inactive AND unvalidated account reports the FIRST check (inactive), per the documented order.
        acct = _acct(self.user, active=False, validated=_VS.NEVER)
        with mock.patch.dict(os.environ, _ON):
            self.assertEqual(evaluate_execution_gate(acct).reason_code, bg.R_ACCOUNT_INACTIVE)


class EnforcementTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="g2", email="g2@x.invalid", password="x")

    def test_require_allows_validated(self):
        with mock.patch.dict(os.environ, _ON):
            d = bg.require_execution_gate(_acct(self.user), trigger="t")
        self.assertTrue(d.allowed)

    def test_require_raises_and_audits_on_refusal(self):
        acct = _acct(self.user, validated=_VS.NEVER)
        with mock.patch.dict(os.environ, _ON):
            with self.assertRaises(ExecutionGateRefused) as cm:
                bg.require_execution_gate(acct, trigger="create_open_trade_job")
        self.assertEqual(cm.exception.reason_code, bg.R_NOT_VALIDATED_NEVER)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="EXECUTION_GATE_REFUSED", entity_id=str(acct.id)).exists())

    def test_require_flag_off_never_raises(self):
        acct = _acct(self.user, active=False, validated=_VS.NEVER, cred="")
        d = bg.require_execution_gate(acct)  # flag OFF
        self.assertTrue(d.allowed)

    def test_audit_failure_does_not_weaken_refusal(self):
        # Audit is fail-open: if log_event throws, the refusal must STILL be raised (safety over telemetry).
        acct = _acct(self.user, validated=_VS.NEVER)
        with mock.patch.dict(os.environ, _ON), \
             mock.patch("core.audit.log_event", side_effect=RuntimeError("audit down")):
            with self.assertRaises(ExecutionGateRefused):
                bg.require_execution_gate(acct, trigger="t")


class OpenTradeFunnelTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="g3", email="g3@x.invalid", password="x")

    def _params(self, acct):
        from execution.services import OpenTradeParams
        return OpenTradeParams(
            account=acct, strategy=None, assignment=None, created_by=self.user,
            symbol="EURUSD", direction="BUY", timeframe="M1", entry_type="MARKET",
            entry_price=None, sl_price=Decimal("1.0"), tp_price=None)

    def test_gate_on_unvalidated_refuses_and_creates_no_job(self):
        from execution.services import create_open_trade_job
        acct = _acct(self.user, validated=_VS.NEVER)
        with mock.patch.dict(os.environ, _ON), \
             mock.patch("execution.services.require_entitlement", return_value=None):
            with self.assertRaises(ExecutionGateRefused):
                create_open_trade_job(self._params(acct))
        self.assertEqual(ExecutionJob.objects.count(), 0)  # no order job created
