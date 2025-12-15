from django.test import TestCase

from .serializers import StrategySerializer


class StrategySerializerTests(TestCase):
    def setUp(self):
        self.base_payload = {
            "name": "Test Strategy",
            "description": "Brief description",
            "style": "SWING",
            "symbol_universe": "EURUSD",
            "timeframe": "H4",
            "edge_type": "TREND_FOLLOWING",
            "risk_per_trade_pct": 1.0,
            "sl_rules": {
                "method": "ATR_MULTIPLE",
                "atr_period": 14,
                "atr_multiple": 1.5,
            },
            "tp_rules": {
                "primary": "FIXED_RR",
                "rr_target": 2.0,
                "use_trailing": True,
                "trailing": {
                    "method": "ATR_TRAIL",
                    "atr_period": 14,
                    "atr_multiple": 1.5,
                },
            },
            "trade_management": {
                "move_to_breakeven": {"enabled": True, "at_r_multiple": 1.0},
                "pyramiding": {"enabled": False, "max_additions": 0},
            },
            "filters": {
                "news_filter": {
                    "mode": "AVOID_NEWS",
                    "pre_event_minutes": 30,
                    "post_event_minutes": 30,
                    "impact_levels": ["HIGH"],
                    "event_types": ["CPI"],
                },
                "time_filters": {"avoid_friday_close": True, "avoid_rollover": True},
                "max_trades_per_day": 5,
            },
            "risk_limits": {
                "daily_max_loss_r": 3.0,
                "weekly_max_loss_r": 8.0,
                "max_open_risk_pct": 5.0,
            },
            "plan_meta": {
                "pre_session_checklist": ["Check economic calendar"],
                "post_session_checklist": ["Review trades"],
                "psychology_rules": {
                    "after_big_win_r": 3.0,
                    "cooldown_minutes_after_big_win": 30,
                    "max_consecutive_losses_before_reduce_size": 2,
                    "reduced_risk_per_trade_pct": 0.5,
                },
            },
            "auto_optimize_by_ai": False,
        }

    def test_valid_payload_passes_validation(self):
        serializer = StrategySerializer(data=self.base_payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_invalid_risk_per_trade_is_rejected(self):
        invalid_payload = self.base_payload.copy()
        invalid_payload["risk_per_trade_pct"] = 50
        serializer = StrategySerializer(data=invalid_payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("risk_per_trade_pct", serializer.errors)
