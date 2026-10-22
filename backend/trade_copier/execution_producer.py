"""trade_copier.execution_producer — gated DEMO-only ExecutionJob producer from replication intents (B-exec).

Turns recorded OPEN replication intents into enqueue-only ``ExecutionJob`` rows, DEMO followers ONLY,
under a FRESH admission re-check performed INSIDE the transactional boundary (never trusting values read
before the lock). Hard rails (ADR-0051 R2 / Friday packet §5):

* **P0-A (flag bypass)** — ``copier_execute_enabled()`` is enforced at the entry of every public write
  seam AND re-checked under the lock; the SHADOW/DEMO mode is captured ONCE and used consistently, so a
  mid-operation flag flip cannot change the produced job. Flags OFF ⇒ no job, no state mutation.
* **P0-B (TOCTOU)** — inside the lock we re-read + re-validate the replication, the follower
  (enabled/config/owner/binding), the destination account (env/active), the symbol, the FRESH broker
  constraints, and the master state; a disabled/rebound/deactivated follower, owner mismatch, stale OPEN
  (superseded by a later CLOSE, or outside the freshness window) is rejected. No broker network call is
  made under the lock.
* **P0-C (payload)** — ``lots`` is a valid positive number sized against FRESH broker constraints; the
  payload carries source/max-lot/provenance (copier ``CPY<id>`` comment); a below-min or clamp-altered
  volume is rejected rather than silently shrunk and reported as success.
* **P0-D (protection/facts)** — an executable OPEN requires complete, directionally-valid SL+TP and
  FRESH authoritative broker constraints; missing/stale/ambiguous/non-authoritative facts are rejected.
* **P1 (SHADOW semantics)** — a suppressed SHADOW job sets ``SHADOW_JOB_RECORDED`` (NOT
  EXECUTION_REQUESTED). Only a real armed DEMO ``PLACE_ORDER`` sets ``EXECUTION_REQUESTED``; only broker
  evidence may ever become ``EXECUTION_VERIFIED``.
* Idempotent (one job per intent, row-locked). Inherits every ``ExecutionJob.save()`` gate. Never LIVE,
  never a bridge-wall bypass. BUILD != ARM — arming + any real DEMO run is operator-gated.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import diff
from trade_copier.flags import copier_execute_demo_armed, copier_execute_enabled
from trade_copier.models import CopierFollower, CopierReplication, MasterEvent, ReplicationStatus
from trade_copier.replication import (
    _broker_constraints,
    _cfg,
    _dec,
    _master_position_from_event,
    validate_follower_binding,
)
from trade_copier.sizing import compute_follower_volume
from trade_copier.symbols import resolve_follower_symbol

logger = logging.getLogger(__name__)

_PRODUCIBLE_OPS = {"OPEN"}          # SCALE_IN later; CLOSE/MODIFY need a verified follower ticket.
_FRESHNESS_SECONDS = 3600          # an OPEN older than this is stale (recovery window) — not produced.
_EPS = 1e-9


def _has(v) -> bool:
    return v is not None and abs(float(v)) > _EPS


def _superseded_by_close(event: MasterEvent) -> bool:
    """True when a LATER FULL_CLOSE for the same master position exists — the OPEN is stale."""
    return (MasterEvent.objects
            .filter(master_id=event.master_id, position_ticket=event.position_ticket,
                    event_type=diff.FULL_CLOSE, sequence__gt=event.sequence)
            .exists())


def _protection_ok(snap) -> tuple[bool, str]:
    """An executable OPEN requires COMPLETE, directionally-valid SL AND TP (packet §5 P0-D)."""
    if snap is None or not _has(snap.price_open):
        return False, "master_entry_unavailable"
    if not (_has(snap.sl) and _has(snap.tp)):
        return False, "incomplete_protection"   # require complete SL+TP for an executable OPEN
    entry, sl, tp, side = float(snap.price_open), float(snap.sl), float(snap.tp), snap.side
    if side == "BUY" and not (sl < entry < tp):
        return False, "protection_not_directional"
    if side == "SELL" and not (tp < entry < sl):
        return False, "protection_not_directional"
    return True, "ok"


def admit_open(replication: CopierReplication) -> tuple[bool, str]:
    """Fail-closed admission for producing a DEMO OPEN. Safe to call before AND inside the lock."""
    if replication.execution_job_id is not None:
        return False, "already_produced"
    if replication.status != ReplicationStatus.SHADOW_RECORDED:
        return False, "not_shadow_intent"
    if replication.requested_op not in _PRODUCIBLE_OPS:
        return False, "op_not_producible"            # CLOSE/MODIFY/SCALE_IN: fail-closed here
    follower = replication.follower
    if not follower.enabled:
        return False, "follower_disabled"

    acct = follower.follower_account
    # DEMO ONLY — never a LIVE/ambiguous destination (fail-closed).
    try:
        from trading.account_policy import is_demo_environment
        if not is_demo_environment(acct):
            return False, "follower_not_demo"
    except Exception:  # noqa: BLE001 — unresolved environment is NOT demo
        return False, "environment_unresolved"
    if not getattr(acct, "is_active", False):
        return False, "account_inactive"

    ok, reason = validate_follower_binding(follower)   # cross-user/self-follow/binding, defense in depth
    if not ok:
        return False, reason

    if not replication.sltp_translation_ok:
        return False, "sltp_incomplete"
    if replication.target_volume is None:
        return False, "no_target_volume"

    event = replication.master_event
    # Staleness: an OPEN superseded by a later CLOSE, or older than the freshness window, is not produced.
    if _superseded_by_close(event):
        return False, "open_superseded_by_close"
    if event.observed_at and event.observed_at < timezone.now() - timedelta(seconds=_FRESHNESS_SECONDS):
        return False, "stale_open_outside_window"

    # Complete, directionally-valid protection from the authoritative same-poll master snapshot.
    snap = _master_position_from_event(event)
    ok, reason = _protection_ok(snap)
    if not ok:
        return False, reason

    # FRESH authoritative broker constraints — NOT the stored flag.
    constraints = _broker_constraints(acct, replication.follower_symbol)
    if not constraints.authoritative:
        return False, "constraints_not_authoritative"
    # F3: the destination instrument must be ENABLED/ready — a disabled/unready symbol fails closed.
    from execution.models import BrokerInstrument
    if not BrokerInstrument.objects.filter(
            account_id=acct.id, broker_symbol=replication.follower_symbol, enabled=True).exists():
        return False, "instrument_not_ready"
    # F2: re-verify the symbol mapping at PRODUCE time — a CopierSymbolMap revoked or changed since the
    # intent was recorded fails closed (the stored follower_symbol is not trusted on its own).
    sym = resolve_follower_symbol(follower.master.trading_account, event.symbol, acct)
    if not sym.ok or sym.follower_symbol != replication.follower_symbol:
        return False, "symbol_map_changed"
    return True, "ok"


def _fresh_open_size(replication: CopierReplication, constraints):
    """Re-size the OPEN from the ORIGINAL master volume against FRESH constraints + current config."""
    event = replication.master_event
    master_vol = float(event.volume_after) if event.event_type == diff.OPEN else float(event.volume_delta)
    return compute_follower_volume(master_vol, _cfg(replication.follower), constraints)


def _max_lot(constraints, follower) -> float | None:
    caps = [c for c in (constraints.max_volume,
                        (float(follower.max_volume) if follower.max_volume is not None else None))
            if c is not None and c > 0]
    return min(caps) if caps else None


def _payload(replication, volume, max_lot, is_demo, execution_mode) -> dict:
    event = replication.master_event
    return {
        "symbol": replication.follower_symbol,
        "provider_symbol": event.symbol,
        "side": event.side,
        "lots": str(volume),                          # a valid positive number; bridge floats it
        "sl_price": str(replication.translated_sl) if replication.translated_sl is not None else None,
        "tp_price": str(replication.translated_tp) if replication.translated_tp is not None else None,
        "entry_price": None,                          # market order
        "is_demo": is_demo,
        "execution_mode": execution_mode,             # 'SHADOW' suppresses; 'DEMO' is a real order
        "comment": f"CPY{replication.id}",            # copier provenance (cycle-prevention + reconcile)
        "signal_source": "copier",
        "max_lot": (str(max_lot) if max_lot is not None else None),   # source/follower cap for the bridge
        "copier_replication_id": replication.id,
    }


def produce_one(replication: CopierReplication) -> dict:
    """Produce (at most) one enqueue-only ExecutionJob for a single OPEN intent. Gated + idempotent.

    P0-A: the producer flag is enforced at entry AND re-checked under the lock; the mode is captured once.
    P0-B: ALL admission is re-run INSIDE the lock on the freshly re-read, locked replication."""
    if not copier_execute_enabled():
        return {"replication_id": replication.id, "produced": False, "reason": "producer_disabled"}

    ok, reason = admit_open(replication)              # cheap preflight (re-checked under the lock)
    if not ok:
        return {"replication_id": replication.id, "produced": False, "reason": reason}

    # Preflight ARM read is a HINT only; the AUTHORITATIVE value is re-read INSIDE the lock (below) so a flag
    # flipped off between here and job creation yields a SHADOW job, never a stale mutation-capable one.
    armed_preflight = copier_execute_demo_armed()

    try:
        with transaction.atomic():
            locked = (CopierReplication.objects
                      .select_for_update()
                      .select_related("follower", "follower__follower_account", "master_event")
                      .get(pk=replication.pk))
            # P0-A re-check under the lock: the producer flag must still be on.
            if not copier_execute_enabled():
                return {"replication_id": replication.id, "produced": False, "reason": "producer_disabled"}
            # F1 (P0-B): lock AND freshly re-read the follower ROW so a disable/rebind committed during
            # promotion is seen and fails closed (the select_related relation alone would be a stale cache).
            # Lock ONLY the follower row (of="self"); the nullable broker_server join cannot be FOR UPDATE'd.
            fresh_follower = (CopierFollower.objects
                              .select_for_update(of=("self",))
                              .select_related("follower_account", "follower_account__broker_server",
                                              "master__trading_account")
                              .get(pk=locked.follower_id))
            locked.follower = fresh_follower
            # P0-B: re-validate EVERYTHING on the locked row (follower/account/owner/binding/staleness/
            # protection/fresh constraints/symbol-map) — a disable/rebind/supersede/revoke loses.
            ok, reason = admit_open(locked)
            if not ok:
                return {"replication_id": replication.id, "produced": False, "reason": reason}

            # AUTHORITATIVE in-lock ARM re-read — a flip-off after the preflight yields SHADOW, no real order.
            armed = copier_execute_demo_armed()
            job_type = ExecutionJob.JobType.PLACE_ORDER if armed else ExecutionJob.JobType.PLACE_ORDER_SHADOW
            mode = "DEMO" if armed else "SHADOW"
            if armed_preflight and not armed:
                logger.info("copier OPEN replication %s: arming flipped off before the locked decision -> SHADOW",
                            replication.id)

            acct = locked.follower.follower_account
            constraints = _broker_constraints(acct, locked.follower_symbol)
            sr = _fresh_open_size(locked, constraints)
            if not sr.ok:
                return {"replication_id": replication.id, "produced": False, "reason": f"fresh_size:{sr.detail}"}
            # P0-C: never silently shrink the requested trade — a clamp-to-cap is a material mismatch.
            if sr.detail == "clamped_to_max":
                return {"replication_id": replication.id, "produced": False, "reason": "volume_would_be_clamped"}
            if not (sr.volume and sr.volume > 0):
                return {"replication_id": replication.id, "produced": False, "reason": "non_positive_volume"}

            max_lot = _max_lot(constraints, locked.follower)
            job = ExecutionJob.objects.create(
                account=acct, job_type=job_type,
                payload=_payload(locked, sr.volume, max_lot, is_demo=True, execution_mode=mode),
                hosted_idempotency_key=f"copier:{locked.pk}")
            # P1: a SHADOW job is NOT an execution request.
            locked.execution_job = job
            locked.status = (ReplicationStatus.EXECUTION_REQUESTED if armed
                             else ReplicationStatus.SHADOW_JOB_RECORDED)
            locked.target_volume = sr.volume          # the FRESH sized volume actually enqueued
            locked.save(update_fields=["execution_job", "status", "target_volume", "updated_at"])
    except Exception as e:  # noqa: BLE001 — an inherited ExecutionJob.save() gate (or any error) must not
        # sink the batch; the intent is left unpromoted (no partial mutation — the atomic rolled back).
        logger.warning("copier produce blocked for replication %s: %s", replication.id, type(e).__name__)
        return {"replication_id": replication.id, "produced": False, "reason": f"gate:{type(e).__name__}"}

    return {"replication_id": replication.id, "produced": True, "job_id": job.id,
            "job_type": job_type, "armed": armed, "volume": sr.volume,
            "status": ReplicationStatus.EXECUTION_REQUESTED if armed else ReplicationStatus.SHADOW_JOB_RECORDED}


def produce_pending(*, limit: int = 200) -> dict:
    """Produce jobs for pending SHADOW_RECORDED OPEN intents on enabled followers. Gated + fail-closed."""
    if not copier_execute_enabled():
        return {"enabled": False, "produced": 0, "considered": 0}
    qs = (CopierReplication.objects
          .filter(status=ReplicationStatus.SHADOW_RECORDED, execution_job__isnull=True,
                  requested_op="OPEN", follower__enabled=True)
          .select_related("follower", "follower__follower_account", "master_event")
          .order_by("id")[:limit])
    produced = 0
    considered = 0
    for rep in qs:
        considered += 1
        try:
            if produce_one(rep).get("produced"):
                produced += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("copier produce_pending error on replication %s: %s", rep.id, e)
    return {"enabled": True, "armed": copier_execute_demo_armed(), "considered": considered, "produced": produced}


# =============================================================================
# Full-CLOSE producer (ADR-0052, Workstream B increment 1) — DEMO-only, SHADOW-by-default.
# Master FULL_CLOSE -> follower FULL CLOSE of the VERIFIED copier-owned follower ticket. Never by symbol.
# =============================================================================

def _matched_open_for_close(close_replication: CopierReplication) -> CopierReplication | None:
    """The reconciled OPEN intent whose follower position this CLOSE targets.

    Resolved by the master POSITION identity (same master, same master position_ticket, same follower),
    NEVER by symbol. Requires a MATCHED reconciliation and a concrete ``follower_ticket`` — an
    unreconciled / AMBIGUOUS / missing OPEN yields None and the CLOSE fails closed."""
    from trade_copier.reconcile import MATCHED
    ev = close_replication.master_event
    return (CopierReplication.objects
            .filter(follower_id=close_replication.follower_id, requested_op="OPEN",
                    reconciliation_status=MATCHED, follower_ticket__isnull=False,
                    master_event__master_id=ev.master_id,
                    master_event__position_ticket=ev.position_ticket)
            .order_by("-id").first())


def _close_already_targets(acct_id: int, follower_ticket: int) -> bool:
    """A non-terminal CLOSE_TRADE already targets this (account, ticket) -> don't duplicate (idempotent).

    Account-scoped: MT5 position tickets are unique per account, not globally."""
    return ExecutionJob.objects.filter(
        job_type=ExecutionJob.JobType.CLOSE_TRADE,
        status__in=(ExecutionJob.Status.PENDING, ExecutionJob.Status.RUNNING),
        account_id=acct_id, payload__ticket=follower_ticket,
    ).exists()


def admit_close(replication: CopierReplication) -> tuple[bool, int, str]:
    """Fail-closed admission for producing a DEMO full CLOSE. Returns ``(ok, follower_ticket, reason)``.

    Safe to call before AND inside the lock. A CLOSE requires a VERIFIED copier-owned follower ticket;
    there is no symbol fallback."""
    if replication.execution_job_id is not None:
        return False, 0, "already_produced"
    if replication.status != ReplicationStatus.SHADOW_RECORDED:
        return False, 0, "not_shadow_intent"
    if replication.requested_op != "CLOSE":
        return False, 0, "op_not_close"
    follower = replication.follower
    if not follower.enabled:
        return False, 0, "follower_disabled"

    acct = follower.follower_account
    try:
        from trading.account_policy import is_demo_environment
        if not is_demo_environment(acct):
            return False, 0, "follower_not_demo"
    except Exception:  # noqa: BLE001 — unresolved environment is NOT demo
        return False, 0, "environment_unresolved"
    if not getattr(acct, "is_active", False):
        return False, 0, "account_inactive"

    ok, reason = validate_follower_binding(follower)   # cross-user/self-follow/binding, defense in depth
    if not ok:
        return False, 0, reason

    # The verified copier-owned follower ticket — the ONLY thing a CLOSE may act on.
    open_repl = _matched_open_for_close(replication)
    if open_repl is None or open_repl.follower_ticket is None:
        return False, 0, "no_verified_follower_ticket"
    ticket = int(open_repl.follower_ticket)

    if _close_already_targets(acct.id, ticket):
        return False, 0, "close_already_in_flight"
    return True, ticket, "ok"


def _close_payload(replication, ticket, is_demo, execution_mode) -> dict:
    event = replication.master_event
    return {
        "symbol": replication.follower_symbol or event.symbol,
        "ticket": ticket,                                  # the VERIFIED follower position — never by symbol
        "is_demo": is_demo,
        "execution_mode": execution_mode,                  # 'SHADOW' is recorded intent only (no job)
        "comment": f"CPYC{replication.id}",                # copier close provenance
        "signal_source": "copier",
        "reason": "copier_master_full_close",
        "copier_replication_id": replication.id,
        "copier_open_replication_id": None,                # filled under the lock from the matched OPEN
    }


def produce_close_one(replication: CopierReplication) -> dict:
    """Produce (at most) one CLOSE for a single FULL_CLOSE intent. Gated + idempotent + fail-closed.

    SHADOW (not armed): record the decision on the replication row (``SHADOW_JOB_RECORDED``, intended
    ``follower_ticket``) and create NO ExecutionJob. Armed DEMO: create one ``CLOSE_TRADE`` job targeting
    the verified follower ticket and set ``EXECUTION_REQUESTED``. All admission is re-run under the lock."""
    if not copier_execute_enabled():
        return {"replication_id": replication.id, "produced": False, "reason": "producer_disabled"}

    ok, _ticket, reason = admit_close(replication)        # cheap preflight (re-checked under the lock)
    if not ok:
        return {"replication_id": replication.id, "produced": False, "reason": reason}

    armed_preflight = copier_execute_demo_armed()         # HINT only; authoritative re-read is in-lock

    try:
        with transaction.atomic():
            locked = (CopierReplication.objects
                      .select_for_update()
                      .select_related("follower", "master_event")
                      .get(pk=replication.pk))
            if not copier_execute_enabled():
                return {"replication_id": replication.id, "produced": False, "reason": "producer_disabled"}
            # Re-read + lock the follower row so a disable/rebind committed mid-flight is seen (fail closed).
            fresh_follower = (CopierFollower.objects
                              .select_for_update(of=("self",))
                              .select_related("follower_account", "follower_account__broker_server",
                                              "master__trading_account")
                              .get(pk=locked.follower_id))
            locked.follower = fresh_follower
            ok, ticket, reason = admit_close(locked)
            if not ok:
                return {"replication_id": replication.id, "produced": False, "reason": reason}

            # AUTHORITATIVE in-lock ARM re-read — a flip-off after the preflight yields SHADOW, no real order.
            armed = copier_execute_demo_armed()
            mode = "DEMO" if armed else "SHADOW"
            if armed_preflight and not armed:
                logger.info("copier CLOSE replication %s: arming flipped off before the locked decision -> SHADOW",
                            replication.id)

            open_repl = _matched_open_for_close(locked)
            if not armed:
                # SHADOW: no broker contact. Record the intended action on the row only (ADR-0052).
                locked.follower_ticket = ticket
                locked.status = ReplicationStatus.SHADOW_JOB_RECORDED
                locked.reconciliation_status = (open_repl.reconciliation_status if open_repl else "")
                locked.save(update_fields=["follower_ticket", "status", "reconciliation_status", "updated_at"])
                return {"replication_id": replication.id, "produced": True, "job_id": None,
                        "armed": False, "follower_ticket": ticket,
                        "status": ReplicationStatus.SHADOW_JOB_RECORDED}

            acct = locked.follower.follower_account
            payload = _close_payload(locked, ticket, is_demo=True, execution_mode=mode)
            payload["copier_open_replication_id"] = open_repl.id if open_repl else None
            job = ExecutionJob.objects.create(
                account=acct, job_type=ExecutionJob.JobType.CLOSE_TRADE,
                payload=payload, hosted_idempotency_key=f"copier-close:{locked.pk}")
            locked.execution_job = job
            locked.follower_ticket = ticket
            locked.status = ReplicationStatus.EXECUTION_REQUESTED
            locked.save(update_fields=["execution_job", "follower_ticket", "status", "updated_at"])
    except Exception as e:  # noqa: BLE001 — an inherited ExecutionJob.save() gate (or any error) must not
        # sink the batch; the intent is left unpromoted (the atomic rolled back, no partial mutation).
        logger.warning("copier close blocked for replication %s: %s", replication.id, type(e).__name__)
        return {"replication_id": replication.id, "produced": False, "reason": f"gate:{type(e).__name__}"}

    return {"replication_id": replication.id, "produced": True, "job_id": job.id,
            "job_type": ExecutionJob.JobType.CLOSE_TRADE, "armed": True, "follower_ticket": ticket,
            "status": ReplicationStatus.EXECUTION_REQUESTED}


def produce_pending_closes(*, limit: int = 200) -> dict:
    """Produce CLOSEs for pending SHADOW_RECORDED FULL_CLOSE intents on enabled followers. Gated + fail-closed."""
    if not copier_execute_enabled():
        return {"enabled": False, "produced": 0, "considered": 0}
    qs = (CopierReplication.objects
          .filter(status=ReplicationStatus.SHADOW_RECORDED, execution_job__isnull=True,
                  requested_op="CLOSE", follower__enabled=True)
          .select_related("follower", "follower__follower_account", "master_event")
          .order_by("id")[:limit])
    produced = 0
    considered = 0
    for rep in qs:
        considered += 1
        try:
            if produce_close_one(rep).get("produced"):
                produced += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("copier produce_pending_closes error on replication %s: %s", rep.id, e)
    return {"enabled": True, "armed": copier_execute_demo_armed(), "considered": considered, "produced": produced}


# =============================================================================
# SL/TP MODIFY producer (ADR-0053, Workstream B increment 2) — DEMO-only, SHADOW-by-default.
# Master SL/TP set/change/remove -> follower MODIFY_POSITION of the VERIFIED copier-owned follower ticket.
# Never by symbol. A stop is NEVER weakened silently: an SL removal or widening fails closed here (and the
# bridge independently refuses an SL widen at send time).
# =============================================================================

def _sl_weakening_reason(event: MasterEvent) -> str | None:
    """Return a reason when replicating THIS master SL change would WEAKEN the follower's downside
    protection (so the producer fails closed). Only SL changes can weaken a stop; an SL_SET, an SL that
    tightens (moves closer to entry), and every TP_* change are safe. Classified from the master event +
    the authoritative same-poll entry — defense-in-depth to the bridge's refuse-widen backstop."""
    et = event.event_type
    if et == diff.SL_REMOVE:
        return "sl_removal_not_auto_replicated"
    if et == diff.SL_CHANGE:
        new_sl = float(event.new_sl) if event.new_sl is not None else None
        if new_sl is None or abs(new_sl) <= _EPS:
            return "sl_removal_not_auto_replicated"          # a change-to-none is a removal
        prev_sl = float(event.prev_sl) if event.prev_sl is not None else None
        snap = _master_position_from_event(event)
        entry = float(snap.price_open) if (snap is not None and _has(snap.price_open)) else None
        if entry is None or prev_sl is None or abs(prev_sl) <= _EPS:
            return "sl_change_unverifiable"                  # cannot prove it is not a weaken -> fail closed
        if abs(entry - new_sl) > abs(entry - prev_sl) + _EPS:
            return "sl_widening_not_auto_replicated"         # stop moved FURTHER from entry = more risk
    return None


