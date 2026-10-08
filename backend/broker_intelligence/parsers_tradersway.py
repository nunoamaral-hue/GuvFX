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

# EXTERNAL classification is DELIBERATELY NOT derived from free-text prose in V1.
# ---------------------------------------------------------------------------------------------------------------
# Three review rounds proved that any prose heuristic for "did money LEAVE the broker?" is bypassable (negation
# words the list misses — "rather than", "instead of"; ambiguous destinations — a broker cabinet "wallet" is
# internal; policy/FAQ boilerplate; co-occurring phrases). The FORBIDDEN error is OVER-claiming EXTERNAL_WITHDRAWAL
# (it would inflate the investor-facing withdrawal metrics, Sponsor §8). So the V1 parser has **no prose→external
# path at all**: it emits INTERNAL_TRANSFER (only on explicit positive internal evidence) or UNKNOWN. The positive
# EXTERNAL_WITHDRAWAL classifier is built and certified against a GENUINE external-withdrawal email (the Sponsor's
# pilot withdrawal, §9/§11) — never guessed from prose. Under-claiming (a real external read as UNKNOWN) is safe;
# over-claiming is not. This invariant is STRUCTURAL: there is no code path from parser text to EXTERNAL_WITHDRAWAL.
_INTERNAL_EVIDENCE = re.compile(
    r"\b(internal transfer|transfer between (your )?[\w ]{0,24}accounts?|between (your )?[\w ]{0,24}accounts?|"
    r"to another of your accounts?|to your other [\w ]{0,12}account|account[- ]to[- ]account|"
    r"between accounts?|inter[- ]?account|moved internally|internally between)\b", re.IGNORECASE)

# Amount is extracted ONLY from an explicit "Amount:"/"Total:" LABEL (never a free scan of the body), so an account
# number or other stray integer can never be mis-grabbed. Currency is VALIDATED against _CURRENCIES.
_LABELED_AMOUNT = re.compile(
    r"(?:withdrawal\s+)?(?:amount|total)\s*[:\-]?\s*(?:(?P<cur1>[A-Za-z]{3}|\$)\s*)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:[.,]\d{1,2})?|\d+)\s*(?P<cur2>[A-Za-z]{3})?", re.IGNORECASE)
_ACCOUNT = re.compile(r"(?:trading\s+)?account(?:\s*(?:number|no\.?|#|:))?\s*[:#]?\s*(\d{4,12})", re.IGNORECASE)
_REFERENCE = re.compile(r"\b(?:reference|ref(?:erence)?\s*(?:id|no\.?|#)?)\s*[:#]?\s*([A-Za-z0-9\-]{3,32})",
                        re.IGNORECASE)


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
    """V1 (bypass-free): INTERNAL_TRANSFER on explicit positive internal evidence, else UNKNOWN. There is NO
    prose→EXTERNAL_WITHDRAWAL path — external is never guessed from email text (see the module note above); it is
    certified against a genuine external-withdrawal sample. So no boilerplate / negation / ambiguous-destination
    input can ever over-claim an external withdrawal."""
    if _INTERNAL_EVIDENCE.search(f"{subject}\n{body}"):
        return "INTERNAL_TRANSFER"
    return "UNKNOWN"


def _extract_amount(body: str):
    """Extract the amount ONLY from an explicit 'Amount:'/'Total:' label (never a free scan), so a stray integer
    (e.g. an account number) can never be mis-grabbed. Number normalisation: US grouping '1,234.56' → strip commas;
    European decimal '12,34' → dot; plain otherwise — so neither a 100x mis-scale nor a fractional-part grab occurs.
    Currency is whitelist-validated (never fabricated). No label ⇒ (None, '') — amount unknown, never guessed."""
    m = _LABELED_AMOUNT.search(body)
    if not m:
        return None, ""
    raw = m.group("num")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{1,2})?", raw):
        raw = raw.replace(",", "")           # US thousands grouping
    elif re.fullmatch(r"\d+,\d{1,2}", raw):
        raw = raw.replace(",", ".")          # European decimal comma
    cur = _norm_currency(m.group("cur2")) or _norm_currency(m.group("cur1"))
    try:
        val = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None, ""
    return (val, cur) if val > 0 else (None, "")


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
