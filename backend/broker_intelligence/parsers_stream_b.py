"""Stream B (ADR-0079) — deterministic withdrawal-email adapters for Pepperstone, Dominion Markets and Vantage.

SYNTHETIC-ONLY. No real broker template is reproduced here; these adapters match conservative, anchored patterns
that the committed SYNTHETIC fixtures exercise. They mirror the TradersWay parser's safety discipline exactly:

* NO LLM; pure, deterministic, no I/O.
* EXACT registrable-domain / subdomain match on the From domain (never a substring) — look-alikes are refused.
  (Sender AUTHENTICITY — Authentication-Results — is enforced separately in the ingestion gate.)
* The lifecycle is classified from ANCHORED phrases (terminal/completion checked before the generic request), so
  "confirm your withdrawal request" is CONFIRMATION_REQUIRED, never REQUESTED, and completion is never inferred
  from a request/confirmation email.
* There is NO prose -> EXTERNAL path. ``transaction_category`` is UNKNOWN unless ALL hold: a requesting/in-flight
  lifecycle, a STRUCTURED line-anchored ``Withdrawal/Payment method: <rail>`` field naming an external money-out
  rail (whitelist), affirmative anchored withdrawal-direction wording, and NO veto wording (deposit/funding/
  rejected/declined/cancelled). Under-claiming (a real external read as UNKNOWN) is safe; over-claiming is the
  forbidden error (it would inflate investor-facing metrics). Externality is otherwise supplied by WP5 correlation.
* A confirmation URL / authorization token is NEVER extracted into any field, and a reference starting with a URL
  scheme is dropped (never store a clickable authorization target).
* Ambiguous account matching is left to the resolver (the parser only emits the broker-STATED source/destination
  account numbers from a single anchored clause, else '').

Registered on demand via ``register_default_parsers`` (parsers_tradersway) — inert until the DARK worker is armed.
"""
from __future__ import annotations

import html as _htmllib
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from .broker_senders import _domain_of, domain_matches
from .parsers import BrokerEmailParser, ParsedBrokerEvent, register_parser

# ---- HTML-aware structured parsing (ADR-0079 / packet 008) ----------------------------------------------------------
# Some brokers (Pepperstone) send multipart/alternative mail whose text/plain alternative is a degraded, VALUE-LESS
# rendering (just the field LABELS); the withdrawal-detail VALUES live only in the text/html part, in a label/spacer/
# value <td> table. The deterministic parser therefore reads the HTML when it is provided, pairing each label cell with
# its value cell — it never runs the free-text regexes directly over raw HTML. ``_CELL`` is a private boundary marker
# inserted at every </td>/</th> so a row's label and value stay on one line yet remain separable.
_CELL = "\x01"
# Fold "smart" punctuation to ASCII BEFORE matching, so anchors written with straight quotes/hyphens match a broker's
# curly apostrophes and en/em dashes (Pepperstone's "We've received your withdrawal request" uses U+2019).
_PUNCT_MAP = {"’": "'", "‘": "'", "‛": "'", "′": "'",
              "“": '"', "”": '"', "„": '"', "″": '"',
              "–": "-", "—": "-", "−": "-", "\xa0": " "}


def _norm_punct(s: str) -> str:
    s = s or ""
    for k, v in _PUNCT_MAP.items():
        s = s.replace(k, v)
    return s


def _html_structured_text(html: str) -> str:
    """Deterministically render an HTML body to structure-preserving text: drop script/style/head; </td>/</th> become a
    cell marker (so a label and its value stay on one line, separable); block/row ends become newlines; remaining tags
    are dropped; entities unescaped; intra-line whitespace collapsed (newlines + markers preserved). Never raises."""
    try:
        s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html or "")
        s = re.sub(r"(?i)<br\s*/?>", "\n", s)
        s = re.sub(r"(?i)</(td|th)>", _CELL, s)
        s = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table)>", "\n", s)
        s = re.sub(r"<[^>]+>", " ", s)
        s = _htmllib.unescape(s)
        s = re.sub(r"[^\S\n]+", " ", s)
        return s
    except Exception:  # noqa: BLE001 — a pathological body must never crash ingestion
        return ""


