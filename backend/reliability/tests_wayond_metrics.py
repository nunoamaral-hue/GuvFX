"""Tests for the Wayond truthful-metrics foundation (pkt-151, ADR-0116).

Covers: measurable latency stages, absent timestamps -> N/A, negative/forward-skew fail-closed,
clock-domain labelling, partial coverage, percentile correctness, zero genuine samples, synthetic
exclusion, duplicate/stale classification, dead-letter aggregation, no broker-fill conflation,
no false zero / false success, value-blind (secret/PII suppression), staff authorization, and the
endpoint DARK flag (404 when OFF).
"""

import json
from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from reliability.services import wayond_metrics as WM
from signal_intake.models import (
    AcquiredMessage, ParserProfile, PendingSignalApproval, SignalProvider,
)

T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=dt_timezone.utc)


def _provider(slug="wayond", status="ARMED"):
    pp, _ = ParserProfile.objects.get_or_create(slug="wayond_v1")
    return SignalProvider.objects.create(slug=slug, parser_profile=pp, status=status)


def _acq(provider, i, *, tg=T0, acq=None, approval=None, outcome="INTAKEN", non_genuine=False, reason=""):
    """Create an AcquiredMessage then force the auto_now_add acquired_at / telegram_date."""
    m = AcquiredMessage.objects.create(
        provider=provider, chat_id="c", message_id=f"m{i}", outcome=outcome,
        non_genuine=non_genuine, reason=reason, approval=approval,
    )
    AcquiredMessage.objects.filter(pk=m.pk).update(
        acquired_at=(acq if acq is not None else tg), telegram_date=tg,
    )
    return m


def _approval(i, *, created=T0, reviewed=None, status="PENDING_APPROVAL", symbol=""):
    a = PendingSignalApproval.objects.create(
        source=WM.WAYOND_SOURCE, chat_id="c", message_id=f"a{i}", status=status, symbol=symbol,
        reviewed_at=reviewed,
    )
    PendingSignalApproval.objects.filter(pk=a.pk).update(created_at=created)
    return a


