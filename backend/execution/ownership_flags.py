"""Phase A — durable strategy ownership: DARK feature flags.

Four INDEPENDENT flags, ALL default OFF (fail-closed), so the whole Phase-A
ownership foundation ships DARK and byte-identical to today until an operator
explicitly enables a stage. Separate flags give granular, independent rollback:

  * STRATEGY_OWNERSHIP_DUAL_WRITE_ENABLED — write the new ownership fields
    (SignalExecutionPlan.strategy_assignment, ExecutionJob.assignment,
    Trade.strategy_assignment). Reads are UNCHANGED. Pure additive write.
  * STRATEGY_MAGIC_SEND_ENABLED — include ``magic`` in the live order payload
    (MONEY-PATH: changes what is sent to the broker). Requires dual-write to be
    meaningful (the magic comes from the plan's assignment).
  * STRATEGY_OWNERSHIP_READ_ENABLED — prefer the magic/FK ownership for
    attribution reads. NOT used by this packet (A5).
  * STRATEGY_OWNERSHIP_ENFORCE_ENABLED — fail-closed owner-scoping in
    close/modify/protection. NOT activated by this packet (A6); the guarded
    checks fail OPEN to today's comment/plan correlation while this is OFF.

Precedence mirrors ``auto_router._multi_account_routing_enabled``: an explicit
Django setting wins; otherwise the env var; otherwise False. The two money-path
flags (magic-send, enforce) emit a one-time WARN the first time they read ON so
enabling them always leaves a durable trace, even when set via the environment.
"""
from __future__ import annotations

import logging
import math
import os

from django.conf import settings

logger = logging.getLogger(__name__)

_TRUE = ("1", "true", "yes", "on")

_WARNED: set[str] = set()


def _flag(name: str, *, warn_on: bool = False) -> bool:
    """Read a DARK flag. Django setting wins, else env, else False (fail-closed).

    ``warn_on`` emits a one-time WARN the first time the flag reads ON (used for
    the money-path flags), so a Class-B enablement always leaves a log trace.
    """
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, "")
    on = val if isinstance(val, bool) else str(val).strip().lower() in _TRUE
    if on and warn_on and name not in _WARNED:
        _WARNED.add(name)
        logger.warning(
            "%s is ON — this is a Phase-A money-path enablement; confirm it is intended.", name)
    return on


def dual_write_enabled() -> bool:
    """A2 — write the new ownership fields. Reads unchanged. Additive, low risk."""
    return _flag("STRATEGY_OWNERSHIP_DUAL_WRITE_ENABLED")


def magic_send_enabled() -> bool:
    """A2 §8 — include ``magic`` in the live order payload (MONEY-PATH)."""
    return _flag("STRATEGY_MAGIC_SEND_ENABLED", warn_on=True)


def ownership_read_enabled() -> bool:
    """A5 — prefer magic/FK for attribution reads. Not used by this packet."""
    return _flag("STRATEGY_OWNERSHIP_READ_ENABLED")


def ownership_enforce_enabled() -> bool:
    """A6 — fail-closed owner-scoping in close/modify/protection (MONEY-PATH)."""
    return _flag("STRATEGY_OWNERSHIP_ENFORCE_ENABLED", warn_on=True)


# Upper bound for the sweep look-back (one year). A value above this is treated as a
# fat-finger and degrades to the default: an over-range window would overflow
# ``timedelta`` (~2.4e10 h) and raise on every monitor tick, and a genuinely long
# look-back is the explicit ``backfill_execution_ownership`` command's job, not the sweep's.
_MAX_SWEEP_WINDOW_HOURS = 8784.0


def ownership_sweep_window_hours() -> float:
    """Rolling look-back (hours) bounding the monitor-chain ownership sweep on
    ``Trade.created_at`` (ingestion time). Setting wins, else env, else default (72h).

    Forward-safety: the sweep only stamps trades INGESTED within this window, so enabling
    (or re-enabling) DUAL_WRITE can never implicitly walk the whole back-catalogue — that
    is the explicit ``backfill_execution_ownership`` command's job. Precedence mirrors
    ``_flag`` (Django setting → env → default). Any value that is not a finite number in
    ``(0, _MAX_SWEEP_WINDOW_HOURS]`` — unparseable, non-positive, NaN/inf, or absurdly
    large enough to overflow ``timedelta`` — degrades to the default; the window is never
    unbounded (a ``=0`` "off" typo, or an ``inf``, must not reopen a full-history walk or
    crash the sweep)."""
    val = getattr(settings, "STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS", None)
    if val is None:
        val = os.getenv("STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS", "")
    try:
        hours = float(val)
    except (TypeError, ValueError):
        return 72.0
    if not math.isfinite(hours) or not (0 < hours <= _MAX_SWEEP_WINDOW_HOURS):
        return 72.0
    return hours
