"""Pure SL/TP translation certification (no DB, no I/O)."""
from django.test import SimpleTestCase

from trade_copier.translation import NONE, SAME_DISTANCE, SAME_PRICE, translate_sltp


class TranslationTests(SimpleTestCase):
    def test_same_price_copies_absolute(self):
        r = translate_sltp(SAME_PRICE, "BUY", master_entry=2000.0, master_sl=1990.0, master_tp=2020.0)
        self.assertTrue(r.ok)
        self.assertEqual((r.sl, r.tp), (1990.0, 2020.0))

    def test_same_price_zero_level_is_none(self):
        r = translate_sltp(SAME_PRICE, "BUY", master_entry=2000.0, master_sl=0.0, master_tp=None)
        self.assertTrue(r.ok)
        self.assertEqual((r.sl, r.tp), (None, None))

    def test_none_policy(self):
        r = translate_sltp(NONE, "BUY", master_entry=2000.0, master_sl=1990.0, master_tp=2020.0)
        self.assertTrue(r.ok)
        self.assertEqual((r.sl, r.tp), (None, None))

    def test_same_distance_preserves_distance(self):
        # master entry 2000, sl 1990 (-10), tp 2020 (+20); follower entry 2001 -> sl 1991, tp 2021.
        r = translate_sltp(SAME_DISTANCE, "BUY", master_entry=2000.0, master_sl=1990.0,
                           master_tp=2020.0, follower_entry=2001.0)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.sl, 1991.0)
        self.assertAlmostEqual(r.tp, 2021.0)

    def test_same_distance_requires_entries(self):
        r = translate_sltp(SAME_DISTANCE, "BUY", master_entry=None, master_sl=1990.0, master_tp=2020.0)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "requires_entry_prices")
