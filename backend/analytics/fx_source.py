"""analytics.fx_source — DARK, DB-only GBP->USD rate source + deterministic selection + provenance + health.

Wires an authoritative daily FX rate into the portfolio USD aggregate WITHOUT any synchronous external dependency:
the request path reads ONLY the ``DailyFxObservation`` store and NEVER calls FRED. Default OFF via
``PORTFOLIO_FX_CONVERSION_ENABLED``; when OFF the dashboard is byte-identical to today (``NullFxRateSource``).

Selection policy (PD decisions, ADR-0058):
  1. Target the previous calendar day relative to ``as_of``.
  2. If that date has no accepted observation (weekend/holiday/gap), carry FORWARD the most recent EARLIER
     accepted observation.
  3. Refuse (return ``None``) when the selected observation is more than 7 calendar days before ``as_of``; the
     caller then keeps today's honest PARTIAL/UNAVAILABLE behaviour.
  4. Never interpolate, fabricate or substitute zero; never auto-substitute a different source (Bank of England
     is monitoring-only — switching sources is a separate governed decision).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional, Tuple

from django.conf import settings
from django.utils import timezone

from analytics import fx_fred as FRED
from analytics import portfolio_fx as FX
from analytics.models import DailyFxObservation

MAX_STALENESS_DAYS = 7
_TRUTHY = ("1", "true", "yes", "on")


def fx_conversion_enabled() -> bool:
    """Master DARK gate for portfolio GBP->USD conversion. Settings override wins, else env; DEFAULT OFF; read
    live (mirrors ``trade_copier.flags`` idiom B) so a Sponsor can toggle without a restart."""
    val = getattr(settings, "PORTFOLIO_FX_CONVERSION_ENABLED", None)
    if val is None:
        val = os.getenv("PORTFOLIO_FX_CONVERSION_ENABLED", "")
    return str(val).strip().lower() in _TRUTHY


@dataclass(frozen=True)
class SelectedRate:
    observation_id: int
    base: str
    quote: str
    rate: Decimal              # exact, unrounded
    effective_date: date
    source: str
    source_series: str
    revision_number: int
    retrieved_at: Optional[datetime]
    age_days: int              # (as_of - effective_date) in calendar days


def select_observation(base: str, quote: str, as_of: date, *,
                       source: str = DailyFxObservation.SOURCE_FED_H10,
                       max_staleness_days: int = MAX_STALENESS_DAYS) -> Optional[SelectedRate]:
    """Deterministic selection per ADR-0058. The EFFECTIVE observation = the latest ACCEPTED revision of the most
    recent accepted date on or before (``as_of`` − 1 day); refused when older than ``max_staleness_days``. Returns
    ``None`` when there is no usable rate (caller keeps honest PARTIAL/UNAVAILABLE). Prior revisions are retained;
    REJECTED revisions are never selected."""
    base = FX.normalise_currency(base)
    quote = FX.normalise_currency(quote)
    target = as_of - timedelta(days=1)                       # (1) previous calendar day
    row = (DailyFxObservation.objects
           .filter(base_currency=base, quote_currency=quote, source=source,
                   acceptance_state=DailyFxObservation.STATE_ACCEPTED,
                   effective_date__lte=target)
           .order_by("-effective_date", "-revision_number")  # (2) most recent earlier date; latest accepted rev
           .first())
    if row is None:
        return None
    age = (as_of - row.effective_date).days
    if age > max_staleness_days:                             # (3) refuse a stale observation
        return None
    return SelectedRate(observation_id=row.id, base=base, quote=quote, rate=row.rate,
                        effective_date=row.effective_date, source=row.source, source_series=row.source_series,
                        revision_number=row.revision_number, retrieved_at=row.retrieved_at, age_days=age)


class StoredFredFxRateSource:
    """DB-only ``FxRateSource`` backed by the Fed H.10 ``DEXUSUK`` ``DailyFxObservation`` store. USD<->USD is
    identity; GBP->USD uses the selected observation (previous-day + carry-forward + 7-day staleness). Any other
    pair, or no usable rate, returns ``None`` => honest PARTIAL/UNAVAILABLE. It NEVER contacts FRED — the request
    path is strictly database-only."""

    def __init__(self, as_of: Optional[date] = None):
        self._as_of = as_of or timezone.now().date()
        self._cache: Dict[Tuple[str, str], Optional[SelectedRate]] = {}

    def _select(self, base: str, quote: str) -> Optional[SelectedRate]:
        key = (FX.normalise_currency(base), FX.normalise_currency(quote))
        if key not in self._cache:
            b, q = key
            self._cache[key] = (select_observation(b, q, self._as_of)
                                if (b, q) == (FRED.BASE, FRED.QUOTE) else None)
        return self._cache[key]

    def get_rate(self, base: str, quote: str) -> Optional[FX.FxRate]:
        b, q = FX.normalise_currency(base), FX.normalise_currency(quote)
        if b == q:
            return FX.FxRate(b, q, 1.0, source="identity")
        sel = self._select(b, q)
        if sel is None:
            return None
        return FX.FxRate(b, q, float(sel.rate), as_of=sel.effective_date.isoformat(),
                         source=f"{sel.source}:{sel.source_series} rev{sel.revision_number}")

    def provenance(self, base: str = FRED.BASE, quote: str = FRED.QUOTE) -> dict:
        """Read-only provenance for the portfolio API disclosure (DB-only). Honest ``available: False`` when no
        usable rate. The exact unrounded rate is surfaced as a decimal STRING (never a lossy float)."""
        sel = self._select(base, quote)
        if sel is None:
            return {"enabled": True, "pair": f"{base}{quote}", "available": False,
                    "source": DailyFxObservation.SOURCE_FED_H10, "source_series": FRED.SERIES,
                    "direction": FRED.DIRECTION, "as_of": self._as_of.isoformat(),
                    "reason": "no_rate_within_staleness"}
        return {"enabled": True, "pair": f"{base}{quote}", "available": True,
                "rate": format(sel.rate.normalize(), "f"), "effective_date": sel.effective_date.isoformat(),
                "source": sel.source, "source_series": sel.source_series, "direction": FRED.DIRECTION,
                "revision_number": sel.revision_number, "observation_id": sel.observation_id,
                "retrieved_at": sel.retrieved_at.isoformat() if sel.retrieved_at else "",
                "as_of": self._as_of.isoformat(), "age_days": sel.age_days}


def fx_ingestion_health(as_of: Optional[date] = None, *,
                        source: str = DailyFxObservation.SOURCE_FED_H10) -> dict:
    """Readiness of the scheduled FX ingestion (ops/health). HEALTHY when a usable (within-staleness) rate exists,
    STALE when the latest accepted observation is too old to convert, EMPTY when nothing is ingested yet."""
    as_of = as_of or timezone.now().date()
    base, quote = FRED.BASE, FRED.QUOTE
    accepted = DailyFxObservation.objects.filter(
        base_currency=base, quote_currency=quote, source=source,
        acceptance_state=DailyFxObservation.STATE_ACCEPTED)
    latest = accepted.order_by("-effective_date", "-revision_number").first()
    if latest is None:
        return {"status": "EMPTY", "source": source, "series": FRED.SERIES, "accepted_count": 0,
                "latest_effective_date": None, "latest_retrieved_at": None, "age_days": None, "convertible": False}
    convertible = select_observation(base, quote, as_of, source=source) is not None
    return {"status": "HEALTHY" if convertible else "STALE", "source": source, "series": FRED.SERIES,
            "accepted_count": accepted.count(),
            "latest_effective_date": latest.effective_date.isoformat(),
            "latest_retrieved_at": latest.retrieved_at.isoformat() if latest.retrieved_at else None,
            "age_days": (as_of - latest.effective_date).days, "convertible": convertible}
