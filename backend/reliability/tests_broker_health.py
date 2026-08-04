"""WP3 (ADR-0030) — Continuous Broker Health Engine tests.

Coverage: six-state transitions, thresholds, adverse-state latching, staleness, tombstone terminality,
idempotent history consumption, batch-fold net signalling, deduplicated notifications, audit emission,
deterministic backoff + jitter, the inert scheduler (flag OFF / no validator) and its armed path
(single-flight + quota), and the isolation invariants (no runtime mutation / broker login / credential
access). All engine writes are gated behind BROKER_CONNECTIVITY_HEALTH_ENABLED — the OFF path is a
no-op.
"""
import os
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.models import AuditEvent
from reliability import broker_health as bh
from reliability import broker_health_scheduler as sched
from reliability.constants import broker_health_config
from reliability.models import AlertEvent, BrokerAccountHealth
from trading.models import BrokerAccountValidationAttempt as Attempt
from trading.models import TradingAccount
from users.models import User

State = BrokerAccountHealth.State

_ON = {
    "BROKER_CONNECTIVITY_HEALTH_ENABLED": "true",
    "BROKER_HEALTH_FAILURE_THRESHOLD": "3",
    "BROKER_HEALTH_SUCCESS_THRESHOLD": "2",
    "BROKER_HEALTH_STALE_TIMEOUT_S": "3600",
    "BROKER_HEALTH_BASE_INTERVAL_S": "300",
    "BROKER_HEALTH_BACKOFF_FACTOR": "2.0",
    "BROKER_HEALTH_MAX_INTERVAL_S": "3600",
    "BROKER_HEALTH_JITTER_FRAC": "0.1",
    "BROKER_HEALTH_QUOTA_PER_CYCLE": "50",
}


def _mk_user(n="u"):
    return User.objects.create_user(username=n, email=f"{n}@x.invalid", password="x")


def _acct(user, *, number="1302575", disconnected=None):
    a = TradingAccount.objects.create(
        user=user, name="A", account_number=number, broker_name="IS6Technologies",
        is_demo=True, is_active=True, password_enc="cipher")
    if disconnected is not None:
        a.disconnected_at = disconnected
        a.save(update_fields=["disconnected_at"])
    return a


def _attempt(account, status, trigger=Attempt.Trigger.HEALTH):
    return Attempt.objects.create(account=account, trigger=trigger, status=status)


class _Base(TestCase):
    def setUp(self):
        self.user = _mk_user()
        self.acct = _acct(self.user)
        self._env = mock.patch.dict(os.environ, _ON)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _health(self):
        return BrokerAccountHealth.objects.get(account=self.acct)

    def _mk_healthy(self, now=None):
        """Seed a single successful validation and fold it → account starts HEALTHY (state_version 1)."""
        _attempt(self.acct, "HEALTHY")
        return bh.record_validation_outcome(self.acct, now=now)


# ─── Feature flag OFF: the whole engine is a no-op ───
class FeatureFlagOffTests(TestCase):
    def setUp(self):
        self.user = _mk_user()
        self.acct = _acct(self.user)
        _attempt(self.acct, "HEALTHY")

    def _off(self):
        return mock.patch.dict(os.environ, {"BROKER_CONNECTIVITY_HEALTH_ENABLED": "false"})

    def test_record_is_noop_and_creates_no_row(self):
        with self._off():
            self.assertIsNone(bh.record_validation_outcome(self.acct))
        self.assertFalse(BrokerAccountHealth.objects.exists())

    def test_sweep_and_contract_and_cycle_are_noop(self):
        with self._off():
            self.assertIsNone(bh.sweep_stale(self.acct))
            self.assertIsNone(bh.get_contract(self.acct))
            res = sched.run_cycle(validator=lambda a: None)
        self.assertEqual(res["reason"], "disabled")
        self.assertFalse(res["ran"])
        self.assertFalse(BrokerAccountHealth.objects.exists())

    def test_flag_must_be_exactly_truthy(self):
        # Defensive: a stray value is treated as OFF (fail-safe), not accidentally ON.
        with mock.patch.dict(os.environ, {"BROKER_CONNECTIVITY_HEALTH_ENABLED": "1"}):
            # "1" is not the accepted truthy token ("true") for this flag helper → OFF.
            self.assertIsNone(bh.record_validation_outcome(self.acct))


