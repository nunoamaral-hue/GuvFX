"""
GFX-PKT-CANONICAL-TRADE-RESULT — renderers.

A renderer turns the ONE ``CanonicalTradeResult`` into a channel-specific rendering. Only the
renderer changes per channel; the canonical trade object does not. Renderers are pure and
READ-ONLY: they format, they never transmit, publish, order, or mutate. There is exactly one
result-card / caption implementation (``intelligence.results_card`` / ``intelligence.caption``),
invoked via the canonical object — so no formatting logic is duplicated across channels.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from .canonical import CanonicalTradeResult, LegResult


def _money(value, currency: str = "$") -> str:
    """Format a signed currency amount, e.g. '+$128.64' / '-$12.00' / '$0.00'."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    sign = "+" if d > 0 else ("-" if d < 0 else "")
    return f"{sign}{currency}{abs(d):,.2f}"


def _fmt_ts(value) -> str:
    """Trim an ISO timestamp to 'YYYY-MM-DD HH:MM:SS UTC' (drop microseconds / offset noise)."""
    s = str(value or "")
    if not s:
        return "n/a"
    s = s.replace("Z", "").split("+")[0].split(".")[0].replace("T", " ").strip()
    return f"{s} UTC" if s else "n/a"


@dataclass(frozen=True)
class RenderedContent:
    """A channel-agnostic render output. ``text`` is the message body; ``media`` is an optional
    media dict (e.g. the results card + caption for a rich channel)."""

    renderer: str
    title: str
    text: str
    media: Optional[dict] = None


class CanonicalRenderer(ABC):
    """Renders a ``CanonicalTradeResult`` into one channel's content. Never transmits/publishes."""

    name: str = "abstract"

    @abstractmethod
    def render(self, result: CanonicalTradeResult) -> RenderedContent:  # pragma: no cover
        ...


