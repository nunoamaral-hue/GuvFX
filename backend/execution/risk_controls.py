"""
GFX-PKT-E3-RUNTIME-RISK-CONTROLS — pre-E3 runtime risk gates (fail-closed).

Additive, shadow-testable risk controls evaluated at **promotion time** (before any
``PLACE_ORDER_SHADOW`` job is created) plus a runtime staleness re-check the worker
applies before it validates. These gates are the pre-condition for a future
demo-live path — they place NO order, call NO ``order_send``, and do NOT touch the
kill switch. Every evaluator is **fail-closed**: if risk state cannot be determined
(any exception), it returns a block reason rather than allowing the action.

Exposure counts BOTH paths on a shared account (see Blueprint doc 06): real open
``Trade`` positions AND in-flight signal exposure (legs of PROMOTED plans).

Controls:
  1. per-account exposure limit        (account_exposure)
  2. per-symbol exposure limit         (symbol_exposure)
  3. max open positions / active jobs  (max_open_positions)
  4. daily drawdown guard              (daily_drawdown)
  6. concurrent-position enforcement   (concurrent_positions)
(5 — runtime staleness re-check — lives in the worker; the signal_timestamp is
propagated into the shadow payload for it.)
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger("guvfx.execution.risk_controls")

from execution.models import (
    MAX_TOTAL_LOT_PER_SIGNAL,
    PLAN_MAX_CONCURRENT_GROUPS,
    ExecutionJob,
    ProposedOrderLeg,
    SignalExecutionPlan,
)
from trading.models import Trade


def _dec_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(str(os.getenv(name, default)))
    except Exception:
        return Decimal(default)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


# Caps (env-overridable). Sized to admit ONE 1.20-lot ti_signals signal (3 × 0.40) plus one
# concurrent overlap = 2.40 lot, bounded (NOT unlimited, NOT a blind 20×). These are the shared
# account/symbol AGGREGATE ceilings; per-SOURCE SIZING is enforced upstream (SignalSourceConfig
# caps → split/promotion/worker/bridge), so raising these to 2.40 does NOT enlarge wayond — its
# per-leg cap stays 0.02, holding it to ~0.40 aggregate. The real margin protection at this size
# is the FREE-MARGIN GUARD below (a lot cap cannot model price/leverage). Env-tunable.
MAX_ACCOUNT_EXPOSURE_LOT = _dec_env("RISK_MAX_ACCOUNT_EXPOSURE_LOT", "2.40")
MAX_SYMBOL_EXPOSURE_LOT = _dec_env("RISK_MAX_SYMBOL_EXPOSURE_LOT", "2.40")
MAX_OPEN_POSITIONS_PER_ACCOUNT = _int_env("RISK_MAX_OPEN_POSITIONS", 20)
MAX_DAILY_DRAWDOWN_ABS = _dec_env("RISK_MAX_DAILY_DRAWDOWN_ABS", "100.00")

# Free-margin guard (env-tunable). Reject a promotion whose projected fill would push the account's
# margin level below this floor. At 20× lot size margin burns 20× faster and a lot cap cannot model
# price/leverage, so this live-account gate is the real safety net — and it is FAIL-CLOSED: for a
# larger-than-default order it BLOCKS (margin_unverifiable) when margin cannot be verified, so a 20×
# order is never placed unverified. It requires the promotion process (the listener) to reach the
# bridge; deploy verifies that path. Runs only for orders above the 0.06 default total.
MARGIN_LEVEL_FLOOR_PCT = _dec_env("RISK_MARGIN_LEVEL_FLOOR_PCT", "300")
MARGIN_GUARD_ENABLED = os.getenv("RISK_MARGIN_GUARD_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)


def _require_terminal_node() -> bool:
    """E3-NODE-ASSIGNMENT-ENFORCEMENT: opt-in (default OFF) so existing behaviour
    is preserved — prod accounts currently ride the legacy null-node claim route.
    Read per-call (not import-time) so tests and a deploy-time enable need no
    reload. Enable at E3 only after every real account has an ACTIVE node."""
    return os.getenv("RISK_REQUIRE_TERMINAL_NODE", "").strip().lower() in ("1", "true", "yes", "on")


def node_assignment_block_reason(account) -> str | None:
    """Terminal-node enforcement for one account (flag-gated, fail-closed by the
    caller's wrapper). Blocks promotion when the account has no terminal node or
    its node is not operator-declared ACTIVE (draining/offline/disabled)."""
    if not _require_terminal_node():
        return None
    node = account.terminal_node
    if node is None:
        return "account_node_unassigned"
    if node.status != node.Status.ACTIVE:
        return "node_not_active"
    return None

_ORDER_OPENING_JOBS = (
    ExecutionJob.JobType.OPEN_TRADE,
    ExecutionJob.JobType.PLACE_ORDER,
    ExecutionJob.JobType.PLACE_ORDER_SHADOW,
)
_ACTIVE_PLAN_STATUSES = (
    SignalExecutionPlan.Status.PLANNED,
    SignalExecutionPlan.Status.PROMOTED,
)


# --- state helpers (each already fail-closed by the outer evaluator) ---------

def _open_position_lots(account_id, symbol=None) -> Decimal:
    """Sum of volumes of currently-open real positions (Trade.close_time is null)."""
    qs = Trade.objects.filter(account_id=account_id, close_time__isnull=True)
    if symbol:
        qs = qs.filter(symbol=symbol)
    return qs.aggregate(s=Sum("volume"))["s"] or Decimal("0")


def _active_signal_lots(account_id, symbol=None, exclude_plan_id=None) -> Decimal:
    """IN-FLIGHT signal exposure: leg lots of PROMOTED plans whose order is NOT YET reflected as an
    open ``Trade``. A leg whose order has filled is already an open position counted by
    ``_open_position_lots`` — counting it here too would DOUBLE-COUNT the same lots and falsely trip
    the exposure gate the moment a second signal overlaps an open one (the first overlap blocked
    every subsequent signal). We dedup by the leg's order comment ``WAY{plan}L{leg}``: a leg whose
    comment matches an open trade is skipped (already counted as a position); only genuinely
    in-flight legs (promoted, order placed, not yet ingested) are added here."""
    legs = ProposedOrderLeg.objects.filter(
        plan__account_id=account_id, plan__status=SignalExecutionPlan.Status.PROMOTED
    ).select_related("plan")
    if symbol:
        legs = legs.filter(plan__symbol=symbol)
    if exclude_plan_id is not None:
        legs = legs.exclude(plan_id=exclude_plan_id)
    open_qs = Trade.objects.filter(account_id=account_id, close_time__isnull=True).exclude(comment="")
    if symbol:
        open_qs = open_qs.filter(symbol=symbol)
    open_comments = set(open_qs.values_list("comment", flat=True))
    total = Decimal("0")
    for leg in legs:
        if f"WAY{leg.plan_id}L{leg.leg_index}" not in open_comments:  # not yet an open trade
            total += leg.lot_size or Decimal("0")
    return total


def exposure_attribution(account_id, symbol=None) -> dict:
    """WS-C: per-SOURCE breakdown of what holds the SHARED account concurrency/exposure budget.

    Account exposure, symbol exposure and concurrency are per-account aggregates SHARED between TI
    and Wayond — a busy Wayond book can block TI (and vice-versa) under a same-source-looking code.
    This makes the shared budget's real consumer visible, so a block can be told apart from a
    stalled-resolver slot leak. Best-effort/read-only; source resolved via ``plan.source``."""
    from collections import defaultdict
    acc = defaultdict(lambda: {"active_plans": 0, "inflight_lots": Decimal("0")})
    plans = SignalExecutionPlan.objects.filter(
        account_id=account_id, status__in=_ACTIVE_PLAN_STATUSES)
    if symbol:
        plans = plans.filter(symbol=symbol)
    for src in plans.values_list("source", flat=True):
        acc[src or "unknown"]["active_plans"] += 1
    legs = ProposedOrderLeg.objects.filter(
        plan__account_id=account_id, plan__status=SignalExecutionPlan.Status.PROMOTED
    ).select_related("plan")
    if symbol:
        legs = legs.filter(plan__symbol=symbol)
    open_comments = set(
        Trade.objects.filter(account_id=account_id, close_time__isnull=True)
        .exclude(comment="").values_list("comment", flat=True))
    for leg in legs:
        if f"WAY{leg.plan_id}L{leg.leg_index}" not in open_comments:
            acc[(leg.plan.source or "unknown")]["inflight_lots"] += leg.lot_size or Decimal("0")
    return {s: {"active_plans": v["active_plans"], "inflight_lots": str(v["inflight_lots"])}
            for s, v in acc.items()}


def _open_positions_count(account_id) -> int:
    """Open real positions + active (PENDING/RUNNING) order-opening jobs."""
    open_trades = Trade.objects.filter(account_id=account_id, close_time__isnull=True).count()
    active_jobs = ExecutionJob.objects.filter(
        account_id=account_id,
        status__in=(ExecutionJob.Status.PENDING, ExecutionJob.Status.RUNNING),
        job_type__in=_ORDER_OPENING_JOBS,
    ).count()
    return open_trades + active_jobs


def _today_realized_pnl(account_id) -> Decimal:
    """Sum of realized P&L on positions closed today (aware, local day)."""
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    qs = Trade.objects.filter(
        account_id=account_id, close_time__isnull=False, close_time__gte=start
    )
    return qs.aggregate(s=Sum("profit"))["s"] or Decimal("0")


# --- control evaluators (return a block reason code, or None) ----------------

def _margin_guard_reason(plan, legs, new_total) -> str | None:
    """Free-margin guard: reject a promotion whose PROJECTED post-trade margin level would fall
    below ``MARGIN_LEVEL_FLOOR_PCT``.

    Uses the bridge ``order_check`` (which places NO order) for a single representative leg to read
    live equity + per-leg margin, then extrapolates the projected margin level for the full plan's
    volume — margin scales linearly with lots. Only runs for orders LARGER than the conservative
    global default (a within-default 0.06 order never stresses a 50k demo — skipped, no network).

    FAIL-CLOSED. For a larger (source-scoped) order this guard is the real safety net at 20× lot
    size — a lot cap cannot model price/leverage. So if the projected margin CANNOT be verified for
    such an order (bridge unreachable / token wrong / missing response fields / timeout), the
    promotion is BLOCKED (``margin_unverifiable``): a 20× order is never placed unverified. Every
    material decision (verified-OK / block / fail-closed) is logged so an inert or blocking guard is
    observable (the pre-network skips — guard disabled, small order — return quietly). Returns a
    block reason or ``None``."""
    if not MARGIN_GUARD_ENABLED:
        return None
    # Small orders (<= the global default total) never threaten margin — skip the network call.
    if new_total <= Decimal(str(MAX_TOTAL_LOT_PER_SIGNAL)):
        return None
    try:
        import json as _json
        import urllib.request as _rq

        base = (os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL") or os.getenv("GUVFX_AGENT_URL")
                or os.getenv("WINDOWS_AGENT_BASE") or "").rstrip("/")
        token = (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_WINDOWS_AGENT_TOKEN")
                 or os.getenv("GUVFX_AGENT_TOKEN") or "").strip().strip('"')
        if not base or not legs:
            logger.warning("margin_guard: cannot verify (no bridge base / no legs) plan=%s total=%s"
                           " -> FAIL-CLOSED", getattr(plan, "id", "?"), new_total)
            return "margin_unverifiable"
        acct = plan.account
        uname = None
        if getattr(acct, "mt5_instance_id", None):
            uname = getattr(acct.mt5_instance, "windows_username", None)
        leg_lot = float(legs[0].lot_size)
        if leg_lot <= 0:
            logger.warning("margin_guard: non-positive leg lot plan=%s -> FAIL-CLOSED",
                           getattr(plan, "id", "?"))
            return "margin_unverifiable"
        body = {
            "username": uname, "symbol": plan.symbol, "side": plan.direction,
            "lots": leg_lot, "sl_price": plan.stop_loss, "tp_price": legs[0].take_profit,
            "max_lot": leg_lot, "signal_source": plan.source,
            "execution_mode": "SHADOW", "comment": f"WAY{plan.id}MG",
        }
        req = _rq.Request(
            f"{base}/mt5/order_check", data=_json.dumps(body).encode("utf-8"), method="POST",
            headers={"X-GuvFX-Agent-Token": token, "Content-Type": "application/json"},
        )
        # 30s to match the worker's order_check timeout — a cold MT5 init can exceed a short one.
        with _rq.urlopen(req, timeout=30) as r:
            resp = _json.loads((r.read() or b"{}").decode("utf-8") or "{}")
        equity = resp.get("equity")
        free_after = resp.get("free_margin")   # free margin AFTER the 1-leg check order
        leg_margin = resp.get("margin")        # margin required for the 1-leg check order
        if not equity or leg_margin is None or free_after is None or float(leg_margin) <= 0:
            logger.warning("margin_guard: incomplete order_check response plan=%s resp_keys=%s"
                           " -> FAIL-CLOSED", getattr(plan, "id", "?"), sorted(resp.keys()))
            return "margin_unverifiable"
        equity = float(equity)
        margin_used_before = equity - float(free_after) - float(leg_margin)   # before the check leg
        margin_per_lot = float(leg_margin) / leg_lot
        projected_margin_used = margin_used_before + float(new_total) * margin_per_lot
        if projected_margin_used <= 0:
            logger.info("margin_guard: projected margin ~0 (no exposure) plan=%s -> OK",
                        getattr(plan, "id", "?"))
            return None
        projected_level = equity / projected_margin_used * 100
        if projected_level < float(MARGIN_LEVEL_FLOOR_PCT):
            logger.warning("margin_guard: projected margin_level %.0f%% < floor %s%% plan=%s total=%s"
                           " -> BLOCK", projected_level, MARGIN_LEVEL_FLOOR_PCT,
                           getattr(plan, "id", "?"), new_total)
            return "margin_level_too_low"
        logger.info("margin_guard: projected margin_level %.0f%% >= floor plan=%s total=%s -> OK",
                    projected_level, getattr(plan, "id", "?"), new_total)
        return None
    except Exception as exc:
        # FAIL-CLOSED: a 20× order must never be placed when margin cannot be verified.
        logger.warning("margin_guard: order_check failed (%s: %s) plan=%s -> FAIL-CLOSED",
                       type(exc).__name__, str(exc)[:120], getattr(plan, "id", "?"))
        return "margin_unverifiable"


def evaluate_promotion_risk(plan, legs) -> str | None:
    """Run all promotion-time risk controls. Returns the first block reason code
    (a stable string) or ``None`` if all pass. FAIL-CLOSED: any error blocks."""
    try:
        account_id = plan.account_id
        symbol = plan.symbol
        new_total = sum((leg.lot_size for leg in legs), Decimal("0"))

        # 0: terminal-node assignment enforcement (flag-gated, default OFF)
        node_reason = node_assignment_block_reason(plan.account)
        if node_reason:
            return node_reason

        # 1 + 2: exposure (shared budget: open positions + in-flight signal legs)
        acct_exposure = _open_position_lots(account_id) + _active_signal_lots(
            account_id, exclude_plan_id=plan.id
        )
        if acct_exposure + new_total > MAX_ACCOUNT_EXPOSURE_LOT:
            logger.warning("risk: account_exposure_exceeded plan=%s source=%s used=%s+new=%s>cap=%s attribution=%s",
                           plan.id, plan.source, acct_exposure, new_total, MAX_ACCOUNT_EXPOSURE_LOT,
                           exposure_attribution(account_id))
            return "account_exposure_exceeded"

        sym_exposure = _open_position_lots(account_id, symbol) + _active_signal_lots(
            account_id, symbol, exclude_plan_id=plan.id
        )
        if sym_exposure + new_total > MAX_SYMBOL_EXPOSURE_LOT:
            logger.warning("risk: symbol_exposure_exceeded plan=%s symbol=%s source=%s attribution=%s",
                           plan.id, symbol, plan.source, exposure_attribution(account_id, symbol))
            return "symbol_exposure_exceeded"

        # 3: max open positions / active jobs
        if _open_positions_count(account_id) >= MAX_OPEN_POSITIONS_PER_ACCOUNT:
            return "max_open_positions_reached"

        # 4: daily drawdown guard
        if _today_realized_pnl(account_id) <= -MAX_DAILY_DRAWDOWN_ABS:
            return "daily_drawdown_hit"

        # 5: free-margin guard (live projected margin level via bridge order_check; fail-open on
        #    read error). The real margin safety net for larger source-scoped orders — a lot cap
        #    cannot model price/leverage.
        margin_reason = _margin_guard_reason(plan, legs, new_total)
        if margin_reason:
            return margin_reason

        # 6: concurrent-position enforcement (other active plans, same account+symbol)
        other_active = (
            SignalExecutionPlan.objects.filter(
                account_id=account_id, symbol=symbol, status__in=_ACTIVE_PLAN_STATUSES
            )
            .exclude(id=plan.id)
            .count()
        )
        if other_active >= PLAN_MAX_CONCURRENT_GROUPS:
            logger.warning("risk: concurrent_position_limit plan=%s symbol=%s source=%s other_active=%s attribution=%s",
                           plan.id, symbol, plan.source, other_active, exposure_attribution(account_id, symbol))
            return "concurrent_position_limit"

        return None
    except Exception:
        # FAIL CLOSED — indeterminate risk state must block, never allow.
        return "risk_state_indeterminate"
