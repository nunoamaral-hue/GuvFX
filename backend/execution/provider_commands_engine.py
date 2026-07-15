"""WS-E — provider trade-management command ENGINE (enqueue-only, gated, source-isolated).

Acts on recorded ``ProviderCommand`` rows: correlate a follow-up to exactly ONE active plan of the
SAME source, then enqueue MODIFY_POSITION / CLOSE_TRADE jobs (or void a not-yet-filled plan). Mirrors
the breakeven sweep: the backend only ENQUEUES jobs (holds no bridge creds); the worker executes.

SAFETY (every one is load-bearing):
* **Gated** — inert unless ``PROVIDER_COMMANDS_ENABLED`` (env) AND the source's
  ``SignalSourceConfig.command_engine_enabled`` are BOTH on (deploy-dark; arm is Nuno-gated/Red).
* **Deterministic correlation** — reply linkage only: ``command.reply_to_message_id == plan.message_id``
  filtered to ``source=command.provider.slug``. Exactly-one active plan → act; zero or >1 → FAIL-CLOSED
  (record AMBIGUOUS/REJECTED, alert, enqueue NOTHING). We never guess a plan from symbol/direction.
* **Source isolation (4 layers)** — (1) every plan query filters ``source=provider.slug``; (2) a hard
  ``plan.source == provider.slug`` assertion before any enqueue; (3) act only for opted-in sources;
  (4) legs correlated by ``plan.id`` (``WAY{plan.id}L{leg}``), never the "WAY" prefix. A TI command can
  never touch a Wayond plan.
* **Never increase risk** — a Move-SL enqueues only when strictly risk-reducing vs the plan SL (reuses
  the breakeven guard); the bridge hard-refuses a widen too.
* **Idempotent** — a command is processed once (``processed`` flag); executors act only on currently
  OPEN legs; the bridge modify/close are themselves idempotent/safe on an already-done position.
* **No public spam** — the engine posts nothing to Telegram; stakeholder cards remain tied to WIN TP
  closes; only operator alerts (private) are raised.
"""
from __future__ import annotations

import logging
import os

from django.db import transaction
from django.utils import timezone

from execution.breakeven import _is_risk_reducing, _leg_trade, _to_decimal, _windows_username
from execution.models import ExecutionJob, SignalExecutionPlan, SignalSourceConfig

logger = logging.getLogger("guvfx.execution.provider_commands")

DEFAULT_LIMIT = 200
_ACTIVE = (SignalExecutionPlan.Status.PLANNED, SignalExecutionPlan.Status.PROMOTED)
_ORDER_TYPES = (ExecutionJob.JobType.PLACE_ORDER, ExecutionJob.JobType.PLACE_TEST_ORDER)


