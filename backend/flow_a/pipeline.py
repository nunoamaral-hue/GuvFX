"""
Flow A — shadow pipeline orchestration.

    Wayond Signal
      -> Strategy Evaluation
      -> Trade Quality Gate v0.1 (Shadow)
      -> OPEN_TRADE Candidate            (only when gate ACCEPTs)
      -> Execution Suppressed            (logged/audited; never dispatched)

Returns an immutable :class:`ShadowRunResult`. ``execution_suppressed`` is always
True and ``execution_job_created`` is always False — Flow A has no code path that
creates a pollable execution job.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from . import candidate as candidate_mod
from . import evaluation as evaluation_mod
from . import quality_gate, signal_intake, suppression
from .types import GateOutcome, ShadowRunResult


def run_shadow(signal: Mapping, strategy: Mapping, *, availability=None) -> ShadowRunResult:
    """Run one Wayond signal through the full Flow A shadow pipeline.

    ``availability`` defaults to None (not consulted — ADR-012 / shadow). Passing
    a non-SSOT value triggers ``FlowAEscalation`` from the gate.
    """
    run_id = uuid.uuid4().hex

    # 1 — Wayond signal intake (reuses intelligence producer).
    envelope = signal_intake.intake_wayond_signal(signal)

    # 2 — Strategy evaluation.
    evaluation = evaluation_mod.evaluate(envelope, strategy)

    # 3 — Trade Quality Gate v0.1 (may raise FlowAEscalation for ADR-012).
    gate = quality_gate.assess(evaluation, availability=availability)

    # 4 — OPEN_TRADE candidate (only on ACCEPT).
    built = None
    if gate.outcome is GateOutcome.ACCEPT:
        built = candidate_mod.build_candidate(evaluation)
        # 5 — Suppression: log/audit only, never dispatch.
        suppression.emit_shadow_candidate(built, run_id=run_id)

    return ShadowRunResult(
        run_id=run_id,
        intelligence_id=envelope.intelligence_id,
        evaluation=evaluation,
        gate=gate,
        candidate=built,
        execution_suppressed=True,
        execution_job_created=False,
    )
