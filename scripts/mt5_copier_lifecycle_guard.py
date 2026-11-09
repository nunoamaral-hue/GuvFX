"""mt5_copier_lifecycle_guard — the final-boundary pre-state / revision / deadline / idempotency guard for a
copier follower MUTATION (ADR-0084, realising the ADR-0083 bridge-enforcement prerequisite).

This is PURE and importable (no MetaTrader5, no network, no Django): it is the decision logic the MT5 signal
bridge calls, immediately before and after it sends a copier MODIFY_PROTECTION / PARTIAL_CLOSE / FULL_CLOSE,
with the typed command (produced by ``trade_copier.live_execution._live_command_envelope``) and a fresh
follower snapshot (what ``mt5.positions_get(ticket=...)`` returned). It maps every situation to ONE explicit
outcome and NEVER converts an uncertain result into success.

It is DARK: the bridge only consults it when ``COPIER_LIFECYCLE_GUARD_ENABLED`` is on; with the flag off (the
default) the bridge path is byte-for-byte unchanged. The guard authorises NOTHING on its own — it only rejects
or admits a send that every existing gate (identity pin, demo/live wall, authorization) already permitted.
"""
from __future__ import annotations

# ─── explicit outcome vocabulary (packet Phase-2). Stable machine-readable codes. ───
APPLIED = "APPLIED"
ALREADY_APPLIED = "ALREADY_APPLIED"
PRE_STATE_MISMATCH = "PRE_STATE_MISMATCH"
DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
STALE_REVISION = "STALE_REVISION"
POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
BROKER_REJECTED = "BROKER_REJECTED"
UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
CONTAINED_FAILURE = "CONTAINED_FAILURE"
PROCEED = "PROCEED"   # internal: admit the send (not a terminal outcome)

OP_MODIFY = "MODIFY_PROTECTION"
OP_PARTIAL = "PARTIAL_CLOSE"
OP_FULL = "FULL_CLOSE"
_OPS = frozenset({OP_MODIFY, OP_PARTIAL, OP_FULL})


def guard_enabled(getenv=None) -> bool:
    """DARK gate. The bridge consults the guard ONLY when COPIER_LIFECYCLE_GUARD_ENABLED is truthy. Default OFF."""
    import os
    g = getenv or os.getenv
    return str(g("COPIER_LIFECYCLE_GUARD_ENABLED", "") or "").strip().lower() in ("1", "true", "yes", "on")


# ─────────────────────────────── value normalisation (never compare display strings) ───────────────────────────────

def _num(v):
    """Parse a FINITE float, else None. A non-finite value (NaN/Inf) is rejected — it would otherwise defeat
    every ``>=`` / ``<`` comparison (NaN compares False) and fail OPEN at the deadline and reduction gates."""
    import math
    if v is None or isinstance(v, bool):
        return None
    try:
        s = str(v).strip()
        if s == "" or s.lower() == "none":
            return None
        f = float(s)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int_or_none(v):
    """Parse an integer-valued number, else None (fail-closed). Accepts 20 / '20' / 20.0 / '20.0'; rejects
    '20.5', 'abc', None, lists, bools."""
    if isinstance(v, bool):
        return None
    f = _num(v)
    if f is None or f != int(f):
        return None
    return int(f)


def _eq_price(a, b, point) -> bool:
    """Equal to within half a point (normalised broker precision), treating None==None and 0/None as 'unset'."""
    a = _num(a); b = _num(b)
    a_unset = a is None or abs(a) <= 1e-12
    b_unset = b is None or abs(b) <= 1e-12
    if a_unset or b_unset:
        return a_unset and b_unset
    eps = (abs(_num(point)) / 2.0) if _num(point) else 1e-9
    return abs(a - b) <= eps + 1e-12


def _eq_vol(a, b, step) -> bool:
    a = _num(a); b = _num(b)
    if a is None or b is None:
        return a is None and b is None
    eps = (abs(_num(step)) / 2.0) if _num(step) else 1e-9
    return abs(a - b) <= eps + 1e-12