def _modify_already_targets(acct_id: int, follower_ticket: int) -> bool:
    """A non-terminal MODIFY_POSITION already targets this (account, ticket) -> don't duplicate."""
    return ExecutionJob.objects.filter(
        job_type=ExecutionJob.JobType.MODIFY_POSITION,
        status__in=(ExecutionJob.Status.PENDING, ExecutionJob.Status.RUNNING),
        account_id=acct_id, payload__ticket=follower_ticket,
    ).exists()


def admit_modify(replication: CopierReplication) -> tuple[bool, int, str]:
    """Fail-closed admission for producing a DEMO SL/TP MODIFY. Returns ``(ok, follower_ticket, reason)``.

    Requires a VERIFIED copier-owned follower ticket (never by symbol) AND that the change does not weaken
    the follower's stop. Safe to call before AND inside the lock."""
    if replication.execution_job_id is not None:
        return False, 0, "already_produced"
    if replication.status != ReplicationStatus.SHADOW_RECORDED:
        return False, 0, "not_shadow_intent"
    if replication.requested_op != "MODIFY_SLTP":
        return False, 0, "op_not_modify"
    follower = replication.follower
    if not follower.enabled:
        return False, 0, "follower_disabled"

    acct = follower.follower_account
    try:
        from trading.account_policy import is_demo_environment
        if not is_demo_environment(acct):
            return False, 0, "follower_not_demo"
    except Exception:  # noqa: BLE001 — unresolved environment is NOT demo
        return False, 0, "environment_unresolved"
    if not getattr(acct, "is_active", False):
        return False, 0, "account_inactive"

    ok, reason = validate_follower_binding(follower)   # cross-user/self-follow/binding, defense in depth
    if not ok:
        return False, 0, reason

    # COMPLETE translated SL+TP required (BLOCKER-3: a MODIFY carries the whole desired state, not a delta).
    if not replication.sltp_translation_ok:
        return False, 0, "sltp_incomplete"

    # A stop is NEVER weakened silently — an SL removal/widening fails closed (deferred capability).
    wr = _sl_weakening_reason(replication.master_event)
    if wr is not None:
        return False, 0, wr

    open_repl = _matched_open_for_close(replication)   # position-keyed (NOT symbol): the matched OPEN ticket
    if open_repl is None or open_repl.follower_ticket is None:
        return False, 0, "no_verified_follower_ticket"
    ticket = int(open_repl.follower_ticket)

    if _modify_already_targets(acct.id, ticket):
        return False, 0, "modify_already_in_flight"
    return True, ticket, "ok"


