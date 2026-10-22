"""WP7 — Broker email identity VERIFICATION + BINDING (DARK).

The missing *reusable* capability (dependency-register item B): a governed, auditable mechanism that transitions a
``BrokerEmailIdentity`` from PENDING to VERIFIED and binds it to its owner, trading-account and receiving mailbox —
replacing the one-off manual binding that produced the single existing certified row (TradersWay / account 46).

CONTRACT — a verification succeeds ONLY on COMPOSED, independent proof, and NEVER auto-verifies:

  1. MAILBOX CONTROL  — the claiming user owns a CONNECTED ``ConnectedMailbox`` whose gmail-canonical address equals
     the identity address. OAuth consent to read that inbox is the proof the user controls it. (Email/mailbox proof.)
  2. ACCOUNT OWNERSHIP — the target ``TradingAccount`` is owned by the SAME user in GuvFX's own records. Email
     ownership ALONE is explicitly NOT proof of trading-account ownership (Sponsor §2), so this is required *in
     addition* to (1).
  3. BROKER MATCH     — the identity's broker matches the trading-account's broker (an identity for broker X can only
     bind an account at broker X).
  4. SAME OWNER       — the mailbox owner, the account owner and the claimant are the same user (no cross-owner bind).
  5. NO CONFLICT      — no OTHER VERIFIED identity with a canonically-equivalent address is bound to a DIFFERENT user
     (one Gmail mailbox = one human), and the (email, broker) row, if it already exists VERIFIED, is not bound to a
     different account/user. Ambiguity ⇒ refuse (never guess, never overwrite a certified binding).

Any failing proof ⇒ REFUSED with a stable reason code, and the attempt is AUDITED (a refused cross-user attempt is
exactly what review must see). A success is idempotent: re-verifying an already-VERIFIED identity to the SAME
owner+account is a CONFIRM no-op (it may additively fill a missing receiving-mailbox link, never alter the certified
owner/account/status). Pure decision (:func:`evaluate`) is separated from the atomic audited write
(:func:`verify_identity`) so the policy is testable without the DB write path or the HTTP surface.

Read/observe + a single governed state transition only. NO execution / strategy / credential / MT5 authority; never a
financial action; never contacts a broker.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from django.db import transaction

from .models import BrokerEmailIdentity, BrokerEmailIdentityAudit, ConnectedMailbox
from .resolver import canonical_address


class VerificationRefused(Exception):
    """Raised when a verification cannot be granted. ``code`` is a stable machine reason; ``message`` is sanitised."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


# Stable, sanitised reason codes (never leak internal detail; safe to surface to the member/UI + audit).
REASON = {
    "mailbox_not_connected": "No connected mailbox of yours matches this address.",
    "account_not_owned": "That trading account is not one of yours.",
    "broker_mismatch": "This address is registered at a different broker than the selected account.",
    "cross_user_conflict": "This address is already verified to a different member.",
    "account_conflict": "This address is already verified to a different account at this broker.",
    "retired_identity": "This identity was retired for this broker; it cannot be re-verified here.",
    "invalid_input": "A broker, address and trading account are all required.",
}


@dataclass
class Decision:
    ok: bool
    code: str                                   # "ok" or a REASON key
    reason: str
    proofs: dict = field(default_factory=dict)
    mailbox: Optional[ConnectedMailbox] = None
    existing: Optional[BrokerEmailIdentity] = None   # the (email, broker) row if one already exists


def _broker_token(name: str) -> str:
    """First broker word, lowercased + alphanumeric-only (e.g. 'IS6 Technologies' → 'is6', 'TradersWay' → 'tradersway')."""
    tok = (name or "").strip().split()
    first = tok[0] if tok else ""
    return re.sub(r"[^a-z0-9]", "", first.lower())


