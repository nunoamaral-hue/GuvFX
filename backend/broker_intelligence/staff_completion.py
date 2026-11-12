"""ADR-0093 — STAFF-GATED completion of a broker-neutral MANUAL_STAFF withdrawal.

ADR-0092 creates a ``REQUESTED`` / in-progress ``Withdrawal`` from two or more unknown-sender discovery candidates when
the broker's emails carry NO stable reference. By design that record stays ``trading_account``-unresolved and never
advances automatically — the autonomous correlation engine EXCLUDES ``MANUAL_STAFF`` rows (``correlation.py`` ADR-0092
invariant: "the engine never touches a staff record"). When the genuine COMPLETION email ("...has been processed
successfully") later arrives, nothing can advance the withdrawal: it is reference-less (the reference-only completion
path returns UNRESOLVED) and staff-owned (the heuristic path skips it).

This module lets a named staff reviewer complete ONE such withdrawal from the evidence that is ALREADY retained and
ALREADY linked to it (one of its ``StaffWithdrawalCorrelation`` candidates). It re-parses that candidate's immutable
stored blob with the current (ADR-0093-corrected) parser, requires a ``WITHDRAWAL_COMPLETED`` classification, resolves
the broker login to exactly ONE owner-scoped ``TradingAccount`` (fail-closed), and advances the withdrawal to
``COMPLETED`` with truthful anchors. It is FAIL-CLOSED, idempotent, append-only in spirit (it mutates only the single
mutable projection row, never evidence), and privacy-safe:

  * Staff identity required (active ``is_staff`` reviewer); withdrawal, broker profile, completion candidate, expected
    account suffix and expected amount are all supplied EXPLICITLY — never inferred.
  * The withdrawal MUST currently be ``REQUESTED`` + ``MANUAL_STAFF`` + ``provenance=REAL`` + account-UNRESOLVED +
    not-yet-completed, owned by the named owner; the completion candidate MUST be one of its APPLIED correlation's
    candidates and MUST post-date the request (bounded window).
  * The candidate's retained evidence MUST re-parse to ``WITHDRAWAL_COMPLETED`` for the selected broker, carry the
    expected amount, and state a login that resolves to EXACTLY ONE owner-scoped account whose suffix matches the
    expected suffix. Zero / multiple / cross-owner / wrong-broker / wrong-suffix ⇒ refuse (no mutation).
  * Currency stays whatever the evidence states (``""`` when unknown — NEVER invented). ``completed_at`` is the
    completion evidence's trusted timestamp (never ``now``); ``requested_at`` is preserved.
  * Idempotent: a re-run on an already-completed, correctly-resolved withdrawal is a no-op.

It NEVER moves money, contacts a broker, reads a mailbox, advances an ingestion cursor, creates a BrokerEvent, or
touches another withdrawal. Production execution is governed separately (Sponsor-gated).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Optional

# A completion must correlate to a REQUEST within this bounded window (a sanity bound on an explicitly-paired repair;
# the genuine Vantage gap is ~19s, normal broker processing is hours-to-days — 90 days is generous but not unbounded).
_MAX_COMPLETION_WINDOW = timedelta(days=90)


class CompletionRefused(Exception):
    """A fail-closed refusal carrying a STABLE, PII-free reason code (safe to log / print / return)."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class CompletionPlan:
    withdrawal_id: int
    owner_id: int
    broker_profile_id: int
    broker_display: str
    completion_candidate_id: int
    completion_sha12: str
    requested_at: object
    completion_at: object           # the completion candidate's trusted timestamp -> completed_at
    duration_seconds: object        # int, or None when it cannot be derived (never a fabricated 0)
    amount: Decimal
    currency: str                   # the currency apply() will PERSIST (the prior value if set, else the evidence's)
    prior_amount: object            # the withdrawal's amount BEFORE apply (for a truthful before/rollback)
    prior_currency: str
    resolved_account_id: int
    resolved_account_suffix: str
    resolved_account_last4: str     # the RESOLVED account's real last-4 (for masking, independent of the suffix arg)
    reviewer: str                   # "user:<id>"
    already_completed: bool = False


def _trusted_ts(candidate):
    return candidate.received_at or candidate.observed_at


