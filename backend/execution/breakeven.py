"""
WS-INCREMENTAL-TP-PROTECTION — monotonic per-leg stop-loss protection ladder.

Each TP of a signal is an INDEPENDENT broker position (never a partial close of one). As profit
targets close, the remaining open legs are protected further, monotonically (SL only ever moves in
the risk-reducing direction):

  INITIAL           original SL on every leg
  BREAKEVEN         after TP1 closes in profit → every remaining OPEN leg's SL → its OWN entry
  TP2_LOCKED        after TP2 closes in profit → the remaining TP3 leg's SL → the planned TP2 price
  (COMPLETE)        TP3 closes → nothing more to do

The DESIRED stage is derived from the HIGHEST profit target confirmed closed — so if TP1 and TP2
both closed before a sweep runs (they can close seconds apart), TP3 advances DIRECTLY to TP2_LOCKED,
never wasting a cycle at BREAKEVEN. State is persisted per leg (``ProposedOrderLeg.protection_stage``)
so it is exactly-once, idempotent and independent of any developer/Claude process.

Design (each property required by the packet):
* **Automatic + VPS-side** — a step of the per-minute ``run_monitor_chain`` cron. No dev machine.
* **Enqueue-only (least privilege)** — this backend holds NO bridge credentials; it writes
  ``MODIFY_POSITION`` jobs; the ingest worker (which holds the token) executes them; the bridge does
  the ``TRADE_ACTION_SLTP`` edit, re-reads the position and returns ``verified_sl`` + ``prior_sl``.
* **Monotonic / never increase risk** — a stage advance is enqueued only when its target SL is
  strictly risk-reducing vs the leg's current-stage baseline; the bridge additionally hard-refuses
  any widen vs the LIVE position SL, so a more-protective MANUAL stop is preserved.
* **Profit-gated** — a stage advances only when the relevant TP leg closed AT/THROUGH its TP in the
  profit direction (not at SL, not a short manual close). Fail-closed on ambiguity.
* **Idempotent** — per-leg ``protection_stage`` + a per-(ticket,stage) in-flight guard; a verified
  modify is never repeated; a position that closed before the modify is a benign no-op.
* **Retry + alert** — a stage's modify retries to ``BREAKEVEN_MAX_ATTEMPTS`` then one deduped alert.
* **Prompt detection** — enqueues a deduped periodic SYNC_POSITIONS so TP closes ingest each tick.

Inert unless ``BREAKEVEN_ENABLED``. Source-isolated: leg↔position correlation is by ``plan.id``
(``WAY{plan.id}L{leg}``), so a TI plan can never touch a Wayond position.
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
# A protection advance still unverified this long after its trigger TP closed is overdue (the modify
# is stuck / repeatedly failing) → operator alert. ~3 monitor cycles by default.
PROTECTION_OVERDUE_SECONDS = int(os.getenv("PROTECTION_OVERDUE_SECONDS", "180"))
# How much of the entry→TP distance the close must cover (profit direction) to count as "TP hit".
# 0.9 tolerates broker slippage while rejecting a short manual close taken well before the TP.
_TP_REACH_FRACTION = Decimal(os.getenv("BREAKEVEN_TP_REACH_FRACTION", "0.9"))

STAGE_INITIAL = "INITIAL"
STAGE_BREAKEVEN = "BREAKEVEN"
STAGE_TP2_LOCKED = "TP2_LOCKED"
_STAGE_RANK = {STAGE_INITIAL: 0, STAGE_BREAKEVEN: 1, STAGE_TP2_LOCKED: 2}


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


def _closed_profitably(trade, tp_price, entry, direction) -> bool:
    """True if ``trade`` closed AT/THROUGH its take-profit in the profit direction (a TP hit), not at
    SL and not a short manual close. Fail-closed on any missing/ambiguous value."""
    if trade is None or trade.close_time is None:
        return False
    cp = _to_decimal(trade.close_price)
    tp = _to_decimal(tp_price)
    en = _to_decimal(entry)
    if cp is None or tp is None or en is None:
        return False
    dist = abs(tp - en)
    if dist == 0:
        return False
    d = (direction or "").strip().upper()
    reached = dist * _TP_REACH_FRACTION
    if d == "SELL":
        return (en - cp) >= reached   # SELL profits as price falls toward/through the (lower) TP
    if d == "BUY":
        return (cp - en) >= reached   # BUY profits as price rises toward/through the (higher) TP
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
    so protection — and the WIN-card pipeline — react without waiting for the next order. The
    execution_health monitor step reclaims any orphaned RUNNING SYNC so a dead one can never
    suppress future syncs indefinitely. Read-only ingestion; safe to run every minute. Best-effort."""
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
                terminal_node_id=account.terminal_node_id,
                status=ExecutionJob.Status.PENDING,
                payload={"windows_username": username, "breakeven_sync": True},
            )
            queued += 1
        except Exception:  # pragma: no cover - defensive; sync is best-effort
            logger.exception("breakeven: failed to enqueue position sync for account %s", account_id)
    return queued


