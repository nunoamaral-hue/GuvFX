"""
Reconciliation runner — orchestrates per-account reconciliation.

Produces a ``reconciliation_run_id``, scans accounts, calls the
reconciliation service, and emits AuditEvents for run start/completion.

Designed to be invoked from:
- management command
- scheduled job
- background worker

**Strict prohibition**: this module must never mutate Trade,
TradingAccount, or any other financial record.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.audit import log_event
from reconciliation.reconciliation_service import reconcile_account
from trading.models import TradingAccount

logger = logging.getLogger(__name__)


def run_reconciliation(
    accounts: list[TradingAccount] | None = None,
    mt5_data_by_account: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """
    Execute a full reconciliation run.

    Args:
        accounts: Specific accounts to reconcile.  If ``None``, all active
            accounts are scanned.
        mt5_data_by_account: Mapping of ``account.id`` → list of MT5 trade
            dicts.  Each dict must contain at least a ``"ticket"`` key.
            Accounts not present in this mapping are skipped (no MT5 data
            available).

    Returns:
        Summary dict:
        {
            "reconciliation_run_id": "<uuid>",
            "accounts_scanned": <int>,
            "accounts_skipped": <int>,
            "total_discrepancies": <int>,
            "discrepancies_by_severity": {"INFO": n, "WARNING": n, "CRITICAL": n},
        }
    """
    run_id = str(uuid.uuid4())
    mt5_data_by_account = mt5_data_by_account or {}

    if accounts is None:
        accounts = list(TradingAccount.objects.filter(is_active=True))

    # --- Audit: run start ---
    _emit_run_start(run_id, len(accounts))

    accounts_scanned = 0
    accounts_skipped = 0
    total_discrepancies = 0
    severity_counts: dict[str, int] = {
        "INFO": 0,
        "WARNING": 0,
        "CRITICAL": 0,
    }

    for account in accounts:
        mt5_trades = mt5_data_by_account.get(account.id)
        if mt5_trades is None:
            # No MT5 data available for this account — skip.
            accounts_skipped += 1
            logger.info(
                "Reconciliation run=%s: skipping account %s (no MT5 data)",
                run_id,
                account.id,
            )
            continue

        accounts_scanned += 1

        try:
            events = reconcile_account(
                account=account,
                mt5_trades=mt5_trades,
                reconciliation_run_id=run_id,
            )
        except Exception:
            logger.exception(
                "Reconciliation run=%s: error processing account %s",
                run_id,
                account.id,
            )
            continue

        total_discrepancies += len(events)
        for ev in events:
            severity_counts[ev.severity] = severity_counts.get(ev.severity, 0) + 1

    # --- Audit: run completion ---
    summary = {
        "reconciliation_run_id": run_id,
        "accounts_scanned": accounts_scanned,
        "accounts_skipped": accounts_skipped,
        "total_discrepancies": total_discrepancies,
        "discrepancies_by_severity": severity_counts,
    }
    _emit_run_completion(run_id, summary)

    logger.info(
        "Reconciliation run=%s completed: scanned=%d skipped=%d discrepancies=%d",
        run_id,
        accounts_scanned,
        accounts_skipped,
        total_discrepancies,
    )

    return summary


# ------------------------------------------------------------------
# Audit helpers
# ------------------------------------------------------------------


def _emit_run_start(run_id: str, account_count: int) -> None:
    """Emit AuditEvent for reconciliation run start."""
    log_event(
        request=None,
        event_type="RECONCILIATION_RUN_STARTED",
        severity="INFO",
        entity_type="reconciliation_run",
        entity_id=run_id,
        metadata={
            "reconciliation_run_id": run_id,
            "account_count": account_count,
        },
    )


def _emit_run_completion(run_id: str, summary: dict[str, Any]) -> None:
    """Emit AuditEvent for reconciliation run completion."""
    log_event(
        request=None,
        event_type="RECONCILIATION_RUN_COMPLETED",
        severity="INFO",
        entity_type="reconciliation_run",
        entity_id=run_id,
        metadata={
            "reconciliation_run_id": run_id,
            "accounts_scanned": summary.get("accounts_scanned", 0),
            "accounts_skipped": summary.get("accounts_skipped", 0),
            "total_discrepancies": summary.get("total_discrepancies", 0),
            "discrepancies_by_severity": summary.get(
                "discrepancies_by_severity", {}
            ),
        },
    )