class TelegramRenderer(CanonicalRenderer):
    """The Stakeholder Telegram review-channel renderer — a factual, three-section system report.

    Section 1 (executive summary) is readable by a non-trader in three seconds; Section 2 (trade
    evidence) lists the individual take-profit legs — closed and still-open — so the reader can see
    real positions; Section 3 (execution analysis) is the auditable data. It supports PROGRESSIVE
    evidence: a card is emitted per profitable leg close, showing the legs closed so far and the
    ones still pending (per §6A it shows both the provider reference entry and the actual fill).

    Plain Telegram text can't colour arbitrary words, so status is carried by ✅/⏳ markers and
    wording; the colour system lives in the result-card image. No chart, no marketing language.
    Read-only: it formats, it never transmits/orders/publishes.
    """

    name = "telegram"

    def title_for(self, r: CanonicalTradeResult) -> str:
        return f"{self._provider(r)} TRADE RESULT — {self._head_status(r)}"

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _provider(r: CanonicalTradeResult) -> str:
        return (r.provider or r.strategy or "GuvFX").upper()

    @staticmethod
    def _head_status(r: CanonicalTradeResult) -> str:
        label = (r.progress or {}).get("label", "")
        if (r.progress or {}).get("final"):
            return f"FINAL {r.outcome}"
        return f"{label} {r.outcome}".strip() if label else r.outcome

    def _closed_legs(self, r: CanonicalTradeResult):
        return [lg for lg in r.legs if getattr(lg, "status", "") == "CLOSED"]

    def _total_closed(self, r: CanonicalTradeResult) -> Decimal:
        closed = self._closed_legs(r)
        if closed:
            tot = Decimal("0")
            for lg in closed:
                try:
                    tot += Decimal(str(lg.profit or 0))
                except (InvalidOperation, TypeError, ValueError):
                    pass
            return tot
        try:
            return Decimal(str(r.net_pnl or 0))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")

    @staticmethod
    def _pip_str(pips) -> str:
        """'+5.0 pips' / '-3.0 pips' / '' when unknown."""
        if pips in (None, ""):
            return ""
        try:
            d = Decimal(str(pips))
        except (InvalidOperation, TypeError, ValueError):
            return ""
        return f"{'+' if d >= 0 else ''}{d} pips"

    def _total_pips(self, r: CanonicalTradeResult) -> str:
        """Sum of CLOSED legs' pips (falls back to the single-trade pips when there are no legs)."""
        closed = self._closed_legs(r)
        if closed:
            tot = Decimal("0")
            any_p = False
            for lg in closed:
                if lg.pips not in (None, ""):
                    try:
                        tot += Decimal(str(lg.pips)); any_p = True
                    except (InvalidOperation, TypeError, ValueError):
                        pass
            return str(tot) if any_p else ""
        return str(r.pips) if r.pips not in (None, "") else ""

    def _leg_line(self, lg: LegResult, currency: str) -> str:
        # Each TP is its OWN trade result (not a guaranteed leg of one trade).
        dir_vol = f"{lg.direction} {lg.volume}".strip()
        if lg.status == "CLOSED":
            pip = self._pip_str(lg.pips)
            tail = f" · {pip}" if pip else ""
            return f"✅ {lg.tp_label} · {dir_vol} · {lg.entry} → {lg.exit} · {_money(lg.profit, currency)}{tail}"
        if lg.status == "OPEN":
            # Filled, still running — show the TP target it is working toward (not a realized exit).
            return f"⏳ {lg.tp_label} · {dir_vol} · {lg.entry} → target {lg.target} · open"
        return f"⏳ {lg.tp_label} · {dir_vol} · target {lg.target} · pending"

    def _evidence_lines(self, r: CanonicalTradeResult, currency: str):
        if r.legs:
            lines = [self._leg_line(lg, currency) for lg in r.legs]
        else:
            # Fallback (single-leg / non-plan trade): one synthesised closed row from the facts.
            pip = self._pip_str(r.pips)
            tail = f" · {pip}" if pip else ""
            lines = [f"✅ TP1 · {r.direction} · {r.actual_fill} → {r.exit or 'n/a'} · "
                     f"{_money(r.net_pnl, currency)}{tail}"]
        return lines

    def _take_profit_line(self, r: CanonicalTradeResult) -> str:
        tps = list(r.take_profits) if r.take_profits else ([r.take_profit] if r.take_profit else [])
        if not tps:
            return "n/a"
        return " · ".join(f"TP{i + 1} {tp}" for i, tp in enumerate(tps) if tp)

    def render(self, r: CanonicalTradeResult) -> RenderedContent:
        cur = r.currency or "$"
        title = self.title_for(r)
        progress = r.progress or {}
        closed, total = progress.get("closed"), progress.get("total")
        final = bool(progress.get("final"))
        total_closed = self._total_closed(r)

        status_line = f"Status: {r.outcome}"
        if total:
            status_line += f" · {'all ' if final else ''}{closed} of {total} legs closed"
        net_label = "Total net profit" if final else "Net profit so far"
        total_pips = self._total_pips(r)
        pip_suffix = f"  ·  {self._pip_str(total_pips)}" if total_pips else ""

        parts = [f"🏆 {title}", ""]
        # Section 1 — executive summary
        parts += [
            f"Strategy: {r.strategy_display_name or r.strategy or 'n/a'}",
            f"Account: {r.account_label or 'n/a'}",
            f"Instrument: {r.symbol}",
            f"Direction: {r.direction}",
            status_line,
            "",
            f"💰 {net_label}: {_money(total_closed, cur)}{pip_suffix}",
            "",
        ]
        # Section 2 — trade evidence
        ev_head = "📊 TRADE EVIDENCE"
        if total:
            ev_head += f" · {closed}/{total} legs closed"
        parts.append(ev_head)
        parts += self._evidence_lines(r, cur)
        if r.legs:
            parts.append(f"Total closed: {_money(total_closed, cur)}"
                         + (f" ({closed} of {total} legs)" if total else ""))
        parts.append("")
        # Section 3 — trade analysis (factual; stakeholder-facing — no execution mode / correlation id)
        parts += [
            "🔎 TRADE ANALYSIS",
            f"Reference entry: {r.reference_entry or 'n/a'}",
            f"Actual fill: {r.actual_fill or 'n/a'}",
            f"Stop loss: {r.stop_loss or 'n/a'}",
            f"Take profits: {self._take_profit_line(r)}",
            f"Exit (this leg): {r.exit or 'n/a'}",
            f"This leg — gross / net: {_money(r.gross_pnl, cur)} / {_money(r.net_pnl, cur)}",
            f"Net profit (closed so far): {_money(total_closed, cur)}",
            f"Closed: {_fmt_ts(r.execution_timestamp)}",
            "",
        ]
        # Factual system note
        if total:
            leg_note = (f"All {total} take-profit legs were executed"
                        if final else f"Take-profit leg {progress.get('label') or closed} of {total} executed")
        else:
            leg_note = "The trade was executed"
        parts.append(f"Trade completed automatically. {leg_note}. No manual intervention occurred.")

        text = "\n".join(parts)
        return RenderedContent(renderer=self.name, title=title, text=text, media=None)


class WIMSRenderer(CanonicalRenderer):
    """The WIMS renderer — produces the content-side ``media`` dict (results card + caption).

    Consumes the canonical object's pre-rendered card/caption (built once via the shared
    ``results_card``/``caption`` factories). Requires a canonical built ``with_media=True``.
    """

    name = "wims"

    def title_for(self, r: CanonicalTradeResult) -> str:
        return f"{r.symbol} {r.direction} winning trade"

    def render(self, r: CanonicalTradeResult) -> RenderedContent:
        if r.result_card is None or r.caption is None:
            raise ValueError(
                "WIMSRenderer requires a CanonicalTradeResult built with_media=True "
                "(result_card/caption are unrendered)."
            )
        media = {"results_card": r.result_card, "caption": r.caption}
        return RenderedContent(
            renderer=self.name, title=self.title_for(r), text=r.caption, media=media,
        )