def _protection_inflight(trade, stage) -> bool:
    """A non-terminal MODIFY_POSITION for THIS account+ticket+stage already exists → don't duplicate.
    Account-scoped (MT5 tickets are per-account, not global)."""
    return ExecutionJob.objects.filter(
        job_type=ExecutionJob.JobType.MODIFY_POSITION,
        status__in=(ExecutionJob.Status.PENDING, ExecutionJob.Status.RUNNING),
        account_id=trade.account_id, payload__ticket=trade.ticket, payload__protection_stage=stage,
    ).exists()


def _enqueue_modify(plan: SignalExecutionPlan, leg, trade, target_sl: Decimal, stage: str):
    """Create ONE MODIFY_POSITION job advancing this leg's SL to ``target_sl`` for ``stage``.
    Enqueue-only; the worker executes it against the bridge. Idempotency key + stage recorded so a
    verified stage is never repeated. Returns the job, or None on missing data / already-in-flight."""
    username = _windows_username(plan.account)
    if not username or not trade.ticket:
        logger.warning("protection: plan %s leg %s missing username/ticket; skipping",
                       plan.id, leg.leg_index)
        return None
    if _protection_inflight(trade, stage):
        return None
    payload = {
        "windows_username": username,
        "ticket": trade.ticket,          # Trade.ticket stores the MT5 position_id
        "symbol": plan.symbol,
        "sl": float(target_sl),
        "plan_id": plan.id,
        "leg_index": leg.leg_index,
        "signal_source": plan.source,
        "correlation_id": plan.correlation_id or "",
        "protection_stage": stage,
        "idempotency_key": f"plan-{plan.id}-leg-{leg.leg_index}-{stage.lower()}-sl-{target_sl}",
        "reason": f"tp_protection_{stage.lower()}",
    }
    tp = _to_decimal(leg.take_profit)
    if tp is not None:
        payload["tp"] = float(tp)   # preserve the leg's own take-profit during the SL edit
    return ExecutionJob.objects.create(
        job_type=ExecutionJob.JobType.MODIFY_POSITION,
        account_id=plan.account_id,
        terminal_node_id=plan.account.terminal_node_id,
        status=ExecutionJob.Status.PENDING,
        payload=payload,
    )


def _desired_stage(plan, leg, legs_by_index, trades_by_index, allow_tp2_lock: bool):
    """The HIGHEST protection stage applicable to this OPEN leg, and its target SL, from the profit
    targets confirmed closed. Returns (stage, target_sl_or_None). ``allow_tp2_lock`` gates the NEW
    TP2-lock stage per source (Wayond stays at state-1 breakeven; ti_signals gets the full ladder)."""
    k = leg.leg_index
    l1, l2 = legs_by_index.get(1), legs_by_index.get(2)
    t1, t2 = trades_by_index.get(1), trades_by_index.get(2)
    tp1_hit = bool(l1 and t1 and _closed_profitably(t1, l1.take_profit, t1.open_price, plan.direction))
    tp2_hit = bool(l2 and t2 and _closed_profitably(t2, l2.take_profit, t2.open_price, plan.direction))
    # TP2 hit → the last remaining leg (TP3) locks in at the planned TP2 price (skips breakeven).
    if allow_tp2_lock and tp2_hit and k == 3:
        return STAGE_TP2_LOCKED, _to_decimal(l2.take_profit)
    # TP1 hit → remaining legs (2, 3) move to their OWN entry (breakeven).
    if tp1_hit and k in (2, 3):
        my = trades_by_index.get(k)
        return STAGE_BREAKEVEN, (_to_decimal(my.open_price) if my else None)
    return STAGE_INITIAL, None


def _incremental_sources() -> set:
    """Sources opted in to the TP2-lock stage (per-source config)."""
    try:
        from execution.models import SignalSourceConfig
        return set(SignalSourceConfig.objects.filter(incremental_protection_enabled=True)
                   .values_list("source", flat=True))
    except Exception:  # pragma: no cover - defensive
        return set()


def _baseline_sl(plan, leg, trade) -> Decimal | None:
    """The SL a stage advance is judged risk-reducing against: the current-stage SL (entry once
    breakeven-applied, else the plan's original SL). The bridge additionally backstops vs the LIVE SL."""
    if (leg.protection_stage or STAGE_INITIAL) == STAGE_BREAKEVEN:
        return _to_decimal(trade.open_price)
    return _to_decimal(plan.stop_loss)


