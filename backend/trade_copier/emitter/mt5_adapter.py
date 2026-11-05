"""trade_copier.emitter.mt5_adapter — a NARROW read-only MT5 position/identity adapter (ADR-0076). Django-free.

The emitter obtains position state ONLY through the host bridge's existing supported READ-ONLY endpoints
(mirroring the ``execution.snapshot_transport`` contract): ``GET /mt5/positions`` and, for identity without a
positions read, ``GET /mt5/snapshots/account`` — presenting the bridge's read-only agent token. There is NO
MetaTrader/broker credential in the emitter, and **no order/modify/close/cancel method exists on this
interface** (asserted by the tests). Missing SL/TP are preserved as ``None`` (the ``0.0 == no level`` rule),
never invented as zeros; an unavailable/failed read returns a structured reason code, never a fabricated
snapshot. The position's broker ``open_time`` is read from the per-position ``time`` field (the honest age
reference for no-late-OPEN).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from trade_copier.bridge_emitter import ObservedMasterPosition


@dataclass(frozen=True)
class SnapshotResult:
    ok: bool
    reason: str = "ok"
    account_login: str = ""
    account_server: str = ""
    trade_mode: Optional[int] = None
    positions: tuple = field(default_factory=tuple)   # tuple[ObservedMasterPosition]


@dataclass(frozen=True)
class IdentityResult:
    ok: bool
    reason: str = "ok"
    account_login: str = ""
    account_server: str = ""
    trade_mode: Optional[int] = None


class PositionSnapshotAdapter:
    """Read-only adapter contract. Implementations MUST expose only these two reads and NO mutation method."""

    def read_positions(self) -> SnapshotResult:
        raise NotImplementedError

    def read_identity(self) -> IdentityResult:
        raise NotImplementedError


def _level(v):
    """A protective level: None stays None; 0.0 (MT5 'no level') → None; else float. Never invents a zero."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if abs(f) <= 1e-9 else f


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dt_from_epoch(v):
    """Broker open time (epoch seconds) → aware datetime. Unparseable/absent → None (never fabricated)."""
    if v in (None, "", 0, 0.0):
        return None
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _default_opener(req, timeout):
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed internal loopback scheme)
        return r.read().decode("utf-8", "ignore")


class BridgeHttpAdapter(PositionSnapshotAdapter):
    """The production host adapter: GETs the LOCAL bridge's read-only endpoints. No write/order capability.

    ``token`` is the bridge's read-only agent token (resolved by the caller from an env var, never logged).
    ``opener`` is injectable so tests never touch the network."""

    def __init__(self, *, base_url: str, token: str, timeout: int = 10, opener=None):
        self.base_url = str(base_url or "").rstrip("/")
        self._token = token or ""
        self.timeout = int(timeout)
        self._opener = opener or _default_opener

    def _get(self, path: str):
        req = urllib.request.Request(self.base_url + path, method="GET",
                                     headers={"X-GuvFX-Agent-Token": self._token})
        raw = self._opener(req, self.timeout)
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else None

    def read_identity(self) -> IdentityResult:
        """Observed live identity WITHOUT reading positions (for attestation): GET /mt5/snapshots/account."""
        try:
            d = self._get("/mt5/snapshots/account")
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return IdentityResult(False, "identity_read_error")
        if not (isinstance(d, dict) and d.get("ok")):
            return IdentityResult(False, "identity_not_ok")
        return IdentityResult(True, "ok", str(d.get("account_login") or ""),
                              str(d.get("account_server") or ""),
                              (int(d["trade_mode"]) if d.get("trade_mode") is not None else None))

    def read_positions(self) -> SnapshotResult:
        try:
            d = self._get("/mt5/positions")
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return SnapshotResult(False, "positions_read_error")
        if not (isinstance(d, dict) and d.get("ok")):
            return SnapshotResult(False, "positions_not_ok")
        raw = d.get("positions")
        if not isinstance(raw, list):
            # ok:true + missing/non-list positions is ambiguous, NOT a confirmed-flat account → fail closed.
            return SnapshotResult(False, "positions_not_a_list")
        out = []
        seen: set = set()
        for p in raw:
            if not (isinstance(p, dict) and p.get("ticket") is not None):
                return SnapshotResult(False, "malformed_position")
            try:
                ticket = int(p["ticket"])
            except (TypeError, ValueError):
                return SnapshotResult(False, "malformed_position")
            if ticket in seen:
                return SnapshotResult(False, "duplicate_ticket")
            seen.add(ticket)
            side = p.get("side")
            if side not in ("BUY", "SELL"):
                side = "BUY" if p.get("type") in (0, "0") else "SELL"
            out.append(ObservedMasterPosition(
                ticket=ticket, symbol=str(p.get("symbol") or ""), side=str(side),
                volume=float(p.get("volume") or 0.0), sl=_level(p.get("sl")), tp=_level(p.get("tp")),
                price_open=_num(p.get("price_open")), open_time=_dt_from_epoch(p.get("time"))))
        return SnapshotResult(True, "ok", str(d.get("account_login") or ""),
                              str(d.get("account_server") or ""),
                              (int(d["trade_mode"]) if d.get("trade_mode") is not None else None),
                              tuple(out))


class FakeSnapshotAdapter(PositionSnapshotAdapter):
    """Synthetic adapter for tests — scripted responses only. No network, no order capability."""

    def __init__(self, *, positions=(), identity: Optional[IdentityResult] = None,
                 positions_fail: Optional[str] = None, account_login: str = "", account_server: str = "",
                 trade_mode: Optional[int] = None):
        self._positions = tuple(positions)
        self._identity = identity
        self._positions_fail = positions_fail
        self._login = account_login
        self._server = account_server
        self._trade_mode = trade_mode

    def set_positions(self, positions, *, fail: Optional[str] = None):
        self._positions = tuple(positions)
        self._positions_fail = fail

    def read_positions(self) -> SnapshotResult:
        if self._positions_fail:
            return SnapshotResult(False, self._positions_fail)
        return SnapshotResult(True, "ok", self._login, self._server, self._trade_mode, self._positions)

    def read_identity(self) -> IdentityResult:
        if self._identity is not None:
            return self._identity
        return IdentityResult(True, "ok", self._login, self._server, self._trade_mode)
