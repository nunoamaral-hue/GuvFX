from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import AuditEvent
from execution.models import TradeOutcomeRecord
from hosted_workspace.models import WorkspaceTransition
from operational_events.models import OperationalEvent
from trading.models import TradingAccount

from .event_sources import (
    enqueue_execution_problem,
    enqueue_strategy_change,
    enqueue_trade_outcome,
    enqueue_workspace_ready,
)
from .services import fulfill_pending_workspace_readiness


def _after_commit(fn, pk):
    def callback():
        try:
            fn(pk)
        except Exception:
            # Belt-and-braces: even test runners or older Django versions that do not honour
            # robust callbacks cannot let notification observation escape into the source path.
            return None
    transaction.on_commit(callback, robust=True)


@receiver(post_save, sender=TradeOutcomeRecord, dispatch_uid="customer_notify_trade_outcome")
def trade_outcome_saved(sender, instance, created, **kwargs):
    if created:
        _after_commit(enqueue_trade_outcome, instance.pk)


@receiver(post_save, sender=AuditEvent, dispatch_uid="customer_notify_strategy_change")
def strategy_audit_saved(sender, instance, created, **kwargs):
    if created and instance.event_type in {
        "SIGNAL_COPY_ARMED", "SIGNAL_COPY_ENABLED", "SIGNAL_COPY_DISABLED",
    }:
        _after_commit(enqueue_strategy_change, instance.pk)


@receiver(post_save, sender=WorkspaceTransition, dispatch_uid="customer_notify_workspace_ready")
def workspace_transition_saved(sender, instance, created, **kwargs):
    if created and instance.state_changed and instance.to_state == "EXECUTION_READY":
        _after_commit(enqueue_workspace_ready, instance.pk)


@receiver(post_save, sender=TradingAccount, dispatch_uid="customer_notify_workspace_confirmed")
def workspace_account_confirmed(sender, instance, **kwargs):
    """The customer-facing ready milestone may precede canonical EXECUTION_READY."""
    if instance.workspace_confirmed_at is None:
        return
    try:
        workspace_id = instance.hosted_workspace.id
    except Exception:
        return
    transaction.on_commit(
        lambda: fulfill_pending_workspace_readiness(workspace_id=workspace_id), robust=True,
    )


@receiver(post_save, sender=OperationalEvent, dispatch_uid="customer_notify_execution_problem")
def operational_event_saved(sender, instance, created, **kwargs):
    if created and instance.customer_visible and instance.severity != "INFO":
        _after_commit(enqueue_execution_problem, instance.pk)
