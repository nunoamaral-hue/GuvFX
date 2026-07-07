"""GFX-PKT-BROKER-SYMBOL-DEPLOY-AND-SYNC — crypto pip-size fix for notification pips.

Proves BTCUSD (and crypto generally) no longer uses the FX 0.0001 default, so a BTCUSD winner's
rendered pip count matches Wayond's convention (a 63200->63450 move is +250 pips, not +2,500,000),
while FX / JPY / gold / indices are unchanged.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from intelligence.trade_result_producer import _pip_size


class PipSizeTests(SimpleTestCase):
    def test_crypto_btcusd_is_one_unit(self):
        self.assertEqual(_pip_size("BTCUSD"), Decimal("1"))

    def test_crypto_variants(self):
        for s in ("BTCUSD", "ETHUSD", "BTCUSD.", "LTCUSD", "SOLUSD"):
            self.assertEqual(_pip_size(s), Decimal("1"), s)

    def test_btcusd_move_renders_sane_pips(self):
        # The packet's acceptance example: 63200 -> 63450 must be ~+250 pips, not +2,500,000.
        pips = (Decimal("63450") - Decimal("63200")) / _pip_size("BTCUSD")
        self.assertEqual(pips, Decimal("250"))

    def test_fx_unchanged(self):
        for s in ("EURUSD", "GBPUSD", "EURGBP", "AUDUSD", "AUDCAD", "EURAUD", "USDCHF"):
            self.assertEqual(_pip_size(s), Decimal("0.0001"), s)

    def test_jpy_unchanged(self):
        self.assertEqual(_pip_size("NZDJPY"), Decimal("0.01"))

    def test_metals_unchanged(self):
        self.assertEqual(_pip_size("XAUUSD"), Decimal("0.1"))
        self.assertEqual(_pip_size("XAGUSD"), Decimal("0.01"))

    def test_indices_unchanged(self):
        self.assertEqual(_pip_size("US30"), Decimal("1"))
