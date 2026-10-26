"""trade_copier.canonical — canonical economic-instrument identity (multi-broker mapping matrix).

Resolve a broker-specific MT5 symbol to a CANONICAL economic identity (asset class, underlying, quote
currency, contract type, expiry) WITHOUT replacing the native broker symbol, and — crucially — WITHOUT
equating two symbols by stripped name alone (``.claude/rules`` / packet §2).

The name-derived identity is cross-checked against the broker's own metadata currencies. When they
disagree — e.g. a GBP-settled spread bet named ``XAUUSD_SB`` whose profit currency is GBP, not the USD
the name implies — the resolution is marked LOW confidence with an explicit ambiguity reason, so the
mapping layer FAILS CLOSED instead of silently treating it as a USD-settled ``XAUUSD``.

This module is PURE: no network, no DB read or write, no order. It only classifies strings + metadata.
The ``CanonicalInstrument`` model is persisted ONLY by the refresh command; the read/API path compares
``CanonicalKey`` values directly, so a read never writes a canonical row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from execution.broker_symbols import base_symbol, normalize_symbol

# ISO-4217 currency codes we recognise as a quote/settlement currency. A conservative, explicit set:
# an unknown second token means we do NOT confidently know the quote currency, so the resolution is
# left low/none-confidence rather than guessed.
_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "SGD", "HKD", "CNH", "CNY",
    "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "ZAR", "MXN", "TRY", "THB", "ILS", "AED", "SAR",
})
# Precious-metal underlyings (MT5 names them with an X-prefix ISO-ish code).
_METALS = frozenset({"XAU", "XAG", "XPT", "XPD"})
# Trade modes that mean "cannot open a new position in the ordinary sense": 0=disabled, 3=close-only.
_NON_OPENABLE_TRADE_MODES = frozenset({0, 3, "0", "3"})

CONF_HIGH = "high"
CONF_LOW = "low"
CONF_NONE = "none"


@dataclass(frozen=True)
class CanonicalKey:
    """The equality key that groups broker symbols sharing one economic underlying.

    Deliberately keyed on the PRICE reference (asset class + underlying + quote currency + optional
    expiry) — NOT on settlement currency, contract size or contract type, which are broker-instance
    attributes surfaced as *material differences* on a candidate rather than silently folded away.
    ``slug`` is the stable string identity used as the ``CanonicalInstrument.key``."""

    asset_class: str
    underlying: str
    quote_currency: str
    expiry: Optional[str] = None  # ISO date for dated instruments; None for spot/perpetual

    @property
    def slug(self) -> str:
        base = f"{self.asset_class}:{self.underlying}:{self.quote_currency}"
        return f"{base}:{self.expiry}" if self.expiry else base


@dataclass
class CanonicalResolution:
    """The full resolution of one broker symbol: its canonical key (or None if unresolved) plus the
    broker-instance attributes and a confidence signal with explicit ambiguity reasons."""

    broker_symbol: str
    key: Optional[CanonicalKey]
    asset_class: str
    underlying: str
    quote_currency: Optional[str]
    settlement_currency: Optional[str]
    contract_type: str
    contract_size: Optional[Decimal]
    expiry: Optional[str]
    confidence: str
    ambiguity_reasons: list = field(default_factory=list)
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


def _symbol_suffix(broker_symbol: str) -> str:
    """The broker suffix after the leading alphanumeric run, upper-cased: ``XAUUSD_SB`` -> ``_SB``."""
    s = normalize_symbol(broker_symbol)
    for i, ch in enumerate(s):
        if not ch.isalnum():
            return s[i:]
    return ""


def _is_spread_bet(broker_symbol: str, path: str) -> bool:
    suffix = _symbol_suffix(broker_symbol)
    if "SB" in suffix:
        return True
    return "SPREAD" in (path or "").upper()


def _asset_class_underlying_quote(base: str):
    """Derive (asset_class, underlying, quote_currency) from the base symbol NAME only.

    Returns quote_currency=None when the name does not confidently encode a quote currency (an unknown
    token), so the caller marks the resolution unresolved rather than guessing."""
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
    left = _DISPLAY_UNDERLYING.get(underlying, underlying)
    return f"{left} / {quote}"


def resolve_canonical(broker_symbol: str, metadata: Optional[dict] = None) -> CanonicalResolution:
    """Classify one broker symbol. Pure; never raises on odd metadata (fails to CONF_NONE instead)."""
    meta = metadata or {}
    sym = str(broker_symbol or "")
    base = base_symbol(sym)
    asset_class, underlying, quote = _asset_class_underlying_quote(base)

    profit_ccy = str(meta.get("currency_profit") or "").upper() or None
    settlement = profit_ccy or quote
    contract_size = _dec(meta.get("contract_size"))
    path = str(meta.get("path") or "")
    contract_type = "SPREADBET" if _is_spread_bet(sym, path) else (
        "SPOT" if asset_class in ("METAL", "FX") else "UNKNOWN")

    reasons: list = []
    if quote is None:
        confidence = CONF_NONE
        reasons.append("quote_currency_unresolved_from_name")
        key = None
    else:
        confidence = CONF_HIGH
        # Anti "name-only" check: the broker's own P&L currency must corroborate the name's quote
        # currency. A spread bet named XAUUSD_SB that settles in GBP must NOT be auto-equated to a
        # USD-settled XAUUSD — the mismatch drops confidence and is recorded, so a cross-pairing fails
        # closed as AMBIGUOUS.
        if profit_ccy and profit_ccy != quote:
            confidence = CONF_LOW
            reasons.append(f"settlement_currency_differs_from_quote:{profit_ccy}!={quote}")
        if contract_type == "SPREADBET":
            confidence = CONF_LOW if confidence == CONF_HIGH else confidence
            reasons.append("spread_bet_contract_type")
        key = CanonicalKey(asset_class=asset_class, underlying=underlying, quote_currency=quote, expiry=None)

    return CanonicalResolution(
        broker_symbol=sym, key=key, asset_class=asset_class, underlying=underlying,
        quote_currency=quote, settlement_currency=settlement, contract_type=contract_type,
        contract_size=contract_size, expiry=None, confidence=confidence,
        ambiguity_reasons=reasons, display_name=_display_name(asset_class, underlying, quote),
    )
