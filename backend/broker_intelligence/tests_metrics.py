"""WP6 — withdrawal metrics projection + member API tests.

Proves the truthfulness rules: PENDING (in flight) is never counted as failed; amounts are summed per-currency
(never mixed) and unknown amounts omitted (not zeroed); processing-duration is None when no completed withdrawal has
both anchors (never a fabricated 0); the metrics API is DARK-gated (404 while off) and owner-scoped (never another
member's withdrawals).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from broker_intelligence.metrics import withdrawal_metrics
from broker_intelligence.models import Provenance, Withdrawal
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "93%04d" % _n


def _acct(user=None, **kw):
    login = _uniq()
    u = user or User.objects.create_user(username="wp6%s" % login, email="%s@x.invalid" % login, password="x")
    d = dict(user=u, name="A", broker_name="TradersWay", account_number=login, is_demo=False, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


def _wd(account, status, *, amount="100.00", currency="USD", requested_at=None, completed_at=None, ref=""):
    return Withdrawal.objects.create(
        trading_account=account, broker_reference_id=ref,
        amount=(Decimal(amount) if amount is not None else None), currency=currency,
        requested_at=requested_at, completed_at=completed_at, status=status, provenance=Provenance.SYNTHETIC)


class WithdrawalMetricsProjection(TestCase):
    def test_pending_is_not_failed_and_counts_are_distinct(self):
        a = _acct()
        _wd(a, Withdrawal.Status.REQUESTED, ref="r1")
        _wd(a, Withdrawal.Status.PROCESSING, ref="r2")
        _wd(a, Withdrawal.Status.PENDING, ref="r3")
        _wd(a, Withdrawal.Status.REJECTED, ref="r4")
        _wd(a, Withdrawal.Status.CANCELLED, ref="r5")
        _wd(a, Withdrawal.Status.UNRESOLVED, ref="r6")
        m = withdrawal_metrics([a])
        self.assertEqual(m["total"], 6)
        self.assertEqual(m["pending_count"], 3)       # requested + processing + pending (in flight)
        self.assertEqual(m["failed_count"], 2)        # rejected + cancelled — NOT pending
        self.assertEqual(m["unresolved_count"], 1)
        self.assertEqual(m["completed_count"], 0)

    def test_completed_amounts_per_currency_never_mixed_and_unknown_omitted(self):
        a = _acct()
        _wd(a, Withdrawal.Status.COMPLETED, amount="200.00", currency="USD", ref="c1",
            requested_at=timezone.now() - timedelta(hours=2), completed_at=timezone.now())
        _wd(a, Withdrawal.Status.COMPLETED, amount="50.00", currency="USD", ref="c2",
            requested_at=timezone.now() - timedelta(hours=1), completed_at=timezone.now())
        _wd(a, Withdrawal.Status.COMPLETED, amount="30.00", currency="EUR", ref="c3")
        _wd(a, Withdrawal.Status.COMPLETED, amount=None, currency="USD", ref="c4")   # unknown amount -> omitted
        m = withdrawal_metrics([a])
        self.assertEqual(m["completed_count"], 4)
        self.assertEqual(m["completed_amount_by_currency"], {"EUR": "30.00", "USD": "250.00"})   # not mixed
        self.assertEqual(m["completed_amount_known_count"], 3)                                    # the None omitted

    def test_processing_duration_only_from_completed_with_both_anchors(self):
        a = _acct()
        t0 = timezone.now() - timedelta(seconds=3600)
        _wd(a, Withdrawal.Status.COMPLETED, ref="d1", requested_at=t0, completed_at=t0 + timedelta(seconds=3600))
        _wd(a, Withdrawal.Status.COMPLETED, ref="d2", requested_at=None, completed_at=timezone.now())  # no anchor
        m = withdrawal_metrics([a])
        d = m["processing_duration"]
        self.assertIsNotNone(d)
        self.assertEqual(d["count"], 1)              # only the one with BOTH anchors
        self.assertEqual(d["median_seconds"], 3600)

    def test_no_completed_means_duration_is_none_not_zero(self):
        a = _acct()
        _wd(a, Withdrawal.Status.REQUESTED, ref="p1")
        m = withdrawal_metrics([a])
        self.assertIsNone(m["processing_duration"])   # never a fabricated 0
        self.assertEqual(m["completed_amount_by_currency"], {})

    def test_empty_is_all_zero_not_error(self):
        a = _acct()
        m = withdrawal_metrics([a])
        self.assertEqual(m["total"], 0)
        self.assertEqual(m["pending_count"], 0)
        self.assertIsNone(m["processing_duration"])


@override_settings(BROKER_WITHDRAWAL_UX_ENABLED="1")
class WithdrawalMetricsApiArmed(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="m6", email="m6@x.invalid", password="x")
        self.a = _acct(user=self.u)
        self.c = APIClient()
        self.c.force_authenticate(self.u)

    def test_owner_scoped_excludes_other_members(self):
        _wd(self.a, Withdrawal.Status.COMPLETED, amount="100.00", currency="USD", ref="mine",
            requested_at=timezone.now() - timedelta(hours=1), completed_at=timezone.now())
        other = _acct()   # different user
        _wd(other, Withdrawal.Status.COMPLETED, amount="999.00", currency="USD", ref="theirs")
        r = self.c.get("/api/broker-intelligence/withdrawals/metrics/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["total"], 1)                                   # only mine
        self.assertEqual(r.json()["completed_amount_by_currency"], {"USD": "100.00"})

    def test_includes_members_own_disconnected_account_history(self):
        # A withdrawal is durable history: the member's OWN disconnected/tombstoned account's withdrawals must NOT
        # silently vanish from their totals (completeness). Still owner-scoped — never another member's data.
        disc = _acct(user=self.u, is_active=False, disconnected_at=timezone.now())
        _wd(disc, Withdrawal.Status.COMPLETED, amount="40.00", currency="USD", ref="hist",
            requested_at=timezone.now() - timedelta(hours=1), completed_at=timezone.now())
        r = self.c.get("/api/broker-intelligence/withdrawals/metrics/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["total"], 1)                                   # the disconnected acct's history is kept
        self.assertEqual(r.json()["completed_amount_by_currency"], {"USD": "40.00"})


@override_settings(BROKER_WITHDRAWAL_UX_ENABLED="0")
class WithdrawalMetricsApiDark(TestCase):
    def test_endpoint_is_404_while_flag_off(self):
        u = User.objects.create_user(username="m6d", email="m6d@x.invalid", password="x")
        c = APIClient()
        c.force_authenticate(u)
        r = c.get("/api/broker-intelligence/withdrawals/metrics/")
        self.assertEqual(r.status_code, 404)   # DARK: feature absent while BROKER_WITHDRAWAL_UX_ENABLED off
