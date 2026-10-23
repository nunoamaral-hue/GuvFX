"""analytics.fx_fred — Federal Reserve H.10 ``DEXUSUK`` (USD per GBP) daily-rate CSV parser + validation.

``DEXUSUK`` is the Fed H.10 "U.S. dollars to one U.K. pound sterling" daily noon buying rate — exactly the
orientation the portfolio needs: multiply a GBP amount by this rate to get USD (quote=USD per 1 base=GBP). The
keyless FRED CSV download (``fredgraph.csv?id=DEXUSUK``) returns rows ``YYYY-MM-DD,<rate>`` with ``.`` on
non-publication days (weekends, holidays).

This module is PURE parsing + validation — it performs NO network I/O (the fetch command owns that), so it is
trivially testable from a fixture with no live-FRED dependency (research.md: reference exactly; evidence.md: state
limitations, never fabricate).
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import List

SOURCE = "FED_H10"
SERIES = "DEXUSUK"
BASE = "GBP"
QUOTE = "USD"
DIRECTION = "USD_PER_GBP"   # rate = USD units per 1 GBP

# Plausibility band for USD/GBP. Historical GBPUSD has ranged roughly 1.05–2.65 across decades; this generous band
# rejects clearly-corrupt values (zero, negative, a decimal-shift, or an inverted GBP-per-USD ≈ 0.7) WITHOUT
# rejecting any genuine published rate. Outside the band ⇒ REJECTED (never silently used).
MIN_PLAUSIBLE = Decimal("0.80")
MAX_PLAUSIBLE = Decimal("3.50")

# A FRED CSV cell that means "no observation published that day" (NOT a corrupt value).
_MISSING_TOKENS = {".", "", "na", "nan", "null", "nd"}


class FxParseError(ValueError):
    """The payload does not look like the expected Fed H.10 ``DEXUSUK`` CSV (wrong series / columns / shape)."""


@dataclass(frozen=True)
class ParsedObservation:
    effective_date: date
    rate: Decimal              # exact, unrounded published value
    observation_checksum: str  # sha256 of the canonical tuple (idempotency / revision key)
    raw_line: str = ""         # exact source CSV line (reproducibility evidence — never a checksum alone)


@dataclass(frozen=True)
class RejectedRow:
    raw_date: str
    raw_value: str
    reason: str                # nonnumeric | nonpositive | implausible | bad_date


@dataclass(frozen=True)
class ParseResult:
    accepted: List[ParsedObservation] = field(default_factory=list)
    rejected: List[RejectedRow] = field(default_factory=list)
    missing_count: int = 0     # non-publication days ("."): absent, neither accepted nor rejected
    row_count: int = 0
    payload_sha256: str = ""   # sha256 of the WHOLE raw payload (transport integrity; NOT the per-observation key)


def observation_checksum(source: str, series: str, base: str, quote: str, eff: date, rate: Decimal) -> str:
    """Canonical per-observation key. An identical published value yields an identical checksum (idempotent
    re-ingest is a no-op); a REVISED value yields a different checksum (stored as a new revision). The rate is
    normalised to its exact decimal string so the key is stable and reproducible."""
    canonical = f"{source}|{series}|{base}|{quote}|{eff.isoformat()}|{format(rate.normalize(), 'f')}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_dexusuk_csv(text: str) -> ParseResult:
    """Parse + validate a Fed H.10 ``DEXUSUK`` CSV payload. Raises ``FxParseError`` when the payload is not the
    expected series/shape (wrong-series guard). Per-row validation never raises — a malformed value is REJECTED
    (collected, counted) and a non-publication ``.`` is recorded as missing; only clean, plausible, positive
    values become accepted observations."""
    payload_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise FxParseError("empty payload")
    if len(header) < 2:
        raise FxParseError("unexpected column count")
    date_col = header[0].strip().lower()
    if date_col not in ("date", "observation_date"):
        raise FxParseError(f"unexpected date column header: {header[0]!r}")
    if header[1].strip().upper() != SERIES:
        raise FxParseError(f"wrong series column: {header[1]!r} (expected {SERIES})")

    accepted: List[ParsedObservation] = []
    rejected: List[RejectedRow] = []
    missing = 0
    rows = 0
    for row in reader:
        if not row or len(row) < 2:
            continue
        rows += 1
        raw_date, raw_val = row[0].strip(), row[1].strip()
        try:
            eff = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            rejected.append(RejectedRow(raw_date, raw_val, "bad_date"))
            continue
        if raw_val.lower() in _MISSING_TOKENS:
            missing += 1
            continue
        try:
            rate = Decimal(raw_val)
        except InvalidOperation:
            rejected.append(RejectedRow(raw_date, raw_val, "nonnumeric"))
            continue
        if rate <= 0:
            rejected.append(RejectedRow(raw_date, raw_val, "nonpositive"))
            continue
        if not (MIN_PLAUSIBLE <= rate <= MAX_PLAUSIBLE):
            rejected.append(RejectedRow(raw_date, raw_val, "implausible"))
            continue
        accepted.append(ParsedObservation(
            eff, rate, observation_checksum(SOURCE, SERIES, BASE, QUOTE, eff, rate),
            raw_line=f"{raw_date},{raw_val}"))
    return ParseResult(accepted=accepted, rejected=rejected, missing_count=missing,
                       row_count=rows, payload_sha256=payload_sha)
