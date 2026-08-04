"""WP1B/WP2 (ADR-0029) — credential-replacement invalidation tests.

Replacing a broker credential must atomically invalidate prior eligibility: validation_status returns to
NEVER, validated_at is cleared, and (when the WP3 health engine is on) health resets to UNKNOWN with no
resume — so the old credential's validation can no longer authorise execution. Covers the atomic
invalidation, health reset, flag-OFF isolation, history preservation, rollback, the dispatch-gate
consequence, and "no resume until a fresh successful validation".
"""
import os
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import AuditEvent
from execution import broker_gate as bg
from reliability import broker_health as bh
from reliability.models import BrokerAccountHealth
from trading import broker_connectivity as bc
from trading.crypto import encrypt_password
from trading.models import BrokerAccountValidationAttempt, TradingAccount

U = get_user_model()
_VS = TradingAccount.ValidationStatus
_ENV = {"GUVFX_FERNET_KEY": Fernet.generate_key().decode(), "DJANGO_SECRET_KEY": "unit-test-secret"}
_HEALTH_ON = dict(_ENV, BROKER_CONNECTIVITY_HEALTH_ENABLED="true")
_GATE_ON = dict(_ENV, BROKER_CONNECTIVITY_EXECUTION_GATE="1")


def _validated_acct(user, number="13025750"):
    with mock.patch.dict(os.environ, _ENV):
        return TradingAccount.objects.create(
            user=user, name="CZ", account_number=number, broker_name="IS6Technologies",
            is_demo=True, is_active=True, validation_status=_VS.VALIDATED,
            validated_at=timezone.now(), password_enc=encrypt_password("oldpw"))


class CredentialInvalidationTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="c1", email="c1@x.invalid", password="x")
        self.acct = _validated_acct(self.user)

    def test_replace_invalidates_validation_status(self):
        with mock.patch.dict(os.environ, _ENV):
            res = bc.replace_credentials(self.acct, "newpw")
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.validation_status, _VS.NEVER)
        self.assertIsNone(self.acct.validated_at)
        self.assertTrue(res["validation_invalidated"])

    def test_replace_reencrypts_credential(self):
        old = self.acct.password_enc
        with mock.patch.dict(os.environ, _ENV):
            bc.replace_credentials(self.acct, "newpw")
        self.acct.refresh_from_db()
        self.assertNotEqual(self.acct.password_enc, old)
        self.assertTrue(self.acct.password_enc)  # still has a credential (rotated, not destroyed)

    def test_replace_resets_health_to_unknown_when_enabled(self):
        with mock.patch.dict(os.environ, _HEALTH_ON):
            BrokerAccountHealth.objects.create(account=self.acct, state="HEALTHY",
                                               consecutive_successes=3, resume_eligible=True,
                                               state_version=2, last_success_at=timezone.now())
            bc.replace_credentials(self.acct, "newpw")
        h = BrokerAccountHealth.objects.get(account=self.acct)
        self.assertEqual(h.state, "UNKNOWN")
        self.assertFalse(h.resume_eligible)
        self.assertEqual(h.reason_code, bh.REASON_CREDENTIAL_REPLACED)
        self.assertEqual((h.consecutive_successes, h.consecutive_failures), (0, 0))
        self.assertIsNone(h.last_success_at)
        self.assertEqual(h.state_version, 3)  # bumped

    def test_health_untouched_when_health_flag_off(self):
        with mock.patch.dict(os.environ, _HEALTH_ON):
            BrokerAccountHealth.objects.create(account=self.acct, state="HEALTHY", state_version=5)
        with mock.patch.dict(os.environ, _ENV):  # health flag OFF during replace
            bc.replace_credentials(self.acct, "newpw")
        h = BrokerAccountHealth.objects.get(account=self.acct)
        self.assertEqual(h.state, "HEALTHY")  # untouched — health engine DARK
        self.assertEqual(h.state_version, 5)

    def test_validation_history_preserved(self):
        BrokerAccountValidationAttempt.objects.create(
            account=self.acct, trigger="add", status="HEALTHY", reason_code="demo_ok")
        before = BrokerAccountValidationAttempt.objects.filter(account=self.acct).count()
        with mock.patch.dict(os.environ, _ENV):
            bc.replace_credentials(self.acct, "newpw")
        self.assertEqual(BrokerAccountValidationAttempt.objects.filter(account=self.acct).count(), before)

    def test_rollback_leaves_account_unchanged(self):
        # A failure inside the atomic block must not partially invalidate: neither the credential nor the
        # validation status changes.
        old_enc, old_status = self.acct.password_enc, self.acct.validation_status
        with mock.patch.dict(os.environ, _ENV), \
             mock.patch("trading.crypto.encrypt_password", side_effect=RuntimeError("kms down")):
            with self.assertRaises(RuntimeError):
                bc.replace_credentials(self.acct, "newpw")
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.password_enc, old_enc)
        self.assertEqual(self.acct.validation_status, old_status)  # still VALIDATED
        self.assertIsNotNone(self.acct.validated_at)

    def test_health_invalidation_error_does_not_abort_rotation(self):
        # Design: a health-engine error must NOT abort the credential rotation/validation invalidation
        # (the gate already fails closed on validation_status=NEVER). The rotation + invalidation still
        # commit; the error is audited.
        with mock.patch.dict(os.environ, _HEALTH_ON), \
             mock.patch("reliability.broker_health.invalidate_for_credential_replacement",
                        side_effect=RuntimeError("health down")):
            bc.replace_credentials(self.acct, "newpw")
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.validation_status, _VS.NEVER)  # invalidation still applied
        self.assertTrue(AuditEvent.objects.filter(
            event_type="BROKER_HEALTH_INVALIDATION_ERROR", entity_id=str(self.acct.pk)).exists())

    def test_dispatch_gate_refuses_after_replace(self):
        with mock.patch.dict(os.environ, _ENV):
            bc.replace_credentials(self.acct, "newpw")
        self.acct.refresh_from_db()
        with mock.patch.dict(os.environ, _GATE_ON):
            d = bg.evaluate_dispatch_gate(self.acct)
        self.assertEqual((d.allowed, d.reason_code), (False, bg.SR_VALIDATION_REQUIRED))

    def test_fresh_validation_converges_health_and_allows_dispatch(self):
        # M1 fix: a freshly-VALIDATED account must converge to HEALTHY on the customer validation flow
        # (not only on the inert scheduler), so the final-dispatch gate allows it immediately.
        class _Outcome:
            def as_dict(self):
                return {"status": "HEALTHY", "reason": "demo_ok", "is_demo": True,
                        "server": "IS6Technologies-Demo", "login_masked": "***", "correlation_id": "c"}

        class _Validator:
            def validate(self, a):
                return _Outcome()

        acct = _validated_acct(self.user, number="99001")
        env = dict(_HEALTH_ON, BROKER_CONNECTIVITY_EXECUTION_GATE="1")
        with mock.patch.dict(os.environ, env):
            bc.run_broker_validation(acct, trigger="add", validator=_Validator())
            c = bh.get_contract(acct)
            self.assertEqual(c["state"], "HEALTHY")   # converged on the validation flow
            d = bg.evaluate_dispatch_gate(acct)
        self.assertTrue(d.allowed)

    def test_no_resume_until_fresh_validation(self):
        with mock.patch.dict(os.environ, _HEALTH_ON):
            BrokerAccountHealth.objects.create(account=self.acct, state="HEALTHY", resume_eligible=True)
            bc.replace_credentials(self.acct, "newpw")
            h = BrokerAccountHealth.objects.get(account=self.acct)
            self.assertEqual((h.state, h.resume_eligible), ("UNKNOWN", False))
            # A fresh successful validation folds UNKNOWN → HEALTHY (validated), still NOT auto-resume.
            BrokerAccountValidationAttempt.objects.create(
                account=self.acct, trigger="retry", status="HEALTHY", reason_code="demo_ok")
            c = bh.record_validation_outcome(self.acct)
        self.assertEqual(c["state"], "HEALTHY")
        self.assertFalse(c["resume_eligible"])  # first validation after replace is not a resume
