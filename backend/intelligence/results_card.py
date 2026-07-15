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

from intelligence.display_labels import source_display_label

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


def _price(value, symbol="") -> str:
    """Format a price at its instrument-natural precision (PRESENTATION ONLY — the stored broker
    value is never mutated). Metals (XAU/XAG/XPT/XPD) 2dp; JPY pairs 3dp; BTC/crypto 2dp; standard
    FX 5dp. A non-numeric value is returned unchanged; empty → ''."""
    if value in (None, ""):
        return ""
    try:
        d = Decimal(str(value))
    except Exception:
        return str(value)
    s = (symbol or "").upper()
    if any(m in s for m in ("XAU", "XAG", "XPT", "XPD")):
        dp = 2
    elif "JPY" in s:
        dp = 3
    elif any(c in s for c in ("BTC", "ETH", "USDT")):
        dp = 2
    else:
        dp = 5
    return f"{d:.{dp}f}"


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
        entry=_price(g("open_price", ""), g("symbol", "")),
        close=_price(g("close_price", ""), g("symbol", "")),
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
            rx = f' rx="{o["rx"]}"' if o.get("rx") else ""
            out.append(f'<rect x="{o["x"]}" y="{o["y"]}" width="{o["w"]}" '
                       f'height="{o["h"]}" fill="{o["fill"]}"{rx}/>')
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
            box = [o["x"], o["y"], o["x"] + o["w"], o["y"] + o["h"]]
            if o.get("rx"):
                d.rounded_rectangle(box, radius=o["rx"], fill=o["fill"])
            else:
                d.rectangle(box, fill=o["fill"])
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


# ---------------------------------------------------------------------------
# GFX-PKT-STAKEHOLDER-OUTPUT-VISUAL-UPGRADE — the polished result card.
# Consumes the CanonicalTradeResult (with per-TP legs). Emoji-free (Pillow's raster
# font has no emoji) — status is carried by COLOUR + shape: green win/profit/TP,
# red SL, blue side/symbol, neutral everything else. No chart. Shows each TP as its
# own trade result; pending TPs are shown greyed (never implied as guaranteed wins).
# NEVER renders execution mode or correlation id (internal-only, per the packet).
# ---------------------------------------------------------------------------
BAND = "#0f172a"       # dark header band
GREEN = ACCENT         # win / profit / TP
RED = "#dc2626"        # stop loss
BLUE = BUY             # side / symbol
GREEN_BG = "#f0fdf4"   # tinted profit block
FAINT = "#7c8698"      # secondary text — darkened for stakeholder-card contrast (A3)


def _dec(v, default="0"):
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(default)


def _signed_money(v) -> str:
    d = _dec(v)
    return f"{'+' if d > 0 else ('-' if d < 0 else '')}${abs(d):,.2f}"


def _signed_pips(v) -> str:
    if v in (None, ""):
        return ""
    d = _dec(v)
    return f"{'+' if d >= 0 else ''}{d} pips"