# ─── Deterministic state transitions ───
class TransitionTests(_Base):
    def test_unknown_to_healthy_on_first_success(self):
        _attempt(self.acct, "HEALTHY")
        c = bh.record_validation_outcome(self.acct)
        self.assertEqual(c["state"], State.HEALTHY)
        self.assertEqual(c["reason_code"], bh.REASON_VALIDATED)
        self.assertTrue(c["eligible"])
        self.assertFalse(c["pause_required"])
        self.assertFalse(c["resume_eligible"])  # nothing was paused
        self.assertEqual(c["state_version"], 1)

    def test_healthy_stays_healthy_without_version_churn(self):
        self._mk_healthy()  # HEALTHY, state_version 1
        _attempt(self.acct, "HEALTHY")
        c = bh.record_validation_outcome(self.acct)
        self.assertEqual(c["state"], State.HEALTHY)
        self.assertEqual(c["state_version"], 1)  # unchanged — no spurious transitions

    def test_below_threshold_failures_stay_healthy(self):
        self._mk_healthy()
        _attempt(self.acct, "NEEDS_ATTENTION")
        _attempt(self.acct, "NEEDS_ATTENTION")  # 2 < threshold 3
        c = bh.record_validation_outcome(self.acct)
        self.assertEqual(c["state"], State.HEALTHY)
        self.assertEqual(self._health().consecutive_failures, 2)

    def test_degraded_after_soft_failure_threshold(self):
        bh.record_validation_outcome(self.acct)
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        c = bh.record_validation_outcome(self.acct)
        self.assertEqual(c["state"], State.DEGRADED)
        self.assertEqual(c["reason_code"], bh.REASON_DEGRADED)
        self.assertTrue(c["pause_required"])
        self.assertFalse(c["eligible"])

    def test_disconnected_after_hard_failure_threshold(self):
        bh.record_validation_outcome(self.acct)
        for _ in range(3):
            _attempt(self.acct, "UNAVAILABLE")
        c = bh.record_validation_outcome(self.acct)
        self.assertEqual(c["state"], State.DISCONNECTED)
        self.assertEqual(c["reason_code"], bh.REASON_DISCONNECTED)
        self.assertTrue(c["pause_required"])

    def test_recovery_degraded_to_healthy_sets_resume_eligible(self):
        bh.record_validation_outcome(self.acct)
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        bh.record_validation_outcome(self.acct)  # DEGRADED
        for _ in range(2):
            _attempt(self.acct, "HEALTHY")
        c = bh.record_validation_outcome(self.acct)  # recover
        self.assertEqual(c["state"], State.HEALTHY)
        self.assertEqual(c["reason_code"], bh.REASON_RECOVERED)
        self.assertTrue(c["resume_eligible"])
        self.assertFalse(c["pause_required"])

    def test_one_success_is_not_enough_to_recover(self):
        bh.record_validation_outcome(self.acct)
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        bh.record_validation_outcome(self.acct)  # DEGRADED
        _attempt(self.acct, "HEALTHY")  # only 1 < success_threshold 2
        c = bh.record_validation_outcome(self.acct)
        self.assertEqual(c["state"], State.DEGRADED)

    def test_adverse_substate_is_latched_no_flapping(self):
        # Once DEGRADED, a hard failure must NOT flip it to DISCONNECTED (anti-flap latch).
        bh.record_validation_outcome(self.acct)
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        bh.record_validation_outcome(self.acct)  # DEGRADED
        v = self._health().state_version
        _attempt(self.acct, "UNAVAILABLE")
        c = bh.record_validation_outcome(self.acct)
        self.assertEqual(c["state"], State.DEGRADED)      # still DEGRADED
        self.assertEqual(c["state_version"], v)           # no transition, no churn

    def test_resume_eligible_is_a_level_tied_to_state_version(self):
        # After recovery, resume_eligible holds (a level bound to the recovery's state_version) across
        # subsequent HEALTHY folds — no version churn, no repeated signal — and clears on the next
        # adverse transition. This is the documented convergence-contract semantics (ADR-0030 §3).
        self._mk_healthy()
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        bh.record_validation_outcome(self.acct)                 # DEGRADED
        for _ in range(2):
            _attempt(self.acct, "HEALTHY")
        c1 = bh.record_validation_outcome(self.acct)            # recover → resume True
        self.assertTrue(c1["resume_eligible"])
        v = c1["state_version"]
        _attempt(self.acct, "HEALTHY")
        c2 = bh.record_validation_outcome(self.acct)            # still HEALTHY, no transition
        self.assertTrue(c2["resume_eligible"])                  # level held
        self.assertEqual(c2["state_version"], v)                # tied to the same version
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        c3 = bh.record_validation_outcome(self.acct)            # DEGRADED again
        self.assertFalse(c3["resume_eligible"])                 # cleared on the adverse transition
        self.assertGreater(c3["state_version"], v)

    def test_reason_code_is_a_level_bound_to_state_version(self):
        # reason_code is set only on a net transition, so it holds (with state_version) across
        # steady-state HEALTHY folds: a long-healthy account that recovered reports "recovered",
        # not "validated" — intended level semantics, pinned here to prevent silent regression.
        self._mk_healthy()
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        bh.record_validation_outcome(self.acct)          # DEGRADED
        for _ in range(2):
            _attempt(self.acct, "HEALTHY")
        c1 = bh.record_validation_outcome(self.acct)     # recover
        self.assertEqual(c1["reason_code"], bh.REASON_RECOVERED)
        v = c1["state_version"]
        _attempt(self.acct, "HEALTHY")
        c2 = bh.record_validation_outcome(self.acct)     # steady-state HEALTHY, no transition
        self.assertEqual(c2["reason_code"], bh.REASON_RECOVERED)  # sticks with the version
        self.assertEqual(c2["state_version"], v)

    def test_version_is_monotonic_across_full_lifecycle(self):
        versions = []
        bh.record_validation_outcome(self.acct)
        versions.append(self._health().state_version)
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        bh.record_validation_outcome(self.acct)
        versions.append(self._health().state_version)
        for _ in range(2):
            _attempt(self.acct, "HEALTHY")
        bh.record_validation_outcome(self.acct)
        versions.append(self._health().state_version)
        self.assertEqual(versions, sorted(versions))
        self.assertTrue(all(b > a for a, b in zip(versions, versions[1:])))


