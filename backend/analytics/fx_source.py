"""analytics.fx_source — DARK, DB-only GBP->USD rate source + deterministic selection + provenance + health.

Wires an authoritative daily FX rate into the portfolio USD aggregate WITHOUT any synchronous external dependency:
the request path reads ONLY the ``DailyFxObservation`` store and NEVER calls FRED. Default OFF via
``PORTFOLIO_FX_CONVERSION_ENABLED``; when OFF the dashboard is byte-identical to today (``NullFxRateSource``).

Selection policy (PD decisions, ADR-0058, F2):
  1. Determine the current calendar date in the GOVERNED timezone (``Europe/London``).
  2. ``target_date = current_date − 1 calendar day``.
  3. Select the latest ACCEPTED Fed H.10 observation with ``effective_date <= target_date`` (so an observation
     dated *today* — or a future-dated one — is never selected).
  4. Carry FORWARD across weekends/holidays/gaps (the most recent earlier accepted observation).
  5. Refuse (return ``None``) when the selected observation is more than 7 calendar days older than the
     TARGET date; the caller then keeps today's honest PARTIAL/UNAVAILABLE behaviour.
  6. Never interpolate, fabricate or substitute zero; never auto-substitute a different source (Bank of England
     is monitoring-only — switching sources is a separate governed decision).

An FX read NEVER breaks the dashboard (F5): any failure degrades to honest "unavailable".
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from analytics import fx_fred as FRED
from analytics import portfolio_fx as FX
from analytics.models import DailyFxObservation

logger = logging.getLogger("guvfx.fx")

MAX_STALENESS_DAYS = 7
# Governed business-date timezone for "today"/previous-day. Europe/London unless a platform-wide business-date
# policy mandates otherwise (overridable via settings for a future governed policy). ZoneInfo handles UK DST.
GOVERNED_TZ = getattr(settings, "PORTFOLIO_FX_BUSINESS_TZ", "Europe/London") or "Europe/London"
_TRUTHY = ("1", "true", "yes", "on")


def fx_conversion_enabled() -> bool:
    """Master DARK gate for portfolio GBP->USD conversion. Settings override wins, else env; DEFAULT OFF; read
    live (mirrors ``trade_copier.flags`` idiom B) so a Sponsor can toggle without a restart."""
    val = getattr(settings, "PORTFOLIO_FX_CONVERSION_ENABLED", None)
    if val is None:
        val = os.getenv("PORTFOLIO_FX_CONVERSION_ENABLED", "")
    return str(val).strip().lower() in _TRUTHY


def fx_conversion_cohort_allowlist() -> frozenset:
    """Founder/operator COHORT allowlist for portfolio GBP->USD conversion (controlled rollout before GA).
    Lowercased emails parsed from ``PORTFOLIO_FX_COHORT_ALLOWLIST`` (comma-separated), settings-override-then-env.
    Plain config, never a secret. Empty/unset ⇒ no one is in the cohort (conversion stays OFF unless the global
    GA gate is on)."""
    raw = getattr(settings, "PORTFOLIO_FX_COHORT_ALLOWLIST", None)
    if raw is None:
        raw = os.getenv("PORTFOLIO_FX_COHORT_ALLOWLIST", "")
    return frozenset(e.strip().lower() for e in str(raw).split(",") if e.strip())


def fx_conversion_enabled_for(user) -> bool:
    """Per-user gate for portfolio GBP->USD conversion. A user gets conversion when EITHER the global GA gate
    ``fx_conversion_enabled()`` is on for everyone, OR their email is in ``fx_conversion_cohort_allowlist()`` (the
    controlled founder cohort). Both unset ⇒ denied (dashboard byte-identical to today, honest PARTIAL). Fail-closed
    on a missing/anonymous user. Lets conversion be enabled for support@ only WITHOUT flipping the global flag, so a
    cohort rollout never converts the whole member base (the directive's 'enable first for support@' requirement)."""
    if fx_conversion_enabled():
        return True
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return (getattr(user, "email", "") or "").strip().lower() in fx_conversion_cohort_allowlist()


def governed_today(now: Optional[datetime] = None) -> date:
    """The current calendar date in the governed timezone (``Europe/London``), DST-correct. ``now`` (an aware
    datetime) is injectable for tests; it defaults to the real clock."""
    now = now or timezone.now()
    return now.astimezone(ZoneInfo(GOVERNED_TZ)).date()


@dataclass(frozen=True)
class SelectedRate:
    observation_id: int
    base: str
    quote: str
    rate: Decimal              # exact, unrounded
    effective_date: date
    source: str
    source_series: str
    source_url: str
    revision_number: int
    retrieved_at: Optional[datetime]
    current_date: date
    target_date: date
    age_days: int              # (target_date - effective_date) in calendar days


def select_observation(base: str, quote: str, current_date: date, *,
                       source: str = DailyFxObservation.SOURCE_FED_H10,
                       max_staleness_days: int = MAX_STALENESS_DAYS) -> Optional[SelectedRate]:
    """Deterministic selection per ADR-0058/F2. ``current_date`` is the governed-timezone 'today'. The EFFECTIVE
    observation = the latest ACCEPTED revision of the most recent accepted date on or before ``current_date − 1``;
    refused when older than ``max_staleness_days`` relative to that TARGET date. Returns ``None`` when there is no
    usable rate (caller keeps honest PARTIAL/UNAVAILABLE). Prior revisions are retained; REJECTED revisions are
    never selected; an observation dated today or in the future is never selected."""
    base = FX.normalise_currency(base)
    quote = FX.normalise_currency(quote)
    target_date = current_date - timedelta(days=1)                   # (2) previous calendar day
    row = (DailyFxObservation.objects
           .filter(base_currency=base, quote_currency=quote, source=source,
                   acceptance_state=DailyFxObservation.STATE_ACCEPTED,
                   effective_date__lte=target_date)                   # (3)(4) never today/future; carry forward
           .order_by("-effective_date", "-revision_number")          # most recent earlier date; latest accepted rev
           .first())
    if row is None:
        return None
    age = (target_date - row.effective_date).days                    # (5) staleness measured from the TARGET date
    if age > max_staleness_days:
        return None
    return SelectedRate(observation_id=row.id, base=base, quote=quote, rate=row.rate,
                        effective_date=row.effective_date, source=row.source, source_series=row.source_series,
                        source_url=row.source_url, revision_number=row.revision_number,
                        retrieved_at=row.retrieved_at, current_date=current_date, target_date=target_date,
                        age_days=age)


class StoredFredFxRateSource:
    """DB-only ``FxRateSource`` backed by the Fed H.10 ``DEXUSUK`` store. USD<->USD is identity; GBP->USD uses the
    selected observation (governed previous-day + carry-forward + 7-day staleness). Any other pair, or no usable
    rate, returns ``None`` => honest PARTIAL/UNAVAILABLE. It NEVER contacts FRED (DB-only), and NEVER raises — any
    failure degrades to unavailable so no FX fault can stop the dashboard summary settling (F5)."""

    def __init__(self, current_date: Optional[date] = None):
        self.current_date = current_date or governed_today()
        self._cache: Dict[Tuple[str, str], Optional[SelectedRate]] = {}

    def _select(self, base: str, quote: str) -> Optional[SelectedRate]:
        key = (FX.normalise_currency(base), FX.normalise_currency(quote))
        if key not in self._cache:
            b, q = key
            try:
                self._cache[key] = (select_observation(b, q, self.current_date)
                                    if (b, q) == (FRED.BASE, FRED.QUOTE) else None)
            except Exception:  # noqa: BLE001 — an FX read must never break the dashboard; degrade to unavailable
                logger.warning("fx_source: observation select failed; degrading to honest unavailable")
                self._cache[key] = None
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
        usable rate. The exact unrounded rate is surfaced as a decimal STRING (never a lossy float). Includes the
        governed timezone, target date and source URL for auditability."""
        sel = self._select(base, quote)
        common = {"enabled": True, "pair": f"{base}{quote}", "source": DailyFxObservation.SOURCE_FED_H10,
                  "source_series": FRED.SERIES, "direction": FRED.DIRECTION, "governed_tz": GOVERNED_TZ,
                  "current_date": self.current_date.isoformat(),
                  "target_date": (self.current_date - timedelta(days=1)).isoformat()}
        if sel is None:
            return {**common, "available": False, "reason": "no_rate_within_staleness"}
        return {**common, "available": True, "rate": format(sel.rate.normalize(), "f"),
                "effective_date": sel.effective_date.isoformat(), "source": sel.source,
                "revision_number": sel.revision_number, "observation_id": sel.observation_id,
                "source_url": sel.source_url,
                "retrieved_at": sel.retrieved_at.isoformat() if sel.retrieved_at else "", "age_days": sel.age_days}


KNOWN_SOURCES = (DailyFxObservation.SOURCE_FED_H10, DailyFxObservation.SOURCE_BOE)


def fx_source_priority() -> tuple:
    """GOVERNED, deterministic source priority for GBP->USD selection. Parsed from ``PORTFOLIO_FX_SOURCE_PRIORITY``
    (comma-separated source codes, settings-override-then-env). DEFAULT ``('FED_H10',)`` — i.e. Fed H.10 ONLY, so
    the request path is byte-identical to today until a Sponsor sets a different priority. Unknown codes are
    dropped; an empty/invalid value falls back to Fed H.10 (never 'no source'). This is the explicit, disclosed
    failover order — it is NOT silent auto-substitution: the SELECTED source is always surfaced in provenance."""
    raw = getattr(settings, "PORTFOLIO_FX_SOURCE_PRIORITY", None)
    if raw is None:
        raw = os.getenv("PORTFOLIO_FX_SOURCE_PRIORITY", "")
    seen, order = set(), []
    for tok in str(raw).split(","):
        s = tok.strip().upper()
        if s in KNOWN_SOURCES and s not in seen:
            seen.add(s); order.append(s)
    return tuple(order) or (DailyFxObservation.SOURCE_FED_H10,)


def select_prioritised(base: str, quote: str, current_date: date, priority: tuple,
                       *, max_staleness_days: int = MAX_STALENESS_DAYS) -> Optional[SelectedRate]:
    """Deterministic failover: try each source in ``priority`` order and return the FIRST that yields a usable
    (within-staleness) observation. Each source applies the same governed selection (prev-London-day + carry
    forward + 7-day staleness). Returns ``None`` when no source has a usable rate (caller keeps honest
    PARTIAL/UNAVAILABLE). Sources are never blended; exactly one source's observation is chosen and disclosed."""
    for src in priority:
        sel = select_observation(base, quote, current_date, source=src, max_staleness_days=max_staleness_days)
        if sel is not None:
            return sel
    return None


class StoredDbFxRateSource:
    """DB-only ``FxRateSource`` over a GOVERNED source PRIORITY (default Fed H.10 only). USD<->USD is identity;
    GBP->USD selects the first within-staleness observation across the priority order (e.g. BoE XUDLUSS then Fed
    H.10 DEXUSUK). Any other pair / no usable rate => ``None`` (honest PARTIAL/UNAVAILABLE). DB-only (never
    contacts a provider) and NEVER raises — any failure degrades to unavailable so no FX fault stops the
    dashboard settling (F5). The provenance always names the SELECTED source (no silent substitution)."""

    def __init__(self, current_date: Optional[date] = None, priority: Optional[tuple] = None):
        self.current_date = current_date or governed_today()
        self.priority = tuple(priority) if priority else fx_source_priority()
        self._cache: Dict[Tuple[str, str], Optional[SelectedRate]] = {}

    def _select(self, base: str, quote: str) -> Optional[SelectedRate]:
        key = (FX.normalise_currency(base), FX.normalise_currency(quote))
        if key not in self._cache:
            b, q = key
            try:
                self._cache[key] = (select_prioritised(b, q, self.current_date, self.priority)
                                    if (b, q) == (FRED.BASE, FRED.QUOTE) else None)
            except Exception:  # noqa: BLE001 — an FX read must never break the dashboard
                logger.warning("fx_source: prioritised observation select failed; degrading to honest unavailable")
                self._cache[key] = None
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
        sel = self._select(base, quote)
        common = {"enabled": True, "pair": f"{base}{quote}", "source_series": FRED.SERIES,
                  "direction": FRED.DIRECTION, "governed_tz": GOVERNED_TZ,
                  "source_priority": list(self.priority),
                  "current_date": self.current_date.isoformat(),
                  "target_date": (self.current_date - timedelta(days=1)).isoformat()}
        if sel is None:
            return {**common, "source": self.priority[0], "available": False,
                    "reason": "no_rate_within_staleness"}
        return {**common, "available": True, "rate": format(sel.rate.normalize(), "f"),
                "effective_date": sel.effective_date.isoformat(), "source": sel.source,
                "selected_source": sel.source, "source_series": sel.source_series,
                "revision_number": sel.revision_number, "observation_id": sel.observation_id,
                "source_url": sel.source_url,
                "retrieved_at": sel.retrieved_at.isoformat() if sel.retrieved_at else "", "age_days": sel.age_days}


def fx_ingestion_health(current_date: Optional[date] = None, *,
                        source: str = DailyFxObservation.SOURCE_FED_H10) -> dict:
    """Readiness of the scheduled FX ingestion (ops/health). HEALTHY when a usable (within-staleness) rate exists,
    STALE when the latest accepted observation is too old to convert, EMPTY when nothing is ingested yet."""
    current_date = current_date or governed_today()
    base, quote = FRED.BASE, FRED.QUOTE
    accepted = DailyFxObservation.objects.filter(
        base_currency=base, quote_currency=quote, source=source,
        acceptance_state=DailyFxObservation.STATE_ACCEPTED)
    latest = accepted.order_by("-effective_date", "-revision_number").first()
    if latest is None:
        return {"status": "EMPTY", "source": source, "series": FRED.SERIES, "accepted_count": 0,
                "latest_effective_date": None, "latest_retrieved_at": None, "age_days": None, "convertible": False,
                "governed_tz": GOVERNED_TZ, "current_date": current_date.isoformat()}
    convertible = select_observation(base, quote, current_date, source=source) is not None
    return {"status": "HEALTHY" if convertible else "STALE", "source": source, "series": FRED.SERIES,
            "accepted_count": accepted.count(),
            "latest_effective_date": latest.effective_date.isoformat(),
            "latest_retrieved_at": latest.retrieved_at.isoformat() if latest.retrieved_at else None,
            "age_days": (current_date - timedelta(days=1) - latest.effective_date).days,
            "convertible": convertible, "governed_tz": GOVERNED_TZ, "current_date": current_date.isoformat()}
