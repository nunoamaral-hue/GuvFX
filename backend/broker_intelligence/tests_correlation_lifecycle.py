"""WP5 — additional withdrawal-lifecycle + concurrency coverage (synthetic EXTERNAL_WITHDRAWAL events only).

Closes gaps the original tests_correlation.py left: the full forward ladder, contradictory-terminal no-flip,
CANCELLED via the engine, the confirmation/approved event-type mapping, amount backfill, the ref-less amount-absent
fail-closed limitation, and BOTH reference-path IntegrityError race-recovery branches (the create path AND the
adoption path — the latter is the savepoint parity fix). No genuine sample: every event is Provenance.SYNTHETIC.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError
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
    return "93%04d" % _n


def _acct(**kw):
    login = _uniq()
    u = User.objects.create_user(username="wp5l%s" % login, email="%s@x.invalid" % login, password="x")
    d = dict(user=u, name="A", broker_name="TradersWay", account_number=login, is_demo=True, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


def _event(account, event_type, *, category=TransactionCategory.EXTERNAL_WITHDRAWAL, ref="",
           amount="200.00", currency="USD", occurred_at=None, received_at=None):
    return BrokerEvent.objects.create(
        trading_account=account, broker="TradersWay", event_type=event_type, transaction_category=category,
        broker_reference_id=ref, amount=(Decimal(amount) if amount is not None else None), currency=currency,
        occurred_at=occurred_at, received_at=received_at or timezone.now(), provenance=Provenance.SYNTHETIC,
        correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)


def _flaky(real):
    """A _find_by_reference side effect that MISSES on the first call (forcing the create/adoption branch) and is
    real thereafter (so the except recovery finds the concurrent winner). Simulates the race interleaving."""
    state = {"n": 0}

    def side_effect(ev):
        state["n"] += 1
        return None if state["n"] == 1 else real(ev)
    return side_effect


class LifecycleLadder(TestCase):
    def test_forward_ladder_requested_processing_completed_then_no_regress(self):
        a = _acct()
        t0 = timezone.now() - timedelta(days=2)
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="L-1", occurred_at=t0))
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.REQUESTED)
        C.correlate_event(_event(a, "WITHDRAWAL_PROCESSING", ref="L-1", occurred_at=timezone.now() - timedelta(days=1)))
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.PROCESSING)
        self.assertEqual(w.requested_at, t0)                      # anchor preserved across the advance
        C.correlate_event(_event(a, "WITHDRAWAL_COMPLETED", ref="L-1", occurred_at=timezone.now()))
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)
        # a late PROCESSING must NOT regress a COMPLETED withdrawal
        C.correlate_event(_event(a, "WITHDRAWAL_PROCESSING", ref="L-1", occurred_at=timezone.now()))
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)
        self.assertEqual(Withdrawal.objects.count(), 1)

    def test_rejected_terminal_status_not_flipped_by_later_completed(self):
        a = _acct()
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="T-1", occurred_at=timezone.now() - timedelta(hours=2)))
        C.correlate_event(_event(a, "WITHDRAWAL_REJECTED", ref="T-1", occurred_at=timezone.now() - timedelta(hours=1)))
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.REJECTED)
        # a COMPLETED arriving after a terminal REJECTED must NOT flip the STATUS (both are rank-3 terminals). The
        # engine may backfill completed_at as evidence, but STATUS is the authority and metrics count COMPLETED-status
        # rows only, so a rejected withdrawal can never enter completed totals (packet §7).
        r = C.correlate_event(_event(a, "WITHDRAWAL_COMPLETED", ref="T-1", occurred_at=timezone.now()))
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.REJECTED)     # status unchanged (no flip)
        self.assertEqual(r.outcome, C.OUT_REFERENCE)               # event consumed, not errored

    def test_completed_terminal_not_flipped_by_later_rejected(self):
        a = _acct()
        t0 = timezone.now() - timedelta(hours=2)
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="T-2", occurred_at=t0))
        C.correlate_event(_event(a, "WITHDRAWAL_COMPLETED", ref="T-2", occurred_at=timezone.now() - timedelta(hours=1)))
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)
        completed_at = w.completed_at
        C.correlate_event(_event(a, "WITHDRAWAL_REJECTED", ref="T-2", occurred_at=timezone.now()))
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)    # a completed withdrawal never regresses to rejected
        self.assertEqual(w.completed_at, completed_at)             # completion anchor stable

    def test_cancelled_terminal_via_engine(self):
        a = _acct()
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="X-1", occurred_at=timezone.now() - timedelta(hours=1)))
        C.correlate_event(_event(a, "WITHDRAWAL_CANCELLED", ref="X-1", occurred_at=timezone.now()))
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.CANCELLED)
        self.assertIsNone(w.completed_at)
        self.assertIsNone(w.duration_seconds())

    def test_confirmation_and_confirmed_map_to_requested(self):
        for et in ("WITHDRAWAL_CONFIRMATION_REQUIRED", "WITHDRAWAL_CONFIRMED"):
            a = _acct()
            C.correlate_event(_event(a, et, ref="C-%s" % et, occurred_at=timezone.now()))
            w = Withdrawal.objects.get(trading_account=a)
            self.assertEqual(w.status, Withdrawal.Status.REQUESTED, et)

    def test_approved_maps_to_processing(self):
        a = _acct()
        C.correlate_event(_event(a, "WITHDRAWAL_APPROVED", ref="AP-1", occurred_at=timezone.now()))
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.PROCESSING)


class AmountBackfill(TestCase):
    def test_amount_backfilled_from_completion_when_request_had_none(self):
        a = _acct()
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="B-1", amount=None,
                                 occurred_at=timezone.now() - timedelta(days=1)))
        w = Withdrawal.objects.get()
        self.assertIsNone(w.amount)
        C.correlate_event(_event(a, "WITHDRAWAL_COMPLETED", ref="B-1", amount="200.00", occurred_at=timezone.now()))
        w.refresh_from_db()
        self.assertEqual(w.amount, Decimal("200.00"))              # backfilled from positive evidence

    def test_known_amount_not_overwritten_by_absent(self):
        a = _acct()
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="B-2", amount="200.00",
                                 occurred_at=timezone.now() - timedelta(days=1)))
        C.correlate_event(_event(a, "WITHDRAWAL_COMPLETED", ref="B-2", amount=None, occurred_at=timezone.now()))
        w = Withdrawal.objects.get()
        self.assertEqual(w.amount, Decimal("200.00"))              # a None completion never nulls a known amount

    def test_refless_amount_absent_completion_is_unresolved_never_guessed(self):
        # The heuristic keys on an exact amount; a ref-less REQUESTED with no amount can never be matched by a later
        # amount-bearing COMPLETED (fail-closed) — the completion stays UNRESOLVED, the request keeps amount=None.
        a = _acct()
        req = _event(a, "WITHDRAWAL_REQUESTED", ref="", amount=None, occurred_at=timezone.now() - timedelta(hours=1))
        C.correlate_event(req)
        comp = _event(a, "WITHDRAWAL_COMPLETED", ref="", amount="200.00", occurred_at=timezone.now())
        r = C.correlate_event(comp)
        self.assertEqual(r.outcome, C.OUT_UNRESOLVED)
        w = Withdrawal.objects.get(requested_event=req)
        self.assertIsNone(w.amount)                                # no guessed backfill


class ReferenceRaceRecovery(TestCase):
    def test_create_path_integrity_race_recovers_against_winner(self):
        a = _acct()
        winner = Withdrawal.objects.create(
            trading_account=a, broker_reference_id="W-RACE", amount=Decimal("200.00"), currency="USD",
            status=Withdrawal.Status.REQUESTED, correlation_method=Withdrawal.CorrelationMethod.REFERENCE_ID,
            provenance=Provenance.SYNTHETIC)
        comp = _event(a, "WITHDRAWAL_COMPLETED", ref="W-RACE", amount="200.00", occurred_at=timezone.now())
        # top-level find MISSES (forces the create branch); the real (account,ref) unique then raises IntegrityError
        # on insert; the except recovery must re-find the winner on a still-usable transaction and advance it.
        with mock.patch.object(C, "_find_by_reference", side_effect=_flaky(C._find_by_reference)):
            r = C.correlate_event(comp)
        self.assertEqual(r.outcome, C.OUT_REFERENCE)
        self.assertEqual(r.withdrawal_id, winner.id)
        winner.refresh_from_db()
        self.assertEqual(winner.status, Withdrawal.Status.COMPLETED)
        self.assertEqual(Withdrawal.objects.count(), 1)            # no duplicate created

    def test_adoption_path_integrity_race_recovers_against_winner(self):
        # Exercise the SAVEPOINT PARITY FIX: a single ref-less heuristic candidate is promoted to the ref, but a
        # concurrent (account,ref) winner already holds it; the promote-save raises a real IntegrityError that must
        # abort only the adoption savepoint and recover against the winner (never poison the outer transaction).
        a = _acct()
        C.correlate_event(_event(a, "WITHDRAWAL_REQUESTED", ref="", amount="250.00",
                                 occurred_at=timezone.now() - timedelta(days=1)))
        self.assertEqual(Withdrawal.objects.filter(broker_reference_id="").count(), 1)
        winner = Withdrawal.objects.create(
            trading_account=a, broker_reference_id="TW-RACE", amount=Decimal("250.00"), currency="USD",
            status=Withdrawal.Status.REQUESTED, correlation_method=Withdrawal.CorrelationMethod.REFERENCE_ID,
            provenance=Provenance.SYNTHETIC)
        comp = _event(a, "WITHDRAWAL_COMPLETED", ref="TW-RACE", amount="250.00", occurred_at=timezone.now())
        with mock.patch.object(C, "_find_by_reference", side_effect=_flaky(C._find_by_reference)):
            r = C.correlate_event(comp)
        self.assertEqual(r.outcome, C.OUT_REFERENCE)
        self.assertEqual(r.withdrawal_id, winner.id)
        winner.refresh_from_db()
        self.assertEqual(winner.status, Withdrawal.Status.COMPLETED)
        # the original ref-less heuristic row survives unchanged (rolled back), never merged or advanced
        orphan = Withdrawal.objects.get(broker_reference_id="")
        self.assertEqual(orphan.status, Withdrawal.Status.REQUESTED)
        self.assertEqual(Withdrawal.objects.count(), 2)
