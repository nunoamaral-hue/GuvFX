"""Follower-ticket reconciliation certification (ADR-0052). READ-ONLY; identity-firewalled; fail-closed.

Discriminating negative controls: an ambiguous (>1) match NEVER picks a ticket; a refused/failed read
writes NOTHING (never assumes a flat follower); a SHADOW intent (no real order) is never reconciled."""
from decimal import Decimal

from django.test import TestCase, override_settings

from trade_copier import reconcile
from trade_copier.diff import ObservedPosition
from trade_copier.models import CopierReplication, ReplicationStatus
from trade_copier.observer import MasterReadResult
from trade_copier.tests_execution_producer import ProducerFixture

RECONCILE_ON = dict(COPIER_ENABLED=True, COPIER_RECONCILE_ENABLED=True)


def _fetch_positions(*positions):
    """A fake identity-verified read returning the given ObservedPositions (ok)."""
    def fetch(_account):
        return MasterReadResult(True, "ok", positions=tuple(positions), read_at=None)
    return fetch


def _fetch_refused(reason="snapshot_identity_login_mismatch"):
    def fetch(_account):
        return MasterReadResult(False, reason)
    return fetch


def _pos(ticket, comment, symbol="XAUUSD"):
    return ObservedPosition(ticket=ticket, symbol=symbol, side="BUY", volume=0.05, comment=comment)


class ReconcileFixture(ProducerFixture):
    def _executed_open(self, follower, *, ticket=None, status=ReplicationStatus.EXECUTION_REQUESTED):
        """An armed-DEMO OPEN intent (a real order was requested) awaiting ticket reconciliation."""
        return CopierReplication.objects.create(
            master_event=self.event, follower=follower, requested_op="OPEN", status=status,
            follower_symbol="XAUUSD", target_volume=Decimal("0.05"), sltp_translation_ok=True,
            follower_ticket=ticket)


class ReconcileTests(ReconcileFixture):
    def test_single_match_sets_ticket_matched(self):
        f = self._follower()
        rep = self._executed_open(f)
        out = reconcile.reconcile_follower(f, fetch=_fetch_positions(_pos(777, f"CPY{rep.id}")))
        self.assertTrue(out["ok"])
        self.assertEqual(out["matched"], 1)
        rep.refresh_from_db()
        self.assertEqual(rep.follower_ticket, 777)
        self.assertEqual(rep.reconciliation_status, reconcile.MATCHED)

    def test_no_match_is_unmatched_no_ticket(self):
        f = self._follower()
        rep = self._executed_open(f)
        out = reconcile.reconcile_follower(f, fetch=_fetch_positions(_pos(777, "CPY999999")))
        self.assertEqual(out["unmatched"], 1)
        rep.refresh_from_db()
        self.assertIsNone(rep.follower_ticket)
        self.assertEqual(rep.reconciliation_status, reconcile.UNMATCHED)

    def test_ambiguous_two_matches_never_picks(self):
        f = self._follower()
        rep = self._executed_open(f)
        tag = f"CPY{rep.id}"
        out = reconcile.reconcile_follower(f, fetch=_fetch_positions(_pos(777, tag), _pos(778, tag)))
        self.assertEqual(out["ambiguous"], 1)
        rep.refresh_from_db()
        self.assertIsNone(rep.follower_ticket)                       # NEVER picks one
        self.assertEqual(rep.reconciliation_status, reconcile.AMBIGUOUS)

    def test_refused_read_writes_nothing(self):
        f = self._follower()
        rep = self._executed_open(f)
        out = reconcile.reconcile_follower(f, fetch=_fetch_refused())
        self.assertFalse(out["ok"])
        rep.refresh_from_db()
        self.assertIsNone(rep.follower_ticket)
        self.assertEqual(rep.reconciliation_status, "")             # never assumed a flat follower

    def test_shadow_intent_not_reconciled(self):
        """A SHADOW OPEN reaches no broker — it must never be reconciled to a live ticket."""
        f = self._follower()
        rep = self._executed_open(f, status=ReplicationStatus.SHADOW_JOB_RECORDED)
        out = reconcile.reconcile_follower(f, fetch=_fetch_positions(_pos(777, f"CPY{rep.id}")))
        self.assertEqual(out["reason"], "nothing_to_reconcile")
        rep.refresh_from_db()
        self.assertIsNone(rep.follower_ticket)

    def test_already_reconciled_not_overwritten(self):
        f = self._follower()
        rep = self._executed_open(f, ticket=555)
        rep.reconciliation_status = reconcile.MATCHED
        rep.save(update_fields=["reconciliation_status"])
        # A fresh read with a DIFFERENT ticket for the same tag must not clobber the confirmed match.
        reconcile.reconcile_follower(f, fetch=_fetch_positions(_pos(999, f"CPY{rep.id}")))
        rep.refresh_from_db()
        self.assertEqual(rep.follower_ticket, 555)

    def test_reconcile_one_wrapper_matches(self):
        f = self._follower()
        rep = self._executed_open(f)
        out = reconcile.reconcile_one(rep, fetch=_fetch_positions(_pos(42, f"CPY{rep.id}")))
        self.assertTrue(out["ok"])
        self.assertEqual(out["follower_ticket"], 42)

    def test_reconcile_one_skips_non_executed(self):
        f = self._follower()
        rep = self._executed_open(f, status=ReplicationStatus.SHADOW_RECORDED)
        out = reconcile.reconcile_one(rep, fetch=_fetch_positions(_pos(42, f"CPY{rep.id}")))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "not_executed_demo")


class ReconcilePassGateTests(ReconcileFixture):
    def test_run_reconcile_pass_gated_off_inert(self):
        f = self._follower()
        rep = self._executed_open(f)
        out = reconcile.run_reconcile_pass(fetch=_fetch_positions(_pos(777, f"CPY{rep.id}")))
        self.assertFalse(out["enabled"])
        rep.refresh_from_db()
        self.assertIsNone(rep.follower_ticket)

    @override_settings(**RECONCILE_ON)
    def test_run_reconcile_pass_enabled_matches(self):
        f = self._follower()
        rep = self._executed_open(f)
        out = reconcile.run_reconcile_pass(fetch=_fetch_positions(_pos(777, f"CPY{rep.id}")))
        self.assertTrue(out["enabled"])
        self.assertEqual(out["matched"], 1)
        rep.refresh_from_db()
        self.assertEqual(rep.follower_ticket, 777)
