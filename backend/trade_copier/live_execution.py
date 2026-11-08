"""trade_copier.live_execution — the ONLY path that admits a LIVE (non-demo) follower OPEN (ADR-0057).

The copier is DEMO-followers-only everywhere else. For an OPEN, ``execution_producer.admit_open`` now
delegates a non-demo follower HERE instead of the blanket ``follower_not_demo`` reject. ``follower_not_demo``
is therefore NOT weakened globally — it is replaced, for OPEN only, by one of two complete admissions:
the existing DEMO admission (demo follower) or this complete per-pair LIVE-copier authorization admission.

Every gate is independent and fail-closed; any absence, drift, expiry, exhausted allowance, stale/pre-watermark
event, or error denies. There is NO broad "allow live" path: a LIVE order requires a human-created
``CopierLiveAuthorization`` for the EXACT (user, master, follower, source→follower symbol), every identity
pinned, within the window and bounded order allowance, with the global DARK LIVE flag on and no kill switch —
and even then the per-runtime ``MT5_ALLOW_LIVE`` boundary + the order-time bridge identity pin remain the final
money-path authority downstream.

LIVE close/modify/partial-close are deliberately NOT implemented here: those producers stay fail-closed for a
LIVE follower (``follower_not_demo``). A copied LIVE OPEN is therefore NOT auto-managed by GuvFX — the bounded
acceptance caps that risk (max_orders=1, max_concurrent_positions=1) and the operator closes it manually. See
``docs/ADRs/0057-live-copier-exact-pair.md`` and ``docs/KNOWN_ISSUES.md``.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from trade_copier.models import (
    CopierLiveAuthorization,
    CopierLiveAuthorizationStatus,
    CopierLiveKillSwitch,
    ReplicationStatus,
)

logger = logging.getLogger(__name__)

_VOL_EPS = 1e-9


def kill_switch_engaged() -> bool:
    """True when ANY durable ``CopierLiveKillSwitch`` is engaged (the DB-backed emergency disarm). Fail-closed:
    on any error treat it as engaged (deny)."""
    try:
        return CopierLiveKillSwitch.objects.filter(engaged=True).exists()
    except Exception:  # noqa: BLE001 — a DB error must never be read as "not disarmed"
        return True


def active_authorization_for(master_account_id: int, follower_account_id: int):
    """The single ACTIVE ``CopierLiveAuthorization`` for this exact pair, else ``None`` (never raises)."""
    try:
        return (CopierLiveAuthorization.objects
                .filter(master_account_id=master_account_id, follower_account_id=follower_account_id,
                        status=CopierLiveAuthorizationStatus.ACTIVE)
                .order_by("-created_at").first())
    except Exception:  # noqa: BLE001 — fail closed
        return None


def _current_identity(account):
    """The account's current pinned ``(login, server, environment)`` as trimmed strings. Never raises; an
    environment that cannot be cleanly resolved comes back as ``""`` so a comparison fails closed."""
    login = str(getattr(account, "account_number", "") or "").strip()
    try:
        server = str(getattr(getattr(account, "broker_server", None), "server_name", "") or "").strip()
    except Exception:  # noqa: BLE001
        server = ""
    try:
        from trading.account_policy import account_environment
        env = str(account_environment(account))
    except Exception:  # noqa: BLE001 — integrity error / mismatch ⇒ unresolved ⇒ fail closed
        env = ""
    return login, server, env


def _identities_match(authz, master_acct, follower_acct) -> tuple[bool, str]:
    """Both the master AND follower current pinned identities must still equal the authorization snapshots
    (login + server + environment). Drift in either invalidates (a re-bound/changed identity was never
    reviewed)."""
    ml, ms, me = _current_identity(master_acct)
    fl, fs, fe = _current_identity(follower_acct)
    if not (ml and fl):
        return False, "identity_unpinned"
    if not (authz.master_login == ml and authz.master_server == ms and authz.master_environment == me):
        return False, "master_identity_drift"
    if not (authz.follower_login == fl and authz.follower_server == fs and authz.follower_environment == fe):
        return False, "follower_identity_drift"
    return True, "ok"


def _authorization_window_ok(authz, now) -> tuple[bool, str]:
    if authz.status != CopierLiveAuthorizationStatus.ACTIVE or authz.revoked_at is not None:
        return False, "authorization_not_active"
    if now < authz.valid_from:
        return False, "authorization_not_yet_valid"
    if now >= authz.expires_at:
        return False, "authorization_expired"
    if authz.orders_consumed >= authz.max_orders:
        return False, "order_allowance_exhausted"
    return True, "ok"


def open_live_copier_count(follower) -> int:
    """How many LIVE copier OPEN positions this follower currently has requested/live (concurrency cap).

    Conservative (over-counts rather than under-counts, since LIVE close is not auto-managed): every OPEN
    replication for this follower in a live/requested state whose produced job is a truthful LIVE order."""
    from trade_copier.models import CopierReplication
    qs = (CopierReplication.objects
          .filter(follower_id=follower.id, requested_op="OPEN",
                  status__in=(ReplicationStatus.EXECUTION_REQUESTED, ReplicationStatus.EXECUTION_VERIFIED),
                  execution_job__isnull=False,
                  execution_job__payload__execution_mode="LIVE"))
    return qs.count()


def admit_open_live(replication, *, now=None) -> tuple[bool, str]:
    """Fail-closed admission for producing a LIVE follower OPEN. Safe to call before AND inside the lock.

    Returns ``(ok, reason)``. ``ok`` is True ONLY when every independent gate passes. This never creates a
    job and never mutates state — ``execution_producer.create_live_open_job`` does that under the lock, and
    re-verifies the allowance there."""
    from trade_copier.flags import copier_live_execute_enabled
    from trade_copier.replication import _broker_constraints, _master_position_from_event, validate_follower_binding
    from trade_copier.execution_producer import (
        _FRESHNESS_SECONDS, _fresh_open_size, _protection_ok, _superseded_by_close)
    from trade_copier.symbols import resolve_follower_symbol
    from trading.account_policy import is_demo_environment

    now = now or timezone.now()

    # 1-4. The whole flag stack must be armed, incl. the explicit LIVE arm; and no durable kill switch.
    if not copier_live_execute_enabled():
        return False, "live_copier_arm_disabled"
    if kill_switch_engaged():
        return False, "live_copier_kill_switch_engaged"

    # Generic producibility (re-checked here so this is self-contained under the lock).
    if replication.execution_job_id is not None:
        return False, "already_produced"
    if replication.status != ReplicationStatus.SHADOW_RECORDED:
        return False, "not_shadow_intent"
    if replication.requested_op != "OPEN":
        return False, "op_not_producible"          # LIVE close/modify/partial-close stay fail-closed
    follower = replication.follower
    if not follower.enabled:
        return False, "follower_disabled"

    facct = follower.follower_account
    macct = follower.master.trading_account

    # This path is for a non-demo follower only; a demo follower must use the demo admission.
    try:
        if is_demo_environment(facct):
            return False, "follower_is_demo_use_demo_path"
    except Exception:  # noqa: BLE001
        return False, "environment_unresolved"

    # 5-7, 13. An ACTIVE, unexpired, allowance-remaining, human-created authorization for the EXACT pair/user.
    authz = active_authorization_for(macct.id, facct.id)
    if authz is None:
        return False, "no_live_authorization"
    if not (authz.user_id == facct.user_id == macct.user_id):
        return False, "user_mismatch"
    if authz.master_account_id != macct.id:
        return False, "master_account_mismatch"
    if authz.follower_account_id != facct.id:
        return False, "follower_account_mismatch"
    ok, reason = _authorization_window_ok(authz, now)
    if not ok:
        return False, reason

    # 8. Exact master + follower broker login/server/environment identities (defence against drift/rebind).
    ok, reason = _identities_match(authz, macct, facct)
    if not ok:
        return False, reason

    # Structural binding (cross-user/self-follow/broker binding) — defence in depth.
    ok, reason = validate_follower_binding(follower)
    if not ok:
        return False, reason

    # 9. Exact verified symbol map: source symbol + resolved follower symbol must match the authorization, and
    #    the CopierSymbolMap must still verify at order time.
    event = replication.master_event
    # The event MUST belong to the follower's (and thus the authorization's) master — enforced by
    # construction in _replicate_one, asserted here so the per-master watermark/identity can never be
    # compared against a foreign master's event.
    if event.master_id != follower.master_id:
        return False, "event_master_mismatch"
    if str(event.symbol) != str(authz.source_symbol):
        return False, "source_symbol_mismatch"
    sym = resolve_follower_symbol(macct, event.symbol, facct)
    if not sym.ok or sym.follower_symbol != authz.follower_symbol:
        return False, "symbol_map_unverified_or_changed"
    if replication.follower_symbol != authz.follower_symbol:
        return False, "intent_follower_symbol_mismatch"

    # 12. Follower active (bridge identity health is enforced by the order-time pin + MT5_ALLOW_LIVE below).
    if not getattr(facct, "is_active", False):
        return False, "account_inactive"

    # 10. Destination instrument enabled + FRESH authoritative broker constraints (never the stored flag).
    from execution.models import BrokerInstrument
    if not BrokerInstrument.objects.filter(
            account_id=facct.id, broker_symbol=authz.follower_symbol, enabled=True).exists():
        return False, "instrument_not_ready"
    constraints = _broker_constraints(facct, authz.follower_symbol)
    if not constraints.authoritative:
        return False, "constraints_not_authoritative"

    # 6. Future-event-only watermark: only an OPEN strictly AFTER the authorization's watermark (by both the
    #    per-master event sequence AND observed time) qualifies — no historical/open/previously-evaluated replay.
    if event.sequence <= authz.watermark_event_sequence:
        return False, "event_before_watermark_sequence"
    if not (event.observed_at and event.observed_at > authz.watermark_at):
        return False, "event_before_watermark_time"

    # Staleness: a superseded or out-of-window OPEN is not produced (recovery window).
    if _superseded_by_close(event):
        return False, "open_superseded_by_close"
    if event.observed_at < now - timedelta(seconds=_FRESHNESS_SECONDS):
        return False, "stale_open_outside_window"

    # 11 (protection) — never copy an UNPROTECTED position to a LIVE account: complete, directionally-valid SL+TP.
    snap = _master_position_from_event(event)
    ok, reason = _protection_ok(snap)
    if not ok:
        return False, reason
    if not replication.sltp_translation_ok:
        return False, "sltp_incomplete"

    # Sizing valid + within the authorized maximum (a clamp-to-cap is a material mismatch, not success).
    sr = _fresh_open_size(replication, constraints)
    if not sr.ok:
        return False, f"fresh_size:{sr.detail}"
    if sr.detail == "clamped_to_max":
        return False, "volume_would_be_clamped"
    if not (sr.volume and sr.volume > 0):
        return False, "non_positive_volume"
    if float(sr.volume) > float(authz.max_volume) + _VOL_EPS:
        return False, "volume_exceeds_authorization"

    # Concurrency cap (independent of the one-order allowance).
    if open_live_copier_count(follower) >= authz.max_concurrent_positions:
        return False, "max_concurrent_reached"

    # 2 (runtime), 14. The existing execution LIVE wall + the per-runtime MT5_ALLOW_LIVE boundary must admit
    #     this follower. allow_live_for_account is the SAME predicate render_bridge_env uses to set
    #     MT5_ALLOW_LIVE, so admission passing means the runtime is (or will be) live-authorized.
    try:
        from execution.bridge_config import allow_live_for_account
        if not allow_live_for_account(facct):
            return False, "runtime_live_boundary_denied"
    except Exception:  # noqa: BLE001
        return False, "runtime_live_boundary_error"

    # 15. No active broker runtime pause / kill switch for this follower.
    try:
        from execution.models import BrokerRuntimePause
        if BrokerRuntimePause.objects.filter(account_id=facct.id, paused=True).exists():
            return False, "broker_runtime_paused"
    except Exception:  # noqa: BLE001
        return False, "broker_pause_unresolved"

    return True, "ok"


def _live_payload(replication, authz, volume, max_lot) -> dict:
    """A TRUTHFUL LIVE OPEN payload: ``is_demo=False`` and ``execution_mode='LIVE'`` (never a disguised demo
    path), carrying the order-time identity pin so the bridge binds the response to the authorized follower
    identity. ``inject_identity_pin`` (ExecutionJob.save) only ``setdefault``s these, so our explicit pin is
    authoritative."""
    event = replication.master_event
    return {
        "symbol": replication.follower_symbol,
        "provider_symbol": event.symbol,
        "side": event.side,
        "lots": str(volume),
        "sl_price": str(replication.translated_sl) if replication.translated_sl is not None else None,
        "tp_price": str(replication.translated_tp) if replication.translated_tp is not None else None,
        "entry_price": None,                              # market order
        "is_demo": False,                                 # TRUTHFUL LIVE — never falsely demo
        "execution_mode": "LIVE",
        "comment": f"CPY{replication.id}",                # copier provenance (cycle-prevention + reconcile)
        "signal_source": "copier",
        "max_lot": (str(max_lot) if max_lot is not None else None),
        "copier_replication_id": replication.id,
        "copier_live_authorization_id": authz.id,
        # Order-time bridge identity pin (ADR-0057 §9) — bind to the authorized follower identity.
        "require_identity_pin": True,
        "expected_login": authz.follower_login,
        "expected_server": authz.follower_server,
        "expected_environment": authz.follower_environment,
    }


def create_live_open_job(locked_replication) -> dict:
    """Create exactly one truthful LIVE ``PLACE_ORDER`` for a LIVE-authorized OPEN, consuming one order
    allowance — ATOMIC. MUST be called from inside ``produce_one``'s ``transaction.atomic`` block with
    ``locked_replication`` already ``select_for_update``'d. Re-reads + locks the authorization row so a
    revoke / expiry / exhausted allowance committed mid-flight is seen and fails closed; a retry can never
    place a second order because the allowance is consumed under the same lock."""
    from execution.models import ExecutionJob
    from trade_copier.execution_producer import _broker_constraints, _fresh_open_size, _max_lot

    follower = locked_replication.follower
    facct = follower.follower_account
    macct = follower.master.trading_account

    authz = (CopierLiveAuthorization.objects
             .select_for_update()
             .filter(master_account_id=macct.id, follower_account_id=facct.id,
                     status=CopierLiveAuthorizationStatus.ACTIVE)
             .order_by("-created_at").first())
    if authz is None:
        return {"replication_id": locked_replication.id, "produced": False, "reason": "no_live_authorization"}

    now = timezone.now()
    ok, reason = _authorization_window_ok(authz, now)
    if not ok:
        return {"replication_id": locked_replication.id, "produced": False, "reason": reason}
    if kill_switch_engaged():
        return {"replication_id": locked_replication.id, "produced": False,
                "reason": "live_copier_kill_switch_engaged"}

    # Concurrency cap re-checked UNDER the authorization row lock (which serializes all order creation for this
    # pair), so two near-simultaneous OPENs on different events cannot both slip past a stale preflight count
    # when max_orders > max_concurrent — the second, serialized after the first commits, sees the new count.
    if open_live_copier_count(follower) >= authz.max_concurrent_positions:
        return {"replication_id": locked_replication.id, "produced": False, "reason": "max_concurrent_reached"}

    constraints = _broker_constraints(facct, locked_replication.follower_symbol)
    sr = _fresh_open_size(locked_replication, constraints)
    if not sr.ok:
        return {"replication_id": locked_replication.id, "produced": False, "reason": f"fresh_size:{sr.detail}"}
    if sr.detail == "clamped_to_max":
        return {"replication_id": locked_replication.id, "produced": False, "reason": "volume_would_be_clamped"}
    if not (sr.volume and sr.volume > 0):
        return {"replication_id": locked_replication.id, "produced": False, "reason": "non_positive_volume"}
    if float(sr.volume) > float(authz.max_volume) + _VOL_EPS:
        return {"replication_id": locked_replication.id, "produced": False, "reason": "volume_exceeds_authorization"}

    max_lot = _max_lot(constraints, follower)
    job = ExecutionJob.objects.create(
        account=facct, job_type=ExecutionJob.JobType.PLACE_ORDER,
        terminal_node_id=getattr(facct, "terminal_node_id", None),
        payload=_live_payload(locked_replication, authz, sr.volume, max_lot),
        hosted_idempotency_key=f"copier-live:{locked_replication.pk}")

    # Consume exactly one allowance unit under the lock; exhausting it retires the authorization (CONSUMED),
    # which also frees the one-ACTIVE-per-pair partial-unique so a deliberate re-grant is possible later.
    authz.orders_consumed = authz.orders_consumed + 1
    if authz.orders_consumed >= authz.max_orders:
        authz.status = CopierLiveAuthorizationStatus.CONSUMED
    authz.save(update_fields=["orders_consumed", "status", "updated_at"])

    locked_replication.execution_job = job
    locked_replication.status = ReplicationStatus.EXECUTION_REQUESTED
    locked_replication.target_volume = sr.volume
    locked_replication.save(update_fields=["execution_job", "status", "target_volume", "updated_at"])

    return {"replication_id": locked_replication.id, "produced": True, "job_id": job.id,
            "job_type": ExecutionJob.JobType.PLACE_ORDER, "armed": True, "live": True,
            "volume": sr.volume, "status": ReplicationStatus.EXECUTION_REQUESTED}


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════
# ADR-0083 — LIVE lifecycle completion: MODIFY_PROTECTION / PARTIAL_CLOSE / FULL_CLOSE on a BOUND follower.
#
# ADR-0057 admitted a LIVE follower OPEN only; the lifecycle ops stayed fail-closed. These admissions complete
# them with the SAME fail-closed discipline, plus three invariants that OPEN does not need:
#   * They act ONLY on an already-LINKED follower position (a VERIFIED, reconciled copier-owned ticket) — never
#     on a symbol, never creating a position. An unlinked/ambiguous ticket denies.
#   * They can NEVER increase or reopen exposure: a MODIFY changes only SL/TP, a PARTIAL reduces (rounded DOWN,
#     never to a full close), a FULL close closes the one bound ticket. No path here produces a PLACE_ORDER.
#   * They require the SEPARATE, per-op authority on the authorization (modify/partial/full-close), plus the
#     per-op DARK flag — distinct from the one-shot ENTRY allowance (which they never consume).
# The authorization that governs a bound position is the one the entry was placed under — which is CONSUMED
# once the single entry is used. So lifecycle ops look it up ACTIVE-OR-CONSUMED (never REVOKED), and the window
# / kill-switch policy decides whether a close may still CONTAIN after expiry or under the switch (close-only,
# exposure-reducing only; a MODIFY never survives expiry or the kill switch). See docs ADR-0083.
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════

OP_MODIFY = "MODIFY"
OP_PARTIAL_CLOSE = "PARTIAL_CLOSE"
OP_FULL_CLOSE = "FULL_CLOSE"

_OP_REQUESTED = {OP_MODIFY: "MODIFY_SLTP", OP_PARTIAL_CLOSE: "PARTIAL_CLOSE", OP_FULL_CLOSE: "CLOSE"}
_CLOSE_OPS = frozenset({OP_PARTIAL_CLOSE, OP_FULL_CLOSE})


def lifecycle_authorization_for(master_account_id: int, follower_account_id: int):
    """The authorization governing an already-bound position for this exact pair: the most recent
    non-REVOKED (ACTIVE **or** CONSUMED) ``CopierLiveAuthorization``. The single entry allowance is CONSUMED
    once the OPEN is placed, but the bound position is still managed under that same reviewed grant; a REVOKED
    authorization governs nothing (the operator deliberately withdrew it → manual containment). Never raises."""
    try:
        return (CopierLiveAuthorization.objects
                .filter(master_account_id=master_account_id, follower_account_id=follower_account_id,
                        status__in=(CopierLiveAuthorizationStatus.ACTIVE,
                                    CopierLiveAuthorizationStatus.CONSUMED),
                        revoked_at__isnull=True)
                .order_by("-created_at").first())
    except Exception:  # noqa: BLE001 — fail closed
        return None


def _lifecycle_authority_ok(authz, op) -> tuple[bool, str]:
    """The authorization must carry the SEPARATE, auditable per-op authority (default False). Fail-closed."""
    if op == OP_MODIFY:
        return (bool(authz.modify_authority), "ok" if authz.modify_authority else "no_modify_authority")
    if op == OP_PARTIAL_CLOSE:
        return (bool(authz.partial_close_authority),
                "ok" if authz.partial_close_authority else "no_partial_close_authority")
    if op == OP_FULL_CLOSE:
        return (bool(authz.full_close_authority),
                "ok" if authz.full_close_authority else "no_full_close_authority")
    return False, "unknown_op"


def _lifecycle_window_ok(authz, op, now) -> tuple[bool, str]:
    """Window/containment decision. Within [valid_from, expires_at): every authorized op is in-window. After
    expiry: a MODIFY is NEVER propagated (it does not reduce exposure); a close/partial-close may CONTAIN an
    already-bound position ONLY when BOTH the authorization's ``close_only_after_expiry`` policy AND the global
    ``copier_live_close_only_after_expiry_enabled`` arm are set (exposure strictly reduced — never a new entry)."""
    if authz.status == CopierLiveAuthorizationStatus.REVOKED or authz.revoked_at is not None:
        return False, "authorization_revoked"
    if now < authz.valid_from:
        return False, "authorization_not_yet_valid"
    if now < authz.expires_at:
        return True, "ok"
    # Expired entry window.
    if op not in _CLOSE_OPS:
        return False, "authorization_expired"
    from trade_copier.flags import copier_live_close_only_after_expiry_enabled
    if authz.close_only_after_expiry and copier_live_close_only_after_expiry_enabled():
        return True, "close_only_after_expiry"
    return False, "entry_window_expired_no_close_only"


def _kill_switch_policy_ok(op) -> tuple[bool, str]:
    """Kill-switch policy. Engaged, it ALWAYS blocks a MODIFY (and every new entry). It blocks a
    close/partial-close too UNLESS the explicit, default-OFF ``copier_live_contain_on_kill_switch_enabled``
    policy is set, which permits ONLY a safety-reducing close/partial-close of an already-bound position."""
    if not kill_switch_engaged():
        return True, "ok"
    if op not in _CLOSE_OPS:
        return False, "live_copier_kill_switch_engaged"
    from trade_copier.flags import copier_live_contain_on_kill_switch_enabled
    if copier_live_contain_on_kill_switch_enabled():
        return True, "kill_switch_close_only_containment"
    return False, "kill_switch_no_close_containment"


def _per_op_flag_armed(op) -> bool:
    from trade_copier.flags import (copier_live_modify_enabled, copier_live_partial_close_enabled,
                                    copier_live_full_close_enabled)
    return {OP_MODIFY: copier_live_modify_enabled,
            OP_PARTIAL_CLOSE: copier_live_partial_close_enabled,
            OP_FULL_CLOSE: copier_live_full_close_enabled}[op]()


def _reducing_op_in_flight(acct_id, ticket) -> bool:
    """A CLOSE_TRADE or PARTIAL_CLOSE_TRADE already in flight for this (account, ticket). Cross-op idempotency:
    never emit a second concurrent exposure-reducing command for one position (a redundant/divergent close)."""
    from trade_copier.execution_producer import _close_already_targets, _partial_close_already_targets
    return _close_already_targets(acct_id, ticket) or _partial_close_already_targets(acct_id, ticket)


def _linked_follower_open(replication):
    """The matched, reconciled OPEN replication whose follower ticket this lifecycle op may act on
    (position-keyed, NEVER by symbol). Returns ``(open_repl|None, ticket|None, reason)``."""
    from trade_copier.execution_producer import _matched_open_for_close
    open_repl = _matched_open_for_close(replication)
    if open_repl is None or open_repl.follower_ticket is None:
        return None, None, "no_verified_follower_ticket"
    return open_repl, int(open_repl.follower_ticket), "ok"


def _governing_authorization(open_repl):
    """The EXACT authorization the bound position was OPENED under — the ``copier_live_authorization_id`` the
    LIVE OPEN recorded on its job payload. ADR-0083: a lifecycle op is governed by the position's OWN reviewed
    grant, NEVER the newest grant for the pair (a later re-grant must not retroactively widen authority or
    borrow a fresh window for an already-open position). Non-REVOKED only. Returns ``(authz|None, reason)``."""
    if open_repl is None or open_repl.execution_job_id is None:
        return None, "open_has_no_live_job"
    try:
        payload = open_repl.execution_job.payload or {}
        aid = payload.get("copier_live_authorization_id")
        aid = int(aid) if aid is not None else None
    except Exception:  # noqa: BLE001 — fail closed
        return None, "governing_authorization_unresolved"
    if aid is None:
        return None, "open_not_live_authorized"
    authz = CopierLiveAuthorization.objects.filter(id=aid, revoked_at__isnull=True).first()
    if authz is None:
        return None, "governing_authorization_revoked_or_absent"
    return authz, "ok"


def _live_lifecycle_preamble(replication, op, *, now) -> tuple[bool, object, int, str]:
    """Shared fail-closed gate stack for every LIVE lifecycle op. Returns ``(ok, authz, ticket, reason)``.

    Mirrors ``admit_open_live`` for the identity/pair/symbol/runtime gates, but binds to an already-LINKED
    position, resolves the GOVERNING authorization (the one the OPEN was placed under — not the newest for the
    pair), and applies the per-op authority + window/containment + kill-switch policy + a stale/superseded guard.
    Never mutates state."""
    # 1. The whole LIVE stack AND the specific per-op arm.
    from trade_copier.flags import copier_live_execute_enabled
    from trading.account_policy import is_demo_environment
    if not copier_live_execute_enabled():
        return False, None, 0, "live_copier_arm_disabled"
    if not _per_op_flag_armed(op):
        return False, None, 0, f"live_{op.lower()}_arm_disabled"

    # 2. Generic producibility (self-contained under the lock).
    if replication.execution_job_id is not None:
        return False, None, 0, "already_produced"
    if replication.status != ReplicationStatus.SHADOW_RECORDED:
        return False, None, 0, "not_shadow_intent"
    if replication.requested_op != _OP_REQUESTED[op]:
        return False, None, 0, "op_mismatch"
    follower = replication.follower
    if not follower.enabled:
        return False, None, 0, "follower_disabled"

    facct = follower.follower_account
    macct = follower.master.trading_account

    # 3. LIVE path only — a demo follower must use the demo producer.
    try:
        if is_demo_environment(facct):
            return False, None, 0, "follower_is_demo_use_demo_path"
    except Exception:  # noqa: BLE001
        return False, None, 0, "environment_unresolved"
    if not getattr(facct, "is_active", False):
        return False, None, 0, "account_inactive"

    # 4. The VERIFIED, reconciled LINKED follower ticket + its OPEN — the ONLY position this op may act on.
    open_repl, ticket, reason = _linked_follower_open(replication)
    if ticket is None:
        return False, None, 0, reason

    # 5. The GOVERNING authorization — the one THIS position was opened under (by recorded id), never REVOKED.
    authz, reason = _governing_authorization(open_repl)
    if authz is None:
        return False, None, 0, reason
    if not (authz.user_id == facct.user_id == macct.user_id):
        return False, None, 0, "user_mismatch"
    if authz.master_account_id != macct.id or authz.follower_account_id != facct.id:
        return False, None, 0, "pair_mismatch"

    # 6. The SEPARATE per-op authority, the window/containment policy, and the kill-switch policy.
    ok, reason = _lifecycle_authority_ok(authz, op)
    if not ok:
        return False, None, 0, reason
    ok, reason = _lifecycle_window_ok(authz, op, now)
    if not ok:
        return False, None, 0, reason
    ok, reason = _kill_switch_policy_ok(op)
    if not ok:
        return False, None, 0, reason

    # 7. Exact pinned identities + structural binding (defence against drift/rebind/cross-user).
    ok, reason = _identities_match(authz, macct, facct)
    if not ok:
        return False, None, 0, reason
    from trade_copier.replication import validate_follower_binding
    ok, reason = validate_follower_binding(follower)
    if not ok:
        return False, None, 0, reason

    # 8. Exact verified symbol map (source + resolved follower symbol must equal the authorization). The intent
    #    follower symbol is required and must equal the authorization — unconditionally (a blank one denies).
    event = replication.master_event
    if event.master_id != follower.master_id:
        return False, None, 0, "event_master_mismatch"
    if str(event.symbol) != str(authz.source_symbol):
        return False, None, 0, "source_symbol_mismatch"
    from trade_copier.symbols import resolve_follower_symbol
    sym = resolve_follower_symbol(macct, event.symbol, facct)
    if not sym.ok or sym.follower_symbol != authz.follower_symbol:
        return False, None, 0, "symbol_map_unverified_or_changed"
    if not replication.follower_symbol or replication.follower_symbol != authz.follower_symbol:
        return False, None, 0, "intent_follower_symbol_mismatch"

    # 9. Ordering / staleness guard (parity with admit_open_live). The lifecycle event must be strictly AFTER
    #    the OPEN it manages, and a MODIFY/PARTIAL superseded by a later FULL_CLOSE is stale — never applied
    #    (so two out-of-order SL changes can never net-widen the live stop, and a close-before-modify wins).
    from trade_copier.execution_producer import _superseded_by_close
    open_event = getattr(open_repl, "master_event", None)
    if open_event is not None and int(event.sequence) <= int(open_event.sequence):
        return False, None, 0, "event_not_after_open"
    if op != OP_FULL_CLOSE and _superseded_by_close(event):
        return False, None, 0, "superseded_by_later_close"

    # 10. The runtime LIVE boundary (MT5_ALLOW_LIVE) + no broker runtime pause (same predicates as OPEN).
    try:
        from execution.bridge_config import allow_live_for_account
        if not allow_live_for_account(facct):
            return False, None, 0, "runtime_live_boundary_denied"
    except Exception:  # noqa: BLE001
        return False, None, 0, "runtime_live_boundary_error"
    try:
        from execution.models import BrokerRuntimePause
        if BrokerRuntimePause.objects.filter(account_id=facct.id, paused=True).exists():
            return False, None, 0, "broker_runtime_paused"
    except Exception:  # noqa: BLE001
        return False, None, 0, "broker_pause_unresolved"

    return True, authz, ticket, "ok"


def admit_modify_live(replication, *, now=None) -> tuple[bool, int, str]:
    """Fail-closed admission for a LIVE SL/TP MODIFY of the bound follower position. ``(ok, ticket, reason)``.

    A MODIFY changes only SL/TP (never volume/side → cannot increase exposure); a complete translated SL+TP is
    required, and an SL removal/weaken fails closed independently."""
    now = now or timezone.now()
    ok, _authz, ticket, reason = _live_lifecycle_preamble(replication, OP_MODIFY, now=now)
    if not ok:
        return False, 0, reason
    if not replication.sltp_translation_ok:
        return False, ticket, "sltp_incomplete"
    from trade_copier.execution_producer import _sl_weakening_reason, _modify_already_targets
    wr = _sl_weakening_reason(replication.master_event)
    if wr is not None:
        return False, ticket, wr
    acct_id = replication.follower.follower_account_id
    if _modify_already_targets(acct_id, ticket):
        return False, ticket, "modify_already_in_flight"
    # A close/partial-close already reducing this ticket supersedes a modify (don't modify a closing position).
    if _reducing_op_in_flight(acct_id, ticket):
        return False, ticket, "close_in_flight_supersedes_modify"
    return True, ticket, "ok"


def admit_full_close_live(replication, *, now=None) -> tuple[bool, int, str]:
    """Fail-closed admission for a LIVE FULL CLOSE of the bound follower position. ``(ok, ticket, reason)``."""
    now = now or timezone.now()
    ok, _authz, ticket, reason = _live_lifecycle_preamble(replication, OP_FULL_CLOSE, now=now)
    if not ok:
        return False, 0, reason
    if _reducing_op_in_flight(replication.follower.follower_account_id, ticket):
        return False, ticket, "close_already_in_flight"
    return True, ticket, "ok"


def admit_partial_close_live(replication, *, now=None, fetch=None) -> tuple[bool, int, float, float, str]:
    """Fail-closed admission for a LIVE PARTIAL CLOSE. ``(ok, ticket, reduction, remaining, reason)``.

    The reduction is derived from the master's confirmed before/after volume applied to the follower's FRESH
    identity-firewalled CURRENT volume, rounded DOWN, and is rejected if it would be a full close, below the
    broker minimum, or leave a sub-minimum remainder — so a partial close only ever strictly REDUCES exposure."""
    now = now or timezone.now()
    ok, _authz, ticket, reason = _live_lifecycle_preamble(replication, OP_PARTIAL_CLOSE, now=now)
    if not ok:
        return False, 0, 0.0, 0.0, reason
    from trade_copier.execution_producer import _partial_reduction, _broker_constraints
    from trade_copier.reconcile import read_follower_position, _fetch_account_positions
    fetch = fetch or _fetch_account_positions
    follower = replication.follower
    acct = follower.follower_account

    ok_read, pos, read_reason = read_follower_position(follower, ticket, fetch=fetch)
    if not ok_read:
        return False, ticket, 0.0, 0.0, f"follower_read:{read_reason}"
    if pos is None:
        return False, ticket, 0.0, 0.0, "follower_ticket_not_open"
    # The fresh follower position symbol must match the intent (which the preamble already pinned == authz).
    if not replication.follower_symbol or str(pos.symbol) != str(replication.follower_symbol):
        return False, ticket, 0.0, 0.0, "symbol_changed"

    constraints = _broker_constraints(acct, replication.follower_symbol or pos.symbol)
    if not constraints.authoritative:
        return False, ticket, 0.0, 0.0, "constraints_not_authoritative"

    event = replication.master_event
    ok_calc, reduction, remaining, calc_reason = _partial_reduction(
        event.volume_before, event.volume_after, pos.volume, constraints)
    if not ok_calc:
        return False, ticket, 0.0, 0.0, calc_reason
    if _reducing_op_in_flight(acct.id, ticket):
        return False, ticket, 0.0, 0.0, "partial_close_already_in_flight"
    return True, ticket, reduction, remaining, "ok"


# ─────────────────────────── typed LIVE lifecycle command envelope + job creation ───────────────────────────
# The ExecutionJob payload IS the typed bridge command. Every LIVE lifecycle command carries: the operation
# type, the follower binding (verified ticket), the account/server/symbol identity pin, the master revision,
# the authorization reference + correlation id, an expiry/deadline, an EXPECTED PRE-STATE fingerprint (so the
# bridge can reject a command whose follower state no longer matches), and the requested POST-STATE. The
# hosted_idempotency_key is the operation id (unique per op+replication) → a replay is a no-op. No arbitrary
# order surface is exposed: each command is one of exactly MODIFY_PROTECTION / PARTIAL_CLOSE / FULL_CLOSE and
# maps to a single fixed ExecutionJob job_type. None of these can OPEN or INCREASE exposure.

_BRIDGE_OP = {OP_MODIFY: "MODIFY_PROTECTION", OP_PARTIAL_CLOSE: "PARTIAL_CLOSE", OP_FULL_CLOSE: "FULL_CLOSE"}


def _live_command_envelope(replication, authz, ticket, op, *, pre_state: dict, post_state: dict) -> dict:
    """The common typed-command fields shared by every LIVE lifecycle command (secret-free)."""
    event = replication.master_event
    return {
        "copier_operation": _BRIDGE_OP[op],               # fixed typed op — never reinterpreted by the bridge
        "ticket": ticket,                                 # the VERIFIED follower position — never by symbol
        "symbol": replication.follower_symbol or event.symbol,
        "is_demo": False,                                 # TRUTHFUL LIVE — never a disguised demo path
        "execution_mode": "LIVE",
        "signal_source": "copier",
        "copier_replication_id": replication.id,
        "copier_live_authorization_id": authz.id,
        "correlation_id": authz.correlation_id or "",
        "master_revision": int(getattr(event, "sequence", 0) or 0),
        # Expiry/deadline — the bridge must reject a command past this instant (ADR-0083 command contract).
        "authorization_expires_at": authz.expires_at.isoformat() if authz.expires_at else None,
        # Expected PRE-state fingerprint + requested POST-state — the bridge verifies pre-state before acting.
        "expected_pre_state": pre_state,
        "requested_post_state": post_state,
        # Order-time bridge identity pin (ADR-0057 §9) — bind the response to the authorized follower identity.
        "require_identity_pin": True,
        "expected_login": authz.follower_login,
        "expected_server": authz.follower_server,
        "expected_environment": authz.follower_environment,
    }


def _relock_live_authz_for_lifecycle(locked_replication, op, *, now):
    """Re-resolve the GOVERNING authorization (the one THIS position was opened under — by recorded id, never the
    newest for the pair) and lock its row, re-applying the window/kill-switch/authority policy under the lock, so
    a revoke/expiry/kill-switch committed mid-flight is seen and fails closed. ``(authz|None, reason)``."""
    open_repl, _ticket, reason = _linked_follower_open(locked_replication)
    if open_repl is None:
        return None, reason
    gov, reason = _governing_authorization(open_repl)
    if gov is None:
        return None, reason
    authz = (CopierLiveAuthorization.objects.select_for_update()
             .filter(id=gov.id, revoked_at__isnull=True).first())
    if authz is None:
        return None, "governing_authorization_revoked_or_absent"
    ok, reason = _lifecycle_authority_ok(authz, op)
    if not ok:
        return None, reason
    ok, reason = _lifecycle_window_ok(authz, op, now)
    if not ok:
        return None, reason
    ok, reason = _kill_switch_policy_ok(op)
    if not ok:
        return None, reason
    return authz, "ok"


def create_live_modify_job(locked_replication, ticket) -> dict:
    """Create exactly one truthful LIVE ``MODIFY_POSITION`` for the bound follower ticket. Called from inside
    ``produce_modify_one``'s lock with the replication locked. Does NOT consume the entry allowance (a MODIFY is
    not an entry) and cannot change volume/side → cannot increase exposure."""
    from execution.models import ExecutionJob
    follower = locked_replication.follower
    facct = follower.follower_account
    macct = follower.master.trading_account
    now = timezone.now()

    authz, reason = _relock_live_authz_for_lifecycle(locked_replication, OP_MODIFY, now=now)
    if authz is None:
        return {"replication_id": locked_replication.id, "produced": False, "reason": reason}

    sl = str(locked_replication.translated_sl) if locked_replication.translated_sl is not None else None
    tp = str(locked_replication.translated_tp) if locked_replication.translated_tp is not None else None
    payload = _live_command_envelope(
        locked_replication, authz, ticket, OP_MODIFY,
        pre_state={"ticket": ticket, "symbol": locked_replication.follower_symbol, "position_open": True},
        post_state={"sl": sl, "tp": tp})
    payload.update({"sl": sl, "tp": tp, "comment": f"CPYM{locked_replication.id}",
                    "reason": "copier_master_sltp_modify"})
    job = ExecutionJob.objects.create(
        account=facct, job_type=ExecutionJob.JobType.MODIFY_POSITION,
        terminal_node_id=getattr(facct, "terminal_node_id", None),
        payload=payload, hosted_idempotency_key=f"copier-live-modify:{locked_replication.pk}")
    locked_replication.execution_job = job
    locked_replication.follower_ticket = ticket
    locked_replication.status = ReplicationStatus.EXECUTION_REQUESTED
    locked_replication.save(update_fields=["execution_job", "follower_ticket", "status", "updated_at"])
    return {"replication_id": locked_replication.id, "produced": True, "job_id": job.id,
            "job_type": ExecutionJob.JobType.MODIFY_POSITION, "armed": True, "live": True,
            "follower_ticket": ticket, "status": ReplicationStatus.EXECUTION_REQUESTED}


def create_live_full_close_job(locked_replication, ticket) -> dict:
    """Create exactly one truthful LIVE ``CLOSE_TRADE`` closing the bound follower ticket. Called from inside
    ``produce_close_one``'s lock. Does NOT consume the entry allowance; cannot reopen/increase exposure."""
    from execution.models import ExecutionJob
    follower = locked_replication.follower
    facct = follower.follower_account
    macct = follower.master.trading_account
    now = timezone.now()

    authz, reason = _relock_live_authz_for_lifecycle(locked_replication, OP_FULL_CLOSE, now=now)
    if authz is None:
        return {"replication_id": locked_replication.id, "produced": False, "reason": reason}

    payload = _live_command_envelope(
        locked_replication, authz, ticket, OP_FULL_CLOSE,
        pre_state={"ticket": ticket, "symbol": locked_replication.follower_symbol, "position_open": True},
        post_state={"closed": True})
    payload.update({"comment": f"CPYC{locked_replication.id}", "reason": "copier_master_full_close"})
    job = ExecutionJob.objects.create(
        account=facct, job_type=ExecutionJob.JobType.CLOSE_TRADE,
        terminal_node_id=getattr(facct, "terminal_node_id", None),
        payload=payload, hosted_idempotency_key=f"copier-live-close:{locked_replication.pk}")
    locked_replication.execution_job = job
    locked_replication.follower_ticket = ticket
    locked_replication.status = ReplicationStatus.EXECUTION_REQUESTED
    locked_replication.save(update_fields=["execution_job", "follower_ticket", "status", "updated_at"])
    return {"replication_id": locked_replication.id, "produced": True, "job_id": job.id,
            "job_type": ExecutionJob.JobType.CLOSE_TRADE, "armed": True, "live": True,
            "follower_ticket": ticket, "status": ReplicationStatus.EXECUTION_REQUESTED}


