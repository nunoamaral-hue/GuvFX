"""WP1A (ADR-0028) — Broker Connectivity customer-journey backend tests.

Covers the feature-flag gate (default OFF ⇒ dark), the validation-history model + service, the
DRF actions (test/retry/status/history/replace/disconnect), user-scoping, secret-safety, fail-closed
behaviour, and the disconnect-is-a-TOMBSTONE invariant (never a row delete). The certified validator
(ADR-0027) is injected as a fake — no MT5/agent/keyring is required.
"""
import os
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import AuditEvent
from trading import broker_connectivity as bc
from trading.crypto import encrypt_password
from trading.models import BrokerAccountValidationAttempt, TradingAccount
from trading.views import TradingAccountViewSet

U = get_user_model()
_ENV = {"GUVFX_FERNET_KEY": Fernet.generate_key().decode(), "DJANGO_SECRET_KEY": "unit-test-secret"}
_ON = dict(_ENV, BROKER_CONNECTIVITY_ENABLED="1")


class _FakeOutcome:
    def __init__(self, d):
        self._d = d

    def as_dict(self):
        return dict(self._d)


class _FakeValidator:
    def __init__(self, outcome=None, raises=False):
        self._o = outcome
        self._raises = raises

    def validate(self, account):
        if self._raises:
            raise RuntimeError("boom")
        return _FakeOutcome(self._o)


_HEALTHY = {"status": "HEALTHY", "reason": "demo_ok", "retryable": False,
            "server": "IS6Technologies-Demo", "login_masked": "***344", "is_demo": True,
            "correlation_id": "corr-ok"}
_UNAVAIL = {"status": "UNAVAILABLE", "reason": "login_timeout", "retryable": True,
            "server": "IS6Technologies-Demo", "login_masked": "***344", "is_demo": None,
            "correlation_id": "corr-un"}
_REJECT = {"status": "NEEDS_ATTENTION", "reason": "invalid_password", "retryable": False,
           "server": "IS6Technologies-Demo", "login_masked": "***344", "is_demo": None,
           "correlation_id": "corr-rej"}


def _acct(user, number="13025750", pw="brokerpw"):
    with mock.patch.dict(os.environ, _ENV):
        return TradingAccount.objects.create(
            user=user, name="CZ", account_number=number, broker_name="IS6Technologies",
            is_demo=True, is_active=True, validation_status=TradingAccount.ValidationStatus.NEVER,
            password_enc=(encrypt_password(pw) if pw else ""))


def _call(method, action, user, pk, data=None):
    factory = APIRequestFactory()
    req = getattr(factory, method)("/x", data or {}, format="json")
    force_authenticate(req, user=user)
    view = TradingAccountViewSet.as_view({method: action})
    return view(req, pk=pk)


class FlagGateTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="u1", email="u1@x.invalid", password="x")
        self.acct = _acct(self.user)

    def test_flag_off_is_dark_404(self):
        with mock.patch.dict(os.environ, _ENV, clear=False):
            os.environ.pop("BROKER_CONNECTIVITY_ENABLED", None)
            resp = _call("post", "bc_test_connection", self.user, self.acct.pk)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(BrokerAccountValidationAttempt.objects.count(), 0)

    def test_flag_on_reaches_handler(self):
        with mock.patch.dict(os.environ, _ON), \
             mock.patch.object(bc, "_make_validator", return_value=_FakeValidator(_HEALTHY)):
            resp = _call("post", "bc_test_connection", self.user, self.acct.pk)
        self.assertEqual(resp.status_code, 200)


class ValidationFlowTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="u2", email="u2@x.invalid", password="x")
        self.acct = _acct(self.user)

    def _run(self, action, outcome):
        with mock.patch.dict(os.environ, _ON), \
             mock.patch.object(bc, "_make_validator", return_value=_FakeValidator(outcome)):
            return _call("post", action, self.user, self.acct.pk)

    def test_healthy_records_attempt_and_persists_validated(self):
        resp = self._run("bc_test_connection", _HEALTHY)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "HEALTHY")
        self.assertEqual(resp.data["trigger"], "test")
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.VALIDATED)
        self.assertIsNotNone(self.acct.validated_at)
        self.assertEqual(BrokerAccountValidationAttempt.objects.filter(account=self.acct).count(), 1)

    def test_unavailable_maps_technical_error_and_keeps_validated_at(self):
        self.acct.validated_at = None
        self.acct.save(update_fields=["validated_at"])
        resp = self._run("bc_test_connection", _UNAVAIL)
        self.assertEqual(resp.data["status"], "UNAVAILABLE")
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.TECHNICAL_ERROR)
        self.assertIsNone(self.acct.validated_at)  # a failure never stamps validated_at

    def test_reject_maps_connection_failed(self):
        self._run("bc_test_connection", _REJECT)
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.CONNECTION_FAILED)

    def test_retry_records_retry_trigger(self):
        resp = self._run("bc_retry_validation", _HEALTHY)
        self.assertEqual(resp.data["trigger"], "retry")

    def test_validator_exception_is_fail_closed_unavailable(self):
        with mock.patch.dict(os.environ, _ON), \
             mock.patch.object(bc, "_make_validator", return_value=_FakeValidator(raises=True)):
            resp = _call("post", "bc_test_connection", self.user, self.acct.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "UNAVAILABLE")
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.TECHNICAL_ERROR)

    def test_secret_safe_output(self):
        resp = self._run("bc_test_connection", _HEALTHY)
        body = str(resp.data).lower()
        self.assertNotIn("password", body)
        self.assertNotIn("password_enc", body)
        self.assertNotIn(self.acct.password_enc.lower(), body)

    def test_over_long_server_is_truncated_not_500(self):
        # Regression (adversarial review): a server value longer than the column must be truncated, never
        # raise at insert (which would return 500 and record no attempt — defeating fail-closed).
        resp = self._run("bc_test_connection", dict(_HEALTHY, server="S" * 200))
        self.assertEqual(resp.status_code, 200)
        att = BrokerAccountValidationAttempt.objects.filter(account=self.acct).first()
        self.assertIsNotNone(att)
        self.assertLessEqual(len(att.server), 160)


class StatusHistoryTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="u3", email="u3@x.invalid", password="x")
        self.acct = _acct(self.user)

    def test_status_and_history(self):
        with mock.patch.dict(os.environ, _ON), \
             mock.patch.object(bc, "_make_validator", return_value=_FakeValidator(_HEALTHY)):
            _call("post", "bc_test_connection", self.user, self.acct.pk)
            _call("post", "bc_retry_validation", self.user, self.acct.pk)
            status_resp = _call("get", "bc_status", self.user, self.acct.pk)
            hist_resp = _call("get", "bc_validation_history", self.user, self.acct.pk)
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.data["validation_status"], "VALIDATED")
        self.assertIsNotNone(status_resp.data["latest_attempt"])
        self.assertEqual(len(hist_resp.data), 2)
        # newest first
        self.assertEqual(hist_resp.data[0]["trigger"], "retry")
        # review WS-P3 finding 3: correlation_id is an operator diagnostic and must NOT appear on the
        # customer-facing attempt projection (status.latest_attempt or the history rows).
        self.assertNotIn("correlation_id", status_resp.data["latest_attempt"])
        self.assertNotIn("correlation_id", hist_resp.data[0])


class ReplaceCredentialsTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="u4", email="u4@x.invalid", password="x")
        self.acct = _acct(self.user, pw="oldpw")

    def test_replace_reencrypts_and_audits_rotation(self):
        before = self.acct.password_enc
        with mock.patch.dict(os.environ, _ON):
            resp = _call("post", "bc_replace_credentials", self.user, self.acct.pk, {"password": "newpw"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["replaced"])
        self.acct.refresh_from_db()
        self.assertNotEqual(self.acct.password_enc, before)
        self.assertNotEqual(self.acct.password_enc, "")
        self.assertTrue(AuditEvent.objects.filter(event_type="CREDENTIAL_ROTATED", entity_id=str(self.acct.id)).exists())

    def test_replace_requires_password(self):
        with mock.patch.dict(os.environ, _ON):
            resp = _call("post", "bc_replace_credentials", self.user, self.acct.pk, {})
        self.assertEqual(resp.status_code, 400)


class DisconnectTombstoneTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="u5", email="u5@x.invalid", password="x")
        self.acct = _acct(self.user, pw="pw")

    def test_disconnect_is_tombstone_not_delete(self):
        with mock.patch.dict(os.environ, _ON):
            resp = _call("post", "bc_disconnect", self.user, self.acct.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["disconnected"])
        self.assertFalse(resp.data["row_deleted"])
        self.assertTrue(resp.data["credential_destroyed"])
        # row RETAINED (never deleted)
        self.assertTrue(TradingAccount.objects.filter(pk=self.acct.pk).exists())
        self.acct.refresh_from_db()
        self.assertFalse(self.acct.is_active)
        self.assertIsNotNone(self.acct.disconnected_at)
        self.assertEqual(self.acct.password_enc, "")
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.NEVER)
        self.assertTrue(AuditEvent.objects.filter(event_type="CREDENTIAL_DESTROYED", entity_id=str(self.acct.id)).exists())


class UserScopingTests(TestCase):
    def setUp(self):
        self.owner = U.objects.create_user(username="own", email="own@x.invalid", password="x")
        self.other = U.objects.create_user(username="oth", email="oth@x.invalid", password="x")
        self.acct = _acct(self.owner)

    def test_other_user_cannot_reach_account(self):
        with mock.patch.dict(os.environ, _ON), \
             mock.patch.object(bc, "_make_validator", return_value=_FakeValidator(_HEALTHY)):
            resp = _call("post", "bc_test_connection", self.other, self.acct.pk)
        self.assertIn(resp.status_code, (403, 404))
        self.assertEqual(BrokerAccountValidationAttempt.objects.count(), 0)


class ServiceUnitTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="u6", email="u6@x.invalid", password="x")
        self.acct = _acct(self.user)

    def test_run_broker_validation_direct(self):
        attempt = bc.run_broker_validation(self.acct, trigger="add", validator=_FakeValidator(_HEALTHY))
        self.assertEqual(attempt.status, "HEALTHY")
        self.assertEqual(attempt.trigger, "add")
        self.assertTrue(attempt.is_demo)

    def test_disconnect_idempotent(self):
        bc.disconnect_account(self.acct)
        # second call is safe (nothing to destroy)
        result = bc.disconnect_account(self.acct)
        self.assertTrue(result["disconnected"])
        self.assertFalse(result["credential_destroyed"])  # already cleared


_BUSY = {"status": "UNAVAILABLE", "reason": "validation_busy", "retryable": True,
         "server": "IS6Technologies-Demo", "login_masked": "***344", "is_demo": None, "correlation_id": "corr-busy"}
_IPC = {"status": "UNAVAILABLE", "reason": "validation_ipc_unavailable", "retryable": True,
        "server": "IS6Technologies-Demo", "login_masked": "***344", "is_demo": None, "correlation_id": "corr-ipc"}


class StatusPreservationTests(TestCase):
    """WS-C (2026-08-05): a NON-authoritative outcome (busy / host-IPC-unavailable / could-not-verify) must not
    downgrade a durable prior success. This is the exact #12 defect — a validation_busy retry flipped a
    previously VALIDATED account to TECHNICAL_ERROR."""

    def setUp(self):
        self.user = U.objects.create_user(username="wsc", email="wsc@x.invalid", password="x")
        self.acct = _acct(self.user)

    def _validate(self, outcome):
        return bc.run_broker_validation(self.acct, trigger="retry", validator=_FakeValidator(outcome))

    def _make_validated(self):
        self._validate(_HEALTHY)
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.VALIDATED)
        self.assertIsNotNone(self.acct.validated_at)
        return self.acct.validated_at

    def test_validation_busy_does_not_downgrade_validated(self):
        prior = self._make_validated()
        attempt = self._validate(_BUSY)
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.VALIDATED)  # preserved
        self.assertEqual(self.acct.validated_at, prior)                                            # unchanged
        self.assertEqual(attempt.reason_code, "validation_busy")                                   # still recorded
        self.assertEqual(attempt.status, "UNAVAILABLE")

    def test_ipc_unavailable_does_not_downgrade_validated(self):
        prior = self._make_validated()
        self._validate(_IPC)
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.VALIDATED)
        self.assertEqual(self.acct.validated_at, prior)

    def test_credential_rejection_still_downgrades_validated(self):
        self._make_validated()
        self._validate(_REJECT)  # invalid_password — an AUTHORITATIVE verdict on the credential
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.CONNECTION_FAILED)

    def test_never_account_ipc_unavailable_is_technical_error(self):
        # No prior success to preserve → an infra failure records TECHNICAL_ERROR (unchanged behaviour).
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.NEVER)
        self._validate(_IPC)
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.validation_status, TradingAccount.ValidationStatus.TECHNICAL_ERROR)
        self.assertIsNone(self.acct.validated_at)

    def test_latest_attempt_recorded_even_when_status_preserved(self):
        self._make_validated()
        self._validate(_BUSY)
        attempts = list(self.acct.validation_attempts.all())   # newest-first
        self.assertEqual(attempts[0].reason_code, "validation_busy")
        self.assertEqual(attempts[-1].reason_code, "demo_ok")

    def test_non_authoritative_set_covers_every_unavailable_taxonomy_reason(self):
        # Drift guard: every platform (UNAVAILABLE) reason the agent can return must be non-authoritative, else a
        # transient platform failure could silently downgrade a durable VALIDATED state.
        from terminal_provisioning.broker_login_validation import UNAVAILABLE, _TAXONOMY
        unavailable = {r for r, (st, _rt) in _TAXONOMY.items() if st == UNAVAILABLE}
        missing = unavailable - bc._NON_AUTHORITATIVE_REASONS
        self.assertEqual(missing, set(), f"UNAVAILABLE taxonomy reasons not marked non-authoritative: {missing}")
        # AND the SAFETY-CRITICAL reverse: the non-authoritative set must contain NOTHING outside the UNAVAILABLE
        # bucket. An AUTHORITATIVE reason (invalid_password/invalid_login/account_disabled/server_not_found/
        # classification_mismatch/credential_missing/broker_server_missing) sneaking in here would PRESERVE
        # VALIDATED on a genuine credential/broker REJECTION — leaving the account execution-eligible with a
        # just-rejected credential (evaluate_execution_gate treats VALIDATED as GATE_OK). Enforce EXACT equality.
        extra = bc._NON_AUTHORITATIVE_REASONS - unavailable
        self.assertEqual(extra, set(), f"non-authoritative reasons outside the UNAVAILABLE bucket: {extra}")

    def test_validation_ipc_unavailable_registered_in_taxonomy(self):
        from terminal_provisioning.broker_login_validation import UNAVAILABLE, _TAXONOMY
        self.assertIn("validation_ipc_unavailable", _TAXONOMY)
        self.assertEqual(_TAXONOMY["validation_ipc_unavailable"], (UNAVAILABLE, True))