# ─── Batch fold nets to a single signal ───
class BatchFoldTests(_Base):
    def test_transient_dip_within_one_fold_is_invisible(self):
        # HEALTHY → (dip) → HEALTHY inside ONE fold nets to no change: no version churn, and —
        # symmetrically with the suppressed pause — NO phantom resume for a never-(net)-paused account.
        c0 = self._mk_healthy()  # HEALTHY, state_version 1, resume_eligible False
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        for _ in range(2):
            _attempt(self.acct, "HEALTHY")
        c = bh.record_validation_outcome(self.acct)  # folds the whole dip+recovery
        self.assertEqual(c["state"], State.HEALTHY)
        self.assertFalse(c["resume_eligible"])              # no phantom resume
        self.assertEqual(c["state_version"], c0["state_version"])  # no net transition → no version churn
        events = list(AuditEvent.objects.filter(
            entity_type="trading_account", entity_id=str(self.acct.pk)
        ).values_list("event_type", flat=True))
        self.assertNotIn("BROKER_HEALTH_DEGRADED", events)       # dip suppressed...
        self.assertNotIn("BROKER_HEALTH_RESUME_ELIGIBLE", events)  # ...and so is the resume (symmetric)

    def test_genuine_recovery_across_folds_signals_resume(self):
        # A pause that PERSISTS across folds (real WP1B-visible pause) recovers with a resume signal.
        self._mk_healthy()
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        cdeg = bh.record_validation_outcome(self.acct)   # persisted DEGRADED
        self.assertEqual(cdeg["state"], State.DEGRADED)
        for _ in range(2):
            _attempt(self.acct, "HEALTHY")
        c = bh.record_validation_outcome(self.acct)      # recover
        self.assertEqual(c["state"], State.HEALTHY)
        self.assertTrue(c["resume_eligible"])
        events = list(AuditEvent.objects.filter(
            entity_type="trading_account", entity_id=str(self.acct.pk)
        ).values_list("event_type", flat=True))
        self.assertIn("BROKER_HEALTH_RESUME_ELIGIBLE", events)


