"""Pure follower-sizing certification (no DB, no I/O)."""
from django.test import SimpleTestCase

from trade_copier.sizing import (
    BALANCE_PROPORTIONAL,
    FIXED_LOT,
    MULTIPLIER,
    RISK_BASED,
    BrokerConstraints,
    FollowerSizing,
    compute_follower_volume,
)

C = BrokerConstraints(min_volume=0.01, lot_step=0.01, max_volume=None)


class SizingTests(SimpleTestCase):
    def test_fixed_lot(self):
        r = compute_follower_volume(0.10, FollowerSizing(mode=FIXED_LOT, fixed_lot=0.01), C)
        self.assertTrue(r.ok)
        self.assertEqual(r.volume, 0.01)

    def test_fixed_lot_unset_rejected(self):
        r = compute_follower_volume(0.10, FollowerSizing(mode=FIXED_LOT, fixed_lot=None), C)
        self.assertFalse(r.ok)
        self.assertEqual(r.detail, "fixed_lot_not_set")

    def test_multiplier(self):
        r = compute_follower_volume(0.10, FollowerSizing(mode=MULTIPLIER, multiplier=0.5), C)
        self.assertTrue(r.ok)
        self.assertEqual(r.volume, 0.05)

    def test_multiplier_floored_to_step(self):
        # 0.10 * 0.33 = 0.033 -> floored DOWN to 0.03 (never rounds up).
        r = compute_follower_volume(0.10, FollowerSizing(mode=MULTIPLIER, multiplier=0.33), C)
        self.assertTrue(r.ok)
        self.assertEqual(r.volume, 0.03)

    def test_below_min_rejected_not_rounded_up(self):
        # 0.10 * 0.05 = 0.005 -> below the 0.01 minimum -> REJECT (never silently round up).
        r = compute_follower_volume(0.10, FollowerSizing(mode=MULTIPLIER, multiplier=0.05), C)
        self.assertFalse(r.ok)
        self.assertEqual(r.detail, "below_min_volume")

    def test_follower_max_volume_clamps_down(self):
        r = compute_follower_volume(1.00, FollowerSizing(mode=MULTIPLIER, multiplier=5.0, max_volume=1.0), C)
        self.assertTrue(r.ok)
        self.assertEqual(r.volume, 1.0)
        self.assertEqual(r.detail, "clamped_to_max")

    def test_broker_max_volume_clamps_down(self):
        c2 = BrokerConstraints(min_volume=0.01, lot_step=0.01, max_volume=2.0)
        r = compute_follower_volume(1.00, FollowerSizing(mode=MULTIPLIER, multiplier=5.0), c2)
        self.assertTrue(r.ok)
        self.assertEqual(r.volume, 2.0)

    def test_balance_proportional(self):
        r = compute_follower_volume(0.10, FollowerSizing(mode=BALANCE_PROPORTIONAL), C,
                                    master_balance=10000, follower_balance=5000)
        self.assertTrue(r.ok)
        self.assertEqual(r.volume, 0.05)

    def test_balance_proportional_requires_balances(self):
        r = compute_follower_volume(0.10, FollowerSizing(mode=BALANCE_PROPORTIONAL), C)
        self.assertFalse(r.ok)
        self.assertEqual(r.detail, "requires_balances")

    def test_risk_based_requires_valid_sl(self):
        r = compute_follower_volume(0.10, FollowerSizing(mode=RISK_BASED, risk_per_trade_pct=1.0), C,
                                    follower_balance=10000, sl_distance=None, pip_value_per_lot=10)
        self.assertFalse(r.ok)
        self.assertEqual(r.detail, "requires_valid_sl")

    def test_risk_based_computes_with_inputs(self):
        # risk 1% of 10000 = 100; sl_distance 50 * pip_value 10 = 500 per lot -> 0.2 lots.
        r = compute_follower_volume(0.10, FollowerSizing(mode=RISK_BASED, risk_per_trade_pct=1.0), C,
                                    follower_balance=10000, sl_distance=50, pip_value_per_lot=10)
        self.assertTrue(r.ok)
        self.assertEqual(r.volume, 0.20)

    def test_invalid_master_volume(self):
        r = compute_follower_volume(0, FollowerSizing(mode=MULTIPLIER, multiplier=1.0), C)
        self.assertFalse(r.ok)
        self.assertEqual(r.detail, "invalid_master_volume")
