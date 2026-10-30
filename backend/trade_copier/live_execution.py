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
