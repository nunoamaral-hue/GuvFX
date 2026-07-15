"""
WS-B AUTO-BREAKEVEN — move a signal's remaining legs to breakeven once TP1 closes.

When a plan's TP1 leg (``leg_index == 1``) closes at profit, its higher legs (TP2/TP3) are still
open. This sweep detects that and, for each still-OPEN remaining leg, enqueues ONE
``MODIFY_POSITION`` ExecutionJob to move that position's stop-loss to its ENTRY price (breakeven),
so the rest of the trade can no longer turn into a loss.

Design (each property is required by the packet):

* **Automatic + VPS-side** — invoked as a step of the per-minute ``run_monitor_chain`` cron inside
  the backend container. No developer machine is involved.
* **Enqueue-only (least privilege)** — this backend module holds NO bridge credentials. It only
  writes ``MODIFY_POSITION`` jobs to the DB; the ingest worker (which already holds the bridge
  token) claims and executes them. The bridge performs the SL edit via ``TRADE_ACTION_SLTP`` and
  re-reads the position to confirm the SL landed.
* **Idempotent** — a leg carries ``breakeven_applied_at`` (terminal, set only after the modify job
  SUCCEEDs) and ``breakeven_job`` (the in-flight/last job). A leg that is applied, or has a
  PENDING/RUNNING job, is never re-enqueued. The bridge additionally no-ops a redundant modify.
* **Retry-on-failure** — a FAILED modify job is re-enqueued up to ``MAX_BREAKEVEN_ATTEMPTS``.
* **Fail-safe / never increase risk** — a modify is enqueued only when the new SL (the entry) is
  strictly risk-reducing versus the plan's original SL (BUY: SL moves up; SELL: SL moves down).
  If that cannot be proven (missing/blank SL, unknown direction), the leg is SKIPPED, never moved.
  The bridge enforces the same guard against the live position SL as a backstop.
* **Verify** — "modification occurred" == the modify job reached SUCCESS, which the worker sets
  only when the bridge returns ``ok`` (i.e. the re-read broker SL matched the request).
* **Alert on failure** — after ``MAX_BREAKEVEN_ATTEMPTS`` failures a single (deduped) ``AlertEvent``
  is raised for operators.

HARD BOUNDARY: this module NEVER opens or closes a position and NEVER changes lot size, TP, risk %,
or any sizing. It only edits an existing position's SL in the risk-reducing direction. It is inert
unless ``BREAKEVEN_ENABLED`` is set (deploy-dark, then arm in a controlled window).
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from trading.models import Trade

from execution.models import ExecutionJob, SignalExecutionPlan

logger = logging.getLogger("guvfx.execution.breakeven")

DEFAULT_LIMIT = 500
MAX_BREAKEVEN_ATTEMPTS = int(os.getenv("BREAKEVEN_MAX_ATTEMPTS", "3"))


def breakeven_enabled() -> bool:
    """Master arm switch. Inert (no sync, no modify) unless explicitly enabled."""
    return os.getenv("BREAKEVEN_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _is_risk_reducing(direction: str, new_sl: Decimal | None, old_sl: Decimal | None) -> bool:
    """True only when moving the SL to ``new_sl`` cannot increase risk. FAIL-CLOSED: any missing
    value or unknown direction returns False (the leg is then skipped, never widened)."""
    if new_sl is None or old_sl is None:
        return False
    d = (direction or "").strip().upper()
    if d == "BUY":
        return new_sl > old_sl   # BUY SL sits below entry; moving it up reduces downside
    if d == "SELL":
        return new_sl < old_sl   # SELL SL sits above entry; moving it down reduces downside
    return False


def _leg_comment(plan: SignalExecutionPlan, leg) -> str:
    """The broker order comment that correlates a Trade to this leg (see signal_promotion)."""
    return "WAY%dL%d" % (plan.id, leg.leg_index)


def _leg_trade(plan: SignalExecutionPlan, leg):
    """Latest ingested Trade for a leg (by its correlation comment), newest open_time first."""
    return (
        Trade.objects.filter(account_id=plan.account_id, comment=_leg_comment(plan, leg))
        .order_by("-open_time")
        .first()
    )


def _windows_username(account) -> str | None:
    inst = getattr(account, "mt5_instance", None)
    return getattr(inst, "windows_username", None) if inst else None


def _ensure_position_sync(account_ids) -> int:
    """Enqueue one plain SYNC_POSITIONS per account that holds open plans, IF none is already
    pending/running for that account. This keeps position closes ingested promptly (every tick)
    so breakeven — and the WIN-card pipeline — react without waiting for the next order. Read-only
    ingestion (never exposure-opening); safe to run every minute. Best-effort: a failure to
    enqueue never aborts the sweep."""
    queued = 0
    for account_id in account_ids:
        try:
            already = ExecutionJob.objects.filter(
                account_id=account_id,
                job_type=ExecutionJob.JobType.SYNC_POSITIONS,
                status__in=[ExecutionJob.Status.PENDING, ExecutionJob.Status.RUNNING],
            ).exists()
            if already:
                continue
            from trading.models import TradingAccount
            account = TradingAccount.objects.filter(id=account_id).select_related("mt5_instance").first()
            username = _windows_username(account) if account else None
            if not username:
                continue
            ExecutionJob.objects.create(
                job_type=ExecutionJob.JobType.SYNC_POSITIONS,
                account_id=account_id,
                # Same node routing as placement-triggered SYNC so the worker can claim it.
                terminal_node_id=account.terminal_node_id,
                status=ExecutionJob.Status.PENDING,
                payload={"windows_username": username, "breakeven_sync": True},
            )
            queued += 1
        except Exception:  # pragma: no cover - defensive; sync is best-effort
            logger.exception("breakeven: failed to enqueue position sync for account %s", account_id)
    return queued


def _enqueue_modify(plan: SignalExecutionPlan, leg, trade, be_price: Decimal):
    """Create ONE MODIFY_POSITION job moving this leg's position SL to ``be_price`` (breakeven).
    Enqueue-only; the worker executes it against the bridge. Returns the job, or None on failure."""
    username = _windows_username(plan.account)
    if not username or not trade.ticket:
        logger.warning("breakeven: plan %s leg %s missing username/ticket; skipping",
                       plan.id, leg.leg_index)
        return None
    tp = _to_decimal(leg.take_profit)
    payload = {
        "windows_username": username,
        "ticket": trade.ticket,          # Trade.ticket stores the MT5 position_id
        "symbol": plan.symbol,
        "sl": float(be_price),
        "plan_id": plan.id,
        "leg_index": leg.leg_index,
        "signal_source": plan.source,
        "correlation_id": plan.correlation_id or "",
        "reason": "auto_breakeven_tp1",
    }
    if tp is not None:
        payload["tp"] = float(tp)
    return ExecutionJob.objects.create(
        job_type=ExecutionJob.JobType.MODIFY_POSITION,
        account_id=plan.account_id,
        # Match the leg's PLACE_ORDER routing so the SAME node-aware worker can claim it.
        terminal_node_id=plan.account.terminal_node_id,
        status=ExecutionJob.Status.PENDING,
        payload=payload,
    )


def _alert_breakeven_failure(plan: SignalExecutionPlan, leg, job) -> None:
    """Raise ONE deduped operator alert after breakeven exhausts its retries. Best-effort:
    an alerting failure never breaks the sweep."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = f"breakeven_failed:plan:{plan.id}:leg:{leg.leg_index}"
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.CRITICAL,
            component=Component.EXECUTION_PIPELINE,
            trading_account_id=plan.account_id,
            title=f"Auto-breakeven FAILED plan #{plan.id} TP{leg.leg_index}",
            body=(f"Moving TP{leg.leg_index} of plan #{plan.id} ({plan.symbol} {plan.direction}) "
                  f"to breakeven failed after {leg.breakeven_attempts} attempts. Position may still "
                  f"be exposed to loss. Last job #{getattr(job, 'id', '?')}: "
                  f"{getattr(job, 'error_message', '')[:200]}"),
            dedup_key=dedup_key,
            status=AlertEvent.Status.OPEN,
            detail={"plan_id": plan.id, "leg_index": leg.leg_index,
                    "attempts": leg.breakeven_attempts, "job_id": getattr(job, "id", None)},
        )
        logger.error("breakeven: ALERT raised for plan %s leg %s (exhausted retries)",
                     plan.id, leg.leg_index)
    except Exception:  # pragma: no cover - defensive; alerting is best-effort
        logger.exception("breakeven: failed to raise alert for plan %s leg %s",
                         plan.id, leg.leg_index)


