"""ADR-0088 — deterministic, privacy-safe GENERIC withdrawal-email detection (NO LLM, no I/O in the core).

Runs ONLY after a specific parser misses (``select_parser`` → None) and ONLY for a canary owner. It never
overrides a specific adapter. Given the message signals it returns a ``GenericDetection`` with a fail-closed
disposition. The core ``detect`` is PURE (no DB, no network): the caller supplies the enabled patterns, the
sender-allowlist verdict and the authenticity verdict. All matching is deterministic anchored substring/regex.

PRIVACY: this module extracts ONLY redacted structural signals (reason-code enums, an amount/currency when a
currency is adjacent, a MASKED account last-4, a candidate broker name derived from the authenticated domain).
It never emits a raw name, full account number, email address, URL/token or message body.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Sequence, Tuple

from .parsers import ParsedBrokerEvent
from .redaction import contains_sensitive

# ---- dispositions ------------------------------------------------------------------------------------
KNOWN_BROKER_VERIFIED = "KNOWN_BROKER_VERIFIED"
GENERIC_HIGH_CONFIDENCE = "GENERIC_HIGH_CONFIDENCE"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
QUARANTINED = "QUARANTINED"
NOT_WITHDRAWAL = "NOT_WITHDRAWAL"

# ---- reason codes (structural; never raw text) ------------------------------------------------------
RC_NO_INTENT = "no_withdrawal_intent"
RC_NEGATIVE = "negative_class"                 # deposit/marketing/verification/margin/trade-alert
RC_AUTH_FAIL = "authentication_failed"
RC_CONTRADICTORY_LIFECYCLE = "contradictory_lifecycle"
RC_NO_EXPLICIT_LIFECYCLE = "no_explicit_lifecycle"
RC_SENDER_NOT_ALLOWLISTED = "sender_not_allowlisted"
RC_NO_MATCHING_PATTERN = "no_matching_pattern"
RC_PATTERN_MATCHED = "pattern_matched"
RC_INTENT = "withdrawal_intent"
RC_EXPLICIT_LIFECYCLE = "explicit_lifecycle"
RC_AMOUNT_PRESENT = "amount_currency_extracted"
RC_SUFFIX_PRESENT = "account_suffix_extracted"
RC_UNSAFE_EVIDENCE = "unsafe_evidence_in_field"
RC_COMPLETION_EXPLICIT = "explicit_completion_phrase"

# ---- proposed lifecycle states (candidate vocabulary; NOT Withdrawal statuses) ----------------------
S_REQUESTED = "REQUESTED"
S_CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
S_PROCESSING = "PROCESSING"
S_COMPLETED = "COMPLETED"
S_FAILED = "FAILED"
S_CANCELLED = "CANCELLED"
S_UNKNOWN = "UNKNOWN"

_STATE_TO_EVENT = {
    S_REQUESTED: "WITHDRAWAL_REQUESTED",
    S_CONFIRMATION_REQUIRED: "WITHDRAWAL_CONFIRMATION_REQUIRED",
    S_PROCESSING: "WITHDRAWAL_PROCESSING",
    S_COMPLETED: "WITHDRAWAL_COMPLETED",
    S_FAILED: "WITHDRAWAL_FAILED",
    S_CANCELLED: "WITHDRAWAL_CANCELLED",
}

# ---- deterministic phrase sets (English + representative multilingual). Lowercased substring match. --
# Explicit withdrawal INTENT — never the bare token "withdrawal" alone is enough on its own to accept, but it
# is the necessary intent signal; acceptance additionally needs an explicit lifecycle phrase below.
_INTENT = (
    "withdraw", "withdrawal", "payout", "pay out", "cash out", "cash-out", "funds out",
    "retiro", "retrait", "auszahlung", "prelievo", "levantamento", "снятие", "вывод средств",
    "出金", "인출",
)
# Hard negatives — a message clearly about something else. Each is checked ONLY when NO withdrawal lifecycle
# phrase is present (a genuine 'confirm your withdrawal' must not be lost to the 'confirm' in a verify set).
_NEG_DEPOSIT = ("deposit received", "deposit completed", "funds added", "top up", "top-up", "einzahlung",
                "入金", "ingreso")
_NEG_MARKETING = ("newsletter", "unsubscribe", "webinar", "trade now", "bonus offer", "special offer",
                  "market update", "promotion", "sign up now", "limited time")
_NEG_VERIFY = ("verify your email", "confirm your email", "email verification", "activate your account",
               "verify your account", "reset your password")
_NEG_MARGIN = ("margin call", "stop out", "margin level", "low margin", "close to margin")
_NEG_TRADEALERT = ("order filled", "position closed", "trade executed", "price alert", "order executed")

# Lifecycle phrase sets. COMPLETED requires an EXPLICIT completion phrase (packet rule: "processed" ALONE is not
# completion; present/future tense is PROCESSING, never COMPLETED).
_COMPLETED = (
    # Completion-SPECIFIC, PAST-TENSE, fund-anchored wording ONLY. ADR-0088: present/future tense and non-fund
    # wording ("a code has been sent to your email", "will be transferred to your bank") must NEVER complete, so the
    # bare fragments "has been sent to" / "transferred to your" / "credited to your" / "has been paid" are excluded
    # in favour of fund-completion-anchored phrases.
    "has been completed", "withdrawal completed", "successfully withdrawn",
    "funds have been sent", "funds have been sent to", "has been paid out",
    "payment has been sent", "has been transferred to your", "has been credited to your",
    "has been sent to your bank", "has been sent to your card", "has been sent to your wallet",
    "we've processed your withdrawal", "we have processed your withdrawal",
    "withdrawal has been processed", "withdrawal was processed", "your withdrawal has been processed",
    "payout completed", "payout has been sent",
    # multilingual completion (completion-specific)
    "auszahlung abgeschlossen", "出金完了", "出金が完了", "인출 완료", "retiro completado",
    "retrait terminé", "prelievo completato",
)
_PROCESSING = (
    "is being processed", "being processed", "is now processing", "withdrawal in progress",
    "under review", "will be processed", "processing your withdrawal", "is in progress",
    "wird bearbeitet", "処理中", "처리 중",
)
_CONFIRMATION = (
    "confirm your withdrawal", "approve or cancel", "approve this withdrawal", "confirm this withdrawal",
    "authorise your withdrawal", "authorize your withdrawal", "confirmation required",
    "please confirm your withdrawal", "verify your withdrawal request",
)
_REQUESTED = (
    "we received your withdrawal", "withdrawal request received", "requested a withdrawal",
    "withdrawal requested", "received your withdrawal request", "your withdrawal request",
    # Submission / acceptance stage (ADR-0089 live observation: "Withdrawal Submitted" / "Withdrawal Request
    # Successful"). A submission or acceptance of the REQUEST is the opening request stage — conservative, NEVER
    # COMPLETED. Gated by the withdrawal-intent requirement so it cannot match an unrelated "submitted"/"successful".
    "withdrawal submitted", "withdrawal has been submitted", "has been submitted", "request submitted",
    "request has been submitted", "request was submitted", "withdrawal request submitted",
    "request successful", "request was successful", "withdrawal request successful", "request is successful",
    "successfully submitted", "request accepted", "request has been accepted", "withdrawal request accepted",
)
_CANCELLED = (
    "has been cancelled", "has been canceled", "withdrawal cancelled", "withdrawal canceled",
    "was cancelled", "was canceled",
)
_FAILED = (
    "has failed", "withdrawal failed", "could not be processed", "was declined", "was rejected",
    "has been rejected", "has been declined",
)

_SYMBOL_TO_ISO = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_ISO = ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "SEK", "NOK", "ZAR", "SGD", "HKD")
_NUM = r"\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_AMOUNT_RE = re.compile(
    r"(?P<sym>[$€£¥])\s?(?P<a1>" + _NUM + r")"
    r"|(?P<iso1>\b(?:" + "|".join(_ISO) + r"))\s?(?P<a2>" + _NUM + r")"
    r"|(?P<a3>" + _NUM + r")\s?(?P<iso2>\b(?:" + "|".join(_ISO) + r"))",
    re.IGNORECASE)
# Account last-4 ONLY (never the full number): 'ending 1234', 'account 99887766', 'account ****1234', '••••1234'.
# The leading ``\d*`` consumes any earlier digits of a bare full number so only the LAST 4 are ever captured.
_SUFFIX_RE = re.compile(
    r"(?:ending(?:\s+in)?|account(?:\s+number)?|acct|wallet|a/c)\D{0,16}\d*(\d{4})\b"
    r"|[*xX•·]{2,}\s?\d*(\d{4})\b",
    re.IGNORECASE)


@dataclass(frozen=True)
class GenericDetection:
    disposition: str
    proposed_state: str = S_UNKNOWN
    reason_codes: Tuple[str, ...] = ()
    confidence: Optional[Decimal] = None
    amount: Optional[Decimal] = None
    currency: str = ""
    broker_reference_id: str = ""
    masked_account_suffix: str = ""
    candidate_broker_name: str = ""
    matched_pattern: str = ""
    parsed_event: Optional[ParsedBrokerEvent] = None


def _norm(subject: str, body: str, html_text: str = "") -> str:
    return " ".join(x for x in (subject or "", body or "", html_text or "") if x).lower()


def _any(text: str, phrases: Sequence[str]) -> bool:
    return any(p in text for p in phrases)


def _classify_states(text: str) -> List[str]:
    """Every lifecycle state whose explicit wording appears. Deterministic; order-independent."""
    out = []
    if _any(text, _COMPLETED):
        out.append(S_COMPLETED)
    if _any(text, _FAILED):
        out.append(S_FAILED)
    if _any(text, _CANCELLED):
        out.append(S_CANCELLED)
    if _any(text, _PROCESSING):
        out.append(S_PROCESSING)
    if _any(text, _CONFIRMATION):
        out.append(S_CONFIRMATION_REQUIRED)
    if _any(text, _REQUESTED):
        out.append(S_REQUESTED)
    return out


_TERMINAL = {S_COMPLETED, S_FAILED, S_CANCELLED}
_STATE_RANK = {S_REQUESTED: 1, S_CONFIRMATION_REQUIRED: 2, S_PROCESSING: 3,
               S_CANCELLED: 4, S_FAILED: 4, S_COMPLETED: 5}


def _extract_amount(text: str) -> Tuple[Optional[Decimal], str]:
    m = _AMOUNT_RE.search(text)
    if not m:
        return None, ""
    if m.group("sym"):
        iso, num = _SYMBOL_TO_ISO.get(m.group("sym"), ""), m.group("a1")
    elif m.group("iso1"):
        iso, num = m.group("iso1").upper(), m.group("a2")
    else:
        iso, num = m.group("iso2").upper(), m.group("a3")
    try:
        amount = Decimal(num.replace(",", "").replace(" ", ""))
    except (InvalidOperation, AttributeError):
        return None, ""
    return amount, iso


def _extract_masked_suffix(text: str) -> str:
    m = _SUFFIX_RE.search(text)
    if not m:
        return ""
    last4 = m.group(1) or m.group(2) or ""
    return ("••••" + last4) if last4 else ""


def _broker_name_from_domain(domain: str) -> str:
    """A safe display name derived from the authenticated DOMAIN (never from the body): 'acme-fx.com' -> 'Acme Fx'."""
    d = (domain or "").strip().lower()
    if not d:
        return ""
    label = d.split(".")[0]
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", label) if w)[:64]


def _pattern_matches(pattern, domain: str, text: str) -> bool:
    """A registry pattern claims this message: its domain set covers the authenticated sender AND at least one of
    its subject/body indicators appears (or it declares none, relying on its domain claim)."""
    doms = pattern.get("domains") if isinstance(pattern, dict) else pattern.domains()
    if domain and doms and not any(domain == d or domain.endswith("." + d) for d in doms):
        return False
    inds = (pattern.get("subject_indicators", []) + pattern.get("body_indicators", [])) if isinstance(pattern, dict) \
        else (list(pattern.subject_indicators or []) + list(pattern.body_indicators or []))
    inds = [str(i).strip().lower() for i in inds if str(i).strip()]
    return (not inds) or _any(text, inds)


def detect(*, subject: str, body: str, from_domain: str, auth_verdict: Optional[str],
           sender_allowlisted: bool, patterns: Sequence = (), html_text: str = "",
           broker_reference_id: str = "") -> GenericDetection:
    """Pure deterministic detection. ``patterns`` are the ENABLED registry rows (model instances or dicts);
    ``sender_allowlisted`` is the WP3b allowlist verdict for the From domain; ``auth_verdict`` the SPF/DKIM/DMARC
    result. Fail-closed throughout."""
    text = _norm(subject, body, html_text)
    reasons: List[str] = []

    states = _classify_states(text)
    has_intent = _any(text, _INTENT) or bool(states)

    # 1) NOT a withdrawal: no intent, or a hard negative with NO withdrawal lifecycle wording.
    if not has_intent:
        return GenericDetection(NOT_WITHDRAWAL, reason_codes=(RC_NO_INTENT,))
    if not states and _any(text, _NEG_DEPOSIT + _NEG_MARKETING + _NEG_VERIFY + _NEG_MARGIN + _NEG_TRADEALERT):
        return GenericDetection(NOT_WITHDRAWAL, reason_codes=(RC_NEGATIVE,))
    reasons.append(RC_INTENT)

    # 2) Contradictory TERMINAL lifecycle wording => quarantine (never guess which terminal state).
    terminal_hits = [s for s in states if s in _TERMINAL]
    if len(terminal_hits) >= 2:
        return GenericDetection(QUARANTINED, reason_codes=(RC_CONTRADICTORY_LIFECYCLE,))

    # 3) Authentication gate — fail-closed. A withdrawal-looking message that is not authenticated is QUARANTINED
    #    (never an accepted event, regardless of tier).
    if (auth_verdict or "").strip().lower() != "pass":
        return GenericDetection(QUARANTINED, reason_codes=tuple(reasons + [RC_AUTH_FAIL]),
                                proposed_state=(max(states, key=lambda s: _STATE_RANK[s]) if states else S_UNKNOWN))

    # 4) Proposed state from explicit wording (most-advanced). No explicit lifecycle => REVIEW_REQUIRED.
    if not states:
        reasons.append(RC_NO_EXPLICIT_LIFECYCLE)
        return GenericDetection(REVIEW_REQUIRED, proposed_state=S_UNKNOWN, reason_codes=tuple(reasons),
                                confidence=Decimal("0.300"),
                                candidate_broker_name=_broker_name_from_domain(from_domain),
                                broker_reference_id=broker_reference_id or "")
    proposed = max(states, key=lambda s: _STATE_RANK[s])
    reasons.append(RC_EXPLICIT_LIFECYCLE)
    if proposed == S_COMPLETED:
        reasons.append(RC_COMPLETION_EXPLICIT)

    # 5) Safe extraction (redacted).
    amount, currency = _extract_amount(text)
    suffix = _extract_masked_suffix(text)
    if amount is not None:
        reasons.append(RC_AMOUNT_PRESENT)
    if suffix:
        reasons.append(RC_SUFFIX_PRESENT)

    # 6) Pattern match (registry) + sender allowlist decide Tier-1 auto-accept vs review.
    matched = ""
    broker_name = _broker_name_from_domain(from_domain)
    for p in patterns:
        if _pattern_matches(p, from_domain, text):
            if isinstance(p, dict):
                matched = f"{p.get('name','')}:{p.get('version',1)}"
                broker_name = (p.get("broker_display_name") or broker_name)[:64]
            else:
                matched = f"{p.name}:{p.version}"
                broker_name = (p.broker_display_name or broker_name)[:64]
            reasons.append(RC_PATTERN_MATCHED)
            break
    if not matched:
        reasons.append(RC_NO_MATCHING_PATTERN)
    if not sender_allowlisted:
        reasons.append(RC_SENDER_NOT_ALLOWLISTED)

    # Guard: never let an authorization URL/token reach a stored field (defence in depth; extraction already avoids it).
    for fld in (broker_name, currency, suffix, broker_reference_id or ""):
        if contains_sensitive(fld):
            return GenericDetection(QUARANTINED, reason_codes=tuple(reasons + [RC_UNSAFE_EVIDENCE]),
                                    proposed_state=proposed)

    # 7) Disposition.
    #    GENERIC_HIGH_CONFIDENCE requires: allowlisted sender (Tier 1) AND a matching enabled pattern AND an
    #    explicit lifecycle. Everything else that is a credible withdrawal is REVIEW_REQUIRED (never auto-accepted).
    if sender_allowlisted and matched:
        conf = Decimal("0.900") if (amount is not None or proposed in (S_COMPLETED, S_CONFIRMATION_REQUIRED)) \
            else Decimal("0.750")
        parsed = ParsedBrokerEvent(
            event_type=_STATE_TO_EVENT.get(proposed, "WITHDRAWAL_REQUESTED"),
            transaction_category="UNKNOWN",   # generic NEVER asserts EXTERNAL_WITHDRAWAL without certified evidence
            broker=broker_name, amount=amount, currency=currency,
            broker_reference_id=broker_reference_id or "", confidence=conf)
        return GenericDetection(GENERIC_HIGH_CONFIDENCE, proposed_state=proposed, reason_codes=tuple(reasons),
                                confidence=conf, amount=amount, currency=currency,
                                masked_account_suffix=suffix, candidate_broker_name=broker_name,
                                matched_pattern=matched, broker_reference_id=broker_reference_id or "",
                                parsed_event=parsed)

    # Credible withdrawal but not auto-acceptable (no pattern, or unseen/non-allowlisted sender) => review candidate.
    conf = Decimal("0.600") if matched else Decimal("0.450")
    return GenericDetection(REVIEW_REQUIRED, proposed_state=proposed, reason_codes=tuple(reasons),
                            confidence=conf, amount=amount, currency=currency,
                            masked_account_suffix=suffix, candidate_broker_name=broker_name,
                            matched_pattern=matched, broker_reference_id=broker_reference_id or "")
