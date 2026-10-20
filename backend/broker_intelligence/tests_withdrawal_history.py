"""PR2 — per-withdrawal history projection + API: owner-scoping, broker/account filters, truthful lifecycle state
(PENDING in-flight ≠ failed; UNRESOLVED visible), per-currency amounts never summed, duration None-not-0, masked
account numbers, and the DARK 404 gate.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from broker_intelligence.metrics import withdrawal_history, withdrawal_metrics
from broker_intelligence.models import BrokerEvent, Provenance, TransactionCategory, Withdrawal
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "61%04d" % _n


def _user():
    s = _uniq()
    return User.objects.create_user(username="wh%s" % s, email="%s@m.invalid" % s, password="x")


def _acct(user, broker="TradersWay", number=None):
    return TradingAccount.objects.create(user=user, name="A", broker_name=broker,
                                         account_number=number or ("518" + _uniq()), is_demo=True, is_active=True)


def _wd(acct, *, status=Withdrawal.Status.COMPLETED, amount="100.00", currency="USD",
        requested=True, completed=True, ref="", method="", provenance=Provenance.REAL):
    # Default REAL: the member-facing history is GENUINE-only (§507); synthetic/sanitised exclusion is asserted
    # explicitly below.
    now = timezone.now()
    return Withdrawal.objects.create(
        trading_account=acct, status=status,
        amount=Decimal(amount) if amount is not None else None, currency=currency,
        requested_at=now - timedelta(hours=2) if requested else None,
        completed_at=now if completed else None,
        broker_reference_id=ref, correlation_method=method, provenance=provenance)


class WithdrawalHistoryProjectionTests(TestCase):
    def test_owner_scoped_and_states_truthful(self):
        u = _user()
        a = _acct(u, "TradersWay")
        _wd(a, status=Withdrawal.Status.COMPLETED, amount="200.00", currency="USD")
        _wd(a, status=Withdrawal.Status.PENDING, amount=None, currency="", completed=False)
        _wd(a, status=Withdrawal.Status.REJECTED, amount="50.00", completed=False)
        _wd(a, status=Withdrawal.Status.UNRESOLVED, amount=None, currency="", requested=False, completed=False)
        # another member's withdrawal must never appear
        other = _user(); _wd(_acct(other), amount="999.00")
        out = withdrawal_history(TradingAccount.objects.filter(user=u))
        states = sorted(r["state"] for r in out["withdrawals"])
        self.assertEqual(states, ["completed", "failed", "pending", "unresolved"])
        self.assertEqual(len(out["withdrawals"]), 4)                 # never the other member's row
        completed = next(r for r in out["withdrawals"] if r["state"] == "completed")
        self.assertEqual(completed["amount"], "200.00")              # observed, not summed
        self.assertEqual(completed["currency"], "USD")
        self.assertIsNotNone(completed["duration_seconds"])          # both anchors present
        pending = next(r for r in out["withdrawals"] if r["state"] == "pending")
        self.assertIsNone(pending["amount"])                        # never fabricated
        self.assertIsNone(pending["duration_seconds"])              # not both anchors → None, never 0

    def test_account_number_is_masked(self):
        u = _user()
        a = _acct(u, "TradersWay", number="51760123")
        _wd(a)
        out = withdrawal_history(TradingAccount.objects.filter(user=u))
        self.assertEqual(out["withdrawals"][0]["account_masked"], "••••0123")
        self.assertNotIn("51760123", str(out))                      # full number never leaks

    def test_synthetic_and_sanitised_rows_excluded_from_member_history(self):
        # §507: a member's history shows ONLY genuine (REAL) withdrawals; synthetic/sanitised test records never
        # appear (and never reach the broker/account selector lists).
        u = _user()
        a = _acct(u, "TradersWay")
        _wd(a, amount="200.00", ref="genuine")
        _wd(a, amount="999.00", ref="synthetic", provenance=Provenance.SYNTHETIC)
        _wd(a, amount="888.00", ref="sanitised", provenance=Provenance.SANITISED)
        out = withdrawal_history(TradingAccount.objects.filter(user=u))
        self.assertEqual(len(out["withdrawals"]), 1)                 # only the genuine row
        self.assertEqual(out["withdrawals"][0]["broker_reference_id"], "genuine")
        self.assertNotIn("999.00", str(out))
        self.assertNotIn("888.00", str(out))

    def test_broker_and_account_filters(self):
        u = _user()
        a_tw = _acct(u, "TradersWay")
        a_is6 = _acct(u, "IS6 Technologies")
        _wd(a_tw, amount="10.00"); _wd(a_is6, amount="20.00")
        allh = withdrawal_history(TradingAccount.objects.filter(user=u))
        self.assertEqual(set(allh["brokers"]), {"TradersWay", "IS6 Technologies"})
        self.assertEqual(len(allh["accounts"]), 2)                  # selector lists cover BOTH brokers
        only_tw = withdrawal_history(TradingAccount.objects.filter(user=u), broker="TradersWay")
        self.assertTrue(all(r["broker"] == "TradersWay" for r in only_tw["withdrawals"]))
        self.assertEqual(set(only_tw["brokers"]), {"TradersWay", "IS6 Technologies"})   # lists stay complete
        only_acct = withdrawal_history(TradingAccount.objects.filter(user=u), account_id=a_is6.id)
        self.assertTrue(all(r["trading_account_id"] == a_is6.id for r in only_acct["withdrawals"]))


@override_settings(BROKER_WITHDRAWAL_UX_ENABLED=True)
class WithdrawalHistoryApiArmedTests(TestCase):
    def setUp(self):
        self.u = _user()
        self.a = _acct(self.u, "TradersWay")
        _wd(self.a, amount="200.00")
        self.c = APIClient(); self.c.force_authenticate(self.u)

    def test_history_owner_scoped(self):
        other = _user(); _wd(_acct(other), amount="999.00")
        r = self.c.get("/api/broker-intelligence/withdrawals/history/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["withdrawals"]), 1)
        self.assertNotIn("999.00", str(body))                       # never another member's data

    def test_foreign_account_id_yields_nothing(self):
        other = _user(); oa = _acct(other); _wd(oa, amount="999.00")
        r = self.c.get(f"/api/broker-intelligence/withdrawals/history/?account_id={oa.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["withdrawals"], [])               # a foreign account id resolves to nothing


class WithdrawalHistoryDarkTests(TestCase):
    def test_404_while_dark(self):
        u = _user()
        c = APIClient(); c.force_authenticate(u)
        self.assertEqual(c.get("/api/broker-intelligence/withdrawals/history/").status_code, 404)


class OwnerAttributedHistoryTests(TestCase):
    """WV1 §4 / ADR-0050 — owner-attributed, account-UNRESOLVED withdrawals surface to the owning member."""

    def _owner_wd(self, user, *, status=Withdrawal.Status.REQUESTED, ref="OW-1", broker="TradersWay"):
        ev = BrokerEvent.objects.create(
            owner=user, broker=broker, event_type="WITHDRAWAL_CONFIRMATION_REQUIRED",
            transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL, broker_reference_id=ref,
            received_at=timezone.now(), provenance=Provenance.REAL,
            correlation_status=BrokerEvent.CorrelationStatus.CORRELATED)
        return Withdrawal.objects.create(
            owner=user, trading_account=None, status=status, broker_reference_id=ref, currency="USD",
            amount=Decimal("250.00") if status == Withdrawal.Status.COMPLETED else None,
            requested_at=timezone.now() - timedelta(minutes=33), requested_event=ev,
            completed_at=timezone.now() if status == Withdrawal.Status.COMPLETED else None,
            correlation_method=Withdrawal.CorrelationMethod.REFERENCE_ID, provenance=Provenance.REAL)

    def test_account_unresolved_withdrawal_surfaces_to_owner(self):
        u = _user()
        self._owner_wd(u)
        out = withdrawal_history(TradingAccount.objects.filter(user=u), owner=u)
        self.assertEqual(len(out["withdrawals"]), 1)
        r = out["withdrawals"][0]
        self.assertTrue(r["account_unresolved"])
        self.assertIsNone(r["trading_account_id"])
        self.assertEqual(r["account_masked"], "UNRESOLVED")        # sentinel, never a fabricated number
        self.assertEqual(r["broker"], "TradersWay")                # derived from the originating event
        self.assertEqual(out["brokers"], ["TradersWay"])
        self.assertEqual(out["accounts"], [])                      # no account selector entry for a null-account row

    def test_account_unresolved_hidden_without_owner_param(self):
        u = _user()
        self._owner_wd(u)
        self.assertEqual(withdrawal_history(TradingAccount.objects.filter(user=u))["withdrawals"], [])   # legacy scope

    def test_account_unresolved_not_visible_to_other_user(self):
        u, other = _user(), _user()
        self._owner_wd(u)
        out = withdrawal_history(TradingAccount.objects.filter(user=other), owner=other)
        self.assertEqual(out["withdrawals"], [])                   # strictly owner-scoped — never cross-user

    def test_mixed_account_and_owner_rows_both_surface(self):
        u = _user()
        a = _acct(u, "TradersWay")
        _wd(a, status=Withdrawal.Status.COMPLETED, amount="100.00")   # account-resolved
        self._owner_wd(u, ref="OW-2")                                  # account-unresolved
        out = withdrawal_history(TradingAccount.objects.filter(user=u), owner=u)
        self.assertEqual(len(out["withdrawals"]), 2)
        self.assertEqual(len(out["accounts"]), 1)                   # only the resolved account is selectable
        self.assertTrue(any(r["account_unresolved"] for r in out["withdrawals"]))

    def test_metrics_count_includes_owner_attributed(self):
        u = _user()
        self._owner_wd(u, status=Withdrawal.Status.COMPLETED)
        m = withdrawal_metrics(TradingAccount.objects.filter(user=u), owner=u)
        self.assertEqual(m["total"], 1)
        self.assertEqual(m["completed_count"], 1)

    @override_settings(BROKER_WITHDRAWAL_UX_ENABLED="1")
    def test_api_surfaces_owner_attributed(self):
        u = _user()
        self._owner_wd(u)
        c = APIClient(); c.force_authenticate(u)
        resp = c.get("/api/broker-intelligence/withdrawals/history/")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["withdrawals"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["account_unresolved"])
