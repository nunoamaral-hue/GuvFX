"""ADR-0091 — broker-NEUTRAL, STAFF-GATED manual withdrawal correlation.

When a broker's withdrawal emails carry NO stable transaction reference, the automatic reference-first / bounded-
heuristic engine cannot (and must not) merge the lifecycle messages — doing so on amount/currency/date/account hints
alone risks a double-count or a wrong merge. This module lets an explicit, named staff reviewer confirm that two or
more unknown-sender discovery candidates are ONE withdrawal lifecycle, and (on apply) project exactly ONE
``REQUESTED`` / in-progress Withdrawal. It is FAIL-CLOSED, idempotent, append-only and privacy-safe:

  * Staff identity required (active ``is_staff`` reviewer).
  * The broker is selected EXPLICITLY by the reviewer and validated (active + operator-verified) — NEVER inferred or
    back-filled; a candidate's own auth-approved registrable domain must match the selected broker.
  * Same tenant/owner; all candidates still NEW and not already correlated.
  * Any conflicting stable reference / amount / currency, any terminal-lifecycle candidate, or any ambiguous
    timestamp REFUSES the operation.
  * The resulting withdrawal is ALWAYS ``REQUESTED`` (never ``COMPLETED`` — "request successful" is not completion).
  * Deterministic idempotency key ⇒ replay creates no duplicate; a unique DB constraint makes it concurrency-safe.
  * No raw body, name, full account number or other PII is read or stored — only owner/broker FKs, the candidates'
    salted provider-message-id hashes, the staff actor, a sanitised reason and enums.

It NEVER moves money, contacts a broker, reads a mailbox, or touches an ingestion cursor.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

# Lifecycle classes (generic detector vocabulary) that are TERMINAL — a manual REQUESTED correlation refuses to
# include one (its evidence belongs to the automatic completion/terminal path, not an in-progress merge).
_TERMINAL_LIFECYCLES = {"COMPLETED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED"}


class CorrelationRefused(Exception):
    """A fail-closed refusal carrying a STABLE, PII-free reason code (safe to log / print / return)."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class CorrelationPlan:
    owner_id: int
    broker_profile: object
    ordered_candidates: list          # discovery candidates in trusted-timestamp order
    ordered_pmid_hashes: List[str]
    reviewer: str                     # "user:<id>"
    reason: str                       # sanitised
    idempotency_key: str
    requested_at: object              # earliest trusted timestamp
    amount: Optional[Decimal]
    currency: str


def _sanitise_reason(reason: str) -> str:
    """Strip control characters, collapse whitespace, bound length. The reason is operator-supplied free text; it must
    never carry control chars (log-injection) and is bounded so it cannot smuggle a large PII blob."""
    r = "".join(ch for ch in (reason or "") if ch == " " or (ch.isprintable() and ch not in "\r\n\t"))
    return " ".join(r.split())[:256]