def _flatten(structured: str) -> str:
    """Collapse the structured text (newlines + cell markers) to one normalised line for lifecycle phrase matching."""
    return re.sub(r"[\s\x01]+", " ", _norm_punct(structured or "")).strip()


def _ordered_cells(structured: str) -> list:
    """Flatten the structured text to an ordered list of non-empty cell texts (split on newline AND the </td>/</th>
    marker). A detail row renders as a LABEL cell immediately followed by its VALUE cell in reading order."""
    out = []
    for part in (structured or "").split("\n"):
        for c in part.split(_CELL):
            c = c.strip().strip("*").strip().rstrip(":").strip()
            if c:
                out.append(c)
    return out


_LBL_METHOD = re.compile(r"(?i)(?:withdrawal|payment|cash[\s-]?out)\s+method")
_LBL_AMOUNT = re.compile(r"(?i)(?:withdrawal\s+)?amount")
_LBL_CURRENCY = re.compile(r"(?i)currency")
_LBL_ACCOUNT = re.compile(r"(?i)(?:trading\s+)?account(?:\s+number)?")
_LBL_REFERENCE = re.compile(r"(?i)(?:reference|transaction)(?:\s+(?:number|id|no\.?|#))?")
# Every known detail label PLUS "name" — used so a field's value is taken only when the NEXT cell is a real value, never
# another label's text, and so the member NAME field's value is never pulled as some other field's value.
_ALL_LABELS = (_LBL_METHOD, _LBL_AMOUNT, _LBL_CURRENCY, _LBL_ACCOUNT, _LBL_REFERENCE, re.compile(r"(?i)name"))


def _field_value(cells: list, label_rx: "re.Pattern") -> str:
    """Return the value for ``label_rx``: the cell IMMEDIATELY AFTER the label cell in reading order, but only when that
    next cell is not itself a label (so a label with a missing value yields '' rather than bleeding the next field)."""
    for i, c in enumerate(cells):
        if label_rx.fullmatch(c):
            nxt = cells[i + 1] if i + 1 < len(cells) else ""
            if nxt and not any(lbl.fullmatch(nxt) for lbl in _ALL_LABELS):
                return _norm_punct(nxt)
            return ""
    return ""
# Externality veto for the STRUCTURED path: deposit/funding only. "cancel(led)" is DELIBERATELY excluded — a
# confirmation-required withdrawal legitimately contains "Approve or Cancel" / "will be cancelled", and a genuine
# cancelled/rejected STATUS is already classified by the earlier lifecycle rules (which win over confirmation-required).
_DEPOSIT_VETO = re.compile(r"\b(deposit(?:ed|s|ing)?|funded|funding|top[\s-]?up|topped\s+up)\b", re.IGNORECASE)

_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "SGD", "HKD", "ZAR", "SEK", "NOK", "DKK",
               "PLN", "CZK", "AED", "MXN", "CNH", "CNY"}

# External money-OUT rails (whitelist). Card rails are included (a card withdrawal/refund is money leaving the
# broker to the member's card); the deposit-veto below prevents a card DEPOSIT from being mis-read as external.
_EXTERNAL_RAILS = (
    "visa", "mastercard", "visa card", "mastercard card", "debit card", "credit card", "card",
    "bank wire", "wire transfer", "bank transfer", "international wire", "sepa", "swift", "faster payments",
    "skrill", "neteller", "perfect money", "webmoney", "advcash", "sticpay", "payeer", "paypal",
    "bitcoin", "ethereum", "usdt", "usdc", "tron", "trc20", "erc20", "litecoin", "ripple", "xrp",
)
# LINE-ANCHORED structured field: "Withdrawal method: Visa card" / "Payment method: Bank wire" at line start.
_RAIL_FIELD = re.compile(
    r"(?m)^[^\S\n]{0,8}(?:withdrawal|payment|cash[\s-]?out)\s+method\s*[:\-][^\S\n]{0,8}(?:"
    + "|".join(re.escape(r) for r in _EXTERNAL_RAILS) + r")\b", re.IGNORECASE)
