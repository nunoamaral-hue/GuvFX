"""
Trade Result Intelligence Producer (Phase 7B).

Packages a **closed trade** from the authoritative GuvFX trade source
(``trading.models.Trade`` — or any object/mapping exposing the same fields) into
an immutable Trade Result Intelligence Envelope.

This phase is packaging + delivery, **not trade execution**. The producer never
opens/closes a trade; it only describes the outcome of one that already closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from .envelope import TradeResultIntelligenceEnvelope, TradeResultPayload

GUVFX_TRADE_SOURCE = "GUVFX_TRADE_HISTORY"


def _get(trade, key, default=None):
    """Read ``key`` from a Trade-like object or a mapping."""
    if isinstance(trade, Mapping):
        return trade.get(key, default)
    return getattr(trade, key, default)


def _to_decimal(value):
    if value in (None, ""):
        return Decimal("0")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _iso(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# Crypto bases whose "pip" (per Wayond's convention) is one whole price unit — a
# BTCUSD move 63200 -> 63450 is +250 pips, NOT +2,500,000 (the FX 0.0001 default).
_CRYPTO_BASES = ("BTC", "ETH", "LTC", "XRP", "BCH", "SOL", "ADA", "DOGE", "DOT", "BNB", "AVAX", "LINK")


def _pip_size(symbol: str) -> Decimal:
    """Best-effort pip size by instrument (documented heuristic, not exact)."""
    s = (symbol or "").upper()
    if s.endswith("JPY"):
        return Decimal("0.01")
    if "XAU" in s or "GOLD" in s:
        return Decimal("0.1")
    if "XAG" in s or "SILVER" in s:
        return Decimal("0.01")
    if any(idx in s for idx in ("US30", "NAS", "NDX", "SPX", "US500", "GER", "UK100", "DOW")):
        return Decimal("1")
    # Crypto (BTCUSD, ETHUSD, …): one whole unit per pip, matching Wayond's captions.
    if any(s.startswith(cb) for cb in _CRYPTO_BASES):
        return Decimal("1")
    return Decimal("0.0001")


def _as_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        from django.utils.dateparse import parse_datetime
        return parse_datetime(value)
    return None


def _duration(open_time, close_time) -> str:
    a, b = _as_datetime(open_time), _as_datetime(close_time)
    if a and b:
        return str(b - a)
    return ""


def _outcome(net_pnl: Decimal) -> str:
    # Mirrors wims ConsumptionContract.ResultType values.
    if net_pnl > 0:
        return "WIN"
    if net_pnl < 0:
        return "LOSS"
    return "BREAKEVEN"


class TradeResultProducer:
    """Builds immutable envelopes from closed GuvFX trades."""

    source = GUVFX_TRADE_SOURCE

    def produce(self, trade) -> TradeResultIntelligenceEnvelope:
        """Wrap a closed ``trade`` in an immutable Trade Result envelope.

        Pure: no I/O, no persistence, no audit. Raises ``ValueError`` if the
        trade is not closed (no close_time / close_price).
        """
        close_time = _get(trade, "close_time")
        close_price = _get(trade, "close_price")
        if close_time in (None, "") or close_price in (None, ""):
            raise ValueError("Trade is not closed (missing close_time/close_price).")

        symbol = str(_get(trade, "symbol", ""))
        side = str(_get(trade, "side", ""))
        open_price = _to_decimal(_get(trade, "open_price"))
        close_price_d = _to_decimal(close_price)
        net_pnl = (
            _to_decimal(_get(trade, "profit"))
            + _to_decimal(_get(trade, "commission"))
            + _to_decimal(_get(trade, "swap"))
        )
        outcome = _outcome(net_pnl)

        direction_sign = Decimal("1") if side.upper() == "BUY" else Decimal("-1")
        pips = (close_price_d - open_price) / _pip_size(symbol) * direction_sign
        pips = pips.quantize(Decimal("0.1"))

        open_time = _get(trade, "open_time")
        trade_id = str(_get(trade, "ticket", "") or _get(trade, "id", "") or "")
        signal_id = str(_get(trade, "signal_id", "") or "")
        summary = (
            f"{symbol} {side} closed {outcome}: net pnl {net_pnl}, {pips} pips."
        )

        payload = TradeResultPayload(
            trade_id=trade_id,
            signal_id=signal_id,
            market=symbol,
            direction=side,
            open_time=_iso(open_time),
            close_time=_iso(close_time),
            duration=_duration(open_time, close_time),
            pnl=str(net_pnl),
            pips=str(pips),
            outcome=outcome,
            confidence="",  # realised outcome — no predictive confidence
            summary=summary,
        )
        return TradeResultIntelligenceEnvelope(
            intelligence_id=uuid.uuid4().hex,
            source=self.source,
            timestamp=_iso(close_time),
            confidence="",
            summary=summary,
            structured_payload=payload,
        )
