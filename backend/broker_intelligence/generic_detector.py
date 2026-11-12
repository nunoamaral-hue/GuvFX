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
# ---- ADR-0099 (Lane A) certified-fingerprint reason codes --------------------------------------------
RC_CERTIFIED_FINGERPRINT = "certified_fingerprint_match"        # exactly one active+certified fingerprint claimed it
RC_MULTIPLE_FINGERPRINTS = "multiple_certified_fingerprint_match"  # >1 distinct certified broker matched => ambiguous
RC_EXTERNAL_EVIDENCE = "external_withdrawal_evidence"           # positive money-left-the-broker evidence present
RC_NO_EXTERNAL_EVIDENCE = "no_external_withdrawal_evidence"     # certified, but message lacks external evidence => UNKNOWN
RC_REQUIRES_REFERENCE_MISSING = "certified_requires_reference_missing"  # semantic requires_reference but none extracted

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

# ---- ADR-0099 transaction categories (mirrors models.TransactionCategory; the detector stays Django-free) ----
CAT_EXTERNAL_WITHDRAWAL = "EXTERNAL_WITHDRAWAL"
CAT_UNKNOWN = "UNKNOWN"

# Positive EXTERNAL-money-out evidence (money LEFT the broker). A CERTIFIED fingerprint UNLOCKS the capability to
# emit EXTERNAL_WITHDRAWAL, but emission still requires one of these per-message signals — certification never
# bypasses the TransactionCategory rule ("never from the word 'withdrawal'/subject/sender/amount/account alone").
# Deliberately a compact, conservative subset that mirrors the spirit of ``parsers_stream_b._EXTERNAL_RAILS``.
_EXTERNAL_RAIL = (
    "to your bank", "to your bank account", "sent to your bank", "bank transfer", "wire transfer", "bank wire",
    "to your card", "to your credit card", "to your debit card", "to your visa", "to your mastercard",
    "to your wallet", "to your e-wallet", "to your ewallet", "e-wallet", "skrill", "neteller", "paypal",
    "iban", "swift", "to your crypto", "crypto wallet", "usdt", "to your paypal", "payment method",
    "withdrawn to your", "sent to your card", "to your receiving", "to your external", "to your linked bank",
)
# Internal-movement veto: even with a "withdraw" word, these say money stayed INSIDE the broker (ADR TransactionCategory
# / the TradersWay 'confirm your withdrawal request' = internal-transfer lesson). Any of these denies external evidence.
_INTERNAL_VETO = (
    "internal transfer", "between your accounts", "to your trading account", "to another of your accounts",
    "transfer to account", "moved to your account", "to your mt4", "to your mt5", "to your wallet account",
    "between accounts", "to your other account",
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


def _extra_phrases(extra, state: str) -> List[str]:
    """Normalised (lowercased, stripped) fingerprint lifecycle phrases for one state. A fingerprint's
    ``lifecycle_phrases`` DRIVE classification — they are ADDED to the built-in sets, never subtract from them."""
    if not extra:
        return []
    return [str(p).strip().lower() for p in (extra.get(state) or []) if str(p).strip()]


def _classify_states(text: str, extra: Optional[dict] = None) -> List[str]:
    """Every lifecycle state whose explicit wording appears (built-in sets UNION any fingerprint-supplied phrases).
    Deterministic; order-independent. ``extra`` is a {STATE: [phrase]} dict from a matched certified fingerprint."""
    out = []
    if _any(text, _COMPLETED) or _any(text, _extra_phrases(extra, S_COMPLETED)):
        out.append(S_COMPLETED)
    if _any(text, _FAILED) or _any(text, _extra_phrases(extra, S_FAILED)):
        out.append(S_FAILED)
    if _any(text, _CANCELLED) or _any(text, _extra_phrases(extra, S_CANCELLED)):
        out.append(S_CANCELLED)
    if _any(text, _PROCESSING) or _any(text, _extra_phrases(extra, S_PROCESSING)):
        out.append(S_PROCESSING)
    if _any(text, _CONFIRMATION) or _any(text, _extra_phrases(extra, S_CONFIRMATION_REQUIRED)):
        out.append(S_CONFIRMATION_REQUIRED)
    if _any(text, _REQUESTED) or _any(text, _extra_phrases(extra, S_REQUESTED)):
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


# ======================================================================================================
# ADR-0099 (Lane A) — CERTIFIED broker-fingerprint matching + certified EXTERNAL_WITHDRAWAL emission.
# A fingerprint is a model instance OR a dict (tests). The caller supplies ONLY active+certified fingerprints;
# ``_fp_eligible`` is the detector's OWN independent fail-closed gate so a mis-filtered caller can never unlock
# certified authority from an uncertified row.
# ======================================================================================================

def _fp_get(fp, attr, default):
    return fp.get(attr, default) if isinstance(fp, dict) else getattr(fp, attr, default)


def _norm_domains(values) -> List[str]:
    out: List[str] = []
    for v in (values or []):
        d = str(v).strip().lower()
        if d and d not in out:
            out.append(d)
    return out


def _domain_covered(domain: str, doms: Sequence[str]) -> bool:
    d = (domain or "").strip().lower()
    return bool(d) and any(d == x or d.endswith("." + x) for x in doms)


def _fp_eligible(fp) -> bool:
    """Default-DENY: a fingerprint grants CERTIFIED authority ONLY when active + certified_capability + status
    CERTIFIED. (dict test shorthand: ``cert=True``.) Fail-closed independent of the caller's own filter."""
    if isinstance(fp, dict):
        if fp.get("cert") is True:
            return True
        return bool(fp.get("certified_capability") and fp.get("is_active")
                    and str(fp.get("governance_status", "")).upper() == "CERTIFIED")
    fn = getattr(fp, "is_certified_active", None)
    return bool(callable(fn) and fn())


def _fp_name(fp) -> str:
    return str(_fp_get(fp, "name", "") or "")


def _fingerprint_matches(fp, *, domain, dkim_domains, envelope_from_domain, reply_to_domain, text) -> bool:
    """A certified fingerprint CLAIMS this message when every DECLARED constraint holds (an undeclared constraint is
    not checked — a fingerprint may authenticate on domain alone). A DECLARED DKIM/envelope/reply-to set MUST align,
    so a spoofed From domain without the signing/envelope alignment the fingerprint declares is rejected."""
    doms = _norm_domains(_fp_get(fp, "authenticated_sender_domains", []))
    if doms and not _domain_covered(domain, doms):
        return False
    dkim_decl = _norm_domains(_fp_get(fp, "dkim_signing_domains", []))
    if dkim_decl and not any(_domain_covered(d, dkim_decl) for d in _norm_domains(dkim_domains)):
        return False
    env_decl = _norm_domains(_fp_get(fp, "envelope_from_domains", []))
    if env_decl and not _domain_covered(envelope_from_domain, env_decl):
        return False
    rt_decl = _norm_domains(_fp_get(fp, "reply_to_domains", []))
    if rt_decl and not _domain_covered(reply_to_domain, rt_decl):
        return False
    inds = [str(i).strip().lower() for i in (list(_fp_get(fp, "subject_indicators", []) or [])
                                             + list(_fp_get(fp, "body_indicators", []) or [])) if str(i).strip()]
    return (not inds) or _any(text, inds)


def _external_evidence(text: str) -> bool:
    """Positive evidence money LEFT the broker: an external rail present AND no internal-movement veto. Certification
    UNLOCKS EXTERNAL_WITHDRAWAL; this per-message gate is what actually authorises it (never the word 'withdrawal')."""
    return _any(text, _EXTERNAL_RAIL) and not _any(text, _INTERNAL_VETO)


def _labelled_value(raw_text: str, label: str, value_rx: str) -> str:
    """Extract the value after an operator-authored field LABEL (e.g. 'Transaction ID: ABC-9'). ReDoS-safe: the label
    is escaped + length-bounded and the separator run is bounded. Case preserved (operates on the ORIGINAL text)."""
    lab = (label or "").strip()
    if not lab or len(lab) > 48:
        return ""
    rx = re.compile(re.escape(lab) + r"\s*[:#\-]?\s{0,4}(" + value_rx + r")", re.IGNORECASE)
    m = rx.search(raw_text or "")
    return m.group(1) if m else ""


def _fp_reference(fp, raw_text: str) -> str:
    """Reference extraction DRIVEN by the fingerprint's ``reference_hint`` label. Bounded; never a URL/token; '' when
    the labelled field is absent (never guesses a mid-sentence number).

    PRIVACY: a long PURE-DIGIT run is indistinguishable from a full account number (which must never be stored
    un-masked — ``contains_sensitive`` does not flag bare digits). A genuine broker reference carries a non-digit
    marker (a prefix or a hyphen, e.g. 'WD-5017', 'request #A12'); a bare run of 7+ digits captured under a
    (possibly mis-chosen) label is dropped rather than persisted as a 'reference' that could be an account number."""
    hint = _fp_get(fp, "reference_hint", "") or ""
    ref = _labelled_value(raw_text, hint, r"[A-Za-z0-9\-]{3,32}").strip() if hint else ""
    if not ref or ref.lower().startswith("http") or re.fullmatch(r"\d{7,}", ref):
        return ""
    return ref[:32]


def _fp_currency_from_field(fp, raw_text: str) -> str:
    """Currency from the fingerprint's ``currency_hint`` labelled field (ADR-0095 'Currency Type: GBP' shape),
    validated against the known ISO set (never invented). Used only when the inline amount carried no currency."""
    hint = _fp_get(fp, "currency_hint", "") or ""
    if not hint:
        return ""
    val = _labelled_value(raw_text, hint, r"[A-Za-z]{3}").upper()
    return val if val in _ISO else ""


_FP_AMOUNT_MAX = 40   # a real amount field value is far under this; the cap keeps a crafted giant number out of the


def _fp_amount_from_field(fp, raw_text: str) -> Tuple[Optional[Decimal], str]:
    """Amount from the fingerprint's ``amount_hint`` labelled field (e.g. 'Withdrawal Amount: 30.00' or 'Amount:
    GBP 30'). Returns (Decimal|None, currency). Used only when no currency-adjacent inline amount was found. Never
    fabricates a number. The captured value is LENGTH-CAPPED (``_FP_AMOUNT_MAX``) before it reaches the amount regex,
    so a crafted giant comma-grouped number in the field cannot drive the quadratic ``_AMOUNT_RE`` scan."""
    hint = _fp_get(fp, "amount_hint", "") or ""
    if not hint:
        return None, ""
    val = _labelled_value(raw_text, hint, r"[$€£¥]?\s?(?:" + _NUM + r")(?:\s?[A-Za-z]{3})?")[:_FP_AMOUNT_MAX]
    if not val:
        return None, ""
    amt, cur = _extract_amount(val.lower())          # handles a symbol/ISO adjacent to the captured number
    if amt is not None:
        return amt, cur
    m = re.search(_NUM, val)
    if not m:
        return None, ""
    try:
        return Decimal(m.group(0).replace(",", "").replace(" ", "")), ""
    except (InvalidOperation, AttributeError):
        return None, ""


def _select_fingerprint(fingerprints, *, domain, dkim_domains, envelope_from_domain, reply_to_domain, text):
    """Return (single_fp, ambiguous): the ONE eligible certified fingerprint that claims this message, or
    (None, True) when two or more DISTINCT certified brokers claim it (fail-closed ambiguity => quarantine)."""
    matched = [fp for fp in (fingerprints or [])
               if _fp_eligible(fp) and _fingerprint_matches(
                   fp, domain=domain, dkim_domains=dkim_domains, envelope_from_domain=envelope_from_domain,
                   reply_to_domain=reply_to_domain, text=text)]
    names = {_fp_name(fp) for fp in matched}
    if len(names) >= 2:
        return None, True
    return (matched[0] if matched else None), False


def detect(*, subject: str, body: str, from_domain: str, auth_verdict: Optional[str],
           sender_allowlisted: bool, patterns: Sequence = (), html_text: str = "",
           broker_reference_id: str = "", fingerprints: Sequence = (), dkim_domains: Sequence = (),
           envelope_from_domain: str = "", reply_to_domain: str = "") -> GenericDetection:
    """Pure deterministic detection. ``patterns`` are the ENABLED legacy registry rows (ADR-0088); ``fingerprints``
    are the ACTIVE+CERTIFIED broker fingerprints (ADR-0099, model instances or dicts) the caller supplies —
    a single matching certified fingerprint MAY unlock a certified ``EXTERNAL_WITHDRAWAL`` (with positive external
    evidence in the message). ``sender_allowlisted`` is the WP3b allowlist verdict; ``auth_verdict`` the SPF/DKIM/
    DMARC result; ``dkim_domains``/``envelope_from_domain``/``reply_to_domain`` feed a fingerprint's declared
    alignment constraints. Fail-closed throughout. With ``fingerprints=()`` behaviour is byte-identical to ADR-0088."""
    text = _norm(subject, body, html_text)
    raw_text = " ".join(x for x in (subject or "", body or "", html_text or "") if x)   # case-preserving (fp field extraction)
    reasons: List[str] = []

    # ADR-0099: pick the ONE certified fingerprint that claims this message (or flag cross-broker ambiguity). Its
    # lifecycle phrases then DRIVE classification alongside the built-in sets.
    fp, fp_ambiguous = _select_fingerprint(
        fingerprints, domain=from_domain, dkim_domains=dkim_domains,
        envelope_from_domain=envelope_from_domain, reply_to_domain=reply_to_domain, text=text)
    fp_phrases = _fp_get(fp, "lifecycle_phrases", {}) if fp is not None else {}

    states = _classify_states(text, fp_phrases)
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

    # 3b) ADR-0099 ambiguity gate — two or more DISTINCT certified brokers claim one message => fail closed.
    if fp_ambiguous:
        return GenericDetection(QUARANTINED, reason_codes=tuple(reasons + [RC_MULTIPLE_FINGERPRINTS]),
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

    # 5) Safe extraction (redacted). A matched certified fingerprint's hints DRIVE the reference (and a field-only
    #    currency), on the CASE-PRESERVING text; an explicit ``broker_reference_id`` from the caller wins.
    amount, currency = _extract_amount(text)
    suffix = _extract_masked_suffix(text)
    fp_ref = ""
    if fp is not None:
        if amount is None:
            fa, fc = _fp_amount_from_field(fp, raw_text)
            if fa is not None:
                amount, currency = fa, (currency or fc)
        if not currency:
            currency = _fp_currency_from_field(fp, raw_text)
        fp_ref = _fp_reference(fp, raw_text)
    reference = (broker_reference_id or "").strip() or fp_ref
    if amount is not None:
        reasons.append(RC_AMOUNT_PRESENT)
    if suffix:
        reasons.append(RC_SUFFIX_PRESENT)

    # 6) Legacy pattern match (registry) + sender allowlist decide the ADR-0088 Tier-1 auto-accept vs review.
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
    for fld in (broker_name, currency, suffix, reference):
        if contains_sensitive(fld):
            return GenericDetection(QUARANTINED, reason_codes=tuple(reasons + [RC_UNSAFE_EVIDENCE]),
                                    proposed_state=proposed)

    # 7a) ADR-0099 CERTIFIED path — a single active+certified fingerprint claimed the message, auth passed and there
    #     is an explicit lifecycle. Certification unlocks the CAPABILITY to emit EXTERNAL_WITHDRAWAL; emission still
    #     requires positive external-money-out evidence IN THE MESSAGE and (if the fingerprint's semantics require a
    #     stable reference) a reference — otherwise the category stays UNKNOWN (a valid lifecycle event, never a
    #     counted withdrawal). The fingerprint's certified domains ARE the sender authority (no separate code
    #     allowlist entry needed — the scalability goal); ``auth_verdict == pass`` is still mandatory.
    if fp is not None:
        fp_name = _fp_name(fp)
        fp_version = _fp_get(fp, "version", 1)
        fp_broker = (_fp_get(fp, "broker_display_name", "") or broker_name)[:64]
        semantics = _fp_get(fp, "semantic_overrides", {}) or {}
        requires_reference = bool(semantics.get("requires_reference"))
        # A certified EXTERNAL_WITHDRAWAL requires BOTH positive external-rail evidence AND an explicit withdrawal
        # INTENT token in the message — never a lifecycle state alone. Otherwise a non-withdrawal broker email that
        # merely says "has been completed … sent to your bank" (a rebate/refund) could be mis-counted as a withdrawal
        # (financial-state over-count). Ambiguous ⇒ UNKNOWN (a valid lifecycle event, never a counted withdrawal).
        external = _external_evidence(text) and _any(text, _INTENT)
        cert_reasons = list(reasons) + [RC_CERTIFIED_FINGERPRINT]
        if requires_reference and not reference:
            category = CAT_UNKNOWN
            cert_reasons.append(RC_REQUIRES_REFERENCE_MISSING)
        elif external:
            category = CAT_EXTERNAL_WITHDRAWAL
            cert_reasons.append(RC_EXTERNAL_EVIDENCE)
        else:
            category = CAT_UNKNOWN
            cert_reasons.append(RC_NO_EXTERNAL_EVIDENCE)
        conf = Decimal("0.950") if (category == CAT_EXTERNAL_WITHDRAWAL
                                    and proposed in (S_COMPLETED, S_CONFIRMATION_REQUIRED)) else Decimal("0.900")
        parsed = ParsedBrokerEvent(
            event_type=_STATE_TO_EVENT.get(proposed, "WITHDRAWAL_REQUESTED"), transaction_category=category,
            broker=fp_broker, amount=amount, currency=currency, broker_reference_id=reference, confidence=conf)
        return GenericDetection(GENERIC_HIGH_CONFIDENCE, proposed_state=proposed, reason_codes=tuple(cert_reasons),
                                confidence=conf, amount=amount, currency=currency, masked_account_suffix=suffix,
                                candidate_broker_name=fp_broker, matched_pattern=f"{fp_name}:{fp_version}:certified",
                                broker_reference_id=reference, parsed_event=parsed)

    # 7b) Legacy ADR-0088 disposition (no certified fingerprint matched).
    #    GENERIC_HIGH_CONFIDENCE requires: allowlisted sender (Tier 1) AND a matching enabled pattern AND an
    #    explicit lifecycle. Everything else that is a credible withdrawal is REVIEW_REQUIRED (never auto-accepted).
    if sender_allowlisted and matched:
        conf = Decimal("0.900") if (amount is not None or proposed in (S_COMPLETED, S_CONFIRMATION_REQUIRED)) \
            else Decimal("0.750")
        parsed = ParsedBrokerEvent(
            event_type=_STATE_TO_EVENT.get(proposed, "WITHDRAWAL_REQUESTED"),
            transaction_category="UNKNOWN",   # generic NEVER asserts EXTERNAL_WITHDRAWAL without certified evidence
            broker=broker_name, amount=amount, currency=currency,
            broker_reference_id=reference, confidence=conf)
        return GenericDetection(GENERIC_HIGH_CONFIDENCE, proposed_state=proposed, reason_codes=tuple(reasons),
                                confidence=conf, amount=amount, currency=currency,
                                masked_account_suffix=suffix, candidate_broker_name=broker_name,
                                matched_pattern=matched, broker_reference_id=reference,
                                parsed_event=parsed)

    # Credible withdrawal but not auto-acceptable (no pattern, or unseen/non-allowlisted sender) => review candidate.
    conf = Decimal("0.600") if matched else Decimal("0.450")
    return GenericDetection(REVIEW_REQUIRED, proposed_state=proposed, reason_codes=tuple(reasons),
                            confidence=conf, amount=amount, currency=currency,
                            masked_account_suffix=suffix, candidate_broker_name=broker_name,
                            matched_pattern=matched, broker_reference_id=reference)
