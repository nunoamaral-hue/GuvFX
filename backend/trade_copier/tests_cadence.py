"""Reconciliation-cadence + detection-latency certification (ADR-0071). Pure — no DB, no I/O.

Certifies the bounded cadence contract: failures only LENGTHEN the interval (never a request storm), jitter
stays within the band, sub-second polling is refused without a REAL measured capacity profile, and poll-path
latency never fabricates a true broker-event latency.
"""
from datetime import datetime, timedelta, timezone as _tz

from django.test import SimpleTestCase

from trade_copier.cadence import (
    CadenceError, CapacityProfile, ReconciliationCadence, SUBSECOND_FLOOR_MS,
    measure_latency, plan_next_delay,
)

T = datetime(2026, 11, 3, 12, 0, 0, tzinfo=_tz.utc)


class CadenceConfigTests(SimpleTestCase):
    def test_default_cadence_is_not_subsecond(self):
        c = ReconciliationCadence()
        self.assertFalse(c.is_subsecond())
        self.assertGreaterEqual(c.min_interval_ms, SUBSECOND_FLOOR_MS)

    def test_bounds_out_of_order_raise(self):
        with self.assertRaises(CadenceError):
            ReconciliationCadence(min_interval_ms=5000, max_interval_ms=1000, base_interval_ms=2000)

    def test_base_outside_band_raises(self):
        with self.assertRaises(CadenceError):
            ReconciliationCadence(min_interval_ms=1000, max_interval_ms=5000, base_interval_ms=9000)

    def test_backoff_below_one_raises(self):
        with self.assertRaises(CadenceError):
            ReconciliationCadence(backoff_factor=0.5)

    def test_jitter_out_of_range_raises(self):
        with self.assertRaises(CadenceError):
            ReconciliationCadence(jitter_frac=1.0)


class PlanNextDelayTests(SimpleTestCase):
    def test_no_failures_no_rng_returns_base(self):
        c = ReconciliationCadence(base_interval_ms=2000)
        self.assertEqual(plan_next_delay(c, consecutive_failures=0), 2000)

    def test_backoff_is_monotonic_nondecreasing_and_capped(self):
        c = ReconciliationCadence(base_interval_ms=1000, min_interval_ms=1000, max_interval_ms=16000,
                                  backoff_factor=2.0, degrade_after_failures=1)
        delays = [plan_next_delay(c, consecutive_failures=f) for f in range(0, 8)]
        for a, b in zip(delays, delays[1:]):
            self.assertLessEqual(a, b)                      # failures never shorten the interval
        self.assertLessEqual(max(delays), 16000)            # capped at max (no unbounded growth)
        self.assertEqual(delays[-1], 16000)

    def test_jitter_stays_within_band(self):
        c = ReconciliationCadence(base_interval_ms=2000, min_interval_ms=1000, max_interval_ms=4000,
                                  jitter_frac=0.5)
        low = plan_next_delay(c, consecutive_failures=0, rng=lambda: 0.0)
        high = plan_next_delay(c, consecutive_failures=0, rng=lambda: 0.999999)
        self.assertGreaterEqual(low, 1000)
        self.assertLessEqual(high, 4000)
        self.assertLess(low, high)                          # jitter actually spreads the value


class SubsecondCapacityTests(SimpleTestCase):
    def test_subsecond_without_capacity_refused(self):
        c = ReconciliationCadence(base_interval_ms=500, min_interval_ms=250, max_interval_ms=2000)
        self.assertTrue(c.is_subsecond())
        ok, reason = c.validate_against_capacity(None)
        self.assertFalse(ok)
        self.assertEqual(reason, "subsecond_requires_measured_capacity")

    def test_subsecond_with_unreal_capacity_refused(self):
        c = ReconciliationCadence(base_interval_ms=500, min_interval_ms=250, max_interval_ms=2000)
        unreal = CapacityProfile(sampled_reads=0, p95_read_latency_ms=10, max_safe_concurrent_reads=1)
        self.assertFalse(c.validate_against_capacity(unreal)[0])

    def test_subsecond_with_measured_capacity_allowed(self):
        c = ReconciliationCadence(base_interval_ms=400, min_interval_ms=250, max_interval_ms=2000)
        measured = CapacityProfile(sampled_reads=200, p95_read_latency_ms=100,
                                   max_safe_concurrent_reads=4, measured_at=T)
        ok, reason = c.validate_against_capacity(measured)   # 250ms >= 2 * 100ms → authorised
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_subsecond_exceeding_measured_capacity_refused(self):
        c = ReconciliationCadence(base_interval_ms=300, min_interval_ms=150, max_interval_ms=2000)
        measured = CapacityProfile(sampled_reads=200, p95_read_latency_ms=100,
                                   max_safe_concurrent_reads=4)
        ok, reason = c.validate_against_capacity(measured)   # 150ms < 2 * 100ms → refused
        self.assertFalse(ok)
        self.assertEqual(reason, "subsecond_exceeds_measured_capacity")

    def test_non_subsecond_always_admissible(self):
        self.assertEqual(ReconciliationCadence().validate_against_capacity(None), (True, "ok"))


class LatencyTests(SimpleTestCase):
    def test_poll_path_has_no_true_broker_latency(self):
        lat = measure_latency(source="POLL", observed_at=T, applied_at=T + timedelta(milliseconds=800))
        self.assertEqual(lat.detect_to_apply_ms, 800)
        self.assertIsNone(lat.true_broker_latency_ms)       # never fabricated for the poll path

    def test_event_path_reports_true_broker_latency(self):
        lat = measure_latency(source="EVENT", observed_at=T + timedelta(milliseconds=200),
                              applied_at=T + timedelta(milliseconds=500),
                              broker_event_time=T)
        self.assertEqual(lat.detect_to_apply_ms, 300)
        self.assertEqual(lat.true_broker_latency_ms, 500)

    def test_negative_delta_is_not_a_latency(self):
        lat = measure_latency(source="EVENT", observed_at=T + timedelta(seconds=1), applied_at=T,
                              broker_event_time=T + timedelta(seconds=2))
        self.assertIsNone(lat.detect_to_apply_ms)           # clock skew / reorder → not reported as latency
        self.assertIsNone(lat.true_broker_latency_ms)
