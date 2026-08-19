from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q

from .models import (
    CustomerNotification,
    CustomerNotificationProjectionCursor,
    CustomerTelegramBinding,
)
from .services import enqueue_customer_notification


def _account_payload(account) -> dict:
    return {
        "account_kind": "demo" if account.is_demo else "trading",
        "account_number": str(account.account_number or "")[:64],
    }


def _trade_context(trade) -> dict:
    """Read-only strategy/leg projection from durable plan + assignment state."""
    strategy = "GuvFX"
    stop_loss = ""
    take_profit = ""
    try:
        from execution.models import SignalExecutionPlan
        from strategies.models import StrategyAssignment
        plan = None
        if trade.correlation_id:
            plan = SignalExecutionPlan.objects.filter(
                account_id=trade.account_id, correlation_id=trade.correlation_id,
            ).order_by("id").first()
        if plan is not None:
            stop_loss = str(plan.stop_loss or "")
            assignment = StrategyAssignment.objects.filter(
                account_id=trade.account_id, signal_source=plan.source,
            ).select_related("strategy").order_by("-id").first()
            strategy = assignment.strategy.name if assignment else plan.source.replace("_", " ").title()
            match = re.search(r"L(\d+)$", str(trade.comment or ""))
            if match:
                leg = plan.legs.filter(leg_index=int(match.group(1))).first()
                take_profit = str(getattr(leg, "take_profit", "") or "")
    except Exception:
        pass
    return {"strategy": strategy, "stop_loss": stop_loss, "take_profit": take_profit}


def _iso(value) -> str:
    return value.isoformat() if value is not None else ""


def _durable_leg_progress(outcome) -> dict:
    """Project the existing account-scoped per-leg close evidence without mutating execution state."""
    try:
        from execution.notifications.contracts import resolve_leg_evidence

        evidence = resolve_leg_evidence(outcome.correlation_id or "", outcome.trade)
    except Exception:
        return {}
    progress = evidence.get("progress") if isinstance(evidence, dict) else None
    legs = evidence.get("legs") if isinstance(evidence, dict) else None
    if not isinstance(progress, dict) or not isinstance(legs, list) or not legs:
        return {}
    realised = Decimal("0")
    has_realised = False
    for leg in legs:
        if not isinstance(leg, dict) or leg.get("status") != "CLOSED":
            continue
        try:
            realised += Decimal(str(leg.get("profit") or "0"))
            has_realised = True
        except (InvalidOperation, TypeError, ValueError):
            continue
    return {
        "strategy": str(evidence.get("strategy_display_name") or "")[:160],
        "progress_label": str(progress.get("label") or "")[:32],
        "progress_closed": int(progress.get("closed") or 0),
        "progress_total": int(progress.get("total") or 0),
        "progress_final": bool(progress.get("final")),
        "realised": str(realised) if has_realised else "",
    }


def enqueue_trade_opened(trade_id: int, *, raise_errors: bool = False):
    try:
        from trading.models import Trade
        trade = Trade.objects.select_related("account__user").get(pk=trade_id)
        context = _trade_context(trade)
        payload = {
            **_account_payload(trade.account), **context,
            "symbol": trade.symbol, "side": trade.side, "volume": trade.volume,
            "entry": trade.open_price, "occurred_at": _iso(trade.open_time),
        }
        return enqueue_customer_notification(
            user=trade.account.user, account=trade.account,
            event_type=CustomerNotification.EventType.TRADE_OPENED,
            source_object_type="trading.Trade", source_object_id=str(trade.pk),
            dedupe_key=f"customer-trade-opened:{trade.pk}", payload=payload,
            occurred_at=trade.open_time,
        )
    except Exception:
        if raise_errors:
            raise
        return None


