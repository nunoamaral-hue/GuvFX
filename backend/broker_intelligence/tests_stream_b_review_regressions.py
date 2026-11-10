"""Stream B (ADR-0079) — regression tests for the adversarial-review findings.

Each test pins one confirmed review finding so it cannot silently return:
 * BucketCoverage           — metrics.py / withdrawal_speed.py bucket every Withdrawal.Status (the live
                              CONFIRMATION_REQUIRED/CONFIRMED regression + the EXPIRED/FAILED gap).
 * OwnerBridgeIsolation     — _find_by_reference must not attach an account-resolved event to a DIFFERENT account's
                              withdrawal of the same owner (cross-account corruption via the new pass 2b).
 * MigrationReverseGuard    — migration 0026 reverse must not silently destroy activated/edited profiles or their
                              append-only audit history.
"""
from __future__ import annotations

import importlib
from datetime import timedelta
from decimal import Decimal

from django.apps import apps as global_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from broker_intelligence import correlation as C, metrics, withdrawal_speed
from broker_intelligence.models import (BrokerEvent, BrokerProfile, BrokerProfileAudit, Provenance,
                                        TransactionCategory, Withdrawal)
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "93%04d" % _n


class BucketCoverage(TestCase):
    """Every Withdrawal.Status must land in exactly one member-facing bucket, in BOTH read models — so a newly added
    status can never silently fall out of pending/completed/failed/unresolved (the review's live regression)."""

    def _assert_total_partition(self, pending, failed):
        pending, failed = set(pending), set(failed)
        completed = {Withdrawal.Status.COMPLETED}
        unresolved = {Withdrawal.Status.UNRESOLVED}
        buckets = [pending, completed, failed, unresolved]
        all_statuses = set(Withdrawal.Status.values)
        # exhaustive: every declared status is bucketed
        self.assertSetEqual(pending | completed | failed | unresolved, all_statuses)
        # disjoint: no status is in two buckets
        for i in range(len(buckets)):
            for j in range(i + 1, len(buckets)):
                self.assertSetEqual(buckets[i] & buckets[j], set())
        # the Stream B statuses are bucketed as intended
        self.assertIn(Withdrawal.Status.CONFIRMATION_REQUIRED, pending)
        self.assertIn(Withdrawal.Status.CONFIRMED, pending)
        self.assertIn(Withdrawal.Status.EXPIRED, failed)
        self.assertIn(Withdrawal.Status.FAILED, failed)

    def test_metrics_buckets_partition_all_statuses(self):
        self._assert_total_partition(metrics._PENDING, metrics._FAILED)
        # _STATE must also cover every status (no implicit 'unresolved' fallback for a real status)
        for s in Withdrawal.Status.values:
            self.assertIn(s, metrics._STATE, s)

    def test_withdrawal_speed_buckets_partition_all_statuses(self):
        self._assert_total_partition(withdrawal_speed._PENDING, withdrawal_speed._FAILED)