_WITHDRAWAL_DIRECTION = re.compile(
    r"\b(to\s+withdraw|withdrawal\s+request|request(?:ed)?\s+(?:a\s+|your\s+)?withdrawal|"
    r"withdraw(?:al)?\s+(?:of\s+)?funds?|withdrawn|cash[\s-]?out)\b", re.IGNORECASE)
_NOT_EXTERNAL_VETO = re.compile(
    r"\b(deposit(?:ed|s|ing)?|funded|funding|top[\s-]?up|topped\s+up|"
    r"rejected|declined|denied|cancell?ed|canceled|expired|failed)\b", re.IGNORECASE)
_INTERNAL_EVIDENCE = re.compile(
    r"\b(internal transfer|transfer between (your )?[\w ]{0,24}accounts?|between (your )?[\w ]{0,24}accounts?|"
    r"to another of your accounts?|account[- ]to[- ]account|inter[- ]?account|moved internally)\b", re.IGNORECASE)

_LABELED_AMOUNT = re.compile(
    r"(?:withdrawal\s+)?(?:amount|total)\s*[:\-]?\s*(?:(?P<cur1>[A-Za-z]{3}|[$£€])\s*)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:[.,]\d{1,2})?|\d+)\s*(?P<cur2>[A-Za-z]{3})?", re.IGNORECASE)
_REFERENCE = re.compile(
    r"\b(?:reference|ref(?:erence)?\s*(?:id|no\.?|#)?|transaction\s*(?:id|no\.?|#)?|request\s*(?:id|no\.?|#))"
    r"[^\S\n]{0,8}[:#]?[^\S\n]{0,8}([A-Za-z0-9\-]{3,32})", re.IGNORECASE)
# Internal trading-account -> internal trading-account ONLY. This deliberately does NOT capture an EXTERNAL
# destination (a card PAN, bank or wallet number): `destination_account` is a broker INTERNAL trading-account number
# resolved against the owner's GuvFX accounts (an external PAN can never satisfy that match), and storing an external
# PAN/bank number would write an UNREDACTABLE personal identifier into the write-once BrokerEvent — a PII leak. `dst`
# is `\d{4,12}\b`, which structurally refuses a 13+ digit card/bank number (no word boundary exists inside a longer
# digit run), and the required "to (your) account" anchor excludes any card/bank/wallet destination entirely.
_TRANSFER_CLAUSE = re.compile(
    r"\baccount\s+(?P<src>\d{4,12})\b[^\n]{0,40}?\bto\s+(?:your\s+)?account\s+(?P<dst>\d{4,12})\b", re.IGNORECASE)

