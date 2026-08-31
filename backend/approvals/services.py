"""A5 service layer: register a PENDING approval, decide it (human-gated), and consume it (fail-closed).

There is NO auto-approve. ``decide`` requires a staff user and a PENDING row; ``is_artefact_approved`` is what
provisioning calls and returns True only for an exact (kind, ref, sha256) APPROVED row AND only while the DARK
``APPROVALS_ENABLED`` gate is on — otherwise it fails closed to False.
"""
from __future__ import annotations

import re

from django.db import transaction

from .flags import approvals_enabled
from .models import ArtefactApproval

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _norm_sha(sha256: str) -> str:
    return str(sha256 or "").strip().lower()


def register_pending(*, artefact_kind: str, artefact_ref: str, sha256: str,
                     requested_by=None, metadata: dict | None = None) -> ArtefactApproval:
    """Idempotently register a PENDING approval for an exact artefact SHA. Never approves. Returns the row
    (existing or new). Raises ValueError on a malformed SHA (defends the exact-bytes binding)."""
    sha = _norm_sha(sha256)
    if not _SHA_RE.fullmatch(sha):
        raise ValueError("sha256 must be 64 lowercase hex chars")
    obj, _created = ArtefactApproval.objects.get_or_create(
        artefact_kind=str(artefact_kind)[:64], artefact_ref=str(artefact_ref)[:200], sha256=sha,
        defaults={"requested_by": requested_by, "metadata": dict(metadata or {})},
    )
    return obj


def decide(approval: ArtefactApproval, *, approve: bool, decided_by, reason: str = "") -> ArtefactApproval:
    """Human-gated PENDING -> APPROVED/REJECTED transition. ``decided_by`` MUST be a staff user (the only
    control path; model output can never reach here). Refuses a non-PENDING row (no re-decide)."""
    if decided_by is None or not getattr(decided_by, "is_staff", False):
        raise PermissionError("artefact approval requires a staff decider")
    with transaction.atomic():
        row = ArtefactApproval.objects.select_for_update().get(pk=approval.pk)
        if row.status != ArtefactApproval.Status.PENDING:
            raise ValueError(f"approval is not PENDING (is {row.status})")
        row.mark(approve=approve, decided_by=decided_by, reason=reason)
        row.save(update_fields=["status", "decided_by", "decided_at", "reason", "updated_at"])
    _emit_decision(row)
    return row


def is_artefact_approved(*, artefact_kind: str, artefact_ref: str, sha256: str) -> bool:
    """Fail-closed consumer for provisioning. True ONLY when the DARK gate is armed AND an exact-bytes APPROVED
    row exists. A malformed sha, a missing row, a PENDING/REJECTED row, or the gate being off -> False."""
    if not approvals_enabled():
        return False
    sha = _norm_sha(sha256)
    if not _SHA_RE.fullmatch(sha):
        return False
    return ArtefactApproval.objects.filter(
        artefact_kind=str(artefact_kind)[:64], artefact_ref=str(artefact_ref)[:200], sha256=sha,
        status=ArtefactApproval.Status.APPROVED,
    ).exists()


def _emit_decision(row: ArtefactApproval) -> None:
    """Record the decision on the operator timeline (fail-open, non-secret). Never raises into the caller."""
    try:
        from operational_events.constants import CATEGORY_SYSTEM
        from operational_events.events import record_event
        record_event(
            category=CATEGORY_SYSTEM, event_type="artefact_approval_decided",
            severity=("INFO" if row.status == ArtefactApproval.Status.APPROVED else "WARNING"),
            source="approvals", reason_code=row.status, customer_visible=False,
            actor=str(getattr(row.decided_by, "email", "") or ""),
            metadata={"artefact_kind": row.artefact_kind, "artefact_ref": row.artefact_ref,
                      "sha256_suffix": row.sha256[-12:]},
            dedup_key=f"artefact-approval:{row.pk}:{row.status}",
        )
    except Exception:  # noqa: BLE001 — telemetry must never break the decision
        pass
