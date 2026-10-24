"""WS3 — withdrawal-speed analytics tests.

Proves the truthfulness rules: speed is measured ONLY from confidently-paired completed withdrawals (both anchors);
pending ages are separate and NEVER mixed into completed averages; a missing duration is never coerced to 0; a
completion-before-request is quarantined; per-broker aggregates respect the minimum-sample policy; the view is
owner-scoped and gated by the default-DENY cohort flag; and the genuine 24.12 REQUESTED pilot withdrawal stays an
in-progress (pending) record, never a fabricated completion.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from broker_intelligence.flags import broker_withdrawal_speed_enabled_for
from broker_intelligence.models import BrokerEvent, Provenance, Withdrawal
from broker_intelligence.withdrawal_speed import (MIN_BROKER_SAMPLE, MIN_HEADLINE_SAMPLE, data_quality,
                                                  summarize_durations, withdrawal_speed)
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "95%04d" % _n


def _user(email=None):
    login = _uniq()
    return User.objects.create_user(username="ws3%s" % login, email=(email or "%s@x.invalid" % login), password="x")


def _acct(user=None, broker="TradersWay", **kw):
    login = _uniq()
    u = user or _user()
    d = dict(user=u, name="A", broker_name=broker, account_number=login, is_demo=False, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


def _event(occurred_at, *, received_at=None, etype="WITHDRAWAL_COMPLETED", owner=None):
    """A minimal completion BrokerEvent. ``occurred_at`` None ⇒ broker did NOT state a time (so a completed_at derived
    from this event is ingestion-OBSERVED timing, not broker-stated)."""
    return BrokerEvent.objects.create(
        event_type=etype, occurred_at=occurred_at, received_at=(received_at or occurred_at or timezone.now()),
        provenance=Provenance.REAL, owner=owner)


def _wd(account, status, *, owner=None, amount="100.00", currency="USD", requested_at=None, completed_at=None,
        ref="", method=Withdrawal.CorrelationMethod.REFERENCE_ID, provenance=Provenance.REAL,
        completed_event=None, requested_event=None):
    return Withdrawal.objects.create(
        trading_account=account, owner=owner, broker_reference_id=ref,
        amount=(Decimal(amount) if amount is not None else None), currency=currency,
        requested_at=requested_at, completed_at=completed_at, status=status,
        correlation_method=method, provenance=provenance,
        completed_event=completed_event, requested_event=requested_event)


def _completed(account, secs, *, ref, owner=None, broker_stated=True, **kw):
    """A COMPLETED withdrawal that is ``secs`` long. By default the completion is BROKER-STATED (a completion event with
    its own occurred_at), so it is a confident speed sample. ``broker_stated=False`` attaches a completion event with NO
    occurred_at — an OBSERVED-timing completion that is disclosed but never charted/averaged."""
    now = timezone.now()
    ce = _event(occurred_at=(now if broker_stated else None), received_at=now, owner=(owner or account.user))
    return _wd(account, Withdrawal.Status.COMPLETED, owner=owner, requested_at=now - timedelta(seconds=secs),
               completed_at=now, ref=ref, completed_event=ce, **kw)


# ----------------------------------------------------------------------------------------------------------------
# Generic reusable helpers (future-proof contract)
# ----------------------------------------------------------------------------------------------------------------
class GenericContractHelpers(TestCase):
    def test_summarize_empty_is_all_none_never_zero(self):
        s = summarize_durations([])
        self.assertEqual(s["count"], 0)
        self.assertIsNone(s["median_seconds"])
        self.assertIsNone(s["average_seconds"])
        self.assertFalse(s["representative"])

    def test_summarize_median_avg_fastest_slowest(self):
        s = summarize_durations([10, 20, 60])   # median 20, mean 30
        self.assertEqual(s["median_seconds"], 20)
        self.assertEqual(s["average_seconds"], 30)
        self.assertEqual(s["fastest_seconds"], 10)
        self.assertEqual(s["slowest_seconds"], 60)
        self.assertTrue(s["representative"])

    def test_representative_requires_min_sample(self):
        self.assertFalse(summarize_durations([10, 20])["representative"])        # n=2 < 3
        self.assertTrue(summarize_durations([10, 20, 30])["representative"])      # n=3

    def test_data_quality_bands(self):
        self.assertEqual(data_quality(0), "insufficient")
        self.assertEqual(data_quality(1), "insufficient")
        self.assertEqual(data_quality(2), "limited")
        self.assertEqual(data_quality(MIN_BROKER_SAMPLE), "sufficient")

    def test_missing_values_not_zeroed(self):
        self.assertEqual(summarize_durations([None, 10, None])["count"], 1)


# ----------------------------------------------------------------------------------------------------------------
# Speed projection
# ----------------------------------------------------------------------------------------------------------------
class WithdrawalSpeedProjection(TestCase):
    def test_median_average_fastest_slowest_over_completed_samples(self):
        a = _acct()
        _completed(a, 10, ref="c1")
        _completed(a, 20, ref="c2")
        _completed(a, 60, ref="c3")
        v = withdrawal_speed([a])
        self.assertEqual(v["headline"]["state"], "ok")
        self.assertEqual(v["headline"]["median_seconds"], 20)
        self.assertEqual(v["headline"]["average_seconds"], 30)
        self.assertEqual(v["summary"]["fastest_seconds"], 10)
        self.assertEqual(v["summary"]["slowest_seconds"], 60)
        self.assertEqual(v["summary"]["completed_with_duration_count"], 3)
        self.assertEqual(len(v["series"]), 3)

    def test_insufficient_sample_shows_low_data_not_hidden(self):
        a = _acct()
        _completed(a, 42, ref="c1")
        v = withdrawal_speed([a])
        self.assertEqual(v["headline"]["state"], "insufficient")   # 1 < MIN_HEADLINE_SAMPLE
        self.assertEqual(v["headline"]["sample_count"], 1)
        self.assertEqual(v["headline"]["median_seconds"], 42)      # the observation is NOT hidden
        self.assertEqual(len(v["series"]), 1)                       # still visible in the trend

    def test_empty_when_no_completed_sample_never_zero(self):
        a = _acct()
        _wd(a, Withdrawal.Status.REQUESTED, ref="r1")
        v = withdrawal_speed([a])
        self.assertEqual(v["headline"]["state"], "empty")
        self.assertIsNone(v["headline"]["median_seconds"])
        self.assertIsNone(v["summary"]["average_seconds"])
        self.assertEqual(v["series"], [])

    def test_pending_excluded_from_completed_but_has_live_elapsed(self):
        a = _acct()
        now = timezone.now()
        _wd(a, Withdrawal.Status.REQUESTED, requested_at=now - timedelta(hours=3), ref="p1")
        _completed(a, 30, ref="c1")
        v = withdrawal_speed([a], now=now)
        self.assertEqual(v["summary"]["in_progress_count"], 1)
        self.assertEqual(v["summary"]["completed_with_duration_count"], 1)
        self.assertEqual(len(v["in_progress"]), 1)
        # Live elapsed ≈ 3h, computed from now − requested_at; NOT part of any completed figure.
        self.assertAlmostEqual(v["in_progress"][0]["elapsed_seconds"], 3 * 3600, delta=5)
        self.assertEqual(v["headline"]["median_seconds"], 30)   # only the completed sample

    def test_completed_without_both_anchors_is_not_a_sample_and_not_zero(self):
        a = _acct()
        # completed but no requested_at anchor → unmeasurable, counted completed but NOT a speed sample
        _wd(a, Withdrawal.Status.COMPLETED, requested_at=None, completed_at=timezone.now(), ref="c1")
        v = withdrawal_speed([a])
        self.assertEqual(v["summary"]["completed_count"], 1)
        self.assertEqual(v["summary"]["completed_with_duration_count"], 0)
        self.assertEqual(v["summary"]["untimed_count"], 1)       # disclosed, not a sample, not zeroed
        self.assertEqual(v["headline"]["state"], "empty")
        self.assertIsNone(v["headline"]["median_seconds"])       # never a fabricated 0
        self.assertEqual(v["series"], [])

    def test_completion_before_request_is_quarantined(self):
        a = _acct()
        now = timezone.now()
        # completed_at precedes requested_at → impossible → excluded + quarantined
        _wd(a, Withdrawal.Status.COMPLETED, requested_at=now, completed_at=now - timedelta(hours=1), ref="c1")
        _completed(a, 50, ref="c2")
        v = withdrawal_speed([a], now=now)
        self.assertEqual(v["summary"]["quarantined_impossible_count"], 1)
        self.assertEqual(v["summary"]["completed_with_duration_count"], 1)   # only the valid one
        self.assertEqual(len(v["series"]), 1)
        self.assertEqual(v["series"][0]["duration_seconds"], 50)

    def test_zero_second_interval_is_valid_fast_not_impossible(self):
        a = _acct()
        now = timezone.now()
        _wd(a, Withdrawal.Status.COMPLETED, requested_at=now, completed_at=now, ref="c1",
            completed_event=_event(occurred_at=now))
        v = withdrawal_speed([a], now=now)
        self.assertEqual(v["summary"]["quarantined_impossible_count"], 0)
        self.assertEqual(v["summary"]["completed_with_duration_count"], 1)
        self.assertEqual(v["series"][0]["duration_seconds"], 0)

    def test_per_broker_comparison_respects_min_sample(self):
        u = _user()
        fast = _acct(user=u, broker="FastBroker")
        slow = _acct(user=u, broker="SlowBroker")
        for i in range(3):
            _completed(fast, 10 + i, ref="f%d" % i)          # 3 samples → sufficient
        _completed(slow, 9000, ref="s1")                      # 1 sample → insufficient
        v = withdrawal_speed([fast, slow], owner=u)
        by = {b["broker"]: b for b in v["brokers"]}
        self.assertEqual(by["FastBroker"]["data_quality"], "sufficient")
        self.assertEqual(by["FastBroker"]["sample_count"], 3)
        self.assertIsNotNone(by["FastBroker"]["median_seconds"])
        self.assertEqual(by["SlowBroker"]["data_quality"], "insufficient")
        self.assertEqual(by["SlowBroker"]["sample_count"], 1)

    def test_currency_preserved_no_fx_conversion(self):
        a = _acct()
        now = timezone.now()
        _wd(a, Withdrawal.Status.COMPLETED, amount="100.00", currency="USD",
            requested_at=now - timedelta(seconds=10), completed_at=now, ref="u1",
            completed_event=_event(occurred_at=now))
        _wd(a, Withdrawal.Status.COMPLETED, amount="80.00", currency="GBP",
            requested_at=now - timedelta(seconds=30), completed_at=now, ref="g1",
            completed_event=_event(occurred_at=now))
        v = withdrawal_speed([a], now=now)
        # Duration is currency-agnostic: both contribute seconds; amounts keep their own currency (never summed/FX).
        self.assertEqual(v["summary"]["completed_with_duration_count"], 2)
        self.assertEqual(set(v["currencies"]), {"USD", "GBP"})
        amts = {(r["currency"], r["amount"]) for r in v["series"]}
        self.assertEqual(amts, {("USD", "100.00"), ("GBP", "80.00")})

    def test_match_basis_reference_vs_heuristic(self):
        a = _acct()
        _completed(a, 10, ref="c1", method=Withdrawal.CorrelationMethod.REFERENCE_ID)
        _completed(a, 20, ref="", method=Withdrawal.CorrelationMethod.HEURISTIC)
        v = withdrawal_speed([a])
        bases = sorted(r["match_basis"] for r in v["series"])
        self.assertEqual(bases, ["heuristic", "reference"])
        # Confidence disclosure: the headline carries the reference vs heuristic split so the UI can caveat.
        self.assertEqual(v["headline"]["reference_count"], 1)
        self.assertEqual(v["headline"]["heuristic_count"], 1)

    def test_observed_timing_completion_excluded_from_headline_but_disclosed(self):
        """P1 fix: a completion whose time is only OBSERVED (the completion event carried no broker-stated occurred_at,
        so completed_at fell back to the ingestion time) could be inflated by a backfill/outage lag — so it is NEVER
        charted or averaged; it is disclosed by count. A broker-stated completion in the same set still counts."""
        a = _acct()
        _completed(a, 30, ref="c1", broker_stated=True)    # charted + averaged
        _completed(a, 99999, ref="c2", broker_stated=False)  # observed-only → excluded from headline/series
        v = withdrawal_speed([a])
        self.assertEqual(v["summary"]["completed_with_duration_count"], 1)   # only the broker-stated one
        self.assertEqual(v["summary"]["observed_timing_count"], 1)           # the other is disclosed, not charted
        self.assertEqual(len(v["series"]), 1)
        self.assertEqual(v["series"][0]["duration_seconds"], 30)             # the 99999 never enters the chart
        self.assertEqual(v["headline"]["median_seconds"], 30)               # not inflated by the observed-timing row

    def test_future_requested_at_elapsed_is_unavailable_not_zero(self):
        """P3 fix: a pending withdrawal whose requested_at is in the FUTURE (clock skew / mis-parsed time) yields a
        null elapsed (rendered N/A), never a misleading 0 presented as a just-made request."""
        a = _acct()
        now = timezone.now()
        _wd(a, Withdrawal.Status.REQUESTED, requested_at=now + timedelta(hours=2), ref="p1")
        v = withdrawal_speed([a], now=now)
        self.assertEqual(len(v["in_progress"]), 1)
        self.assertIsNone(v["in_progress"][0]["elapsed_seconds"])

    def test_genuine_only_excludes_synthetic(self):
        a = _acct()
        _completed(a, 10, ref="c1", provenance=Provenance.REAL)
        _completed(a, 20, ref="c2", provenance=Provenance.SYNTHETIC)
        v = withdrawal_speed([a])
        self.assertEqual(v["summary"]["completed_with_duration_count"], 1)   # only the REAL one
        self.assertEqual(v["excluded_non_genuine_count"], 1)
        self.assertFalse(v["demonstration"])

    def test_tenant_isolation_owner_scoped(self):
        a = _acct()
        other = _acct()   # different user + account
        _completed(a, 10, ref="c1")
        _completed(other, 999, ref="x1")
        v = withdrawal_speed([a], owner=a.user)
        self.assertEqual(v["summary"]["completed_with_duration_count"], 1)
        self.assertEqual(v["series"][0]["duration_seconds"], 10)   # never the other owner's 999

    def test_genuine_pilot_requested_stays_in_progress_not_completed(self):
        """The real 24.12 USD / REQUESTED TradersWay withdrawal must remain an in-progress record — never fabricated
        into a completion, never a speed sample. Headline is empty until a genuine completion is observed."""
        a = _acct(broker="TradersWay")
        now = timezone.now()
        _wd(a, Withdrawal.Status.REQUESTED, amount="24.12", currency="USD",
            requested_at=now - timedelta(minutes=33), ref="4114051")
        v = withdrawal_speed([a], now=now)
        self.assertEqual(v["headline"]["state"], "empty")
        self.assertEqual(v["summary"]["completed_with_duration_count"], 0)
        self.assertEqual(len(v["in_progress"]), 1)
        self.assertEqual(v["in_progress"][0]["amount"], "24.12")
        self.assertEqual(v["in_progress"][0]["currency"], "USD")
        self.assertAlmostEqual(v["in_progress"][0]["elapsed_seconds"], 33 * 60, delta=5)


# ----------------------------------------------------------------------------------------------------------------
# Flag gate
# ----------------------------------------------------------------------------------------------------------------
class SpeedCohortGate(TestCase):
    def test_default_deny(self):
        u = _user("deny@x.invalid")
        self.assertFalse(broker_withdrawal_speed_enabled_for(u))

    @override_settings(BROKER_WITHDRAWAL_SPEED_OWNER_ALLOWLIST="cohort@x.invalid")
    def test_allowlist_includes_only_listed(self):
        self.assertTrue(broker_withdrawal_speed_enabled_for(_user("cohort@x.invalid")))
        self.assertFalse(broker_withdrawal_speed_enabled_for(_user("outsider@x.invalid")))

    @override_settings(BROKER_WITHDRAWAL_SPEED_ENABLED="1")
    def test_ga_global_enables_everyone(self):
        self.assertTrue(broker_withdrawal_speed_enabled_for(_user("anyone@x.invalid")))

    def test_anonymous_denied(self):
        class Anon:
            is_authenticated = False
            email = ""
        self.assertFalse(broker_withdrawal_speed_enabled_for(Anon()))
        self.assertFalse(broker_withdrawal_speed_enabled_for(None))


# ----------------------------------------------------------------------------------------------------------------
# View (owner-scoped, DARK-gated)
# ----------------------------------------------------------------------------------------------------------------
class SpeedView(TestCase):
    URL = "/api/broker-intelligence/withdrawals/speed/"

    def test_404_when_cohort_off(self):
        u = _user("off@x.invalid")
        c = APIClient()
        c.force_authenticate(u)
        self.assertEqual(c.get(self.URL).status_code, 404)

    @override_settings(BROKER_WITHDRAWAL_SPEED_OWNER_ALLOWLIST="on@x.invalid")
    def test_200_and_owner_scoped_when_cohort_on(self):
        u = _user("on@x.invalid")
        a = _acct(user=u)
        _completed(a, 15, ref="c1")
        # another member's data must never appear
        other = _acct()
        _completed(other, 777, ref="x1")
        c = APIClient()
        c.force_authenticate(u)
        r = c.get(self.URL)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["metric"]["key"], "withdrawal_processing_time")
        self.assertEqual(body["summary"]["completed_with_duration_count"], 1)
        self.assertEqual(body["series"][0]["duration_seconds"], 15)

    @override_settings(BROKER_WITHDRAWAL_SPEED_OWNER_ALLOWLIST="on@x.invalid")
    def test_requires_authentication(self):
        self.assertIn(APIClient().get(self.URL).status_code, (401, 403))