# Anchored lifecycle phrases, evaluated in this ORDER (terminal/completion before the generic request so a
# "confirm your withdrawal request" email is CONFIRMATION_REQUIRED, and a completion is never inferred from a
# request). Each is a full-text search over "subject\nbody" lowercased.
_LIFECYCLE_RULES = (
    ("WITHDRAWAL_COMPLETED", re.compile(
        r"\bwithdrawal(?:\s+request)?(?:\s+#?\d{1,16})?\s+(?:has\s+been\s+|is\s+now\s+|was\s+)?"
        r"(?:completed|complete)\b|\bfunds\s+(?:have\s+been\s+)?(?:sent|withdrawn|paid\s+out|"
        r"credited\s+to\s+your\s+(?:card|bank|wallet|account))\b|"
        # Broker "processed" completion (Pepperstone): the terminal "We've processed your withdrawal" /
        # "your withdrawal has been processed" notification. PAST-TENSE only — "is being processed" / "will be" /
        # "may be processed" stay NON-terminal (fall through to WITHDRAWAL_PROCESSING), never COMPLETED.
        r"\bwe(?:'ve|\s+have)\s+processed\s+your\s+withdrawal\b|"
        r"\bwithdrawal(?:\s+request)?(?:\s+#?\w{1,20})?\s+(?:has\s+been|have\s+been|was|is\s+now)\s+processed\b",
        re.IGNORECASE)),
    ("WITHDRAWAL_EXPIRED", re.compile(
        r"\b(?:confirmation(?:\s+link|\s+window)?|withdrawal(?:\s+request)?)\s+(?:has\s+)?expired\b|"
        r"\bexpired\s+(?:without|before)\s+confirmation\b", re.IGNORECASE)),
    ("WITHDRAWAL_FAILED", re.compile(
        r"\bwithdrawal(?:\s+request)?(?:\s+#?\d{1,16})?\s+(?:has\s+)?failed\b|"
        r"\b(?:processing|payment|payout)\s+(?:has\s+)?failed\b", re.IGNORECASE)),
    ("WITHDRAWAL_REJECTED", re.compile(
        r"\bwithdrawal(?:\s+request)?(?:\s+#?\d{1,16})?\s+(?:has\s+been\s+|was\s+|been\s+)?"
        r"(?:rejected|declined|denied)\b", re.IGNORECASE)),
    ("WITHDRAWAL_CANCELLED", re.compile(
        r"\bwithdrawal(?:\s+request)?(?:\s+#?\d{1,16})?\s+(?:has\s+been\s+|was\s+|been\s+)?"
        r"(?:cancell?ed|canceled)\b", re.IGNORECASE)),
    ("WITHDRAWAL_CONFIRMED", re.compile(
        r"\bwithdrawal(?:\s+request)?\s+(?:has\s+been\s+)?confirmed\b|"
        r"\b(?:you\s+(?:have\s+)?confirmed|confirmation\s+(?:received|successful|complete))\b", re.IGNORECASE)),
    ("WITHDRAWAL_PROCESSING", re.compile(
        r"\bwithdrawal(?:\s+request)?\s+(?:is\s+)?(?:being\s+processed|in\s+progress|now\s+processing)\b|"
        r"\bprocessing\s+your\s+withdrawal\b", re.IGNORECASE)),
    ("WITHDRAWAL_APPROVED", re.compile(
        r"\bwithdrawal(?:\s+request)?\s+(?:has\s+been\s+)?approved\b", re.IGNORECASE)),
    ("WITHDRAWAL_CONFIRMATION_REQUIRED", re.compile(
        r"\b(?:confirm\s+(?:your\s+)?withdrawal|please\s+confirm\s+(?:your\s+)?withdrawal|"
        r"confirmation\s+(?:is\s+)?required|verify\s+(?:your\s+)?withdrawal(?:\s+request)?|"
        r"action\s+required[^\n]{0,40}withdrawal|"
        # Approve/cancel confirmation flow (Pepperstone): "Approve or Cancel ... withdrawal", the two action controls
        # "Approve withdrawal ... Cancel withdrawal", or the deadline clause "withdrawal confirmation will be cancelled".
        r"approve\s+or\s+cancel[^\n]{0,40}withdrawal|"
        r"approve\s+withdrawal[^\n]{0,80}cancel\s+withdrawal|"
        r"withdrawal\s+confirmation\s+will\s+be\s+cancell?ed)\b", re.IGNORECASE)),
    ("WITHDRAWAL_REQUESTED", re.compile(
        r"\bwithdrawal\s+request\s+(?:has\s+been\s+)?(?:received|submitted|created|registered)\b|"
        r"\bwe(?:'ve|\s+have)\s+received\s+your\s+withdrawal\s+request\b|"
        r"\byou(?:'ve|\s+have)\s+requested\s+a\s+withdrawal\b", re.IGNORECASE)),
)
_EXTERNAL_ELIGIBLE = frozenset({
    "WITHDRAWAL_REQUESTED", "WITHDRAWAL_CONFIRMATION_REQUIRED", "WITHDRAWAL_CONFIRMED",
    "WITHDRAWAL_PROCESSING", "WITHDRAWAL_APPROVED"})


def _norm_currency(tok: str) -> str:
    t = (tok or "").strip().upper()
    return {"$": "USD", "£": "GBP", "€": "EUR"}.get(t, t if t in _CURRENCIES else "")


def _classify_lifecycle(subject: str, body: str) -> Optional[str]:
    t = _norm_punct(f"{subject}\n{body}").lower()   # fold smart punctuation so straight-quote anchors match
    for event_type, rx in _LIFECYCLE_RULES:
        if rx.search(t):
            return event_type
    return None