def plan_staff_completion(*, withdrawal_id, owner, broker_profile_id, completion_candidate_id,
                          expected_account_suffix, expected_amount, approved_by) -> CompletionPlan:
    """Validate the staff completion FAIL-CLOSED and return an admissible plan. Pure read-only; raises
    ``CompletionRefused`` with a stable code on any violation. ``owner`` is a resolved User; ``expected_amount`` a
    Decimal; ``expected_account_suffix`` a short digit string the resolved login must end with."""
    from django.contrib.auth import get_user_model
    from .models import (Withdrawal, StaffWithdrawalCorrelation, UnknownSenderDiscoveryCandidate as C,
                         BrokerProfile, EvidenceBlob)
    from .evidence import EvidenceStore, EvidenceStoreError
    from .gmail_source import parse_rfc822
    from .broker_senders import is_allowlisted_broker_sender
    from .parsers import select_parser
    from . import parsers_tradersway as _tw
    from .resolver import resolve_transaction_account

    # 1) Reviewer must be an ACTIVE STAFF user.
    U = get_user_model()
    key = (approved_by or "").strip()
    reviewer = (U.objects.filter(email__iexact=key).first() or U.objects.filter(username=key).first()) if key else None
    if reviewer is None or not reviewer.is_active or not reviewer.is_staff:
        raise CompletionRefused("REVIEWER_NOT_STAFF", "--approved-by must be an active staff user")

    # 2) Withdrawal exists and is owned by the named owner.
    wd = Withdrawal.objects.filter(id=withdrawal_id).select_related("trading_account").first()
    if wd is None:
        raise CompletionRefused("WITHDRAWAL_NOT_FOUND", f"withdrawal id={withdrawal_id} does not exist")
    if wd.owner_id != owner.id:
        raise CompletionRefused("OWNER_MISMATCH", "withdrawal owner does not match --owner")

    # 2a) The expected account suffix is a REQUIRED safety cross-check — never allow it to be blank (which would
    #     silently disable the suffix verification and mask the account as a bare '••••').
    suffix = (expected_account_suffix or "").strip()
    if not suffix:
        raise CompletionRefused("EXPECTED_SUFFIX_REQUIRED", "--expected-account-suffix must be a non-empty digit suffix")

    # 2b) Idempotency: an already-completed, account-resolved withdrawal is a NO-OP (not a refusal).
    if wd.status == Withdrawal.Status.COMPLETED and wd.trading_account_id is not None and wd.completed_at is not None:
        acctnum = (wd.trading_account.account_number or "") if wd.trading_account_id else ""
        return CompletionPlan(
            withdrawal_id=wd.id, owner_id=owner.id, broker_profile_id=broker_profile_id, broker_display="",
            completion_candidate_id=completion_candidate_id, completion_sha12="", requested_at=wd.requested_at,
            completion_at=wd.completed_at, duration_seconds=wd.duration_seconds(), amount=wd.amount,
            currency=wd.currency or "", prior_amount=wd.amount, prior_currency=wd.currency or "",
            resolved_account_id=wd.trading_account_id, resolved_account_suffix=acctnum[-len(suffix):],
            resolved_account_last4=acctnum[-4:], reviewer=f"user:{reviewer.id}", already_completed=True)

    # 3) Expected current state (fail-closed preconditions).
    if wd.status != Withdrawal.Status.REQUESTED:
        raise CompletionRefused("WITHDRAWAL_NOT_REQUESTED", f"status is {wd.status}, expected REQUESTED")
    if wd.correlation_method != Withdrawal.CorrelationMethod.MANUAL_STAFF:
        raise CompletionRefused("WITHDRAWAL_NOT_MANUAL_STAFF", f"correlation_method is {wd.correlation_method!r}")
    if str(wd.provenance) != "REAL":
        raise CompletionRefused("WITHDRAWAL_NOT_REAL", f"provenance is {wd.provenance!r}, expected REAL")
    if wd.trading_account_id is not None:
        raise CompletionRefused("ACCOUNT_ALREADY_RESOLVED", "trading_account is already set; expected UNRESOLVED")
    if wd.completed_at is not None or wd.completed_event_id is not None:
        raise CompletionRefused("ALREADY_HAS_COMPLETION", "withdrawal already carries a completion anchor/event")
    if wd.requested_at is None:
        raise CompletionRefused("NO_REQUESTED_ANCHOR", "withdrawal has no requested_at to order against")

    # 4) Exactly one APPLIED staff correlation for this withdrawal; its broker must be the EXPLICIT --broker-profile-id.
    decisions = list(StaffWithdrawalCorrelation.objects.filter(
        resulting_withdrawal_id=wd.id, status=StaffWithdrawalCorrelation.Status.APPLIED))
    if len(decisions) != 1:
        raise CompletionRefused("CORRELATION_NOT_UNIQUE", f"{len(decisions)} APPLIED correlations for this withdrawal")
    dec = decisions[0]
    bp = BrokerProfile.objects.filter(id=broker_profile_id).first()
    if bp is None:
        raise CompletionRefused("BROKER_PROFILE_NOT_FOUND", "--broker-profile-id does not exist")
    if dec.broker_profile_id != bp.id:
        raise CompletionRefused("BROKER_PROFILE_MISMATCH", "selected broker does not match the withdrawal's correlation")

    # 5) The completion candidate MUST be one of this correlation's candidates, and MUST post-date the request.
    cand = dec.candidates.filter(id=completion_candidate_id).first()
    if cand is None:
        raise CompletionRefused("CANDIDATE_NOT_LINKED", "--completion-candidate-id is not a candidate of this correlation")
    cts = _trusted_ts(cand)
    if cts is None:
        raise CompletionRefused("CANDIDATE_NO_TIMESTAMP", "completion candidate has no trusted timestamp")
    if cts < wd.requested_at:
        raise CompletionRefused("COMPLETION_BEFORE_REQUEST", "completion evidence predates the request")
    if (cts - wd.requested_at) > _MAX_COMPLETION_WINDOW:
        raise CompletionRefused("COMPLETION_WINDOW_EXCEEDED", "completion is outside the bounded correlation window")

    # 6) Re-parse the candidate's IMMUTABLE stored blob with the current parser (read-only; no event created).
    blob = EvidenceBlob.objects.filter(sha256=cand.message_sha256).first()
    if blob is None:
        raise CompletionRefused("EVIDENCE_NOT_RETAINED", "no retained evidence blob for this candidate")
    _tw.register_default_parsers()
    try:
        raw = EvidenceStore().read(blob)
    except (EvidenceStoreError, OSError):
        raise CompletionRefused("EVIDENCE_UNREADABLE", "stored evidence could not be read (integrity/availability)")
    d = parse_rfc822(raw)
    subject, body, from_address = d.get("subject", ""), d.get("body", ""), d.get("from_address", "")
    if not is_allowlisted_broker_sender(from_address):
        raise CompletionRefused("SENDER_NOT_ALLOWLISTED", "evidence sender is not an allowlisted broker")
    if d.get("auth_verdict") != "pass":
        raise CompletionRefused("EVIDENCE_NOT_AUTHENTICATED", "evidence is not authenticated (auth_verdict != pass)")
    parser = select_parser(subject=subject, body=body, from_address=from_address)
    parsed = parser.parse(subject=subject, body=body, from_address=from_address) if parser else None
    if parsed is None or parsed.event_type != "WITHDRAWAL_COMPLETED":
        raise CompletionRefused("EVIDENCE_NOT_A_COMPLETION",
                                f"evidence re-parses to {getattr(parsed, 'event_type', None)}, not WITHDRAWAL_COMPLETED")
    if (parsed.broker or "").strip().lower() != (bp.name or "").strip().lower():
        raise CompletionRefused("COMPLETION_BROKER_MISMATCH", "completion broker does not match the selected profile")

    # 7) Amount: parsed must be present and equal the operator-asserted expected amount (and any existing value).
    if parsed.amount is None:
        raise CompletionRefused("AMOUNT_NOT_EVIDENCED", "completion carries no amount")
    try:
        exp_amt = Decimal(str(expected_amount))
    except (ArithmeticError, ValueError, TypeError):
        raise CompletionRefused("EXPECTED_AMOUNT_INVALID", "--expected-amount is not a number")
    if parsed.amount != exp_amt:
        raise CompletionRefused("AMOUNT_MISMATCH", "parsed amount != --expected-amount")
    if wd.amount is not None and wd.amount != parsed.amount:
        raise CompletionRefused("AMOUNT_CONFLICTS_WITHDRAWAL", "parsed amount conflicts with the existing withdrawal amount")

    # 8) Account resolution: the broker-stated login must resolve to EXACTLY ONE owner-scoped account whose suffix
    #    matches the expected suffix (never cross-owner, never suffix-only — resolve_transaction_account matches the
    #    FULL account_number owner-scoped and returns None on 0/>1).
    if not (parsed.source_account or "").strip():
        raise CompletionRefused("LOGIN_NOT_EVIDENCED", "completion states no account login")
    acct = resolve_transaction_account(owner, parsed.broker, [parsed.source_account])
    if acct is None:
        raise CompletionRefused("ACCOUNT_UNRESOLVED", "login does not resolve to exactly one owner-scoped account")
    acct_num = acct.account_number or ""
    if suffix and not acct_num.endswith(suffix):
        raise CompletionRefused("ACCOUNT_SUFFIX_MISMATCH", "resolved account suffix != --expected-account-suffix")
    tok = (parsed.broker or "").strip().split()
    if tok and tok[0].lower() not in (acct.broker_name or "").lower():
        raise CompletionRefused("ACCOUNT_BROKER_MISMATCH", "resolved account broker does not match the completion broker")

    # Currency to PERSIST: keep a currency the withdrawal already carries; otherwise adopt the one the evidence
    # states (which is '' when the broker does not state one — never invented).
    result_currency = (wd.currency or "") or (parsed.currency or "")
    return CompletionPlan(
        withdrawal_id=wd.id, owner_id=owner.id, broker_profile_id=bp.id, broker_display=bp.name,
        completion_candidate_id=cand.id, completion_sha12=(cand.message_sha256 or "")[:12],
        requested_at=wd.requested_at, completion_at=cts, duration_seconds=int((cts - wd.requested_at).total_seconds()),
        amount=parsed.amount, currency=result_currency, prior_amount=wd.amount, prior_currency=(wd.currency or ""),
        resolved_account_id=acct.id, resolved_account_suffix=acct_num[-len(suffix):],
        resolved_account_last4=acct_num[-4:], reviewer=f"user:{reviewer.id}")


