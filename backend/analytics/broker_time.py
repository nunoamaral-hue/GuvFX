"""analytics.broker_time — DST-aware conversion of MT5 broker-server wall-time to canonical UTC.

Proven (2026-09-28): MT5 ``deal.time`` is broker SERVER wall-time encoded as a unix second (``utcfromtimestamp``
decodes it to the broker's wall clock, not true UTC). This module converts it correctly USING THE BROKER'S IANA
timezone, so the offset follows DST across seasons instead of a hard-coded constant.

FAIL-SAFE: every function returns ``None`` when the server timezone is unset/invalid. Nothing in the ingest hot
path calls these yet — the mechanism ships DARK and inert until an operator sets an AUTHORITATIVE
``BrokerServer.server_timezone`` per server (packet B3/B5: do not guess an offset, do not rewrite history until
proven). ``measure_broker_offset`` provides the evidence to set it.

DST fall-back ambiguity: ``deal.time`` is a wall-clock value with the offset stripped, so a wall time in the ONE
ambiguous hour of the autumn fall-back (e.g. 03:30 occurring twice) is irrecoverable from the source alone. These
helpers resolve it to the earlier (pre-transition) offset (zoneinfo ``fold=0``); a deal executed in the SECOND
occurrence is therefore placed 1h early. Bounded (≤1h, ≤once/year, only that window) and inert today. When the
mechanism is wired live, flag deals landing in a server's fall-back hour as ambiguous rather than trusting fold=0.
"""
from __future__ import annotations

from datetime import datetime, timezone as _tz
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9
    ZoneInfo = None  # type: ignore


def _zone(server_timezone: Optional[str]):
    if not server_timezone or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(server_timezone)
    except Exception:  # noqa: BLE001 - unknown/invalid IANA name -> fail safe
        return None


def broker_offset_hours(server_timezone: Optional[str], at_utc: Optional[datetime] = None) -> Optional[float]:
    """The broker server's UTC offset in hours at ``at_utc`` (DST-aware), or None when the tz is unset/invalid."""
    tz = _zone(server_timezone)
    if tz is None:
        return None
    ref = at_utc or datetime.now(_tz.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=_tz.utc)
    off = ref.astimezone(tz).utcoffset()
    return off.total_seconds() / 3600.0 if off is not None else None


def deal_time_to_true_utc(unix_ts, server_timezone: Optional[str]) -> Optional[datetime]:
    """Convert an MT5 ``deal.time`` (unix seconds encoding broker SERVER wall-time) to a true-UTC aware datetime,
    DST-aware via the broker's IANA timezone. Returns None when ``unix_ts`` is missing or the tz is unset/invalid
    (caller then keeps the existing naive handling — no silent wrong conversion)."""
    tz = _zone(server_timezone)
    if unix_ts is None or tz is None:
        return None
    try:
        wall = datetime.utcfromtimestamp(float(unix_ts))    # broker wall-clock datetime (naive), per the MT5 gotcha
    except (ValueError, OSError, TypeError):
        return None
    local = wall.replace(tzinfo=tz)                          # reinterpret that wall time as broker-local ...
    return local.astimezone(_tz.utc)                         # ... and convert to true UTC (DST handled by ZoneInfo)


def broker_wall_to_true_utc(wall_dt: datetime, server_timezone: Optional[str]) -> Optional[datetime]:
    """Convert an already-decoded broker wall-time datetime (naive or the mislabeled-UTC value GuvFX stores today)
    to true UTC, DST-aware. Returns None when the tz is unset/invalid. Used by a future idempotent migration once a
    server's timezone is authoritatively established."""
    tz = _zone(server_timezone)
    if wall_dt is None or tz is None:
        return None
    naive = wall_dt.replace(tzinfo=None)                     # drop the (mislabeled) UTC tag -> the broker wall clock
    return naive.replace(tzinfo=tz).astimezone(_tz.utc)
