"""Pure diff-engine certification (no DB, no I/O) — the deterministic core of the observer."""
from django.test import SimpleTestCase

from trade_copier import diff
from trade_copier.diff import ObservedPosition as P, diff_positions


def _types(events):
    return [(e.event_type, e.ticket) for e in events]


class DiffEngineTests(SimpleTestCase):
    def test_open_detected(self):
        ev = diff_positions([], [P(1, "XAUUSD", "BUY", 0.10)])
        self.assertEqual(_types(ev), [(diff.OPEN, 1)])
        self.assertEqual(ev[0].volume_delta, 0.10)

    def test_full_close_detected(self):
        ev = diff_positions([P(1, "XAUUSD", "BUY", 0.10)], [])
        self.assertEqual(_types(ev), [(diff.FULL_CLOSE, 1)])
        self.assertEqual(ev[0].volume_delta, -0.10)

    def test_partial_close_detected(self):
        ev = diff_positions([P(1, "XAUUSD", "BUY", 0.10)], [P(1, "XAUUSD", "BUY", 0.04)])
        self.assertEqual(_types(ev), [(diff.PARTIAL_CLOSE, 1)])
        self.assertAlmostEqual(ev[0].volume_delta, -0.06)

    def test_volume_increase_detected(self):
        ev = diff_positions([P(1, "XAUUSD", "BUY", 0.10)], [P(1, "XAUUSD", "BUY", 0.25)])
        self.assertEqual(_types(ev), [(diff.VOLUME_INCREASE, 1)])
        self.assertAlmostEqual(ev[0].volume_delta, 0.15)

    def test_sl_set_change_remove(self):
        self.assertEqual(_types(diff_positions([P(1, "X", "BUY", 0.1, sl=0.0)],
                                               [P(1, "X", "BUY", 0.1, sl=1990.0)])), [(diff.SL_SET, 1)])
        self.assertEqual(_types(diff_positions([P(1, "X", "BUY", 0.1, sl=1990.0)],
                                               [P(1, "X", "BUY", 0.1, sl=1995.0)])), [(diff.SL_CHANGE, 1)])
        self.assertEqual(_types(diff_positions([P(1, "X", "BUY", 0.1, sl=1990.0)],
                                               [P(1, "X", "BUY", 0.1, sl=0.0)])), [(diff.SL_REMOVE, 1)])

    def test_tp_set_change_remove(self):
        self.assertEqual(_types(diff_positions([P(1, "X", "BUY", 0.1, tp=None)],
                                               [P(1, "X", "BUY", 0.1, tp=2100.0)])), [(diff.TP_SET, 1)])
        self.assertEqual(_types(diff_positions([P(1, "X", "BUY", 0.1, tp=2100.0)],
                                               [P(1, "X", "BUY", 0.1, tp=2200.0)])), [(diff.TP_CHANGE, 1)])
        self.assertEqual(_types(diff_positions([P(1, "X", "BUY", 0.1, tp=2100.0)],
                                               [P(1, "X", "BUY", 0.1, tp=None)])), [(diff.TP_REMOVE, 1)])

    def test_sl_and_tp_change_same_ticket_both_emitted(self):
        ev = diff_positions([P(1, "X", "BUY", 0.1, sl=1990.0, tp=0.0)],
                            [P(1, "X", "BUY", 0.1, sl=1995.0, tp=2100.0)])
        self.assertEqual(_types(ev), [(diff.SL_CHANGE, 1), (diff.TP_SET, 1)])

    def test_no_change_is_noop(self):
        self.assertEqual(diff_positions([P(1, "X", "BUY", 0.1, sl=1990.0, tp=2100.0)],
                                        [P(1, "X", "BUY", 0.1, sl=1990.0, tp=2100.0)]), [])

    def test_float_noise_suppressed(self):
        self.assertEqual(diff_positions([P(1, "X", "BUY", 0.1)],
                                        [P(1, "X", "BUY", 0.10000000001)]), [])

    def test_sub_step_volume_change_suppressed(self):
        # A change smaller than half a lot step is float noise, not a partial close.
        self.assertEqual(diff_positions([P(1, "X", "BUY", 0.10)],
                                        [P(1, "X", "BUY", 0.1004)]), [])

    def test_multiple_tickets_deterministic_order(self):
        prev = [P(2, "A", "BUY", 0.1), P(1, "B", "SELL", 0.2, sl=1.0)]
        new = [P(1, "B", "SELL", 0.2, sl=1.1), P(3, "C", "BUY", 0.3)]
        ev = diff_positions(prev, new)
        # ticket 1 SL_CHANGE, ticket 2 FULL_CLOSE, ticket 3 OPEN — ordered by ticket.
        self.assertEqual(_types(ev), [(diff.SL_CHANGE, 1), (diff.FULL_CLOSE, 2), (diff.OPEN, 3)])

    def test_from_payload_maps_bridge_dict(self):
        p = diff.ObservedPosition.from_payload(
            {"ticket": 5, "symbol": "EURUSD", "type": 1, "volume": 0.5,
             "sl": 1.05, "tp": 0.0, "price_open": 1.10, "magic": 42, "comment": "x"})
        self.assertEqual((p.ticket, p.symbol, p.side, p.volume, p.sl, p.tp, p.magic),
                         (5, "EURUSD", "SELL", 0.5, 1.05, 0.0, 42))