def enqueue_trade_outcome(outcome_id: int, *, raise_errors: bool = False):
    try:
        from execution.models import TradeOutcomeRecord
        outcome = TradeOutcomeRecord.objects.select_related("trade__account__user").get(pk=outcome_id)
        trade = outcome.trade
        progress = _durable_leg_progress(outcome)
        event_type = (
            CustomerNotification.EventType.TRADE_CLOSED
            if not progress or progress["progress_final"]
            else CustomerNotification.EventType.TRADE_UPDATED
        )
        context = _trade_context(trade)
        if progress.get("strategy"):
            context["strategy"] = progress["strategy"]
        result = progress.get("realised") or str(outcome.net_pnl)
        aggregate_outcome = outcome.outcome
        if progress.get("realised"):
            realised = Decimal(progress["realised"])
            aggregate_outcome = (
                TradeOutcomeRecord.Outcome.WIN if realised > 0
                else TradeOutcomeRecord.Outcome.LOSS if realised < 0
                else TradeOutcomeRecord.Outcome.BREAKEVEN
            )
        payload = {
            **_account_payload(trade.account), **context,
            "symbol": trade.symbol, "side": trade.side,
            "result": result,
            "currency": trade.profit_currency or trade.account.account_currency or "USD",
            "outcome": aggregate_outcome,
            "progress_closed": progress.get("progress_closed", ""),
            "progress_total": progress.get("progress_total", ""),
            "occurred_at": _iso(trade.close_ingested_at or outcome.created_at),
        }
        if event_type == CustomerNotification.EventType.TRADE_UPDATED:
            payload["progress_label"] = (
                progress.get("progress_label", "")
                if outcome.outcome == TradeOutcomeRecord.Outcome.WIN else ""
            )
        return enqueue_customer_notification(
            user=trade.account.user, account=trade.account,
            event_type=event_type,
            source_object_type="execution.TradeOutcomeRecord", source_object_id=str(outcome.pk),
            dedupe_key=f"customer-trade-outcome:{outcome.pk}", payload=payload,
            occurred_at=trade.close_ingested_at or outcome.created_at,
        )
    except Exception:
        if raise_errors:
            raise
        return None


def enqueue_trade_closed(outcome_id: int, *, raise_errors: bool = False):
    """Backward-compatible name for the durable trade-outcome projection."""
    return enqueue_trade_outcome(outcome_id, raise_errors=raise_errors)


def enqueue_strategy_change(audit_id, *, raise_errors: bool = False):
    try:
        from core.models import AuditEvent
        from strategies.models import StrategyAssignment
        from trading.models import TradingAccount
        audit = AuditEvent.objects.select_related("user").get(pk=audit_id)
        mapping = {
            "SIGNAL_COPY_ARMED": CustomerNotification.EventType.STRATEGY_ENABLED,
            "SIGNAL_COPY_ENABLED": CustomerNotification.EventType.STRATEGY_ENABLED,
            "SIGNAL_COPY_DISABLED": CustomerNotification.EventType.STRATEGY_DISABLED,
        }
        event_type = mapping.get(audit.event_type)
        if not event_type or not audit.user_id or not audit.entity_id:
            return None
        account = TradingAccount.objects.filter(pk=audit.entity_id, user_id=audit.user_id).first()
        if account is None:
            return None
        metadata = audit.metadata if isinstance(audit.metadata, dict) else {}
        assignment = None
        assignment_id = metadata.get("assignment_id")
        if assignment_id:
            assignment = StrategyAssignment.objects.filter(
                pk=assignment_id, account=account,
            ).select_related("strategy").first()
        if assignment is None:
            assignment = StrategyAssignment.objects.filter(
                account=account,
            ).select_related("strategy").order_by("-is_active", "-id").first()
        strategy = assignment.strategy.name if assignment else "GuvFX"
        return enqueue_customer_notification(
            user=audit.user, account=account, event_type=event_type,
            source_object_type="core.AuditEvent", source_object_id=str(audit.pk),
            dedupe_key=f"customer-strategy-change:{audit.pk}",
            payload={**_account_payload(account), "strategy": strategy},
            occurred_at=audit.created_at,
        )
    except Exception:
        if raise_errors:
            raise
        return None


def enqueue_workspace_ready(transition_id: int, *, raise_errors: bool = False):
    try:
        from hosted_workspace.models import WorkspaceTransition
        transition = WorkspaceTransition.objects.select_related(
            "workspace__trading_account__user").get(pk=transition_id)
        if not transition.state_changed or transition.to_state != "EXECUTION_READY":
            return None
        account = transition.workspace.trading_account
        return enqueue_customer_notification(
            user=account.user, account=account,
            event_type=CustomerNotification.EventType.WORKSPACE_READY,
            source_object_type="hosted_workspace.WorkspaceTransition",
            source_object_id=str(transition.pk),
            dedupe_key=f"customer-workspace-ready:{transition.pk}",
            payload=_account_payload(account), occurred_at=transition.created_at,
        )
    except Exception:
        if raise_errors:
            raise
        return None