def provider_commands_enabled() -> bool:
    """Master arm switch (env). Inert unless explicitly enabled AND the source opts in."""
    return os.getenv("PROVIDER_COMMANDS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- correlation
def resolve_target_plan(command):
    """Return ``(plan_or_None, reason)``. Reply linkage only (deterministic, source-scoped).
    Exactly-one active plan → act; zero → ``no_reply_match``; >1 → ``ambiguous_reply`` (fail-closed)."""
    slug = command.provider.slug
    if not command.reply_to_message_id:
        return None, "no_reply_metadata"
    qs = SignalExecutionPlan.objects.filter(
        source=slug, message_id=command.reply_to_message_id, status__in=_ACTIVE)
    n = qs.count()
    if n == 1:
        return qs.first(), "reply"
    if n > 1:
        return None, "ambiguous_reply"
    return None, "no_reply_match"


# ---------------------------------------------------------------- executors
def _open_legs(plan):
    """[(leg, open_trade)] for each leg whose position is currently OPEN (correlated by plan.id)."""
    out = []
    for leg in plan.legs.order_by("leg_index"):
        t = _leg_trade(plan, leg)
        if t is not None and t.close_time is None:
            out.append((leg, t))
    return out


def _enqueue_modify(plan, leg, trade, sl, reason):
    username = _windows_username(plan.account)
    if not username or not trade.ticket:
        return None
    payload = {"windows_username": username, "ticket": trade.ticket, "symbol": plan.symbol,
               "sl": float(sl), "plan_id": plan.id, "leg_index": leg.leg_index,
               "signal_source": plan.source, "correlation_id": plan.correlation_id or "", "reason": reason}
    tp = _to_decimal(leg.take_profit)
    if tp is not None:
        payload["tp"] = float(tp)
    return ExecutionJob.objects.create(
        job_type=ExecutionJob.JobType.MODIFY_POSITION, account_id=plan.account_id,
        terminal_node_id=plan.account.terminal_node_id, status=ExecutionJob.Status.PENDING, payload=payload)


def _enqueue_close(plan, leg, trade):
    username = _windows_username(plan.account)
    if not username or not trade.ticket:
        return None
    return ExecutionJob.objects.create(
        job_type=ExecutionJob.JobType.CLOSE_TRADE, account_id=plan.account_id,
        terminal_node_id=plan.account.terminal_node_id, status=ExecutionJob.Status.PENDING,
        payload={"windows_username": username, "ticket": trade.ticket, "symbol": plan.symbol,
                 "plan_id": plan.id, "leg_index": leg.leg_index, "signal_source": plan.source,
                 "correlation_id": plan.correlation_id or "", "reason": "provider_close"})


def _cancel_pending_order_jobs(plan):
    """Fail still-unclaimed PENDING order-opening jobs for this plan's legs (cancel before fill).
    Under ``select_for_update`` so it can't race a worker claim mid-flight."""
    cancelled = []
    now = timezone.now()
    with transaction.atomic():
        job_ids = [lid for lid in plan.legs.values_list("execution_job_id", flat=True) if lid]
        jobs = (ExecutionJob.objects.select_for_update(skip_locked=True)
                .filter(id__in=job_ids, status=ExecutionJob.Status.PENDING, job_type__in=_ORDER_TYPES))
        for j in jobs:
            j.status = ExecutionJob.Status.FAILED
            j.error_message = "cancelled by provider command"
            j.finished_at = now
            j.save(update_fields=["status", "error_message", "finished_at"])
            cancelled.append(j.id)
    return cancelled


def _apply_command(command, plan):
    """Enqueue the jobs for one command against its (already source-asserted) plan. Returns a
    result dict; a ``rejected`` key means nothing was enqueued."""
    ct = command.command_type
    res = {"plan_id": plan.id, "jobs": [], "skipped": [], "cancelled_jobs": []}

    if ct in ("MOVE_SL_BE", "MOVE_SL_PRICE"):
        if ct == "MOVE_SL_PRICE":
            price = _to_decimal((command.args or {}).get("price"))
            if price is None or price <= 0:
                return {"rejected": "invalid_price"}
        old_sl = _to_decimal(plan.stop_loss)
        for leg, trade in _open_legs(plan):
            new_sl = (_to_decimal(trade.open_price) if ct == "MOVE_SL_BE"
                      else _to_decimal((command.args or {}).get("price")))
            if new_sl is None or not _is_risk_reducing(plan.direction, new_sl, old_sl):
                res["skipped"].append(leg.leg_index)  # never widen (default posture)
                continue
            j = _enqueue_modify(plan, leg, trade, new_sl,
                                "provider_move_sl_be" if ct == "MOVE_SL_BE" else "provider_move_sl_price")
            if j:
                res["jobs"].append(j.id)
        return res

    if ct == "CLOSE_ALL":
        for leg, trade in _open_legs(plan):
            j = _enqueue_close(plan, leg, trade)
            if j:
                res["jobs"].append(j.id)
        res["cancelled_jobs"] = _cancel_pending_order_jobs(plan)  # void unfilled legs too
        return res

    if ct == "CLOSE_LEG":
        n = int((command.args or {}).get("leg_index") or 0)
        for leg, trade in _open_legs(plan):
            if leg.leg_index == n:
                j = _enqueue_close(plan, leg, trade)
                if j:
                    res["jobs"].append(j.id)
        if not res["jobs"]:
            res["skipped"].append(f"leg{n}_not_open")
        return res

    if ct == "CANCEL":
        if plan.status == SignalExecutionPlan.Status.PLANNED:
            updated = SignalExecutionPlan.objects.filter(
                id=plan.id, status=SignalExecutionPlan.Status.PLANNED
            ).update(status=SignalExecutionPlan.Status.VOIDED)
            res["voided_plan"] = bool(updated)
            return res
        # PROMOTED: this is MARKET execution — cancel any still-unclaimed jobs and CLOSE filled legs.
        res["cancelled_jobs"] = _cancel_pending_order_jobs(plan)
        for leg, trade in _open_legs(plan):
            j = _enqueue_close(plan, leg, trade)
            if j:
                res["jobs"].append(j.id)
        return res

    return {"rejected": "non_actionable"}


def _alert(command, reason):
    """One deduped WARN when a command needs operator attention (ambiguous / no match)."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup = f"provider_command_unresolved:cmd:{command.id}"
        if AlertEvent.objects.filter(dedup_key=dedup, status=AlertEvent.Status.OPEN).exists():
            return
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.WARN, component=Component.EXECUTION_PIPELINE,
            title=f"Provider command not applied ({reason})",
            body=(f"{command.provider.slug} command {command.command_type} (msg {command.message_id}) "
                  f"could not be applied: {reason}. Raw: {(command.raw_text or '')[:160]}"),
            dedup_key=dedup, status=AlertEvent.Status.OPEN,
            detail={"command_id": command.id, "reason": reason, "type": command.command_type})
    except Exception:  # pragma: no cover - alerting is best-effort
        logger.exception("provider_commands: alert failed cmd=%s", command.id)


def _finalize(command, status, result):
    from signal_intake.models import ProviderCommand
    ProviderCommand.objects.filter(id=command.id).update(
        status=status, processed=True, result=result, processed_at=timezone.now())


def apply_provider_commands(*, limit: int = DEFAULT_LIMIT) -> dict:
    """Monitor-chain step. Process PENDING provider commands for opted-in sources. Enqueue-only,
    idempotent, source-isolated. Inert unless armed."""
    if not provider_commands_enabled():
        return {"enabled": False}
    from signal_intake.models import ProviderCommand
    counts = {"enabled": True, "applied": 0, "rejected": 0, "ambiguous": 0, "skipped_source": 0}

    enabled_sources = set(
        SignalSourceConfig.objects.filter(command_engine_enabled=True).values_list("source", flat=True))
    pending = (ProviderCommand.objects.filter(status=ProviderCommand.Status.PENDING, processed=False)
               .select_related("provider").order_by("id")[:limit])
    for cmd in pending:
        slug = cmd.provider.slug
        if slug not in enabled_sources:
            counts["skipped_source"] += 1
            continue  # source not opted-in → leave PENDING (a later arm will pick it up)
        plan, reason = resolve_target_plan(cmd)
        if plan is None:
            ambiguous = reason == "ambiguous_reply"
            _finalize(cmd, ProviderCommand.Status.AMBIGUOUS if ambiguous else ProviderCommand.Status.REJECTED,
                      {"reason": reason})
            _alert(cmd, reason)
            counts["ambiguous" if ambiguous else "rejected"] += 1
            continue
        # Layer-2 source-isolation assertion (belt-and-braces beyond the query filter).
        if plan.source != slug:
            _finalize(cmd, ProviderCommand.Status.REJECTED,
                      {"reason": "source_mismatch", "plan_source": plan.source})
            counts["rejected"] += 1
            continue
        try:
            result = _apply_command(cmd, plan)
        except Exception as exc:
            logger.exception("provider_commands: apply failed cmd=%s", cmd.id)
            _finalize(cmd, ProviderCommand.Status.HELD, {"error": type(exc).__name__})
            counts["rejected"] += 1
            continue
        if result.get("rejected"):
            _finalize(cmd, ProviderCommand.Status.REJECTED, result)
            counts["rejected"] += 1
        else:
            _finalize(cmd, ProviderCommand.Status.APPLIED, result)
            counts["applied"] += 1
            logger.info("provider_commands: APPLIED %s cmd=%s plan=%s -> %s",
                        cmd.command_type, cmd.id, plan.id, result)
    return counts