def _classify_category(subject: str, body: str, lifecycle: Optional[str]) -> str:
    blob = f"{subject}\n{body}"
    if _INTERNAL_EVIDENCE.search(blob):
        return "INTERNAL_TRANSFER"
    if (lifecycle in _EXTERNAL_ELIGIBLE and _RAIL_FIELD.search(blob)
            and _WITHDRAWAL_DIRECTION.search(blob) and not _NOT_EXTERNAL_VETO.search(blob)):
        return "EXTERNAL_WITHDRAWAL"
    return "UNKNOWN"


def _extract_amount(body: str):
    m = _LABELED_AMOUNT.search(body)
    if not m:
        return None, ""
    raw = m.group("num")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{1,2})?", raw):
        raw = raw.replace(",", "")
    elif re.fullmatch(r"\d+,\d{1,2}", raw):
        raw = raw.replace(",", ".")
    cur = _norm_currency(m.group("cur2")) or _norm_currency(m.group("cur1"))
    try:
        val = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None, ""
    return (val, cur) if val > 0 else (None, "")


class _FieldConflict(Exception):
    """A structured field carries internally inconsistent money evidence — fail closed (decline), never guess."""


def _amount_currency_from_fields(amount_s: str, currency_s: str):
    """Parse the STRUCTURED 'Withdrawal amount' + 'Currency' field values (no GBP assumption). Raises _FieldConflict
    when the amount field embeds a currency that disagrees with the Currency field. Returns (Decimal|None, cur)."""
    cur_field = _norm_currency((currency_s or "").strip())
    amount_s = (amount_s or "").strip()
    m = re.search(r"(?P<c1>[A-Za-z]{3}|[$£€])?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:[.,]\d{1,2})?|\d+)"
                  r"\s*(?P<c2>[A-Za-z]{3})?", amount_s)
    if not m:
        return None, cur_field
    raw = m.group("num")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{1,2})?", raw):
        raw = raw.replace(",", "")
    elif re.fullmatch(r"\d+,\d{1,2}", raw):
        raw = raw.replace(",", ".")
    cur_inline = _norm_currency(m.group("c2") or "") or _norm_currency(m.group("c1") or "")
    if cur_field and cur_inline and cur_field != cur_inline:
        raise _FieldConflict("amount-embedded currency %s != currency field %s" % (cur_inline, cur_field))
    try:
        val = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None, (cur_field or cur_inline)
    return (val if val > 0 else None), (cur_field or cur_inline)


def _classify_category_structured(lifecycle: Optional[str], method_value: str, flat: str) -> str:
    """Externality from the STRUCTURED 'Withdrawal method' field value + lifecycle. Under-claims (UNKNOWN) when the rail
    is absent/ambiguous; only claims EXTERNAL_WITHDRAWAL when the method names an external money-out rail, the lifecycle
    is a requesting/in-flight stage, and there is no deposit/internal-transfer evidence."""
    if _INTERNAL_EVIDENCE.search(flat):
        return "INTERNAL_TRANSFER"
    mv = (method_value or "").lower()
    is_external_rail = bool(mv) and any(rail in mv for rail in _EXTERNAL_RAILS)
    if lifecycle in _EXTERNAL_ELIGIBLE and is_external_rail and not _DEPOSIT_VETO.search(flat):
        return "EXTERNAL_WITHDRAWAL"
    return "UNKNOWN"