class OwnerBridgeIsolation(TestCase):
    """One owner holding two accounts at a broker, with a reference reused across those accounts (the schema's
    per-account uniqueness allows it). An account-resolved neutral event for A2 must NEVER advance A1's withdrawal."""

    def _user(self):
        login = _uniq()
        return User.objects.create_user(username="obr%s" % login, email="%s@x.invalid" % login, password="x")

    def _acct(self, user):
        login = _uniq()
        return TradingAccount.objects.create(user=user, name="A", broker_name="Pepperstone", account_number=login,
                                             is_demo=False, is_active=True)

    def _event(self, *, user, account, event_type, category, ref, amount="15.00", currency="GBP", minutes=0):
        t = timezone.now() - timedelta(days=1) + timedelta(minutes=minutes)
        return BrokerEvent.objects.create(
            trading_account=account, owner=user, broker="Pepperstone", event_type=event_type,
            transaction_category=category, broker_reference_id=ref,
            amount=(Decimal(amount) if amount is not None else None), currency=currency,
            occurred_at=t, received_at=t, provenance=Provenance.SYNTHETIC,
            correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)

    def test_account_resolved_neutral_event_cannot_advance_a_sibling_accounts_withdrawal(self):
        u = self._user()
        a1, a2 = self._acct(u), self._acct(u)
        # W_A1: a real external withdrawal on A1, owner-attributed, reference '100'.
        self._event(user=u, account=a1, event_type="WITHDRAWAL_REQUESTED",
                    category=TransactionCategory.EXTERNAL_WITHDRAWAL, ref="100", minutes=0)
        # A2 reuses reference '100' for a DIFFERENT withdrawal whose follow-up is UNKNOWN (no rail re-asserted).
        self._event(user=u, account=a2, event_type="WITHDRAWAL_PROCESSING",
                    category=TransactionCategory.UNKNOWN, ref="100", minutes=30)
        C.run_correlation()
        w_a1 = Withdrawal.objects.get(trading_account=a1)
        self.assertEqual(w_a1.status, Withdrawal.Status.REQUESTED)       # A2's event did NOT advance A1 to PROCESSING
        self.assertIsNone(w_a1.processing_started_at)                    # nor stamp A1 with A2's evidence
        self.assertFalse(Withdrawal.objects.filter(trading_account=a2).exists())  # advance-only: no A2 row fabricated

    def test_account_less_follow_up_still_bridges_to_the_owners_resolved_withdrawal(self):
        # The legitimate mixed-resolution case must keep working: an account-LESS follow-up (owner + ref only)
        # advances the owner's account-resolved withdrawal.
        u = self._user()
        a1 = self._acct(u)
        self._event(user=u, account=a1, event_type="WITHDRAWAL_REQUESTED",
                    category=TransactionCategory.EXTERNAL_WITHDRAWAL, ref="200", minutes=0)
        # account-less neutral processing follow-up (trading_account=None), same owner + ref
        t = timezone.now() - timedelta(minutes=5)
        BrokerEvent.objects.create(
            trading_account=None, owner=u, broker="Pepperstone", event_type="WITHDRAWAL_PROCESSING",
            transaction_category=TransactionCategory.UNKNOWN, broker_reference_id="200",
            amount=Decimal("15.00"), currency="GBP", occurred_at=t, received_at=t, provenance=Provenance.SYNTHETIC,
            correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)
        C.run_correlation()
        w = Withdrawal.objects.get(trading_account=a1)
        self.assertEqual(w.status, Withdrawal.Status.PROCESSING)         # the bridge still advances the real record
        self.assertIsNotNone(w.processing_started_at)


class MigrationReverseGuard(TestCase):
    """Migration 0026 reverse must refuse to destroy an activated / edited profile and its append-only audit history,
    while cleanly removing an untouched seed."""

    def setUp(self):
        self.mod = importlib.import_module("broker_intelligence.migrations.0026_stream_b_broker_profiles")
        self.slugs = ["pepperstone", "dominion_markets", "vantage"]

    def test_untouched_seed_reverses_cleanly(self):
        # Migration 0027 (GFX-PEPPERSTONE-WITHDRAWAL-DEMO-003) activates Pepperstone and migration 0030
        # (GFX-WITHDRAWAL-VANTAGE-CORRELATE-EXEC-019 / ADR-0090) activates Vantage in the migrated test DB. To
        # exercise 0026's clean reverse of an UNTOUCHED seed (the behaviour under test, which neither 0027 nor 0030
        # changes), first restore BOTH activated profiles to their pristine seed state — mirroring a world where those
        # later activations were never applied.
        for slug in ("pepperstone", "vantage"):
            bp = BrokerProfile.objects.get(slug=slug)
            BrokerProfileAudit.objects.filter(broker_profile=bp).exclude(changed_by__icontains="0026").delete()
            BrokerProfile.objects.filter(slug=slug).update(is_active=False, provenance="SEED")
        # The three profiles are now seeded-untouched in the test DB (is_active=False, SEED, one CREATE audit).
        self.assertEqual(BrokerProfile.objects.filter(slug__in=self.slugs).count(), 3)
        self.mod._unseed(global_apps, None)
        self.assertEqual(BrokerProfile.objects.filter(slug__in=self.slugs).count(), 0)

    def test_reverse_refuses_to_destroy_an_activated_profile(self):
        bp = BrokerProfile.objects.get(slug="pepperstone")       # first in _SLUGS, so no sibling is deleted first
        bp.is_active = True
        bp.save()                                                # real save() appends an append-only UPDATE audit row
        with self.assertRaises(RuntimeError):
            self.mod._unseed(global_apps, None)
        self.assertTrue(BrokerProfile.objects.filter(slug="pepperstone").exists())   # preserved, not destroyed
        self.assertEqual(BrokerProfile.objects.filter(slug__in=self.slugs).count(), 3)
