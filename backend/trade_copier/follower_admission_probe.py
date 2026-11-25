"""trade_copier.follower_admission_probe — the fail-closed #48 follower admission / identity / flatness probe for
the governed #46->#48 owner-demo arm (GFX-COPIER-OWNER-DEMO-ASYM-VERIFIER-172, ADR-0129).

WHY THIS EXISTS
---------------
ADR-0128 left ``host_admission_probe`` = None because there was no value-blind, authoritative read-back of the
follower #48 host posture. The atomic arm must, before engaging, have FRESH, AUTHORITATIVE evidence that the
intended follower identity is correct, its live admission (``MT5_ALLOW_LIVE``) is OFF, and it is FLAT (no open
positions). This module evaluates exactly that from an INJECTED read-only evidence provider and FAILS CLOSED on
stale, ambiguous, mismatched, unavailable or non-flat results.

CONTRACT
--------
``evaluate_follower_admission(reader, ...)`` calls the injected ``reader()`` (a reviewed, read-only interface — a
signed host read in production; a synthetic dict in tests) and returns a value-blind verdict
``{"ok", "off", "identity_ok", "flat", "reason", ...}``. ``off`` is True ONLY when EVERY gate passes, so a single
``host_admission_probe`` returning ``{"off": verdict["off"]}`` fails the arm's HOST_ADMISSION entry gate unless the
follower is provably the right identity, flat, and admission-off on fresh evidence.

PRODUCTION STATE: DARK. The default production reader reports UNAVAILABLE (the signed read-only #48 host interface
is a separate, Sponsor-gated host rollout — ADR-0129 "Integration gap"), so the probe fails closed and the owner
demo stays unarmable. The probe is exercised only through injected synthetic readers in tests.

The verdict is value-blind: booleans, counts and reason codes only — never a login, balance, key, or other-tenant
datum. The expected identity is compared, never echoed.
"""
from __future__ import annotations

from typing import Callable, Optional

# Stable, value-blind reason codes.
R_VERIFIED = "verified"
R_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
R_STALE = "EVIDENCE_STALE"
R_AMBIGUOUS = "EVIDENCE_AMBIGUOUS"
R_IDENTITY_MISMATCH = "FOLLOWER_IDENTITY_MISMATCH"
R_NON_FLAT = "FOLLOWER_NOT_FLAT"
R_ADMISSION_NOT_OFF = "ADMISSION_NOT_OFF"

DEFAULT_MAX_AGE_S = 60   # evidence older than this is STALE (the follower posture must be freshly observed)


def _blocked(reason: str, **extra) -> dict:
    d = {"ok": False, "off": False, "identity_ok": False, "flat": False, "reason": reason}
    d.update(extra)
    return d


def evaluate_follower_admission(
    reader: Optional[Callable[[], dict]],
    *,
    expected_follower_account_id: int,
    expected_identity: str,
    now_epoch: int,
    max_age_s: int = DEFAULT_MAX_AGE_S,
) -> dict:
    """Evaluate the follower #48 admission posture from a read-only ``reader()`` and return a value-blind,
    fail-closed verdict. ``reader()`` is expected to return a dict shaped like::

        {"ok": True, "follower_account_id": 48, "follower_identity": "<login@server>",
         "admission_off": True, "flat": True, "open_positions": 0, "observed_at_epoch": <int>, "source": "..."}

    Any missing/False/ambiguous field, stale evidence, an identity mismatch, a non-flat follower, or admission that
    is not provably OFF ⇒ a BLOCKED verdict (``off`` False). The `reader` makes NO mutation and NO order action."""
    if reader is None:
        return _blocked(R_UNAVAILABLE)
    try:
        ev = reader()
    except Exception:  # noqa: BLE001 — a reader error is fail-closed (unavailable), never a crash
        return _blocked(R_UNAVAILABLE, detail="reader_error")
    if not isinstance(ev, dict) or not ev.get("ok"):
        return _blocked(R_UNAVAILABLE)

    # Freshness — the posture must be observed within the window (no stale/absent timestamp).
    observed = ev.get("observed_at_epoch")
    try:
        observed_i = int(observed)
    except (TypeError, ValueError):
        return _blocked(R_STALE, detail="no_observed_at")
    age = int(now_epoch) - observed_i
    if observed_i <= 0 or age < 0 or age > int(max_age_s):
        return _blocked(R_STALE, observed_age_s=age)

    # Identity — the evidence must name EXACTLY the intended follower account AND identity (no ambiguity).
    fid = ev.get("follower_account_id")
    ident = ev.get("follower_identity")
    if fid is None or ident is None or str(ident).strip() == "":
        return _blocked(R_AMBIGUOUS)
    try:
        if int(fid) != int(expected_follower_account_id):
            return _blocked(R_IDENTITY_MISMATCH)
    except (TypeError, ValueError):
        return _blocked(R_AMBIGUOUS)
    if str(ident) != str(expected_identity):
        return _blocked(R_IDENTITY_MISMATCH)

    # Flatness — require an explicit, unambiguous flat reading (no open positions). An absent/None position count
    # is AMBIGUOUS, not "assumed flat".
    open_positions = ev.get("open_positions")
    flat_flag = ev.get("flat")
    if open_positions is None or flat_flag is None:
        return _blocked(R_AMBIGUOUS, detail="flatness_unknown")
    try:
        if int(open_positions) != 0 or not bool(flat_flag):
            return _blocked(R_NON_FLAT, open_positions=int(open_positions))
    except (TypeError, ValueError):
        return _blocked(R_AMBIGUOUS, detail="flatness_unparseable")

    # Admission — must be provably OFF before we may arm.
    if ev.get("admission_off") is not True:
        return _blocked(R_ADMISSION_NOT_OFF)

    return {"ok": True, "off": True, "identity_ok": True, "flat": True, "reason": R_VERIFIED,
            "observed_age_s": age}


def production_follower_reader(follower_account_id: int, *, executor_resolver: Optional[Callable] = None):  # noqa: ARG001
    """Return the PRODUCTION read-only evidence reader for follower #48. In the repository-only / DARK phase the
    reviewed signed read-only #48 host interface is NOT yet available, so this returns a reader that reports
    EVIDENCE_UNAVAILABLE (fail closed) — wiring the real signed read is a separate, Sponsor-gated host rollout
    (ADR-0129 "Integration gap"). Never contacts the host here."""
    def _reader() -> dict:
        return {"ok": False, "reason": R_UNAVAILABLE, "source": "unprovisioned"}
    return _reader