def _modify_payload(replication, ticket, is_demo, execution_mode) -> dict:
    event = replication.master_event
    return {
        "symbol": replication.follower_symbol or event.symbol,
        "ticket": ticket,                                  # the VERIFIED follower position — never by symbol
        "sl": (str(replication.translated_sl) if replication.translated_sl is not None else None),
        "tp": (str(replication.translated_tp) if replication.translated_tp is not None else None),
        "is_demo": is_demo,
        "execution_mode": execution_mode,                  # 'SHADOW' is recorded intent only (no job)
        "comment": f"CPYM{replication.id}",                # copier modify provenance
        "signal_source": "copier",
        "reason": "copier_master_sltp_modify",
        "copier_replication_id": replication.id,
    }


def produce_modify_one(replication: CopierReplication) -> dict:
    """Produce (at most) one SL/TP MODIFY for a single MODIFY_SLTP intent. Gated + idempotent + fail-closed.

    SHADOW (not armed): record the decision on the replication row and create NO ExecutionJob. Armed DEMO:
    create one ``MODIFY_POSITION`` job targeting the verified follower ticket. All admission re-run under the
    lock (a stop weaken or a lost/rebound follower loses)."""
    if not copier_execute_enabled():
        return {"replication_id": replication.id, "produced": False, "reason": "producer_disabled"}

    ok, _ticket, reason = admit_modify(replication)       # cheap preflight (re-checked under the lock)
    if not ok:
        return {"replication_id": replication.id, "produced": False, "reason": reason}

    armed_preflight = copier_execute_demo_armed()         # HINT only; authoritative re-read is in-lock

    try:
        with transaction.atomic():
            locked = (CopierReplication.objects
                      .select_for_update()
                      .select_related("follower", "master_event")
                      .get(pk=replication.pk))
            if not copier_execute_enabled():
                return {"replication_id": replication.id, "produced": False, "reason": "producer_disabled"}
            fresh_follower = (CopierFollower.objects
                              .select_for_update(of=("self",))
                              .select_related("follower_account", "follower_account__broker_server",
                                              "master__trading_account")
                              .get(pk=locked.follower_id))
            locked.follower = fresh_follower
            ok, ticket, reason = admit_modify(locked)
            if not ok:
                return {"replication_id": replication.id, "produced": False, "reason": reason}

            # AUTHORITATIVE in-lock ARM re-read — a flip-off after the preflight yields SHADOW, no real order.
            armed = copier_execute_demo_armed()
            mode = "DEMO" if armed else "SHADOW"
            if armed_preflight and not armed:
                logger.info("copier MODIFY replication %s: arming flipped off before the locked decision -> SHADOW",
                            replication.id)

            if not armed:
                # SHADOW: no broker contact. Record the intended action on the row only.
                locked.follower_ticket = ticket
                locked.status = ReplicationStatus.SHADOW_JOB_RECORDED
                locked.save(update_fields=["follower_ticket", "status", "updated_at"])
                return {"replication_id": replication.id, "produced": True, "job_id": None,
                        "armed": False, "follower_ticket": ticket,
                        "status": ReplicationStatus.SHADOW_JOB_RECORDED}

            acct = locked.follower.follower_account
            job = ExecutionJob.objects.create(
                account=acct, job_type=ExecutionJob.JobType.MODIFY_POSITION,
                payload=_modify_payload(locked, ticket, is_demo=True, execution_mode=mode),
                hosted_idempotency_key=f"copier-modify:{locked.pk}")
            locked.execution_job = job
            locked.follower_ticket = ticket
            locked.status = ReplicationStatus.EXECUTION_REQUESTED
            locked.save(update_fields=["execution_job", "follower_ticket", "status", "updated_at"])
    except Exception as e:  # noqa: BLE001 — an inherited ExecutionJob.save() gate (or any error) must not
        logger.warning("copier modify blocked for replication %s: %s", replication.id, type(e).__name__)
        return {"replication_id": replication.id, "produced": False, "reason": f"gate:{type(e).__name__}"}

    return {"replication_id": replication.id, "produced": True, "job_id": job.id,
            "job_type": ExecutionJob.JobType.MODIFY_POSITION, "armed": True, "follower_ticket": ticket,
            "status": ReplicationStatus.EXECUTION_REQUESTED}


