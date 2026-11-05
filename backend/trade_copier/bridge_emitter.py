"""trade_copier.bridge_emitter — the repository-side host bridge EMITTER (ADR-0074 Phase-1).

The fast half's producer: the host-side component that OBSERVES a master's normalised open-position state and
emits SIGNED, account-scoped envelopes to the backend's event-ingestion endpoint
(``trade_copier.event_ingestion`` / ``/api/copy-trading/event-ingest/``). It is the counterpart of the verifier
and uses the SAME pure signing construction, so an envelope it builds verifies there byte-for-byte.

Repository-only / DARK: this module is implemented and tested in the repo but is NOT installed on Windows and
NOT enabled. It is default OFF (``enabled=False`` unless constructed as enabled) and performs NO broker WRITES —
it only READS a snapshot it is handed (the host passes in observed positions; the emitter never attaches,
orders, modifies or closes). It never retrieves or logs a secret: the HMAC key is supplied by an injected
``key_provider`` (a synthetic key in tests; the host's own fail-closed env resolver in production), and the
signature/secret/full payload are never logged.

Durability & safety contract:

* **Monotonic per-ticket revisions.** Each ticket's ``revision`` starts at 1 on its first emission and strictly
  increases; a re-observed-identical state emits nothing (the pure ``diff`` produces no change), so revisions
  are never burned on noise.
* **Explicit missing SL/TP preserved.** ``sl``/``tp`` are carried verbatim; ``None`` means "no level" (same
  ``0.0 == no level`` rule as the poll path), so an SL/TP REMOVAL is represented, never silently dropped.
* **Open / modify / partial close / full close.** One revision per changed ticket carries the COMPLETE current
  state (``closed=True`` for a full close); the backend fold+overlay classifies the change type.
* **Bounded, durable spool + retry.** Unacked envelopes live in a bounded durable spool; ``flush`` retries up
  to ``max_attempts`` then drops to a recorded dead-letter (never an unbounded queue, never a silent infinite
  retry). The authoritative reconciliation poll remains the correctness backstop for a dropped envelope.
* **Deduplicate safely.** A spooled revision ``(ticket, revision)`` is assigned once and never regenerated; the
  backend further dedups by the unique ``(master, ticket, revision)`` and the single-use nonce, so an at-least-
  once retry is idempotent.
* **Never extend an entry deadline.** A revision's ``broker_event_time`` (the true change time) is stamped ONCE
  at observation and is IMMUTABLE across every retry/re-sign. A retry re-signs only the transport envelope
  (fresh nonce + timestamp so the endpoint's skew/expiry pass); the payload — including ``broker_event_time`` —
  is unchanged, so the backend's admissibility deadline (measured from ``broker_event_time``) can never be
  reset by a late re-send. A buffered OPEN stays late and is ruled ``ENTRY_FAILED_FINAL`` at the backend.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from trade_copier import event_ingestion as ei
from trade_copier.diff import ObservedPosition, diff_positions

logger = logging.getLogger(__name__)

DEFAULT_MAX_PENDING = 1000          # bounded spool depth (backpressure recorded, never unbounded)
DEFAULT_MAX_ATTEMPTS = 5            # bounded retries before dead-letter (never an infinite retry)
DEFAULT_TTL_SECONDS = 30            # signed-envelope validity window (<= backend MAX_EXPIRY_WINDOW)


# ─────────────────────────────── observed input (host-supplied; no broker read here) ───────────────────────────────

@dataclass(frozen=True)
class ObservedMasterPosition:
    """One open position the host observed for the master. ``sl``/``tp`` None == no level (preserved verbatim).

    ``open_time`` is the position's broker OPEN time — a core field of an MT5 position (``positions_get`` exposes
    it), and the HONEST age reference for OPEN admissibility: the host MUST supply it so a buffered/late OPEN is
    ruled inadmissible by its true age, never admitted off the observation wall-clock. ``broker_event_time`` is
    the broker's own per-change time, available only when the host has a deals/history feed (the positions feed
    does NOT expose it); it is left None otherwise — NEVER fabricated to 'now' (that would reset an entry
    deadline)."""
    ticket: int
    symbol: str
    side: str
    volume: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    price_open: Optional[float] = None
    original_volume: Optional[float] = None
    open_time: Optional[datetime] = None
    broker_event_time: Optional[datetime] = None

    def to_observed(self) -> ObservedPosition:
        return ObservedPosition(ticket=int(self.ticket), symbol=str(self.symbol or ""),
                                side=str(self.side or ""), volume=float(self.volume or 0.0),
                                sl=self.sl, tp=self.tp)


# ─────────────────────────────── durable spool (injectable; file on host, memory in tests) ───────────────────────────────

@dataclass
class _SpoolState:
    per_ticket_revision: dict = field(default_factory=dict)   # ticket(str) -> last revision assigned
    last_snapshot: dict = field(default_factory=dict)         # ticket(str) -> normalised position dict
    pending: list = field(default_factory=list)               # unacked batches [{batch_id, revisions, attempts}]
    dropped: int = 0                                          # dead-lettered batch count (recorded, not silent)


class MemorySpool:
    """In-process durable-enough spool for tests (survives emitter re-construction only if the SAME instance is
    reused; use ``FileSpool`` to exercise a true process restart)."""
    def __init__(self):
        self._raw = _serialize(_SpoolState())

    def load(self) -> _SpoolState:
        return _deserialize(self._raw)

    def save(self, state: _SpoolState) -> None:
        self._raw = _serialize(state)


class FileSpool:
    """Durable JSON-file spool for the host. An atomic replace (tmp + ``os.replace``) so a crash never leaves a
    half-written spool; a fresh emitter constructed from the same path resumes exactly (restart/replay safe)."""
    def __init__(self, path: str):
        self.path = path

    def load(self) -> _SpoolState:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return _deserialize(fh.read())
        except (FileNotFoundError, ValueError, OSError):
            return _SpoolState()

    def save(self, state: _SpoolState) -> None:
        tmp = f"{self.path}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(_serialize(state))
        os.replace(tmp, self.path)


def _serialize(state: _SpoolState) -> str:
    return json.dumps({"per_ticket_revision": state.per_ticket_revision, "last_snapshot": state.last_snapshot,
                       "pending": state.pending, "dropped": state.dropped}, sort_keys=True)


def _deserialize(raw: str) -> _SpoolState:
    d = json.loads(raw) if raw else {}
    return _SpoolState(per_ticket_revision=dict(d.get("per_ticket_revision") or {}),
                       last_snapshot=dict(d.get("last_snapshot") or {}),
                       pending=list(d.get("pending") or []), dropped=int(d.get("dropped") or 0))


# ─────────────────────────────── transport (injectable; fake in tests, HTTP on host) ───────────────────────────────

@dataclass(frozen=True)
class SendResult:
    ok: bool
    status: int = 0
    reason: str = ""


# A transport takes a signed envelope dict and returns a SendResult. It NEVER sees or logs the secret.
Transport = Callable[[dict], SendResult]


def null_transport(envelope: dict) -> SendResult:
    """The DARK/default transport: sends NOWHERE (no network). Used until a real destination is configured."""
    return SendResult(ok=False, status=0, reason="no_transport_configured")


# ─────────────────────────────── the emitter ───────────────────────────────

@dataclass(frozen=True)
class EmitResult:
    changed_tickets: tuple
    revisions_emitted: int
    batch_id: str = ""


class BridgeEmitter:
    """Observe-and-emit for ONE master account. Default OFF. No broker write; no secret logged."""

    def __init__(self, *, account_id: int, spool, key_provider: Callable[[], str],
                 transport: Transport = null_transport, enabled: bool = False,
                 broker_login: str = "", broker_server: str = "",
                 clock: Callable[[], float] = None, now_dt: Callable[[], datetime] = None,
                 max_pending: int = DEFAULT_MAX_PENDING, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS, key_id: str = ei.DEFAULT_KEY_ID):
        self.account_id = int(account_id)
        self.spool = spool
        self.key_provider = key_provider
        self.transport = transport
        self.enabled = bool(enabled)
        self.broker_login = str(broker_login or "")
        self.broker_server = str(broker_server or "")
        self._clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self._now_dt = now_dt or (lambda: datetime.now(timezone.utc))
        self.max_pending = int(max_pending)
        self.max_attempts = int(max_attempts)
        self.ttl_seconds = int(ttl_seconds)
        self.key_id = str(key_id)

    # -- observation → spool (no network) --------------------------------------------------------------
    def observe(self, positions) -> EmitResult:
        """Diff the handed snapshot against the last observed one, assign monotonic per-ticket revisions for the
        changed tickets, and enqueue ONE durable batch. No network, no broker write. Inert when disabled."""
        if not self.enabled:
            return EmitResult(changed_tickets=(), revisions_emitted=0)
        state = self.spool.load()
        current = {int(p.ticket): p for p in positions}
        prev_positions = [_obs_from_dict(t, d) for t, d in state.last_snapshot.items()]
        new_positions = [p.to_observed() for p in current.values()]
        specs = diff_positions(prev_positions, new_positions)
        changed = sorted({int(s.ticket) for s in specs})
        if not changed:
            # Still persist the (unchanged) snapshot baseline so a first observe records it.
            state.last_snapshot = {str(t): _dict_from_obs(p) for t, p in current.items()}
            self.spool.save(state)
            return EmitResult(changed_tickets=(), revisions_emitted=0)

        now_dt = self._now_dt()
        revisions = []
        for ticket in changed:
            rev_no = int(state.per_ticket_revision.get(str(ticket), 0)) + 1
            state.per_ticket_revision[str(ticket)] = rev_no
            if ticket in current:
                p = current[ticket]
                # broker_event_time is carried ONLY when the host genuinely has one (deals feed); it is NEVER
                # fabricated to 'now'. OPEN admissibility uses the position's true open_time instead.
                revisions.append(_revision_payload(
                    self.account_id, ticket, rev_no, now_dt, p.broker_event_time, closed=False, pos=p,
                    broker_login=self.broker_login, broker_server=self.broker_server))
            else:
                revisions.append(_revision_payload(
                    self.account_id, ticket, rev_no, now_dt, None, closed=True, pos=None,
                    broker_login=self.broker_login, broker_server=self.broker_server))

        batch = {"batch_id": uuid.uuid4().hex, "revisions": revisions, "attempts": 0}
        if len(state.pending) >= self.max_pending:
            # Bounded backpressure: never grow unbounded. Record the drop; the authoritative poll corrects it.
            state.dropped += 1
            logger.warning("copier bridge emitter spool full (account=%s) — batch dropped to dead-letter",
                           self.account_id)
        else:
            state.pending.append(batch)
        state.last_snapshot = {str(t): _dict_from_obs(p) for t, p in current.items()}
        self.spool.save(state)
        return EmitResult(changed_tickets=tuple(changed), revisions_emitted=len(revisions),
                          batch_id=batch["batch_id"])

    # -- spool → transport (at-least-once, bounded retry) ----------------------------------------------
    def flush(self) -> dict:
        """(Re)send pending batches through the transport. Idempotent/at-least-once: the backend dedups by the
        single-use nonce + unique (ticket, revision). A re-sign uses a FRESH nonce + timestamp (so skew/expiry
        pass) but NEVER rewrites a revision's ``broker_event_time`` — a retry cannot extend an entry deadline.
        After ``max_attempts`` a batch is dead-lettered (recorded). Inert when disabled."""
        if not self.enabled:
            return {"enabled": False, "sent": 0, "pending": 0, "dropped": 0}
        secret = self.key_provider()
        if not secret:
            return {"enabled": True, "sent": 0, "pending": len(self.spool.load().pending), "reason": "no_key"}
        state = self.spool.load()
        sent = 0
        still_pending = []
        for batch in state.pending:
            envelope = self._sign_batch(batch["revisions"], secret)
            result = _safe_send(self.transport, envelope)
            if result.ok:
                sent += 1
                continue
            batch["attempts"] = int(batch.get("attempts", 0)) + 1
            if batch["attempts"] >= self.max_attempts:
                state.dropped += 1
                logger.warning("copier bridge emitter batch dead-lettered after %s attempts (account=%s): %s",
                               batch["attempts"], self.account_id, result.reason)
                continue
            still_pending.append(batch)
        state.pending = still_pending
        self.spool.save(state)
        return {"enabled": True, "sent": sent, "pending": len(still_pending), "dropped": state.dropped}

    def _sign_batch(self, revisions: list, secret: str) -> dict:
        payload = {"revisions": revisions}
        return ei.sign_ingest_request(account_id=self.account_id, secret=secret, nonce=uuid.uuid4().hex,
                                      now=self._clock(), payload=payload, key_id=self.key_id,
                                      ttl_seconds=self.ttl_seconds)


# ─────────────────────────────── helpers ───────────────────────────────

def _revision_payload(account_id, ticket, revision, observed_dt, broker_event_dt, *, closed, pos,
                      broker_login, broker_server) -> dict:
    r = {
        "account_id": int(account_id), "ticket": int(ticket), "revision": int(revision),
        "observed_time": observed_dt.isoformat(),
        "closed": bool(closed), "broker_login": broker_login, "broker_server": broker_server,
    }
    if broker_event_dt is not None:   # carried only when genuinely known; never fabricated to 'now'
        r["broker_event_time"] = broker_event_dt.isoformat()
    if not closed and pos is not None:
        r.update({"symbol": pos.symbol, "side": pos.side, "volume": float(pos.volume),
                  "sl": (None if pos.sl is None else float(pos.sl)),
                  "tp": (None if pos.tp is None else float(pos.tp)),
                  "price_open": (None if pos.price_open is None else float(pos.price_open)),
                  "original_volume": (None if pos.original_volume is None else float(pos.original_volume))})
        if pos.open_time is not None:   # the OPEN's true broker age reference (no-late-OPEN guarantee)
            r["open_time"] = pos.open_time.isoformat()
    return r


def _obs_from_dict(ticket, d: dict) -> ObservedPosition:
    return ObservedPosition(ticket=int(ticket), symbol=d.get("symbol", ""), side=d.get("side", ""),
                            volume=float(d.get("volume") or 0.0), sl=d.get("sl"), tp=d.get("tp"))


def _dict_from_obs(p: ObservedMasterPosition) -> dict:
    return {"symbol": p.symbol, "side": p.side, "volume": float(p.volume),
            "sl": (None if p.sl is None else float(p.sl)), "tp": (None if p.tp is None else float(p.tp))}


def _safe_send(transport: Transport, envelope: dict) -> SendResult:
    try:
        out = transport(envelope)
        return out if isinstance(out, SendResult) else SendResult(ok=bool(out))
    except Exception as e:  # noqa: BLE001 — a transport fault is a retryable send failure, never a crash
        return SendResult(ok=False, status=0, reason=type(e).__name__)


def resolve_host_key() -> str:
    """Host-side key resolver (fail closed) — the emitter's OWN dedicated secret, NEVER ``GUVFX_AGENT_TOKEN``.

    Standalone host services cannot import ``core.credentials`` (security.md RULE 3), so the emitter reads its
    own env following the SAME contract: a missing/short key yields an empty string → the emitter sends nothing
    (never a silent fallback to a different secret). The value is never logged."""
    for name in ("COPIER_INGEST_HMAC_KEY", "COPIER_INGEST_HMAC_SECRET"):
        val = (os.getenv(name) or "").strip()
        if len(val) >= 32 and "xxx" not in val.lower():
            return val
    return ""
