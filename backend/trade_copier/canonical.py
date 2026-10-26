"""trade_copier.canonical — canonical economic-instrument identity (multi-broker mapping matrix).

Resolve a broker-specific MT5 symbol to a CANONICAL economic identity WITHOUT replacing the native
symbol and WITHOUT equating two symbols by stripped name alone (packet §2).

The economic dimensions are modelled SEPARATELY and never conflated:

  * asset class + underlying/base asset          — what is traded
  * price quote / reference currency             — what the price is denominated in (from the NAME)
  * settlement / P&L currency                    — what profit accrues in  (broker ``currency_profit``)
  * margin currency                              — what margin is held in  (broker ``currency_margin``)
  * contract type (SPOT/CFD, SPREADBET, FUTURE)  — the instrument form
  * contract size                                — units per 1.0 lot
  * expiry                                       — for dated instruments

The CANONICAL IDENTITY is the price reference only: ``(asset_class, underlying, quote_currency, +expiry)``.
Settlement/margin currency, contract type and contract size are broker-instance attributes, NOT identity:
Pepperstone ``XAUUSD_SB`` references USD gold pricing (its canonical identity IS ``METAL:XAU:USD``) while
its P&L settles in GBP — that is a *material compatibility warning* on a cross-broker pairing, not evidence
that the XAU/USD identity is wrong. So ``currency_profit`` is NEVER used as a proxy for the quote currency,
and the settlement/contract-type facts do NOT lower identity confidence; they are reported and drive
cross-pair compatibility separately (see ``trade_copier.matrix``).

Pure: no network, no DB read or write, no order. The ``CanonicalInstrument`` model is persisted only by the
refresh command; the read/API path compares ``CanonicalKey`` values directly, so a read never writes a row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from execution.broker_symbols import base_symbol, normalize_symbol

# ISO-4217 codes we recognise as a quote/settlement/margin currency (conservative, explicit set).
_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "SGD", "HKD", "CNH", "CNY",
    "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "ZAR", "MXN", "TRY", "THB", "ILS", "AED", "SAR",
})
_METALS = frozenset({"XAU", "XAG", "XPT", "XPD"})

# Identity confidence: confidence that the CANONICAL IDENTITY (asset/underlying/quote) was read correctly —
# NOT a statement about cross-broker compatibility. HIGH when the name confidently encodes a known
# underlying + known quote; NONE when it does not. (There is deliberately no settlement-based downgrade.)
CONF_HIGH = "high"
CONF_NONE = "none"

CT_SPOT = "SPOT"
CT_SPREADBET = "SPREADBET"
CT_FUTURE = "FUTURE"
CT_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CanonicalKey:
    """The equality key that groups broker symbols sharing one economic underlying: the PRICE reference
    (asset class + underlying + quote currency + optional expiry). ``slug`` is the stable ``CanonicalInstrument.key``."""

    asset_class: str
    underlying: str
    quote_currency: str
    expiry: Optional[str] = None

    @property
    def slug(self) -> str:
        base = f"{self.asset_class}:{self.underlying}:{self.quote_currency}"
        return f"{base}:{self.expiry}" if self.expiry else base


@dataclass
class CanonicalResolution:
    """One broker symbol fully resolved: canonical key (or None) + the SEPARATE economic dimensions + the
    identity confidence (never influenced by settlement/contract type)."""

    broker_symbol: str
    key: Optional[CanonicalKey]
    asset_class: str
    underlying: str
    quote_currency: Optional[str]       # price reference (from the name)
    settlement_currency: Optional[str]  # P&L currency (broker currency_profit)
    margin_currency: Optional[str]      # margin currency (broker currency_margin)
    contract_type: str
    contract_size: Optional[Decimal]
    expiry: Optional[str]
    identity_confidence: str
    identity_notes: list = field(default_factory=list)
    display_name: str = ""

    @property
    def resolved(self) -> bool:
        return self.key is not None


def _dec(v):
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _ccy(v) -> Optional[str]:
    s = str(v or "").upper().strip()
    return s or None


def _symbol_suffix(broker_symbol: str) -> str:
    s = normalize_symbol(broker_symbol)
    for i, ch in enumerate(s):
        if not ch.isalnum():
            return s[i:]
    return ""


def _contract_type(broker_symbol: str, path: str, expiry: Optional[str], asset_class: str) -> str:
    suffix = _symbol_suffix(broker_symbol)
    up_path = (path or "").upper()
    if "SB" in suffix or "SPREAD" in up_path:
        return CT_SPREADBET
    if expiry:
        return CT_FUTURE
    if asset_class in ("METAL", "FX"):
        return CT_SPOT
    return CT_UNKNOWN


def _asset_class_underlying_quote(base: str):
    b = base.upper()
    if len(b) >= 6 and b[:3] in _METALS and b[3:6] in _CURRENCIES:
        return "METAL", b[:3], b[3:6]
    if len(b) == 6 and b[:3] in _CURRENCIES and b[3:6] in _CURRENCIES:
        return "FX", b[:3], b[3:6]
    return "UNKNOWN", b, None


_DISPLAY_UNDERLYING = {"XAU": "Gold", "XAG": "Silver", "XPT": "Platinum", "XPD": "Palladium"}


def _display_name(asset_class: str, underlying: str, quote: Optional[str]) -> str:
    if quote is None:
        return underlying
    return f"{_DISPLAY_UNDERLYING.get(underlying, underlying)} / {quote}"


def resolve_canonical(broker_symbol: str, metadata: Optional[dict] = None) -> CanonicalResolution:
    """Classify one broker symbol. Pure; never raises on odd metadata (identity falls to CONF_NONE instead)."""
    meta = metadata or {}
    sym = str(broker_symbol or "")
    base = base_symbol(sym)
    asset_class, underlying, quote = _asset_class_underlying_quote(base)

    # SEPARATE dimensions — settlement/margin come from the broker, NOT from the name, and are never used
    # as a proxy for the price quote currency.
    settlement = _ccy(meta.get("currency_profit"))
    margin = _ccy(meta.get("currency_margin"))
    contract_size = _dec(meta.get("contract_size"))
    expiry = None  # dated-future expiry parsing is a future extension; spot symbols carry none
    contract_type = _contract_type(sym, str(meta.get("path") or ""), expiry, asset_class)

    notes: list = []
    if quote is None:
        identity_confidence = CONF_NONE
        notes.append("quote_currency_unresolved_from_name")
        key = None
    else:
        identity_confidence = CONF_HIGH   # the IDENTITY (XAU/USD) is read confidently from the name
        key = CanonicalKey(asset_class=asset_class, underlying=underlying, quote_currency=quote, expiry=expiry)

    return CanonicalResolution(
        broker_symbol=sym, key=key, asset_class=asset_class, underlying=underlying, quote_currency=quote,
        settlement_currency=settlement, margin_currency=margin, contract_type=contract_type,
        contract_size=contract_size, expiry=expiry, identity_confidence=identity_confidence,
        identity_notes=notes, display_name=_display_name(asset_class, underlying, quote),
    )
