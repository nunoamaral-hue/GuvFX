"""WP5.2 — broker-connectivity → operational-event PROJECTION wiring tests (ADR-0032 §WP5.2).

Projections are registered via ``transaction.on_commit`` at the durable emission point, so tests wrap the
triggering call in ``captureOnCommitCallbacks(execute=True)`` to run the post-commit callbacks — which
also proves the rollback-discard (no phantom) path, since a rolled-back block never captures its hook.
"""
from __future__ import annotations

import os
from unittest import mock

from django.db import transaction
from django.test import TestCase, TransactionTestCase

from users.models import User
from trading.models import BrokerAccountValidationAttempt, TradingAccount

from . import broker_projection as bp
from .models import OperationalEvent

_ON = {"OPERATIONS_EVENTS_ENABLED": "1"}
_ON_HEALTH = {"OPERATIONS_EVENTS_ENABLED": "1", "BROKER_CONNECTIVITY_HEALTH_ENABLED": "1"}
_OFF = {"OPERATIONS_EVENTS_ENABLED": "0"}


def _account(name="p"):
    u = User.objects.create_user(username=name, email=f"{name}@example.com", password="pw-12345")
    return TradingAccount.objects.create(
        user=u, name="A", account_number="100200", is_demo=True, broker_name="DemoBroker")


class _Rec:
    """Minimal stand-in for a BrokerRuntimePause row (project_pause_audit reads only id + reason_code)."""
    def __init__(self, rec_id=7, reason_code="broker_health_degraded"):
        self.id = rec_id
        self.reason_code = reason_code


class _StubValidator:
    def __init__(self, d):
        self._d = d

    def validate(self, account):
        payload = self._d

        class _R:
            def as_dict(self):
                return dict(payload)
        return _R()


