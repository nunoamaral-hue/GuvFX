"""trade_copier.lifecycle_outcome_record — map a bridge lifecycle-guard OUTCOME (ADR-0085, realising ADR-0084)
onto the EXISTING canonical copier records: ``CopierReplication.reconciliation_status`` and the governed
operator failure alert (``reliability.AlertEvent`` via ``trade_copier.failure_alerts``).

This is the persistence→alert half of the ADR-0085 wiring — the function the (separately gated) armed
close/modify lifecycle dispatcher calls with the bridge result, and the mapping the partial-close path's failure
surfacing reuses. It is DARK: it does NOTHING unless the bridge result carries an explicit ``lifecycle_outcome``
(only produced when the HOST guard flag ``COPIER_LIFECYCLE_GUARD_ENABLED`` is on). It NEVER recovers / retries /
re-sends a trade, NEVER infers success from missing data (an unverifiable outcome stays UNKNOWN and alerts), and
widens NO authority. It raises NO NEW alert enum — it maps onto the EXISTING ADR-0083 failure categories.
"""
from __future__ import annotations

import logging

from trade_copier import failure_alerts as _fa

logger = logging.getLogger(__name__)

# The ADR-0084 explicit outcome vocabulary (kept in sync with scripts/mt5_copier_lifecycle_guard.py). Declared
# as plain strings so this backend module never imports the HOST-only guard script.
APPLIED = "APPLIED"
ALREADY_APPLIED = "ALREADY_APPLIED"
PRE_STATE_MISMATCH = "PRE_STATE_MISMATCH"
DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
STALE_REVISION = "STALE_REVISION"
POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
BROKER_REJECTED = "BROKER_REJECTED"
UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
CONTAINED_FAILURE = "CONTAINED_FAILURE"

_SUCCESS_OUTCOMES = frozenset({APPLIED, ALREADY_APPLIED})
_TERMINAL_FAILURES = frozenset({PRE_STATE_MISMATCH, DEADLINE_EXCEEDED, STALE_REVISION, POSITION_NOT_FOUND,
                                BROKER_REJECTED, UNKNOWN_OUTCOME, CONTAINED_FAILURE})
_ALL_OUTCOMES = _SUCCESS_OUTCOMES | _TERMINAL_FAILURES

# Guard outcome -> EXISTING failure-alert category (never a NEW competing enum). BROKER_REJECTED is op-specific.
_OUTCOME_TO_CATEGORY = {
    PRE_STATE_MISMATCH: _fa.PRE_STATE_MISMATCH,
    STALE_REVISION: _fa.PRE_STATE_MISMATCH,        # an out-of-order revision is a pre-state/ordering failure
    POSITION_NOT_FOUND: _fa.PRE_STATE_MISMATCH,    # the follower ticket is not open (same category the reasons map to)
    DEADLINE_EXCEEDED: _fa.DEADLINE_EXCEEDED,
    UNKNOWN_OUTCOME: _fa.UNKNOWN_OUTCOME,
    CONTAINED_FAILURE: _fa.CONTAINED_FAILURE,
}
_BROKER_REJECT_CATEGORY = {
    "MODIFY_PROTECTION": _fa.MODIFICATION_FAILED,
    "FULL_CLOSE": _fa.FULL_CLOSE_FAILED,
    "PARTIAL_CLOSE": _fa.PARTIAL_CLOSE_FAILED,
}


def failure_category_for(outcome: str, op: str):
    """The EXISTING failure-alert category for a terminal guard outcome, or None for a success outcome."""
    if outcome == BROKER_REJECTED:
        return _BROKER_REJECT_CATEGORY.get(op, _fa.CONTAINED_FAILURE)
    return _OUTCOME_TO_CATEGORY.get(outcome)


def record_lifecycle_outcome(replication, bridge_result, *, op, set_reconciliation: bool = True) -> dict:
    """Record a bridge lifecycle-guard outcome onto the replication + raise the governed operator alert for a
    terminal failure. DARK no-op unless ``bridge_result`` carries a ``lifecycle_outcome``. Fail-open: a recorder
    error never propagates into the caller. Returns a secret-free summary.

    - ``set_reconciliation`` writes the outcome onto ``reconciliation_status`` NON-DESTRUCTIVELY — never over a
      partial-close TERMINAL state the authoritative partial reconciler owns. Pass ``False`` where another
      authoritative reconciler owns the status (the dedicated partial-close path).
    - A SUCCESS outcome (APPLIED / ALREADY_APPLIED) raises NO alert.
    - A TERMINAL FAILURE raises ONE de-duplicated governed alert (gated by ``copier_alerts_enabled``, fail-open).
    - Never recovers / retries / re-sends; never infers success; never converts missing data into
      zero / closed / success.
    """
    try:
        br = bridge_result or {}
        outcome = str(br.get("lifecycle_outcome") or "").strip()
        if not outcome:
            return {"recorded": False, "reason": "no_lifecycle_outcome"}
        if outcome not in _ALL_OUTCOMES:
            # An unrecognised code fails CLOSED to a contained failure — never silently ignored.
            outcome = CONTAINED_FAILURE
        reason = str(br.get("lifecycle_reason") or "")[:64]
        status_written = None
        alerted = False

        if set_reconciliation and replication is not None:
            current = getattr(replication, "reconciliation_status", "") or ""
            # Write ONLY over an empty status or a PRIOR lifecycle-guard outcome. NEVER stomp a status owned by
            # another authoritative reconciler — the follower-ticket binding (MATCHED / UNMATCHED / AMBIGUOUS /
            # DIVERGENT_MISSING) that later lifecycle ops depend on, or ANY partial-close state (the in-flight
            # PCLOSE_SENT / PCLOSE_OUTCOME_UNKNOWN / PCLOSE_RECON_* and the PCLOSE_* terminals).
            if current == "" or current in _ALL_OUTCOMES:
                replication.reconciliation_status = outcome[:24]
                try:
                    replication.save(update_fields=["reconciliation_status", "updated_at"])
                except Exception:  # noqa: BLE001 — model without updated_at: fall back
                    replication.save(update_fields=["reconciliation_status"])
                status_written = outcome[:24]

        if outcome in _TERMINAL_FAILURES:
            category = failure_category_for(outcome, op) or _fa.CONTAINED_FAILURE
            alerted = _fa.raise_copier_alert(
                category,
                scope_key=f"{op}:repl:{getattr(replication, 'id', '')}",
                trading_account=_trading_account_for(replication),
                ticket=getattr(replication, "follower_ticket", None),
                reason=reason or outcome.lower(),
                correlation_id=str(br.get("correlation_id") or "")[:40],
                revision=_revision_for(replication))
        return {"recorded": True, "outcome": outcome, "reconciliation_status": status_written,
                "alerted": bool(alerted)}
    except Exception:  # noqa: BLE001 — the recorder is fail-open; it must never break the caller's flow
        logger.exception("copier lifecycle-outcome record failed (op=%s)", op)
        return {"recorded": False, "reason": "record_error"}


def _trading_account_for(replication):
    try:
        return replication.follower.follower_account
    except Exception:  # noqa: BLE001
        return None


def _revision_for(replication) -> int:
    try:
        return int(getattr(replication.master_event, "sequence", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0
