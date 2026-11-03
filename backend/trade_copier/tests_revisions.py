"""Hybrid EVENT-path revision gate certification (ADR-0071). Pure — no DB, no I/O.

The fast path's whole correctness guarantee is the strict-monotone per-ticket revision: a replay or a
re-ordered older emission can never move a ticket's state backwards, and a structurally-malformed emission is
dropped (not silently coerced). The poll path remains authoritative; these tests certify the event fold only.
"""
from datetime import datetime, timezone as _tz

from django.test import SimpleTestCase

from trade_copier.revisions import PositionRevision, fold_revisions, is_stale

T = datetime(2026, 11, 3, 12, 0, 0, tzinfo=_tz.utc)


def _rev(ticket, revision, *, closed=False, volume=0.10, sl=None, tp=None, side="BUY", symbol="XAUUSD"):
    return PositionRevision(account_id=46, ticket=ticket, revision=revision, observed_time=T,
                            closed=closed, symbol=symbol, side=side, volume=volume, sl=sl, tp=tp)


class RevisionStructureTests(SimpleTestCase):
    def test_valid_open_revision(self):
        self.assertEqual(_rev(100, 1).structurally_valid(), (True, "ok"))

    def test_bad_ticket_rejected(self):
        self.assertEqual(_rev(0, 1).structurally_valid()[1], "bad_ticket")

    def test_bad_revision_rejected(self):
        self.assertEqual(_rev(100, 0).structurally_valid()[1], "bad_revision")

    def test_bad_side_rejected(self):
        self.assertEqual(_rev(100, 1, side="LONG").structurally_valid()[1], "bad_side")

    def test_non_positive_volume_rejected(self):
        self.assertEqual(_rev(100, 1, volume=0).structurally_valid()[1], "non_positive_volume")

    def test_closed_revision_is_valid_without_levels(self):
        self.assertEqual(_rev(100, 5, closed=True, volume=0, side="").structurally_valid(), (True, "ok"))

    def test_closed_revision_has_no_position(self):
        with self.assertRaises(ValueError):
            _rev(100, 5, closed=True).to_observed_position()

    def test_zero_level_matches_poll_path_representation(self):
        # The event path uses the SAME diff._to_level as the poll path: 0.0 is carried verbatim on the
        # ObservedPosition and the diff engine treats 0.0/None equivalently as "no level" (diff._has_level).
        from trade_copier.diff import ObservedPosition
        pos = _rev(100, 1, sl=0.0, tp=2100.0).to_observed_position()
        self.assertEqual(pos.sl, ObservedPosition.from_payload(
            {"ticket": 100, "symbol": "XAUUSD", "side": "BUY", "volume": 0.10, "sl": 0.0, "tp": 2100.0}).sl)
        self.assertEqual(pos.tp, 2100.0)


class StaleRuleTests(SimpleTestCase):
    def test_nothing_applied_is_not_stale(self):
        self.assertFalse(is_stale(1, None))

    def test_equal_revision_is_stale(self):
        self.assertTrue(is_stale(5, 5))

    def test_lower_revision_is_stale(self):
        self.assertTrue(is_stale(4, 5))

    def test_higher_revision_is_fresh(self):
        self.assertFalse(is_stale(6, 5))


class FoldTests(SimpleTestCase):
    def test_highest_revision_wins_regardless_of_order(self):
        out = fold_revisions([_rev(100, 3, sl=1950), _rev(100, 1, sl=1990), _rev(100, 2, sl=1970)])
        self.assertEqual(out.applied_revision[100], 3)
        self.assertEqual(len(out.positions), 1)
        self.assertEqual(out.positions[0].sl, 1950.0)

    def test_duplicate_and_older_rejected(self):
        out = fold_revisions([_rev(100, 2), _rev(100, 2), _rev(100, 1)])
        self.assertEqual(out.applied_revision[100], 2)
        self.assertEqual(len([r for r in out.rejected if r[2] == "stale_revision"]), 2)

    def test_closed_winner_removes_ticket(self):
        out = fold_revisions([_rev(100, 1), _rev(100, 2, closed=True, volume=0, side="")])
        self.assertEqual(out.positions, ())                # the newer CLOSED revision wins → no open position
        self.assertEqual(out.applied_revision[100], 2)

    def test_prior_applied_seeds_hwm_and_rejects_replay(self):
        out = fold_revisions([_rev(100, 2)], prior_applied={100: 2})
        self.assertEqual(out.positions, ())                # revision 2 already applied earlier → no-op
        self.assertEqual(out.rejected[0][2], "stale_revision")

    def test_malformed_revision_dropped_not_folded(self):
        out = fold_revisions([_rev(100, 1), _rev(0, 1)])   # second has a bad ticket
        self.assertEqual(len(out.positions), 1)
        self.assertIn("bad_ticket", [r[2] for r in out.rejected])

    def test_multiple_tickets_independent(self):
        out = fold_revisions([_rev(100, 1), _rev(200, 1), _rev(200, 2, closed=True, volume=0, side="")])
        tickets = {p.ticket for p in out.positions}
        self.assertEqual(tickets, {100})                   # 200 closed; 100 open
        self.assertEqual(out.applied_revision, {100: 1, 200: 2})
