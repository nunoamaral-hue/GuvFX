"""Phase A §10 — strategy-ownership scoping primitive for close/modify/protection.

A protection / close / modify / reconciliation worker acting on behalf of one
StrategyAssignment must never touch a position owned by another — even on the same
TradingAccount, symbol, direction, source or customer. This is the guarded check.

DARK by default: it fails OPEN (returns None = allow) unless
STRATEGY_OWNERSHIP_ENFORCE_ENABLED is set (A6, a separate money-path gate). It also
fails open when the target Trade carries no durable ``strategy_assignment`` yet
(legacy/unbackfilled), so legacy positions remain manageable via today's
comment/plan correlation during migration. It only ever BLOCKS when enforcement is
on AND both sides carry a durable, MISMATCHED owner.

This module is the tested primitive; wiring it into the individual protection
workers (breakeven, close_monitor, tp_protection, provider_commands, reconcile) is
done in the A5/A6 read-switch/enforce packet, where enforcement is actually armed.
"""
from __future__ import annotations

import logging

from execution import ownership_flags

logger = logging.getLogger(__name__)

CROSS_STRATEGY = "cross_strategy_ownership"


def owner_block_reason(trade, acting_assignment) -> str | None:
    """Return a block reason if ``acting_assignment`` must NOT act on ``trade``, else None.

    Fail-open (None) when: enforcement is OFF; the trade has no durable owner;
    or the acting assignment is unknown. Blocks only on a proven owner mismatch.
    """
    if not ownership_flags.ownership_enforce_enabled():
        return None  # DARK — no enforcement until A6
    owner_id = getattr(trade, "strategy_assignment_id", None)
    if owner_id is None:
        return None  # legacy/unattributed — manageable via comment/plan (fail-open)
    acting_id = getattr(acting_assignment, "id", None)
    if acting_id is None:
        return None  # unknown actor — do not block (fail-open)
    if owner_id != acting_id:
        logger.warning(
            "cross_strategy_ownership_blocked trade=%s trade_owner=%s acting=%s — refused.",
            getattr(trade, "id", None), owner_id, acting_id)
        return CROSS_STRATEGY
    return None


def assert_owner(trade, acting_assignment) -> None:
    """Raise ``OwnershipViolation`` when ``owner_block_reason`` blocks (for call sites
    that prefer an exception to a reason string)."""
    reason = owner_block_reason(trade, acting_assignment)
    if reason:
        raise OwnershipViolation(reason)


class OwnershipViolation(Exception):
    """Raised when a strategy would act on another strategy's position (A6-enforced)."""
