"""WP4 — TradersWay deterministic email parser (parser #1). NO LLM in the critical path.

Turns a TradersWay payments email into at most one ``ParsedBrokerEvent``, extracting account/amount/currency/
reference and classifying BOTH the lifecycle (``event_type``) AND the transaction category. Governing safety rule
(Sponsor packet 2026-10-08 §7/§8): a message is ``EXTERNAL_WITHDRAWAL`` ONLY with positive evidence that money LEFT
the broker (an external destination / payment method / receiving institution / explicit external type). The word
"withdrawal", the subject, the sender, an amount or an account number are NOT sufficient — TradersWay labels an
internal transfer "confirm your withdrawal request". Absent external evidence ⇒ ``UNKNOWN`` (never guessed). The
parser NEVER extracts or emits a confirmation URL / authorization token (sensitive; lives only in immutable
evidence) and NEVER follows a link.

Registered on demand via ``register_default_parsers()`` (called by the WP3b worker), not at import — so it is inert
until the DARK ingestion worker is armed. Deterministic + pure; validated by synthetic fixtures (labelled) and the
genuine internal-transfer email as a permanent NEGATIVE regression. Production certification of the positive path
awaits a genuine external-withdrawal sample.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from .parsers import BrokerEmailParser, ParsedBrokerEvent, register_parser

_SENDER_DOMAIN = "tradersway.com"

_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "SGD", "HKD", "ZAR", "SEK", "NOK", "DKK",
               "PLN", "CZK", "AED", "MXN", "CNH", "CNY"}

# EXTERNAL evidence must be DIRECTIONAL + POSITIVE (money leaving the broker to an external destination) — NEVER a
# bare token like "bank account" that also appears in negations/boilerplate/footers. Two high-precision forms:
#   (1) a payout VERB followed (within a short span) by "to your <external destination>";
#   (2) an explicit structured "Withdrawal method/type/destination: <external>" label.
_EXTERNAL_PAYOUT = re.compile(
    r"\b(withdrawn|sent|paid|payout|transferred|credited|disbursed|remitted)\b[^.\n]{0,40}?"
    r"\bto\s+your\s+(bank|card|e-?wallet|wallet|visa|mastercard|maestro|paypal|skrill|neteller|crypto|"
    r"bank\s+account|card\s+ending)\b", re.IGNORECASE)
_EXTERNAL_METHOD = re.compile(
    r"\bwithdrawal\s+(type|method|destination)\s*[:\-]\s*"
    r"(bank|wire|card|crypto|e-?wallet|wallet|external|skrill|neteller|paypal|visa|mastercard)\b", re.IGNORECASE)
# INTERNAL evidence — broadened so realistic phrasing ("between your MT5 accounts", "between your two accounts",
# "to your other account", "internal transfer", "account-to-account") is caught.
_INTERNAL_EVIDENCE = re.compile(
    r"\b(internal transfer|transfer between (your )?[\w ]{0,24}accounts?|between (your )?[\w ]{0,24}accounts?|"
    r"to another of your accounts?|to your other [\w ]{0,12}account|account[- ]to[- ]account|"
    r"between accounts?|inter[- ]?account)\b", re.IGNORECASE)
# Negation immediately preceding an external phrase disqualifies it (context-awareness the substring test lacked).
_NEGATION = re.compile(r"\b(not|no|never|cannot|can['’]?t|won['’]?t|will not|isn['’]?t|"
                       r"aren['’]?t|do not|don['’]?t)\b", re.IGNORECASE)

# Money: optional leading symbol/code, a grouped-or-plain number, optional trailing code. Currency is VALIDATED
# against _CURRENCIES (not "any 3 uppercase letters"), so "NET"/"VAT" etc. are not treated as currencies.
_MONEY = re.compile(r"(?P<cur1>\$|[A-Za-z]{3})?\s*(?P<amt>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
                    r"\s*(?P<cur2>[A-Za-z]{3})?")
_AMOUNT_LABEL = re.compile(r"(?:withdrawal\s+)?(?:amount|total)\s*[:\-]?\s*", re.IGNORECASE)
_ACCOUNT = re.compile(r"(?:trading\s+)?account(?:\s*(?:number|no\.?|#|:))?\s*[:#]?\s*(\d{4,12})", re.IGNORECASE)
_REFERENCE = re.compile(r"\b(?:reference|ref(?:erence)?\s*(?:id|no\.?|#)?)\s*[:#]?\s*([A-Za-z0-9\-]{3,32})",
                        re.IGNORECASE)


def _has_unnegated(pattern, text: str) -> bool:
    """True if ``pattern`` matches at least once WITHOUT a negation word in the 40 chars immediately before it."""
    for m in pattern.finditer(text):
        if not _NEGATION.search(text[max(0, m.start() - 40):m.start()]):
            return True
    return False


def _norm_currency(tok: str) -> str:
    t = (tok or "").strip().upper()
    if t == "$":
        return "USD"
    return t if t in _CURRENCIES else ""


def _classify_lifecycle(subject: str, body: str) -> Optional[str]:
    """Map TradersWay wording to a lifecycle event type, or None if no recognisable withdrawal-lifecycle signal."""
    t = f"{subject}\n{body}".lower()
    if "confirm" in t and "withdrawal" in t:
        return "WITHDRAWAL_CONFIRMATION_REQUIRED"
    if re.search(r"\bwithdrawal (confirmed|has been confirmed)\b", t):
        return "WITHDRAWAL_CONFIRMED"
    if re.search(r"\b(withdrawal (completed|complete)|funds (have been )?withdrawn)\b", t):
        return "WITHDRAWAL_COMPLETED"
    if re.search(r"\bwithdrawal (processed|has been processed|has been sent|is being processed)\b", t):
        return "WITHDRAWAL_PROCESSING"
    if re.search(r"\bwithdrawal (approved|has been approved)\b", t):
        return "WITHDRAWAL_APPROVED"
    if re.search(r"\bwithdrawal (rejected|declined|was declined)\b", t):
        return "WITHDRAWAL_REJECTED"
    if re.search(r"\bwithdrawal (cancelled|canceled)\b", t):
        return "WITHDRAWAL_CANCELLED"
    if re.search(r"\bwithdrawal (request(ed)?|has been received|received)\b", t):
        return "WITHDRAWAL_REQUESTED"
    return None


def _classify_category(subject: str, body: str) -> str:
    """Conservative + directional. EXTERNAL_WITHDRAWAL ONLY on positive, un-negated external-payout evidence (a
    payout verb → 'to your <external destination>', or a structured 'Withdrawal method: <external>' label). INTERNAL
    only on positive internal evidence. Conflicting signals OR no positive evidence ⇒ UNKNOWN (never guessed, never
    inferred from the word 'withdrawal'/subject/sender/amount/account). Under-claiming (a genuine external read as
    UNKNOWN) is safe and is tightened by the real-sample certification; over-claiming an internal/ambiguous email as
    EXTERNAL is the forbidden error (§8) and cannot happen here."""
    t = f"{subject}\n{body}"
    internal = bool(_INTERNAL_EVIDENCE.search(t))
    external = _has_unnegated(_EXTERNAL_PAYOUT, t) or _has_unnegated(_EXTERNAL_METHOD, t)
    if internal and external:
        return "UNKNOWN"                 # conflicting signals — never guess
    if internal:
        return "INTERNAL_TRANSFER"
    if external:
        return "EXTERNAL_WITHDRAWAL"
    return "UNKNOWN"                      # no positive evidence either way — never guessed


def _parse_money(text: str):
    """First valid money token in ``text``: skips account-number-like grabs (preceded by 'account'), requires a
    WHITELISTED currency or an explicit decimal (so a bare integer isn't taken as an amount), and refuses an
    ambiguous comma (e.g. European '200,00') rather than mis-scaling it. Never fabricates a currency."""
    for m in _MONEY.finditer(text):
        raw = m.group("amt")
        if "account" in text[max(0, m.start() - 12):m.start()].lower():
            continue                     # don't grab an account number as an amount
        if "," in raw:
            if not re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{1,2})?", raw):
                continue                 # ambiguous comma (not clean thousands grouping) — never mis-scale
            raw = raw.replace(",", "")
        cur = _norm_currency(m.group("cur2")) or _norm_currency(m.group("cur1"))
        if not cur and "." not in raw:
            continue                     # bare integer without a valid currency — likely not an amount
        try:
            val = Decimal(raw)
        except (InvalidOperation, ValueError):
            continue
        if val > 0:
            return val, cur
    return None, ""


def _extract_amount(body: str):
    """Prefer the money adjacent to an explicit 'Amount:' label (robust against account numbers earlier in the
    body); fall back to the first valid currency-qualified money anywhere."""
    lbl = _AMOUNT_LABEL.search(body)
    if lbl:
        val, cur = _parse_money(body[lbl.end():lbl.end() + 40])
        if val is not None:
            return val, cur
    return _parse_money(body)


class TradersWayParser:
    name = "tradersway"
    version = "v1"

    def can_parse(self, *, subject: str, body: str, from_address: str) -> bool:
        if _SENDER_DOMAIN not in (from_address or "").lower():
            return False
        t = f"{subject}\n{body}".lower()
        return any(k in t for k in ("withdrawal", "withdraw", "transfer", "payout"))

    def parse(self, *, subject: str, body: str, from_address: str) -> Optional[ParsedBrokerEvent]:
        lifecycle = _classify_lifecycle(subject, body)
        if lifecycle is None:
            return None                                   # not a recognisable withdrawal-lifecycle message
        category = _classify_category(subject, body)
        amount, currency = _extract_amount(body)
        acct_m = _ACCOUNT.search(body)
        ref_m = _REFERENCE.search(body)
        # broker_reference_id is the broker's REFERENCE label only — NEVER a URL/token. Anchored to "reference".
        reference = ref_m.group(1) if ref_m else ""
        return ParsedBrokerEvent(
            event_type=lifecycle,
            transaction_category=category,
            broker="TradersWay",
            amount=amount,
            currency=currency,
            occurred_at=None,                             # broker-stated time parsed by the worker from headers (WP3b)
            broker_reference_id=reference,
            confidence=Decimal("0.800"))


def register_default_parsers() -> None:
    """Register the production parsers. Called by the WP3b ingestion worker at start-up — NOT at import, so the
    registry stays empty (DARK) until the worker is armed."""
    register_parser(TradersWayParser())


# Static assertion the class satisfies the Protocol (cheap guard; no runtime effect).
_PROTOCOL_OK: BrokerEmailParser = TradersWayParser()
