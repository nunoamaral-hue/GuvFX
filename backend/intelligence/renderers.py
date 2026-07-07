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
from typing import Optional

from .canonical import CanonicalTradeResult


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
    """The Stakeholder Telegram review-channel renderer (text; DRAFT wording).

    Reproduces the deployed dry-run envelope wording, now sourced from the canonical object.
    Shows BOTH the provider reference entry and the actual fill (entry-price policy §6A).
    """

    name = "telegram"

    def title_for(self, r: CanonicalTradeResult) -> str:
        return f"GuvFX — winning trade: {r.symbol} {r.direction}"

    def render(self, r: CanonicalTradeResult) -> RenderedContent:
        title = self.title_for(r)
        text = "\n".join([
            title,
            r.summary,
            f"Strategy: {r.strategy or 'n/a'}",
            f"{r.symbol} {r.direction}",
            f"Signal entry (ref): {r.reference_entry or 'n/a'}  |  Filled: {r.actual_fill}",
            f"SL: {r.stop_loss or 'n/a'}  |  TP: {r.take_profit or 'n/a'}",
            f"Profit: {r.net_pnl}  ({r.pips} pips)",
            f"Closed: {r.execution_timestamp or 'n/a'}",
            f"ref: {r.correlation_id or 'n/a'}",
        ])
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
