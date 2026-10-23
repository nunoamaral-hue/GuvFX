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
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Dict, List, Optional, Protocol

USD = "USD"


def _money_half_even(value: float) -> float:
    """Quantise a USD money amount to 2 decimal places with Decimal ROUND_HALF_EVEN — deterministic banker's
    rounding on the exact decimal value, not float-binary rounding (ADR-0058 / PD: displayed portfolio money uses
    Decimal ROUND_HALF_EVEN). For values already exact at ≤2dp (every USD-only total) the result is identical to
    the prior ``round(x, 2)``, so flag-OFF behaviour is unchanged (guarded by the analytics regression suite)."""
    return float(Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))

# ── Safe, non-identifying exclusion categories ────────────────────────────────────────────────────────────────
# Why a figure could not enter a USD aggregate, expressed as a stable CATEGORY CODE the UI maps to a human label.
# These are deliberately coarse and carry NO credentials, account numbers, logins or any PII (security.md: redact
# in evidence; evidence.md: state limitations — say what was not covered). The UI rolls them into two buckets: a
# currency bucket (CURRENCY_UNCONVERTIBLE) and a "stale or missing account data" bucket (everything else).
CURRENCY_UNCONVERTIBLE = "currency_unconvertible"   # a non-USD figure with no authoritative rate
STALE_OBSERVATION = "stale_observation"             # the account's live read could not be completed (stale)
MISSING_SNAPSHOT = "missing_snapshot"               # no observed figure to convert (balance/equity unavailable)
READ_REFUSED = "read_refused"                        # an identity-firewall / permission refusal (reserved)

# A figure's low-level ``reason`` (from ``to_usd`` and the open-P/L path) -> its safe UI category.
_REASON_CATEGORY = {
    "rate_unavailable": CURRENCY_UNCONVERTIBLE,
    "amount_unavailable": MISSING_SNAPSHOT,
    "amount_invalid": MISSING_SNAPSHOT,
    "stale_read": STALE_OBSERVATION,
    "read_refused": READ_REFUSED,
}

# Aggregate reporting states (machine codes the UI maps to labels):
STATE_OK = "OK"                     # every item converted — a trustworthy measured USD total
STATE_PARTIAL = "PARTIAL"           # some items converted, some excluded — a measured total over the INCLUDED subset
STATE_UNAVAILABLE = "UNAVAILABLE"   # items existed but NONE could be converted — NO measured number (never 0.0)
STATE_EMPTY = "EMPTY"               # no items at all (e.g. no accounts in scope) — genuinely nothing to report


def reason_category(reason: Optional[str]) -> str:
    """Map a low-level ``reason`` to its safe, non-identifying UI category code."""
    return _REASON_CATEGORY.get(reason or "", MISSING_SNAPSHOT)


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
    unconverted: List[dict] = field(default_factory=list)  # [{account_id, native, native_currency, reason, category}]

    @property
    def excluded_count(self) -> int:
        return len(self.unconverted)

    @property
    def state(self) -> str:
        """OK/PARTIAL/UNAVAILABLE/EMPTY. Distinguishes "all eligible items excluded" (UNAVAILABLE — there is no
        honest number, so ``as_dict`` emits ``null``, never a misleading 0.0) from "no items at all" (EMPTY —
        genuinely nothing to report). ``converted_count`` is the number of items that actually entered the sum."""
        if self.converted_count > 0:
            return STATE_PARTIAL if self.unconverted else STATE_OK
        return STATE_UNAVAILABLE if self.unconverted else STATE_EMPTY

    @property
    def reason(self) -> Optional[str]:
        """The dominant exclusion category (``None`` when nothing was excluded; ``"mixed"`` when exclusions span
        more than one category). The UI treats only ``currency_unconvertible`` specially; everything else means
        "stale or missing account data"."""
        cats = {u.get("category") for u in self.unconverted if u.get("category")}
        if not cats:
            return None
        return next(iter(cats)) if len(cats) == 1 else "mixed"

    def as_dict(self) -> dict:
        st = self.state
        return {
            # A measured number ONLY when at least one item converted. UNAVAILABLE carries ``null`` so an
            # all-excluded portfolio can never read as a measured "$0"; EMPTY (no accounts) keeps 0.0.
            "total_usd": (None if st == STATE_UNAVAILABLE else _money_half_even(self.total_usd)),
            "basis": self.basis,
            "state": st,
            "reason": self.reason,
            "converted_count": self.converted_count,   # preserved (existing consumers) == included_count
            "included_count": self.converted_count,
            "excluded_count": self.excluded_count,
            "unconverted": self.unconverted,
        }


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
                                    "native_currency": rec["native_currency"], "reason": rec["reason"],
                                    "category": reason_category(rec["reason"])})
    return agg
