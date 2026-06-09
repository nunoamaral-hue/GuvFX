"""
GuvFX Economic Event Framework (B16.5)

Adds FACTUAL economic-event context to research observations so GuvFX can
ask "what conditions existed when this strategy performed?" — including
proximity to scheduled macro events.

This is NOT a news-trading system. NOT sentiment analysis. NOT NLP. NOT ML.
NOT outcome prediction. It records only factual event metadata
(name, type, impact, currency, time) and the symbol-relative relevance.

Provider abstraction (no hard-coded vendor, no paid service required):
  - EventProvider     : interface
  - StaticEventProvider : in-config calendar (default; empty unless configured)
  - MockEventProvider   : deterministic synthetic events for tests/validation
  - get_event_provider(): factory selected via settings (default: static)

Research Mode only.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Controlled vocabularies ──
IMPACT_LEVELS = ("LOW", "MEDIUM", "HIGH")
EVENT_TYPES = (
    "Interest Rate Decision",
    "CPI",
    "Inflation",
    "NFP",
    "Employment",
    "GDP",
    "PMI",
    "Central Bank Speech",
    "Retail Sales",
    "Other",
)
RELEVANCE_LEVELS = ("NONE", "LOW", "MEDIUM", "HIGH")

_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
_UNRANK = {0: "LOW", 1: "MEDIUM", 2: "HIGH"}


@dataclass
class EconomicEvent:
    """Factual scheduled-event metadata. No outcome, no forecast, no sentiment."""
    event_name: str
    event_type: str          # one of EVENT_TYPES
    impact_level: str        # LOW / MEDIUM / HIGH
    currency: str            # e.g. USD, EUR, JPY
    event_time: int          # epoch seconds, UTC

    def normalised(self) -> "EconomicEvent":
        et = self.event_type if self.event_type in EVENT_TYPES else "Other"
        il = self.impact_level.upper() if self.impact_level else "LOW"
        if il not in IMPACT_LEVELS:
            il = "LOW"
        return EconomicEvent(self.event_name, et, il, (self.currency or "").upper(), int(self.event_time))


# ─────────────────────────────────────────────────────────────────────
# Symbol → currency relevance map (which currencies' events matter)
# ─────────────────────────────────────────────────────────────────────

# Per symbol: currency -> base relevance weight (HIGH / MEDIUM)
SYMBOL_CURRENCY_RELEVANCE: dict[str, dict[str, str]] = {
    "EURUSD": {"EUR": "HIGH", "USD": "HIGH"},
    "GBPUSD": {"GBP": "HIGH", "USD": "HIGH"},
    "USDJPY": {"USD": "HIGH", "JPY": "HIGH"},
    "USDCHF": {"USD": "HIGH", "CHF": "HIGH"},
    "USDCAD": {"USD": "HIGH", "CAD": "HIGH"},
    "AUDUSD": {"AUD": "HIGH", "USD": "HIGH"},
    "NZDUSD": {"NZD": "HIGH", "USD": "HIGH"},
    "EURGBP": {"EUR": "HIGH", "GBP": "HIGH"},
    "EURJPY": {"EUR": "HIGH", "JPY": "HIGH"},
    "GBPJPY": {"GBP": "HIGH", "JPY": "HIGH"},
    # Metals — USD primary, macro-sensitive
    "XAUUSD": {"USD": "HIGH"},
    "XAGUSD": {"USD": "HIGH"},
    # Crypto — USD macro, medium relevance
    "BTCUSD": {"USD": "MEDIUM"},
    "ETHUSD": {"USD": "MEDIUM"},
    # Indices — local index currency + USD macro
    ".US30Cash": {"USD": "HIGH"},
    ".US500Cash": {"USD": "HIGH"},
    ".USTECHCash": {"USD": "HIGH"},
    ".DE30Cash": {"EUR": "HIGH", "USD": "MEDIUM"},
    ".UK100Cash": {"GBP": "HIGH", "USD": "MEDIUM"},
    ".WTICrude": {"USD": "MEDIUM"},
    ".BrentCrude": {"USD": "MEDIUM"},
}

# Asset classes that react to major USD macro even without a direct currency leg
_MACRO_SENSITIVE_PREFIXES = (".",)  # indices/CFDs
_MACRO_SENSITIVE_SYMBOLS = {"XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD", ".WTICrude", ".BrentCrude"}


def event_relevance(symbol: str, event: EconomicEvent) -> str:
    """
    Return HIGH / MEDIUM / LOW / NONE for an event relative to a symbol.

    Rule: relevance = min(currency-relevance-weight, event-impact). A
    matching high-relevance currency with a HIGH-impact event → HIGH;
    a LOW-impact event on the same currency → LOW. USD HIGH-impact macro
    events carry MEDIUM relevance to macro-sensitive instruments that have
    no direct currency leg.
    """
    ev = event.normalised()
    cur_map = SYMBOL_CURRENCY_RELEVANCE.get(symbol, {})
    cur_weight = cur_map.get(ev.currency)

    if cur_weight is None:
        # No direct currency leg. Major macro carve-out.
        macro = symbol in _MACRO_SENSITIVE_SYMBOLS or symbol.startswith(_MACRO_SENSITIVE_PREFIXES)
        if ev.currency == "USD" and ev.impact_level == "HIGH" and macro:
            return "MEDIUM"
        return "NONE"

    combined = min(_RANK.get(cur_weight, 1), _RANK.get(ev.impact_level, 0))
    return _UNRANK[combined]


# ─────────────────────────────────────────────────────────────────────
# Providers
# ─────────────────────────────────────────────────────────────────────

class EventProvider(ABC):
    """Interface for an economic-event source. Implementations must be
    side-effect free and never place orders or touch execution."""

    @abstractmethod
    def get_events(self, start_epoch: int, end_epoch: int) -> list[EconomicEvent]:
        """Return events with start_epoch <= event_time <= end_epoch."""
        raise NotImplementedError


class StaticEventProvider(EventProvider):
    """
    Calendar from a static in-config list. Default provider.

    Production-safe: returns [] unless explicitly configured via
    settings.GUVFX_ECONOMIC_EVENTS (list of dicts) or constructor events.
    """

    def __init__(self, events: list[EconomicEvent] | None = None):
        if events is None:
            events = self._load_from_settings()
        self._events = [e.normalised() for e in events]

    @staticmethod
    def _load_from_settings() -> list[EconomicEvent]:
        try:
            from django.conf import settings
            raw = getattr(settings, "GUVFX_ECONOMIC_EVENTS", []) or []
        except Exception:
            raw = []
        out = []
        for d in raw:
            try:
                out.append(EconomicEvent(
                    event_name=d.get("event_name", d.get("name", "Event")),
                    event_type=d.get("event_type", "Other"),
                    impact_level=d.get("impact_level", "LOW"),
                    currency=d.get("currency", ""),
                    event_time=int(d.get("event_time", 0)),
                ))
            except Exception:
                continue
        return out

    def get_events(self, start_epoch: int, end_epoch: int) -> list[EconomicEvent]:
        return [e for e in self._events if start_epoch <= e.event_time <= end_epoch]


class MockEventProvider(EventProvider):
    """
    Deterministic synthetic events for tests/validation — generated relative
    to a reference epoch. Not for production calendars.
    """

    def __init__(self, events: list[EconomicEvent] | None = None):
        self._events = [e.normalised() for e in (events or [])]

    @classmethod
    def scenario(cls, reference_epoch: int, kind: str) -> "MockEventProvider":
        """Build a provider for a named validation scenario relative to ref time."""
        ref = int(reference_epoch)
        if kind == "none":
            return cls([])
        if kind == "usd_high":
            return cls([EconomicEvent("US Non-Farm Payrolls", "NFP", "HIGH", "USD", ref + 45 * 60)])
        if kind == "eur_high":
            return cls([EconomicEvent("ECB Interest Rate Decision", "Interest Rate Decision", "HIGH", "EUR", ref + 30 * 60)])
        if kind == "irrelevant":
            return cls([EconomicEvent("Australia Employment Change", "Employment", "HIGH", "AUD", ref + 20 * 60)])
        return cls([])

    def get_events(self, start_epoch: int, end_epoch: int) -> list[EconomicEvent]:
        return [e for e in self._events if start_epoch <= e.event_time <= end_epoch]


def get_event_provider() -> EventProvider:
    """Factory. Defaults to StaticEventProvider (empty unless configured)."""
    try:
        from django.conf import settings
        kind = getattr(settings, "GUVFX_EVENT_PROVIDER", "static")
    except Exception:
        kind = "static"
    if kind == "mock":
        return MockEventProvider()
    return StaticEventProvider()


# ─────────────────────────────────────────────────────────────────────
# News context builder (consumed by the B16 feature extractor)
# ─────────────────────────────────────────────────────────────────────

NO_EVENT = {"impact": "NONE", "event_relevance": "NONE"}


def build_news_context(
    symbol: str,
    at_epoch: int,
    provider: EventProvider | None = None,
    window_hours: int = 24,
) -> dict:
    """
    Return the nearest RELEVANT scheduled event around ``at_epoch`` for a
    symbol, as factual context. If none, returns {"impact": "NONE"}.

    Never raises — failure degrades to NONE.
    """
    try:
        provider = provider or get_event_provider()
        w = int(window_hours * 3600)
        events = provider.get_events(int(at_epoch) - w, int(at_epoch) + w)
        relevant = []
        for e in events:
            rel = event_relevance(symbol, e)
            if rel != "NONE":
                relevant.append((e, rel))
        if not relevant:
            return dict(NO_EVENT)

        # nearest by absolute time distance
        e, rel = min(relevant, key=lambda x: abs(x[0].event_time - int(at_epoch)))
        delta_min = round((e.event_time - int(at_epoch)) / 60)
        upcoming = delta_min >= 0
        return {
            "impact": e.impact_level,
            "event_type": e.event_type,
            "event_name": e.event_name,
            "currency": e.currency,
            "event_time": datetime.fromtimestamp(e.event_time, tz=timezone.utc).isoformat(),
            "minutes_to_event": delta_min if upcoming else 0,
            "minutes_since_event": (-delta_min) if not upcoming else 0,
            "event_relevance": rel,
            "is_upcoming": upcoming,
        }
    except Exception as exc:  # pragma: no cover
        logger.warning("build_news_context failed: %s", exc)
        return dict(NO_EVENT)