# ─── Unknown / malformed status is fail-safe ───
class ClassificationTests(TestCase):
    def test_classify_status_fail_safe(self):
        self.assertEqual(bh.classify_status("HEALTHY"), bh.SUCCESS)
        self.assertEqual(bh.classify_status("healthy"), bh.SUCCESS)
        self.assertEqual(bh.classify_status("UNAVAILABLE"), bh.FAILURE_HARD)
        self.assertEqual(bh.classify_status("NEEDS_ATTENTION"), bh.FAILURE_SOFT)
        self.assertEqual(bh.classify_status("GIBBERISH"), bh.FAILURE_SOFT)  # never SUCCESS
        self.assertEqual(bh.classify_status(""), bh.FAILURE_SOFT)
        self.assertEqual(bh.classify_status(None), bh.FAILURE_SOFT)


# ─── Staleness (time-driven) ───
class StaleTests(_Base):
    def test_healthy_becomes_stale_after_timeout(self):
        t0 = timezone.now()
        self._mk_healthy(now=t0)
        later = t0 + timedelta(seconds=3601)
        c = bh.sweep_stale(self.acct, now=later)
        self.assertEqual(c["state"], State.STALE)
        self.assertEqual(c["reason_code"], bh.REASON_STALE)
        self.assertTrue(c["pause_required"])

    def test_not_stale_within_timeout(self):
        t0 = timezone.now()
        self._mk_healthy(now=t0)
        c = bh.sweep_stale(self.acct, now=t0 + timedelta(seconds=100))
        self.assertEqual(c["state"], State.HEALTHY)

    def test_stale_recovers_on_success(self):
        t0 = timezone.now()
        self._mk_healthy(now=t0)
        bh.sweep_stale(self.acct, now=t0 + timedelta(seconds=3601))  # STALE
        # Fresh successful validations carry evidence time == the recovery moment (as the scheduler's
        # validator would), so the recovery is not immediately re-flagged stale.
        t_rec = t0 + timedelta(seconds=3700)
        for _ in range(2):
            a = _attempt(self.acct, "HEALTHY")
            Attempt.objects.filter(pk=a.pk).update(created_at=t_rec)
        c = bh.record_validation_outcome(self.acct, now=t_rec)
        self.assertEqual(c["state"], State.HEALTHY)
        self.assertTrue(c["resume_eligible"])

    def test_sweep_noop_when_not_healthy(self):
        for _ in range(3):
            _attempt(self.acct, "UNAVAILABLE")
        bh.record_validation_outcome(self.acct)  # DISCONNECTED
        c = bh.sweep_stale(self.acct, now=timezone.now() + timedelta(days=1))
        self.assertEqual(c["state"], State.DISCONNECTED)  # stale only applies from HEALTHY

    def test_last_success_uses_evidence_time_not_consumption_time(self):
        # Folding an OLD successful attempt must clock staleness from the attempt's evidence time,
        # not from "now" — otherwise a backfilled stale success masks staleness for a full window.
        t_old = timezone.now() - timedelta(seconds=7200)
        a = _attempt(self.acct, "HEALTHY")
        Attempt.objects.filter(pk=a.pk).update(created_at=t_old)  # override auto_now_add
        bh.record_validation_outcome(self.acct, now=timezone.now())
        h = self._health()
        self.assertEqual(h.last_success_at, t_old)  # evidence time, not consumption time
        # ...and it is therefore already past the 3600s window → a sweep flags STALE immediately.
        c = bh.sweep_stale(self.acct, now=timezone.now())
        self.assertEqual(c["state"], State.STALE)

    def test_stale_escalates_to_disconnected_on_hard_failures(self):
        t0 = timezone.now()
        self._mk_healthy(now=t0)
        bh.sweep_stale(self.acct, now=t0 + timedelta(seconds=3601))  # STALE
        for _ in range(3):
            _attempt(self.acct, "UNAVAILABLE")
        c = bh.record_validation_outcome(self.acct, now=t0 + timedelta(seconds=3700))
        self.assertEqual(c["state"], State.DISCONNECTED)          # STALE → DISCONNECTED escalation
        self.assertEqual(c["reason_code"], bh.REASON_DISCONNECTED)  # accurate reason, not "stale"


