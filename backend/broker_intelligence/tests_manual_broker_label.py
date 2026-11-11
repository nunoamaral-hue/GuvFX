"""GFX-WITHDRAWAL-MANUAL-BROKER-LABEL-057 — the member-facing broker LABEL for a broker-neutral, reference-less
MANUAL_STAFF withdrawal (ADR-0092) must resolve to its APPLIED staff-correlation decision's broker profile, by
GOVERNED PROVENANCE only: truthfully, owner-scoped, fail-closed, and WITHOUT weakening the data model, inferring a
missing field, or regressing the existing account- / event-derived resolution (ADR-0050).

Presentation-layer test only — it asserts nothing about production and builds its own fixtures. It mirrors the
production Withdrawal #4 shape: owner-attributed, NO trading account, NO originating parsed event, no amount /
currency / reference, status REQUESTED, correlation_method MANUAL_STAFF, linked by exactly one APPLIED
StaffWithdrawalCorrelation whose broker_profile is the authoritative broker.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from broker_intelligence.metrics import _row_broker, _staff_correlation_broker_map, withdrawal_history
from broker_intelligence.models import (BrokerEvent, BrokerProfile, Provenance, StaffWithdrawalCorrelation,
                                        TransactionCategory, Withdrawal)
from broker_intelligence.withdrawal_speed import withdrawal_speed
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "%05d" % _n


def _user():
    s = _uniq()
    return User.objects.create_user(username="mbl%s" % s, email="mbl%s@m.invalid" % s, password="x")


def _acct(user, broker="TradersWay", number=None):
    return TradingAccount.objects.create(user=user, name="A", broker_name=broker,
                                         account_number=number or ("519" + _uniq()), is_demo=True, is_active=True)


def _profile(name="Vantage", slug="vantage"):
    s = _uniq()
    return BrokerProfile.objects.create(name="%s-%s" % (name, s), slug="%s-%s" % (slug, s), is_active=True)


def _manual_wd(owner, *, status=Withdrawal.Status.REQUESTED):
    """A broker-neutral MANUAL_STAFF withdrawal — owner-attributed, NO account, NO originating event, no amount /
    currency / reference (exactly the production Withdrawal #4 shape)."""
    return Withdrawal.objects.create(
        owner=owner, trading_account=None, status=status, amount=None, currency="", broker_reference_id="",
        requested_at=timezone.now() - timedelta(minutes=5), requested_event=None, completed_event=None,
        correlation_method=Withdrawal.CorrelationMethod.MANUAL_STAFF, provenance=Provenance.REAL)


def _decision(owner, profile, wd, *, status=StaffWithdrawalCorrelation.Status.APPLIED):
    return StaffWithdrawalCorrelation.objects.create(
        owner=owner, broker_profile=profile, resulting_withdrawal=wd, status=status, lifecycle_result="REQUESTED",
        reviewer="user:%d" % (owner.id if owner else 0), idempotency_key="idem-%s" % _uniq())


def _event_wd(owner, *, broker="Pepperstone"):
    """An owner-attributed, account-UNRESOLVED withdrawal whose broker comes from its originating EVENT (precedence 2)."""
    ev = BrokerEvent.objects.create(
        owner=owner, broker=broker, event_type="WITHDRAWAL_CONFIRMATION_REQUIRED",
        transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL, broker_reference_id="EVREF",
        received_at=timezone.now(), provenance=Provenance.REAL,
        correlation_status=BrokerEvent.CorrelationStatus.CORRELATED)
    return Withdrawal.objects.create(
        owner=owner, trading_account=None, status=Withdrawal.Status.REQUESTED, broker_reference_id="EVREF",
        currency="USD", amount=None, requested_at=timezone.now() - timedelta(minutes=10), requested_event=ev,
        correlation_method=Withdrawal.CorrelationMethod.REFERENCE_ID, provenance=Provenance.REAL)


class ManualStaffBrokerLabelTests(TestCase):
    def _row(self, owner, wd_id):
        out = withdrawal_history(TradingAccount.objects.filter(user=owner), owner=owner)
        return next((r for r in out["withdrawals"] if r["id"] == wd_id), None), out

    def test_1_manual_staff_renders_broker_and_stays_unresolved(self):
        u = _user(); p = _profile("Vantage", "vantage"); wd = _manual_wd(u); _decision(u, p, wd)
        row, out = self._row(u, wd.id)
        self.assertIsNotNone(row)
        self.assertEqual(row["broker"], p.name)                      # resolved from the APPLIED decision's profile
        self.assertEqual(row["status"], Withdrawal.Status.REQUESTED)
        self.assertEqual(row["state"], "pending")                    # REQUESTED is in-flight, NEVER completed
        self.assertIsNone(row["completed_at"])                       # no false completion
        self.assertIsNone(row["amount"])                             # unknown stays unknown (never fabricated)
        self.assertEqual(row["currency"], "")
        self.assertEqual(row["broker_reference_id"], "")             # reference remains absent
        self.assertTrue(row["account_unresolved"])
        self.assertEqual(row["account_masked"], "UNRESOLVED")        # never a fabricated account number
        self.assertFalse(row["reference_correlated"])                # MANUAL_STAFF is not a reference match
        self.assertIn(p.name, out["brokers"])                        # the broker selector lists it
        self.assertEqual(out["accounts"], [])                        # no account selector entry (null account)

    def test_2_bypass_resolver_label_is_blank(self):
        # Mutation anchor: WITHOUT the staff map the key Vantage assertion FAILS (proves the resolver is load-bearing).
        u = _user(); p = _profile(); wd = _manual_wd(u); _decision(u, p, wd)
        self.assertEqual(_row_broker(wd), "")                                        # precedence 1-2 only → blank (old)
        self.assertEqual(_row_broker(wd, _staff_correlation_broker_map(u)), p.name)  # precedence 3 supplies the label

    def test_3_event_derived_pepperstone_unchanged(self):
        u = _user(); wd = _event_wd(u, broker="Pepperstone")
        row, _ = self._row(u, wd.id)
        self.assertEqual(row["broker"], "Pepperstone")               # precedence 2 unchanged

    def test_4_account_derived_tradersway_unchanged(self):
        u = _user(); a = _acct(u, "TradersWay")
        wd = Withdrawal.objects.create(trading_account=a, owner=u, status=Withdrawal.Status.COMPLETED,
                                       amount=Decimal("12.00"), currency="USD",
                                       requested_at=timezone.now() - timedelta(hours=1), completed_at=timezone.now(),
                                       correlation_method=Withdrawal.CorrelationMethod.REFERENCE_ID,
                                       provenance=Provenance.REAL)
        row, _ = self._row(u, wd.id)
        self.assertEqual(row["broker"], "TradersWay")                # precedence 1 unchanged

    def test_5_account_precedence_over_staff_decision(self):
        # A resolved account ALWAYS wins over a staff decision (precedence 1 > 3) — no regression of account resolution.
        u = _user(); a = _acct(u, "TradersWay"); p = _profile("Vantage", "vantage")
        wd = Withdrawal.objects.create(trading_account=a, owner=u, status=Withdrawal.Status.REQUESTED,
                                       requested_at=timezone.now(), amount=None, currency="",
                                       correlation_method=Withdrawal.CorrelationMethod.MANUAL_STAFF,
                                       provenance=Provenance.REAL)
        _decision(u, p, wd)
        self.assertEqual(_row_broker(wd, _staff_correlation_broker_map(u)), "TradersWay")

    def test_6_event_precedence_over_staff_decision(self):
        # An originating event wins over a staff decision (precedence 2 > 3).
        u = _user(); p = _profile("Vantage", "vantage"); wd = _event_wd(u, broker="Pepperstone"); _decision(u, p, wd)
        self.assertEqual(_row_broker(wd, _staff_correlation_broker_map(u)), "Pepperstone")

    def test_7_non_applied_decision_ignored(self):
        for st in (StaffWithdrawalCorrelation.Status.PROPOSED, StaffWithdrawalCorrelation.Status.REJECTED,
                   StaffWithdrawalCorrelation.Status.SUPERSEDED):
            u = _user(); p = _profile(); wd = _manual_wd(u); _decision(u, p, wd, status=st)
            row, _ = self._row(u, wd.id)
            self.assertEqual(row["broker"], "", "status %s must not produce a label" % st)

    def test_8_decision_for_other_withdrawal_ignored(self):
        u = _user(); p = _profile(); wd = _manual_wd(u); other = _manual_wd(u)
        _decision(u, p, other)                                       # APPLIED decision points at `other`, not `wd`
        self.assertEqual(self._row(u, wd.id)[0]["broker"], "")       # `wd` has no decision of its own → blank
        self.assertEqual(self._row(u, other.id)[0]["broker"], p.name)

    def test_9_cross_owner_decision_ignored_and_no_leak(self):
        a = _user(); b = _user(); p = _profile()
        wd_a = _manual_wd(a)
        # A decision OWNED BY B that (pathologically) references A's withdrawal must NOT label it for A…
        StaffWithdrawalCorrelation.objects.create(owner=b, broker_profile=p, resulting_withdrawal=wd_a,
                                                  status=StaffWithdrawalCorrelation.Status.APPLIED,
                                                  lifecycle_result="REQUESTED", reviewer="user:%d" % b.id,
                                                  idempotency_key="idem-x-%s" % _uniq())
        self.assertEqual(self._row(a, wd_a.id)[0]["broker"], "")     # owner A sees blank (the decision is B's)
        out_b = withdrawal_history(TradingAccount.objects.filter(user=b), owner=b)
        self.assertEqual(out_b["withdrawals"], [])                   # …and B never sees A's withdrawal at all

    def test_10_conflicting_applied_brokers_fail_closed(self):
        u = _user(); p1 = _profile("Vantage", "vantage"); p2 = _profile("Pepperstone", "pepperstone")
        wd = _manual_wd(u)
        _decision(u, p1, wd); _decision(u, p2, wd)                   # two APPLIED decisions, DIFFERENT brokers
        self.assertEqual(self._row(u, wd.id)[0]["broker"], "")       # ambiguous provenance → blank, never a guess

    def test_11_missing_provenance_blank(self):
        u = _user(); wd = _manual_wd(u)                              # no StaffWithdrawalCorrelation at all
        self.assertEqual(self._row(u, wd.id)[0]["broker"], "")

    def test_12_no_history_row_duplication(self):
        u = _user(); p = _profile(); wd = _manual_wd(u); _decision(u, p, wd)
        out = withdrawal_history(TradingAccount.objects.filter(user=u), owner=u)
        self.assertEqual([r["id"] for r in out["withdrawals"]].count(wd.id), 1)
        self.assertEqual(len(out["withdrawals"]), 1)

    def test_13_resolver_is_read_only_no_mutation(self):
        # Public-statistics / publication state is never touched: the resolver reads, it never writes.
        u = _user(); p = _profile(); wd = _manual_wd(u); _decision(u, p, wd)
        before = (Withdrawal.objects.count(), StaffWithdrawalCorrelation.objects.count(), BrokerProfile.objects.count())
        withdrawal_history(TradingAccount.objects.filter(user=u), owner=u)
        after = (Withdrawal.objects.count(), StaffWithdrawalCorrelation.objects.count(), BrokerProfile.objects.count())
        self.assertEqual(before, after)

    def test_14_owner_scoped_counts_no_cross_tenant_leak(self):
        a = _user(); b = _user(); p = _profile()
        _decision(a, p, _manual_wd(a)); _decision(a, p, _manual_wd(a))   # A: 2
        _decision(b, p, _manual_wd(b))                                   # B: 1
        out_a = withdrawal_history(TradingAccount.objects.filter(user=a), owner=a)
        out_b = withdrawal_history(TradingAccount.objects.filter(user=b), owner=b)
        self.assertEqual(len(out_a["withdrawals"]), 2)
        self.assertEqual(len(out_b["withdrawals"]), 1)
        self.assertTrue({r["id"] for r in out_a["withdrawals"]}.isdisjoint({r["id"] for r in out_b["withdrawals"]}))
        self.assertTrue(all(r["broker"] == p.name for r in out_a["withdrawals"]))

    def test_15_no_n_plus_one_in_row_count(self):
        # The staff-correlation map is ONE bounded query — the total query count does NOT grow with the number of
        # manual-staff withdrawals (no per-row correlation lookup).
        def count_for(num):
            u = _user(); p = _profile()
            for _ in range(num):
                _decision(u, p, _manual_wd(u))
            accts = list(TradingAccount.objects.filter(user=u))   # materialise so account-id eval is outside the measure
            withdrawal_history(accts, owner=u)                    # warm schema / content-type caches
            with CaptureQueriesContext(connection) as ctx:
                out = withdrawal_history(accts, owner=u)
            self.assertEqual(len(out["withdrawals"]), num)
            self.assertTrue(all(r["broker"] == p.name for r in out["withdrawals"]))
            return len(ctx)
        self.assertEqual(count_for(1), count_for(8))              # constant in row count → no N+1


class ManualStaffBrokerLabelSpeedTests(TestCase):
    def test_pending_manual_withdrawal_labels_broker_in_speed_view(self):
        # The withdrawal-speed in-progress list must label the pending manual withdrawal consistently with history.
        u = _user(); p = _profile("Vantage", "vantage"); wd = _manual_wd(u); _decision(u, p, wd)
        out = withdrawal_speed(TradingAccount.objects.filter(user=u), owner=u, now=timezone.now())
        ip = next((r for r in out["in_progress"] if r["withdrawal_id"] == wd.id), None)
        self.assertIsNotNone(ip)
        self.assertEqual(ip["broker"], p.name)                       # consistent with withdrawal_history
        self.assertEqual(ip["status"], Withdrawal.Status.REQUESTED)
        self.assertTrue(ip["account_unresolved"])
        self.assertIsNone(ip["amount"])                              # unknown stays unknown