def _broker_matches(identity_broker: str, account_broker: str) -> bool:
    """True iff the identity broker and the account broker refer to the same brokerage (token-prefix match, either
    direction). Deliberately lenient on surrounding words ('IS6 Technologies' ~ 'IS6Technologies-Live') but anchored
    on the first token so 'TradersWay' can never match 'IS6…'."""
    a, b = _broker_token(identity_broker), _broker_token(account_broker)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _owner_connected_mailbox(user, email: str) -> Optional[ConnectedMailbox]:
    """A CONNECTED mailbox owned by ``user`` whose gmail-canonical address equals ``email``'s. The proof the user
    controls the inbox the broker's mail is delivered to. Canonical equality unifies gmail.com/googlemail.com +
    dots/+tag (§5); a non-Google address compares bare. Owner-scoped (never another member's mailbox)."""
    target = canonical_address(email)
    if not target:
        return None
    for mb in ConnectedMailbox.objects.filter(user=user, status=ConnectedMailbox.Status.CONNECTED):
        if canonical_address(mb.primary_email) == target:
            return mb
    return None


def evaluate(user, email: str, broker_name: str, trading_account) -> Decision:
    """Compute the verification verdict WITHOUT writing. Pure of side effects (DB reads only). Returns a
    :class:`Decision` carrying every proof so the write path + the audit record the exact basis of the outcome."""
    email = (email or "").strip()
    broker_name = (broker_name or "").strip()
    if not email or not broker_name or trading_account is None:
        return Decision(False, "invalid_input", REASON["invalid_input"])

    canon = canonical_address(email)
    proofs: dict = {
        "email_canonical": canon,
        "broker": broker_name,
        "account_id": getattr(trading_account, "id", None),
    }

    # (1) MAILBOX CONTROL — owner-scoped CONNECTED mailbox matching the canonical address.
    mailbox = _owner_connected_mailbox(user, email)
    proofs["mailbox_control"] = mailbox is not None
    proofs["mailbox_id"] = getattr(mailbox, "id", None)

    # (2) ACCOUNT OWNERSHIP — the GuvFX trading-account record is the claimant's own.
    account_owned = getattr(trading_account, "user_id", None) == getattr(user, "id", None)
    proofs["account_owned"] = account_owned

    # (3) BROKER MATCH — identity broker ~ account broker.
    broker_match = _broker_matches(broker_name, getattr(trading_account, "broker_name", "") or "")
    proofs["broker_match"] = broker_match

    # (4) SAME OWNER — all three agree (implied by owner-scoped (1)+(2), asserted explicitly, fail-closed).
    same_owner = bool(mailbox) and account_owned and getattr(mailbox, "user_id", None) == getattr(user, "id", None)
    proofs["same_owner"] = same_owner

    # (5) NO CONFLICT — no VERIFIED canonically-equivalent address bound to a DIFFERENT member (one Gmail = one human),
    # and no same-(email,broker) VERIFIED row bound to a different account. Compared CANONICALLY so dotted/+tag/
    # googlemail spellings cannot evade the firewall.
    existing = BrokerEmailIdentity.objects.filter(email=email, broker_name=broker_name).first()
    proofs["existing_identity_id"] = getattr(existing, "id", None)
    conflict_code = None
    for other in BrokerEmailIdentity.objects.filter(status=BrokerEmailIdentity.Status.VERIFIED,
                                                    user__isnull=False):
        if canonical_address(other.email) != canon:
            continue
        if other.user_id != getattr(user, "id", None):
            conflict_code = "cross_user_conflict"                       # same mailbox, different member → refuse
            break
        if (_broker_matches(broker_name, other.broker_name)
                and other.trading_account_id not in (None, getattr(trading_account, "id", None))):
            conflict_code = "account_conflict"                          # same member+broker, different account → refuse
            break
    proofs["conflict"] = conflict_code

    # Evidence corroboration (strengthening only, NEVER required — continuous ingestion is OFF): an owner-attributed
    # BrokerEvent whose broker-stated source/destination account equals this account's number.
    proofs["evidence_corroborated"] = _evidence_corroborated(user, trading_account)

    # First failing proof, in a deliberate order (cheapest/most-specific first).
    if not mailbox:
        return Decision(False, "mailbox_not_connected", REASON["mailbox_not_connected"], proofs, None, existing)
    if not account_owned:
        return Decision(False, "account_not_owned", REASON["account_not_owned"], proofs, mailbox, existing)
    if not broker_match:
        return Decision(False, "broker_mismatch", REASON["broker_mismatch"], proofs, mailbox, existing)
    if not same_owner:
        return Decision(False, "cross_user_conflict", REASON["cross_user_conflict"], proofs, mailbox, existing)
    if conflict_code:
        return Decision(False, conflict_code, REASON[conflict_code], proofs, mailbox, existing)
    return Decision(True, "ok", "Verified.", proofs, mailbox, existing)