# ─── Tombstone terminality ───
class TombstoneTests(_Base):
    def test_disconnected_account_tombstones_and_is_terminal(self):
        self.acct.disconnected_at = timezone.now()
        self.acct.save(update_fields=["disconnected_at"])
        c = bh.record_validation_outcome(self.acct)
        self.assertEqual(c["state"], State.TOMBSTONED)
        self.assertEqual(c["reason_code"], bh.REASON_TOMBSTONED)
        # A subsequent HEALTHY attempt must NOT revive a tombstoned account.
        _attempt(self.acct, "HEALTHY")
        c2 = bh.record_validation_outcome(self.acct)
        self.assertEqual(c2["state"], State.TOMBSTONED)

    def test_healthy_then_tombstoned(self):
        self._mk_healthy()  # HEALTHY
        self.acct.disconnected_at = timezone.now()
        self.acct.save(update_fields=["disconnected_at"])
        c = bh.record_validation_outcome(self.acct)
        self.assertEqual(c["state"], State.TOMBSTONED)
        self.assertTrue(c["pause_required"])
        self.assertFalse(c["resume_eligible"])


# ─── Idempotent history consumption ───
class IdempotencyTests(_Base):
    def test_watermark_prevents_double_consumption(self):
        _attempt(self.acct, "NEEDS_ATTENTION")
        bh.record_validation_outcome(self.acct)
        h1 = self._health()
        # Re-run with no new attempts: contract identical, no counter drift, no new audit rows.
        before = AuditEvent.objects.count()
        c = bh.record_validation_outcome(self.acct)
        h2 = self._health()
        self.assertEqual(h1.consecutive_failures, h2.consecutive_failures)
        self.assertEqual(h1.state_version, h2.state_version)
        self.assertEqual(c["state_version"], h1.state_version)
        self.assertEqual(AuditEvent.objects.count(), before)  # no duplicate signalling

    def test_consumes_preexisting_history(self):
        # Attempts created BEFORE any health row must still be folded in on first record.
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        c = bh.record_validation_outcome(self.acct)
        self.assertEqual(c["state"], State.DEGRADED)
        self.assertEqual(self._health().last_consumed_attempt_id,
                         self.acct.validation_attempts.order_by("-id").first().id)