def _idempotency_key(owner_id: int, broker_profile_id: int, pmid_hashes) -> str:
    payload = "swc:v1:%s:%s:%s" % (owner_id, broker_profile_id, ":".join(sorted(pmid_hashes)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence_facts(candidate) -> dict:
    """Resolve the amount/currency/reference ALREADY RECORDED (privacy-safe, redacted) for this candidate's retained
    evidence, if any. A pre-retention discovery candidate has no EvidenceBlob/event ⇒ all empty. Reads only the
    already-approved redacted BrokerEvent columns — never the raw body."""
    from .models import EvidenceBlob
    blob = EvidenceBlob.objects.filter(sha256=candidate.message_sha256).first()
    ev = blob.events.order_by("id").first() if blob is not None else None
    if ev is None:
        return {"amount": None, "currency": "", "reference": ""}
    return {"amount": ev.amount, "currency": (ev.currency or ""), "reference": (ev.broker_reference_id or "").strip()}


def plan_correlation(*, candidate_ids, broker_profile_id, approved_by, reason) -> CorrelationPlan:
    """Validate a proposed manual correlation FAIL-CLOSED and return an admissible plan. Pure read-only (no mutation);
    raises ``CorrelationRefused`` with a stable code on any violation."""
    from django.contrib.auth import get_user_model
    from .models import (UnknownSenderDiscoveryCandidate as C, BrokerProfile, StaffWithdrawalCorrelation)

    # 1) Reviewer must be an ACTIVE STAFF user.
    U = get_user_model()
    key = (approved_by or "").strip()
    reviewer = (U.objects.filter(email__iexact=key).first() or U.objects.filter(username=key).first()) if key else None
    if reviewer is None or not reviewer.is_active or not reviewer.is_staff:
        raise CorrelationRefused("REVIEWER_NOT_STAFF", "--approved-by must be an active staff user")

    # 2) At least two DISTINCT, existing candidate ids.
    raw = list(candidate_ids or [])
    ids = list(dict.fromkeys(raw))
    if len(ids) != len(raw):
        raise CorrelationRefused("DUPLICATE_CANDIDATE_ID", "a candidate id was supplied more than once")
    if len(ids) < 2:
        raise CorrelationRefused("TOO_FEW_CANDIDATES", "at least two candidates are required")
    cands = list(C.objects.filter(id__in=ids))
    if len(cands) != len(ids):
        raise CorrelationRefused("CANDIDATE_NOT_FOUND", "a candidate id does not exist")

    # 3) Broker profile: EXPLICIT, exists, active, operator-verified (never inferred).
    bp = BrokerProfile.objects.filter(id=broker_profile_id).first()
    if bp is None:
        raise CorrelationRefused("BROKER_PROFILE_NOT_FOUND", "--broker-profile-id does not exist")
    if not bp.is_active:
        raise CorrelationRefused("BROKER_PROFILE_INACTIVE", "the selected broker profile is not active")
    if str(getattr(bp, "provenance", "")) != "OPERATOR_VERIFIED":
        raise CorrelationRefused("BROKER_PROFILE_NOT_OPERATOR_VERIFIED", "the selected broker profile is not operator-verified")
    bp_domains = {d.strip().lower() for d in (bp.approved_sender_domains or "").split(",") if d.strip()}

    # 4) Same owner (tenant).
    owners = {c.owner_id for c in cands}
    if len(owners) != 1 or None in owners:
        raise CorrelationRefused("CROSS_TENANT", "candidates do not share exactly one owner")
    owner_id = next(iter(owners))

    # 5) Eligibility: all NEW and not already in an applied correlation.
    for c in cands:
        if c.review_state != C.ReviewState.NEW:
            raise CorrelationRefused("CANDIDATE_NOT_ELIGIBLE", f"candidate {c.id} is not NEW")
    if StaffWithdrawalCorrelation.objects.filter(
            candidates__in=cands, status=StaffWithdrawalCorrelation.Status.APPLIED).exists():
        raise CorrelationRefused("CANDIDATE_ALREADY_CORRELATED", "a candidate is already in an applied correlation")

    # 6) BROKER-BINDING (fail-closed; the CRITICAL rule): each candidate's auth-approved registrable domain must be a
    # sender domain of the EXPLICITLY selected profile, and no candidate may already be bound to a DIFFERENT profile.
    for c in cands:
        dom = (c.registrable_domain or "").strip().lower()
        if not dom:
            raise CorrelationRefused("CANDIDATE_DOMAIN_UNKNOWN", f"candidate {c.id} has no auth-approved domain")
        if dom not in bp_domains:
            raise CorrelationRefused("BROKER_MISATTRIBUTION", f"candidate {c.id} domain is not a sender of the selected broker")
        if c.associated_broker_profile_id not in (None, bp.id):
            raise CorrelationRefused("CONFLICTING_BROKER_ASSOCIATION", f"candidate {c.id} is bound to a different broker profile")
    if len({(c.registrable_domain or "").strip().lower() for c in cands}) != 1:
        raise CorrelationRefused("CONFLICTING_BROKER_EVIDENCE", "candidates carry different sender domains")

    # 7) No terminal-lifecycle candidate (keeps completion out of a REQUESTED merge).
    for c in cands:
        if (c.lifecycle_class or "").upper() in _TERMINAL_LIFECYCLES:
            raise CorrelationRefused("TERMINAL_LIFECYCLE_CANDIDATE", f"candidate {c.id} is terminal; refuse a REQUESTED merge")

    # 8) Conflicting recorded reference / amount / currency ⇒ fail closed (never guess-merge).
    facts = [_evidence_facts(c) for c in cands]
    if len({f["reference"] for f in facts if f["reference"]}) > 1:
        raise CorrelationRefused("CONFLICTING_REFERENCE", "candidates carry different stable references")
    amts = {(f["amount"], f["currency"]) for f in facts if f["amount"] is not None and f["currency"]}
    if len(amts) > 1:
        raise CorrelationRefused("CONFLICTING_AMOUNT_CURRENCY", "candidates carry different amount/currency")

    # 9) Order by TRUSTED timestamp (received_at, else observed_at) — never by candidate id. Earliest is requested_at.
    def _ts(c):
        return c.received_at or c.observed_at
    if any(_ts(c) is None for c in cands):
        raise CorrelationRefused("AMBIGUOUS_TIMESTAMP", "a candidate has no trusted timestamp")
    ordered = sorted(cands, key=_ts)

    amount, currency = (next(iter(amts)) if amts else (None, ""))
    return CorrelationPlan(
        owner_id=owner_id, broker_profile=bp, ordered_candidates=ordered,
        ordered_pmid_hashes=[c.provider_message_id_hash for c in ordered],
        reviewer=f"user:{reviewer.id}", reason=_sanitise_reason(reason),
        idempotency_key=_idempotency_key(owner_id, bp.id, [c.provider_message_id_hash for c in cands]),
        requested_at=_ts(ordered[0]), amount=amount, currency=currency)


def apply_correlation(plan: CorrelationPlan):
    """Atomically create AT MOST ONE ``REQUESTED`` withdrawal + the append-only decision, mark the candidates
    CORRELATED, and link full provenance. Idempotent: a replay (same idempotency key) is a no-op returning the
    existing decision. Returns ``(decision, created: bool)``. Never COMPLETED, never money, never a cursor."""
    from django.db import transaction, IntegrityError
    from .models import (StaffWithdrawalCorrelation, Withdrawal, Provenance,
                         UnknownSenderDiscoveryCandidate as C)

    existing = StaffWithdrawalCorrelation.objects.filter(idempotency_key=plan.idempotency_key).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            locked = list(C.objects.select_for_update().filter(
                id__in=[c.id for c in plan.ordered_candidates]).order_by("id"))
            # A concurrent winner may have created THIS decision (and marked these candidates CORRELATED) while we
            # waited for the lock. Re-check the idempotency key FIRST, so the loser returns the idempotent no-op rather
            # than a spurious CANDIDATE_NOT_ELIGIBLE.
            won = StaffWithdrawalCorrelation.objects.filter(idempotency_key=plan.idempotency_key).first()
            if won is not None:
                return won, False
            for c in locked:                      # genuine TOCTOU (a DIFFERENT op changed a candidate) -> refuse
                if c.review_state != C.ReviewState.NEW:
                    raise CorrelationRefused("CANDIDATE_NOT_ELIGIBLE", f"candidate {c.id} changed state")
            w = Withdrawal.objects.create(
                owner_id=plan.owner_id, trading_account=None, broker_reference_id="",
                amount=plan.amount, currency=plan.currency or "",
                status=Withdrawal.Status.REQUESTED, requested_at=plan.requested_at,
                correlation_method=Withdrawal.CorrelationMethod.MANUAL_STAFF, provenance=Provenance.REAL)
            dec = StaffWithdrawalCorrelation.objects.create(
                owner_id=plan.owner_id, broker_profile=plan.broker_profile,
                ordered_candidate_pmid_hashes=list(plan.ordered_pmid_hashes), lifecycle_result="REQUESTED",
                reviewer=plan.reviewer, reason=plan.reason, idempotency_key=plan.idempotency_key,
                resulting_withdrawal=w, status=StaffWithdrawalCorrelation.Status.APPLIED)
            dec.candidates.set([c.id for c in plan.ordered_candidates])
            for c in locked:
                c.review_state = C.ReviewState.CORRELATED
                if c.associated_broker_profile_id is None:
                    c.associated_broker_profile = plan.broker_profile
                c.save(update_fields=["review_state", "associated_broker_profile", "updated_at"])
            return dec, True
    except IntegrityError:
        # A concurrent apply won the unique idempotency key; our transaction rolled back (no duplicate withdrawal).
        existing = StaffWithdrawalCorrelation.objects.filter(idempotency_key=plan.idempotency_key).first()
        if existing is not None:
            return existing, False
        raise