def _deadline_epoch(command):
    """Parse the command's hard deadline (authorization_expires_at) to epoch seconds. None ⇒ no parsable deadline
    ⇒ the caller fails closed (a mutation without a verifiable deadline is never admitted)."""
    v = command.get("authorization_expires_at")
    if v is None:
        return None
    n = _num(v)
    if n is not None:
        return n   # already epoch seconds (finite)
    try:
        from datetime import datetime, timezone
        s = str(v).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)   # a tz-less deadline is UTC, never host-local
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


# ─────────────────────────────── pre-send admission ───────────────────────────────

def admit_mutation(command: dict, snapshot, *, now_epoch: float, last_applied_revision: int = -1,
                   point=None, volume_step=None) -> tuple[str, str]:
    """Decide whether a copier mutation may be SENT. Returns ``(outcome, reason)``.

    ``outcome`` is ``PROCEED`` to send, ``ALREADY_APPLIED`` when the requested post-state already holds (a safe
    no-op success), or a fail-closed terminal outcome (PRE_STATE_MISMATCH / DEADLINE_EXCEEDED / STALE_REVISION /
    POSITION_NOT_FOUND / CONTAINED_FAILURE). ``snapshot`` is the follower's CURRENT position (a mapping with
    exists/symbol/volume/sl/tp/login/server) or ``None``/``{"exists": False}`` when the ticket is absent.

    Fail-closed on any missing/unparseable field. Never admits a send it cannot fully verify."""
    op = str(command.get("copier_operation") or "")
    if op not in _OPS:
        return CONTAINED_FAILURE, "unknown_operation"

    ticket = command.get("ticket")
    if ticket is None:
        return CONTAINED_FAILURE, "command_missing_ticket"

    # Hard deadline — a mutation past its deadline is never sent (no late action at an unpredictable price).
    deadline = _deadline_epoch(command)
    if deadline is None:
        return CONTAINED_FAILURE, "command_missing_deadline"
    now = _num(now_epoch)
    if now is None or now >= float(deadline):
        return DEADLINE_EXCEEDED, "past_authorization_deadline"

    # Monotonic revision — an older master revision must never overwrite a newer applied one. A non-integer /
    # garbage revision fails closed (never raises out of the guard).
    rev_i = _int_or_none(command.get("master_revision"))
    if rev_i is None:
        return CONTAINED_FAILURE, "command_bad_revision"
    last_i = _int_or_none(last_applied_revision)
    if last_i is None:
        last_i = -1
    if rev_i <= last_i:
        return STALE_REVISION, f"revision_{rev_i}_le_applied_{last_i}"

    snap = snapshot or {}
    exists = bool(snap.get("exists", snap.get("ticket") is not None)) if snap else False

    # Position presence.
    if not exists:
        # A FULL close of an absent position is already-done (idempotent); a modify/partial of an absent
        # position cannot proceed.
        if op == OP_FULL:
            return ALREADY_APPLIED, "position_already_closed"
        return POSITION_NOT_FOUND, "follower_ticket_not_open"

    # Identity + symbol + ticket pre-state fingerprint (identity is part of the pre-state). The command MUST
    # carry the identity pin + login + server + symbol for a copier mutation; a command missing ANY of them is
    # malformed and fails closed — the final-boundary fingerprint never silently weakens to an optional field.
    if str(snap.get("ticket")) != str(ticket):
        return PRE_STATE_MISMATCH, "ticket_mismatch"
    exp_login = str(command.get("expected_login") or "").strip()
    exp_server = str(command.get("expected_server") or "").strip()
    exp_symbol = str(command.get("symbol") or (command.get("expected_pre_state") or {}).get("symbol") or "").strip()
    if not (command.get("require_identity_pin") and exp_login and exp_server and exp_symbol):
        return CONTAINED_FAILURE, "command_missing_identity_or_symbol"
    if str(snap.get("login") or "").strip() != exp_login:
        return PRE_STATE_MISMATCH, "login_mismatch"
    if str(snap.get("server") or "").strip() != exp_server:
        return PRE_STATE_MISMATCH, "server_mismatch"
    if str(snap.get("symbol") or "").strip() != exp_symbol:
        return PRE_STATE_MISMATCH, "symbol_mismatch"

    pre = command.get("expected_pre_state") or {}
    post = command.get("requested_post_state") or {}

    if op == OP_MODIFY:
        tgt_sl = post.get("sl")
        tgt_tp = post.get("tp")
        # A copier LIVE modify always carries a complete desired SL+TP (the backend requires it). A command with
        # NO numeric target is malformed — never admit it: it would strip the follower's protection, or falsely
        # report already-applied against an unprotected position (a no-op that silently skips a real modify).
        if _num(tgt_sl) is None and _num(tgt_tp) is None:
            return CONTAINED_FAILURE, "modify_missing_targets"
        # Idempotency: the follower already carries the requested SL/TP ⇒ already applied.
        if _eq_price(snap.get("sl"), tgt_sl, point) and _eq_price(snap.get("tp"), tgt_tp, point):
            return ALREADY_APPLIED, "sltp_already_set"
        return PROCEED, "ok"

    if op == OP_PARTIAL:
        exp_cur = pre.get("expected_current_volume")
        remaining = post.get("expected_remaining")
        reduce_by = post.get("reduce_by")
        if reduce_by is None or remaining is None:
            return CONTAINED_FAILURE, "command_missing_partial_volumes"
        # Idempotency: the follower is already at the expected remaining ⇒ the partial already happened.
        if _eq_vol(snap.get("volume"), remaining, volume_step):
            return ALREADY_APPLIED, "already_reduced"
        # Pre-state: the follower volume must equal the volume the reduction was computed against.
        if exp_cur is not None and not _eq_vol(snap.get("volume"), exp_cur, volume_step):
            return PRE_STATE_MISMATCH, "volume_pre_state_mismatch"
        # A reduction must strictly reduce and never exceed the current volume (defence in depth vs the backend).
        cur = _num(snap.get("volume"))
        rb = _num(reduce_by)
        if cur is None or rb is None or rb <= 0 or rb >= cur:
            return PRE_STATE_MISMATCH, "reduction_not_strictly_reducing"
        return PROCEED, "ok"

    # OP_FULL — the position exists; proceed to close it.
    return PROCEED, "ok"


