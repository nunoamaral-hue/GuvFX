"""Stream B (ADR-0079) — the Pepperstone GBP-15 card confirmation-required lifecycle, END-TO-END.

The SYNTHETIC, PII-free Pepperstone fixtures (from ``tests_parsers_stream_b``) are run through the REAL deterministic
parser and the REAL correlation engine (``run_correlation`` — the same two-pass driver the DARK worker uses). This is
the packet's headline safety scenario: the first email is CONFIRMATION_REQUIRED, never COMPLETED; confirmation and
completion advance idempotently; a missing confirmation never auto-completes; out-of-order and duplicate deliveries
produce no duplicate or inflated record; and an unrelated account can never attach to another account's withdrawal.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from broker_intelligence import correlation as C
from broker_intelligence.models import BrokerEvent, Provenance, Withdrawal
from broker_intelligence.parsers_stream_b import PEPPERSTONE
from broker_intelligence import tests_parsers_stream_b as F
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _acct(**kw):
    global _n
    _n += 1
    login = "93%04d" % _n
    u = User.objects.create_user(username="sbs%s" % login, email="%s@x.invalid" % login, password="x")
    d = dict(user=u, name="A", broker_name="Pepperstone", account_number=login, is_demo=False, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


def _mk_event(account, fixture, *, minutes, ref=None):
    """Parse a synthetic Pepperstone fixture with the REAL parser and persist it as a BrokerEvent for ``account``.
    ``minutes`` orders events in broker-stated time; ``ref`` overrides the fixture reference (cross-account test)."""
    parsed = PEPPERSTONE.parse(**fixture)
    assert parsed is not None, fixture["subject"]
    occurred = timezone.now() - timedelta(days=1) + timedelta(minutes=minutes)
    return BrokerEvent.objects.create(
        trading_account=account, broker=parsed.broker, event_type=parsed.event_type,
        transaction_category=parsed.transaction_category,
        broker_reference_id=(ref if ref is not None else parsed.broker_reference_id),
        amount=parsed.amount, currency=parsed.currency, occurred_at=occurred, received_at=occurred,
        provenance=Provenance.SYNTHETIC, correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)


class FirstEmailIsConfirmationNotCompletion(TestCase):
    def test_confirmation_required_opens_at_confirmation_required_never_completed(self):
        a = _acct()
        _mk_event(a, F.PS_CONFIRMATION_REQUIRED, minutes=0)
        C.run_correlation()
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.CONFIRMATION_REQUIRED)   # NOT completed from the first email
        self.assertIsNotNone(w.confirmation_required_at)                      # stage anchor stamped
        self.assertIsNone(w.completed_at)
        self.assertIsNone(w.confirmed_at)
        self.assertEqual(str(w.amount), "15.00")
        self.assertEqual(w.currency, "GBP")


class FullLadderAndIdempotency(TestCase):
    def test_confirmation_confirmed_processing_completed_single_record(self):
        a = _acct()
        _mk_event(a, F.PS_CONFIRMATION_REQUIRED, minutes=0)
        _mk_event(a, F.PS_CONFIRMED, minutes=10)
        _mk_event(a, F.PS_PROCESSING, minutes=20)
        _mk_event(a, F.PS_COMPLETED, minutes=30)
        C.run_correlation()
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)
        self.assertIsNotNone(w.confirmation_required_at)
        self.assertIsNotNone(w.confirmed_at)
        self.assertIsNotNone(w.processing_started_at)
        self.assertIsNotNone(w.completed_at)
        self.assertEqual(Withdrawal.objects.count(), 1)

    def test_rerun_is_idempotent(self):
        a = _acct()
        _mk_event(a, F.PS_CONFIRMATION_REQUIRED, minutes=0)
        _mk_event(a, F.PS_COMPLETED, minutes=30)
        C.run_correlation()
        w1 = Withdrawal.objects.get()
        completed_at = w1.completed_at
        C.run_correlation()                 # a second pass changes nothing
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 1)
        w1.refresh_from_db()
        self.assertEqual(w1.status, Withdrawal.Status.COMPLETED)
        self.assertEqual(w1.completed_at, completed_at)


class OutOfOrderDelivery(TestCase):
    def test_completion_before_request_never_fabricates_then_resolves(self):
        a = _acct()
        # The COMPLETED email (category UNKNOWN, carries the ref) arrives and is correlated FIRST.
        _mk_event(a, F.PS_COMPLETED, minutes=30)
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 0)     # a completion NEVER creates a withdrawal
        # The confirmation-required email arrives later; a re-run opens the record AND advances the pending completion.
        _mk_event(a, F.PS_CONFIRMATION_REQUIRED, minutes=0)
        C.run_correlation()
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)
        self.assertIsNotNone(w.confirmation_required_at)
        self.assertEqual(Withdrawal.objects.count(), 1)

    def test_late_confirmed_does_not_regress_a_completed_withdrawal(self):
        a = _acct()
        _mk_event(a, F.PS_CONFIRMATION_REQUIRED, minutes=0)
        _mk_event(a, F.PS_COMPLETED, minutes=30)
        C.run_correlation()
        # a CONFIRMED (rank 3) arriving after COMPLETED (rank 5) must not pull the status backwards
        _mk_event(a, F.PS_CONFIRMED, minutes=40)
        C.run_correlation()
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)


class DuplicateDelivery(TestCase):
    def test_duplicate_confirmation_email_yields_one_withdrawal(self):
        a = _acct()
        _mk_event(a, F.PS_CONFIRMATION_REQUIRED, minutes=0)
        _mk_event(a, F.PS_CONFIRMATION_REQUIRED, minutes=1)   # a second delivery of the same logical email
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 1)
        self.assertEqual(Withdrawal.objects.get().status, Withdrawal.Status.CONFIRMATION_REQUIRED)


class MissingConfirmation(TestCase):
    def test_unconfirmed_withdrawal_stays_confirmation_required(self):
        a = _acct()
        _mk_event(a, F.PS_CONFIRMATION_REQUIRED, minutes=0)
        C.run_correlation()
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.CONFIRMATION_REQUIRED)   # never auto-advances without evidence
        self.assertIsNone(w.completed_at)

    def test_explicit_expiry_event_advances_to_expired(self):
        a = _acct()
        _mk_event(a, F.PS_CONFIRMATION_REQUIRED, minutes=0)
        _mk_event(a, F.PS_EXPIRED, minutes=120)
        C.run_correlation()
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.EXPIRED)                 # only on EXPLICIT expiry evidence
        self.assertIsNone(w.completed_at)                                     # an expiry is not a completion


class CrossAccountIsolation(TestCase):
    def test_same_reference_on_two_accounts_never_merges(self):
        a1, a2 = _acct(), _acct()
        _mk_event(a1, F.PS_CONFIRMATION_REQUIRED, minutes=0, ref="SHARED-REF-1")
        _mk_event(a2, F.PS_CONFIRMATION_REQUIRED, minutes=0, ref="SHARED-REF-1")
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 2)                      # account-scoped: two distinct records
        self.assertEqual(Withdrawal.objects.filter(trading_account=a1).count(), 1)
        self.assertEqual(Withdrawal.objects.filter(trading_account=a2).count(), 1)

    def test_completion_for_one_account_cannot_complete_anothers_withdrawal(self):
        a1, a2 = _acct(), _acct()
        _mk_event(a1, F.PS_CONFIRMATION_REQUIRED, minutes=0, ref="REF-A1")
        # a completion carrying a DIFFERENT account's withdrawal reference must not touch a1's record
        _mk_event(a2, F.PS_COMPLETED, minutes=30, ref="REF-A1")
        C.run_correlation()
        w1 = Withdrawal.objects.get(trading_account=a1)
        self.assertEqual(w1.status, Withdrawal.Status.CONFIRMATION_REQUIRED)  # untouched by a2's completion
        self.assertFalse(Withdrawal.objects.filter(trading_account=a2).exists())  # completion fabricated nothing
