"""
Audit logging service for GuvFX platform.

This module provides a fail-open audit logging function that captures
security-relevant events without blocking business operations.

Usage:
    from core.audit import log_event

    log_event(
        request,
        event_type="STRATEGY_CREATED",
        entity_type="strategy",
        entity_id=str(strategy.id),
        metadata={"name": strategy.name}
    )
"""
import logging
from typing import Optional, Any

from django.http import HttpRequest

logger = logging.getLogger(__name__)


def get_client_ip(request: HttpRequest) -> Optional[str]:
    """
    Extract the client IP address from the request.

    Handles X-Forwarded-For header for requests behind proxies/load balancers.
    """
    if not request:
        return None

    # Check for forwarded header (Traefik, nginx, etc.)
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        # Take the first IP in the chain (original client)
        return x_forwarded_for.split(",")[0].strip()

    # Fall back to REMOTE_ADDR
    return request.META.get("REMOTE_ADDR")


def get_user_agent(request: HttpRequest) -> str:
    """Extract the User-Agent header from the request."""
    if not request:
        return ""
    return request.META.get("HTTP_USER_AGENT", "")[:500]  # Truncate to 500 chars


def get_request_path(request: HttpRequest) -> str:
    """Extract the request path."""
    if not request:
        return ""
    return getattr(request, "path", "")[:255]


def get_request_method(request: HttpRequest) -> str:
    """Extract the HTTP method."""
    if not request:
        return ""
    return getattr(request, "method", "")[:12]


