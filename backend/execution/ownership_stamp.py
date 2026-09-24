"""Phase A §9 — durable Trade → StrategyAssignment attribution (DARK dual-write).

Stamps ``Trade.strategy_assignment`` from broker-result metadata, DB-authoritative,
using magic (registry) with the WAY comment as corroboration/fallback:

    magic known + comment agrees      → STRONG   (stamp)
    magic known + comment CONFLICTS   → CONFLICT  (do NOT stamp; alert/quarantine)
    magic unknown/0 + comment resolves→ LEGACY    (stamp from comment; migration path)
    neither resolves                  → UNATTRIBUTED (leave NULL; fail-closed for
                                                       strategy-specific actions)

Every candidate owner is account-verified (``owner.account_id == trade.account_id``);
a mismatch is never stamped. Runs only under STRATEGY_OWNERSHIP_DUAL_WRITE_ENABLED
(else no-op), so it is byte-identical to today while DARK. Never guesses.
"""
from __future__ import annotations

import logging
import re

from execution import ownership_flags
from strategies.magic_allocation import ASSIGNMENT_MAGIC_BASE

logger = logging.getLogger(__name__)

_WAY_RE = re.compile(r"^WAY(\d+)L(\d+)$")

# Result codes (returned; also logged for observability §17).
STRONG = "strong"
LEGACY = "legacy"
CONFLICT = "conflict"
UNATTRIBUTED = "unattributed"
MISMATCH = "mismatch"
NOOP = "noop"


def _owner_by_magic(magic, account_id):
    """The StrategyAssignment whose registry magic == ``magic`` (account-verified)."""
    if magic is None or magic < ASSIGNMENT_MAGIC_BASE:
        return None
    from strategies.models import StrategyAssignment
    owner = StrategyAssignment.objects.filter(magic_number=magic).first()
    if owner is None:
        return None
    return owner if owner.account_id == account_id else None


def _owner_by_comment(comment, account_id):
    """The owner resolved via ``WAY{plan}L{leg}`` → plan.strategy_assignment
    (account-verified). Falls back to the plan's (account, source) resolution when
    the plan carries no dual-written assignment."""
    if not comment:
        return None
    m = _WAY_RE.match(comment.strip())
    if not m:
        return None
    from execution.models import SignalExecutionPlan
    from execution.signal_promotion import _resolve_plan_assignment
    plan = SignalExecutionPlan.objects.filter(id=int(m.group(1))).first()
    if plan is None or plan.account_id != account_id:
        return None
    owner = _resolve_plan_assignment(plan)
    if owner is None:
        return None
    return owner if owner.account_id == account_id else None


def resolve_owner_for_trade(trade):
    """Return ``(owner_or_None, result_code)`` per the §9 matrix. Read-only."""
    magic_owner = _owner_by_magic(trade.magic_number, trade.account_id)
    comment_owner = _owner_by_comment(trade.comment, trade.account_id)

    if magic_owner is not None and comment_owner is not None:
        if magic_owner.id == comment_owner.id:
            return magic_owner, STRONG
        return None, CONFLICT  # fail-closed: never guess between conflicting owners
    if magic_owner is not None:
        return magic_owner, STRONG
    if comment_owner is not None:
        return comment_owner, LEGACY
    return None, UNATTRIBUTED


def stamp_trade_ownership(trade, *, save: bool = True) -> str:
    """Resolve + persist ``trade.strategy_assignment``. Returns the result code.

    No-op (returns NOOP) unless STRATEGY_OWNERSHIP_DUAL_WRITE_ENABLED. Idempotent:
    an already-attributed trade is left unchanged (returns STRONG). Never overwrites
    a set owner and never stamps on CONFLICT/UNATTRIBUTED/MISMATCH.
    """
    if not ownership_flags.dual_write_enabled():
        return NOOP
    if trade.strategy_assignment_id is not None:
        return STRONG
    owner, code = resolve_owner_for_trade(trade)
    if code == CONFLICT:
        logger.warning(
            "ownership_conflict trade=%s account=%s magic=%s comment=%s — magic and comment "
            "resolve to different assignments; left UNATTRIBUTED (fail-closed).",
            trade.id, trade.account_id, trade.magic_number, trade.comment)
        return CONFLICT
    if owner is None:
        return code  # UNATTRIBUTED (or MISMATCH, folded into no-stamp)
    trade.strategy_assignment = owner
    if save:
        trade.save(update_fields=["strategy_assignment"])
    logger.info("ownership_stamped trade=%s assignment=%s via=%s", trade.id, owner.id, code)
    return code


def sweep_trade_ownership(limit: int = 200) -> dict:
    """Monitor-chain step: stamp ownership on recently-INGESTED un-owned trades.

    No-op (returns ``{"skipped": "dark"}``) unless STRATEGY_OWNERSHIP_DUAL_WRITE_ENABLED.
    Idempotent + bounded, so it is safe to run every tick. Only considers trades that
    carry an assignment magic or a WAY comment (nothing to guess otherwise), AND whose
    ``created_at`` (ingestion time) is within the rolling forward-safety window
    (``STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS``, default 72h). The window makes ownership a
    property of NEW execution activity: enabling/re-enabling DUAL_WRITE can never
    implicitly walk the whole back-catalogue — full-history attribution is the explicit
    ``backfill_execution_ownership`` command's job. The bound is on ``created_at`` (not
    ``open_time``) so a late-ingested older position is still caught on the tick after it
    lands (async ingestion).
    """
    if not ownership_flags.dual_write_enabled():
        return {"skipped": "dark"}
    from datetime import timedelta
    from django.db.models import Q
    from django.utils import timezone
    from trading.models import Trade
    cutoff = timezone.now() - timedelta(hours=ownership_flags.ownership_sweep_window_hours())
    qs = (Trade.objects.filter(strategy_assignment__isnull=True, created_at__gte=cutoff)
          .filter(Q(magic_number__gte=ASSIGNMENT_MAGIC_BASE) | Q(comment__startswith="WAY"))
          .order_by("-id")[:limit])
    counts: dict = {}
    for trade in qs:
        code = stamp_trade_ownership(trade, save=True)
        counts[code] = counts.get(code, 0) + 1
    return counts
