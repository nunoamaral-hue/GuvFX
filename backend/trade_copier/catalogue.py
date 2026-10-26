"""trade_copier.catalogue — per-account instrument catalogue reads + per-follower sizing validation.

A thin, READ-ONLY layer over ``execution.BrokerInstrument`` (the account-scoped symbol cache synced from
each account's OWN identity-verified bridge by ``manage.py sync_broker_instruments``). No network, no
order, no write. It provides the catalogue-freshness + tradability signals the mapping matrix needs, and
the per-follower sizing validation (packet §5):

  * ``catalogue_state(account)``       — synced / empty / stale, with counts and newest ``synced_at``.
  * ``is_tradable(metadata)``          — the symbol can open a position (trade_mode not disabled/close-only).
  * ``validate_follower_sizing(...)``  — a configured lot the follower's broker can actually execute,
    validated against the follower's OWN volume_min/step/max (never a hard-coded broker value), with the
    economic exposure (lot × contract_size) surfaced so contract-size differences (XAUUSD 100 vs
    XAUUSD_SB 1) are preserved, not hidden. Fail-closed: when the follower's constraints are not synced the
    result is ``ok=False`` with ``reason='constraints_unavailable'`` (a readiness state, never a silent pass).

Catalogue coverage is NOT execution readiness: this layer only reports what the broker offers and whether a
configured size is executable; it grants no authority and places no order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.utils import timezone

from execution.models import BrokerInstrument

# A catalogue older than this is "stale" and shown as such (configurable). This is a DISCOVERY-layer
# freshness signal; the execution path re-reads fresh broker constraints independently at order time.
_STALE_SECONDS = int(os.getenv("COPIER_CATALOGUE_STALE_SECONDS", str(7 * 24 * 3600)) or 0)

# MT5 SYMBOL_TRADE_MODE: 0=DISABLED, 1=LONGONLY, 2=SHORTONLY, 3=CLOSEONLY, 4=FULL.
_NON_OPENABLE_TRADE_MODES = frozenset({0, 3, "0", "3", "SYMBOL_TRADE_MODE_DISABLED", "SYMBOL_TRADE_MODE_CLOSEONLY"})


def _dec(v):
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def catalogue_state(account) -> dict:
    """Freshness + size of ``account``'s synced catalogue. ``empty`` when never synced (fail-closed)."""
    qs = BrokerInstrument.objects.filter(account=account)
    total = qs.count()
    if total == 0:
        return {"state": "empty", "symbol_count": 0, "enabled_count": 0, "synced_at": None, "age_seconds": None}
    enabled = qs.filter(enabled=True).count()
    newest = qs.order_by("-synced_at").values_list("synced_at", flat=True).first()
    age = (timezone.now() - newest).total_seconds() if newest else None
    state = "stale" if (age is not None and _STALE_SECONDS and age > _STALE_SECONDS) else "synced"
    return {
        "state": state, "symbol_count": total, "enabled_count": enabled,
        "synced_at": newest.isoformat() if newest else None,
        "age_seconds": int(age) if age is not None else None,
    }


def is_catalogue_stale(account) -> bool:
    return catalogue_state(account)["state"] == "stale"


def is_tradable(metadata: Optional[dict]) -> bool:
    """A symbol can OPEN a position: trade_mode is not disabled/close-only and it is not hidden."""
    meta = metadata or {}
    if meta.get("visible") is False:
        return False
    mode = meta.get("trade_mode", meta.get("trade_calc_mode"))
    return mode not in _NON_OPENABLE_TRADE_MODES


def is_expired(metadata: Optional[dict]) -> bool:
    """True when the instrument carries a past expiry date (dated futures). Spot symbols never expire."""
    meta = metadata or {}
    raw = meta.get("expiry") or meta.get("expiration_time")
    if not raw:
        return False
    try:
        from datetime import date, datetime
        if isinstance(raw, (int, float)):  # epoch seconds
            return datetime.utcfromtimestamp(raw).date() < date.today()
        return datetime.fromisoformat(str(raw)[:10]).date() < date.today()
    except (ValueError, TypeError, OverflowError, OSError):
        return False  # unparseable expiry is not treated as expired here (surfaced elsewhere as ambiguity)


@dataclass
class SizingValidation:
    ok: bool
    reason: str                       # 'valid' | 'constraints_unavailable' | ';'-joined failure codes
    failures: list = field(default_factory=list)
    volume_min: Optional[Decimal] = None
    volume_step: Optional[Decimal] = None
    volume_max: Optional[Decimal] = None
    contract_size: Optional[Decimal] = None
    fixed_lot_exposure: Optional[Decimal] = None   # lot × contract_size (economic exposure, preserved)


def validate_follower_sizing(account, follower_symbol: str, *, fixed_lot=None, max_volume=None) -> SizingValidation:
    """Validate a per-follower configured size against THIS follower symbol's own broker constraints.

    Fail-closed and per-follower: reads only ``account``'s own ``BrokerInstrument`` row for
    ``follower_symbol``. Rejects a fixed lot below ``volume_min``, above ``volume_max`` or off the
    ``volume_step`` grid, and a ``max_volume`` ceiling above the broker maximum. When the row is missing
    (constraints not synced) it returns ``ok=False, reason='constraints_unavailable'`` — a readiness
    block, never a silent pass. The step check uses exact ``Decimal`` arithmetic so float-representation
    artefacts (0.1 + 0.2) never produce a false off-step rejection."""
    row = BrokerInstrument.objects.filter(account=account, broker_symbol=follower_symbol).first()
    if row is None:
        return SizingValidation(ok=False, reason="constraints_unavailable")
    spec = row.metadata or {}
    vmin, vstep, vmax = _dec(spec.get("volume_min")), _dec(spec.get("volume_step")), _dec(spec.get("volume_max"))
    csize = _dec(spec.get("contract_size"))
    fl, mv = _dec(fixed_lot), _dec(max_volume)
    failures: list = []
    if fl is not None:
        if vmin is not None and fl < vmin:
            failures.append(f"fixed_lot_below_min:{fl}<{vmin}")
        if vmax is not None and fl > vmax:
            failures.append(f"fixed_lot_above_max:{fl}>{vmax}")
        if vstep is not None and vstep > 0 and vmin is not None:
            steps = (fl - vmin) / vstep
            if steps != steps.to_integral_value():
                failures.append(f"fixed_lot_off_step:{fl}%{vstep}(min {vmin})")
    if mv is not None and vmax is not None and mv > vmax:
        failures.append(f"max_volume_above_broker_max:{mv}>{vmax}")
    exposure = (fl * csize) if (fl is not None and csize is not None) else None
    return SizingValidation(
        ok=not failures, reason=(";".join(failures) if failures else "valid"), failures=failures,
        volume_min=vmin, volume_step=vstep, volume_max=vmax, contract_size=csize, fixed_lot_exposure=exposure,
    )