def log_event(
    request: Optional[HttpRequest],
    event_type: str,
    severity: str = "INFO",
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """
    Log an audit event.

    This function is fail-open: it will NEVER throw an exception.
    If logging fails, it logs an error to the console and continues.

    Args:
        request: The HTTP request (can be None for system events)
        event_type: The type of event (see AuditEvent.EventType)
        severity: Event severity (DEBUG, INFO, WARN, ERROR, CRITICAL)
        entity_type: Type of entity (e.g., 'strategy', 'account')
        entity_id: ID of the entity (will be converted to string)
        metadata: Additional context (must NOT contain sensitive data)
    """
    try:
        # Import here to avoid circular imports
        from core.models import AuditEvent

        # Extract user from request
        user = None
        if request:
            user = getattr(request, "user", None)
            if user and not user.is_authenticated:
                user = None

        # Build event data
        event_data = {
            "user": user,
            "event_type": event_type,
            "severity": severity,
            "entity_type": entity_type or "",
            "entity_id": str(entity_id) if entity_id is not None else None,
            "ip_address": get_client_ip(request) if request else None,
            "user_agent": get_user_agent(request) if request else "",
            "path": get_request_path(request) if request else "",
            "method": get_request_method(request) if request else "",
            "metadata": _sanitize_metadata(metadata or {}),
        }

        # Create the audit event
        AuditEvent.objects.create(**event_data)

        # Also log to console for observability
        user_str = user.email if user else "anonymous"
        logger.info(
            f"AUDIT: [{severity}] {event_type} by {user_str} "
            f"entity={entity_type}:{entity_id}"
        )

    except Exception as e:
        # Fail-open: log error but don't raise
        logger.error(
            f"Failed to create audit event: {e}. "
            f"Event was: {event_type}, entity={entity_type}:{entity_id}"
        )


def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """
    Sanitize metadata to remove any potentially sensitive fields.

    This is a safety net - callers should not pass sensitive data,
    but we filter known patterns just in case.
    """
    sensitive_keys = {
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "auth",
        "credential",
        "private",
        "ssn",
        "credit_card",
        "cvv",
    }

    sanitized = {}
    for key, value in metadata.items():
        # Check if key contains any sensitive patterns
        key_lower = key.lower()
        if any(sensitive in key_lower for sensitive in sensitive_keys):
            sanitized[key] = "[REDACTED]"
        elif isinstance(value, dict):
            sanitized[key] = _sanitize_metadata(value)
        else:
            sanitized[key] = value

    return sanitized


# Convenience functions for common event types


def log_auth_login(request: HttpRequest, user_id: int, email: str) -> None:
    """Log a successful login event."""
    log_event(
        request,
        event_type="AUTH_LOGIN",
        severity="INFO",
        entity_type="user",
        entity_id=str(user_id),
        metadata={"email": email},
    )


def log_auth_logout(request: HttpRequest) -> None:
    """Log a logout event."""
    user = getattr(request, "user", None)
    log_event(
        request,
        event_type="AUTH_LOGOUT",
        severity="INFO",
        entity_type="user",
        entity_id=str(user.id) if user and user.is_authenticated else None,
    )


def log_auth_failed(request: HttpRequest, email: str, reason: str = "") -> None:
    """Log a failed authentication attempt."""
    log_event(
        request,
        event_type="AUTH_FAILED",
        severity="WARN",
        entity_type="user",
        metadata={"email": email, "reason": reason},
    )


def log_strategy_created(request: HttpRequest, strategy) -> None:
    """Log strategy creation."""
    log_event(
        request,
        event_type="STRATEGY_CREATED",
        severity="INFO",
        entity_type="strategy",
        entity_id=str(strategy.id),
        metadata={"name": strategy.name},
    )


def log_strategy_updated(request: HttpRequest, strategy, changed_fields: list = None) -> None:
    """Log strategy update."""
    log_event(
        request,
        event_type="STRATEGY_UPDATED",
        severity="INFO",
        entity_type="strategy",
        entity_id=str(strategy.id),
        metadata={"name": strategy.name, "changed_fields": changed_fields or []},
    )


def log_strategy_deleted(request: HttpRequest, strategy_id: int, strategy_name: str) -> None:
    """Log strategy deletion."""
    log_event(
        request,
        event_type="STRATEGY_DELETED",
        severity="INFO",
        entity_type="strategy",
        entity_id=str(strategy_id),
        metadata={"name": strategy_name},
    )


def log_backtest_config_created(request: HttpRequest, config) -> None:
    """Log backtest config creation."""
    log_event(
        request,
        event_type="BACKTEST_CONFIG_CREATED",
        severity="INFO",
        entity_type="backtest_config",
        entity_id=str(config.id),
        metadata={
            "name": config.name,
            "strategy_id": config.strategy_id,
        },
    )


def log_backtest_run_created(request: HttpRequest, run) -> None:
    """Log backtest run creation."""
    log_event(
        request,
        event_type="BACKTEST_RUN_CREATED",
        severity="INFO",
        entity_type="backtest_run",
        entity_id=str(run.id),
        metadata={
            "config_id": run.config_id,
            "symbol": run.symbol,
            "timeframe": run.timeframe,
        },
    )


def log_backtests_processed(request: HttpRequest, count: int) -> None:
    """Log batch backtest processing."""
    log_event(
        request,
        event_type="BACKTEST_RUNS_PROCESSED",
        severity="INFO",
        entity_type="backtest_run",
        metadata={"processed_count": count},
    )


def log_assignment_created(request: HttpRequest, assignment) -> None:
    """Log strategy assignment creation."""
    log_event(
        request,
        event_type="ASSIGNMENT_CREATED",
        severity="INFO",
        entity_type="assignment",
        entity_id=str(assignment.id),
        metadata={
            "strategy_id": assignment.strategy_id,
            "account_id": assignment.account_id,
        },
    )


def log_execution_attempt(
    request: HttpRequest,
    event_type: str,
    account_id: str,
    reason: str = "Endpoint not implemented",
) -> None:
    """Log an execution control attempt (currently all return 501)."""
    log_event(
        request,
        event_type=event_type,
        severity="WARN",
        entity_type="account",
        entity_id=account_id,
        metadata={"reason": reason, "status": "not_implemented"},
    )


# =============================================================================
# Execution Job Audit Helpers
# =============================================================================


def log_execution_job_created(
    request: HttpRequest,
    job_id: str,
    job_type: str,
    account_id: int,
    strategy_id: int | None = None,
    metadata: dict | None = None,
) -> None:
    """Log execution job creation."""
    log_event(
        request,
        event_type="EXECUTION_JOB_CREATED",
        severity="INFO",
        entity_type="execution_job",
        entity_id=job_id,
        metadata={
            "job_type": job_type,
            "account_id": account_id,
            "strategy_id": strategy_id,
            **(metadata or {}),
        },
    )


def log_execution_job_claimed(
    request: HttpRequest | None,
    job_id: str,
    worker_id: str,
    account_id: int,
    routing_mode: str | None = None,
    terminal_node_id: int | None = None,
) -> None:
    """Log execution job claimed by worker."""
    meta: dict[str, Any] = {
        "worker_id": worker_id,
        "account_id": account_id,
    }
    if routing_mode:
        meta["routing_mode"] = routing_mode
    if terminal_node_id is not None:
        meta["terminal_node_id"] = terminal_node_id
    log_event(
        request,
        event_type="EXECUTION_JOB_CLAIMED",
        severity="INFO",
        entity_type="execution_job",
        entity_id=job_id,
        metadata=meta,
    )


def log_execution_job_completed(
    request: HttpRequest | None,
    job_id: str,
    success: bool,
    account_id: int,
    result: dict | None = None,
    error_message: str | None = None,
) -> None:
    """Log execution job completion (success or failure)."""
    event_type = "EXECUTION_JOB_COMPLETED" if success else "EXECUTION_JOB_FAILED"
    severity = "INFO" if success else "WARN"

    metadata = {
        "success": success,
        "account_id": account_id,
    }
    if result:
        # Only include non-sensitive result fields
        safe_result = {
            k: v for k, v in result.items()
            if k in ("ticket", "price", "volume", "symbol", "placed_at", "order_type")
        }
        metadata["result"] = safe_result
    if error_message:
        metadata["error"] = error_message[:500]  # Truncate long errors

    log_event(
        request,
        event_type=event_type,
        severity=severity,
        entity_type="execution_job",
        entity_id=job_id,
        metadata=metadata,
    )


def log_trades_ingested(
    request: HttpRequest,
    account_id: int,
    inserted: int,
    updated: int,
    deals_count: int,
) -> None:
    """Log trade ingestion from Windows agent."""
    log_event(
        request,
        event_type="TRADES_INGESTED",
        severity="INFO",
        entity_type="trading_account",
        entity_id=str(account_id),
        metadata={
            "inserted": inserted,
            "updated": updated,
            "deals_count": deals_count,
        },
    )


def log_trades_sync_queued(
    request: HttpRequest | None,
    account_id: int,
    trigger_job_id: int,
    sync_job_id: int,
) -> None:
    """Log automatic trade sync job queued after demo trade completion."""
    log_event(
        request,
        event_type="TRADES_SYNC_QUEUED",
        severity="INFO",
        entity_type="execution_job",
        entity_id=str(sync_job_id),
        metadata={
            "account_id": account_id,
            "trigger_job_id": trigger_job_id,
            "sync_job_id": sync_job_id,
        },
    )


# =============================================================================
# Strategy Signal Audit Helpers
# =============================================================================


def log_signal_evaluated(
    request: HttpRequest | None,
    strategy_id: int,
    account_id: int,
    symbol: str,
    signal_result: dict | None = None,
) -> None:
    """Log signal evaluation (whether or not it produced a trade)."""
    log_event(
        request,
        event_type="SIGNAL_EVALUATED",
        severity="INFO",
        entity_type="strategy",
        entity_id=str(strategy_id),
        metadata={
            "account_id": account_id,
            "symbol": symbol,
            "result": signal_result or {},
        },
    )


def log_signal_rejected(
    request: HttpRequest | None,
    strategy_id: int,
    account_id: int,
    symbol: str,
    reason: str,
    details: dict | None = None,
) -> None:
    """Log signal rejection with reason."""
    log_event(
        request,
        event_type="SIGNAL_REJECTED",
        severity="WARN",
        entity_type="strategy",
        entity_id=str(strategy_id),
        metadata={
            "account_id": account_id,
            "symbol": symbol,
            "reason": reason,
            **(details or {}),
        },
    )


def log_signal_created(
    request: HttpRequest | None,
    strategy_id: int,
    account_id: int,
    job_id: int,
    symbol: str,
    side: str,
    lots: float,
    entry_price: float | None = None,
    sl_price: float | None = None,
    tp_price: float | None = None,
) -> None:
    """Log signal created and job queued."""
    log_event(
        request,
        event_type="SIGNAL_CREATED",
        severity="INFO",
        entity_type="execution_job",
        entity_id=str(job_id),
        metadata={
            "strategy_id": strategy_id,
            "account_id": account_id,
            "symbol": symbol,
            "side": side,
            "lots": lots,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
        },
    )


# =============================================================================
# Worker Authentication Audit Helpers
# =============================================================================


def log_worker_auth_success(
    request: HttpRequest | None,
    worker_id: str,
) -> None:
    """Log a successful worker authentication."""
    log_event(
        request,
        event_type="WORKER_AUTH_SUCCESS",
        severity="INFO",
        entity_type="worker",
        metadata={"worker_id": worker_id},
    )


def log_worker_auth_failed(
    request: HttpRequest | None,
    worker_id: str,
    reason: str = "",
) -> None:
    """Log a failed worker authentication attempt."""
    log_event(
        request,
        event_type="WORKER_AUTH_FAILED",
        severity="WARN",
        entity_type="worker",
        metadata={"worker_id": worker_id, "reason": reason},
    )


# =============================================================================
# Subscription Mutation Audit Helpers
# =============================================================================


def log_subscription_created(
    request: HttpRequest | None,
    user_id: int,
    plan: str,
    metadata: dict | None = None,
) -> None:
    """Log subscription creation."""
    log_event(
        request,
        event_type="SUBSCRIPTION_CREATED",
        severity="INFO",
        entity_type="subscription",
        entity_id=str(user_id),
        metadata={"plan": plan, **(metadata or {})},
    )


def log_subscription_updated(
    request: HttpRequest | None,
    user_id: int,
    changed_fields: list | None = None,
    metadata: dict | None = None,
) -> None:
    """Log subscription update."""
    log_event(
        request,
        event_type="SUBSCRIPTION_UPDATED",
        severity="INFO",
        entity_type="subscription",
        entity_id=str(user_id),
        metadata={"changed_fields": changed_fields or [], **(metadata or {})},
    )


# =============================================================================
# Admin Override Audit Helpers
# =============================================================================


def log_admin_override(
    request: HttpRequest,
    action: str,
    entity_type: str = "",
    entity_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Log an admin override action."""
    log_event(
        request,
        event_type="ADMIN_OVERRIDE",
        severity="WARN",
        entity_type=entity_type,
        entity_id=entity_id,
        metadata={"action": action, **(metadata or {})},
    )


# =============================================================================
# Entitlement Enforcement Audit Helpers
# =============================================================================


# =============================================================================
# Backtest Worker Audit Helpers (Packet B — B2)
# =============================================================================


def log_backtest_execution_claimed(
    execution_id: int,
    job_id: int,
    worker_hostname: str,
    run_identifier: str,
) -> None:
    """Log a backtest execution claimed by a worker."""
    log_event(
        None,
        event_type="BACKTEST_EXECUTION_CLAIMED",
        severity="INFO",
        entity_type="backtest_execution",
        entity_id=str(execution_id),
        metadata={
            "job_id": job_id,
            "worker_hostname": worker_hostname,
            "run_identifier": run_identifier,
        },
    )


def log_backtest_execution_started(
    execution_id: int,
    job_id: int,
    worker_hostname: str,
) -> None:
    """Log a backtest execution started."""
    log_event(
        None,
        event_type="BACKTEST_EXECUTION_STARTED",
        severity="INFO",
        entity_type="backtest_execution",
        entity_id=str(execution_id),
        metadata={
            "job_id": job_id,
            "worker_hostname": worker_hostname,
        },
    )


def log_backtest_execution_completed(
    execution_id: int,
    job_id: int,
    duration_seconds: float | None = None,
) -> None:
    """Log a backtest execution completed successfully."""
    meta: dict[str, Any] = {"job_id": job_id}
    if duration_seconds is not None:
        meta["duration_seconds"] = duration_seconds
    log_event(
        None,
        event_type="BACKTEST_EXECUTION_COMPLETED",
        severity="INFO",
        entity_type="backtest_execution",
        entity_id=str(execution_id),
        metadata=meta,
    )


def log_backtest_execution_failed(
    execution_id: int,
    job_id: int,
    error: str = "",
) -> None:
    """Log a backtest execution failure."""
    log_event(
        None,
        event_type="BACKTEST_EXECUTION_FAILED",
        severity="ERROR",
        entity_type="backtest_execution",
        entity_id=str(execution_id),
        metadata={
            "job_id": job_id,
            "error": error[:500],
        },
    )


# =============================================================================
# Backtest API Audit Helpers (Packet B — B5)
# =============================================================================


def log_backtest_job_created(
    request: HttpRequest | None,
    job_id: int,
    execution_id: int,
    strategy_id: int,
    symbol: str,
) -> None:
    """Log a new backtest job + execution creation via API."""
    log_event(
        request,
        event_type="BACKTEST_JOB_CREATED",
        severity="INFO",
        entity_type="backtest_job",
        entity_id=str(job_id),
        metadata={
            "execution_id": execution_id,
            "strategy_id": strategy_id,
            "symbol": symbol,
        },
    )


def log_backtest_status_viewed(
    request: HttpRequest | None,
    job_id: int,
) -> None:
    """Log a backtest status retrieval."""
    log_event(
        request,
        event_type="BACKTEST_STATUS_VIEWED",
        severity="INFO",
        entity_type="backtest_job",
        entity_id=str(job_id),
    )


def log_backtest_results_viewed(
    request: HttpRequest | None,
    job_id: int,
) -> None:
    """Log a backtest results retrieval."""
    log_event(
        request,
        event_type="BACKTEST_RESULTS_VIEWED",
        severity="INFO",
        entity_type="backtest_job",
        entity_id=str(job_id),
    )


def log_backtest_artifacts_viewed(
    request: HttpRequest | None,
    job_id: int,
) -> None:
    """Log a backtest artifacts listing retrieval."""
    log_event(
        request,
        event_type="BACKTEST_ARTIFACTS_VIEWED",
        severity="INFO",
        entity_type="backtest_job",
        entity_id=str(job_id),
    )


# =============================================================================
# Backtest Promotion Audit Helpers (Packet B — B7)
# =============================================================================


def log_backtest_promotion_created(
    request: HttpRequest | None,
    promotion_id: int,
    execution_id: int,
    job_id: int,
) -> None:
    """Log a new promotion candidate creation via API."""
    log_event(
        request,
        event_type="BACKTEST_PROMOTION_CREATED",
        severity="INFO",
        entity_type="promotion_candidate",
        entity_id=str(promotion_id),
        metadata={
            "execution_id": execution_id,
            "job_id": job_id,
        },
    )


def log_backtest_promotion_reviewed(
    request: HttpRequest | None,
    promotion_id: int,
    execution_id: int,
    decision: str,
    reviewer_id: int,
    has_notes: bool = False,
) -> None:
    """Log a promotion candidate review action (Packet C1)."""
    log_event(
        request,
        event_type="BACKTEST_PROMOTION_REVIEWED",
        severity="INFO",
        entity_type="promotion_candidate",
        entity_id=str(promotion_id),
        metadata={
            "execution_id": execution_id,
            "decision": decision,
            "reviewer_id": reviewer_id,
            "has_notes": has_notes,
        },
    )


def log_entitlement_denied(
    request: HttpRequest | None,
    user_id: int,
    capability: str,
    plan: str | None = None,
    resolved_mode: str | None = None,
) -> None:
    """Log an entitlement denial."""
    log_event(
        request,
        event_type="ENTITLEMENT_DENIED",
        severity="WARN",
        entity_type="user",
        entity_id=str(user_id),
        metadata={
            "capability": capability,
            "plan": plan,
            "resolved_access_mode": resolved_mode,
        },
    )