def enqueue_execution_problem(event_id: int, *, raise_errors: bool = False):
    try:
        from operational_events.models import OperationalEvent
        event = OperationalEvent.objects.select_related("account__user").get(pk=event_id)
        if not event.customer_visible or event.account_id is None or event.severity == "INFO":
            return None
        haystack = f"{event.event_type} {event.reason_code}".lower()
        if any(word in haystack for word in ("place", "order", "trade")):
            code = "trade_not_placed"
        elif any(word in haystack for word in ("workspace", "runtime", "terminal")):
            code = "workspace_attention"
        else:
            code = "temporarily_unavailable"
        hour = event.created_at.strftime("%Y%m%d%H")
        # One customer message per account/problem class/hour, even if infrastructure emits many rows.
        dedupe = f"customer-execution-problem:{event.account_id}:{event.event_type}:{event.reason_code}:{hour}"
        return enqueue_customer_notification(
            user=event.account.user, account=event.account,
            event_type=CustomerNotification.EventType.EXECUTION_PROBLEM,
            source_object_type="operational_events.OperationalEvent", source_object_id=str(event.pk),
            dedupe_key=dedupe, payload={"message_code": code}, occurred_at=event.created_at,
        )
    except Exception:
        if raise_errors:
            raise
        return None


def collect_customer_notification_events(*, limit: int = 1000) -> dict:
    """Backfill/reconcile durable facts independently from their originating services."""
    counts = {"trade_opened": 0, "trade_updated": 0, "trade_closed": 0, "strategy_changed": 0,
              "execution_problem": 0, "workspace_ready": 0, "errors": 0}
    first_connected = CustomerTelegramBinding.objects.filter(is_active=True).order_by(
        "connected_at").values_list("connected_at", flat=True).first()
    if first_connected is None:
        return counts

    from core.models import AuditEvent
    from execution.models import TradeOutcomeRecord
    from hosted_workspace.models import WorkspaceTransition
    from operational_events.models import OperationalEvent
    from trading.models import Trade

    sources = [
        ("trade_opened", Trade.objects.filter(created_at__gte=first_connected), enqueue_trade_opened),
        ("trade_outcomes", TradeOutcomeRecord.objects.filter(created_at__gte=first_connected), enqueue_trade_outcome),
        ("strategy_changed", AuditEvent.objects.filter(
            created_at__gte=first_connected,
            event_type__in=["SIGNAL_COPY_ARMED", "SIGNAL_COPY_ENABLED", "SIGNAL_COPY_DISABLED"],
        ), enqueue_strategy_change),
        ("workspace_ready", WorkspaceTransition.objects.filter(
            created_at__gte=first_connected, state_changed=True, to_state="EXECUTION_READY",
        ), enqueue_workspace_ready),
        ("execution_problem", OperationalEvent.objects.filter(
            created_at__gte=first_connected, customer_visible=True,
        ).exclude(severity="INFO"), enqueue_execution_problem),
    ]
    for key, queryset, handler in sources:
        try:
            with transaction.atomic():
                cursor, _ = CustomerNotificationProjectionCursor.objects.get_or_create(source=key)
                cursor = CustomerNotificationProjectionCursor.objects.select_for_update().get(pk=cursor.pk)
                pending = queryset
                if cursor.last_created_at is not None:
                    pending = pending.filter(
                        Q(created_at__gt=cursor.last_created_at)
                        | Q(created_at=cursor.last_created_at, id__gt=cursor.last_object_id)
                    )
                objects = list(pending.order_by("created_at", "id").values_list(
                    "id", "created_at",
                )[:limit])
                for object_id, created_at in objects:
                    row = handler(object_id, raise_errors=True)
                    if row is not None:
                        if key == "trade_outcomes":
                            outcome_key = (
                                "trade_updated"
                                if row.event_type == CustomerNotification.EventType.TRADE_UPDATED
                                else "trade_closed"
                            )
                            counts[outcome_key] += 1
                        else:
                            counts[key] += 1
                    cursor.last_created_at = created_at
                    cursor.last_object_id = str(object_id)
                if objects:
                    cursor.save(update_fields=["last_created_at", "last_object_id", "updated_at"])
        except Exception:
            # This is an isolated projection transaction. Roll back the cursor and batch so the
            # same durable source event is retried; never raise into execution or another source.
            counts["errors"] += 1
    return counts
