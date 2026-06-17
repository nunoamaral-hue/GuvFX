"""
Flow A Build Phase 1 — tests.

Run on the isolated SQLite settings shim (no Postgres role; production DB
untouched):

    cd backend
    DJANGO_SETTINGS_MODULE=flow_a._shadow_settings python manage.py test flow_a
"""

from __future__ import annotations

import pathlib

from django.apps import apps
from django.test import SimpleTestCase

from flow_a import pipeline, suppression
from flow_a.quality_gate import MIN_CONFIDENCE
from flow_a.types import FlowAEscalation, GateOutcome, OpenTradeCandidate

FLOW_A_DIR = pathlib.Path(__file__).resolve().parent


def _flow_a_source_files():
    """Flow A production source modules — excludes this test file itself, which
    legitimately mentions the forbidden tokens to assert against them."""
    return [
        py for py in FLOW_A_DIR.rglob("*.py")
        if py.name != "tests.py" and "__pycache__" not in py.parts
    ]

BASE_SIGNAL = {
    "signal_id": "WAYOND-TEST-001",
    "market": "XAUUSD",
    "direction": "BUY",
    "entry": "3350.0",
    "stop_loss": "3335.0",
    "take_profit": "3370.0",
    "timestamp": "2026-06-14T08:00:00Z",
    "confidence": "72",
    "summary": "Test long setup on gold.",
}
STRATEGY = {
    "name": "Test Strategy",
    "is_active": True,
    "symbol_universe": "XAUUSD,EURUSD",
    "timeframe": "H1",
    "risk_per_trade_pct": "1.0",
}


class AcceptPathTests(SimpleTestCase):
    def test_accept_builds_candidate_and_suppresses(self):
        result = pipeline.run_shadow(BASE_SIGNAL, STRATEGY)
        self.assertTrue(result.evaluation.matched)
        self.assertIs(result.gate.outcome, GateOutcome.ACCEPT)
        self.assertIsInstance(result.candidate, OpenTradeCandidate)
        # The candidate mirrors the execution payload shape.
        self.assertEqual(result.candidate.symbol, "XAUUSD")
        self.assertEqual(result.candidate.direction, "BUY")
        self.assertEqual(result.candidate.sl_price, "3335.0")
        # Suppression invariants.
        self.assertTrue(result.execution_suppressed)
        self.assertFalse(result.execution_job_created)


class RejectPathTests(SimpleTestCase):
    def test_reject_on_low_confidence(self):
        signal = {**BASE_SIGNAL, "confidence": str(int(MIN_CONFIDENCE) - 1)}
        result = pipeline.run_shadow(signal, STRATEGY)
        self.assertIs(result.gate.outcome, GateOutcome.REJECT)
        self.assertIsNone(result.candidate)
        self.assertTrue(result.execution_suppressed)
        self.assertFalse(result.execution_job_created)

    def test_default_reject_on_uncertainty_missing_sl(self):
        signal = {**BASE_SIGNAL, "stop_loss": "0"}
        # "0" parses but is on the wrong side for BUY -> reject; also covers the
        # uncertainty/default-reject posture for risk-control inputs.
        result = pipeline.run_shadow(signal, STRATEGY)
        self.assertIs(result.gate.outcome, GateOutcome.REJECT)
        self.assertIsNone(result.candidate)

    def test_reject_when_strategy_does_not_match_market(self):
        strategy = {**STRATEGY, "symbol_universe": "EURUSD,GBPUSD"}
        result = pipeline.run_shadow(BASE_SIGNAL, strategy)
        self.assertFalse(result.evaluation.matched)
        self.assertIs(result.gate.outcome, GateOutcome.REJECT)
        self.assertIsNone(result.candidate)


class EscalatePathTests(SimpleTestCase):
    def test_non_ssot_availability_escalates_adr012(self):
        # ADR-012: any non-None availability that is not an SSOT result must
        # escalate rather than be interpreted.
        with self.assertRaises(FlowAEscalation):
            pipeline.run_shadow(BASE_SIGNAL, STRATEGY, availability={"can_trade": True})


class SuppressionStructuralTests(SimpleTestCase):
    def test_candidate_is_not_a_django_model(self):
        result = pipeline.run_shadow(BASE_SIGNAL, STRATEGY)
        from django.db.models import Model
        self.assertNotIsInstance(result.candidate, Model)

    def test_emit_refuses_model_instances(self):
        from django.contrib.auth.models import Group  # any Model class
        with self.assertRaises(TypeError):
            suppression.emit_shadow_candidate(Group(name="x"), run_id="r")

    def test_flow_a_never_imports_execution(self):
        # Structural guarantee: no Flow A source references the execution app or
        # constructs an ExecutionJob / pollable PENDING job.
        offenders = []
        for py in _flow_a_source_files():
            text = py.read_text()
            if "import execution" in text or "from execution" in text:
                offenders.append(f"{py.name}: imports execution")
            if "ExecutionJob(" in text or "create_open_trade_job(" in text:
                offenders.append(f"{py.name}: constructs an execution job")
        self.assertEqual(offenders, [], f"suppression breach: {offenders}")


class ComplianceTests(SimpleTestCase):
    def test_adr009_flow_a_persists_no_models(self):
        models = [m.__name__ for m in apps.get_app_config("flow_a").get_models()]
        self.assertEqual(models, [], f"flow_a must persist no models, found {models}")

    def test_adr012_no_derived_can_trade(self):
        # Flow A must not recreate/derive/duplicate/reinterpret can_trade. The
        # only permitted reference is the SSOT endpoint path itself (in docs/
        # guard messages). No source may *assign* a can_trade value.
        for py in _flow_a_source_files():
            text = py.read_text()
            self.assertNotIn("can_trade =", text, f"{py.name} appears to derive can_trade")
            self.assertNotIn("can_trade=", text, f"{py.name} appears to derive can_trade")
