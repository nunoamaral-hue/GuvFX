"""WP1B/WP2 (ADR-0029) — broker-health runtime PAUSE (degradation processing) tests.

Covers: the four adverse states → durable pause; state_version idempotency + stale-version guard (an
older decision never reverses a newer one); recovery records resume-eligibility WITHOUT resuming;
creation-time block of new exposure-opening jobs while paused; flag-OFF/health-OFF inertness; and the
non-destructive invariants (no runtime deletion, no credential mutation, no order creation).
"""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import AuditEvent
from execution import broker_gate as bg
from execution import runtime_pause as rp
from execution.models import BrokerRuntimePause, ExecutionJob
from reliability.models import BrokerAccountHealth
from trading.models import TradingAccount

U = get_user_model()
_VS = TradingAccount.ValidationStatus
_ON_BOTH = {"BROKER_CONNECTIVITY_EXECUTION_GATE": "1", "BROKER_CONNECTIVITY_HEALTH_ENABLED": "true"}


def _acct(user, *, number="1302575", validated=_VS.VALIDATED):
    return TradingAccount.objects.create(
        user=user, name="A", account_number=number, broker_name="IS6Technologies",
        is_demo=True, is_active=True, validation_status=validated, password_enc="cipher")


def _health(account, state, version=1, resume=False):
    return BrokerAccountHealth.objects.create(
        account=account, state=state, state_version=version, resume_eligible=resume)


class PauseProcessingTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="p1", email="p1@x.invalid", password="x")
        self.acct = _acct(self.user)

    def _pause(self):
        return BrokerRuntimePause.objects.get(account=self.acct)

    def test_each_adverse_state_persists_pause(self):
        cases = [("DEGRADED", bg.SR_HEALTH_DEGRADED), ("STALE", bg.SR_HEALTH_STALE),
                 ("DISCONNECTED", bg.SR_HEALTH_DISCONNECTED), ("TOMBSTONED", bg.SR_ACCOUNT_TOMBSTONED)]
        for i, (state, reason) in enumerate(cases):
            u = U.objects.create_user(username=f"p1{i}", email=f"p1{i}@x.invalid", password="x")
            acct = _acct(u, number=f"77{i}")
            _health(acct, state, version=4)
            with mock.patch.dict(os.environ, _ON_BOTH):
                d = rp.process_broker_health_pause(acct)
            self.assertTrue(d["paused"], state)
            self.assertEqual(d["reason_code"], reason, state)
            self.assertEqual(d["source_state_version"], 4, state)
            self.assertIsNotNone(d["paused_at"], state)

    def test_pause_is_idempotent_on_repeat_version(self):
        _health(self.acct, "DEGRADED", version=5)
        with mock.patch.dict(os.environ, _ON_BOTH):
            rp.process_broker_health_pause(self.acct)
            n = AuditEvent.objects.filter(event_type="BROKER_RUNTIME_PAUSED").count()
            rp.process_broker_health_pause(self.acct)  # same version → no-op
        self.assertEqual(AuditEvent.objects.filter(event_type="BROKER_RUNTIME_PAUSED").count(), n)
        self.assertEqual(self._pause().last_processed_version, 5)

    def test_stale_version_ignored_never_reverses(self):
        h = _health(self.acct, "DEGRADED", version=10)
        with mock.patch.dict(os.environ, _ON_BOTH):
            rp.process_broker_health_pause(self.acct)  # pause at v10
            # An older version arrives (e.g. a delayed/out-of-order signal) → must be ignored.
            h.state = "HEALTHY"
            h.state_version = 9
            h.save(update_fields=["state", "state_version"])
            d = rp.process_broker_health_pause(self.acct)
        self.assertTrue(d["paused"])  # newer pause decision NOT reversed by the stale version
        self.assertEqual(d["last_processed_version"], 10)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="BROKER_HEALTH_STALE_PAUSE_VERSION_IGNORED").exists())

    def test_newer_version_supersedes(self):
        h = _health(self.acct, "DEGRADED", version=5)
        with mock.patch.dict(os.environ, _ON_BOTH):
            rp.process_broker_health_pause(self.acct)  # DEGRADED v5
            h.state = "DISCONNECTED"
            h.state_version = 6
            h.save(update_fields=["state", "state_version"])
            d = rp.process_broker_health_pause(self.acct)
        self.assertEqual(d["reason_code"], bg.SR_HEALTH_DISCONNECTED)
        self.assertEqual(d["last_processed_version"], 6)

    def test_recovery_records_eligibility_without_resuming(self):
        h = _health(self.acct, "DEGRADED", version=5)
        with mock.patch.dict(os.environ, _ON_BOTH):
            rp.process_broker_health_pause(self.acct)  # paused
            h.state = "HEALTHY"
            h.state_version = 7
            h.resume_eligible = True
            h.save(update_fields=["state", "state_version", "resume_eligible"])
            d = rp.process_broker_health_pause(self.acct)
        self.assertTrue(d["resume_eligible"])   # recovery recorded
        self.assertTrue(d["paused"])            # but STILL paused — no automatic resume
        self.assertTrue(AuditEvent.objects.filter(event_type="BROKER_RECOVERY_DETECTED").exists())

    def test_recovery_via_broken_edge_still_marks_resumable(self):
        # M1: a paused account recovers to HEALTHY but the contract's resume_eligible is False (e.g. a
        # credential replace → re-validate produces a "validated", not "recovered", edge). The durable
        # record must STILL mark resume_eligible (keyed on the live contract's eligible), so the resume
        # service can recognise it — but must NOT auto-resume.
        h = _health(self.acct, "DEGRADED", version=5)
        with mock.patch.dict(os.environ, _ON_BOTH):
            rp.process_broker_health_pause(self.acct)  # paused
            h.state = "HEALTHY"
            h.state_version = 8
            h.resume_eligible = False  # broken edge — WP3 did NOT flag a recovery
            h.save(update_fields=["state", "state_version", "resume_eligible"])
            d = rp.process_broker_health_pause(self.acct)
        self.assertTrue(d["resume_eligible"])  # durable record recognises it as resumable
        self.assertTrue(d["paused"])           # still paused — no auto-resume

    def test_pause_audited(self):
        _health(self.acct, "DEGRADED", version=3)
        with mock.patch.dict(os.environ, _ON_BOTH):
            rp.process_broker_health_pause(self.acct)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="BROKER_RUNTIME_PAUSED", entity_id=str(self.acct.pk)).exists())


class PauseInertnessTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="p2", email="p2@x.invalid", password="x")
        self.acct = _acct(self.user)
        _health(self.acct, "DEGRADED", version=4)

    def test_noop_when_exec_flag_off(self):
        with mock.patch.dict(os.environ, {"BROKER_CONNECTIVITY_HEALTH_ENABLED": "true"}):  # exec OFF
            self.assertIsNone(rp.process_broker_health_pause(self.acct))
        self.assertFalse(BrokerRuntimePause.objects.exists())

    def test_noop_when_health_flag_off(self):
        with mock.patch.dict(os.environ, {"BROKER_CONNECTIVITY_EXECUTION_GATE": "1"}):  # health OFF
            self.assertIsNone(rp.process_broker_health_pause(self.acct))
        self.assertFalse(BrokerRuntimePause.objects.exists())

    def test_healthy_account_gets_no_row(self):
        BrokerAccountHealth.objects.filter(account=self.acct).update(state="HEALTHY")
        with mock.patch.dict(os.environ, _ON_BOTH):
            self.assertIsNone(rp.process_broker_health_pause(self.acct))
        self.assertFalse(BrokerRuntimePause.objects.exists())


class PauseNonDestructiveTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="p3", email="p3@x.invalid", password="x")
        self.acct = _acct(self.user)
        _health(self.acct, "DEGRADED", version=4)

    def test_pause_does_not_mutate_account_or_create_orders(self):
        before = TradingAccount.objects.get(pk=self.acct.pk)
        with mock.patch.dict(os.environ, _ON_BOTH):
            rp.process_broker_health_pause(self.acct)
        after = TradingAccount.objects.get(pk=self.acct.pk)
        self.assertEqual(after.password_enc, before.password_enc)  # no credential mutation
        self.assertEqual(after.is_active, before.is_active)        # no is_active flip
        self.assertEqual(after.validation_status, before.validation_status)
        self.assertEqual(ExecutionJob.objects.count(), 0)          # no order/execution created


class CreationBlockedWhilePausedTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="p4", email="p4@x.invalid", password="x")
        self.acct = _acct(self.user)  # VALIDATED (passes eligibility) but health will be adverse

    def _mk_job(self):
        return ExecutionJob.objects.create(account=self.acct, job_type="PLACE_ORDER", payload={}, status="PENDING")

    def test_creation_refused_when_health_pause_required(self):
        _health(self.acct, "DEGRADED", version=2)
        with mock.patch.dict(os.environ, _ON_BOTH):
            with self.assertRaises(bg.ExecutionGateRefused) as ctx:
                self._mk_job()
        self.assertEqual(ctx.exception.reason_code, bg.SR_HEALTH_DEGRADED)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_creation_allowed_when_healthy(self):
        _health(self.acct, "HEALTHY", version=2)
        with mock.patch.dict(os.environ, _ON_BOTH):
            job = self._mk_job()
        self.assertEqual(job.job_type, "PLACE_ORDER")

    def test_creation_unaffected_when_flags_off(self):
        _health(self.acct, "DEGRADED", version=2)  # adverse, but flags OFF ⇒ no pause block
        job = self._mk_job()
        self.assertEqual(job.job_type, "PLACE_ORDER")

    def test_require_not_broker_paused_noop_health_off(self):
        _health(self.acct, "DEGRADED", version=2)
        with mock.patch.dict(os.environ, {"BROKER_CONNECTIVITY_EXECUTION_GATE": "1"}):  # health OFF
            rp.require_not_broker_paused(self.acct)  # must not raise


class ValidationFlowPersistsPauseTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="p5", email="p5@x.invalid", password="x")
        self.acct = _acct(self.user)

    def test_degrading_validation_persists_pause(self):
        from trading import broker_connectivity as bc

        class _Outcome:
            def as_dict(self):
                return {"status": "NEEDS_ATTENTION", "reason": "invalid_password"}

        class _Validator:
            def validate(self, a):
                return _Outcome()

        _health(self.acct, "DEGRADED", version=3)  # already degraded
        with mock.patch.dict(os.environ, _ON_BOTH):
            bc.run_broker_validation(self.acct, trigger="test", validator=_Validator())
        rec = BrokerRuntimePause.objects.filter(account=self.acct).first()
        self.assertIsNotNone(rec)
        self.assertTrue(rec.paused)