# ─── Deduplicated notifications ───
class NotificationTests(_Base):
    def _open_broker_alerts(self):
        return AlertEvent.objects.filter(
            trading_account=self.acct, status=AlertEvent.Status.OPEN,
            dedup_key__startswith=f"BROKER_HEALTH:{self.acct.pk}:")

    def test_sustained_degraded_dedups_to_single_open_alert(self):
        bh.record_validation_outcome(self.acct)
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        bh.record_validation_outcome(self.acct)  # → DEGRADED, opens alert
        _attempt(self.acct, "NEEDS_ATTENTION")   # still degraded (latched)
        bh.record_validation_outcome(self.acct)
        self.assertEqual(self._open_broker_alerts().count(), 1)

    def test_recovery_resolves_open_alert(self):
        bh.record_validation_outcome(self.acct)
        for _ in range(3):
            _attempt(self.acct, "UNAVAILABLE")
        bh.record_validation_outcome(self.acct)  # DISCONNECTED alert
        self.assertEqual(self._open_broker_alerts().count(), 1)
        for _ in range(2):
            _attempt(self.acct, "HEALTHY")
        bh.record_validation_outcome(self.acct)  # recover
        self.assertEqual(self._open_broker_alerts().count(), 0)  # resolved
        self.assertTrue(AlertEvent.objects.filter(
            trading_account=self.acct, status=AlertEvent.Status.RESOLVED).exists())

    def test_disconnected_alert_is_critical(self):
        bh.record_validation_outcome(self.acct)
        for _ in range(3):
            _attempt(self.acct, "UNAVAILABLE")
        bh.record_validation_outcome(self.acct)
        alert = self._open_broker_alerts().first()
        self.assertEqual(alert.severity, AlertEvent.Severity.CRITICAL)

    def test_recovery_resolves_acknowledged_alert_too(self):
        # An operator-acknowledged adverse alert must also clear on recovery, not linger forever.
        bh.record_validation_outcome(self.acct)
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        bh.record_validation_outcome(self.acct)  # DEGRADED → opens alert
        AlertEvent.objects.filter(trading_account=self.acct, status=AlertEvent.Status.OPEN).update(
            status=AlertEvent.Status.ACKNOWLEDGED)
        for _ in range(2):
            _attempt(self.acct, "HEALTHY")
        bh.record_validation_outcome(self.acct)  # recover
        self.assertFalse(AlertEvent.objects.filter(
            trading_account=self.acct,
            status__in=(AlertEvent.Status.OPEN, AlertEvent.Status.ACKNOWLEDGED)).exists())


# ─── Audit emission ───
class AuditTests(_Base):
    def _events(self):
        return list(AuditEvent.objects.filter(
            entity_type="trading_account", entity_id=str(self.acct.pk)
        ).values_list("event_type", flat=True))

    def test_degraded_and_pause_required_audited(self):
        bh.record_validation_outcome(self.acct)
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        bh.record_validation_outcome(self.acct)
        events = self._events()
        self.assertIn("BROKER_HEALTH_DEGRADED", events)
        self.assertIn("BROKER_HEALTH_PAUSE_REQUIRED", events)

    def test_recovered_and_resume_eligible_audited(self):
        bh.record_validation_outcome(self.acct)
        for _ in range(3):
            _attempt(self.acct, "NEEDS_ATTENTION")
        bh.record_validation_outcome(self.acct)
        for _ in range(2):
            _attempt(self.acct, "HEALTHY")
        bh.record_validation_outcome(self.acct)
        events = self._events()
        self.assertIn("BROKER_HEALTH_RECOVERED", events)
        self.assertIn("BROKER_HEALTH_RESUME_ELIGIBLE", events)

    def test_stale_detected_audited(self):
        t0 = timezone.now()
        self._mk_healthy(now=t0)
        bh.sweep_stale(self.acct, now=t0 + timedelta(seconds=3601))
        self.assertIn("BROKER_HEALTH_STALE_DETECTED", self._events())

    def test_audit_metadata_is_secret_free(self):
        for _ in range(3):
            _attempt(self.acct, "UNAVAILABLE")
        bh.record_validation_outcome(self.acct)
        ev = AuditEvent.objects.filter(event_type="BROKER_HEALTH_DISCONNECTED").first()
        blob = str(ev.metadata) + str(ev.__dict__)
        self.assertNotIn("cipher", blob)  # password_enc value never surfaces
        self.assertIn("state", ev.metadata)


