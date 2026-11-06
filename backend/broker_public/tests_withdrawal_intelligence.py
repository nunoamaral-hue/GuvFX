"""broker_public — Stream C withdrawal-intelligence aggregation + disclosure tests (ADR-0080).

SYNTHETIC ONLY. Adversarial fixture matrix: eligible multi-account cohort; below observation threshold; below
distinct-account threshold; single-account dominance; incomplete timestamps; confirmation-required flow; failed &
expired; ambiguous broker identity; uncertified; mixed currencies + k-anon; malicious strings / PII-like content;
differencing (non-published stub carries no counts); corrected/idempotent recount; cross-tenant contamination;
genuine-only + account-resolved only; and PII-free / no-exact-timestamp output. Plus direct unit tests of the central
disclosure layer. No real withdrawal, email, account or amount appears anywhere here.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from broker_intelligence.models import Provenance, Withdrawal
from broker_public import disclosure
from broker_public.disclosure import Reason, State
from broker_public.models import BrokerMetricCategory, PublicBroker
from broker_public.withdrawal_intelligence import CATEGORY_KEY, compute_withdrawal_intelligence
from trading.models import TradingAccount

User = get_user_model()
CERT = frozenset({"Pepperstone"})
_n = 0


def _uniq():
    global _n
    _n += 1
    return "70%04d" % _n


def _broker(slug="pepperstone", source="Pepperstone", published=True, display_name="Pepperstone"):
    return PublicBroker.objects.create(slug=slug, display_name=display_name, source_slug=source,
                                       is_published=published)


def _category(*, min_contributors=3, min_samples=5, min_completed=5, max_share=40, min_bucket=5, version=1):
    return BrokerMetricCategory.objects.create(
        key=CATEGORY_KEY, status=BrokerMetricCategory.Status.ACTIVE, methodology_version=version,
        methodology="stream c test category", min_contributors=min_contributors, min_samples=min_samples,
        min_completed_samples=min_completed, max_contributor_share_pct=max_share, min_bucket_size=min_bucket)


def _acct(broker="Pepperstone"):
    login = _uniq()
    u = User.objects.create_user(username="wi%s" % login, email="%s@x.invalid" % login, password="x")
    return TradingAccount.objects.create(user=u, name="A", broker_name=broker, account_number=login,
                                         is_demo=False, is_active=True)


def _wd(acct, *, status=Withdrawal.Status.COMPLETED, req_h=10, conf_h=9, comp_h=8, confirmation_required=False,
        currency="USD", provenance=Provenance.REAL, ref=""):
    now = timezone.now()

    def at(h):
        return now - timedelta(hours=h) if h is not None else None

    completed_at = at(comp_h) if status == Withdrawal.Status.COMPLETED else None
    return Withdrawal.objects.create(
        trading_account=acct, status=status, provenance=provenance, broker_reference_id=ref,
        amount=Decimal("100.00"), currency=currency,
        requested_at=at(req_h),
        confirmation_required_at=(at(req_h) if confirmation_required else None),
        confirmed_at=at(conf_h),
        completed_at=completed_at)


def _published_cohort(broker_name="Pepperstone", n_accounts=3, per=(2, 2, 1), **wd_kwargs):
    """A cohort that clears the default floors (3 contributors, 5 observations, each fully laddered COMPLETED)."""
    accts = [_acct(broker_name) for _ in range(n_accounts)]
    for acct, k in zip(accts, per):
        for _ in range(k):
            _wd(acct, **wd_kwargs)
    return accts


class WithdrawalIntelligenceCompute(TestCase):
    def setUp(self):
        _category()

    def test_eligible_multi_account_cohort_publishes_three_separate_stages(self):
        pb = _broker()
        _published_cohort()
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"], {"state": State.PUBLISHED, "reason": Reason.OK})
        self.assertEqual(r["sample_sufficiency"]["contributors"], 3)
        self.assertEqual(r["sample_sufficiency"]["observations"], 5)
        speed = r["withdrawal_speed"]
        # three SEPARATE stages, each published from its own both-anchor subset
        for stage in ("request_to_confirmation", "confirmation_to_completion", "total_request_to_completion"):
            self.assertEqual(speed[stage]["state"], State.PUBLISHED, stage)
            self.assertEqual(speed[stage]["count"], 5)
            self.assertIn("median_seconds", speed[stage])
        # total (req->completed) is 2h; req->confirmed is 1h; confirmed->completed is 1h — never summed/combined
        self.assertEqual(speed["total_request_to_completion"]["median_seconds"], 2 * 3600)
        self.assertEqual(speed["request_to_confirmation"]["median_seconds"], 1 * 3600)
        self.assertEqual(speed["confirmation_to_completion"]["median_seconds"], 1 * 3600)
        rates = r["lifecycle_outcomes"]["rates"]
        self.assertEqual(rates["completion_pct"], 100)
        self.assertEqual(rates["denominator"], 5)
        self.assertIn("completed + rejected + cancelled + expired + failed", rates["denominator_definition"])

    def test_below_observation_threshold_suppressed(self):
        pb = _broker()
        for _ in range(4):
            _wd(_acct())   # 4 distinct accounts, 4 observations (< min_samples 5)
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"], {"state": State.SUPPRESSED, "reason": Reason.BELOW_MIN_SAMPLES})

    def test_below_distinct_account_threshold_suppressed(self):
        # Raise the contributor floor to 5 for this case: the 40%-share winsorise inherently requires >=3 distinct
        # contributors to survive, so a 3-contributor cohort below a floor of 5 is the clean BELOW_MIN_CONTRIBUTORS case.
        BrokerMetricCategory.objects.filter(key=CATEGORY_KEY).update(min_contributors=5)
        pb = _broker()
        for a in (_acct(), _acct(), _acct()):   # 3 distinct accounts (< min_contributors 5), 2 obs each
            _wd(a)
            _wd(a)
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"]["state"], State.SUPPRESSED)
        self.assertEqual(r["evidence_status"]["reason"], Reason.BELOW_MIN_CONTRIBUTORS)

    def test_single_account_dominance_is_reason_coded(self):
        pb = _broker()
        a, b, c = _acct(), _acct(), _acct()
        for _ in range(20):
            _wd(a)
        _wd(b)
        _wd(c)   # raw cohort (3 contributors, 22 obs) WOULD pass, but one account dominates -> winsorise collapses it
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"]["state"], State.SUPPRESSED)
        self.assertEqual(r["evidence_status"]["reason"], Reason.SINGLE_ACCOUNT_DOMINANCE)

    def test_incomplete_timestamps_stage_unavailable_but_total_publishes(self):
        pb = _broker()
        _published_cohort(conf_h=None)   # no confirmation anchor -> stage1/stage2 have no both-anchor pairs
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"]["state"], State.PUBLISHED)
        speed = r["withdrawal_speed"]
        self.assertEqual(speed["request_to_confirmation"]["state"], State.UNAVAILABLE)
        self.assertEqual(speed["request_to_confirmation"]["reason"], Reason.STAGE_ANCHORS_MISSING)
        self.assertEqual(speed["confirmation_to_completion"]["state"], State.UNAVAILABLE)
        self.assertEqual(speed["total_request_to_completion"]["state"], State.PUBLISHED)
        self.assertNotIn("count", speed["request_to_confirmation"])   # a withheld stage carries NO number

    def test_confirmation_required_flow_recorded(self):
        pb = _broker()
        _published_cohort(confirmation_required=True)
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        flow = r["lifecycle_outcomes"]["confirmation_required_flow"]
        self.assertEqual(flow["required_confirmation_step"], 5)
        self.assertEqual(flow.get("no_confirmation_step", 0), 0)   # 0-count bucket drops out of the k-anon vector

    def test_rare_terminal_outcomes_are_k_anonymised(self):
        # A small mixed cohort: rare terminal outcomes (count 1) must NOT appear as exact lifecycle counts OR as
        # invertible rates — they fold into the k-anon 'suppressed' group. This holds with NO suppression flag armed.
        pb = _broker()
        a, b, c = _acct(), _acct(), _acct()
        _wd(a)
        _wd(a)                                            # 2 completed
        _wd(b, status=Withdrawal.Status.EXPIRED)
        _wd(b, status=Withdrawal.Status.FAILED)
        _wd(c, status=Withdrawal.Status.REJECTED)
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"]["state"], State.PUBLISHED)
        counts = r["lifecycle_outcomes"]["counts"]
        for rare in ("terminal_expired", "terminal_failed", "terminal_rejected", "broker_reported_completed"):
            self.assertNotIn(rare, counts)               # every sub-threshold bucket folded, never an exact 1..4
        rates = r["lifecycle_outcomes"]["rates"]
        self.assertNotIn("expired_pct", rates)           # a rare outcome rate is never published as an exact pct
        self.assertIn("suppressed_pct", rates)
        self.assertEqual(rates["denominator"], 5)

    def test_outcome_rates_published_for_large_cohort(self):
        pb = _broker()
        for _ in range(8):
            _wd(_acct(), status=Withdrawal.Status.COMPLETED)   # 8 completed (distinct accounts)
        for _ in range(5):
            _wd(_acct(), status=Withdrawal.Status.REJECTED)    # 5 rejected (distinct accounts)
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"]["state"], State.PUBLISHED)
        rates = r["lifecycle_outcomes"]["rates"]
        self.assertEqual(rates["denominator"], 13)
        self.assertEqual(rates["completion_pct"], 62)          # round(100*8/13); both groups clear the k-anon floor
        self.assertEqual(rates["failed_pct"], 38)

    def test_stage_dominated_by_one_contributor_is_withheld(self):
        # The cohort clears its floors, but EVERY completed-with-both-anchors observation is from ONE member, so the
        # total-stage figure would be that member's exact durations — it must be withheld (per-stage contributor floor).
        pb = _broker()
        a, b, c = _acct(), _acct(), _acct()
        for _ in range(5):
            _wd(a, status=Withdrawal.Status.COMPLETED)
        for _ in range(4):
            _wd(b, status=Withdrawal.Status.REQUESTED, conf_h=None)   # requested-only, no completion anchor
        for _ in range(4):
            _wd(c, status=Withdrawal.Status.REQUESTED, conf_h=None)
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"]["state"], State.PUBLISHED)   # 3 contributors / 13 obs
        total = r["withdrawal_speed"]["total_request_to_completion"]
        self.assertEqual(total["state"], State.PARTIAL)
        self.assertEqual(total["reason"], Reason.BELOW_MIN_COMPLETED)
        self.assertNotIn("median_seconds", total)                         # no number for a one-member stage

    def test_ambiguous_broker_identity_unavailable(self):
        pb = _broker(source="")   # no authoritative internal key
        _published_cohort()
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"], {"state": State.UNAVAILABLE, "reason": Reason.AMBIGUOUS_BROKER_IDENTITY})

    def test_uncertified_broker_unavailable(self):
        pb = _broker()
        _published_cohort()
        r = compute_withdrawal_intelligence(pb, certified_brokers=frozenset())   # prod default
        self.assertEqual(r["evidence_status"], {"state": State.UNAVAILABLE, "reason": Reason.NOT_CERTIFIED})

    def test_no_eligible_evidence_unavailable(self):
        pb = _broker()
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"], {"state": State.UNAVAILABLE, "reason": Reason.NO_ELIGIBLE_EVIDENCE})

    def test_mixed_currencies_counts_only_no_amounts(self):
        pb = _broker()
        accts = _published_cohort()
        _wd(accts[0], currency="EUR")   # add a 6th obs in a different currency (owner already has 2 -> winsorised out)
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        ccy = r["per_currency_counts"]
        self.assertTrue(all(isinstance(v, int) for v in ccy.values()))
        self.assertNotIn("amount", str(ccy))

    def test_minority_currency_is_always_suppressed(self):
        # Stream C k-anon is UNCONDITIONAL (no suppression flag needed): a lone currency is never an exact count.
        pb = _broker()
        accts = [_acct() for _ in range(6)]
        for a in accts[:5]:
            _wd(a, currency="USD")
        _wd(accts[5], currency="EUR")
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"]["state"], State.PUBLISHED)
        self.assertNotIn("EUR", r["per_currency_counts"])

    def test_cross_tenant_contamination_excluded(self):
        pb = _broker()   # Pepperstone
        _published_cohort("Pepperstone")
        other = _acct("Vantage")
        _wd(other, currency="JPY")   # a DIFFERENT broker's withdrawal must never enter Pepperstone's aggregate
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["sample_sufficiency"]["observations"], 5)   # the Vantage row is not counted
        self.assertNotIn("JPY", r["per_currency_counts"])

    def test_synthetic_and_account_unresolved_excluded(self):
        pb = _broker()
        _published_cohort()   # 5 genuine
        _wd(_acct(), provenance=Provenance.SYNTHETIC)   # synthetic -> excluded
        # account-UNRESOLVED (no trading_account) -> excluded
        Withdrawal.objects.create(trading_account=None, owner=None, status=Withdrawal.Status.COMPLETED,
                                  provenance=Provenance.REAL, currency="USD",
                                  requested_at=timezone.now() - timedelta(hours=3), completed_at=timezone.now())
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["sample_sufficiency"]["observations"], 5)

    def test_suppressed_stub_is_number_free_against_differencing(self):
        pb = _broker()
        for _ in range(4):
            _wd(_acct())   # below sample floor -> SUPPRESSED
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        self.assertEqual(r["evidence_status"]["state"], State.SUPPRESSED)
        for leaky in ("sample_sufficiency", "observation_window", "data_freshness", "withdrawal_speed",
                      "lifecycle_outcomes", "per_currency_counts"):
            self.assertNotIn(leaky, r, "withheld payload must carry no counts/window/timestamps (differencing guard)")

    def test_idempotent_recompute(self):
        pb = _broker()
        _published_cohort()
        self.assertEqual(compute_withdrawal_intelligence(pb, certified_brokers=CERT),
                         compute_withdrawal_intelligence(pb, certified_brokers=CERT))

    def test_output_is_pii_free_and_has_no_exact_timestamps(self):
        pb = _broker()
        _published_cohort()
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        forbidden = {"owner", "owner_id", "user", "user_id", "account", "account_number", "trading_account",
                     "broker_reference_id", "reference", "email", "amount", "login", "message_id"}

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    self.assertNotIn(k, forbidden, "PII-ish key leaked: %s" % k)
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(r)
        # every public timestamp is date-granular (YYYY-MM-DD) — no sub-day precision / no ISO 'T'
        self.assertNotIn("T", r["observation_window"]["end"] or "")
        self.assertNotIn("T", r["data_freshness"] or "")
        self.assertEqual(len(r["data_freshness"]), 10)

    def test_malicious_display_name_is_neutralised(self):
        pb = _broker(display_name="<script>alert(1)</script> visit http://evil.test/x ABCdef1234567890token")
        _published_cohort()
        r = compute_withdrawal_intelligence(pb, certified_brokers=CERT)
        dn = r["broker"]["display_name"]
        self.assertNotIn("<script>", dn)
        self.assertNotIn("http://evil.test", dn)
        self.assertIn("[REDACTED]", dn)   # the URL/token were redacted


class DisclosureLayer(TestCase):
    def test_public_lifecycle_maps_every_status(self):
        self.assertEqual(set(disclosure.PUBLIC_LIFECYCLE), set(Withdrawal.Status))
        # the four Stream B states are mapped to DISTINCT terminals, not collapsed to 'unresolved'
        self.assertEqual(disclosure.public_lifecycle_bucket(Withdrawal.Status.EXPIRED), "terminal_expired")
        self.assertEqual(disclosure.public_lifecycle_bucket(Withdrawal.Status.FAILED), "terminal_failed")
        self.assertEqual(disclosure.public_lifecycle_bucket(Withdrawal.Status.CONFIRMATION_REQUIRED),
                         "confirmation_required")

    def test_safe_public_text_redacts_escapes_and_caps(self):
        self.assertEqual(disclosure.safe_public_text("Pepperstone"), "Pepperstone")
        self.assertEqual(disclosure.safe_public_text(""), "")
        out = disclosure.safe_public_text("<b>x</b> http://evil.test/p?q=1")
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("[REDACTED]", out)
        self.assertLessEqual(len(disclosure.safe_public_text("A" * 500)), disclosure.MAX_PUBLIC_TEXT + 1)

    def test_safe_public_url_allowlist(self):
        self.assertEqual(disclosure.safe_public_url("https://pepperstone.com"), "https://pepperstone.com")
        self.assertEqual(disclosure.safe_public_url("javascript:alert(1)"), "")
        self.assertEqual(disclosure.safe_public_url("data:text/html,x"), "")
        self.assertEqual(disclosure.safe_public_url("pepperstone.com"), "")   # scheme-less fails closed
        self.assertEqual(disclosure.safe_public_url(""), "")

    def test_stage_seconds_guard(self):
        now = timezone.now()
        self.assertEqual(disclosure.stage_seconds(now - timedelta(hours=1), now), 3600)
        self.assertIsNone(disclosure.stage_seconds(None, now))
        self.assertIsNone(disclosure.stage_seconds(now, None))
        self.assertIsNone(disclosure.stage_seconds(now, now - timedelta(hours=1)))   # impossible -> None, not negative
        self.assertEqual(disclosure.stage_seconds(now, now), 0)                       # a valid instantaneous stage

    def test_coarsen_to_date(self):
        now = timezone.now()
        self.assertEqual(disclosure.coarsen_to_date(now), now.date())
        self.assertIsNone(disclosure.coarsen_to_date(None))

    def test_cohort_state_transitions(self):
        self.assertEqual(disclosure.cohort_state(0, 0, min_contributors=3, min_samples=5),
                         (State.UNAVAILABLE, Reason.NO_ELIGIBLE_EVIDENCE))
        self.assertEqual(disclosure.cohort_state(2, 9, min_contributors=3, min_samples=5),
                         (State.SUPPRESSED, Reason.BELOW_MIN_CONTRIBUTORS))
        self.assertEqual(disclosure.cohort_state(3, 4, min_contributors=3, min_samples=5),
                         (State.SUPPRESSED, Reason.BELOW_MIN_SAMPLES))
        self.assertEqual(disclosure.cohort_state(3, 5, min_contributors=3, min_samples=5),
                         (State.PUBLISHED, Reason.OK))
