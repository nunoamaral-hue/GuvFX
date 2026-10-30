"""Periodic READ-ONLY deal-history ingestion for MONITORED accounts.

The closed-trade history that feeds the dashboard's realized metrics (Net P&L, Win Rate,
Profit Factor, Max Drawdown, Expectancy, closed-trade count, daily realized P&L) is
ingested by the ``SYNC_POSITIONS`` worker: it reads each account's OWN per-tenant bridge
(``GET /mt5/snapshots/deals`` — a fixed 90-day window) and upserts one
:class:`trading.models.Trade` per closed position via ``build_positions_from_deals``
(identity-firewalled, idempotent; deposits/withdrawals/credits carry no ``position_id`` and
are never entered as trades).

Historically a ``SYNC_POSITIONS`` was ENQUEUED for an account only while it held an OPEN
execution plan (``execution.breakeven._ensure_position_sync``) or immediately after a
placement (the auto-sync). A MONITORING-only account — connected and shown on the dashboard
but with no GuvFX execution — therefore never received a sync, so its real broker closed
trades were never ingested. The dashboard then showed "no closed-trade history" / N/A metrics
even when the broker held trades. Observed on account 46 (2026-10-29): the broker held 48
closed trades (+$163.92 net) while GuvFX had persisted 0 — triggering one sync ingested all
48 and the metrics populated truthfully.

This module is the durable trigger that closes that gap. It enqueues one read-only
``SYNC_POSITIONS`` per MONITORED account on a schedule, reusing the existing, proven
ingestion machinery. It creates **no** orders and changes **no** execution semantics.

Guarantees:
- **Scoped** to accounts that are connected AND own a READY per-tenant read endpoint — exactly
  the set whose ``/mt5/snapshots/deals`` is reachable. An account without a READY endpoint would
  fail closed in the worker anyway, so it is not enqueued.
- **Idempotent**: an account that already has a PENDING/RUNNING sync is skipped (never piled up);
  ``upsert_trades`` itself is replay-safe (``get_or_create`` on ``(account, ticket)``).
- **Fail-closed per account**: no resolvable Windows identity → skipped, never guessed.
- **Quiet backoff**: an account whose last monitored probe FAILED within ``failure_cooldown_minutes``
  is skipped, so an account whose bridge cannot currently serve the deals read (e.g. a stale bridge
  still refusing a live account) is re-probed at most once per cooldown instead of every cycle — and
  auto-recovers within one cooldown once its bridge is fixed.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Default re-probe backoff for an account whose last monitored sync FAILED (minutes).
DEFAULT_FAILURE_COOLDOWN_MIN = 180


def monitored_account_ids() -> list[int]:
    """Account ids the dashboard monitors and that have a READY per-tenant read bridge.

    = connected (``disconnected_at IS NULL``) AND owning a READY
    :class:`execution.models.HostedExecutionEndpoint` with a non-empty ``base_url``. This is
    precisely the set whose per-tenant ``/mt5/snapshots/deals`` is resolvable
    (``resolve_account_snapshot_base``); it deliberately excludes accounts with no READY endpoint
    (they would fail closed in the worker — enqueuing them would only churn)."""
    from trading.models import TradingAccount
    from execution.models import HostedExecutionEndpoint

    ready_ids = set(
        HostedExecutionEndpoint.objects
        .filter(state=HostedExecutionEndpoint.State.READY)
        .exclude(base_url="")
        .values_list("trading_account_id", flat=True)
    )
    if not ready_ids:
        return []
    return list(
        TradingAccount.objects
        .filter(disconnected_at__isnull=True, id__in=ready_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )


def enqueue_monitored_deal_syncs(account_ids=None,
                                 failure_cooldown_minutes: int = DEFAULT_FAILURE_COOLDOWN_MIN) -> dict:
    """Enqueue one read-only ``SYNC_POSITIONS`` per monitored account lacking an in-flight sync.

    ``account_ids`` overrides the monitored set (used by tests / a targeted re-sync); otherwise
    :func:`monitored_account_ids` is used. Returns a summary dict. Best-effort: a per-account error
    is logged and does not abort the sweep. Creates no orders.
    """
    from datetime import timedelta

    from django.utils import timezone

    from trading.models import TradingAccount
    from execution.models import ExecutionJob
    # Reuse the single, hosted-aware Windows-identity resolver so monitored ingestion and the
    # breakeven freshness sweep can never diverge on how an account's sync identity is derived.
    from execution.breakeven import _sync_windows_username

    ids = list(account_ids) if account_ids is not None else monitored_account_ids()
    cutoff = (timezone.now() - timedelta(minutes=failure_cooldown_minutes)) if failure_cooldown_minutes else None

    queued = 0
    skipped_in_flight = 0
    skipped_no_identity = 0
    skipped_cooldown = 0
    missing = 0

    for account_id in ids:
        try:
            in_flight = ExecutionJob.objects.filter(
                account_id=account_id,
                job_type=ExecutionJob.JobType.SYNC_POSITIONS,
                status__in=[ExecutionJob.Status.PENDING, ExecutionJob.Status.RUNNING],
            ).exists()
            if in_flight:
                skipped_in_flight += 1
                continue

            if cutoff is not None:
                # Back off ONLY when the MOST RECENT monitored probe for this account FAILED within the window.
                # A subsequent successful identity-verified sync (a recovered bridge) therefore clears the backoff
                # IMMEDIATELY, so a recovered account resumes normal scheduled coverage without waiting out the full
                # window (ADR-0067). Account-scoped: another account's failure never suppresses this one, and only
                # OUR OWN monitored probes count (an unrelated execution sync never affects monitored ingestion).
                latest = (ExecutionJob.objects
                          .filter(account_id=account_id,
                                  job_type=ExecutionJob.JobType.SYNC_POSITIONS,
                                  payload__monitored_ingest=True)
                          .order_by("-created_at")
                          .values("status", "created_at").first())
                if (latest is not None
                        and latest["status"] == ExecutionJob.Status.FAILED
                        and latest["created_at"] >= cutoff):
                    skipped_cooldown += 1
                    continue

            account = (TradingAccount.objects
                       .filter(id=account_id)
                       .select_related("mt5_instance").first())
            if account is None:
                missing += 1
                continue

            username = _sync_windows_username(account)
            if not username:
                skipped_no_identity += 1
                continue

            ExecutionJob.objects.create(
                job_type=ExecutionJob.JobType.SYNC_POSITIONS,
                account_id=account_id,
                terminal_node_id=account.terminal_node_id,
                status=ExecutionJob.Status.PENDING,
                payload={"windows_username": username, "monitored_ingest": True},
            )
            queued += 1
        except Exception:  # pragma: no cover - defensive; the sweep must survive one bad account
            logger.exception("monitored_ingest: failed to enqueue sync for account %s", account_id)

    summary = {
        "considered": len(ids),
        "queued": queued,
        "skipped_in_flight": skipped_in_flight,
        "skipped_cooldown": skipped_cooldown,
        "skipped_no_identity": skipped_no_identity,
        "missing": missing,
    }
    logger.info("monitored_ingest: %s", summary)
    return summary
