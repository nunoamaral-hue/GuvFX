"""Phase-5 change-classification tests (ADR-0074). PURE — no DB.

Asserts the named taxonomy AND the no-noise guarantee: floating price, floating P&L and formatting-only decimal
differences never produce a change (they are never diffed)."""
from django.test import SimpleTestCase

from trade_copier import change_types as ct
from trade_copier.diff import ObservedPosition


def _p(ticket=1, symbol="XAUUSD", side="BUY", volume=0.10, sl=None, tp=None, price_current=None):
    return ObservedPosition(ticket=ticket, symbol=symbol, side=side, volume=volume, sl=sl, tp=tp,
                            price_current=price_current)


class ChangeClassificationTests(SimpleTestCase):
    def _names(self, prev, new):
        return [c.change_type for c in ct.classify_position_changes(prev, new)]

    def test_entry_opened(self):
        self.assertEqual(self._names([], [_p()]), [ct.ENTRY_OPENED])

    def test_position_closed(self):
        self.assertEqual(self._names([_p()], []), [ct.POSITION_CLOSED])

    def test_sl_added_changed_removed(self):
        self.assertEqual(self._names([_p(sl=None)], [_p(sl=1990)]), [ct.STOP_LOSS_ADDED])
        self.assertEqual(self._names([_p(sl=1990)], [_p(sl=1995)]), [ct.STOP_LOSS_CHANGED])
        self.assertEqual(self._names([_p(sl=1990)], [_p(sl=None)]), [ct.STOP_LOSS_REMOVED])

    def test_tp_added_changed_removed(self):
        self.assertEqual(self._names([_p(tp=None)], [_p(tp=2100)]), [ct.TAKE_PROFIT_ADDED])
        self.assertEqual(self._names([_p(tp=2100)], [_p(tp=2110)]), [ct.TAKE_PROFIT_CHANGED])
        self.assertEqual(self._names([_p(tp=2100)], [_p(tp=None)]), [ct.TAKE_PROFIT_REMOVED])

    def test_zero_is_no_level(self):
        # MT5 encodes "no level" as 0.0 — adding 0.0 is not a change; setting a real level from 0.0 IS an add.
        self.assertEqual(self._names([_p(sl=0.0)], [_p(sl=0.0)]), [])
        self.assertEqual(self._names([_p(sl=0.0)], [_p(sl=1990)]), [ct.STOP_LOSS_ADDED])

    def test_sl_and_tp_together_collapses(self):
        names = self._names([_p(sl=1990, tp=2100)], [_p(sl=1995, tp=2110)])
        self.assertEqual(names, [ct.SL_TP_CHANGED_TOGETHER])

    def test_partial_close_and_volume_increase(self):
        self.assertEqual(self._names([_p(volume=0.10)], [_p(volume=0.04)]), [ct.PARTIAL_CLOSE])
        self.assertEqual(self._names([_p(volume=0.10)], [_p(volume=0.20)]), [ct.VOLUME_INCREASED])

    def test_floating_price_and_pnl_never_emit(self):
        # Only price_current moves (floating P&L); structural fields unchanged → NO change.
        self.assertEqual(self._names([_p(price_current=1999.0)], [_p(price_current=2050.5)]), [])

    def test_formatting_only_decimal_noise_never_emits(self):
        # A sub-epsilon price round-trip is not a modification.
        self.assertEqual(self._names([_p(sl=1990.0)], [_p(sl=1990.0 + 1e-12)]), [])

    def test_change_type_for_maps_all_diff_types(self):
        from trade_copier import diff
        for et in (diff.OPEN, diff.FULL_CLOSE, diff.PARTIAL_CLOSE, diff.VOLUME_INCREASE,
                   diff.SL_SET, diff.SL_CHANGE, diff.SL_REMOVE, diff.TP_SET, diff.TP_CHANGE, diff.TP_REMOVE):
            self.assertTrue(ct.change_type_for(et), f"no mapping for {et}")
        self.assertEqual(ct.change_type_for("NONSENSE"), "")
