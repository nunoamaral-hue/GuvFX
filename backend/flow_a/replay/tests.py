"""
Flow A replay — routing tests.

Proves the SignalResult → Flow A candidate → suppression chain deterministically,
without requiring SCE to fire (which depends on market geometry). Uses a
duck-typed SignalResult so it runs under the isolated flow_a shadow shim with no
dependency on the strategies engine stack.

    cd backend
    DJANGO_SETTINGS_MODULE=flow_a._shadow_settings python manage.py test flow_a.replay
"""

from __future__ import annotations

import types

from django.test import SimpleTestCase

from flow_a.replay.adapter import signal_result_to_candidate
from flow_a.suppression import emit_shadow_candidate
from flow_a.types import OpenTradeCandidate


def _duck_signal(**kw):
    base = dict(ok=True, signal_type="BUY", symbol="EURUSD",
                entry_price=1.1000, sl_price=1.0960, tp_price=1.1080,
                lots=0.02, reason="sce_signal", details={})
    base.update(kw)
    return types.SimpleNamespace(**base)


class AdapterTests(SimpleTestCase):
    def test_buy_signalresult_maps_to_candidate(self):
        cand = signal_result_to_candidate(
            _duck_signal(), symbol="EURUSD", timeframe="H1", risk_per_trade_pct="1.0")
        self.assertIsInstance(cand, OpenTradeCandidate)
        self.assertEqual(cand.direction, "BUY")
        self.assertEqual(cand.symbol, "EURUSD")
        self.assertEqual(cand.entry_price, "1.1")
        self.assertEqual(cand.sl_price, "1.096")
        self.assertEqual(cand.tp_price, "1.108")
        self.assertIn("sce-replay:EURUSD", cand.comment)

    def test_sell_maps(self):
        cand = signal_result_to_candidate(
            _duck_signal(signal_type="SELL", sl_price=1.1040, tp_price=1.0920),
            symbol="EURUSD", timeframe="H1", risk_per_trade_pct="1.0")
        self.assertEqual(cand.direction, "SELL")

    def test_no_signal_rejected_by_adapter(self):
        with self.assertRaises(ValueError):
            signal_result_to_candidate(
                _duck_signal(signal_type=None), symbol="EURUSD",
                timeframe="H1", risk_per_trade_pct="1.0")


class RoutingSuppressionTests(SimpleTestCase):
    def test_signal_routes_to_suppression_no_job(self):
        cand = signal_result_to_candidate(
            _duck_signal(), symbol="EURUSD", timeframe="H1", risk_per_trade_pct="1.0")
        record = emit_shadow_candidate(cand, run_id="replay-routing-test")
        self.assertTrue(record["execution_suppressed"])
        self.assertFalse(record["execution_job_created"])
        self.assertEqual(record["candidate"]["direction"], "BUY")
        self.assertEqual(record["event"], "FLOW_A_SHADOW_CANDIDATE_SUPPRESSED")