def build_result_card_model(r):
    """Build the (width, height, ops) model for the redesigned stakeholder card from a
    CanonicalTradeResult. Read-only; renders nothing itself."""
    legs = list(getattr(r, "legs", ()) or ())
    closed = [lg for lg in legs if lg.status == "CLOSED"]
    total_profit = sum((_dec(lg.profit) for lg in closed), Decimal("0")) if closed else _dec(r.net_pnl)
    total_pips = sum((_dec(lg.pips) for lg in closed if lg.pips not in (None, "")), Decimal("0")) if closed else _dec(r.pips)
    tps = list(getattr(r, "take_profits", ()) or ())
    final = bool((getattr(r, "progress", {}) or {}).get("final"))
    n_total = (r.progress or {}).get("total") or len(legs) or 1
    n_closed = (r.progress or {}).get("closed") or len(closed)

    # Instrument-aware price formatter bound to this card's symbol (presentation only).
    def p(v):
        return _price(v, r.symbol)

    # analysis rows (label, value, value_colour) — NO exec mode / correlation id
    analysis = [
        ("Reference entry", p(r.reference_entry) or "n/a", INK),
        ("Actual fill", p(r.actual_fill) or "n/a", INK),
        ("Stop loss", p(r.stop_loss) or "n/a", RED),
    ]
    for i, tp in enumerate(tps or ([r.take_profit] if r.take_profit else [])):
        analysis.append((f"TP{i + 1}", p(tp) or "n/a", GREEN))
    analysis += [
        ("Exit price", p(r.exit) or "n/a", INK),
        ("Gross PNL", _signed_money(r.gross_pnl), INK),
        ("Net profit", _signed_money(total_profit), GREEN),
        ("Closed", _fmt_time(r.execution_timestamp), MUTED),
    ]

    row_h, an_h = 128, 56
    y = 0
    ops = []
    # Slightly taller profit banner + section headers for more breathing room (A3 polish).
    H_HEADER, H_SUMMARY, H_METRIC, H_SECHDR, H_FOOT = 150, 132, 236, 72, 156
    height = (H_HEADER + H_SUMMARY + H_METRIC + H_SECHDR + row_h * max(len(legs), 1)
              + H_SECHDR + an_h * len(analysis) + H_FOOT)
    ops.append({"op": "rect", "x": 0, "y": 0, "w": W, "h": height, "fill": WHITE})

    # --- header band ---
    ops.append({"op": "rect", "x": 0, "y": 0, "w": W, "h": H_HEADER, "fill": BAND})
    ops.append({"op": "text", "x": MARGIN, "y": 68, "t": f"{source_display_label(r.provider)} Trade Result",
                "size": 46, "fill": WHITE, "anchor": "start", "bold": True})
    ops.append({"op": "text", "x": MARGIN, "y": 112, "t": r.strategy_display_name or r.strategy or "Automated strategy",
                "size": 27, "fill": "#94a3b8", "anchor": "start", "bold": False})
    # outcome badge (green WIN)
    badge = (r.outcome or "WIN").upper()
    bw = _measure(badge, 34) + 56
    ops.append({"op": "rect", "x": W - MARGIN - bw, "y": 46, "w": bw, "h": 58, "fill": GREEN, "rx": 29})
    ops.append({"op": "text", "x": W - MARGIN - bw / 2, "y": 86, "t": badge, "size": 34,
                "fill": WHITE, "anchor": "middle", "bold": True})
    y = H_HEADER

    # --- summary strip ---
    ops.append({"op": "text", "x": MARGIN, "y": y + 74, "t": r.symbol, "size": 60, "fill": BLUE,
                "anchor": "start", "bold": True})
    sx = MARGIN + _measure(r.symbol + "  ", 60)
    ops.append({"op": "text", "x": sx, "y": y + 74, "t": r.direction, "size": 40, "fill": BLUE,
                "anchor": "start", "bold": True})
    ops.append({"op": "text", "x": W - MARGIN, "y": y + 52, "t": "ACCOUNT", "size": 22, "fill": FAINT,
                "anchor": "end", "bold": False})
    ops.append({"op": "text", "x": W - MARGIN, "y": y + 88, "t": r.account_label or "n/a", "size": 32,
                "fill": INK, "anchor": "end", "bold": False})
    ops.append({"op": "line", "x1": MARGIN, "y1": y + H_SUMMARY - 1, "x2": W - MARGIN,
                "y2": y + H_SUMMARY - 1, "stroke": DIVIDER})
    y += H_SUMMARY

    # --- main metric (tinted) ---
    ops.append({"op": "rect", "x": 0, "y": y, "w": W, "h": H_METRIC, "fill": GREEN_BG})
    ops.append({"op": "text", "x": MARGIN, "y": y + 50, "t": ("TOTAL PROFIT" if final else "PROFIT SO FAR"),
                "size": 26, "fill": "#15803d", "anchor": "start", "bold": True})
    ops.append({"op": "text", "x": MARGIN, "y": y + 140, "t": _signed_money(total_profit), "size": 92,
                "fill": GREEN, "anchor": "start", "bold": True})
    pip_txt = _signed_pips(total_pips)
    if pip_txt:
        ops.append({"op": "text", "x": W - MARGIN, "y": y + 92, "t": pip_txt, "size": 44, "fill": GREEN,
                    "anchor": "end", "bold": True})
    ops.append({"op": "text", "x": W - MARGIN, "y": y + 140, "t": f"{n_closed} of {n_total} take-profits closed",
                "size": 28, "fill": MUTED, "anchor": "end", "bold": False})
    y += H_METRIC

    # --- TP results ---
    ops.append({"op": "text", "x": MARGIN, "y": y + 42, "t": "TAKE-PROFIT RESULTS", "size": 26,
                "fill": MUTED, "anchor": "start", "bold": True})
    y += H_SECHDR
    if not legs:  # fallback single-row
        legs = [type("L", (), dict(tp_label="TP1", direction=r.direction, volume="",
                entry=r.actual_fill, exit=r.exit, target=r.take_profit, profit=r.net_pnl,
                status="CLOSED", pips=r.pips))()]
    for lg in legs:
        is_closed = lg.status == "CLOSED"
        bar = GREEN if is_closed else "#cbd5e1"
        ops.append({"op": "rect", "x": 0, "y": y + 18, "w": 12, "h": row_h - 36, "fill": bar})
        b1 = y + 62
        ops.append({"op": "text", "x": MARGIN, "y": b1, "t": lg.tp_label, "size": 46,
                    "fill": (GREEN if is_closed else MUTED), "anchor": "start", "bold": True})
        dv = f"{lg.direction} {lg.volume}".strip()
        ops.append({"op": "text", "x": MARGIN + _measure(lg.tp_label + "  ", 46), "y": b1, "t": dv,
                    "size": 34, "fill": (BLUE if is_closed else FAINT), "anchor": "start", "bold": True})
        if is_closed:
            ops.append({"op": "text", "x": W - MARGIN, "y": b1, "t": _signed_money(lg.profit), "size": 46,
                        "fill": GREEN, "anchor": "end", "bold": True})
            pp = _signed_pips(lg.pips)
            if pp:
                ops.append({"op": "text", "x": W - MARGIN, "y": y + 104, "t": pp, "size": 30,
                            "fill": GREEN, "anchor": "end", "bold": False})
        else:
            ops.append({"op": "text", "x": W - MARGIN, "y": b1, "t": "PENDING", "size": 30,
                        "fill": FAINT, "anchor": "end", "bold": True})
        # line 2: entry -> exit / target (instrument-aware precision)
        b2 = y + 104
        left = p(lg.entry)
        ops.append({"op": "text", "x": MARGIN, "y": b2, "t": left, "size": 32, "fill": MUTED,
                    "anchor": "start", "bold": False})
        ax = MARGIN + _measure(left, 32) + 16
        ops.append({"op": "arrow", "x": ax, "y": b2 - 11, "len": 34, "stroke": (MUTED if is_closed else FAINT)})
        right = p(lg.exit) if is_closed else f"target {p(lg.target)}"
        ops.append({"op": "text", "x": ax + 34 + 16, "y": b2, "t": right, "size": 32,
                    "fill": (INK if is_closed else FAINT), "anchor": "start", "bold": False})
        ops.append({"op": "line", "x1": MARGIN, "y1": y + row_h - 1, "x2": W - MARGIN,
                    "y2": y + row_h - 1, "stroke": DIVIDER})
        y += row_h

    # --- trade analysis ---
    ops.append({"op": "text", "x": MARGIN, "y": y + 42, "t": "TRADE ANALYSIS", "size": 26,
                "fill": MUTED, "anchor": "start", "bold": True})
    y += H_SECHDR
    for label, value, col in analysis:
        ops.append({"op": "text", "x": MARGIN, "y": y + 36, "t": label, "size": 30, "fill": MUTED,
                    "anchor": "start", "bold": False})
        ops.append({"op": "text", "x": W - MARGIN, "y": y + 36, "t": str(value), "size": 30, "fill": col,
                    "anchor": "end", "bold": (col == GREEN)})
        ops.append({"op": "line", "x1": MARGIN, "y1": y + an_h - 1, "x2": W - MARGIN,
                    "y2": y + an_h - 1, "stroke": "#f4f6f8"})
        y += an_h

    # --- footer ---
    ops.append({"op": "text", "x": MARGIN, "y": y + 58, "t": "Trade completed automatically · No manual intervention",
                "size": 28, "fill": MUTED, "anchor": "start", "bold": False})
    ops.append({"op": "text", "x": MARGIN, "y": y + 100, "t": "Generated from GuvFX execution data — not a broker screenshot.",
                "size": 24, "fill": FAINT, "anchor": "start", "bold": False})
    return W, height, ops


def render_result_card(r) -> dict:
    """Render the redesigned stakeholder card (PNG deliverable + SVG preview) from a canonical."""
    model = build_result_card_model(r)
    png = to_png_bytes(model)
    b64 = base64.b64encode(png).decode("ascii")
    return {"format": "png", "kind": "stakeholder_result_card", "png_base64": b64,
            "data_uri": f"data:image/png;base64,{b64}", "svg": to_svg(model)}


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