def produce_pending_modifies(*, limit: int = 200) -> dict:
    """Produce MODIFYs for pending SHADOW_RECORDED MODIFY_SLTP intents on enabled followers. Gated + fail-closed."""
    if not copier_execute_enabled():
        return {"enabled": False, "produced": 0, "considered": 0}
    qs = (CopierReplication.objects
          .filter(status=ReplicationStatus.SHADOW_RECORDED, execution_job__isnull=True,
                  requested_op="MODIFY_SLTP", follower__enabled=True)
          .select_related("follower", "follower__follower_account", "master_event")
          .order_by("id")[:limit])
    produced = 0
    considered = 0
    for rep in qs:
        considered += 1
        try:
            if produce_modify_one(rep).get("produced"):
                produced += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("copier produce_pending_modifies error on replication %s: %s", rep.id, e)
    return {"enabled": True, "armed": copier_execute_demo_armed(), "considered": considered, "produced": produced}


# =============================================================================
# Partial-CLOSE producer (ADR-0055, Workstream B increment 3) — DEMO-only, SHADOW-by-default.
# Master PARTIAL_CLOSE -> PROPORTIONAL follower reduction of the VERIFIED copier-owned ticket via a DISTINCT
# PARTIAL_CLOSE_TRADE job. Never by symbol; never a full close; fresh authoritative follower volume; broker
# min/step; round DOWN so rounding never over-reduces; a sub-min reduction/remainder fails closed (no
# full-close substitution policy exists in this increment).
# =============================================================================

