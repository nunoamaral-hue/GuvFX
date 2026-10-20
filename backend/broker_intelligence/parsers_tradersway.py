"""WP4 — TradersWay deterministic email parser (parser #1). NO LLM in the critical path.

Turns a TradersWay payments email into at most one ``ParsedBrokerEvent``, extracting account/amount/currency/
reference and classifying BOTH the lifecycle (``event_type``) AND the transaction category. Governing safety rule
(Sponsor packet 2026-10-08 §7/§8): a message is ``EXTERNAL_WITHDRAWAL`` ONLY with positive evidence that money LEFT
the broker. The word "withdrawal", the subject, the sender, an amount or an account number are NOT sufficient —
TradersWay labels an internal transfer "confirm your withdrawal request". Absent external evidence ⇒ ``UNKNOWN``
(never guessed). The parser NEVER extracts or emits a confirmation URL / authorization token (sensitive; lives only
in immutable evidence) and NEVER follows a link.

The single certified parser→EXTERNAL path (WV1 board-demo packet 2026-10-19, against the GENUINE TradersWay
external-withdrawal REQUEST email): the broker's STRUCTURED ``Payment method: <external rail>`` field (crypto / bank
wire / e-wallet — a whitelist, never free prose) naming a money-OUT rail, AND a withdrawal-DIRECTION lifecycle. Both
are required: direction comes from the withdrawal wording (``parse`` runs ``_classify_lifecycle`` first), externality
from the label — so a DEPOSIT via the same rail (also "Payment method: Ethereum") is never mis-read as a withdrawal.
There is still NO prose→EXTERNAL path; the label is a structured field, not a heuristic over narrative text.

The genuine external-payout COMPLETION ("Payment request #N … status changed from Confirmed to Completed") carries
NO rail and NO direction wording (fewer fields than the request), so its category is honestly ``UNKNOWN`` — its
externality is NOT re-derived from its own text but established by WP5 correlation to the REQUEST that shares its
authenticated broker reference #N. A completion can therefore never, on its own, create or inflate a withdrawal.

Registered on demand via ``register_default_parsers()`` (called by the WP3b worker), not at import — so it is inert
until the DARK ingestion worker is armed. Deterministic + pure; validated by synthetic fixtures (labelled), the
genuine internal-transfer email (permanent NEGATIVE regression), and the genuine external request+completion pair
reproduced REDACTED in ``tests_tradersway_external_template.py``.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from .parsers import BrokerEmailParser, ParsedBrokerEvent, register_parser

_SENDER_DOMAIN = "tradersway.com"

_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "SGD", "HKD", "ZAR", "SEK", "NOK", "DKK",
               "PLN", "CZK", "AED", "MXN", "CNH", "CNY"}

# EXTERNAL classification is DELIBERATELY NOT derived from free-text PROSE.
# ---------------------------------------------------------------------------------------------------------------
# Three review rounds proved that any prose heuristic for "did money LEAVE the broker?" is bypassable (negation
# words the list misses — "rather than", "instead of"; ambiguous destinations — a broker cabinet "wallet" is
# internal; policy/FAQ boilerplate; co-occurring phrases). The FORBIDDEN error is OVER-claiming EXTERNAL_WITHDRAWAL
# (it would inflate the investor-facing withdrawal metrics, Sponsor §8). So the parser STILL has no PROSE→external
# path: narrative text can only ever yield INTERNAL_TRANSFER (on explicit positive internal evidence) or UNKNOWN.
# The ONE certified external path is the broker's STRUCTURED ``Payment method: <external rail>`` label
# (``_EXTERNAL_EVIDENCE`` below), gated by a withdrawal-direction lifecycle — certified against the GENUINE pilot
# external-withdrawal REQUEST (WV1 packet 2026-10-19). A label is a parsed field with a whitelisted value, not a
# guess over narrative; under-claiming (a real external read as UNKNOWN, e.g. an unlisted rail) is safe, over-
# claiming is not. A genuine completion carries no label at all → UNKNOWN → external-ness supplied only by WP5
# correlation to the referenced request (so a completion can never create or inflate a withdrawal on its own).
_INTERNAL_EVIDENCE = re.compile(
    r"\b(internal transfer|transfer between (your )?[\w ]{0,24}accounts?|between (your )?[\w ]{0,24}accounts?|"
    r"to another of your accounts?|to your other [\w ]{0,12}account|account[- ]to[- ]account|"
    r"between accounts?|inter[- ]?account|moved internally|internally between|"
    # Certified TradersWay internal-transfer template (genuine captured email, 2026-10-12): money moving TO the
    # member's OWN trading account is internal — positive evidence, never external.
    r"transferr?ing to your [\w ]{0,12}account)\b", re.IGNORECASE)

# TradersWay internal-transfer template grammar (certified against the genuine captured email, redacted):
#   "<source> in amount of <amt> <cur> transferring to your trading account <destination>.
#    Your withdrawal request number is: <reference>."
_AMOUNT_OF = re.compile(
    r"amount\s+of\s+(?:(?P<cur1>[A-Za-z]{3}|\$)\s*)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:[.,]\d{1,2})?|\d+)\s*(?P<cur2>[A-Za-z]{3})?", re.IGNORECASE)
# Reference stays on the SAME line as the label ([^\S\n] = horizontal whitespace only) so a confirmation URL on the
# next line can never be captured; a captured token starting with a URL scheme is additionally rejected in parse().
# Each horizontal-whitespace run is BOUNDED ({0,8}) — a genuine label never has more than a few spaces before the
# value — so no catastrophic backtracking is possible on a long (e.g. HTML &nbsp; U+00A0) whitespace run (RULE: ReDoS).
_REQUEST_NUMBER = re.compile(
    r"(?:withdrawal\s+)?request\s+number\s+is[^\S\n]{0,8}[:#]?[^\S\n]{0,8}([A-Za-z0-9\-]{3,32})", re.IGNORECASE)
# Source + destination are captured as ONE anchored transfer clause ("<src> in amount of <...> transferring to your
# [trading] account <dst>") — never two independent body-wide first-matches, so an unrelated "to your account <N>"
# elsewhere (e.g. a login/greeting line) cannot be mis-taken as the destination. The middle is bounded + newline-free.
_TRANSFER_CLAUSE = re.compile(
    r"\b(?P<src>\d{4,12})\s+in\s+amount\s+of\s+[^\n]{0,60}?"
    r"transferr?ing\s+to\s+your\s+(?:trading\s+)?account\s+(?P<dst>\d{4,12})\b", re.IGNORECASE)

# Amount is extracted ONLY from an explicit "Amount:"/"Total:" LABEL (never a free scan of the body), so an account
# number or other stray integer can never be mis-grabbed. Currency is VALIDATED against _CURRENCIES.
_LABELED_AMOUNT = re.compile(
    r"(?:withdrawal\s+)?(?:amount|total)\s*[:\-]?\s*(?:(?P<cur1>[A-Za-z]{3}|\$)\s*)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:[.,]\d{1,2})?|\d+)\s*(?P<cur2>[A-Za-z]{3})?", re.IGNORECASE)
_ACCOUNT = re.compile(r"(?:trading\s+)?account(?:\s*(?:number|no\.?|#|:))?\s*[:#]?\s*(\d{4,12})", re.IGNORECASE)
_REFERENCE = re.compile(
    r"\b(?:reference|ref(?:erence)?\s*(?:id|no\.?|#)?)[^\S\n]{0,8}[:#]?[^\S\n]{0,8}([A-Za-z0-9\-]{3,32})",
    re.IGNORECASE)

# POSITIVE external evidence: the broker's STRUCTURED "Payment method: <rail>" field naming an external money-OUT
# rail. Whitelisted values only (an unlisted rail ⇒ UNKNOWN, a safe under-claim — never a prose guess). This label is
# NECESSARY but NOT SUFFICIENT for EXTERNAL_WITHDRAWAL: the same rail label appears on DEPOSITS (money IN) and on
# COMPLETION notices, so ``_classify_category`` additionally requires a requesting/in-flight withdrawal lifecycle AND
# affirmative anchored withdrawal-direction wording AND the absence of deposit-direction wording (see below). The
# label is certified against the genuine pilot external-withdrawal REQUEST (2026-10-19): "Payment method: Ethereum".
_EXTERNAL_PAYMENT_RAILS = (
    "ethereum", "erc20", "erc-20", "bitcoin", "bitcoin cash", "litecoin", "dogecoin", "monero", "dash",
    "tether", "usdt", "usdc", "tron", "trc20", "trc-20", "ripple", "xrp",
    "bank wire", "wire transfer", "bank transfer", "international wire", "sepa", "swift",
    "skrill", "neteller", "perfect money", "webmoney", "advcash", "sticpay", "payeer",
)
_EXTERNAL_EVIDENCE = re.compile(
    r"payment\s+method\s*[:\-]\s*(?:" + "|".join(re.escape(r) for r in _EXTERNAL_PAYMENT_RAILS) + r")\b",
    re.IGNORECASE)

# Lifecycles that MAY bear EXTERNAL_WITHDRAWAL: a withdrawal being REQUESTED or in flight. A direction-NEUTRAL
# COMPLETION (``WITHDRAWAL_COMPLETED``) and any terminal event are DELIBERATELY excluded — a completion's externality
# is established ONLY by WP5 correlation to its request (shared authenticated reference), never from its own text.
_EXTERNAL_ELIGIBLE_LIFECYCLES = frozenset({
    "WITHDRAWAL_REQUESTED", "WITHDRAWAL_CONFIRMATION_REQUIRED", "WITHDRAWAL_CONFIRMED",
    "WITHDRAWAL_PROCESSING", "WITHDRAWAL_APPROVED"})
# Affirmative, ANCHORED withdrawal-DIRECTION evidence (money leaving) — NOT a scattered "withdrawal" substring that a
# footer/disclaimer on a deposit could supply. Required (together with the rail label) for EXTERNAL_WITHDRAWAL.
_WITHDRAWAL_DIRECTION = re.compile(
    r"\b(to\s+withdraw|withdrawal\s+request|request(?:ed)?\s+(?:a\s+|your\s+)?withdrawal|"
    r"withdraw(?:al)?\s+(?:of\s+)?funds?|withdrawn)\b", re.IGNORECASE)
# Deposit-direction wording (money arriving). Its presence VETOES EXTERNAL_WITHDRAWAL — a deposit/funding email that
# quotes a rail and mentions "withdrawal" only in boilerplate must never be read as a withdrawal. (Verified: the
# genuine external-withdrawal request contains no "deposit" token, so this veto never under-claims the real case.)
_DEPOSIT_DIRECTION = re.compile(r"\bdeposit(?:ed|s|ing)?\b", re.IGNORECASE)

# TradersWay external-payout references (genuine pilot pair, 2026-10-19): the REQUEST states "request number #<N>";
# the COMPLETION states "Payment request #<N>" — the SAME authenticated broker transaction reference links the pair
# (WP5 correlation). Digits only, same-line (``[^\S\n]`` = horizontal whitespace), so a confirmation URL/token on
# the next line can never be captured as a reference. Distinct anchors (the request has no "payment request" phrase;
# the completion has no "request number" phrase — verified on the genuine emails) so neither cross-matches. Each
# horizontal-whitespace run is BOUNDED ({0,8}) so a long run (e.g. HTML &nbsp; → U+00A0, which [^\S\n] matches and
# gmail_source does not collapse) followed by a non-digit cannot trigger catastrophic backtracking (ReDoS).
_TW_REQUEST_NUMBER = re.compile(r"request\s+number[^\S\n]{0,8}[:#]?[^\S\n]{0,8}#?[^\S\n]{0,8}(\d{3,16})", re.IGNORECASE)
_TW_PAYMENT_REQUEST = re.compile(r"payment\s+request[^\S\n]{0,8}[:#]?[^\S\n]{0,8}#?[^\S\n]{0,8}(\d{3,16})", re.IGNORECASE)


def _norm_currency(tok: str) -> str:
    t = (tok or "").strip().upper()
    if t == "$":
        return "USD"
    return t if t in _CURRENCIES else ""


def _classify_lifecycle(subject: str, body: str) -> Optional[str]:
    """Map TradersWay wording to a lifecycle event type, or None if no recognisable withdrawal-lifecycle signal."""
    t = f"{subject}\n{body}".lower()
    # TradersWay external-payout COMPLETION: "Payment request #N … status changed from Confirmed to Completed". A
    # NEUTRAL payment-request completion — it carries NO "withdrawal"/"withdraw" token and NO rail, so direction/
    # category are NOT asserted here (category stays UNKNOWN in ``_classify_category``); WP5 correlation inherits
    # externality from the referenced request. Matched FIRST so the generic "confirm" branch cannot mislabel the
    # word "Confirmed" as confirmation-required. Certified against the genuine completion email (2026-10-19).
    if "payment request" in t and "status changed" in t and "confirmed to completed" in t:
        return "WITHDRAWAL_COMPLETED"
    # ANCHORED confirm-withdrawal-request phrase (not two scattered substrings): a deposit/notice whose footer merely
    # contains "confirm" and "withdrawal" independently must NOT be labelled a withdrawal confirmation.
    if re.search(r"confirm\s+(?:your\s+)?withdrawal\s+request", t) or \
       re.search(r"withdrawal\s+request[^\n]{0,30}confirm", t):
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


def _classify_category(subject: str, body: str, lifecycle: Optional[str]) -> str:
    """INTERNAL_TRANSFER on explicit positive internal evidence (checked FIRST — if an email somehow carried both an
    internal-transfer clause and a payment-rail label, internal wins, a safe under-claim of external). Otherwise
    EXTERNAL_WITHDRAWAL only when ALL FOUR hold: (1) the ``lifecycle`` is a requesting/in-flight withdrawal state
    (``_EXTERNAL_ELIGIBLE_LIFECYCLES`` — a COMPLETION or terminal event is NEVER external from its own text), (2) the
    structured ``Payment method: <rail>`` label is present (``_EXTERNAL_EVIDENCE``), (3) affirmative ANCHORED
    withdrawal-direction wording is present (``_WITHDRAWAL_DIRECTION``), and (4) NO deposit-direction wording is
    present (``_DEPOSIT_DIRECTION`` veto). Else UNKNOWN. This structurally enforces the module invariant: there is no
    prose→external path; a deposit via the same rail, a completion carrying a rail, and footer/boilerplate mentions of
    "withdrawal" can none of them over-claim. A genuine completion stays UNKNOWN; its externality is supplied ONLY by
    WP5 correlation to the request that shares its authenticated reference. Under-claiming is safe; over-claiming is
    the forbidden error (it would inflate investor-facing withdrawal metrics)."""
    blob = f"{subject}\n{body}"
    if _INTERNAL_EVIDENCE.search(blob):
        return "INTERNAL_TRANSFER"
    if (lifecycle in _EXTERNAL_ELIGIBLE_LIFECYCLES
            and _EXTERNAL_EVIDENCE.search(blob)
            and _WITHDRAWAL_DIRECTION.search(blob)
            and not _DEPOSIT_DIRECTION.search(blob)):
        return "EXTERNAL_WITHDRAWAL"
    return "UNKNOWN"


def _extract_amount(body: str):
    """Extract the amount ONLY from an explicit 'Amount:'/'Total:' label (never a free scan), so a stray integer
    (e.g. an account number) can never be mis-grabbed. Number normalisation: US grouping '1,234.56' → strip commas;
    European decimal '12,34' → dot; plain otherwise — so neither a 100x mis-scale nor a fractional-part grab occurs.
    Currency is whitelist-validated (never fabricated). No label ⇒ (None, '') — amount unknown, never guessed."""
    m = _LABELED_AMOUNT.search(body) or _AMOUNT_OF.search(body)   # labelled "Amount:" or template "amount of <n> <cur>"
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
        # R3a: EXACT registrable-domain / subdomain match on the From domain — NOT a substring. The old substring test
        # (`_SENDER_DOMAIN in from_address`) accepted a look-alike like `eviltradersway.com`; this does not. (Sender
        # authenticity — Authentication-Results — is enforced separately in the ingestion gate; the From is not trusted
        # on its own.)
        from .broker_senders import _domain_of, domain_matches
        if not domain_matches(_domain_of(from_address), _SENDER_DOMAIN):
            return False
        t = f"{subject}\n{body}".lower()
        # "payment request" admits the external-payout COMPLETION, which carries no withdraw/withdrawal/transfer/
        # payout token (verified on the genuine completion email) and would otherwise be rejected here unparsed.
        return any(k in t for k in ("withdrawal", "withdraw", "transfer", "payout", "payment request"))

    def parse(self, *, subject: str, body: str, from_address: str) -> Optional[ParsedBrokerEvent]:
        lifecycle = _classify_lifecycle(subject, body)
        if lifecycle is None:
            return None                                   # not a recognisable withdrawal-lifecycle message
        category = _classify_category(subject, body, lifecycle)
        amount, currency = _extract_amount(body)
        # broker_reference_id is the broker's REFERENCE label only — NEVER a URL/token. Anchored to "reference", the
        # internal "request number is: <N>" template, or the external-payout pair's "Payment request #N" (completion)
        # / "request number #N" (request). Same-line anchors only. Absent ⇒ '' (never fabricated).
        ref_m = (_REFERENCE.search(body) or _REQUEST_NUMBER.search(body)
                 or _TW_PAYMENT_REQUEST.search(body) or _TW_REQUEST_NUMBER.search(body))
        reference = ref_m.group(1) if ref_m else ""
        if reference.lower().startswith("http"):          # never store a URL scheme as a reference (never fabricate)
            reference = ""
        # Transaction source/destination trading accounts, from the SINGLE anchored transfer clause. 0 or >1 distinct
        # clauses ⇒ both '' (UNRESOLVED — never guess a first body-wide match, never a stray "to your account <N>").
        pairs = {(m.group("src"), m.group("dst")) for m in _TRANSFER_CLAUSE.finditer(body)}
        source_account, destination_account = next(iter(pairs)) if len(pairs) == 1 else ("", "")
        return ParsedBrokerEvent(
            event_type=lifecycle,
            transaction_category=category,
            broker="TradersWay",
            amount=amount,
            currency=currency,
            occurred_at=None,                             # broker-stated time parsed by the worker from headers (WP3b)
            broker_reference_id=reference,
            source_account=source_account,
            destination_account=destination_account,
            confidence=Decimal("0.800"))


def register_default_parsers() -> None:
    """Register the production parsers (the single canonical entry point). Called by the WP3b ingestion worker at
    start-up — NOT at import, so the registry stays empty (DARK) until the worker is armed. Registers every supported
    broker: TradersWay (#1) and IS6 (#2, PR-B); both share one mailbox but stay independently authenticated +
    attributed. ``register_parser`` is idempotent by (name, version), so re-registration is a no-op."""
    from .parsers_is6 import IS6Parser
    register_parser(TradersWayParser())
    register_parser(IS6Parser())


# Static assertion the class satisfies the Protocol (cheap guard; no runtime effect).
_PROTOCOL_OK: BrokerEmailParser = TradersWayParser()
