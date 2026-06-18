"""
Adapter: SCE ``SignalResult`` → Flow A ``OpenTradeCandidate``.

A thin, lossless field mapping — no strategy logic, no decisions. SCE decides;
this only reshapes the result into the Flow A candidate the suppression path
already understands. ``SignalResult`` carries no confidence, so a fixed
shadow-only convention label is used (NOT a derived score).
"""

from __future__ import annotations

from flow_a.types import OpenTradeCandidate

# Shadow-only confidence convention. SCE emits no confidence score; we do NOT
# fabricate one — this is a constant marker recorded in the candidate comment.
SCE_SHADOW_CONFIDENCE = "engine:SCE"


def signal_result_to_candidate(result, *, symbol: str, timeframe: str,
                               risk_per_trade_pct: str) -> OpenTradeCandidate:
    """Map an accepted SCE BUY/SELL SignalResult to a Flow A candidate."""
    if result.signal_type not in ("BUY", "SELL"):
        raise ValueError("only BUY/SELL SignalResults convert to a candidate")

    def s(v):
        return None if v is None else str(v)

    return OpenTradeCandidate(
        symbol=result.symbol or symbol,
        direction=result.signal_type,
        timeframe=timeframe,
        entry_type="MARKET",
        entry_price=s(result.entry_price),
        sl_price=str(result.sl_price),
        tp_price=s(result.tp_price),
        risk_per_trade_pct=str(risk_per_trade_pct),
        comment=f"sce-replay:{result.symbol or symbol}:{SCE_SHADOW_CONFIDENCE}",
    )