def sweep_breakeven(*, limit: int = DEFAULT_LIMIT) -> dict:
    """One idempotent pass. For each PROMOTED plan whose TP1 leg has CLOSED, enqueue a breakeven
    SL move for every remaining OPEN leg (fail-safe, deduped). Returns a counts dict."""
    counts = {"enabled": True, "scanned": 0, "synced": 0, "enqueued": 0, "applied": 0,
              "inflight": 0, "skipped": 0, "alerted": 0}
    if not breakeven_enabled():
        return {"enabled": False}

    terminal = (ExecutionJob.Status.SUCCESS, ExecutionJob.Status.FAILED)
    active = (ExecutionJob.Status.PENDING, ExecutionJob.Status.RUNNING)

    plans = list(
        SignalExecutionPlan.objects.filter(status=SignalExecutionPlan.Status.PROMOTED)
        .select_related("account", "account__mt5_instance")
        .order_by("id")[:limit]
    )

    # Keep position state fresh so a just-closed TP1 is visible to this and the next tick.
    counts["synced"] = _ensure_position_sync({p.account_id for p in plans})

    for plan in plans:
        counts["scanned"] += 1
        legs = list(plan.legs.select_related("breakeven_job").order_by("leg_index"))
        tp1 = next((l for l in legs if l.leg_index == 1), None)
        if tp1 is None:
            continue
        # Breakeven triggers only once TP1's position has CLOSED (its TP was hit). If TP1 is still
        # open — or its close has not been ingested yet — do nothing (fail-closed on ingestion lag).
        tp1_trade = _leg_trade(plan, tp1)
        if tp1_trade is None or tp1_trade.close_time is None:
            continue

        for leg in legs:
            if leg.leg_index == 1:
                continue
            if leg.breakeven_applied_at:
                continue  # terminal — already moved and broker-verified
            trade = _leg_trade(plan, leg)
            if trade is None or trade.close_time is not None:
                continue  # this leg is not a currently-OPEN position (closed or never filled)

            # Reconcile a prior modify job before enqueuing another.
            job = leg.breakeven_job
            if job is not None:
                if job.status == ExecutionJob.Status.SUCCESS:
                    leg.breakeven_applied_at = timezone.now()
                    leg.save(update_fields=["breakeven_applied_at"])
                    counts["applied"] += 1
                    continue
                if job.status in active:
                    counts["inflight"] += 1
                    continue
                # FAILED
                if leg.breakeven_attempts >= MAX_BREAKEVEN_ATTEMPTS:
                    _alert_breakeven_failure(plan, leg, job)
                    counts["alerted"] += 1
                    continue
                # else: fall through and retry (re-enqueue below)

            # FAIL-SAFE price guard: move to this leg's actual entry only if risk-reducing.
            be_price = _to_decimal(trade.open_price)
            old_sl = _to_decimal(plan.stop_loss)
            if not _is_risk_reducing(plan.direction, be_price, old_sl):
                logger.info("breakeven: plan %s leg %s NOT risk-reducing (entry=%s sl=%s dir=%s); skip",
                            plan.id, leg.leg_index, be_price, old_sl, plan.direction)
                counts["skipped"] += 1
                continue

            new_job = _enqueue_modify(plan, leg, trade, be_price)
            if new_job is None:
                counts["skipped"] += 1
                continue
            leg.breakeven_job = new_job
            leg.breakeven_attempts = (leg.breakeven_attempts or 0) + 1
            leg.save(update_fields=["breakeven_job", "breakeven_attempts"])
            counts["enqueued"] += 1
            logger.info("breakeven: plan %s leg %s → MODIFY_POSITION job %s sl=%s (attempt %s)",
                        plan.id, leg.leg_index, new_job.id, be_price, leg.breakeven_attempts)

    return counts