def apply_staff_completion(plan: CompletionPlan):
    """Atomically advance the single MANUAL_STAFF withdrawal to COMPLETED under a row lock, re-validating the
    fail-closed preconditions inside the lock (TOCTOU). Mutates ONLY the mutable projection row (never evidence, never
    another withdrawal). Returns ``(withdrawal, changed: bool)``; a replay is a no-op (changed=False)."""
    from django.db import transaction
    from django.utils import timezone
    from .models import Withdrawal

    if plan.already_completed:
        return Withdrawal.objects.get(id=plan.withdrawal_id), False
    with transaction.atomic():
        wd = Withdrawal.objects.select_for_update().get(id=plan.withdrawal_id)
        if wd.status == Withdrawal.Status.COMPLETED and wd.trading_account_id == plan.resolved_account_id:
            return wd, False
        # Re-validate the exact preconditions under the lock (a concurrent op may have changed the row).
        if (wd.status != Withdrawal.Status.REQUESTED
                or wd.correlation_method != Withdrawal.CorrelationMethod.MANUAL_STAFF
                or wd.trading_account_id is not None or wd.completed_at is not None
                or wd.completed_event_id is not None or wd.owner_id != plan.owner_id):
            raise CompletionRefused("PRECONDITION_CHANGED", "withdrawal state changed before apply (no mutation)")
        wd.trading_account_id = plan.resolved_account_id
        wd.status = Withdrawal.Status.COMPLETED
        wd.completed_at = plan.completion_at
        wd.last_status_change_at = timezone.now()
        fields = ["trading_account", "status", "completed_at", "last_status_change_at", "updated_at"]
        if wd.amount is None:
            wd.amount = plan.amount
            fields.append("amount")
        # Currency: persist the evidence currency ONLY when the withdrawal has none AND the evidence states one;
        # otherwise leave it as-is (unknown stays '' — never invented; a known value is never overwritten).
        if not (wd.currency or "") and plan.currency:
            wd.currency = plan.currency
            fields.append("currency")
        wd.save(update_fields=fields)
        return wd, True