def _evidence_corroborated(user, trading_account) -> bool:
    """True iff an owner-attributed BrokerEvent for ``user`` states a source/destination account equal to this
    account's number. Read-only; a bonus signal recorded in the proofs, never a precondition."""
    acct_no = str(getattr(trading_account, "account_number", "") or "").strip()
    if not acct_no:
        return False
    from .models import BrokerEvent
    from django.db.models import Q
    return BrokerEvent.objects.filter(
        Q(source_account=acct_no) | Q(destination_account=acct_no), owner=user).exists()


def verify_identity(*, user, email: str, broker_name: str, trading_account, actor: str = "") -> BrokerEmailIdentity:
    """Verify + bind (or CONFIRM an already-verified identity). Audited unconditionally. Raises
    :class:`VerificationRefused` (after DURABLY writing a REJECT audit) when the composed proof fails. Idempotent: a
    re-verify to the SAME owner+account is a CONFIRM that never alters the certified owner/account/status (it may
    additively fill a missing receiving-mailbox link). NEVER overwrites a binding to a different owner/account.

    Every refusal — the pre-lock proof failure AND a refusal discovered UNDER the row lock (TOCTOU: the row became
    VERIFIED to a different owner/account, or RETIRED, after :func:`evaluate`) — writes a DURABLE REJECT audit
    OUTSIDE any atomic block that then rolls back, so a refused (e.g. cross-user) attempt is always retained for
    review. The SUCCESS mutation + its audit are wrapped together in one atomic block under a ``select_for_update``
    row lock; the preservation invariant is re-checked under that lock, so a certified binding can never be
    re-pointed even under a race."""
    email = (email or "").strip()
    broker_name = (broker_name or "").strip()
    d = evaluate(user, email, broker_name, trading_account)
    actor = actor or _user_label(user)
    uid = getattr(user, "id", None)
    aid = getattr(trading_account, "id", None)

    def _reject(identity, code, before):
        """Durable refusal audit (written outside the lock) + raise."""
        _audit(BrokerEmailIdentityAudit.Action.REJECT, identity=identity, subject_user=user, actor=actor,
               email=email, broker_name=broker_name, trading_account=trading_account, mailbox=d.mailbox,
               outcome=code, reason=REASON.get(code, code), before=before or "", after=before or "", proofs=d.proofs)
        raise VerificationRefused(code, REASON.get(code, code))

    if not d.ok:
        _reject(d.existing, d.code, getattr(d.existing, "status", ""))

    # SUCCESS-OR-PRESERVE under a row lock. A refusal found under the lock is DEFERRED (not written inside the
    # atomic, which would roll back its own audit) and raised durably after the block.
    deferred = None    # (identity, code, before)
    with transaction.atomic():
        # P0 (ADR-0054): serialise this owner-scoped identity write against governed erasure on the per-owner lock.
        from .erasure_lock import acquire_owner_erasure_lock
        acquire_owner_erasure_lock(getattr(user, "id", None))
        # get-or-create the (email, broker) row (uniqueness guarantees at most one). select_for_update serialises
        # concurrent verifies of the same row.
        existing = (BrokerEmailIdentity.objects.select_for_update()
                    .filter(email=email, broker_name=broker_name).first())
        if existing is None:
            identity = BrokerEmailIdentity.objects.create(
                email=email, broker_name=broker_name, user=user, trading_account=trading_account,
                connected_mailbox=d.mailbox, status=BrokerEmailIdentity.Status.VERIFIED,
                origin=BrokerEmailIdentity.Origin.BROKER_REGISTERED)
            _audit(BrokerEmailIdentityAudit.Action.VERIFY, identity=identity, subject_user=user, actor=actor,
                   email=email, broker_name=broker_name, trading_account=trading_account, mailbox=d.mailbox,
                   outcome="ok", reason="Verified + bound.", before="", after=identity.status, proofs=d.proofs)
            return identity

        before = existing.status
        if existing.status == BrokerEmailIdentity.Status.VERIFIED:
            if existing.user_id == uid and existing.trading_account_id == aid:
                # CONFIRM — preserve the certified binding untouched; only ADD the mailbox link if still missing.
                filled = False
                if existing.connected_mailbox_id is None and d.mailbox is not None:
                    existing.connected_mailbox = d.mailbox
                    existing.save(update_fields=["connected_mailbox", "updated_at"])
                    filled = True
                _audit(BrokerEmailIdentityAudit.Action.CONFIRM, identity=existing, subject_user=user, actor=actor,
                       email=email, broker_name=broker_name, trading_account=trading_account, mailbox=d.mailbox,
                       outcome="confirmed_mailbox_filled" if filled else "already_verified",
                       reason="Re-verify of an already-verified identity." + (" Receiving mailbox linked." if filled
                                                                              else ""),
                       before=before, after=existing.status, proofs=d.proofs)
                return existing
            # VERIFIED to a DIFFERENT owner/account — re-checked under the lock (TOCTOU-safe). Preserve, refuse.
            deferred = (existing, "cross_user_conflict" if existing.user_id != uid else "account_conflict", before)
        elif existing.status == BrokerEmailIdentity.Status.RETIRED:
            # A retired identity is never silently re-animated into a live binding.
            deferred = (existing, "retired_identity", before)
        else:
            # PENDING/UNRESOLVED row for the SAME owner+account → promote to VERIFIED and bind.
            existing.user = user
            existing.trading_account = trading_account
            existing.connected_mailbox = d.mailbox
            existing.status = BrokerEmailIdentity.Status.VERIFIED
            existing.save(update_fields=["user", "trading_account", "connected_mailbox", "status", "updated_at"])
            _audit(BrokerEmailIdentityAudit.Action.VERIFY, identity=existing, subject_user=user, actor=actor,
                   email=email, broker_name=broker_name, trading_account=trading_account, mailbox=d.mailbox,
                   outcome="ok", reason="Verified + bound (was %s)." % (before or "new"), before=before,
                   after=existing.status, proofs=d.proofs)
            return existing

    # Reached only when a refusal was deferred out of the lock (no mutation happened). Durable audit + raise.
    _reject(deferred[0], deferred[1], deferred[2])


