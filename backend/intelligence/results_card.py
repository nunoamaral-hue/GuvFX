"""
Results card renderer (Phase: Wayond content).

Renders the *results / history* section of a winning trade as a self-contained
SVG — the MT5 "Account History"-style row(s) filtered to a single order or day,
with the profit highlighted. This is what rides the WIMS packet to add
legitimacy to social content. It is NOT a chart and does NOT screen-capture the
terminal: it is generated from GuvFX trade data, so there is no broker-ToS or
VPS-automation concern.

Pure / dependency-free (string SVG). PNG conversion, if ever needed, is a
downstream step and intentionally out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

COLUMNS = [
    ("open_time", "Open Time", 168),
    ("symbol", "Symbol", 90),
    ("type", "Type", 64),
    ("volume", "Volume", 78),
    ("open_price", "Open Price", 104),
    ("sl", "S / L", 96),
    ("tp", "T / P", 96),
    ("close_time", "Close Time", 168),
    ("close_price", "Close Price", 104),
    ("profit", "Profit", 96),
]

_PALETTE = {
    "bg": "#0b0e11",
    "panel": "#11161c",
    "header": "#1b2530",
    "grid": "#243140",
    "text": "#e6edf3",
    "muted": "#8b98a5",
    "win": "#2ec27e",
    "brand": "#8b5cf6",
}


@dataclass
class ResultRow:
    open_time: str
    symbol: str
    type: str
    volume: str
    open_price: str
    sl: str
    tp: str
    close_time: str
    close_price: str
    profit: str


def _esc(s) -> str:
    return (
        str(s)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt(value, places=None) -> str:
    if value in (None, ""):
        return ""
    if places is not None:
        try:
            return f"{Decimal(str(value)):.{places}f}"
        except Exception:
            return str(value)
    return str(value)


def row_from_trade(trade) -> ResultRow:
    """Build a ResultRow from a trading.models.Trade-like object or mapping."""
    def g(k, default=""):
        if isinstance(trade, dict):
            return trade.get(k, default)
        return getattr(trade, k, default)

    net = (
        Decimal(str(g("profit", 0) or 0))
        + Decimal(str(g("commission", 0) or 0))
        + Decimal(str(g("swap", 0) or 0))
    )
    return ResultRow(
        open_time=str(g("open_time", "")),
        symbol=str(g("symbol", "")),
        type=str(g("side", "")).lower(),
        volume=_fmt(g("volume", ""), 2),
        open_price=_fmt(g("open_price", ""), 5),
        sl=_fmt(g("stop_loss", g("sl", "")), 5) if g("stop_loss", g("sl", "")) else "",
        tp=_fmt(g("take_profit", g("tp", "")), 5) if g("take_profit", g("tp", "")) else "",
        close_time=str(g("close_time", "")),
        close_price=_fmt(g("close_price", ""), 5),
        profit=_fmt(net, 2),
    )


def render_results_card(rows, *, title="Closed Trade Result",
                        account_label="GuvFX", total_profit=None) -> str:
    """Render the results rows as an SVG string (winners only by convention)."""
    width = sum(w for _, _, w in COLUMNS) + 32
    header_h, row_h, top = 64, 30, 104
    height = top + row_h * (len(rows) + 1) + 56

    if total_profit is None:
        total = sum(Decimal(str(r.profit or 0)) for r in rows)
    else:
        total = Decimal(str(total_profit))

    p = _PALETTE
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="ui-monospace,Menlo,Consolas,monospace">',
        f'<rect width="{width}" height="{height}" fill="{p["bg"]}"/>',
        f'<rect x="16" y="16" width="{width-32}" height="{height-32}" rx="10" fill="{p["panel"]}"/>',
        # brand bar + title
        f'<rect x="16" y="16" width="6" height="{height-32}" fill="{p["brand"]}"/>',
        f'<text x="36" y="46" fill="{p["text"]}" font-size="20" font-weight="700">{_esc(title)}</text>',
        f'<text x="36" y="70" fill="{p["muted"]}" font-size="13">{_esc(account_label)}'
        f' · results filtered to order/day</text>',
    ]

    # table header
    x = 32
    y = top - row_h
    parts.append(f'<rect x="24" y="{y-20}" width="{width-48}" height="{header_h-40}" fill="{p["header"]}"/>')
    for key, label, w in COLUMNS:
        anchor = "end" if key in ("profit", "volume", "open_price", "close_price", "sl", "tp") else "start"
        tx = x + (w - 10 if anchor == "end" else 4)
        parts.append(
            f'<text x="{tx}" y="{y-2}" fill="{p["muted"]}" font-size="12" '
            f'text-anchor="{anchor}">{_esc(label)}</text>'
        )
        x += w

    # rows
    for i, r in enumerate(rows):
        ry = top + i * row_h
        if i % 2 == 0:
            parts.append(f'<rect x="24" y="{ry-18}" width="{width-48}" height="{row_h}" fill="#0e141b"/>')
        x = 32
        for key, _label, w in COLUMNS:
            val = getattr(r, key)
            is_profit = key == "profit"
            anchor = "end" if key in ("profit", "volume", "open_price", "close_price", "sl", "tp") else "start"
            tx = x + (w - 10 if anchor == "end" else 4)
            color = p["win"] if is_profit else p["text"]
            weight = "700" if is_profit else "400"
            parts.append(
                f'<text x="{tx}" y="{ry+2}" fill="{color}" font-size="12" '
                f'font-weight="{weight}" text-anchor="{anchor}">{_esc(val)}</text>'
            )
            x += w

    # total footer
    fy = top + len(rows) * row_h + 16
    parts.append(f'<line x1="24" y1="{fy-8}" x2="{width-24}" y2="{fy-8}" stroke="{p["grid"]}"/>')
    parts.append(
        f'<text x="{width-26}" y="{fy+16}" fill="{p["win"]}" font-size="16" '
        f'font-weight="700" text-anchor="end">Profit: {_fmt(total, 2)}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)
