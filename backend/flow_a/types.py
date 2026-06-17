"""
Flow A shared types — immutable, transient artifacts only.

Every artifact produced by Flow A is a frozen dataclass (ADR-009: the producer
side persists no models). Nothing here is a Django model; nothing here is, or
becomes, an ``execution.ExecutionJob``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class GateOutcome(str, Enum):
    """Trade Quality Gate v0.1 outcomes (Draft / Shadow / not live-approved)."""

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    ESCALATE = "ESCALATE"


class FlowAEscalation(Exception):
    """Raised when Flow A hits a condition that must be escalated, not decided.

    Used to enforce ADR-012: Flow A never invents trading availability. If an
    availability check is requested without an authoritative SSOT result, the
    pipeline escalates instead of fabricating ``can_trade``.
    """


@dataclass(frozen=True)
class EvaluationResult:
    """Outcome of the Strategy Evaluation layer (immutable)."""

    matched: bool
    reasons: tuple[str, ...]
    # Proposed trade parameters distilled from the signal + strategy (or None).
    proposed: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GateDecision:
    """Trade Quality Gate v0.1 decision (immutable, shadow-only)."""

    outcome: GateOutcome
    reasons: tuple[str, ...]
    thresholds: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        return d


@dataclass(frozen=True)
class OpenTradeCandidate:
    """An OPEN_TRADE *candidate* — a description, never an execution instruction.

    Field names deliberately mirror ``execution.services.create_open_trade_job``'s
    payload so a future (separately authorised) live path can adopt this shape
    without reinterpretation. This object is a frozen dataclass: it is **not** an
    ``ExecutionJob`` and cannot be polled by the MT5 worker.
    """

    symbol: str
    direction: str
    timeframe: str
    entry_type: str
    entry_price: str | None
    sl_price: str
    tp_price: str | None
    risk_per_trade_pct: str
    comment: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ShadowRunResult:
    """Full result of one Flow A shadow run (immutable)."""

    run_id: str
    intelligence_id: str
    evaluation: EvaluationResult
    gate: GateDecision
    candidate: OpenTradeCandidate | None
    # Hard, explicit suppression facts — always True in shadow mode.
    execution_suppressed: bool
    execution_job_created: bool

    def to_dict(self) -> dict:
        d = {
            "run_id": self.run_id,
            "intelligence_id": self.intelligence_id,
            "evaluation": self.evaluation.to_dict(),
            "gate": self.gate.to_dict(),
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "execution_suppressed": self.execution_suppressed,
            "execution_job_created": self.execution_job_created,
        }
        return d