def _user_label(user) -> str:
    return (getattr(user, "email", "") or getattr(user, "username", "") or f"user:{getattr(user, 'id', '?')}")[:128]


def audit_refusal(*, user, email: str = "", broker_name: str = "", code: str, reason: str = "", actor: str = "",
                  trading_account=None, proofs: Optional[dict] = None) -> None:
    """Write a DURABLE append-only REJECT audit for a refusal decided OUTSIDE :func:`verify_identity` — e.g. the API
    owner-scope pre-check that refuses a non-owned / non-existent ``trading_account_id`` (a refused CROSS-USER bind)
    before the service is reached, and the malformed-input refusal. The security contract requires EVERY refusal —
    and explicitly a refused cross-user attempt — to leave a reviewable record; the view short-circuits those before
    the audited service, so it calls this to keep the forensic trail complete. No binding is created or changed; no
    secret or raw body is recorded (only ids + a sanitised reason + a proof summary)."""
    _audit(BrokerEmailIdentityAudit.Action.REJECT, identity=None, subject_user=user,
           actor=actor or _user_label(user), email=email or "", broker_name=broker_name or "",
           trading_account=trading_account, mailbox=None, outcome=code,
           reason=reason or REASON.get(code, code), before="", after="", proofs=proofs or {})


def _audit(action, *, identity, subject_user, actor, email, broker_name, trading_account, mailbox,
           outcome, reason, before, after, proofs):
    # TOTAL sink: clamp every CharField to its column max_length so an oversized/pathological INPUT (the member
    # supplies the email/broker) can never raise a DB DataError that turns a clean refusal into a 500 AND drops the
    # audit row. The audit must record even a malformed attempt (security.md: auth rejections are logged, or
    # 'no auth errors' is not provable). proofs (JSONField) is unbounded and left as-is.
    BrokerEmailIdentityAudit.objects.create(
        identity=identity, action=action, subject_user=subject_user, actor=(actor or "")[:128],
        broker_name=(broker_name or "")[:64], email=(email or "")[:255], trading_account=trading_account,
        connected_mailbox=mailbox, outcome=(outcome or "")[:24], reason=(reason or "")[:255],
        status_before=(before or "")[:12], status_after=(after or "")[:12], proofs=proofs or {})
