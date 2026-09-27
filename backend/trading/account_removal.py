"""trading.account_removal — member-facing "Remove account" = a history-RETAINING decommission (tombstone).

The member action reuses the certified ``disconnect_account`` tombstone (credential destroyed + is_active=False
+ disconnected_at set; the row and all immutable Trade/attribution history are RETAINED — NEVER a hard delete),
and additionally de-eligibilises execution (disarm + suppress auto-arm) and releases the runtime endpoint, so a
removed account:
  * stops all NEW automated execution (disconnected_at is the estate-wide execution kill: readiness, the arm
    gate and managed_start all fail closed on it; auto_router fan-out filters is_active=True);
  * releases the entitlement slot immediately (every owned/active count honors disconnected_at);
  * is skipped by the observation cron (no ongoing polling);
  * retains full trading/audit/attribution history for dashboards and forensics.

FAIL-CLOSED: refuses while the account has OPEN broker positions (never orphans a live trade; the member must
close them first). Idempotent. Owner-scoping is enforced by the caller (the viewset's owner-scoped get_object).

SCOPE NOTE (host follow-up): the physical MT5 process / RDP session teardown for a Provider-B terminal needs a
host primitive that does not yet exist (host_agent_dispatch has no TERMINATE_TERMINAL/END_SESSION). Until that
lands the tenant terminal may keep running on the host after removal, but it is fully removed from execution
eligibility, observation and the entitlement cap; the residual is host CPU/RAM only, tracked as a host op.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError


def open_position_count(account) -> int:
    """Number of still-OPEN broker positions ingested for this account (close_time NULL). Used by the removal
    gate; a live position must be closed by the member before the account can be removed."""
    from trading.models import Trade
    return Trade.objects.filter(account=account, close_time__isnull=True).count()


def remove_account(account, *, actor: str = "", request=None) -> dict:
    """Remove (decommission) a broker account, retaining history. Fail-closed on open positions. Idempotent."""
    from execution.models import ExecutionJob
    from trading.broker_connectivity import disconnect_account

    if account.disconnected_at is not None:
        return {"removed": True, "already": True, "open_positions": 0}

    # 1) OPEN-POSITION GATE (fail-closed) — never orphan a live position.
    open_positions = open_position_count(account)
    if open_positions:
        raise ValidationError({"detail": (
            "This account still has open trades. Close them in MT5 before removing the account.")})

    ws = getattr(account, "hosted_workspace", None)
    with transaction.atomic():
        # 2) Cancel any PENDING order-opening jobs so nothing already queued dispatches after removal.
        (ExecutionJob.objects
         .filter(account=account, status=ExecutionJob.Status.PENDING,
                 job_type__in=[ExecutionJob.JobType.PLACE_ORDER, ExecutionJob.JobType.OPEN_TRADE])
         .update(status=ExecutionJob.Status.FAILED, error_message="account_removed",
                 finished_at=timezone.now()))
        # 3) De-eligibilise execution (disarm + suppress auto-arm) for a hosted workspace — best-effort; the
        # tombstone below is the authoritative execution kill regardless.
        if ws is not None:
            try:
                from execution.hosted_provisioning import disarm_hosted_workspace_execution
                disarm_hosted_workspace_execution(account, actor=actor or "account_removal", request=request)
            except Exception:  # noqa: BLE001 — never let de-arm failure block the removal (tombstone still runs)
                pass
        # 4) TOMBSTONE (credential destroy + is_active=False + disconnected_at) — the history-retaining core.
        disconnect_account(account, actor=actor or "account_removal", request=request)
        # 5) Release the runtime endpoint (idempotent, best-effort).
        if ws is not None:
            try:
                from execution.endpoint_service import retire_endpoint
                retire_endpoint(ws, actor=actor or "account_removal")
            except Exception:  # noqa: BLE001
                pass

    return {"removed": True, "already": False, "open_positions": 0}