# ─────────────────────────────── post-send outcome classification ───────────────────────────────

def classify_result(command: dict, *, send_determinate: bool, broker_rejected: bool, post_snapshot,
                    point=None, volume_step=None) -> tuple[str, str]:
    """Map a send result + a FRESH post-send follower snapshot to an explicit outcome. ``(outcome, reason)``.

    ``send_determinate`` is True only when the bridge has a definite broker response (retcode). When it is
    False (timeout, lost connection, ``None`` result, or no post-snapshot) the outcome is UNKNOWN_OUTCOME — it
    is NEVER inferred as success and requires reconciliation. ``broker_rejected`` marks a definitive broker
    refusal. On a determinate success, the post-state is re-verified; a contradiction is CONTAINED_FAILURE."""
    op = str(command.get("copier_operation") or "")
    if op not in _OPS:
        return CONTAINED_FAILURE, "unknown_operation"

    if post_snapshot is None:
        return UNKNOWN_OUTCOME, "no_post_snapshot"
    if not send_determinate:
        return UNKNOWN_OUTCOME, "indeterminate_send"
    if broker_rejected:
        return BROKER_REJECTED, "broker_rejected"

    snap = post_snapshot or {}
    exists = bool(snap.get("exists", snap.get("ticket") is not None)) if snap else False
    post = command.get("requested_post_state") or {}

    if op == OP_FULL:
        return (APPLIED, "closed") if not exists else (CONTAINED_FAILURE, "close_not_reflected")
    if op == OP_MODIFY:
        if not exists:
            return CONTAINED_FAILURE, "position_vanished_after_modify"
        if _eq_price(snap.get("sl"), post.get("sl"), point) and _eq_price(snap.get("tp"), post.get("tp"), point):
            return APPLIED, "sltp_set"
        return CONTAINED_FAILURE, "sltp_not_reflected"
    # OP_PARTIAL
    if not exists:
        # The whole position is gone after a PARTIAL — ambiguous (over-close / external close): contain.
        return CONTAINED_FAILURE, "position_gone_after_partial"
    if _eq_vol(snap.get("volume"), post.get("expected_remaining"), volume_step):
        return APPLIED, "reduced"
    return CONTAINED_FAILURE, "remaining_volume_mismatch"
