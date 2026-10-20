"""WV1 §4 / ADR-0050 — owner-attributed, account-UNRESOLVED withdrawal correlation.

Covers the genuine-pilot shape: an EXTERNAL_WITHDRAWAL request whose TradersWay account is NOT registered in GuvFX is
projected OWNER-attributed with trading_account NULL; a neutral COMPLETED-lifecycle event (category UNKNOWN — the
genuine payout completion carries no rail/direction) ADVANCES that withdrawal by the shared authenticated reference
and NEVER creates one. Also: cross-owner isolation, idempotency, account-resolved-request + account-less-completion,
and that a deposit/other completion whose reference matches nothing has no effect (no metric inflation).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
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
    return "97%04d" % _n


def _owner():
    login = _uniq()
    return User.objects.create_user(username="ow%s" % login, email="%s@x.invalid" % login, password="x")


def _acct(owner, number=None):
    return TradingAccount.objects.create(user=owner, name="A", broker_name="TradersWay",
                                         account_number=number or _uniq(), is_demo=True, is_active=True)


def _event(*, owner=None, account=None, event_type="WITHDRAWAL_CONFIRMATION_REQUIRED",
           category=TransactionCategory.EXTERNAL_WITHDRAWAL, ref="", amount="250.00", currency="USD",
           occurred_at=None, received_at=None):
    return BrokerEvent.objects.create(
        owner=owner, trading_account=account, broker="TradersWay", event_type=event_type,
        transaction_category=category, broker_reference_id=ref,
        amount=(Decimal(amount) if amount is not None else None), currency=currency,
        occurred_at=occurred_at, received_at=received_at or timezone.now(), provenance=Provenance.SYNTHETIC,
        correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)


class OwnerAttributedCreation(TestCase):
    def test_owner_only_external_request_creates_account_unresolved_withdrawal(self):
        u = _owner()
        req = _event(owner=u, account=None, ref="W-1", occurred_at=timezone.now())
        res = C.correlate_event(req)
        self.assertEqual(res.outcome, C.OUT_CREATED)
        w = Withdrawal.objects.get()
        self.assertIsNone(w.trading_account_id)                 # account UNRESOLVED
        self.assertEqual(w.owner_id, u.id)                      # OWNER-attributed
        self.assertEqual(w.status, Withdrawal.Status.REQUESTED)
        self.assertEqual(w.broker_reference_id, "W-1")

    def test_owner_scoped_reference_is_idempotent(self):
        u = _owner()
        req = _event(owner=u, account=None, ref="W-1")
        C.correlate_event(req)
        # Re-ingest the SAME content as a distinct row (re-run safety) -> owner-scoped unique keeps ONE withdrawal.
        req2 = _event(owner=u, account=None, ref="W-1")
        C.correlate_event(req2)
        self.assertEqual(Withdrawal.objects.filter(owner=u, trading_account__isnull=True).count(), 1)

    def test_two_owners_same_reference_are_two_withdrawals(self):
        a, b = _owner(), _owner()
        C.correlate_event(_event(owner=a, account=None, ref="W-9"))
        C.correlate_event(_event(owner=b, account=None, ref="W-9"))
        self.assertEqual(Withdrawal.objects.count(), 2)         # owner-scoped key separates them


class AdvanceOnlyCompletion(TestCase):
    def test_genuine_pair_request_then_neutral_completion(self):
        # The genuine shape: EXTERNAL request (owner, account-None, ref) + neutral UNKNOWN/COMPLETED completion sharing
        # the ref, 33 minutes apart. One owner-attributed withdrawal, REQUESTED -> COMPLETED, with a duration.
        u = _owner()
        t0 = timezone.now() - timedelta(minutes=33, seconds=1)
        t1 = timezone.now()
        req = _event(owner=u, account=None, event_type="WITHDRAWAL_CONFIRMATION_REQUIRED",
                     category=TransactionCategory.EXTERNAL_WITHDRAWAL, ref="7654321", occurred_at=t0)
        comp = _event(owner=u, account=None, event_type="WITHDRAWAL_COMPLETED",
                      category=TransactionCategory.UNKNOWN, ref="7654321", amount=None, currency="", occurred_at=t1)
        summary = C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 1)
        w = Withdrawal.objects.get()
        self.assertEqual(w.owner_id, u.id)
        self.assertIsNone(w.trading_account_id)
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)
        self.assertEqual(w.requested_event_id, req.id)
        self.assertEqual(w.completed_event_id, comp.id)
        self.assertEqual(w.duration_seconds(), int((t1 - t0).total_seconds()))   # 1981s, EMAIL-to-EMAIL interval
        self.assertEqual(summary.get(C.OUT_COMPLETION_ADVANCED), 1)
        comp.refresh_from_db()
        self.assertEqual(comp.correlation_status, BrokerEvent.CorrelationStatus.CORRELATED)

    def test_neutral_completion_alone_never_creates(self):
        # A completion with NO matching external request must leave UNRESOLVED and create NOTHING (externality is only
        # ever asserted by the request). This is what makes a deposit/other completion harmless.
        u = _owner()
        comp = _event(owner=u, account=None, event_type="WITHDRAWAL_COMPLETED",
                      category=TransactionCategory.UNKNOWN, ref="ORPHAN-1", amount=None, currency="")
        summary = C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 0)
        self.assertEqual(summary.get(C.OUT_UNRESOLVED), 1)
        comp.refresh_from_db()
        self.assertEqual(comp.correlation_status, BrokerEvent.CorrelationStatus.UNRESOLVED)   # re-evaluable later

    def test_completion_is_not_projected_by_pass1(self):
        # correlate_event (pass 1) must SKIP a non-external completion; only run_correlation's pass 2 advances it.
        u = _owner()
        comp = _event(owner=u, account=None, event_type="WITHDRAWAL_COMPLETED",
                      category=TransactionCategory.UNKNOWN, ref="W-1", amount=None, currency="")
        self.assertEqual(C.correlate_event(comp).outcome, C.OUT_SKIP_NOT_EXTERNAL)

    def test_deposit_completion_with_unmatched_ref_has_no_effect(self):
        # A DEPOSIT completion (UNKNOWN/COMPLETED) whose reference matches no external withdrawal cannot inflate: a
        # genuine external withdrawal for the same owner with a DIFFERENT ref is untouched.
        u = _owner()
        C.correlate_event(_event(owner=u, account=None, ref="WITHDRAW-1"))
        _event(owner=u, account=None, event_type="WITHDRAWAL_COMPLETED", category=TransactionCategory.UNKNOWN,
               ref="DEPOSIT-1", amount=None, currency="")
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 1)
        self.assertEqual(Withdrawal.objects.get().status, Withdrawal.Status.REQUESTED)   # NOT completed by the deposit

    def test_cross_owner_completion_does_not_advance_other_owners_withdrawal(self):
        a, b = _owner(), _owner()
        C.correlate_event(_event(owner=a, account=None, ref="SHARED-REF"))
        # Owner B's completion with the SAME ref must not complete owner A's withdrawal.
        _event(owner=b, account=None, event_type="WITHDRAWAL_COMPLETED", category=TransactionCategory.UNKNOWN,
               ref="SHARED-REF", amount=None, currency="")
        C.run_correlation()
        wa = Withdrawal.objects.get(owner=a)
        self.assertEqual(wa.status, Withdrawal.Status.REQUESTED)       # untouched by B's completion
        self.assertIsNone(wa.completed_at)

    def test_account_resolved_request_then_account_less_completion_advances_by_owner(self):
        # Common shape: the REQUEST resolved a GuvFX account; the COMPLETION email carries no account, only the shared
        # reference. The completion advances the account-resolved withdrawal via (owner, ref).
        u = _owner()
        acct = _acct(u)
        req = _event(owner=u, account=acct, ref="W-77", occurred_at=timezone.now() - timedelta(minutes=10))
        C.correlate_event(req)
        _event(owner=u, account=None, event_type="WITHDRAWAL_COMPLETED", category=TransactionCategory.UNKNOWN,
               ref="W-77", amount=None, currency="", occurred_at=timezone.now())
        C.run_correlation()
        w = Withdrawal.objects.get()
        self.assertEqual(w.trading_account_id, acct.id)
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)

    def test_account_resolved_request_then_account_less_external_advance_no_duplicate(self):
        # Review regression (HIGH double-count): an account-RESOLVED EXTERNAL request creates W1(account=A); a later
        # account-LESS EXTERNAL advancing/completion event for the SAME withdrawal (restates the rail, omits the
        # account) must ADVANCE W1 via the owner bridge, NOT create a parallel account-null row.
        u = _owner()
        acct = _acct(u)
        req = _event(owner=u, account=acct, ref="DUP-1", occurred_at=timezone.now() - timedelta(minutes=5))
        C.correlate_event(req)
        # account-less EXTERNAL advancing event (PROCESSING) sharing the reference
        proc = _event(owner=u, account=None, event_type="WITHDRAWAL_PROCESSING",
                      category=TransactionCategory.EXTERNAL_WITHDRAWAL, ref="DUP-1", amount=None, currency="")
        res = C.correlate_event(proc)
        self.assertEqual(res.outcome, C.OUT_REFERENCE)                    # advanced, not created
        self.assertEqual(Withdrawal.objects.count(), 1)                   # NO duplicate
        w = Withdrawal.objects.get()
        self.assertEqual(w.trading_account_id, acct.id)
        self.assertEqual(w.status, Withdrawal.Status.PROCESSING)

    def test_account_less_request_then_account_resolved_external_advance_no_duplicate(self):
        # The symmetric reverse order must also converge to ONE withdrawal.
        u = _owner()
        acct = _acct(u)
        req = _event(owner=u, account=None, ref="DUP-2", occurred_at=timezone.now() - timedelta(minutes=5))
        C.correlate_event(req)
        adv = _event(owner=u, account=acct, event_type="WITHDRAWAL_APPROVED",
                     category=TransactionCategory.EXTERNAL_WITHDRAWAL, ref="DUP-2", amount=None, currency="")
        res = C.correlate_event(adv)
        self.assertEqual(res.outcome, C.OUT_REFERENCE)
        self.assertEqual(Withdrawal.objects.count(), 1)                   # NO duplicate

    def test_account_resolved_request_then_account_less_external_completed_no_duplicate(self):
        # And the account-less EXTERNAL *completion* variant (a completion that DID classify external) also advances.
        u = _owner()
        acct = _acct(u)
        C.correlate_event(_event(owner=u, account=acct, ref="DUP-3",
                                 occurred_at=timezone.now() - timedelta(minutes=5)))
        C.correlate_event(_event(owner=u, account=None, event_type="WITHDRAWAL_COMPLETED",
                                 category=TransactionCategory.EXTERNAL_WITHDRAWAL, ref="DUP-3",
                                 amount=None, currency="", occurred_at=timezone.now()))
        self.assertEqual(Withdrawal.objects.count(), 1)
        self.assertEqual(Withdrawal.objects.get().status, Withdrawal.Status.COMPLETED)

    def test_run_correlation_is_idempotent(self):
        u = _owner()
        _event(owner=u, account=None, category=TransactionCategory.EXTERNAL_WITHDRAWAL, ref="W-1",
               occurred_at=timezone.now() - timedelta(minutes=5))
        _event(owner=u, account=None, event_type="WITHDRAWAL_COMPLETED", category=TransactionCategory.UNKNOWN,
               ref="W-1", amount=None, currency="", occurred_at=timezone.now())
        C.run_correlation()
        C.run_correlation()          # second pass must be a no-op
        self.assertEqual(Withdrawal.objects.count(), 1)
        self.assertEqual(Withdrawal.objects.get().status, Withdrawal.Status.COMPLETED)
