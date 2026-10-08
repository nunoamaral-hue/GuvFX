"""WP5 — withdrawal correlation engine tests.

Covers: reference-id determinism + idempotency; monotonic non-regressing status; bounded heuristic fallback
(single match / ambiguous / unresolved / new-record); EXTERNAL_WITHDRAWAL-only projection (the internal-transfer
negative); fail-closed guards; and that correlation preserves BrokerEvent append-only evidence.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from broker_intelligence import correlation as C
from broker_intelligence.models import BrokerEvent, Provenance, TransactionCategory, Withdrawal
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "92%04d" % _n


def _acct(**kw):
    login = _uniq()
    u = User.objects.create_user(username="wp5%s" % login, email="%s@x.invalid" % login, password="x")
    d = dict(user=u, name="A", broker_name="TradersWay", account_number=login, is_demo=True, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


def _event(account, event_type, *, category=TransactionCategory.EXTERNAL_WITHDRAWAL, ref="",
           amount="200.00", currency="USD", occurred_at=None, received_at=None,
           provenance=Provenance.SYNTHETIC):
    return BrokerEvent.objects.create(
        trading_account=account, broker="TradersWay", event_type=event_type, transaction_category=category,
        broker_reference_id=ref, amount=(Decimal(amount) if amount is not None else None), currency=currency,
        occurred_at=occurred_at, received_at=received_at or timezone.now(), provenance=provenance,
        correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)


class ReferenceIdCorrelation(TestCase):
    def test_requested_then_completed_same_ref_is_one_withdrawal(self):
        a = _acct()
        t0 = timezone.now() - timedelta(days=2)
        t1 = timezone.now()
        req = _event(a, "WITHDRAWAL_REQUESTED", ref="W-100", occurred_at=t0)
        r1 = C.correlate_event(req)
        self.assertEqual(r1.outcome, C.OUT_CREATED)
        comp = _event(a, "WITHDRAWAL_COMPLETED", ref="W-100", occurred_at=t1)
        r2 = C.correlate_event(comp)
        self.assertEqual(r2.outcome, C.OUT_REFERENCE)
        self.assertEqual(r1.withdrawal_id, r2.withdrawal_id)                 # SAME withdrawal
        self.assertEqual(Withdrawal.objects.count(), 1)
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)
        self.assertEqual(w.correlation_method, Withdrawal.CorrelationMethod.REFERENCE_ID)
        self.assertEqual(w.requested_event_id, req.id)
        self.assertEqual(w.completed_event_id, comp.id)
        self.assertEqual(w.duration_seconds(), int((t1 - t0).total_seconds()))
        req.refresh_from_db(); comp.refresh_from_db()
        self.assertEqual(req.correlation_status, BrokerEvent.CorrelationStatus.CORRELATED)
        self.assertEqual(comp.correlation_status, BrokerEvent.CorrelationStatus.CORRELATED)

    def test_status_never_regresses_and_backfills_requested(self):
        # COMPLETED observed FIRST (out of order), then the REQUESTED arrives: status stays COMPLETED, requested
        # anchor is backfilled.
        a = _acct()
        t0 = timezone.now() - timedelta(days=1)
        comp = _event(a, "WITHDRAWAL_COMPLETED", ref="W-9", occurred_at=timezone.now())
        C.correlate_event(comp)
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)
        self.assertIsNone(w.requested_at)
        req = _event(a, "WITHDRAWAL_REQUESTED", ref="W-9", occurred_at=t0)
        C.correlate_event(req)
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)              # NOT regressed to REQUESTED
        self.assertEqual(w.requested_at, t0)                                 # backfilled
        self.assertEqual(w.requested_event_id, req.id)
        self.assertEqual(Withdrawal.objects.count(), 1)

    def test_rerun_is_idempotent(self):
        a = _acct()
        _event(a, "WITHDRAWAL_REQUESTED", ref="W-1", occurred_at=timezone.now())
        s1 = C.run_correlation()
        s2 = C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 1)
        self.assertEqual(s1.get("created"), 1)
        self.assertEqual(s2.get("processed", 0), 0)                          # nothing left UNRESOLVED/AMBIGUOUS

    def test_refless_requested_then_ref_completed_is_one_withdrawal(self):
        # A ref-less REQUESTED followed by a ref-CARRYING COMPLETED for the same withdrawal must adopt (promote) the
        # heuristic row to the reference — ONE record, never two (double-count fix).
        a = _acct()
        req = _event(a, "WITHDRAWAL_REQUESTED", ref="", amount="250.00", occurred_at=timezone.now() - timedelta(days=1))
        C.correlate_event(req)
        self.assertEqual(Withdrawal.objects.count(), 1)
        comp = _event(a, "WITHDRAWAL_COMPLETED", ref="TW-77", amount="250.00", occurred_at=timezone.now())
        r = C.correlate_event(comp)
        self.assertEqual(r.outcome, C.OUT_REFERENCE)
        self.assertEqual(Withdrawal.objects.count(), 1)                     # adopted, not duplicated
        w = Withdrawal.objects.get()
        self.assertEqual(w.broker_reference_id, "TW-77")                    # promoted to the authoritative ref
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)
        self.assertEqual(w.completed_event_id, comp.id)

    def test_refless_ref_adoption_ambiguous_creates_authoritative_ref_row(self):
        # Two ref-less candidates + a ref COMPLETED: ambiguous adoption must NOT merge; create the authoritative ref
        # row rather than guess which heuristic row to promote.
        a = _acct()
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="", amount="600.00",
                                 occurred_at=timezone.now() - timedelta(days=1)))
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="", amount="600.00",
                                 occurred_at=timezone.now() - timedelta(days=1)))
        comp = _event(a, "WITHDRAWAL_COMPLETED", ref="TW-88", amount="600.00", occurred_at=timezone.now())
        r = C.correlate_event(comp)
        self.assertEqual(r.outcome, C.OUT_CREATED)                          # authoritative ref row, no merge-guess
        self.assertEqual(Withdrawal.objects.filter(broker_reference_id="TW-88").count(), 1)
        self.assertEqual(Withdrawal.objects.count(), 3)                     # 2 heuristic + 1 ref (none merged)

    def test_same_event_processed_twice_is_idempotent_no_duplicate(self):
        # Event-level idempotency: re-processing the SAME ref-less opening event (duplicate/concurrent pass) is a
        # structural no-op, not a second withdrawal (the heuristic opening path has no DB uniqueness).
        a = _acct()
        req = _event(a, "WITHDRAWAL_REQUESTED", ref="", amount="150.00", occurred_at=timezone.now())
        self.assertEqual(C.correlate_event(req).outcome, C.OUT_CREATED)
        self.assertEqual(C.correlate_event(req).outcome, C.OUT_ALREADY)     # re-read sees CORRELATED -> no-op
        self.assertEqual(Withdrawal.objects.count(), 1)

    def test_rejected_is_terminal_no_completed_at(self):
        a = _acct()
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="W-2", occurred_at=timezone.now() - timedelta(hours=1)))
        C.correlate_event(_event(a, "WITHDRAWAL_REJECTED", ref="W-2", occurred_at=timezone.now()))
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.REJECTED)
        self.assertIsNone(w.completed_at)
        self.assertIsNone(w.duration_seconds())


class HeuristicCorrelation(TestCase):
    def test_single_candidate_within_window_correlates(self):
        a = _acct()
        t0 = timezone.now() - timedelta(days=1)
        req = _event(a, "WITHDRAWAL_REQUESTED", ref="", amount="300.00", occurred_at=t0)
        r1 = C.correlate_event(req)
        self.assertEqual(r1.outcome, C.OUT_CREATED)
        comp = _event(a, "WITHDRAWAL_COMPLETED", ref="", amount="300.00", occurred_at=timezone.now())
        r2 = C.correlate_event(comp)
        self.assertEqual(r2.outcome, C.OUT_HEURISTIC)
        self.assertEqual(r1.withdrawal_id, r2.withdrawal_id)
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)
        self.assertEqual(w.correlation_method, Withdrawal.CorrelationMethod.HEURISTIC)

    def test_two_candidates_are_ambiguous_never_guessed(self):
        a = _acct()
        # two ref-less requested withdrawals, same amount/currency/window
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="", amount="500.00",
                                 occurred_at=timezone.now() - timedelta(days=1)))
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="", amount="500.00",
                                 occurred_at=timezone.now() - timedelta(days=1)))
        self.assertEqual(Withdrawal.objects.count(), 2)
        comp = _event(a, "WITHDRAWAL_COMPLETED", ref="", amount="500.00", occurred_at=timezone.now())
        r = C.correlate_event(comp)
        self.assertEqual(r.outcome, C.OUT_AMBIGUOUS)
        comp.refresh_from_db()
        self.assertEqual(comp.correlation_status, BrokerEvent.CorrelationStatus.AMBIGUOUS)
        # neither candidate advanced to COMPLETED
        self.assertEqual(Withdrawal.objects.filter(status=Withdrawal.Status.COMPLETED).count(), 0)

    def test_completion_with_no_match_is_unresolved_never_fabricated(self):
        a = _acct()
        comp = _event(a, "WITHDRAWAL_COMPLETED", ref="", amount="777.00", occurred_at=timezone.now())
        r = C.correlate_event(comp)
        self.assertEqual(r.outcome, C.OUT_UNRESOLVED)
        self.assertEqual(Withdrawal.objects.count(), 0)                      # no fabricated withdrawal
        comp.refresh_from_db()
        self.assertEqual(comp.correlation_status, BrokerEvent.CorrelationStatus.UNRESOLVED)

    def test_outside_window_does_not_match_creates_or_unresolved(self):
        a = _acct()
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="", amount="400.00",
                                 occurred_at=timezone.now() - timedelta(days=60)))
        comp = _event(a, "WITHDRAWAL_COMPLETED", ref="", amount="400.00", occurred_at=timezone.now())
        r = C.correlate_event(comp)
        self.assertEqual(r.outcome, C.OUT_UNRESOLVED)                        # the old request is out of the window


class ProjectionGuards(TestCase):
    def test_internal_transfer_is_never_correlated(self):
        # The genuine TradersWay "confirm your withdrawal request" email was an INTERNAL TRANSFER — it must never
        # produce a withdrawal, even though its lifecycle event_type is a withdrawal-confirmation one.
        a = _acct()
        ev = _event(a, "WITHDRAWAL_CONFIRMATION_REQUIRED", category=TransactionCategory.INTERNAL_TRANSFER,
                    ref="INT-1")
        r = C.correlate_event(ev)
        self.assertEqual(r.outcome, C.OUT_SKIP_NOT_EXTERNAL)
        self.assertEqual(Withdrawal.objects.count(), 0)

    def test_deposit_and_unknown_are_skipped(self):
        a = _acct()
        for cat in (TransactionCategory.DEPOSIT, TransactionCategory.UNKNOWN):
            ev = _event(a, "WITHDRAWAL_REQUESTED", category=cat, ref="X")
            self.assertEqual(C.correlate_event(ev).outcome, C.OUT_SKIP_NOT_EXTERNAL)
        self.assertEqual(Withdrawal.objects.count(), 0)

    def test_event_without_account_is_skipped(self):
        ev = BrokerEvent.objects.create(
            trading_account=None, broker="TradersWay", event_type="WITHDRAWAL_REQUESTED",
            transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL, broker_reference_id="W-x",
            amount=Decimal("10.00"), currency="USD", received_at=timezone.now(), provenance=Provenance.SYNTHETIC,
            correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)
        self.assertEqual(C.correlate_event(ev).outcome, C.OUT_SKIP_NO_ACCOUNT)
        self.assertEqual(Withdrawal.objects.count(), 0)

    def test_non_lifecycle_event_type_is_skipped(self):
        a = _acct()
        ev = _event(a, "SOME_OTHER_NOTICE", ref="W-z")
        self.assertEqual(C.correlate_event(ev).outcome, C.OUT_SKIP_NOT_LIFECYCLE)
        self.assertEqual(Withdrawal.objects.count(), 0)

    def test_run_correlation_only_touches_external_withdrawals(self):
        a = _acct()
        _event(a, "WITHDRAWAL_REQUESTED", ref="W-ext", occurred_at=timezone.now())
        _event(a, "WITHDRAWAL_CONFIRMATION_REQUIRED", category=TransactionCategory.INTERNAL_TRANSFER, ref="INT")
        summary = C.run_correlation()
        self.assertEqual(summary["processed"], 1)                           # the internal-transfer row is not scanned
        self.assertEqual(Withdrawal.objects.count(), 1)


class AppendOnlyPreserved(TestCase):
    def test_correlation_only_mutates_correlation_status(self):
        a = _acct()
        ev = _event(a, "WITHDRAWAL_REQUESTED", ref="W-ao", amount="123.00", occurred_at=timezone.now())
        C.correlate_event(ev)
        ev.refresh_from_db()
        self.assertEqual(ev.correlation_status, BrokerEvent.CorrelationStatus.CORRELATED)
        # an evidential field is still write-once after correlation
        ev.amount = Decimal("999.00")
        with self.assertRaises(ValidationError):
            ev.save()