class ProjectionUnitTests(TestCase):
    def setUp(self):
        self.a = _account()

    def _emit(self, fn, *a, **kw):
        with mock.patch.dict(os.environ, _ON):
            with self.captureOnCommitCallbacks(execute=True):
                fn(*a, **kw)

    # ── VALIDATION ──
    def test_validation_success_info_customer_visible(self):
        self._emit(bp.project_validation, self.a, attempt_id=11, status="HEALTHY",
                   reason_code="demo_ok", is_demo=True, trigger="add", correlation_id="c1")
        e = OperationalEvent.objects.get()
        self.assertEqual((e.category, e.severity, e.customer_visible), ("VALIDATION", "INFO", True))
        self.assertEqual(e.dedup_key, "broker_validation:attempt:11")
        self.assertEqual(e.correlation_id, "c1")
        self.assertEqual(set(e.metadata), {"status", "reason_code", "retryable", "is_demo", "trigger"})

    def test_validation_customer_failure_is_warning(self):
        self._emit(bp.project_validation, self.a, attempt_id=12, status="NEEDS_ATTENTION")
        self.assertEqual(OperationalEvent.objects.get().severity, "WARNING")

    def test_validation_technical_failure_is_error(self):
        self._emit(bp.project_validation, self.a, attempt_id=13, status="UNAVAILABLE")
        self.assertEqual(OperationalEvent.objects.get().severity, "ERROR")

    def test_validation_dedup_on_attempt_id(self):
        self._emit(bp.project_validation, self.a, attempt_id=14, status="HEALTHY")
        self._emit(bp.project_validation, self.a, attempt_id=14, status="HEALTHY")
        self.assertEqual(OperationalEvent.objects.filter(dedup_key="broker_validation:attempt:14").count(), 1)

    # ── HEALTH ──
    def test_health_degraded_warning_customer_visible(self):
        self._emit(bp.project_health_transition, self.a, from_state="HEALTHY", to_state="DEGRADED",
                   reason_code="degraded_auth", state_version=3, pause_required=True)
        e = OperationalEvent.objects.get()
        self.assertEqual((e.category, e.severity, e.customer_visible), ("HEALTH", "WARNING", True))
        self.assertEqual(e.event_type, "broker_health_degraded")
        self.assertEqual(e.dedup_key, f"broker_health:{self.a.pk}:3")
        self.assertTrue(e.metadata["pause_required"])

    def test_health_disconnected_is_error_not_critical(self):
        self._emit(bp.project_health_transition, self.a, from_state="HEALTHY", to_state="DISCONNECTED",
                   reason_code="broker_unreachable", state_version=4)
        self.assertEqual(OperationalEvent.objects.get().severity, "ERROR")

    def test_health_validated_vs_recovered_event_type(self):
        self._emit(bp.project_health_transition, self.a, from_state="UNKNOWN", to_state="HEALTHY",
                   state_version=1)
        self.assertEqual(OperationalEvent.objects.get().event_type, "broker_health_validated")
        OperationalEvent.objects.all().delete()
        self._emit(bp.project_health_transition, self.a, from_state="DEGRADED", to_state="HEALTHY",
                   state_version=5)
        self.assertEqual(OperationalEvent.objects.get().event_type, "broker_health_recovered")

    def test_health_dedup_on_state_version(self):
        self._emit(bp.project_health_transition, self.a, from_state="HEALTHY", to_state="STALE",
                   state_version=6)
        self._emit(bp.project_health_transition, self.a, from_state="HEALTHY", to_state="STALE",
                   state_version=6)
        self.assertEqual(OperationalEvent.objects.filter(dedup_key=f"broker_health:{self.a.pk}:6").count(), 1)

    # ── CREDENTIAL ──
    def test_credential_invalidation_is_operator_only(self):
        self._emit(bp.project_credential_invalidation, self.a, state_version=7)
        e = OperationalEvent.objects.get()
        self.assertEqual((e.category, e.customer_visible), ("CREDENTIAL", False))

    def test_credential_rotation_is_customer_visible(self):
        self._emit(bp.project_credential_rotation, self.a, resulting_status="NEVER",
                   updated_at_iso="2026-08-04T00:00:00+00:00")
        e = OperationalEvent.objects.get()
        self.assertEqual((e.category, e.severity, e.customer_visible), ("CREDENTIAL", "INFO", True))

    # ── DISCONNECT ──
    def test_disconnect_customer_visible_with_metadata(self):
        self._emit(bp.project_disconnect, self.a, disconnected_at_iso="2026-08-04T00:00:00+00:00",
                   credential_destroyed=True)
        e = OperationalEvent.objects.get()
        self.assertEqual((e.category, e.customer_visible), ("CONNECTIVITY", True))
        self.assertEqual(e.dedup_key, f"broker_disconnect:{self.a.pk}")
        self.assertTrue(e.metadata["credential_destroyed"])
        self.assertFalse(e.metadata["row_deleted"])

    # ── PAUSE / RESUME ──
    def test_pause_paused_customer_warning(self):
        self._emit(bp.project_pause_audit, "BROKER_RUNTIME_PAUSED", self.a, version=2, rec=_Rec())
        e = OperationalEvent.objects.get()
        self.assertEqual((e.category, e.severity, e.customer_visible), ("RUNTIME", "WARNING", True))

    def test_pause_recovery_detected_operator_only(self):
        self._emit(bp.project_pause_audit, "BROKER_RECOVERY_DETECTED", self.a, version=2, rec=_Rec())
        self.assertFalse(OperationalEvent.objects.get().customer_visible)

    def test_pause_execution_gate_refused_is_not_projected_here(self):
        self._emit(bp.project_pause_audit, "EXECUTION_GATE_REFUSED", self.a, version=2, rec=None)
        self.assertEqual(OperationalEvent.objects.count(), 0)

    def test_resume_completed_customer_visible(self):
        self._emit(bp.project_resume_audit, "BROKER_RUNTIME_RESUMED", self.a, current=5, reason="ok")
        e = OperationalEvent.objects.get()
        self.assertEqual((e.category, e.customer_visible, e.event_type),
                         ("RUNTIME", True, "broker_resume_completed"))

    def test_resume_refused_operator_warning(self):
        self._emit(bp.project_resume_audit, "BROKER_RUNTIME_RESUME_REFUSED", self.a, current=0,
                   reason="broker_resume_not_eligible")
        e = OperationalEvent.objects.get()
        self.assertEqual((e.severity, e.customer_visible), ("WARNING", False))

    def test_resume_refusals_with_distinct_reasons_are_distinct_rows(self):
        # Both refusals carry no version (current=0) — an empty dedup key keeps distinct reasons distinct.
        self._emit(bp.project_resume_audit, "BROKER_RUNTIME_RESUME_REFUSED", self.a, current=0,
                   reason="broker_resume_not_eligible")
        self._emit(bp.project_resume_audit, "BROKER_RUNTIME_RESUME_REFUSED", self.a, current=0,
                   reason="broker_validation_required")
        reasons = set(OperationalEvent.objects.filter(event_type="broker_resume_refused")
                      .values_list("reason_code", flat=True))
        self.assertEqual(reasons, {"broker_resume_not_eligible", "broker_validation_required"})

    # ── EXECUTION refusals ──
    def test_execution_creation_refusal_operator_only(self):
        self._emit(bp.project_execution_refusal, self.a, reason_code="broker_validation_required",
                   phase="creation", trigger="scheduler_h1")
        e = OperationalEvent.objects.get()
        self.assertEqual((e.category, e.severity, e.customer_visible), ("EXECUTION", "WARNING", False))
        self.assertEqual(e.event_type, "broker_execution_gate_refused")

    def test_execution_dispatch_refusal_dedup_on_job(self):
        self._emit(bp.project_execution_refusal, self.a, reason_code="broker_account_disconnected",
                   phase="dispatch", job_id=99)
        self._emit(bp.project_execution_refusal, self.a, reason_code="broker_account_disconnected",
                   phase="dispatch", job_id=99)
        self.assertEqual(OperationalEvent.objects.filter(dedup_key="exec:dispatch:99").count(), 1)

    def test_promotion_rejection_same_reason_dedups(self):
        self._emit(bp.project_promotion_rejection, self.a, plan_id=42, reason_code="broker_gate_not_validated")
        self._emit(bp.project_promotion_rejection, self.a, plan_id=42, reason_code="broker_gate_not_validated")
        rows = OperationalEvent.objects.filter(event_type="broker_promotion_rejected")
        self.assertEqual(rows.count(), 1)
        self.assertFalse(rows.first().customer_visible)

    def test_promotion_rejection_distinct_reasons_are_distinct_rows(self):
        self._emit(bp.project_promotion_rejection, self.a, plan_id=42, reason_code="broker_gate_not_validated")
        self._emit(bp.project_promotion_rejection, self.a, plan_id=42, reason_code="broker_gate_disconnected")
        self.assertEqual(OperationalEvent.objects.filter(event_type="broker_promotion_rejected").count(), 2)

    # ── DARK + fail-open + no-secret ──
    def test_flag_off_records_nothing(self):
        with mock.patch.dict(os.environ, _OFF):
            with self.captureOnCommitCallbacks(execute=True):
                bp.project_validation(self.a, attempt_id=1, status="HEALTHY")
                bp.project_health_transition(self.a, from_state="HEALTHY", to_state="DEGRADED",
                                             state_version=1)
                bp.project_execution_refusal(self.a, reason_code="x", phase="dispatch", job_id=1)
        self.assertEqual(OperationalEvent.objects.count(), 0)

    def test_projection_helper_is_structurally_fail_open_on_bad_input(self):
        # Malformed input that raises during synchronous fact extraction (before on_commit) must be
        # swallowed by the @_failopen decorator — never surfacing to the authoritative caller.
        with mock.patch.dict(os.environ, _ON):
            bp.project_health_transition(self.a, from_state="X", to_state="Y",
                                         state_version="not-an-int")  # must not raise
        self.assertEqual(OperationalEvent.objects.count(), 0)

    def test_registration_failure_is_fail_open(self):
        # A failure registering the on_commit hook must never surface to the caller.
        with mock.patch.dict(os.environ, _ON), \
             mock.patch("operational_events.broker_projection.transaction.on_commit",
                        side_effect=RuntimeError("boom")):
            bp.project_validation(self.a, attempt_id=1, status="HEALTHY")  # must not raise

    def test_no_secret_or_ciphertext_in_projected_metadata(self):
        self._emit(bp.project_credential_rotation, self.a, resulting_status="NEVER",
                   updated_at_iso="2026-08-04T00:00:00+00:00")
        blob = str(OperationalEvent.objects.get().metadata).lower()
        for marker in ("password", "secret", "token", "cipher", "hash", "keyring"):
            self.assertNotIn(marker, blob)