# ----------------------------------------------------------------------------------------------
# pure helpers
# ----------------------------------------------------------------------------------------------
class PureHelpers(TestCase):
    def test_percentile_nearest_rank(self):
        xs = list(range(1, 101))
        self.assertEqual(WM._percentile(xs, 0.50), 50.0)
        self.assertEqual(WM._percentile(xs, 0.95), 95.0)
        self.assertEqual(WM._percentile(xs, 0.99), 99.0)
        self.assertIsNone(WM._percentile([], 0.5))
        self.assertEqual(WM._percentile([7.0], 0.99), 7.0)

    def test_rate_never_false_zero(self):
        self.assertIsNone(WM._rate(0, 0))     # no denominator -> None, NEVER 0.0
        self.assertEqual(WM._rate(0, 5), 0.0)  # a genuine measured zero is fine
        self.assertEqual(WM._rate(1, 4), 0.25)

    def test_rate_tiny_positive_is_not_a_false_zero(self):
        # a rare-but-real event in a huge denominator must NOT render as 0.0
        for num, den in [(1, 60000), (2, 100000), (1, 50000)]:
            r = WM._rate(num, den)
            self.assertIsNotNone(r)
            self.assertGreater(r, 0.0, f"{num}/{den} rendered as a false zero")

    def test_norm_hold_reason_allowlist(self):
        self.assertEqual(WM._norm_hold_reason("missing_stop_loss"), "missing_stop_loss")
        self.assertEqual(WM._norm_hold_reason("promotion_rejected:SECRET_CODE"), "promotion_rejected")
        self.assertEqual(WM._norm_hold_reason("some free text with an account 1302587"), "other")
        self.assertEqual(WM._norm_hold_reason(""), "other")

    def test_skew_guard_downgrades_measured_to_partial(self):
        valid = [(T0, T0 + timedelta(milliseconds=100)) for _ in range(WM.MIN_SAMPLE + 1)]
        skew = [(T0 + timedelta(milliseconds=50), T0) for _ in range(WM.MIN_SAMPLE + 1)]  # >25% skew
        r = WM.latency_from_pairs("s", valid + skew, clock_domain=WM.CLK_CROSS, numerator="n", denominator="d")
        self.assertEqual(r["status"], WM.PARTIAL)
        self.assertEqual(r["na_reason"], "high_skew_exclusion")
        self.assertIsNone(r["p50_ms"])  # survivor-biased percentile withheld

    def test_min_sample_boundary_is_measured(self):
        r = WM.latency_from_pairs(
            "s", [(T0, T0 + timedelta(milliseconds=7))] * WM.MIN_SAMPLE,
            clock_domain="c", numerator="n", denominator="d")
        self.assertEqual(r["status"], WM.MEASURED)  # exactly MIN_SAMPLE qualifies
        self.assertEqual(r["p99_ms"], 7.0)

    def test_latency_measured_partial_na_and_skew(self):
        pairs = [(T0, T0 + timedelta(milliseconds=1000)) for _ in range(WM.MIN_SAMPLE + 5)]
        pairs.append((T0 + timedelta(milliseconds=5), T0))  # forward-skew -> excluded, NOT clamped to 0
        pairs.append((None, T0))                            # missing endpoint -> excluded
        r = WM.latency_from_pairs("s", pairs, clock_domain="c", numerator="n", denominator="d")
        self.assertEqual(r["status"], WM.MEASURED)
        self.assertEqual(r["excluded_skew"], 1)
        self.assertEqual(r["excluded_missing"], 1)
        self.assertEqual(r["p50_ms"], 1000.0)

        partial = WM.latency_from_pairs("s", [(T0, T0 + timedelta(milliseconds=3))] * 3,
                                        clock_domain="c", numerator="n", denominator="d")
        self.assertEqual(partial["status"], WM.PARTIAL)
        self.assertIsNone(partial["p50_ms"])  # percentile withheld below MIN_SAMPLE

        empty = WM.latency_from_pairs("s", [], clock_domain="c", numerator="n", denominator="d")
        self.assertEqual(empty["status"], WM.NA)
        self.assertIsNone(empty["p50_ms"])

    def test_absent_stage_is_unmeasurable_never_number(self):
        a = WM.absent_stage("broker_ack", reason="absent")
        self.assertEqual(a["status"], WM.UNMEASURABLE)
        self.assertIsNone(a["p50_ms"])
        self.assertEqual(a["valid_samples"], 0)


# ----------------------------------------------------------------------------------------------
# integration — zero genuine data: everything EMPTY/N/A, never a false zero or success
# ----------------------------------------------------------------------------------------------
class EmptyEstate(TestCase):
    def test_empty_estate_no_false_zero(self):
        out = WM.build_wayond_metrics(now=timezone.now())
        lat = {s["stage"]: s for s in out["latency_stages"]}
        self.assertEqual(lat["source_to_acquisition"]["status"], WM.NA)
        self.assertEqual(lat["parse_start_to_complete"]["status"], WM.UNMEASURABLE)
        q = out["quality"]
        self.assertEqual(q["messages_observed"]["status"], WM.EMPTY)
        # a success/fill/slippage figure must never be a fabricated 0 on an empty estate
        self.assertEqual(q["submission_success"]["status"], WM.NA)
        self.assertIsNone(q["submission_success"]["value"])
        self.assertEqual(q["broker_fill"]["status"], WM.NA)
        self.assertEqual(q["slippage"]["status"], WM.NA)
        self.assertEqual(q["duplicate_rate"]["status"], WM.NA)
        self.assertEqual(q["listener_liveness"]["status"], WM.NA)


