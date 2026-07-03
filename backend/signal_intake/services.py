"""
Signal-intake services (E0, SHADOW).

Reuses the deployed Wayond parser (``intelligence.telegram_source``) to turn
Telegram messages into ``PendingSignalApproval`` rows for human review.

HARD BOUNDARY: this module must NEVER create an ExecutionJob, place an order,
or import ``execution``. Approving a signal only records the decision. The
signal→order bridge is a later, separately-gated packet.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

# Reuse the already-deployed, pure Wayond parser (content side). Importing this
# parsing utility does NOT couple the execution and content flows: this app never
# touches the WIMS ConsumptionContract, and never creates an order.
from intelligence.telegram_source import Kind, classify_messages, parse_message

# core is shared infrastructure (NOT the execution app) — importing it does not
# cross the one-way signal_intake→execution boundary.
from core.observability import log_stage, new_correlation_id

from .models import PendingSignalApproval, SignalAuditEvent

SOURCE = PendingSignalApproval.Source.WAYOND_TELEGRAM


def _audit(actor, event, approval, **detail):
    return SignalAuditEvent.objects.create(
        actor=actor, event=event, approval=approval, detail=detail,
    )


@transaction.atomic
def intake_parsed(parsed, *, actor=None, source=SOURCE, provider=None) -> PendingSignalApproval:
    """Create or return the PendingSignalApproval for a parsed Telegram message.

    Idempotent on (source, message_id): a duplicate message returns the existing
    record and creates nothing new. UNKNOWN/unparseable messages are quarantined,
    never turned into a tradeable approval. Creates NO ExecutionJob.

    SIGNAL-ACQUISITION: when ``provider`` is given (dispatcher path), the approval
    links to it and its ``slug`` becomes the source (backwards-compatible — the
    legacy file-intake path passes no provider and keeps ``source=SOURCE``).
    """
    if provider is not None:
        source = provider.slug
    mid = parsed.message_id or f"{parsed.market}-{parsed.direction}-{parsed.entry}"

    existing = PendingSignalApproval.objects.filter(source=source, message_id=mid).first()
    if existing is not None:
        return existing  # dedup / replay-safe

    if parsed.kind == Kind.SIGNAL and parsed.is_tradeable_shape():
        # OPS-OBSERVABILITY: mint the correlation id that ties this execution
        # attempt's lifecycle together (stages 1-2 here → planning → shadow job).
        correlation_id = new_correlation_id()
        log_stage("signal_received", correlation_id, source=str(source), message_id=mid)
        approval = PendingSignalApproval.objects.create(
            source=source, message_id=mid, provider=provider,
            symbol=parsed.market, direction=parsed.direction,
            entry=parsed.entry, stop_loss=parsed.stop_loss,
            take_profit=(parsed.take_profits[0] if parsed.take_profits else ""),
            take_profits=list(parsed.take_profits),
            raw_payload={"raw_text": parsed.raw_text, "kind": parsed.kind},
            correlation_id=correlation_id,
            status=PendingSignalApproval.Status.PENDING_APPROVAL,
        )
        _audit(actor, SignalAuditEvent.Event.SIGNAL_RECEIVED, approval,
               message_id=mid, symbol=parsed.market, direction=parsed.direction)
        log_stage("parse_complete", correlation_id, approval_id=approval.id,
                  symbol=parsed.market, direction=parsed.direction, kind=str(parsed.kind))
        return approval

    # Not a tradeable signal -> quarantine (do not guess into an approval).
    approval = PendingSignalApproval.objects.create(
        source=source, message_id=mid,
        raw_payload={"raw_text": parsed.raw_text, "kind": parsed.kind,
                     "reason": getattr(parsed, "reason", "") or "not a tradeable signal"},
        status=PendingSignalApproval.Status.QUARANTINED,
    )
    _audit(actor, SignalAuditEvent.Event.SIGNAL_QUARANTINED, approval,
           message_id=mid, kind=parsed.kind)
    return approval


def intake_message(text: str, message_id: str = "", *, actor=None) -> PendingSignalApproval:
    """Parse a single raw Telegram message body and intake it."""
    return intake_parsed(parse_message(text, message_id), actor=actor)


def ingest_messages(messages, *, actor=None) -> dict:
    """Classify + intake a batch of {message_id, text} dicts (dedup-aware).

    Returns a summary; UPDATE messages (TP-hit/move-SL) are not new signals and
    are skipped. Creates NO ExecutionJob.
    """
    seen = set(
        PendingSignalApproval.objects.filter(source=SOURCE)
        .values_list("message_id", flat=True)
    )
    plan = classify_messages(messages, seen_ids=seen)
    created, quarantined = [], []
    for p in plan.signals:
        a = intake_parsed(p, actor=actor)
        created.append(a)
    for p in plan.quarantined:
        quarantined.append(intake_parsed(p, actor=actor))
    return {
        "created": created,
        "quarantined": quarantined,
        "updates_skipped": len(plan.updates),
        "duplicates_skipped": len(plan.duplicates),
    }


REVIEW_PERMISSION = "signal_intake.review_signals"


class ReviewPermissionDenied(Exception):
    """Raised when a caller without the ``review_signals`` permission attempts to
    approve/reject. A persisted APPROVAL_DENIED audit is written first (in
    autocommit, before the atomic mutation block, so it survives the raise)."""


def can_review(reviewer) -> bool:
    """FAIL-CLOSED reviewer check: an active user holding
    ``signal_intake.review_signals`` (superusers qualify via has_perm). None,
    inactive, unauthorised, or any error → False."""
    try:
        return bool(
            reviewer is not None
            and getattr(reviewer, "is_active", False)
            and reviewer.has_perm(REVIEW_PERMISSION)
        )
    except Exception:
        return False  # indeterminate permission state must deny, never allow


def _require_reviewer(reviewer, approval, action: str) -> None:
    if can_review(reviewer):
        return
    # Audit the refused attempt BEFORE raising (autocommit → persists).
    _audit(
        reviewer if getattr(reviewer, "pk", None) else None,
        SignalAuditEvent.Event.APPROVAL_DENIED, approval,
        action=action, reviewer=str(reviewer) if reviewer else "(none)",
    )
    raise ReviewPermissionDenied(
        f"{action} denied: reviewer {reviewer!r} lacks {REVIEW_PERMISSION}"
    )


def approve(approval: PendingSignalApproval, *, reviewer=None, notes="") -> PendingSignalApproval:
    """Approve a pending signal. SHADOW: records the decision only.

    Deliberately creates NO ExecutionJob and places NO order. The approved status
    is a human decision that a *future*, separately-gated bridge would act on.
    E3-APPROVAL-RBAC: requires the ``review_signals`` permission (fail-closed).
    """
    _require_reviewer(reviewer, approval, "approve")
    with transaction.atomic():
        approval.status = PendingSignalApproval.Status.APPROVED
        approval.reviewer = reviewer
        approval.reviewed_at = timezone.now()
        approval.review_notes = notes
        approval.save(update_fields=["status", "reviewer", "reviewed_at", "review_notes"])
        _audit(reviewer, SignalAuditEvent.Event.SIGNAL_APPROVED, approval, notes=notes)
    return approval


def reject(approval: PendingSignalApproval, *, reviewer=None, notes="") -> PendingSignalApproval:
    _require_reviewer(reviewer, approval, "reject")
    with transaction.atomic():
        approval.status = PendingSignalApproval.Status.REJECTED
        approval.reviewer = reviewer
        approval.reviewed_at = timezone.now()
        approval.review_notes = notes
        approval.save(update_fields=["status", "reviewer", "reviewed_at", "review_notes"])
        _audit(reviewer, SignalAuditEvent.Event.SIGNAL_REJECTED, approval, notes=notes)
    return approval
