"""trade_copier.failure_alerts — governed copier terminal-failure alerting (ADR-0074 Phase 9).

Routes copier terminal-failure SIGNALS to the EXISTING governed operator alert channel
(``reliability.AlertEvent``) — no new ad-hoc Telegram/email path is created. A directly-created ``AlertEvent``
is surfaced on the read-only operations summary + the alerts API; OUTBOUND delivery is handled by the SEPARATE,
default-OFF governed outbox+dispatcher (``trade_copier.alert_delivery``), whose default transport has NO real
destination — so raising an alert enqueues a durable delivery row but nothing is actually sent until an operator
both arms ``COPIER_ALERT_DISPATCH_ENABLED`` and configures a real transport. Alerts are de-duplicated
(``dedup_key``), secret-free (title/body/detail carry only ids and reason codes), carry the exact failure
reason, distinguish a RETRYABLE observation failure from a TERMINAL execution failure, and NEVER recover a
trade — they only expose operator action.

Gated by ``copier_alerts_enabled()`` (default OFF). When OFF the terminal failure is still durably recorded as
append-only ``CopierLifecycleTransition`` evidence by the lifecycle projection; this module only governs whether
an operator ``AlertEvent`` is ALSO raised. Every public call is fail-open: alerting must never break ingestion
or a projection pass.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ─── alert-category taxonomy (packet Phase 9 + ADR-0083 Phase-2 extension). These are ALERT categories, not
#     new lifecycle states — the 8-state machine (ADR-0071 invariant) is unchanged; categories classify failure
#     REASONS. Machine-readable, stable codes. ───
ENTRY_FAILED_FINAL = "ENTRY_FAILED_FINAL"
MODIFY_FAILED_FINAL = "MODIFICATION_FAILED"
CLOSE_FAILED_FINAL = "FULL_CLOSE_FAILED"
CONTAINED_FAILURE = "CONTAINED_FAILURE"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
MAPPING_UNAVAILABLE = "MAPPING_UNAVAILABLE"
EVENT_REJECTED = "EVENT_REJECTED"
RECONCILIATION_DIVERGENCE = "DIVERGED"
# ADR-0083 LIVE lifecycle failure categories (added here; mapped from the admission/command reason codes).
PARTIAL_CLOSE_FAILED = "PARTIAL_CLOSE_FAILED"
UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
PRE_STATE_MISMATCH = "PRE_STATE_MISMATCH"
AUTHORIZATION_EXPIRED_OR_INVALID = "AUTHORIZATION_EXPIRED_OR_INVALID"

# Packet-canonical aliases (same VALUES as the legacy constant names above) so the exact reason codes the
# packet lists are importable directly.
MODIFICATION_FAILED = MODIFY_FAILED_FINAL
FULL_CLOSE_FAILED = CLOSE_FAILED_FINAL
DIVERGED = RECONCILIATION_DIVERGENCE

ALL_CATEGORIES = frozenset({
    ENTRY_FAILED_FINAL, MODIFY_FAILED_FINAL, CLOSE_FAILED_FINAL, CONTAINED_FAILURE, IDENTITY_MISMATCH,
    MAPPING_UNAVAILABLE, EVENT_REJECTED, RECONCILIATION_DIVERGENCE, PARTIAL_CLOSE_FAILED, UNKNOWN_OUTCOME,
    DEADLINE_EXCEEDED, PRE_STATE_MISMATCH, AUTHORIZATION_EXPIRED_OR_INVALID})

# severity + retryable posture per category.
_CRITICAL = frozenset({CLOSE_FAILED_FINAL, CONTAINED_FAILURE, RECONCILIATION_DIVERGENCE,
                       PARTIAL_CLOSE_FAILED, UNKNOWN_OUTCOME, PRE_STATE_MISMATCH})
_INFO = frozenset({EVENT_REJECTED})
# retryable == an OBSERVATION failure the bridge can re-emit past; terminal == an EXECUTION-side failure.
_RETRYABLE = frozenset({EVENT_REJECTED})

# ADR-0083: map the live-execution admission / command / lifecycle reason codes to an alert category. An
# UNKNOWN broker outcome stays UNKNOWN (never inferred as success); an expired/invalid/revoked authorization is
# surfaced; a bridge pre-state fingerprint rejection is surfaced. A reason not listed falls back to CONTAINED.
_REASON_TO_CATEGORY = {
    # authorization
    "no_live_authorization": AUTHORIZATION_EXPIRED_OR_INVALID,
    "governing_authorization_revoked_or_absent": AUTHORIZATION_EXPIRED_OR_INVALID,
    "authorization_revoked": AUTHORIZATION_EXPIRED_OR_INVALID,
    "authorization_expired": AUTHORIZATION_EXPIRED_OR_INVALID,
    "entry_window_expired_no_close_only": AUTHORIZATION_EXPIRED_OR_INVALID,
    "order_allowance_exhausted": AUTHORIZATION_EXPIRED_OR_INVALID,
    # identity / mapping
    "master_identity_drift": IDENTITY_MISMATCH,
    "follower_identity_drift": IDENTITY_MISMATCH,
    "user_mismatch": IDENTITY_MISMATCH,
    "pair_mismatch": IDENTITY_MISMATCH,
    "symbol_map_unverified_or_changed": MAPPING_UNAVAILABLE,
    "intent_follower_symbol_mismatch": MAPPING_UNAVAILABLE,
    # staleness / ordering / pre-state
    "event_not_after_open": PRE_STATE_MISMATCH,
    "superseded_by_later_close": PRE_STATE_MISMATCH,
    "symbol_changed": PRE_STATE_MISMATCH,
    "follower_ticket_not_open": PRE_STATE_MISMATCH,
    "pre_state_mismatch": PRE_STATE_MISMATCH,
    # outcome
    "unknown_outcome": UNKNOWN_OUTCOME,
    "broker_outcome_unknown": UNKNOWN_OUTCOME,
    "deadline_exceeded": DEADLINE_EXCEEDED,
    # lifecycle link signals
    "modify_failed": MODIFY_FAILED_FINAL,
    "close_failed": CLOSE_FAILED_FINAL,
    "partial_close_failed": PARTIAL_CLOSE_FAILED,
    "link_divergent": RECONCILIATION_DIVERGENCE,
    "link_lost": RECONCILIATION_DIVERGENCE,
}

# A cap on the per-alert audit history so a flapping failure can never grow an AlertEvent unboundedly.
_MAX_REVISION_HISTORY = 20


def classify_lifecycle_failure(reason: str) -> str:
    """Map a lifecycle/admission/command reason code to a stable alert category (fallback CONTAINED_FAILURE)."""
    return _REASON_TO_CATEGORY.get((reason or "").strip(), CONTAINED_FAILURE)


def _redact_ticket(ticket) -> str | None:
    """Mask a position ticket to a '…NNNN' tail (consistent with the operator view) so an outbound-deliverable
    alert never carries a full ticket. None stays None."""
    if ticket is None:
        return None
    s = str(ticket).strip()
    if not s:
        return None
    return "…" + s[-4:] if len(s) > 4 else "…"


def _severity(category: str) -> str:
    from reliability.models import AlertEvent
    if category in _CRITICAL:
        return AlertEvent.Severity.CRITICAL
    if category in _INFO:
        return AlertEvent.Severity.INFO
    return AlertEvent.Severity.WARN


def raise_copier_alert(category: str, *, scope_key: str, trading_account=None, ticket=None,
                       lifecycle=None, reason: str = "", correlation_id: str = "", revision: int = 0) -> bool:
    """Raise/update ONE de-duplicated governed operator alert for a copier failure. Returns True iff a NEW alert
    row was created OR an existing OPEN alert was materially updated to a strictly newer ``revision``; False on a
    pure duplicate/replay/stale observation. Gated + fail-open. Secret-free.

    De-duplication contract: one OPEN alert per ``(category, scope_key)``. A duplicate/replayed/restart-driven
    observation with ``revision`` <= the recorded one is suppressed (no storm). A materially newer failure
    ``revision`` REPLACES the open alert's content in place and appends an auditable entry to
    ``detail.revision_history`` (never a second open row). Resolution is NEVER inferred from silence here — an
    OPEN alert stays open until confirmed lifecycle evidence resolves it (operator / evidence-gated resolver);
    this function never marks CLOSED / ZERO-EXPOSURE / RECOVERED."""
    try:
        from trade_copier.flags import copier_alerts_enabled
        if not copier_alerts_enabled():
            return False
        from reliability.constants import Component
        from reliability.models import AlertEvent
        rev = int(revision or 0)
        dedup_key = f"copier:{category}:{scope_key}"[:200]
        existing = AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).first()
        if existing is not None:
            prior_rev = int((existing.detail or {}).get("revision", 0) or 0)
            if rev <= prior_rev:
                return False   # pure duplicate / replay / restart / stale — never a storm
            # Materially newer failure revision — replace in place with an auditable revision link/history.
            detail = dict(existing.detail or {})
            history = list(detail.get("revision_history") or [])
            history.append({"revision": prior_rev, "reason": str(detail.get("reason", ""))[:64]})
            detail["revision_history"] = history[-_MAX_REVISION_HISTORY:]
            detail["revision"] = rev
            detail["reason"] = (reason or "")[:64]
            detail["supersedes_revision"] = prior_rev
            existing.detail = detail
            existing.body = (f"ticket={detail.get('ticket_redacted')} reason={detail['reason']} "
                             f"lifecycle={detail.get('lifecycle_id')} rev={rev}")[:2000]
            existing.save(update_fields=["detail", "body"])
            # A materially-worse superseding failure re-enqueues ONE delivery (idempotent via dedup_key) so an
            # operator is notified of the newer revision, not just the first.
            try:
                from trade_copier.alert_delivery import enqueue_delivery
                enqueue_delivery(existing, category=category, severity=str(existing.severity), payload=detail)
            except Exception:  # noqa: BLE001 — delivery enqueue is best-effort; the row is already updated
                logger.exception("copier alert reopen re-enqueue failed (category=%s)", category)
            return True
        detail = {
            "category": category,
            "reason": (reason or "")[:64],
            "ticket_redacted": _redact_ticket(ticket),
            "lifecycle_id": getattr(lifecycle, "id", None),
            "retryable": category in _RETRYABLE,
            "correlation_id": (correlation_id or "")[:40],
            "revision": rev,
            "revision_history": [],
        }
        severity = _severity(category)
        alert = AlertEvent.objects.create(
            component=Component.COPY_TRADING,
            severity=severity,
            trading_account=trading_account,
            title=f"Copy trading: {category}"[:200],
            body=(f"ticket={detail['ticket_redacted']} reason={detail['reason']} "
                  f"lifecycle={detail['lifecycle_id']} rev={rev}")[:2000],
            dedup_key=dedup_key,
            detail=detail,
        )
        # Governed DELIVERY (ADR-0074 Phase-9): also enqueue ONE durable outbox row (idempotent via dedup_key).
        # Enqueue is governed by this same flag; the SEPARATE dispatcher (copier_alert_dispatch_enabled, default
        # OFF) decides whether a queued delivery is ever SENT, and the default transport has no real destination.
        from trade_copier.alert_delivery import enqueue_delivery
        enqueue_delivery(alert, category=category, severity=str(severity), payload=detail)
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
    """Classify a terminal lifecycle row + its latest transition trigger into an alert category. The CONTAINED
    case delegates to the shared ``classify_lifecycle_failure`` reason→category map (ADR-0083), so a close/
    partial-close/divergence/pre-state/unknown-outcome/authorization trigger is surfaced with its precise
    category; an unmapped trigger falls back to CONTAINED_FAILURE."""
    from trade_copier.lifecycle import LifecycleState
    if state == LifecycleState.ENTRY_FAILED_FINAL.value:
        return ENTRY_FAILED_FINAL
    if state == LifecycleState.CONTAINED_FAILURE.value:
        return classify_lifecycle_failure(latest_trigger)
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

    from trade_copier.lifecycle import LifecycleState
    from trade_copier.models import CopierPositionLifecycle, CopierLifecycleTransition
    raised = 0
    scanned = 0
    # ONLY failure-terminal states (CONTAINED_FAILURE / ENTRY_FAILED_FINAL) — NEVER a successfully CLOSED
    # lifecycle (a routine success must never manufacture a CRITICAL "failure" alert).
    terminal = (CopierPositionLifecycle.objects.filter(is_terminal=True)
                .exclude(state=LifecycleState.CLOSED.value)
                .select_related("master", "master__trading_account").order_by("-updated_at")[:limit])
    for row in terminal:
        scanned += 1
        try:
            # Classify from the most recent REAL failure trigger — skip 'post_terminal_*_ignored' absorptions,
            # which would otherwise mask the specific failure category (and spawn a second, less-specific row).
            trigger = ""
            for t in CopierLifecycleTransition.objects.filter(lifecycle=row).order_by("-id")[:20]:
                if not (t.trigger or "").startswith("post_terminal_"):
                    trigger = t.trigger
                    break
            category = _category_for_terminal(row.state, trigger)
            ta = getattr(row.master, "trading_account", None)
            if raise_copier_alert(category, scope_key=f"lifecycle:{row.id}", trading_account=ta,
                                  ticket=row.master_position_ticket, lifecycle=row,
                                  reason=(row.failure_reason or trigger),
                                  revision=int(row.applied_sequence or 0)):
                raised += 1
        except Exception:  # noqa: BLE001 — one pathological row must never sink the whole scan
            logger.exception("copier failure-alert pass row failed (lifecycle=%s)", getattr(row, "id", None))

    # Non-terminal modify-rejection signals (WARN) — one open alert per affected lifecycle; the lifecycle's
    # monotone applied_sequence is the revision so a recurrence refreshes the open alert (never frozen at first).
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
        try:
            ta = getattr(lc.master, "trading_account", None)
            if raise_copier_alert(MODIFY_FAILED_FINAL, scope_key=f"lifecycle:{lc.id}:modify", trading_account=ta,
                                  ticket=lc.master_position_ticket, lifecycle=lc, reason="modify_failed",
                                  revision=int(getattr(lc, "applied_sequence", 0) or 0)):
                raised += 1
        except Exception:  # noqa: BLE001 — isolate per-row
            logger.exception("copier modify-failed alert row failed (lifecycle=%s)", getattr(lc, "id", None))
    return {"enabled": True, "raised": raised, "scanned": scanned}