# ----------------------------------------------------------------------------------------------
# integration — measurable stages + classification
# ----------------------------------------------------------------------------------------------
class MeasurableStages(TestCase):
    def setUp(self):
        self.p = _provider()

    def test_source_to_acquisition_measured_cross_domain(self):
        for i in range(WM.MIN_SAMPLE + 2):
            _acq(self.p, i, tg=T0, acq=T0 + timedelta(milliseconds=800))
        out = WM.build_wayond_metrics(now=T0 + timedelta(hours=1), window_days=30)
        stage = next(s for s in out["latency_stages"] if s["stage"] == "source_to_acquisition")
        self.assertEqual(stage["status"], WM.MEASURED)
        self.assertEqual(stage["clock_domain"], WM.CLK_CROSS)
        self.assertEqual(stage["p50_ms"], 800.0)

    def test_acquisition_to_approval_measured_via_fk(self):
        for i in range(WM.MIN_SAMPLE + 1):
            a = _approval(i, created=T0 + timedelta(milliseconds=400))
            _acq(self.p, i, tg=T0, acq=T0, approval=a)
        out = WM.build_wayond_metrics(now=T0 + timedelta(hours=1))
        stage = next(s for s in out["latency_stages"] if s["stage"] == "acquisition_to_approval_created")
        self.assertEqual(stage["status"], WM.MEASURED)
        self.assertEqual(stage["clock_domain"], WM.CLK_PLATFORM)
        self.assertEqual(stage["p50_ms"], 400.0)

    def test_approval_decision_time_measured(self):
        for i in range(WM.MIN_SAMPLE + 1):
            _approval(i, created=T0, reviewed=T0 + timedelta(seconds=30), status="APPROVED")
        out = WM.build_wayond_metrics(now=T0 + timedelta(hours=1))
        stage = next(s for s in out["latency_stages"] if s["stage"] == "approval_decision_time")
        self.assertEqual(stage["status"], WM.MEASURED)
        self.assertEqual(stage["p50_ms"], 30000.0)

    def test_negative_skew_rows_excluded_not_clamped(self):
        # acquired BEFORE the provider timestamp (impossible forward-skew) must be excluded.
        for i in range(5):
            _acq(self.p, i, tg=T0 + timedelta(seconds=10), acq=T0)  # acq < tg -> skew
        out = WM.build_wayond_metrics(now=T0 + timedelta(hours=1))
        stage = next(s for s in out["latency_stages"] if s["stage"] == "source_to_acquisition")
        self.assertEqual(stage["excluded_skew"], 5)
        self.assertEqual(stage["valid_samples"], 0)
        self.assertEqual(stage["status"], WM.NA)

    def test_outcome_breakdown_and_stale_rate(self):
        _acq(self.p, 1, outcome="INTAKEN")
        _acq(self.p, 2, outcome="STALE")
        _acq(self.p, 3, outcome="QUARANTINED")
        out = WM.build_wayond_metrics(now=T0 + timedelta(hours=1))
        q = out["quality"]["outcomes"]
        self.assertEqual(q["counts"]["STALE"], 1)
        self.assertEqual(q["stale_rate"], round(1 / 3, 4))
        self.assertEqual(out["quality"]["dead_letter"]["quarantined"], 1)

    def test_synthetic_nongenuine_excluded_from_genuine_cohort(self):
        _acq(self.p, 1, outcome="INTAKEN", non_genuine=False)
        _acq(self.p, 2, outcome="DROPPED_NOT_ARMED", non_genuine=True)
        out = WM.build_wayond_metrics(now=T0 + timedelta(hours=1))
        m = out["quality"]["messages_observed"]
        self.assertEqual(m["genuine_observed"], 1)
        self.assertEqual(m["non_genuine_observed"], 1)
        self.assertEqual(out["quality"]["dead_letter"]["dropped_not_armed"], 1)

    def test_approval_rate(self):
        _approval(1, status="APPROVED")
        _approval(2, status="REJECTED")
        _approval(3, status="PENDING_APPROVAL")
        out = WM.build_wayond_metrics(now=T0 + timedelta(hours=1))
        ap = out["quality"]["approvals"]
        self.assertEqual(ap["approval_rate"], 0.5)
        self.assertEqual(ap["pending"], 1)

    def test_provider_scope_isolation_excludes_non_wayond(self):
        # a second, non-Wayond provider's acquisitions must NOT be counted under the Wayond banner
        other_pp = ParserProfile.objects.create(slug="generic_v1")
        other = SignalProvider.objects.create(slug="otherprov", parser_profile=other_pp, status="ARMED")
        _acq(self.p, 1, outcome="INTAKEN")              # Wayond
        _acq(other, 2, outcome="INTAKEN")               # NOT Wayond
        _acq(other, 3, outcome="STALE")                 # NOT Wayond
        out = WM.build_wayond_metrics(now=T0 + timedelta(hours=1))
        self.assertEqual(out["quality"]["messages_observed"]["genuine_observed"], 1)
        slugs = [p["slug"] for p in out["provider_coverage"]["providers"]]
        self.assertIn("wayond", slugs)
        self.assertNotIn("otherprov", slugs)

    def test_cohort_is_always_genuine_no_synthetic_toggle(self):
        # the lying include_synthetic toggle was removed; the label must always say 'genuine'
        out = WM.build_wayond_metrics(now=T0 + timedelta(hours=1))
        self.assertEqual(out["cohort"], "genuine")

    def test_broker_fill_kept_separate_and_absent(self):
        out = WM.build_wayond_metrics(now=timezone.now())
        lat = {s["stage"]: s for s in out["latency_stages"]}
        # the ack->fill and submission->ack stages are ABSENT in their own right
        self.assertEqual(lat["broker_ack_to_fill"]["status"], WM.UNMEASURABLE)
        self.assertEqual(lat["submission_to_broker_ack"]["status"], WM.UNMEASURABLE)
        self.assertTrue(out["boundaries"]["broker_fill_separate_from_submission"])


