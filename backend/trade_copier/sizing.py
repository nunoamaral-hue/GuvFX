"""trade_copier.sizing — PURE follower position-sizing (no Django, no I/O).

Computes the follower's target volume from the master's volume + the follower's configuration, then
enforces the destination broker's lot constraints and the follower's own caps. Deterministic and
unit-tested (synthetic certification).

Modes (packet §7):
  * FIXED_LOT            — a fixed lot per copied trade.
  * MULTIPLIER           — master volume × multiplier.
  * BALANCE_PROPORTIONAL — master volume × (follower_balance / master_balance).
  * RISK_BASED           — risk budget / (SL distance × pip value per lot).

Safety rules (packet §7): respect broker min/step/max; respect the follower's ``max_volume``; round
DOWN to the lot step; NEVER silently round up beyond a cap. A volume that cannot be represented
within the broker minimum, or that is missing a required input, is REJECTED (no silent fallback).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Optional

# Sizing-mode string constants (mirror models.SizingMode).
FIXED_LOT = "FIXED_LOT"
MULTIPLIER = "MULTIPLIER"
BALANCE_PROPORTIONAL = "BALANCE_PROPORTIONAL"
RISK_BASED = "RISK_BASED"


@dataclass(frozen=True)
class BrokerConstraints:
    """Destination broker's lot rules for the resolved follower symbol (from BrokerInstrument
    metadata when available; conservative defaults otherwise).

    ``authoritative`` is True only when the bounds came from the broker's own synced metadata. When
    False the bounds are non-authoritative defaults: tolerable for a SHADOW intent calculation, but a
    real DEMO job must NOT be created on guessed bounds (ADR-0051 BLOCKER-4) — B-exec rejects them."""
    min_volume: float = 0.01
    lot_step: float = 0.01
    max_volume: Optional[float] = None
    authoritative: bool = False


@dataclass(frozen=True)
class FollowerSizing:
    """The follower's sizing configuration (lifted from a CopierFollower by the caller)."""
    mode: str
    fixed_lot: Optional[float] = None
    multiplier: Optional[float] = 1.0
    risk_per_trade_pct: Optional[float] = None
    max_volume: Optional[float] = None


@dataclass(frozen=True)
class SizingResult:
    ok: bool
    volume: Optional[float]
    detail: str  # a short machine-readable note on success, or the rejection reason


def _d(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _floor_to_step(volume: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return volume
    return (volume / step).to_integral_value(rounding=ROUND_DOWN) * step


def compute_follower_volume(
    master_volume,
    cfg: FollowerSizing,
    constraints: BrokerConstraints,
    *,
    master_balance=None,
    follower_balance=None,
    sl_distance=None,
    pip_value_per_lot=None,
) -> SizingResult:
    """Return the follower target volume (lot-step-floored, cap-clamped) or a fail-closed rejection."""
    mv = _d(master_volume)
    if mv is None or mv <= 0:
        return SizingResult(False, None, "invalid_master_volume")

    # 1) Raw target by mode.
    if cfg.mode == FIXED_LOT:
        raw = _d(cfg.fixed_lot)
        if raw is None or raw <= 0:
            return SizingResult(False, None, "fixed_lot_not_set")
    elif cfg.mode == MULTIPLIER:
        mult = _d(cfg.multiplier)
        if mult is None or mult <= 0:
            return SizingResult(False, None, "multiplier_not_set")
        raw = mv * mult
    elif cfg.mode == BALANCE_PROPORTIONAL:
        mb, fb = _d(master_balance), _d(follower_balance)
        if mb is None or fb is None or mb <= 0:
            return SizingResult(False, None, "requires_balances")
        raw = mv * (fb / mb)
    elif cfg.mode == RISK_BASED:
        risk_pct, fb = _d(cfg.risk_per_trade_pct), _d(follower_balance)
        sld, pvl = _d(sl_distance), _d(pip_value_per_lot)
        if risk_pct is None or risk_pct <= 0 or fb is None or fb <= 0:
            return SizingResult(False, None, "requires_risk_and_balance")
        if sld is None or sld <= 0 or pvl is None or pvl <= 0:
            # No valid stop-loss ⇒ do NOT silently apply risk-based sizing (packet §7).
            return SizingResult(False, None, "requires_valid_sl")
        risk_amount = fb * (risk_pct / Decimal(100))
        raw = risk_amount / (sld * pvl)
    else:
        return SizingResult(False, None, "unknown_sizing_mode")

    step = _d(constraints.lot_step) or Decimal("0.01")
    minv = _d(constraints.min_volume) or Decimal("0.01")

    # 2) Apply the effective max cap (broker max AND follower max_volume), clamping DOWN only.
    caps = [c for c in (_d(constraints.max_volume), _d(cfg.max_volume)) if c is not None and c > 0]
    clamped = False
    if caps:
        cap = min(caps)
        if raw > cap:
            raw = cap
            clamped = True

    # 3) Floor to the lot step (never round UP past a cap or a risk budget).
    vol = _floor_to_step(raw, step)

    # 4) Enforce the broker minimum — reject rather than round up (rounding up could breach risk).
    if vol < minv:
        return SizingResult(False, None, "below_min_volume")

    detail = "clamped_to_max" if clamped else "ok"
    return SizingResult(True, float(vol), detail)
