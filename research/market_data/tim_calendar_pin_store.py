"""Approved trading-session calendar pin store (ADR-0130).

The authority for "when is the market open" must NOT come from the release under inspection — otherwise a
producer could relabel an unexplained gap as a scheduled closure simply by shipping a calendar that says the
market was shut. The consumer therefore resolves the calendar from an APPROVED, out-of-band pin store (the
analogue of the operational signing trust store) and recomputes coverage against THAT calendar. A release
whose bound calendar id/digest is not an approved pin is rejected. Fail-closed, value-blind.
"""
from __future__ import annotations

import json

from . import tim_session_calendar as CAL

PIN_STORE_SCHEMA_VERSION = "1.0"


class CalendarPinError(Exception):
    """A calendar pin store is malformed. Fail-closed: a bad store never yields a silent approval."""


class CalendarPinStore:
    def __init__(self, approved: dict):
        self._approved = approved  # calendar_id -> {"calendar": dict, "calendar_digest": str}

    def approved(self, calendar_id: str):
        """Return {"calendar", "calendar_digest"} for an APPROVED calendar id, else None."""
        return self._approved.get(calendar_id)


def from_dict(d: dict) -> CalendarPinStore:
    if not isinstance(d, dict) or d.get("schema_version") != PIN_STORE_SCHEMA_VERSION:
        raise CalendarPinError("pin store: schema_version must be %r" % PIN_STORE_SCHEMA_VERSION)
    entries = d.get("calendars")
    if not isinstance(entries, list):
        raise CalendarPinError("pin store: calendars must be a list")
    approved = {}
    for e in entries:
        if not isinstance(e, dict) or set(e) != {"calendar_id", "calendar_digest", "status", "calendar"}:
            raise CalendarPinError("pin store: each entry needs {calendar_id, calendar_digest, status, calendar}")
        cal = e["calendar"]
        CAL.validate_calendar(cal)  # fail-closed on a malformed pinned calendar
        dg = CAL.calendar_digest(cal)
        if e["calendar_id"] != cal["calendar_id"]:
            raise CalendarPinError("pin store: entry calendar_id does not match the calendar")
        if e["calendar_digest"] != dg:
            raise CalendarPinError("pin store: entry calendar_digest does not match the calendar bytes")
        if e["status"] not in ("approved", "revoked"):
            raise CalendarPinError("pin store: status must be approved or revoked")
        if e["status"] != "approved":
            continue
        if cal["calendar_id"] in approved:
            raise CalendarPinError("pin store: duplicate approved calendar_id")
        approved[cal["calendar_id"]] = {"calendar": cal, "calendar_digest": dg}
    return CalendarPinStore(approved)


def load(path: str) -> CalendarPinStore:
    with open(path, "r", encoding="utf-8") as fh:
        return from_dict(json.load(fh))