# ----------------------------------------------------------------------------------------------
# value-blindness: no message bodies / handles / account / email in the output
# ----------------------------------------------------------------------------------------------
class ValueBlind(TestCase):
    def test_no_secret_or_pii_in_output(self):
        p = _provider()
        a = _approval(1, status="APPROVED", symbol="XAUUSD")
        _acq(p, 1, approval=a, reason="SECRET_free_text_reason_should_not_leak")
        # stash secret-shaped values where a naive serializer might pick them up
        AcquiredMessage.objects.filter(approval=a).update(
            raw_payload={"text": "BUY GOLD secret body", "chat": "@handle", "account": "1302587"},
        )
        blob = json.dumps(WM.build_wayond_metrics(now=T0 + timedelta(hours=1)), default=str)
        for secret in ("SECRET_free_text", "secret body", "@handle", "1302587", "BUY GOLD"):
            self.assertNotIn(secret, blob)
        self.assertTrue(json.loads(blob)["value_blind"])


# ----------------------------------------------------------------------------------------------
# endpoint: staff-only + DARK flag (404 when OFF)
# ----------------------------------------------------------------------------------------------
class Endpoint(TestCase):
    URL = "/api/reliability/wayond-metrics/"

    def setUp(self):
        from rest_framework.test import APIClient
        User = get_user_model()
        self.staff = User.objects.create_user(
            username="wm_staff", email="s@x.invalid", password="x", is_staff=True)
        self.member = User.objects.create_user(
            username="wm_member", email="m@x.invalid", password="x", is_staff=False)
        self.api = APIClient()

    def test_unauth_is_rejected(self):
        self.assertIn(self.api.get(self.URL).status_code, (401, 403))

    def test_non_staff_forbidden_even_when_flag_on(self):
        self.api.force_authenticate(user=self.member)
        with override_settings(WAYOND_METRICS_OPERATOR_ENABLED="1"):
            self.assertEqual(self.api.get(self.URL).status_code, 403)

    def test_staff_dark_404_when_flag_off(self):
        self.api.force_authenticate(user=self.staff)
        self.assertEqual(self.api.get(self.URL).status_code, 404)

    def test_staff_200_when_flag_on(self):
        self.api.force_authenticate(user=self.staff)
        with override_settings(WAYOND_METRICS_OPERATOR_ENABLED="1"):
            resp = self.api.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["schema"], "wayond_truthful_metrics.v1")