class RollbackNoPhantomTests(TestCase):
    def setUp(self):
        self.a = _account("rb")

    def test_rolled_back_transaction_projects_nothing(self):
        with mock.patch.dict(os.environ, _ON):
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        bp.project_health_transition(self.a, from_state="HEALTHY", to_state="DEGRADED",
                                                     state_version=1)
                        raise RuntimeError("rollback")
                except RuntimeError:
                    pass
        self.assertEqual(OperationalEvent.objects.count(), 0)  # on_commit discarded on rollback → no phantom


class WiredSiteIntegrationTests(TestCase):
    """Exercise the real authoritative functions and assert they project (and stay unaffected)."""

    def setUp(self):
        self.a = _account("wired")

    def test_run_broker_validation_projects(self):
        from trading.broker_connectivity import run_broker_validation
        v = _StubValidator({"status": "HEALTHY", "reason": "demo_ok", "retryable": False,
                            "is_demo": True, "server": "S", "login_masked": "**1", "correlation_id": "c9"})
        with mock.patch.dict(os.environ, _ON):
            with self.captureOnCommitCallbacks(execute=True):
                attempt = run_broker_validation(self.a, trigger="add", validator=v)
        e = OperationalEvent.objects.get(category="VALIDATION")
        self.assertEqual(e.dedup_key, f"broker_validation:attempt:{attempt.id}")
        self.assertEqual(e.status, "HEALTHY")

    def test_run_broker_validation_dark_projects_nothing(self):
        from trading.broker_connectivity import run_broker_validation
        v = _StubValidator({"status": "HEALTHY"})
        with mock.patch.dict(os.environ, _OFF):
            with self.captureOnCommitCallbacks(execute=True):
                run_broker_validation(self.a, trigger="add", validator=v)
        self.assertEqual(OperationalEvent.objects.count(), 0)

    def test_disconnect_account_projects(self):
        from trading.broker_connectivity import disconnect_account
        with mock.patch.dict(os.environ, _ON):
            with self.captureOnCommitCallbacks(execute=True):
                disconnect_account(self.a)
        e = OperationalEvent.objects.get(category="CONNECTIVITY")
        self.assertEqual(e.dedup_key, f"broker_disconnect:{self.a.pk}")

    def test_replace_credentials_projects_rotation(self):
        from trading.broker_connectivity import replace_credentials
        with mock.patch.dict(os.environ, _ON):
            with self.captureOnCommitCallbacks(execute=True):
                replace_credentials(self.a, "new-password-123")
        self.assertTrue(OperationalEvent.objects.filter(
            category="CREDENTIAL", event_type="broker_credential_replaced").exists())

    def test_health_engine_transition_projects(self):
        # With both flags on, folding a HEALTHY attempt drives an UNKNOWN→HEALTHY transition → 1 HEALTH event.
        from reliability.broker_health import record_validation_outcome
        BrokerAccountValidationAttempt.objects.create(
            account=self.a, trigger="add", status="HEALTHY", reason_code="demo_ok")
        with mock.patch.dict(os.environ, _ON_HEALTH):
            with self.captureOnCommitCallbacks(execute=True):
                record_validation_outcome(self.a)
        e = OperationalEvent.objects.get(category="HEALTH")
        self.assertEqual(e.event_type, "broker_health_validated")
        self.assertTrue(e.customer_visible)


class AutocommitFailOpenTests(TransactionTestCase):
    """TransactionTestCase → no wrapping transaction, so transaction.on_commit runs the projection
    callback SYNCHRONOUSLY (real autocommit). This is where the recorder's fail-open guarantee is
    exercised: a recorder failure must never surface to the authoritative caller."""

    def test_recorder_failure_in_autocommit_is_swallowed(self):
        a = _account("aco")
        with mock.patch.dict(os.environ, _ON), \
             mock.patch("operational_events.broker_projection.record_event",
                        side_effect=RuntimeError("boom")):
            bp.project_validation(a, attempt_id=1, status="HEALTHY")  # must not raise

    def test_authoritative_validation_unaffected_when_recorder_raises(self):
        from trading.broker_connectivity import run_broker_validation
        a = _account("aco2")
        v = _StubValidator({"status": "HEALTHY"})
        with mock.patch.dict(os.environ, _ON), \
             mock.patch("operational_events.broker_projection.record_event",
                        side_effect=RuntimeError("boom")):
            attempt = run_broker_validation(a, trigger="add", validator=v)  # must return, not raise
        self.assertTrue(BrokerAccountValidationAttempt.objects.filter(id=attempt.id).exists())