class _GenericWithdrawalParser:
    """A deterministic per-broker withdrawal parser. One instance per broker (name/version/sender_domain/display).

    When ``html_body`` is supplied (the ingestion gate passes it to parsers that set ``WANTS_HTML_BODY``) the parser
    reads the HTML STRUCTURALLY — pairing each detail label cell with its value cell — because a broker's text/plain
    alternative can be a degraded, value-less rendering. It never runs the free-text regexes directly over raw HTML.
    With no HTML it uses the legacy text path unchanged.

    ``WANTS_HTML_BODY`` is PER-BROKER (default False): only a broker whose text/plain alternative is known to drop the
    detail values (Pepperstone) opts in. A broker with a complete text/plain part (Dominion, Vantage) stays on the
    text path, so the HTML preference never shadows a good text/plain body for them."""

    def __init__(self, *, name: str, version: str, sender_domain: str, display: str, wants_html: bool = False):
        self.name = name
        self.version = version
        self._sender_domain = sender_domain
        self._display = display
        self.WANTS_HTML_BODY = bool(wants_html)

    def can_parse(self, *, subject: str, body: str, from_address: str) -> bool:
        if not domain_matches(_domain_of(from_address), self._sender_domain):
            return False
        t = f"{subject}\n{body}".lower()
        return any(k in t for k in ("withdrawal", "withdraw", "cash out", "cash-out", "payout"))

    def parse(self, *, subject: str, body: str, from_address: str, html_body: str = "") -> Optional[ParsedBrokerEvent]:
        if html_body:
            try:
                ev = self._parse_html(subject=subject, from_address=from_address, html_body=html_body)
            except _FieldConflict:
                return None                     # conflicting money evidence -> fail closed, never guess
            if ev is not None:
                return ev
            # HTML present but no lifecycle recognised there -> try the (possibly degraded) text alternative.
        return self._parse_text(subject=subject, body=body, from_address=from_address)

    def _parse_html(self, *, subject: str, from_address: str, html_body: str) -> Optional[ParsedBrokerEvent]:
        structured = _html_structured_text(html_body)
        flat = _flatten(structured)
        lifecycle = _classify_lifecycle(subject, flat)
        if lifecycle is None:
            return None
        cells = _ordered_cells(structured)
        amount, currency = _amount_currency_from_fields(          # may raise _FieldConflict
            _field_value(cells, _LBL_AMOUNT), _field_value(cells, _LBL_CURRENCY))
        category = _classify_category_structured(lifecycle, _field_value(cells, _LBL_METHOD), flat)
        account = _field_value(cells, _LBL_ACCOUNT)
        source_account = account if re.fullmatch(r"\d{4,12}", account or "") else ""   # internal trading acct only
        reference = _field_value(cells, _LBL_REFERENCE)
        reference = "" if reference.lower().startswith("http") else re.sub(r"[^A-Za-z0-9\-]", "", reference)[:32]
        return ParsedBrokerEvent(
            event_type=lifecycle, transaction_category=category, broker=self._display,
            amount=amount, currency=currency, occurred_at=None, broker_reference_id=reference,
            source_account=source_account, destination_account="", confidence=Decimal("0.800"))

    def _parse_text(self, *, subject: str, body: str, from_address: str) -> Optional[ParsedBrokerEvent]:
        lifecycle = _classify_lifecycle(subject, body)
        if lifecycle is None:
            return None
        category = _classify_category(subject, body, lifecycle)
        amount, currency = _extract_amount(body)
        ref_m = _REFERENCE.search(body)
        reference = ref_m.group(1) if ref_m else ""
        if reference.lower().startswith("http"):
            reference = ""
        pairs = {(m.group("src"), m.group("dst")) for m in _TRANSFER_CLAUSE.finditer(body)}
        source_account, destination_account = next(iter(pairs)) if len(pairs) == 1 else ("", "")
        return ParsedBrokerEvent(
            event_type=lifecycle, transaction_category=category, broker=self._display,
            amount=amount, currency=currency, occurred_at=None, broker_reference_id=reference,
            source_account=source_account, destination_account=destination_account,
            confidence=Decimal("0.750"))


# One parser per in-scope broker. Sender domains mirror the seeded BrokerProfile approved_sender_domains. Only
# Pepperstone opts into the HTML path (its text/plain alternative drops the withdrawal-detail values).
PEPPERSTONE = _GenericWithdrawalParser(name="pepperstone", version="v1", sender_domain="pepperstone.com",
                                       display="Pepperstone", wants_html=True)
DOMINION = _GenericWithdrawalParser(name="dominion_markets", version="v1", sender_domain="dominionmarkets.com",
                                    display="Dominion Markets")
VANTAGE = _GenericWithdrawalParser(name="vantage", version="v1", sender_domain="vantagemarkets.com",
                                   display="Vantage")

STREAM_B_PARSERS = (PEPPERSTONE, DOMINION, VANTAGE)


def register_stream_b_parsers() -> None:
    """Register the Stream B adapters (idempotent by (name, version)). Called from register_default_parsers."""
    for p in STREAM_B_PARSERS:
        register_parser(p)


# Static Protocol conformance guard (no runtime effect).
_PROTOCOL_OK: BrokerEmailParser = PEPPERSTONE
