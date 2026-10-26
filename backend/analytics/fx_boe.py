"""analytics.fx_boe — Bank of England ``XUDLUSS`` (US dollar into Sterling) daily spot-rate CSV parser + validation.

``XUDLUSS`` is the Bank of England's daily "Spot exchange rate, US $ into Sterling" — the SAME orientation the
portfolio needs (multiply a GBP amount by this rate to get USD: quote=USD per 1 base=GBP), identical to the Fed
H.10 ``DEXUSUK`` orientation. The BoE Statistical Interactive Database (IADB) serves a keyless CSV
(``_iadb-fromshowcolumns.asp?csv.x=yes&SeriesCodes=XUDLUSS``) with rows ``DD Mon YYYY,<rate>`` and NO row for a
non-publication day (unlike the Fed CSV's ``.`` placeholder). BoE publishes next-business-day (T+1), materially
fresher than the Fed H.10 ~1-week lag.

Like ``fx_fred``, this module is PURE parsing + validation — NO network I/O (the fetch command owns that) — so it
is testable from a fixture with no live-BoE dependency. The same ``observation_checksum`` keying is reused so a
BoE revision is idempotent/append-only exactly like a Fed one. Direction is asserted (USD per GBP); a corrupt /
zero / negative / implausible value is REJECTED, never silently used (research.md / evidence.md / data.md).
"""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import List

from analytics.fx_fred import (
    FxParseError, MAX_PLAUSIBLE, MIN_PLAUSIBLE, ParsedObservation, ParseResult, RejectedRow,
    _MISSING_TOKENS, observation_checksum,
)
from analytics.models import DailyFxObservation

SOURCE = DailyFxObservation.SOURCE_BOE   # "BOE"
SERIES = "XUDLUSS"
BASE = "GBP"
QUOTE = "USD"
DIRECTION = "USD_PER_GBP"   # rate = USD units per 1 GBP (same orientation as Fed H.10 DEXUSUK)

# BoE IADB date cells are "DD Mon YYYY" (e.g. "01 Oct 2026"); some historic exports use "DD/Mon/YYYY".
_DATE_FORMATS = ("%d %b %Y", "%d/%b/%Y")


def _parse_date(raw: str):
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_xudluss_csv(text: str) -> ParseResult:
    """Parse + validate a BoE ``XUDLUSS`` CSV payload. Raises ``FxParseError`` on the wrong series/shape
    (wrong-series guard). Per-row validation never raises: a malformed value is REJECTED (collected, counted);
    only clean, plausible, positive values become accepted observations. USD-per-GBP orientation is intrinsic to
    the series (the plausibility band ~0.80–3.50 would reject an inverted GBP-per-USD ≈ 0.7)."""
    payload_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise FxParseError("empty payload")
    if len(header) < 2:
        raise FxParseError("unexpected column count")
    if header[0].strip().lower() not in ("date", "observation_date"):
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
        eff = _parse_date(raw_date)
        if eff is None:
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
