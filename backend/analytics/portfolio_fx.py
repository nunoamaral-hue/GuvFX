"""analytics.portfolio_fx — USD normalisation for the multi-account portfolio dashboard.

Reporting/base currency is USD. This layer converts each account's native monetary figures to USD and aggregates
them HONESTLY:

* USD -> USD is always rate 1.0.
* A non-USD figure is converted only with an authoritative rate from the injected ``rate_source``; pair
  orientation/inversion is handled generically (GBPUSD direct, USDJPY inverse, etc.).
* If a required rate is missing / stale / zero / invalid, the figure is NOT converted and NOT summed — the
  affected account is surfaced under ``unconverted`` and the aggregate ``basis`` becomes ``PARTIAL``. We never
  silently use rate 1.0 for a non-USD currency, and never sum mixed denominations into one number
  (governance: data.md point-in-time correctness; evidence.md "state limitations / do not fabricate").

There is no reliable internal FX-rate source in GuvFX today (verified), and every production account is effectively
USD (``account_currency`` is unpopulated -> USD). So the default ``NullFxRateSource`` converts USD->USD only and
degrades everything else to PARTIAL — correct and honest until a vetted rate source is wired.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

USD = "USD"


def normalise_currency(cur: Optional[str]) -> str:
    """A blank/None account currency means 'unpopulated' which the estate treats as USD (see forensic)."""
    c = (cur or "").strip().upper()
    return c or USD


@dataclass(frozen=True)
class FxRate:
    """An authoritative rate to multiply a ``base`` amount by to get a ``quote`` amount, with provenance."""
    base: str
    quote: str
    rate: float
    as_of: str = ""      # ISO timestamp of the quote (traceability)
    source: str = ""


class FxRateSource(Protocol):
    def get_rate(self, base: str, quote: str) -> Optional[FxRate]:
        """Return an FxRate for base->quote (or its invertible counterpart), or None if unavailable/stale."""
        ...


class NullFxRateSource:
    """No external rates. USD<->USD is trivially 1.0; everything else is unavailable (honest PARTIAL degrade)."""

    def get_rate(self, base: str, quote: str) -> Optional[FxRate]:
        if normalise_currency(base) == USD and normalise_currency(quote) == USD:
            return FxRate(USD, USD, 1.0, source="identity")
        return None


class MappingFxRateSource:
    """A simple explicit-rate source (dict of ('BASE','QUOTE') -> rate) with generic inversion. Intended for tests
    and a future vetted feed; it inverts a quote it only holds in the opposite direction (USDJPY <-> JPYUSD)."""

    def __init__(self, rates: Dict[tuple, float], *, as_of: str = "", source: str = "mapping"):
        self._rates = {(normalise_currency(b), normalise_currency(q)): float(r) for (b, q), r in rates.items()}
        self._as_of = as_of
        self._source = source

    def get_rate(self, base: str, quote: str) -> Optional[FxRate]:
        b, q = normalise_currency(base), normalise_currency(quote)
        if b == q:
            return FxRate(b, q, 1.0, self._as_of, "identity")
        direct = self._rates.get((b, q))
        if direct is not None and direct > 0:
            return FxRate(b, q, direct, self._as_of, self._source)
        inv = self._rates.get((q, b))
        if inv is not None and inv > 0:
            return FxRate(b, q, 1.0 / inv, self._as_of, self._source + ":inverted")
        return None


def to_usd(amount: Optional[float], currency: Optional[str], rate_source: FxRateSource) -> dict:
    """Convert ``amount`` in ``currency`` to USD. Returns a traceable record::

        {"ok": bool, "usd": float|None, "native": float|None, "native_currency": str,
         "rate": float|None, "rate_as_of": str, "rate_source": str, "reason": str}

    ``ok`` is False (usd None) when the amount is missing or a non-USD rate is unavailable/invalid — the caller
    must then treat the figure as unconverted, never as zero or as if it were USD.
    """
    cur = normalise_currency(currency)
    rec = {"ok": False, "usd": None, "native": amount, "native_currency": cur,
           "rate": None, "rate_as_of": "", "rate_source": "", "reason": ""}
    if amount is None:
        rec["reason"] = "amount_unavailable"
        return rec
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        rec["reason"] = "amount_invalid"
        return rec
    if cur == USD:
        rec.update(ok=True, usd=amt, rate=1.0, rate_source="identity", reason="usd")
        return rec
    fx = rate_source.get_rate(cur, USD)
    if fx is None or not fx.rate or fx.rate <= 0:
        rec["reason"] = "rate_unavailable"
        return rec
    rec.update(ok=True, usd=amt * fx.rate, rate=fx.rate, rate_as_of=fx.as_of,
               rate_source=fx.source, reason="converted")
    return rec


@dataclass
class UsdAggregate:
    total_usd: float = 0.0
    basis: str = "USD"                                   # "USD" (all converted) or "PARTIAL" (some excluded)
    converted_count: int = 0
    unconverted: List[dict] = field(default_factory=list)  # [{account_id, native, native_currency, reason}]

    def as_dict(self) -> dict:
        return {"total_usd": round(self.total_usd, 2), "basis": self.basis,
                "converted_count": self.converted_count, "unconverted": self.unconverted}


def aggregate_usd(items: List[dict], rate_source: FxRateSource) -> UsdAggregate:
    """Sum ``[{account_id, amount, currency}]`` into USD. Any item that cannot be converted is EXCLUDED from the
    total and listed in ``unconverted`` with a reason, and the basis becomes PARTIAL — mixed denominations are
    never summed into one number."""
    agg = UsdAggregate()
    for it in items:
        rec = to_usd(it.get("amount"), it.get("currency"), rate_source)
        if rec["ok"]:
            agg.total_usd += rec["usd"]
            agg.converted_count += 1
        else:
            agg.basis = "PARTIAL"
            agg.unconverted.append({"account_id": it.get("account_id"), "native": rec["native"],
                                    "native_currency": rec["native_currency"], "reason": rec["reason"]})
    return agg
