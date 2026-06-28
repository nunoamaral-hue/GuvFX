"""
Trade result card renderer (Wayond / WIMS content).

Renders a winning trade as a clean, mobile/social-style **trade result card**
(a.k.a. trade receipt / trade history card) — readable on Telegram and Instagram,
with the profit prominent. It is generated from GuvFX trade data; it is NOT a
broker screenshot and carries no broker branding.

One layout model drives both outputs so they never drift:
  * ``to_png_bytes`` — the Telegram/social-ready raster (the attached deliverable)
  * ``to_svg``       — vector, internal/preview only

Supports one row or multiple partial-close rows; a Total Profit summary is shown
when there is more than one row.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal

from PIL import Image, ImageDraw, ImageFont

# ---- palette (deliberately app-neutral; not a broker theme) ----------------
WHITE = "#ffffff"
INK = "#111827"
MUTED = "#6b7280"
DIVIDER = "#edeff2"
BUY = "#2563eb"
SELL = "#e23b3b"
PROFIT = "#1f74e0"   # profit stands out in blue, matching the reference
ACCENT = "#16a34a"   # green left bar = winning trade marker

W = 1080
MARGIN = 56
ROW_H = 150
HEADER_H = 0
FOOTER_H = 200

_SCRATCH = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def _font(size: int):
    return ImageFont.load_default(size=size)  # scalable DejaVu (Pillow >= 10.1)


def _measure(text: str, size: int) -> float:
    return _SCRATCH.textlength(str(text), font=_font(size))


@dataclass
class TradeRow:
    symbol: str
    direction: str   # BUY / SELL
    volume: str
    entry: str
    close: str
    close_time: str
    profit: str      # net, formatted


def _net(trade) -> Decimal:
    def g(k):
        return trade.get(k, 0) if isinstance(trade, dict) else getattr(trade, k, 0)
    return (
        Decimal(str(g("profit") or 0))
        + Decimal(str(g("commission") or 0))
        + Decimal(str(g("swap") or 0))
    )


def _num(value, places) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{Decimal(str(value)):.{places}f}"
    except Exception:
        return str(value)


def _price(value) -> str:
    """Show a price at its natural precision (5dp FX, 2dp metals/indices)."""
    if value in (None, ""):
        return ""
    return str(value)


def _fmt_time(value) -> str:
    """Normalise an ISO timestamp to 'YYYY.MM.DD HH:MM:SS' (broker-style)."""
    s = str(value or "")
    if not s:
        return ""
    s = s.replace("Z", "").split("+")[0].split(".")[0]
    if "T" in s:
        d, _, t = s.partition("T")
    elif " " in s:
        d, _, t = s.partition(" ")
    else:
        return s
    return f"{d.replace('-', '.')} {t}".strip()


def row_from_trade(trade) -> TradeRow:
    """Build a TradeRow from a trading.models.Trade-like object or mapping."""
    def g(k, d=""):
        return trade.get(k, d) if isinstance(trade, dict) else getattr(trade, k, d)
    return TradeRow(
        symbol=str(g("symbol", "")),
        direction=str(g("side", "")).upper(),
        volume=_num(g("volume", ""), 2),
        entry=_price(g("open_price", "")),
        close=_price(g("close_price", "")),
        close_time=_fmt_time(g("close_time", "")),
        profit=_num(_net(trade), 2),
    )


def _money(value) -> str:
    d = Decimal(str(value))
    return f"{d:,.2f}"


# ---------------------------------------------------------------------------
# Layout model: list of primitive draw ops shared by SVG + PNG renderers
# ---------------------------------------------------------------------------
def build_card_model(rows, *, title="Trade result", total_profit=None):
    """Return ``(width, height, ops)`` for the given rows.

    ``ops`` are dicts: rect / line / text (text y is the baseline).
    """
    multi = len(rows) > 1
    height = 96 + ROW_H * len(rows) + (FOOTER_H if (multi or rows) else 0)
    ops = []
    ops.append({"op": "rect", "x": 0, "y": 0, "w": W, "h": height, "fill": WHITE})

    # title strip
    ops.append({"op": "text", "x": MARGIN, "y": 64, "t": title, "size": 34,
                "fill": MUTED, "anchor": "start", "bold": False})

    y0 = 96
    for i, r in enumerate(rows):
        top = y0 + i * ROW_H
        # green winning-trade accent bar
        ops.append({"op": "rect", "x": 0, "y": top + 18, "w": 12, "h": ROW_H - 36,
                    "fill": ACCENT})
        # line 1: SYMBOL  direction volume  .......  PROFIT
        bx = MARGIN
        base1 = top + 64
        ops.append({"op": "text", "x": bx, "y": base1, "t": r.symbol, "size": 48,
                    "fill": INK, "anchor": "start", "bold": True})
        bx += _measure(r.symbol + "  ", 48)
        dcol = BUY if r.direction == "BUY" else SELL
        dtext = f"{r.direction.lower()} {r.volume}".strip()
        ops.append({"op": "text", "x": bx, "y": base1, "t": dtext, "size": 40,
                    "fill": dcol, "anchor": "start", "bold": True})
        ops.append({"op": "text", "x": W - MARGIN, "y": base1, "t": r.profit,
                    "size": 54, "fill": PROFIT, "anchor": "end", "bold": True})
        # line 2: entry -[arrow]- close .......... close time
        # The arrow is drawn as a vector (the bundled font lacks U+2192), so it
        # renders identically in PNG and SVG with no missing-glyph boxes.
        base2 = top + 118
        ops.append({"op": "text", "x": MARGIN, "y": base2, "t": r.entry,
                    "size": 36, "fill": MUTED, "anchor": "start", "bold": False})
        ax = MARGIN + _measure(r.entry, 36) + 18
        ops.append({"op": "arrow", "x": ax, "y": base2 - 12, "len": 38, "stroke": MUTED})
        ops.append({"op": "text", "x": ax + 38 + 18, "y": base2, "t": r.close,
                    "size": 36, "fill": MUTED, "anchor": "start", "bold": False})
        ops.append({"op": "text", "x": W - MARGIN, "y": base2, "t": r.close_time,
                    "size": 34, "fill": MUTED, "anchor": "end", "bold": False})
        # row divider
        ops.append({"op": "line", "x1": MARGIN, "y1": top + ROW_H - 2,
                    "x2": W - MARGIN, "y2": top + ROW_H - 2, "stroke": DIVIDER})

    # footer total
    if total_profit is None:
        total = sum(Decimal(str(r.profit or 0)) for r in rows)
    else:
        total = Decimal(str(total_profit))
    fy = y0 + ROW_H * len(rows)
    ops.append({"op": "line", "x1": MARGIN, "y1": fy + 24, "x2": W - MARGIN,
                "y2": fy + 24, "stroke": DIVIDER})
    ops.append({"op": "text", "x": W // 2, "y": fy + 86, "t": "Total Profit",
                "size": 36, "fill": MUTED, "anchor": "middle", "bold": False})
    ops.append({"op": "text", "x": W // 2, "y": fy + 162, "t": _money(total),
                "size": 76, "fill": INK, "anchor": "middle", "bold": True})
    return W, height, ops


_SVG_ANCHOR = {"start": "start", "middle": "middle", "end": "end"}
_PNG_ANCHOR = {"start": "ls", "middle": "ms", "end": "rs"}


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def to_svg(model) -> str:
    width, height, ops = model
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="Helvetica,Arial,sans-serif">']
    for o in ops:
        if o["op"] == "rect":
            out.append(f'<rect x="{o["x"]}" y="{o["y"]}" width="{o["w"]}" '
                       f'height="{o["h"]}" fill="{o["fill"]}"/>')
        elif o["op"] == "line":
            out.append(f'<line x1="{o["x1"]}" y1="{o["y1"]}" x2="{o["x2"]}" '
                       f'y2="{o["y2"]}" stroke="{o["stroke"]}" stroke-width="2"/>')
        elif o["op"] == "arrow":
            x, y, ln, c = o["x"], o["y"], o["len"], o["stroke"]
            out.append(f'<line x1="{x}" y1="{y}" x2="{x+ln}" y2="{y}" '
                       f'stroke="{c}" stroke-width="4"/>')
            out.append(f'<polygon points="{x+ln},{y} {x+ln-13},{y-8} {x+ln-13},{y+8}" '
                       f'fill="{c}"/>')
        elif o["op"] == "text":
            weight = "700" if o["bold"] else "400"
            out.append(
                f'<text x="{o["x"]:.0f}" y="{o["y"]}" fill="{o["fill"]}" '
                f'font-size="{o["size"]}" font-weight="{weight}" '
                f'text-anchor="{_SVG_ANCHOR[o["anchor"]]}">{_esc(o["t"])}</text>'
            )
    out.append("</svg>")
    return "".join(out)


def to_png_bytes(model) -> bytes:
    import io
    width, height, ops = model
    img = Image.new("RGB", (width, height), WHITE)
    d = ImageDraw.Draw(img)
    for o in ops:
        if o["op"] == "rect":
            d.rectangle([o["x"], o["y"], o["x"] + o["w"], o["y"] + o["h"]], fill=o["fill"])
        elif o["op"] == "line":
            d.line([o["x1"], o["y1"], o["x2"], o["y2"]], fill=o["stroke"], width=2)
        elif o["op"] == "arrow":
            x, y, ln, c = o["x"], o["y"], o["len"], o["stroke"]
            d.line([x, y, x + ln, y], fill=c, width=4)
            d.polygon([(x + ln, y), (x + ln - 13, y - 8), (x + ln - 13, y + 8)], fill=c)
        elif o["op"] == "text":
            sw = 1 if o["bold"] else 0
            d.text((o["x"], o["y"]), str(o["t"]), font=_font(o["size"]),
                   fill=o["fill"], anchor=_PNG_ANCHOR[o["anchor"]],
                   stroke_width=sw, stroke_fill=o["fill"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_card(rows, *, title="Trade result", total_profit=None) -> dict:
    """Render rows to both PNG (deliverable) and SVG (internal).

    Returns a media dict ready to attach to a WIMS ConsumptionContract:
    ``{format, kind, png_base64, data_uri, svg}``.
    """
    model = build_card_model(rows, title=title, total_profit=total_profit)
    png = to_png_bytes(model)
    b64 = base64.b64encode(png).decode("ascii")
    return {
        "format": "png",
        "kind": "trade_result_card",
        "png_base64": b64,
        "data_uri": f"data:image/png;base64,{b64}",
        "svg": to_svg(model),
    }
