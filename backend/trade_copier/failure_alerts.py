"""trade_copier.failure_alerts — governed copier terminal-failure alerting (ADR-0074 Phase 9).

Routes copier terminal-failure SIGNALS to the EXISTING governed operator alert channel
(``reliability.AlertEvent``) — no new Telegram/email path is created, and nothing is pushed outward (a
directly-created ``AlertEvent`` is surfaced on the read-only operations summary + the alerts API; it is not
auto-delivered). Alerts are de-duplicated (``dedup_key``), secret-free (title/body/detail carry only ids and
reason codes), carry the exact failure reason, distinguish a RETRYABLE observation failure from a TERMINAL
execution failure, and NEVER recover a trade — they only expose operator action.

Gated by ``copier_alerts_enabled()`` (default OFF). When OFF the terminal failure is still durably recorded as
append-only ``CopierLifecycleTransition`` evidence by the lifecycle projection; this module only governs whether
an operator ``AlertEvent`` is ALSO raised. Every public call is fail-open: alerting must never break ingestion
or a projection pass.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ─── alert-category taxonomy (packet Phase 9). These are ALERT categories, not new lifecycle states —
#     the 8-state machine (ADR-0071 invariant) is unchanged; categories classify failure REASONS. ───
ENTRY_FAILED_FINAL = "ENTRY_FAILED_FINAL"
MODIFY_FAILED_FINAL = "MODIFY_FAILED_FINAL"
CLOSE_FAILED_FINAL = "CLOSE_FAILED_FINAL"
CONTAINED_FAILURE = "CONTAINED_FAILURE"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
MAPPING_UNAVAILABLE = "MAPPING_UNAVAILABLE"
EVENT_REJECTED = "EVENT_REJECTED"
RECONCILIATION_DIVERGENCE = "RECONCILIATION_DIVERGENCE"

# severity + retryable posture per category.
_CRITICAL = frozenset({CLOSE_FAILED_FINAL, CONTAINED_FAILURE, RECONCILIATION_DIVERGENCE})
_INFO = frozenset({EVENT_REJECTED})
# retryable == an OBSERVATION failure the bridge can re-emit past; terminal == an EXECUTION-side failure.
_RETRYABLE = frozenset({EVENT_REJECTED})


def _severity(category: str) -> str:
    from reliability.models import AlertEvent
    if category in _CRITICAL:
        return AlertEvent.Severity.CRITICAL
    if category in _INFO:
        return AlertEvent.Severity.INFO
    return AlertEvent.Severity.WARN


def raise_copier_alert(category: str, *, scope_key: str, trading_account=None, ticket=None,
                       lifecycle=None, reason: str = "", correlation_id: str = "") -> bool:
    """Raise ONE de-duplicated governed operator alert for a copier failure. Returns True iff a new alert row
    was created. Gated + fail-open. Secret-free."""
    try:
        from trade_copier.flags import copier_alerts_enabled
        if not copier_alerts_enabled():
            return False
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = f"copier:{category}:{scope_key}"[:200]
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return False
        detail = {
            "category": category,
            "reason": (reason or "")[:64],
            "ticket": int(ticket) if ticket is not None else None,
            "lifecycle_id": getattr(lifecycle, "id", None),
            "retryable": category in _RETRYABLE,
            "correlation_id": (correlation_id or "")[:40],
        }
        AlertEvent.objects.create(
            component=Component.COPY_TRADING,
            severity=_severity(category),
            trading_account=trading_account,
            title=f"Copy trading: {category}"[:200],
            body=(f"ticket={detail['ticket']} reason={detail['reason']} "
                  f"lifecycle={detail['lifecycle_id']}")[:2000],
            dedup_key=dedup_key,
            detail=detail,
        )
        return True
    except Exception:  # noqa: BLE001 — alerting is fail-open; never raise into the caller
        logger.exception("copier failure-alert raise failed (category=%s)", category)
        return False


def raise_ingest_rejection(reason: str, *, account_id: int, correlation_id: str = "") -> bool:
    """Map a permanent ingestion rejection to a governed alert (gated, fail-open)."""
    try:
        category = {
            "account_not_master": MAPPING_UNAVAILABLE,
            "identity_mismatch": IDENTITY_MISMATCH,
            "account_scope_violation": IDENTITY_MISMATCH,
        }.get(reason, EVENT_REJECTED)
        trading_account = _trading_account(account_id)
        return raise_copier_alert(
            category, scope_key=f"account:{int(account_id)}", trading_account=trading_account,
            reason=reason, correlation_id=correlation_id)
    except Exception:  # noqa: BLE001
        logger.exception("copier ingest-rejection alert failed (reason=%s)", reason)
        return False


def _trading_account(account_id: int):
    try:
        from trading.models import TradingAccount
        return TradingAccount.objects.filter(id=int(account_id)).first()
    except Exception:  # noqa: BLE001
        return None


def _category_for_terminal(state: str, latest_trigger: str) -> str:
    """Classify a terminal lifecycle row + its latest transition trigger into an alert category."""
    from trade_copier.lifecycle import LifecycleState
    if state == LifecycleState.ENTRY_FAILED_FINAL.value:
        return ENTRY_FAILED_FINAL
    if state == LifecycleState.CONTAINED_FAILURE.value:
        if latest_trigger == "close_failed":
            return CLOSE_FAILED_FINAL
        if latest_trigger in ("link_divergent", "link_lost"):
            return RECONCILIATION_DIVERGENCE
        return CONTAINED_FAILURE
    return CONTAINED_FAILURE


def run_failure_alert_pass(*, limit: int = 500) -> dict:
    """One gated, fail-open scan that raises de-duplicated operator alerts for copier terminal failures.

    Inert unless ``COPIER_ENABLED`` AND ``COPIER_ALERTS_ENABLED`` are on. Reads terminal ``CopierPositionLifecycle``
    rows + their latest transition trigger, classifies the alert category, and raises one de-duplicated
    ``AlertEvent`` each. Also raises WARN alerts for recent non-terminal ``modify_failed`` signals. Creates NO
    order, NO job. Returns a secret-free summary."""
    from trade_copier.flags import copier_alerts_enabled
    if not copier_alerts_enabled():
        return {"enabled": False, "raised": 0, "scanned": 0}

    from trade_copier.models import CopierPositionLifecycle, CopierLifecycleTransition
    raised = 0
    scanned = 0
    terminal = (CopierPositionLifecycle.objects.filter(is_terminal=True)
                .select_related("master", "master__trading_account").order_by("-updated_at")[:limit])
    for row in terminal:
        scanned += 1
        latest = (CopierLifecycleTransition.objects.filter(lifecycle=row).order_by("-id").first())
        trigger = latest.trigger if latest is not None else ""
        category = _category_for_terminal(row.state, trigger)
        ta = getattr(row.master, "trading_account", None)
        if raise_copier_alert(category, scope_key=f"lifecycle:{row.id}", trading_account=ta,
                              ticket=row.master_position_ticket, lifecycle=row,
                              reason=(row.failure_reason or trigger)):
            raised += 1

    # Non-terminal modify-rejection signals (WARN) — one open alert per affected lifecycle.
    modify_failed = (CopierLifecycleTransition.objects.filter(trigger="modify_failed")
                     .select_related("lifecycle", "lifecycle__master", "lifecycle__master__trading_account")
                     .order_by("-id")[:limit])
    seen: set[int] = set()
    for tr in modify_failed:
        lc = tr.lifecycle
        if lc.id in seen:
            continue
        seen.add(lc.id)
        scanned += 1
        ta = getattr(lc.master, "trading_account", None)
        if raise_copier_alert(MODIFY_FAILED_FINAL, scope_key=f"lifecycle:{lc.id}:modify", trading_account=ta,
                              ticket=lc.master_position_ticket, lifecycle=lc, reason="modify_failed"):
            raised += 1
    return {"enabled": True, "raised": raised, "scanned": scanned}
