"""Deterministic contiguous half-open monthly chunk planning (GFX-PKT-006C).

No acquisition happens here — this only plans [start, end) monthly windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .contracts import ContractError, MINUTE_UTC_RE


@dataclass(frozen=True)
class Chunk:
    start_utc: str
    end_utc: str


def _parse(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not MINUTE_UTC_RE.match(value):
        raise ContractError(f"{field} must be a minute-aligned UTC 'Z' instant")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ContractError(f"{field} is not a valid calendar instant: {value!r}") from exc


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def _next_month_start(dt: datetime) -> datetime:
    year, month = dt.year, dt.month
    if month == 12:
        return datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(year, month + 1, 1, tzinfo=timezone.utc)


def monthly_chunks(range_start_utc: str, range_end_utc: str) -> list[Chunk]:
    """Plan contiguous half-open monthly chunks covering [start, end).

    Partial first/last months are preserved without expanding the requested range.
    """
    start = _parse(range_start_utc, "range_start_utc")
    end = _parse(range_end_utc, "range_end_utc")
    if not (end > start):
        raise ContractError("range_end_utc must be later than range_start_utc")

    chunks: list[Chunk] = []
    cursor = start
    while cursor < end:
        boundary = _next_month_start(cursor)
        chunk_end = boundary if boundary < end else end
        chunks.append(Chunk(start_utc=_fmt(cursor), end_utc=_fmt(chunk_end)))
        cursor = chunk_end
    return chunks
