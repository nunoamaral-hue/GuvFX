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

STAGE-2 PHYSICAL CLEANUP (2026-09-29): logical removal above is authoritative and commits FIRST; the physical host
teardown (stop observer task + tenant bridge/watchdog + port, terminate the tenant's own terminal64, end the RDP
session, remove the RemoteApp/AppLocker rule, delete runtime/tenant dirs, disable the Windows identity) is a
SEPARATE, asynchronous, retryable Stage-2 driven by the hosted cron (``hosted_workspace.decommission.
run_workspace_cleanup``) via the governed signed-executor primitives. ``remove_account`` enqueues it by flipping
``HostedMt5Workspace.cleanup_state`` to PENDING inside the tombstone transaction. INVARIANT: a Stage-2 failure
NEVER un-tombstones, restores credentials or restores entitlement consumption — the account stays logically
removed and cleanup simply retries. Provider-B (hosted workspace) only; the BETA path uses the slot release below.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError


class AccountRemovedError(APIException):
    """A LIVE operation was attempted on a removed (tombstoned) account. 409 so the member sees a clear
    already-removed state rather than a generic validation error."""
    status_code = 409
    default_detail = "This account has been removed. Add it again to trade."
    default_code = "account_removed"


def account_is_removed(account) -> bool:
    """True iff the account has been logically removed (tombstoned). ``disconnected_at`` is the authoritative,
    estate-wide execution kill — every LIVE-op entrypoint must fail closed on it (a removed account's credentials
    are destroyed and its endpoint retired, so it must not trade, arm, mint an MT5 session, re-validate or
    re-credential without an explicit re-add). History/idempotent/analytics paths intentionally do NOT call this."""
    return getattr(account, "disconnected_at", None) is not None


def guard_live_op(account) -> None:
    """Shared fail-closed guard: reject a LIVE operation on a removed account with a customer-safe 409. Call at
    every live-op entrypoint (Start, Open MT5 mint / delivery authority, View-MT5 explicit resolve, strategy arm,
    connection test, credential replace). NEVER call from history/idempotent surfaces (detail get, remove,
    bc_status, bc_validation_history, trade-history, analytics) which must keep resolving tombstoned rows."""
    if account_is_removed(account):
        raise AccountRemovedError()


def open_position_count(account) -> int:
    """Number of still-OPEN broker positions ingested for this account (close_time NULL). Used by the removal
    gate; a live position must be closed by the member before the account can be removed."""
    from trading.models import Trade
    return Trade.objects.filter(account=account, close_time__isnull=True).count()


def _release_beta_runtime_slot(account, *, actor: str) -> None:
    """Release the account's BETA runtime capacity slot (HELD -> STOPPED, a DB-only state change) so removal
    frees the per-user AND global beta-pool slots — not just the entitlement/owned-account cap. Without this a
    removed account's runtime stays HELD and blocks the member's replacement (per_user_runtime_cap) and leaks a
    global pool slot. BETA-only + best-effort; a no-op for Provider-B accounts (no AccountRuntime) and for a
    runtime already released. The physical MT5/RDP process teardown legitimately remains a host follow-up."""
    try:
        from terminal_provisioning.models import AccountRuntime
        from terminal_provisioning.beta_capacity import release_beta_slot
        rt = AccountRuntime.objects.filter(
            trading_account=account, cohort=AccountRuntime.Cohort.BETA).first()
        if rt is not None:
            release_beta_slot(rt, reason="account_removed")   # HELD -> STOPPED; frees per-user + global slot
    except Exception:  # noqa: BLE001 — never let slot release block the removal (tombstone is authoritative)
        pass


def remove_account(account, *, actor: str = "", request=None) -> dict:
    """Remove (decommission) a broker account, retaining history. Fail-closed on open positions. Idempotent
    and race-safe (row-locked re-check)."""
    from execution.models import ExecutionJob
    from trading.broker_connectivity import disconnect_account
    from trading.models import TradingAccount

    if account.disconnected_at is not None:
        return {"removed": True, "already": True, "open_positions": 0}

    # 1) OPEN-POSITION GATE (fail-closed) — never orphan a live position.
    open_positions = open_position_count(account)
    if open_positions:
        raise ValidationError({"detail": (
            "This account still has open trades. Close them in MT5 before removing the account.")})

    ws = getattr(account, "hosted_workspace", None)
    with transaction.atomic():
        # Row-lock + re-check inside the txn so two concurrent removes don't both run the teardown (idempotent
        # final state either way, but this avoids a duplicate credential-destroy audit / double work).
        locked = TradingAccount.objects.select_for_update().get(pk=account.pk)
        if locked.disconnected_at is not None:
            return {"removed": True, "already": True, "open_positions": 0}
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
        # 5b) Enqueue STAGE-2 physical host teardown (async, retryable). Set INSIDE the txn so the cleanup marker
        # commits atomically with the tombstone: there can never be a tombstone without a queued cleanup, nor a
        # queued cleanup without a tombstone. The physical op is deferred to the hosted cron
        # (hosted_workspace.decommission.run_workspace_cleanup); a failure there NEVER un-tombstones. A queryset
        # .update() avoids the model's immutable-binding refetch and touches only the cleanup_* columns.
        if ws is not None:
            try:
                from hosted_workspace.decommission import CLEANUP_PENDING
                from hosted_workspace.models import HostedMt5Workspace
                (HostedMt5Workspace.objects.filter(pk=ws.pk)
                 .update(cleanup_state=CLEANUP_PENDING, cleanup_attempts=0,
                         cleanup_next_retry_at=timezone.now(), cleanup_last_reason="queued_on_remove"))
            except Exception:  # noqa: BLE001 — never let cleanup enqueue block the removal (tombstone authoritative)
                pass
        # 6) Release the BETA runtime capacity slot so the member can immediately add a replacement.
        _release_beta_runtime_slot(account, actor=actor or "account_removal")

    return {"removed": True, "already": False, "open_positions": 0}