def _partial_reduction(master_before, master_after, current_volume, constraints) -> tuple[bool, float, float, str]:
    """PURE proportional-reduction calc. Returns ``(ok, reduction, remaining, reason)``.

    reduction = floor((before-after)/before * current / step) * step  (round DOWN — never over-reduce).
    Fail closed on an invalid master ratio, unavailable constraints, a zero/below-min reduction, a reduction
    that is not strictly less than the current volume (would be a full close), or a sub-min remainder."""
    import math
    mb, ma, cur = float(master_before), float(master_after), float(current_volume)
    if mb <= 0 or ma < 0 or ma >= mb:
        return False, 0.0, 0.0, "master_ratio_invalid"
    ratio = (mb - ma) / mb
    if not (0.0 < ratio < 1.0):
        return False, 0.0, 0.0, "master_ratio_invalid"
    if cur <= 0:
        return False, 0.0, 0.0, "follower_volume_invalid"
    step = float(constraints.lot_step or 0)
    vmin = float(constraints.min_volume or 0)
    if step <= 0 or vmin <= 0:
        return False, 0.0, 0.0, "constraints_unavailable"
    raw = ratio * cur
    if raw <= 0:
        return False, 0.0, 0.0, "zero_requested"
    reduction = round(math.floor((raw / step) + 1e-9) * step, 8)
    if reduction <= 0:
        return False, 0.0, 0.0, "rounds_to_zero"
    if reduction < vmin:
        return False, 0.0, 0.0, "below_min_reduction"
    if reduction >= cur:
        return False, 0.0, 0.0, "would_be_full_close"
    remaining = round(cur - reduction, 8)
    if remaining < vmin:
        return False, 0.0, 0.0, "remainder_below_min"
    return True, reduction, remaining, "ok"


