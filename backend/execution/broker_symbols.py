"""
GFX-PKT-BROKER-SYMBOL-REGISTRY — broker/account-aware symbol resolution.

Replaces the hardcoded ``SIGNAL_ALLOWED_SYMBOLS`` allowlist for the Wayond signal path. A valid
signal (mandatory SL+TP, market entry) trades ONLY if its symbol resolves to a symbol the account's
broker actually offers; otherwise it is rejected FAIL-CLOSED with a clear reason. The provider
(Wayond) symbol is always preserved for audit; the resolved BROKER symbol is what the order is
placed under.

``can_account_trade_symbol(account, provider_symbol)`` resolves:
  1. account has synced ``BrokerInstrument`` rows → resolve against them (exact match first, then
     unique base-symbol map for broker suffixes like ``BTCUSD.`` / ``XAUUSD+``);
  2. account has NONE → fall back to the DEFAULT baseline (the legacy SIGNAL_ALLOWED_SYMBOLS) so
     existing behaviour is preserved until a broker sync runs — UNLESS ``BROKER_SYMBOL_REGISTRY_STRICT``
     is set, in which case it fails closed (``SYMBOL_REGISTRY_UNAVAILABLE``).

Reject reasons: ``SYMBOL_NOT_AVAILABLE_ON_BROKER`` / ``SYMBOL_MAPPING_AMBIGUOUS`` /
``SYMBOL_REGISTRY_UNAVAILABLE`` / ``invalid_symbol``.

BOUNDARY: this module places NO order and makes NO network/MT5 call — it only reads
``BrokerInstrument`` rows and normalises strings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from execution.models import SIGNAL_ALLOWED_SYMBOLS, BrokerInstrument

# Reject reason codes (fail-closed).
SYMBOL_NOT_AVAILABLE_ON_BROKER = "SYMBOL_NOT_AVAILABLE_ON_BROKER"
SYMBOL_MAPPING_AMBIGUOUS = "SYMBOL_MAPPING_AMBIGUOUS"
SYMBOL_REGISTRY_UNAVAILABLE = "SYMBOL_REGISTRY_UNAVAILABLE"
INVALID_SYMBOL = "invalid_symbol"
# Accept reason codes.
REASON_EXACT = "broker_symbol_exact"
REASON_MAPPED = "broker_symbol_mapped"
REASON_DEFAULT = "default_baseline"

#: Symbols an account with NO synced broker cache may trade (preserves legacy behaviour until a
#: broker sync runs). Derived from the legacy allowlist — NOT widened.
DEFAULT_BASELINE_SYMBOLS = tuple(s.upper() for s in SIGNAL_ALLOWED_SYMBOLS)


@dataclass(frozen=True)
class SymbolResolution:
    accepted: bool
    provider_symbol: str
    reason: str
    broker_symbol: Optional[str] = None
    metadata: dict = field(default_factory=dict)


def normalize_symbol(symbol) -> str:
    return str(symbol or "").strip().upper()


def base_symbol(broker_symbol) -> str:
    """The base instrument of a broker symbol, stripping a broker suffix.

    The suffix is the trailing run starting at the first non-alphanumeric char, e.g.
    ``BTCUSD.`` / ``XAUUSD+`` / ``EURUSD.r`` -> ``BTCUSD`` / ``XAUUSD`` / ``EURUSD``. Deterministic;
    the sync command stores this so provider matching is exact.
    """
    out = []
    for ch in normalize_symbol(broker_symbol):
        if ch.isalnum():
            out.append(ch)
        else:
            break
    return "".join(out)


def _strict() -> bool:
    return os.getenv("BROKER_SYMBOL_REGISTRY_STRICT", "").strip().lower() in ("1", "true", "yes", "on")


def _accept(sym, broker_symbol, reason, metadata=None) -> SymbolResolution:
    return SymbolResolution(True, sym, reason, broker_symbol, metadata or {})


def _reject(sym, reason) -> SymbolResolution:
    return SymbolResolution(False, sym, reason, None, {})


def can_account_trade_symbol(account, provider_symbol) -> SymbolResolution:
    """Resolve ``provider_symbol`` to a broker symbol on ``account``. Fail-closed. Read-only."""
    sym = normalize_symbol(provider_symbol)
    if not sym:
        return _reject(sym, INVALID_SYMBOL)
    try:
        instruments = list(BrokerInstrument.objects.filter(account=account, enabled=True))
    except Exception:  # pragma: no cover - defensive: registry table/query failure
        return _reject(sym, SYMBOL_REGISTRY_UNAVAILABLE)

    if instruments:
        exact = [i for i in instruments if normalize_symbol(i.broker_symbol) == sym]
        if len(exact) == 1:
            return _accept(sym, exact[0].broker_symbol, REASON_EXACT, exact[0].metadata)
        if len(exact) > 1:
            return _reject(sym, SYMBOL_MAPPING_AMBIGUOUS)
        mapped = [i for i in instruments if normalize_symbol(i.base_symbol) == sym]
        if len(mapped) == 1:
            return _accept(sym, mapped[0].broker_symbol, REASON_MAPPED, mapped[0].metadata)
        if len(mapped) > 1:
            return _reject(sym, SYMBOL_MAPPING_AMBIGUOUS)
        return _reject(sym, SYMBOL_NOT_AVAILABLE_ON_BROKER)

    # No synced broker cache for this account.
    if _strict():
        return _reject(sym, SYMBOL_REGISTRY_UNAVAILABLE)
    if sym in DEFAULT_BASELINE_SYMBOLS:
        return _accept(sym, sym, REASON_DEFAULT, {})
    return _reject(sym, SYMBOL_NOT_AVAILABLE_ON_BROKER)
