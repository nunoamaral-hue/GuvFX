"""GuvFX-TIM pinned, source-specific trading-session calendars (ADR-0130).

A calendar is a PINNED, content-addressed description of when a source's market is open, so that expected bar
counts and gap classifications are computed from an explicit schedule instead of raw calendar minutes. It is
deliberately self-contained and dependency-free (NO zoneinfo / tzdata): DST and offset changes are declared
EXPLICITLY as dated UTC-offset rules, so the same bytes yield the same expected counts on any host.

Semantics (all half-open `[open, close)`; timeframe is minutes):
  * ``utc_offset_rules``  — sorted ``{effective_from_utc, offset_seconds, label}``; the offset in force at a
    UTC instant is the last rule whose ``effective_from_utc`` is <= it. Local time = UTC + offset.
  * ``weekly_sessions_local`` — ISO weekday ("1"=Mon … "7"=Sun) -> list of [``"HH:MM"``, ``"HH:MM"``] in LOCAL
    time; ``"24:00"`` means end-of-day. Empty list / missing weekday = closed.
  * ``holidays`` — ``"YYYY-MM-DD"`` local dates that are FULLY closed (override the weekly schedule).
  * ``exceptional_sessions_local`` — ``"YYYY-MM-DD"`` -> list of [open, close] local, overriding the weekly
    schedule for that date (partial sessions, half-days).

The calendar NEVER invents data; it only says which minutes are IN SESSION. Classifying an in-session minute
that has no bar as a verified no-tick vs an unexplained gap is the caller's job, using separate evidence.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json

CALENDAR_SCHEMA_VERSION = "1.0"
_REQUIRED_KEYS = ("schema_version", "calendar_id", "source_id", "iana_timezone", "timeframe_seconds",
                  "utc_offset_rules", "weekly_sessions_local", "holidays", "exceptional_sessions_local")


class CalendarError(Exception):
    """A pinned calendar is malformed. Fail-closed: a bad calendar never yields a silent count."""


def canonical_bytes(cal: dict) -> bytes:
    return json.dumps(cal, sort_keys=True, separators=(",", ":")).encode("utf-8")


def calendar_digest(cal: dict) -> str:
    return hashlib.sha256(canonical_bytes(cal)).hexdigest()


def _parse_hhmm(s: str) -> int:
    """Minutes-after-local-midnight for 'HH:MM'; '24:00' -> 1440 (end of day)."""
    try:
        h, m = s.split(":")
        hi, mi = int(h), int(m)
    except Exception:
        raise CalendarError("bad HH:MM: %r" % (s,))
    if not (0 <= hi <= 24 and 0 <= mi <= 59 and (hi, mi) != (24, 1) and hi * 60 + mi <= 1440):
        raise CalendarError("HH:MM out of range: %r" % (s,))
    return hi * 60 + mi


def _epoch_min(iso: str) -> int:
    if not (isinstance(iso, str) and iso.endswith("Z")):
        raise CalendarError("timestamp must be canonical UTC 'Z': %r" % (iso,))
    try:
        d = _dt.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    except Exception:
        raise CalendarError("unparseable UTC instant: %r" % (iso,))
    if d.second != 0:
        raise CalendarError("timestamp must be minute-aligned: %r" % (iso,))
    return int(d.timestamp()) // 60


def validate_calendar(cal: dict) -> None:
    """Structural validation of a pinned calendar. Fail-closed."""
    if not isinstance(cal, dict) or set(cal) != set(_REQUIRED_KEYS):
        raise CalendarError("calendar: exact keys %r required" % (list(_REQUIRED_KEYS),))
    if cal["schema_version"] != CALENDAR_SCHEMA_VERSION:
        raise CalendarError("calendar: schema_version must be %r" % CALENDAR_SCHEMA_VERSION)
    for k in ("calendar_id", "source_id", "iana_timezone"):
        if not (isinstance(cal[k], str) and cal[k]):
            raise CalendarError("calendar: %s must be a non-empty string" % k)
    if cal["timeframe_seconds"] != 60:
        raise CalendarError("calendar: only M1 (timeframe_seconds=60) is supported")
    rules = cal["utc_offset_rules"]
    if not (isinstance(rules, list) and rules):
        raise CalendarError("calendar: at least one utc_offset_rule is required")
    last = None
    for r in rules:
        if not isinstance(r, dict) or set(r) != {"effective_from_utc", "offset_seconds", "label"}:
            raise CalendarError("calendar: utc_offset_rule keys {effective_from_utc, offset_seconds, label}")
        e = _epoch_min(r["effective_from_utc"])
        if last is not None and e <= last:
            raise CalendarError("calendar: utc_offset_rules must be strictly increasing by effective_from_utc")
        last = e
        if not isinstance(r["offset_seconds"], int) or isinstance(r["offset_seconds"], bool):
            raise CalendarError("calendar: offset_seconds must be int")
        if r["offset_seconds"] % 60 != 0:
            raise CalendarError("calendar: offset_seconds must be a whole number of minutes")
        if abs(r["offset_seconds"]) > 18 * 3600:
            raise CalendarError("calendar: offset_seconds out of range")
    ws = cal["weekly_sessions_local"]
    if not isinstance(ws, dict):
        raise CalendarError("calendar: weekly_sessions_local must be an object")
    for wd, spans in ws.items():
        if wd not in {"1", "2", "3", "4", "5", "6", "7"}:
            raise CalendarError("calendar: weekday keys must be '1'..'7'")
        _validate_spans(spans)
    if not (isinstance(cal["holidays"], list) and all(isinstance(h, str) for h in cal["holidays"])):
        raise CalendarError("calendar: holidays must be a list of 'YYYY-MM-DD'")
    for h in cal["holidays"]:
        _parse_date(h)
    exc = cal["exceptional_sessions_local"]
    if not isinstance(exc, dict):
        raise CalendarError("calendar: exceptional_sessions_local must be an object")
    for d, spans in exc.items():
        _parse_date(d)
        _validate_spans(spans)


def _validate_spans(spans) -> None:
    if not isinstance(spans, list):
        raise CalendarError("calendar: a session span list is required (may be empty)")
    prev_close = -1
    for span in spans:
        if not (isinstance(span, list) and len(span) == 2):
            raise CalendarError("calendar: each span is ['HH:MM','HH:MM']")
        o, c = _parse_hhmm(span[0]), _parse_hhmm(span[1])
        if c <= o:
            raise CalendarError("calendar: span close must be after open (half-open)")
        if o < prev_close:
            raise CalendarError("calendar: session spans must be ordered and non-overlapping")
        prev_close = c


def _parse_date(s: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(s)
    except Exception:
        raise CalendarError("bad date: %r" % (s,))


def _offset_at(rules: list, utc_min: int) -> int:
    off = None
    for r in rules:
        if _epoch_min(r["effective_from_utc"]) <= utc_min:
            off = r["offset_seconds"]
        else:
            break
    if off is None:
        raise CalendarError("calendar: no utc_offset_rule in force at %d (range precedes the first rule)" % utc_min)
    return off


def _spans_for_local_date(cal: dict, d: _dt.date):
    ds = d.isoformat()
    if ds in cal["holidays"]:
        return []
    if ds in cal["exceptional_sessions_local"]:
        return cal["exceptional_sessions_local"][ds]
    return cal["weekly_sessions_local"].get(str(d.isoweekday()), [])


def is_in_session(cal: dict, utc_min: int) -> bool:
    """Is the UTC minute (epoch minutes) inside a scheduled session for this calendar?"""
    off_min = _offset_at(cal["utc_offset_rules"], utc_min) // 60
    local_total = utc_min + off_min
    local_day = local_total // 1440
    minute_of_day = local_total - local_day * 1440
    d = _dt.date(1970, 1, 1) + _dt.timedelta(days=local_day)
    for span in _spans_for_local_date(cal, d):
        o, c = _parse_hhmm(span[0]), _parse_hhmm(span[1])
        if o <= minute_of_day < c:
            return True
    return False


def expected_bar_count(cal: dict, range_start_utc: str, range_end_utc: str) -> int:
    """Count of in-session M1 minutes in the half-open UTC range [start, end)."""
    s, e = _epoch_min(range_start_utc), _epoch_min(range_end_utc)
    if e <= s:
        raise CalendarError("range end must be after start")
    if e - s > 400000:
        raise CalendarError("range too large (bound: ~277 days of minutes)")
    return sum(1 for m in range(s, e) if is_in_session(cal, m))


def classify_coverage(cal: dict, observed_minute_epochs, range_start_utc: str, range_end_utc: str,
                      verified_no_tick_minutes=None) -> dict:
    """Classify every minute in [start, end): scheduled_closure | in_session_present | verified_no_tick |
    unexplained_missing. ``observed_minute_epochs`` and ``verified_no_tick_minutes`` are iterables of epoch
    MINUTES. Returns counts + the sorted unexplained-missing minutes (bounded). An unexplained gap stays
    visible and drives the quality verdict (PASS iff zero unexplained)."""
    s, e = _epoch_min(range_start_utc), _epoch_min(range_end_utc)
    observed = set(int(x) for x in observed_minute_epochs)
    no_tick = set(int(x) for x in (verified_no_tick_minutes or ()))
    scheduled_closure = in_session_present = verified_no_tick = unexplained = 0
    unexplained_list = []
    for m in range(s, e):
        if not is_in_session(cal, m):
            scheduled_closure += 1
            continue
        if m in observed:
            in_session_present += 1
        elif m in no_tick:
            verified_no_tick += 1
        else:
            unexplained += 1
            if len(unexplained_list) < 64:
                unexplained_list.append(m)
    expected = in_session_present + verified_no_tick + unexplained
    return {
        "expected_bar_count": expected,
        "observed_in_session": in_session_present,
        "scheduled_closure_minutes": scheduled_closure,
        "verified_no_tick_minutes": verified_no_tick,
        "unexplained_missing_minutes": unexplained,
        "unexplained_sample_epoch_min": unexplained_list,
        "quality_verdict": "PASS" if unexplained == 0 else "DEGRADED",
    }
