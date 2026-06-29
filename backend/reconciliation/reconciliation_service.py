"""
Reconciliation service — detection-only.

Compares platform Trade records against MT5 source data and creates
``ReconciliationEvent`` records for every detected discrepancy.

**Strict prohibition**:
This module must **never** update ``Trade``, ``TradingAccount``, or any
other financial record.  It is read-only with respect to financial data
and write-only with respect to ``ReconciliationEvent`` and ``AuditEvent``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.db import IntegrityError

from core.audit import log_event
from reconciliation.reconciliation_models import ReconciliationEvent
from trading.models import Trade, TradingAccount

logger = logging.getLogger(__name__)

# Fields that are compared between MT5 and platform Trade records.
# Maps (Trade model field → expected MT5 dict key).
_COMPARABLE_FIELDS: dict[str, str] = {
    "volume": "volume",
    "open_price": "open_price",
    "close_price": "close_price",
    "profit": "profit",
    "commission": "commission",
    "swap": "swap",
    "symbol": "symbol",
    "side": "side",
}

# Fields whose values are monetary and may have an associated currency
# field on the Trade model.
_MONETARY_FIELDS: dict[str, str] = {
    "profit": "profit_currency",
    "commission": "commission_currency",
    "swap": "swap_currency",
}


def _safe_str(value: Any) -> str:
    """Coerce a value to a stable text representation for storage/hashing."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(value.normalize())
    return str(value)


def _values_match(platform_val: Any, mt5_val: Any) -> bool:
    """
    Compare a platform value with its MT5 counterpart.

    Numeric values are compared as Decimal to avoid float round-trip noise.
    None/missing values on *both* sides are treated as equal.
    """
    if platform_val is None and mt5_val is None:
        return True
    if platform_val is None or mt5_val is None:
        return False
    try:
        return Decimal(str(platform_val)) == Decimal(str(mt5_val))
    except Exception:
        return str(platform_val) == str(mt5_val)


def _determine_severity(
    field_name: str,
    trade: Trade,
    mt5_data: dict[str, Any],
) -> str:
    """
    Determine the severity of a field-level discrepancy.

    Rules:
    - If a monetary field has null currency on the platform side, the
      comparison is inherently unreliable → INFO.
    - profit / commission / swap mismatches → CRITICAL (financial).
    - volume / price mismatches → WARNING (significant but non-monetary).
    - Other field mismatches → INFO.
    """
    # Monetary field with unknown currency → INFO (incomparable)
    if field_name in _MONETARY_FIELDS:
        currency_field = _MONETARY_FIELDS[field_name]
        platform_currency = getattr(trade, currency_field, None)
        if platform_currency is None or platform_currency == "":
            return ReconciliationEvent.Severity.INFO

    if field_name in ("profit", "commission", "swap"):
        return ReconciliationEvent.Severity.CRITICAL
    if field_name in ("volume", "open_price", "close_price"):
        return ReconciliationEvent.Severity.WARNING
    return ReconciliationEvent.Severity.INFO


def _build_metadata(
    trade: Trade,
    field_name: str,
    mt5_data: dict[str, Any],
) -> dict[str, Any]:
    """
    Build bounded structured metadata for a discrepancy event.

    Allowed shape:
    {
        "comparison_source": "mt5_snapshot",
        "comparison_context": { limited set of safe keys },
        "detected_by": "reconciliation_service",
    }
    """
    context: dict[str, Any] = {
        "symbol": mt5_data.get("symbol", ""),
    }
    # Include volume and relevant price for context, truncated to safe size.
    if "volume" in mt5_data:
        context["volume"] = _safe_str(mt5_data["volume"])
    if "open_price" in mt5_data:
        context["price"] = _safe_str(mt5_data["open_price"])

    # Include currency context for monetary fields
    if field_name in _MONETARY_FIELDS:
        currency_attr = _MONETARY_FIELDS[field_name]
        context["platform_currency"] = getattr(trade, currency_attr, None) or "UNKNOWN"

    return {
        "comparison_source": "mt5_snapshot",
        "comparison_context": context,
        "detected_by": "reconciliation_service",
    }