def create_live_partial_close_job(locked_replication, ticket, reduction, remaining) -> dict:
    """Create exactly one truthful LIVE ``PARTIAL_CLOSE_TRADE`` reducing the bound follower ticket by
    ``reduction`` lots. Called from inside ``produce_partial_close_one``'s lock. Does NOT consume the entry
    allowance; ``reduction`` is a verified strict reduction (< current volume) → cannot increase/reopen."""
    from execution.models import ExecutionJob
    follower = locked_replication.follower
    facct = follower.follower_account
    macct = follower.master.trading_account
    now = timezone.now()

    if not (reduction and float(reduction) > 0):
        return {"replication_id": locked_replication.id, "produced": False, "reason": "non_positive_reduction"}

    authz, reason = _relock_live_authz_for_lifecycle(locked_replication, OP_PARTIAL_CLOSE, now=now)
    if authz is None:
        return {"replication_id": locked_replication.id, "produced": False, "reason": reason}

    payload = _live_command_envelope(
        locked_replication, authz, ticket, OP_PARTIAL_CLOSE,
        pre_state={"ticket": ticket, "symbol": locked_replication.follower_symbol,
                   "position_open": True, "expected_current_volume": str(round(float(reduction) + float(remaining), 8))},
        post_state={"reduce_by": str(reduction), "expected_remaining": str(remaining)})
    payload.update({"volume": str(reduction), "expected_remaining": str(remaining),
                    "comment": f"CPYP{locked_replication.id}", "reason": "copier_master_partial_close"})
    job = ExecutionJob.objects.create(
        account=facct, job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE,
        terminal_node_id=getattr(facct, "terminal_node_id", None),
        payload=payload, hosted_idempotency_key=f"copier-live-partial:{locked_replication.pk}")
    locked_replication.execution_job = job
    locked_replication.follower_ticket = ticket
    locked_replication.status = ReplicationStatus.EXECUTION_REQUESTED
    locked_replication.save(update_fields=["execution_job", "follower_ticket", "status", "updated_at"])
    return {"replication_id": locked_replication.id, "produced": True, "job_id": job.id,
            "job_type": ExecutionJob.JobType.PARTIAL_CLOSE_TRADE, "armed": True, "live": True,
            "follower_ticket": ticket, "reduction": reduction, "remaining": remaining,
            "status": ReplicationStatus.EXECUTION_REQUESTED}