def _alert_protection_failure(plan, leg, stage, job) -> None:
    """One deduped CRITICAL alert after a stage's modify exhausts its retries. Best-effort."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = f"tp_protection_failed:plan:{plan.id}:leg:{leg.leg_index}:{stage}"
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.CRITICAL,
            component=Component.EXECUTION_PIPELINE,
            trading_account_id=plan.account_id,
            title=f"TP protection FAILED plan #{plan.id} TP{leg.leg_index} ({stage})",
            body=(f"Advancing TP{leg.leg_index} of plan #{plan.id} ({plan.symbol} {plan.direction}) "
                  f"to {stage} failed after {leg.breakeven_attempts} attempts. The remaining position "
                  f"may be under-protected. Last job #{getattr(job, 'id', '?')}: "
                  f"{getattr(job, 'error_message', '')[:180]}"),
            dedup_key=dedup_key, status=AlertEvent.Status.OPEN,
            detail={"plan_id": plan.id, "leg_index": leg.leg_index, "stage": stage,
                    "attempts": leg.breakeven_attempts, "job_id": getattr(job, "id", None)})
        logger.error("protection: ALERT plan %s leg %s stage %s (exhausted retries)",
                     plan.id, leg.leg_index, stage)
    except Exception:  # pragma: no cover - alerting is best-effort
        logger.exception("protection: alert failed plan %s leg %s", plan.id, leg.leg_index)


def _alert_overdue_protection(plan, leg, stage, trigger_close, now) -> None:
    """WS-K: one deduped WARN when a protection advance is still unverified well after its trigger TP
    closed (stuck modify). Auto-resolves implicitly once the leg reaches the stage (dedup by stage)."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = f"tp_protection_overdue:plan:{plan.id}:leg:{leg.leg_index}:{stage}"
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return
        age = int((now - trigger_close).total_seconds()) if trigger_close else None
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.WARN, component=Component.EXECUTION_PIPELINE,
            trading_account_id=plan.account_id,
            title=f"TP protection OVERDUE plan #{plan.id} TP{leg.leg_index} ({stage})",
            body=(f"TP protection for plan #{plan.id} leg {leg.leg_index} has not been broker-verified "
                  f"{age}s after its trigger TP closed. The remaining position may be under-protected."),
            dedup_key=dedup_key, status=AlertEvent.Status.OPEN,
            detail={"plan_id": plan.id, "leg_index": leg.leg_index, "stage": stage, "overdue_s": age})
    except Exception:  # pragma: no cover - alerting is best-effort
        logger.exception("protection: overdue-alert failed plan %s leg %s", plan.id, leg.leg_index)


