"""
GFX-PKT-CANONICAL-TRADE-RESULT — the single canonical representation of a completed trade.

``CanonicalTradeResult`` is the ONE source of truth for a closed trade's result. Every downstream
consumer (the Stakeholder Telegram review channel, WIMS, and future social channels) renders FROM
this object — only the renderer changes, never the trade object. It is a pure, immutable dataclass:
building one is READ-ONLY (no order, no transmit, no publish, no mutation). It carries the trade
facts, the signal/parser provenance, the execution context, and the *references* a renderer needs
to produce a result card + caption + statistics block.

Boundary: this module imports NO transport, NO execution app, and places NO order (ADR-009:
intelligence never imports execution). Signal/plan linkage is supplied by the caller as a plain
``linkage`` dict — the execution-side caller resolves it (it owns the plan) and hands it in. The
only heavy dependency (the PNG/SVG results card, which pulls Pillow) is imported lazily and ONLY
when media is requested, so the execution notification path stays light.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .trade_result_producer import TradeResultProducer, _get, _to_decimal


@dataclass(frozen=True)
class LegResult:
    """One take-profit leg of a multi-leg signal, for the progressive trade-evidence section.

    A Wayond signal splits into up to N legs (one per TP). Each leg closes independently, so a
    result is emitted per leg close — this carries that leg's facts and whether it is CLOSED (hit
    its TP), OPEN (filled, still running) or PENDING (not yet filled). Pure data; frozen.
    """

    index: int             # 1-based leg index
    tp_label: str          # "TP1" / "TP2" / "TP3"
    direction: str         # BUY / SELL
    volume: str            # lot size, formatted (e.g. "0.02")
    entry: str             # actual fill (or the plan reference entry if not yet filled)
    target: str            # this leg's TP target price
    exit: str              # close price — "" while not closed
    profit: str            # net profit for this leg — "" while not closed
    status: str            # CLOSED / OPEN / PENDING
    close_time: str        # ISO close time — "" while not closed
    pips: str = ""         # this leg's pip result (closed legs only; symbol-aware)


@dataclass(frozen=True)
class CanonicalTradeResult:
    """Immutable, transport-agnostic representation of one completed trade result.

    This is the contract every renderer consumes. Wording/format lives in the renderers; this
    object holds only DATA + rendering references. Frozen: fields cannot be reassigned.
    """

    # --- provenance / linkage ---
    strategy: str          # strategy lineage (falls back to the signal source when unlinked)
    provider: str          # signal provider slug (e.g. "wayond")
    signal_id: str         # originating approval message id
    correlation_id: str    # end-to-end trace id
    parser_profile: str    # parser profile slug
    parser_confidence: str # parser certification level (LOW/MEDIUM/HIGH)
    execution_mode: str    # SHADOW / DEMO / ... (mode the order ran under)
    account_label: str
    is_demo: bool          # demo/live indicator

    # --- instrument / prices ---
    symbol: str
    direction: str
    reference_entry: str   # provider's advisory signal entry (reference only, per §6A)
    actual_fill: str       # the real market fill (open price)
    stop_loss: str
    take_profit: str
    exit: str              # close price

    # --- result ---
    pips: str
    gross_pnl: str         # profit only (before commission/swap)
    net_pnl: str           # profit + commission + swap
    outcome: str           # WIN / LOSS / BREAKEVEN
    execution_duration: str
    trade_timestamp: str      # open time (ISO)
    execution_timestamp: str  # close time (ISO)
    summary: str

    # --- rendering references (single source; renderers consume these) ---
    statistics: dict = field(default_factory=dict)     # statistics block
    card_rows: tuple = ()                               # result-card + caption reference data
    result_card: Optional[dict] = None                 # rendered card media {png_base64,svg,...}
    caption: Optional[str] = None                       # rendered social caption
    currency: str = "$"

    # --- progressive multi-leg evidence (GFX-PKT-CANONICAL-LEG-EVIDENCE) ---
    # Populated from the execution-side leg resolver (ADR-009: intelligence never queries the
    # plan itself). Empty for single-leg / non-plan trades; renderers degrade gracefully.
    strategy_display_name: str = ""                    # human strategy name (e.g. "Wayond Auto Demo")
    legs: tuple = ()                                   # tuple[LegResult] — all legs (closed + open)
    take_profits: tuple = ()                           # ("2367.50","2371.50","2375.50")
    progress: dict = field(default_factory=dict)       # {"closed":1,"total":3,"label":"TP1","final":False}

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


#: Keys the caller may supply in ``linkage`` (all optional; blanks where absent). The
#: execution-side caller resolves these from the SignalExecutionPlan and passes them in.
LINKAGE_KEYS = (
    "reference_entry", "stop_loss", "take_profit", "source", "signal_id",
    "provider", "parser_profile", "parser_confidence", "execution_mode",
)


def _gross_pnl(trades) -> Decimal:
    """Gross = summed raw profit (before commission/swap) across the trade(s)."""
    return sum((_to_decimal(_get(t, "profit")) for t in trades), Decimal("0"))


def _aggregate(trades, total_net: Decimal) -> dict:
    """One aggregate trade dict from a (possibly partial-close) sequence — mirrors the WIMS path."""
    first, last = trades[0], trades[-1]
    if len(trades) == 1:
        return {
            "ticket": str(_get(first, "ticket", "") or ""),
            "symbol": _get(first, "symbol"), "side": _get(first, "side"),
            "open_time": _get(first, "open_time"), "close_time": _get(first, "close_time"),
            "open_price": _get(first, "open_price"), "close_price": _get(first, "close_price"),
            "profit": _get(first, "profit"), "commission": _get(first, "commission", 0),
            "swap": _get(first, "swap", 0), "signal_id": _get(first, "signal_id", ""),
        }
    return {
        "ticket": str(_get(first, "ticket", "") or ""),
        "symbol": _get(first, "symbol"), "side": _get(first, "side"),
        "open_time": _get(first, "open_time"), "close_time": _get(last, "close_time"),
        "open_price": _get(first, "open_price"), "close_price": _get(last, "close_price"),
        "profit": str(total_net), "commission": "0", "swap": "0",
    }


def _leg_with_pips(leg: dict, symbol: str) -> LegResult:
    """Build a LegResult from a resolver leg dict, computing this leg's pips for a CLOSED leg.

    Reuses the symbol-aware pip calc (``caption._pips_for`` -> ``_pip_size``) so gold/crypto/JPY
    render correct per-TP pips. Open/pending legs carry no pips (not realised)."""
    from .caption import _pips_for  # local: symbol-aware pip calc, no Pillow

    d = dict(leg)
    d.setdefault("pips", "")
    if d.get("status") == "CLOSED" and d.get("exit") and d.get("entry"):
        p = _pips_for(symbol, d.get("entry"), d.get("exit"), d.get("direction"))
        d["pips"] = "" if p is None else f"{p:.1f}"
    return LegResult(**d)


def build_canonical_trade_result(
    trade, *, correlation_id: str = "", signal_source: str = "",
    account_label: str = "GuvFX", currency: str = "$", with_media: bool = False,
    linkage: Optional[dict] = None, leg_evidence: Optional[dict] = None,
) -> CanonicalTradeResult:
    """Build the ONE canonical result for a closed trade (or partial-close sequence).

    ``trade`` is a ``trading.models.Trade``-like object/mapping, or a list of them (partial
    closes of one order). ``linkage`` is an optional dict (see ``LINKAGE_KEYS``) supplied by the
    execution-side caller — it carries the provider/parser/plan facts intelligence must not look
    up itself (ADR-009). Read-only: derives everything from the trade(s) + linkage; never mutates,
    orders, transmits, or publishes. ``with_media=True`` eagerly renders the result card + caption
    (WIMS path); the light execution/Telegram path leaves them ``None``.
    """
    link = linkage or {}
    trades = list(trade) if isinstance(trade, (list, tuple)) else [trade]
    total_net = sum(
        (
            _to_decimal(_get(t, "profit")) + _to_decimal(_get(t, "commission"))
            + _to_decimal(_get(t, "swap"))
            for t in trades
        ),
        Decimal("0"),
    )
    agg = _aggregate(trades, total_net)
    payload = TradeResultProducer().produce(agg).structured_payload  # pnl/pips/outcome/duration

    acct = _get(trades[0], "account", None)
    is_demo = bool(getattr(acct, "is_demo", False))
    label = getattr(acct, "name", None) or account_label
    # ``strategy`` matches the deployed envelope EXACTLY: signal source, else the plan source
    # (NO provider fallback — behaviour-preserving). The provider slug lives on ``provider``.
    strategy = signal_source or link.get("source", "")
    provider = link.get("provider", "") or signal_source or link.get("source", "")

    gross = _gross_pnl(trades)
    net = _to_decimal(payload.pnl)
    statistics = {
        "symbol": payload.market, "direction": payload.direction, "outcome": payload.outcome,
        "pips": str(payload.pips), "gross_pnl": str(gross), "net_pnl": str(net),
        "duration": payload.duration, "currency": currency,
    }

    # Progressive multi-leg evidence (supplied by the execution-side resolver; ADR-009 keeps
    # intelligence out of the plan). Absent for single-leg / non-plan trades -> empty, renderers
    # fall back to the top-level facts.
    ev = leg_evidence or {}
    legs = tuple(_leg_with_pips(leg, payload.market) for leg in ev.get("legs", ()))
    take_profits = tuple(ev.get("take_profits", ()) or ())
    progress = dict(ev.get("progress", {}) or {})
    strategy_display_name = str(ev.get("strategy_display_name", "") or "")

    card_rows: tuple = ()
    result_card = None
    caption = None
    if with_media:
        # WIMS/social path: the partial-close rows card. The redesigned stakeholder card
        # (per-TP legs) is a separate render — ``results_card.render_result_card`` — driven by
        # the Telegram stakeholder path, so this WIMS behaviour is unchanged.
        from .results_card import render_card, row_from_trade  # local: pulls Pillow only here
        from .caption import build_caption
        rows = [row_from_trade(t) for t in trades]
        card_rows = tuple(rows)
        result_card = render_card(
            rows, title=f"{rows[0].symbol} {rows[0].direction} winning trade",
            total_profit=str(total_net),
        )
        caption = build_caption(rows, net_profit=total_net, currency=currency)

    return CanonicalTradeResult(
        strategy=strategy, provider=provider, signal_id=link.get("signal_id", "") or str(payload.signal_id or ""),
        correlation_id=correlation_id or "", parser_profile=link.get("parser_profile", ""),
        parser_confidence=link.get("parser_confidence", ""), execution_mode=link.get("execution_mode", ""),
        account_label=label, is_demo=is_demo,
        symbol=payload.market, direction=payload.direction,
        reference_entry=link.get("reference_entry", ""), actual_fill=str(_get(agg, "open_price", "") or ""),
        stop_loss=link.get("stop_loss", ""), take_profit=link.get("take_profit", ""),
        exit=str(_get(agg, "close_price", "") or ""),
        pips=str(payload.pips), gross_pnl=str(gross), net_pnl=str(net), outcome=payload.outcome,
        execution_duration=payload.duration, trade_timestamp=payload.open_time,
        execution_timestamp=payload.close_time, summary=payload.summary,
        statistics=statistics, card_rows=card_rows, result_card=result_card, caption=caption,
        currency=currency,
        strategy_display_name=strategy_display_name, legs=legs, take_profits=take_profits,
        progress=progress,
    )