def admit_partial_close(replication: CopierReplication, *, fetch=None) -> tuple[bool, int, float, float, str]:
    """Fail-closed admission for a DEMO partial close. Returns ``(ok, ticket, reduction, remaining, reason)``.

    Requires a VERIFIED copier-owned follower ticket (never by symbol), a FRESH identity-firewalled follower
    read (ticket still open, symbol consistent), and a valid proportional reduction against FRESH broker
    constraints + the authoritative CURRENT follower volume."""
    from trade_copier.reconcile import read_follower_position
    from trade_copier.reconcile import _fetch_account_positions
    fetch = fetch or _fetch_account_positions

    if replication.execution_job_id is not None:
        return False, 0, 0.0, 0.0, "already_produced"
    if replication.status != ReplicationStatus.SHADOW_RECORDED:
        return False, 0, 0.0, 0.0, "not_shadow_intent"
    if replication.requested_op != "PARTIAL_CLOSE":
        return False, 0, 0.0, 0.0, "op_not_partial_close"
    follower = replication.follower
    if not follower.enabled:
        return False, 0, 0.0, 0.0, "follower_disabled"

    acct = follower.follower_account
    try:
        from trading.account_policy import is_demo_environment
        if not is_demo_environment(acct):
            return False, 0, 0.0, 0.0, "follower_not_demo"
    except Exception:  # noqa: BLE001 — unresolved environment is NOT demo
        return False, 0, 0.0, 0.0, "environment_unresolved"
    if not getattr(acct, "is_active", False):
        return False, 0, 0.0, 0.0, "account_inactive"

    ok, reason = validate_follower_binding(follower)
    if not ok:
        return False, 0, 0.0, 0.0, reason

    open_repl = _matched_open_for_close(replication)   # position-keyed (NOT symbol): the matched OPEN ticket
    if open_repl is None or open_repl.follower_ticket is None:
        return False, 0, 0.0, 0.0, "no_verified_follower_ticket"
    ticket = int(open_repl.follower_ticket)

    # FRESH identity-firewalled follower read: ticket still open + symbol consistent + authoritative volume.
    ok_read, pos, read_reason = read_follower_position(follower, ticket, fetch=fetch)
    if not ok_read:
        return False, ticket, 0.0, 0.0, f"follower_read:{read_reason}"
    if pos is None:
        return False, ticket, 0.0, 0.0, "follower_ticket_not_open"
    if replication.follower_symbol and str(pos.symbol) != str(replication.follower_symbol):
        return False, ticket, 0.0, 0.0, "symbol_changed"

    constraints = _broker_constraints(acct, replication.follower_symbol or pos.symbol)
    if not constraints.authoritative:
        return False, ticket, 0.0, 0.0, "constraints_not_authoritative"

    event = replication.master_event
    ok_calc, reduction, remaining, calc_reason = _partial_reduction(
        event.volume_before, event.volume_after, pos.volume, constraints)
    if not ok_calc:
        return False, ticket, 0.0, 0.0, calc_reason

    if _partial_close_already_targets(acct.id, ticket):
        return False, ticket, 0.0, 0.0, "partial_close_already_in_flight"
    return True, ticket, reduction, remaining, "ok"