def sweep_breakeven(*, limit: int = DEFAULT_LIMIT) -> dict:
    """One idempotent pass of the protection ladder over every PROMOTED plan. Returns a counts dict.
    (Kept named ``sweep_breakeven`` for the monitor-chain wiring; it now drives the full ladder.)"""
    counts = {"enabled": True, "scanned": 0, "synced": 0, "enqueued": 0, "applied": 0,
              "inflight": 0, "skipped": 0, "alerted": 0, "tp2_locked": 0, "overdue": 0,
              "noop_closed": 0, "deferred": 0}
    if not breakeven_enabled():
        return {"enabled": False}

    active = (ExecutionJob.Status.PENDING, ExecutionJob.Status.RUNNING)
    plans = list(
        SignalExecutionPlan.objects.filter(status=SignalExecutionPlan.Status.PROMOTED)
        .select_related("account", "account__mt5_instance").order_by("id")[:limit]
    )
    # Keep position state fresh so a just-closed TP is visible to this and the next tick.
    counts["synced"] = _ensure_position_sync({p.account_id for p in plans})
    incremental_sources = _incremental_sources()

    for plan in plans:
        counts["scanned"] += 1
        allow_tp2_lock = plan.source in incremental_sources
        legs = list(plan.legs.select_related("breakeven_job").order_by("leg_index"))
        legs_by_index = {l.leg_index: l for l in legs}
        trades_by_index = {l.leg_index: _leg_trade(plan, l) for l in legs}

        for leg in legs:
            if leg.leg_index == 1:
                continue  # TP1 is the trigger, never itself a protection target
            trade = trades_by_index.get(leg.leg_index)
            if trade is None or trade.close_time is not None:
                continue  # not a currently-OPEN position (closed/never-filled → nothing to protect)

            desired_stage, target_sl = _desired_stage(
                plan, leg, legs_by_index, trades_by_index, allow_tp2_lock)
            current = leg.protection_stage or STAGE_INITIAL
            if _STAGE_RANK[desired_stage] <= _STAGE_RANK.get(current, 0):
                continue  # already protected at/beyond the desired stage (monotonic)

            # Reconcile the last modify job before enqueuing another.
            job = leg.breakeven_job
            job_stage = (job.payload or {}).get("protection_stage") if job is not None else None
            job_result = (job.result or {}) if job is not None else {}
            attempted_this_stage = job is not None and job_stage == desired_stage

            # WS-K: alert only when this stage has ACTUALLY been attempted and its modify is stuck —
            # never on the first sighting of a TP-close that ingested late (that is a fresh detection,
            # not a stuck modify). The trigger is TP1 for breakeven, TP2 for the lock.
            now = timezone.now()
            trig = trades_by_index.get(2 if desired_stage == STAGE_TP2_LOCKED else 1)
            trig_close = trig.close_time if trig else None
            if (attempted_this_stage and job.status != ExecutionJob.Status.SUCCESS
                    and trig_close and (now - trig_close).total_seconds() > PROTECTION_OVERDUE_SECONDS):
                _alert_overdue_protection(plan, leg, desired_stage, trig_close, now)
                counts["overdue"] += 1

            soft_defer = False
            if attempted_this_stage:
                if job.status == ExecutionJob.Status.SUCCESS:
                    if job_result.get("already_closed"):
                        # The bridge found no OPEN position to modify. If the leg truly closed, close
                        # ingestion sets close_time and the leg is skipped next tick; until then DO
                        # NOT mark it protected — the SL was never moved. Fail-closed: never claim a
                        # protection we did not apply (a stale/mismatched ticket would otherwise mask
                        # a still-open, unprotected leg). A persistently-open leg here surfaces via
                        # the overdue WARN once its trigger close ages out.
                        counts["noop_closed"] += 1
                        continue
                    # A broker-verified SL move → advance the persisted stage (idempotent, monotonic).
                    leg.protection_stage = desired_stage
                    if leg.breakeven_applied_at is None:
                        leg.breakeven_applied_at = timezone.now()
                    leg.save(update_fields=["protection_stage", "breakeven_applied_at"])
                    counts["applied"] += 1
                    if desired_stage == STAGE_TP2_LOCKED:
                        counts["tp2_locked"] += 1
                    continue
                if job.status in active:
                    counts["inflight"] += 1
                    continue
                if job.status == ExecutionJob.Status.FAILED:
                    if job_result.get("retryable"):
                        # Soft, self-healing rejection (SL inside the broker stops/freeze band, e.g. the
                        # TP2-lock right after TP2 closed). Re-enqueue WITHOUT paging or counting it
                        # toward the retry cap — the position is moving toward TP3, away from this SL,
                        # so a later sweep lands it once the band clears.
                        soft_defer = True
                        counts["deferred"] += 1
                    elif leg.breakeven_attempts >= MAX_BREAKEVEN_ATTEMPTS:
                        _alert_protection_failure(plan, leg, desired_stage, job)
                        counts["alerted"] += 1
                        continue
                    # else: hard failure under the cap → fall through and retry
            # else (no job, or a job for an older stage) → fall through and enqueue

            # Risk guard — advance the SL only if strictly risk-reducing vs the current-stage baseline.
            baseline = _baseline_sl(plan, leg, trade)
            if target_sl is None or not _is_risk_reducing(plan.direction, target_sl, baseline):
                logger.info("protection: plan %s leg %s stage %s NOT risk-reducing (target=%s base=%s dir=%s); skip",
                            plan.id, leg.leg_index, desired_stage, target_sl, baseline, plan.direction)
                counts["skipped"] += 1
                continue

            new_job = _enqueue_modify(plan, leg, trade, target_sl, desired_stage)
            if new_job is None:
                counts["inflight" if _protection_inflight(trade, desired_stage) else "skipped"] += 1
                continue
            leg.breakeven_job = new_job
            # Bump the retry counter only for a genuine (re)attempt of this stage; a soft broker-band
            # deferral is expected and must not march the leg toward the CRITICAL retry-exhausted page.
            if soft_defer:
                pass  # attempts unchanged
            elif job_stage == desired_stage:
                leg.breakeven_attempts = leg.breakeven_attempts + 1
            else:
                leg.breakeven_attempts = 1
            leg.save(update_fields=["breakeven_job", "breakeven_attempts"])
            counts["enqueued"] += 1
            logger.info("protection: plan %s leg %s → %s MODIFY job %s sl=%s (attempt %s%s)",
                        plan.id, leg.leg_index, desired_stage, new_job.id, target_sl,
                        leg.breakeven_attempts, " soft-defer" if soft_defer else "")

    return counts