# ─── Deterministic backoff + jitter ───
class BackoffJitterTests(TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, _ON)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.cfg = broker_health_config()

    def test_backoff_base_when_no_failures(self):
        self.assertEqual(sched.next_interval_s(0, self.cfg), 300.0)

    def test_backoff_grows_and_clamps(self):
        self.assertEqual(sched.next_interval_s(1, self.cfg), 600.0)
        self.assertEqual(sched.next_interval_s(2, self.cfg), 1200.0)
        # 300 * 2^10 would be 307200 → clamped to max 3600
        self.assertEqual(sched.next_interval_s(10, self.cfg), 3600.0)

    def test_backoff_never_overflows(self):
        self.assertEqual(sched.next_interval_s(10_000, self.cfg), 3600.0)

    def test_backoff_pathological_factor_does_not_raise(self):
        # A pathological configured factor must saturate to the clamp, never raise OverflowError.
        cfg = dict(self.cfg, backoff_factor=1e200)
        self.assertEqual(sched.next_interval_s(5, cfg), float(cfg["max_interval_s"]))

    def test_jitter_is_deterministic_and_within_ceiling(self):
        u = _mk_user("j")
        acct = _acct(u)
        h = BrokerAccountHealth.objects.create(account=acct, consecutive_failures=0, state_version=3)
        now = timezone.now()
        a = sched.compute_next_check_at(h, now, self.cfg)
        b = sched.compute_next_check_at(h, now, self.cfg)
        self.assertEqual(a, b)  # deterministic — no random source
        delta = (a - now).total_seconds()
        # Downward-only jitter: interval ∈ [base·(1-frac), base]; never above base (≤ max_interval_s).
        self.assertGreaterEqual(delta, 300.0 * (1 - 0.1))
        self.assertLessEqual(delta, 300.0)

    def test_next_check_never_exceeds_max_interval(self):
        u = _mk_user("mx")
        acct = _acct(u)
        h = BrokerAccountHealth.objects.create(account=acct, consecutive_failures=50, state_version=1)
        now = timezone.now()
        delta = (sched.compute_next_check_at(h, now, self.cfg) - now).total_seconds()
        self.assertLessEqual(delta, float(self.cfg["max_interval_s"]))