def _partial_close_already_targets(acct_id: int, follower_ticket: int) -> bool:
    """A non-terminal PARTIAL_CLOSE_TRADE already targets this (account, ticket) -> don't duplicate."""
    return ExecutionJob.objects.filter(
        job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE,
        status__in=(ExecutionJob.Status.PENDING, ExecutionJob.Status.RUNNING),
        account_id=acct_id, payload__ticket=follower_ticket,
    ).exists()


def _partial_close_payload(replication, ticket, reduction, remaining, is_demo, execution_mode) -> dict:
    event = replication.master_event
    return {
        "symbol": replication.follower_symbol or event.symbol,
        "ticket": ticket,                                  # EXPLICIT verified ticket — never by symbol
        "volume": str(reduction),                          # EXPLICIT partial volume — bridge floats + re-normalizes
        "is_demo": is_demo,
        "execution_mode": execution_mode,                  # 'SHADOW' is recorded intent only (no job)
        "comment": f"CPYP{replication.id}",                # copier partial-close provenance
        "signal_source": "copier",
        "reason": "copier_master_partial_close",
        "copier_replication_id": replication.id,
        "expected_remaining": str(remaining),
    }


def produce_partial_close_one(replication: CopierReplication, *, fetch=None) -> dict:
    """Produce (at most) one partial close for a PARTIAL_CLOSE intent. Gated + idempotent + fail-closed.

    SHADOW (default): record the computed reduction on the row, create NO ExecutionJob. Armed DEMO: create one
    ``PARTIAL_CLOSE_TRADE`` job. All admission (incl. the fresh follower read + proportional calc) re-run under
    the lock."""
    if not copier_execute_enabled():
        return {"replication_id": replication.id, "produced": False, "reason": "producer_disabled"}

    ok, _ticket, _reduction, _remaining, reason = admit_partial_close(replication, fetch=fetch)
    if not ok:
        return {"replication_id": replication.id, "produced": False, "reason": reason}

    # Preflight ARM read is a cheap hint ONLY and must NOT govern the job decision — the authoritative value
    # is re-read INSIDE the lock below, so a flag flipped off between here and the locked decision yields
    # SHADOW + no job.
    armed_preflight = copier_execute_demo_armed()

    try:
        with transaction.atomic():
            locked = (CopierReplication.objects
                      .select_for_update()
                      .select_related("follower", "master_event")
                      .get(pk=replication.pk))
            if not copier_execute_enabled():
                return {"replication_id": replication.id, "produced": False, "reason": "producer_disabled"}
            fresh_follower = (CopierFollower.objects
                              .select_for_update(of=("self",))
                              .select_related("follower_account", "follower_account__broker_server",
                                              "master__trading_account")
                              .get(pk=locked.follower_id))
            locked.follower = fresh_follower
            ok, ticket, reduction, remaining, reason = admit_partial_close(locked, fetch=fetch)
            if not ok:
                return {"replication_id": replication.id, "produced": False, "reason": reason}

            # P1 — AUTHORITATIVE ARM re-read INSIDE the lock, immediately before the job decision. If DEMO
            # arming was disabled between the preflight hint and here, record SHADOW only and create NO
            # mutation-capable job. The payload's execution mode derives from THIS in-lock value.
            armed = copier_execute_demo_armed()
            mode = "DEMO" if armed else "SHADOW"
            if not armed:
                if armed_preflight:
                    logger.info("copier partial-close replication %s: arming flipped off between preflight "
                                "and the locked decision -> SHADOW, no job", replication.id)
                locked.follower_ticket = ticket
                locked.target_volume = _dec(reduction)     # the intended partial reduction (SHADOW record)
                locked.status = ReplicationStatus.SHADOW_JOB_RECORDED
                locked.save(update_fields=["follower_ticket", "target_volume", "status", "updated_at"])
                return {"replication_id": replication.id, "produced": True, "job_id": None, "armed": False,
                        "follower_ticket": ticket, "reduction": reduction, "remaining": remaining,
                        "status": ReplicationStatus.SHADOW_JOB_RECORDED}

            acct = locked.follower.follower_account
            job = ExecutionJob.objects.create(
                account=acct, job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE,
                # ADR-0056 HIGH-1: snapshot the account's node so the dedicated consumer's node-authorization
                # gate is satisfiable (every other seam snapshots it; the partial-close producer previously
                # omitted it, leaving the job NULL-node and unroutable to a node-aware worker).
                terminal_node_id=getattr(acct, "terminal_node_id", None),
                payload=_partial_close_payload(locked, ticket, reduction, remaining, is_demo=True,
                                               execution_mode=mode),
                hosted_idempotency_key=f"copier-pclose:{locked.pk}")
            locked.execution_job = job
            locked.follower_ticket = ticket
            locked.target_volume = _dec(reduction)
            locked.status = ReplicationStatus.EXECUTION_REQUESTED
            locked.save(update_fields=["execution_job", "follower_ticket", "target_volume", "status", "updated_at"])
    except Exception as e:  # noqa: BLE001 — an inherited ExecutionJob.save() gate (or any error) must not sink
        logger.warning("copier partial-close blocked for replication %s: %s", replication.id, type(e).__name__)
        return {"replication_id": replication.id, "produced": False, "reason": f"gate:{type(e).__name__}"}

    return {"replication_id": replication.id, "produced": True, "job_id": job.id,
            "job_type": ExecutionJob.JobType.PARTIAL_CLOSE_TRADE, "armed": True, "follower_ticket": ticket,
            "reduction": reduction, "remaining": remaining, "status": ReplicationStatus.EXECUTION_REQUESTED}


def produce_pending_partial_closes(*, limit: int = 200, fetch=None) -> dict:
    """Produce partial closes for pending SHADOW_RECORDED PARTIAL_CLOSE intents. Gated + fail-closed."""
    if not copier_execute_enabled():
        return {"enabled": False, "produced": 0, "considered": 0}
    qs = (CopierReplication.objects
          .filter(status=ReplicationStatus.SHADOW_RECORDED, execution_job__isnull=True,
                  requested_op="PARTIAL_CLOSE", follower__enabled=True)
          .select_related("follower", "follower__follower_account", "master_event")
          .order_by("id")[:limit])
    produced = 0
    considered = 0
    for rep in qs:
        considered += 1
        try:
            if produce_partial_close_one(rep, fetch=fetch).get("produced"):
                produced += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("copier produce_pending_partial_closes error on replication %s: %s", rep.id, e)
    return {"enabled": True, "armed": copier_execute_demo_armed(), "considered": considered, "produced": produced}