def reconcile_account(
    account: TradingAccount,
    mt5_trades: list[dict[str, Any]],
    reconciliation_run_id: str,
) -> list[ReconciliationEvent]:
    """
    Compare platform Trades for *account* against *mt5_trades* snapshot.

    Args:
        account: The TradingAccount to reconcile.
        mt5_trades: List of dicts representing MT5 source records.
            Each dict must have at least a ``"ticket"`` key.
        reconciliation_run_id: Opaque run identifier.

    Returns:
        List of newly-created ``ReconciliationEvent`` instances.

    The function:
    - reads Trade rows (no writes)
    - creates ReconciliationEvent rows
    - emits AuditEvent for each discrepancy
    - suppresses duplicate discrepancies within the same run via DB
      unique constraint + IntegrityError catch
    """
    # Index MT5 records by ticket for O(1) lookup.
    mt5_by_ticket: dict[str, dict[str, Any]] = {}
    for mt5_rec in mt5_trades:
        ticket = str(mt5_rec.get("ticket", ""))
        if ticket:
            mt5_by_ticket[ticket] = mt5_rec

    # Read platform trades — never written back.
    platform_trades = Trade.objects.filter(account=account).only(
        "id",
        "ticket",
        "symbol",
        "side",
        "volume",
        "open_price",
        "close_price",
        "profit",
        "commission",
        "swap",
        "profit_currency",
        "commission_currency",
        "swap_currency",
    )

    created_events: list[ReconciliationEvent] = []

    for trade in platform_trades:
        mt5_data = mt5_by_ticket.get(trade.ticket)
        if mt5_data is None:
            # Platform trade not present in MT5 snapshot — that is a
            # significant discrepancy but not a field-level comparison.
            event = _create_event(
                account=account,
                run_id=reconciliation_run_id,
                recon_type="missing_in_mt5",
                ticket=trade.ticket,
                field_name="existence",
                mt5_value="",
                platform_value="present",
                severity=ReconciliationEvent.Severity.CRITICAL,
                metadata={
                    "comparison_source": "mt5_snapshot",
                    "comparison_context": {"symbol": trade.symbol},
                    "detected_by": "reconciliation_service",
                },
            )
            if event:
                created_events.append(event)
            continue

        # Field-level comparison
        for model_field, mt5_key in _COMPARABLE_FIELDS.items():
            platform_val = getattr(trade, model_field, None)
            mt5_val = mt5_data.get(mt5_key)

            if _values_match(platform_val, mt5_val):
                continue

            severity = _determine_severity(model_field, trade, mt5_data)
            metadata = _build_metadata(trade, model_field, mt5_data)

            event = _create_event(
                account=account,
                run_id=reconciliation_run_id,
                recon_type="trade_field_mismatch",
                ticket=trade.ticket,
                field_name=model_field,
                mt5_value=_safe_str(mt5_val),
                platform_value=_safe_str(platform_val),
                severity=severity,
                metadata=metadata,
            )
            if event:
                created_events.append(event)

    # Check for MT5 records not present on the platform
    platform_tickets = set(
        platform_trades.values_list("ticket", flat=True)
    )
    for ticket, mt5_data in mt5_by_ticket.items():
        if ticket not in platform_tickets:
            event = _create_event(
                account=account,
                run_id=reconciliation_run_id,
                recon_type="missing_in_platform",
                ticket=ticket,
                field_name="existence",
                mt5_value="present",
                platform_value="",
                severity=ReconciliationEvent.Severity.CRITICAL,
                metadata={
                    "comparison_source": "mt5_snapshot",
                    "comparison_context": {
                        "symbol": mt5_data.get("symbol", ""),
                    },
                    "detected_by": "reconciliation_service",
                },
            )
            if event:
                created_events.append(event)

    return created_events


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _create_event(
    *,
    account: TradingAccount,
    run_id: str,
    recon_type: str,
    ticket: str,
    field_name: str,
    mt5_value: str,
    platform_value: str,
    severity: str,
    metadata: dict[str, Any],
) -> ReconciliationEvent | None:
    """
    Create a ReconciliationEvent with deterministic signature and
    duplicate suppression.

    Returns the created instance, or ``None`` if the event was suppressed
    as a duplicate within the same run.
    """
    sig = ReconciliationEvent.compute_signature(
        reconciliation_run_id=run_id,
        account_id=account.id,
        ticket=ticket,
        field_name=field_name,
        mt5_value=mt5_value,
        platform_value=platform_value,
    )

    try:
        event = ReconciliationEvent.objects.create(
            account=account,
            reconciliation_run_id=run_id,
            reconciliation_type=recon_type,
            ticket=ticket,
            field_name=field_name,
            mt5_value=mt5_value,
            platform_value=platform_value,
            severity=severity,
            resolution_status=ReconciliationEvent.ResolutionStatus.OPEN,
            signature=sig,
            metadata=metadata,
        )
    except IntegrityError:
        # Duplicate within the same run — suppressed by DB constraint.
        logger.debug(
            "Duplicate discrepancy suppressed: run=%s ticket=%s field=%s",
            run_id,
            ticket,
            field_name,
        )
        return None

    # Emit audit event for discrepancy detection (fail-open)
    _emit_discrepancy_audit(event)

    return event


def _emit_discrepancy_audit(event: ReconciliationEvent) -> None:
    """Emit an AuditEvent for a detected discrepancy."""
    log_event(
        request=None,
        event_type="RECONCILIATION_DISCREPANCY",
        severity=event.severity,
        entity_type="reconciliation_event",
        entity_id=str(event.id),
        metadata={
            "reconciliation_run_id": event.reconciliation_run_id,
            "account_id": event.account_id,
            "ticket": event.ticket,
            "field_name": event.field_name,
            "reconciliation_type": event.reconciliation_type,
        },
    )