# ─── Scheduler framework ───
class SchedulerTests(_Base):
    def test_flag_on_but_no_validator_is_inert(self):
        res = sched.run_cycle(validator=None)
        self.assertEqual(res["reason"], "no_validator")
        self.assertFalse(res["ran"])
        self.assertFalse(BrokerAccountHealth.objects.exists())

    def test_armed_cycle_validates_and_folds(self):
        # Seed a due health row (next_check_at NULL == due).
        BrokerAccountHealth.objects.create(account=self.acct)
        calls = []

        def validator(account):
            calls.append(account.pk)
            _attempt(account, "HEALTHY")

        res = sched.run_cycle(validator=validator)
        self.assertTrue(res["ran"])
        self.assertEqual(res["validated"], 1)
        self.assertEqual(calls, [self.acct.pk])
        self.assertEqual(self._health().state, State.HEALTHY)
        self.assertIsNotNone(self._health().next_check_at)  # rescheduled

    def test_quota_caps_accounts_per_cycle(self):
        BrokerAccountHealth.objects.create(account=self.acct)
        a2 = _acct(self.user, number="222")
        BrokerAccountHealth.objects.create(account=a2)
        with mock.patch.dict(os.environ, {"BROKER_HEALTH_QUOTA_PER_CYCLE": "1"}):
            res = sched.run_cycle(validator=lambda a: _attempt(a, "HEALTHY"))
        self.assertEqual(res["claimed"], 1)
        self.assertEqual(res["validated"], 1)

    def test_tombstoned_accounts_are_not_scheduled(self):
        BrokerAccountHealth.objects.create(account=self.acct, state=State.TOMBSTONED)
        res = sched.run_cycle(validator=lambda a: _attempt(a, "HEALTHY"))
        self.assertEqual(res["claimed"], 0)

    def test_not_due_accounts_are_skipped(self):
        future = timezone.now() + timedelta(hours=1)
        BrokerAccountHealth.objects.create(account=self.acct, next_check_at=future)
        res = sched.run_cycle(validator=lambda a: _attempt(a, "HEALTHY"))
        self.assertEqual(res["claimed"], 0)

    def test_one_bad_account_does_not_abort_cycle(self):
        BrokerAccountHealth.objects.create(account=self.acct)
        a2 = _acct(self.user, number="333")
        BrokerAccountHealth.objects.create(account=a2)

        def flaky(account):
            if account.pk == self.acct.pk:
                raise RuntimeError("boom")
            _attempt(account, "HEALTHY")

        res = sched.run_cycle(validator=flaky)
        self.assertEqual(res["errors"], 1)
        self.assertEqual(res["validated"], 1)  # the other account still processed


# ─── Isolation invariants: no runtime mutation / broker login / credential access ───
class IsolationInvariantTests(_Base):
    def test_engine_does_not_touch_account_trading_state(self):
        before = TradingAccount.objects.get(pk=self.acct.pk)
        for _ in range(3):
            _attempt(self.acct, "UNAVAILABLE")
        bh.record_validation_outcome(self.acct)
        after = TradingAccount.objects.get(pk=self.acct.pk)
        # WP3 reads validation_status/credentials; it must not WRITE them (that's WP1A's job).
        self.assertEqual(before.validation_status, after.validation_status)
        self.assertEqual(before.password_enc, after.password_enc)
        self.assertEqual(before.is_active, after.is_active)

    def test_source_has_no_execution_or_credential_coupling(self):
        import inspect

        src = inspect.getsource(bh) + inspect.getsource(sched)
        # Concrete coupling tokens (not prose): WP3 must not call execution or credential APIs.
        for forbidden in ("order_send", "create_open_trade_job", "ExecutionJob",
                          "decrypt(", ".password_enc", "destroy_customer_credential",
                          "resolve_secret", "order_check"):
            self.assertNotIn(forbidden, src,
                             f"WP3 engine must not reference {forbidden!r}")

    def test_engine_uses_row_level_locking(self):
        import inspect
        self.assertIn("select_for_update", inspect.getsource(bh))
        self.assertIn("skip_locked", inspect.getsource(sched))


# ─── get_contract read path ───
class ContractReadTests(_Base):
    def test_contract_none_before_first_record(self):
        self.assertIsNone(bh.get_contract(self.acct))  # no row yet

    def test_contract_shape(self):
        bh.record_validation_outcome(self.acct)
        c = bh.get_contract(self.acct)
        self.assertEqual(
            set(c.keys()),
            {"account_id", "state", "eligible", "pause_required",
             "resume_eligible", "reason_code", "state_version", "updated_at"})


# ─── Migration smoke: model + indexes exist ───
class MigrationSmokeTests(_Base):
    def test_table_and_convergence_fields_present(self):
        h = BrokerAccountHealth.objects.create(account=self.acct)
        h.refresh_from_db()
        for f in ("state", "reason_code", "resume_eligible", "state_version",
                  "last_consumed_attempt_id", "next_check_at"):
            self.assertTrue(hasattr(h, f))
        self.assertEqual(h.state, State.UNKNOWN)
