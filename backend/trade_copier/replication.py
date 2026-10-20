"""trade_copier.replication — the replication INTENT engine (PR B; creates NO ExecutionJobs).

For one ``MasterEvent``, decide for EACH configured follower whether it participates, and record a
single idempotent ``CopierReplication`` row capturing the computed intent (sized volume, resolved
follower symbol, translated SL/TP) — or a REJECTED/SKIPPED outcome with a reason. It places,
modifies or closes NOTHING: the gated DEMO execution-job creation is a separate follow-up.

Invariants (packet §6/§9/§11):
  * Idempotent — at most one CopierReplication per ``(master_event, follower)`` (unique constraint +
    get-or-create), so a re-run never double-replicates.
  * Isolated — one follower's failure never affects another's (per-follower try/except).
  * Cross-user and self-follow are REJECTED fail-closed; a disabled follower takes NO new entries and
    otherwise follows its disable policy (A keep-managing / B detach / C close).
  * Nothing is executed and no LIVE authority is touched.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from trade_copier import diff
from trade_copier.models import (
    CopierReplication,
    DisablePolicy,
    MasterPositionSnapshot,
    ReplicationStatus,
    SizingMode,
    SltpTranslation,
)
from trade_copier.sizing import BrokerConstraints, FollowerSizing, compute_follower_volume
from trade_copier.symbols import resolve_follower_symbol
from trade_copier.translation import translate_sltp

logger = logging.getLogger(__name__)

_OPEN_LIKE = {diff.OPEN, diff.VOLUME_INCREASE}
_CLOSE_LIKE = {diff.FULL_CLOSE, diff.PARTIAL_CLOSE}
_MODIFY_LIKE = {diff.SL_SET, diff.SL_CHANGE, diff.SL_REMOVE,
                diff.TP_SET, diff.TP_CHANGE, diff.TP_REMOVE}

_OP_FOR_EVENT = {
    diff.OPEN: "OPEN",
    diff.VOLUME_INCREASE: "SCALE_IN",
    diff.FULL_CLOSE: "CLOSE",
    diff.PARTIAL_CLOSE: "PARTIAL_CLOSE",
    diff.SL_SET: "MODIFY_SLTP", diff.SL_CHANGE: "MODIFY_SLTP", diff.SL_REMOVE: "MODIFY_SLTP",
    diff.TP_SET: "MODIFY_SLTP", diff.TP_CHANGE: "MODIFY_SLTP", diff.TP_REMOVE: "MODIFY_SLTP",
}


def validate_follower_binding(follower) -> tuple[bool, str]:
    """Structural eligibility (packet §6): not the master itself, same GuvFX owner, valid broker binding."""
    master_acct = follower.master.trading_account
    facct = follower.follower_account
    if facct.id == master_acct.id:
        return False, "self_follow"
    if facct.user_id != master_acct.user_id:
        return False, "cross_user"
    if not (str(getattr(facct, "account_number", "") or "").strip()
            and (facct.broker_server_id or str(getattr(facct, "broker_name", "") or "").strip())):
        return False, "invalid_broker_binding"
    return True, "ok"


def _participation(follower, event_type: str) -> tuple[bool, str]:
    """Does this follower participate in this event? Disabled followers follow their disable policy."""
    if follower.enabled:
        return True, ""
    if event_type in _OPEN_LIKE:
        return False, "disabled_no_new_entries"
    policy = follower.disable_policy
    if policy == DisablePolicy.A_KEEP_MANAGING:
        return True, ""                       # keep managing existing positions
    if policy == DisablePolicy.B_DETACH:
        return False, "detached_manual"       # stop replication; manual management
    if policy == DisablePolicy.C_CLOSE:
        if event_type in _CLOSE_LIKE:
            return True, ""                   # mirror closes only
        return False, "closing_policy_skips_modify"
    return False, "disabled"


def _broker_constraints(account, follower_symbol: str) -> BrokerConstraints:
    """Destination broker lot rules for the resolved symbol, from BrokerInstrument metadata if synced."""
    try:
        from execution.models import BrokerInstrument
        row = (BrokerInstrument.objects
               .filter(account_id=getattr(account, "id", None), broker_symbol=follower_symbol)
               .only("metadata").first())
        md = (row.metadata if row and isinstance(row.metadata, dict) else {}) or {}
        def _get(*keys):
            for k in keys:
                if md.get(k) is not None:
                    return md.get(k)
            return None
        vmin = _get("volume_min", "min_volume")
        vstep = _get("volume_step", "lot_step")
        vmax = _get("volume_max", "max_volume")
        # Authoritative only when the broker's OWN synced metadata provided the critical bounds
        # (min + step). Otherwise the bounds are non-authoritative defaults (BLOCKER-4): fine for a
        # SHADOW intent, never for a real DEMO job.
        authoritative = (row is not None and vmin is not None and vstep is not None)
        return BrokerConstraints(
            min_volume=float(vmin) if vmin is not None else 0.01,
            lot_step=float(vstep) if vstep is not None else 0.01,
            max_volume=float(vmax) if vmax is not None else None,
            authoritative=authoritative,
        )
    except Exception:  # noqa: BLE001 — metadata is best-effort; non-authoritative defaults otherwise
        return BrokerConstraints()  # authoritative=False


def _cfg(follower) -> FollowerSizing:
    return FollowerSizing(
        mode=follower.sizing_mode,
        fixed_lot=(float(follower.fixed_lot) if follower.fixed_lot is not None else None),
        multiplier=(float(follower.multiplier) if follower.multiplier is not None else None),
        risk_per_trade_pct=(float(follower.risk_per_trade_pct)
                            if follower.risk_per_trade_pct is not None else None),
        max_volume=(float(follower.max_volume) if follower.max_volume is not None else None),
    )


def _record(event, follower, op, status, reason="", **fields) -> CopierReplication:
    """Create the single idempotent ledger row for (event, follower). Returns the existing row if a
    concurrent pass already created it (unique constraint)."""
    try:
        with transaction.atomic():
            return CopierReplication.objects.create(
                master_event=event, follower=follower, requested_op=op,
                status=status, reason=reason[:64], **fields)
    except IntegrityError:
        return CopierReplication.objects.get(master_event=event, follower=follower)


def _master_position_from_event(event):
    """The authoritative CURRENT master position state captured in the SAME poll as the event, so
    SL/TP are the COMPLETE desired state (both sides), never a single changed field (BLOCKER-3)."""
    return (MasterPositionSnapshot.objects
            .filter(poll_id=event.from_poll_id, position_ticket=event.position_ticket)
            .first())


def _replicate_one(event, follower) -> CopierReplication | None:
    if CopierReplication.objects.filter(master_event=event, follower=follower).exists():
        return None  # idempotent: already replicated this event to this follower

    # BLOCKER-4: an unknown master event type must FAIL CLOSED — never default to a trading mutation.
    op = _OP_FOR_EVENT.get(event.event_type)
    if op is None:
        return _record(event, follower, "UNKNOWN", ReplicationStatus.REJECTED, "unknown_event_type")

    ok, reason = validate_follower_binding(follower)
    if not ok:
        return _record(event, follower, op, ReplicationStatus.REJECTED, reason)

    participate, skip_reason = _participation(follower, event.event_type)
    if not participate:
        return _record(event, follower, op, ReplicationStatus.SKIPPED, skip_reason)

    sym = resolve_follower_symbol(follower.master.trading_account, event.symbol, follower.follower_account)
    if not sym.ok:
        return _record(event, follower, op, ReplicationStatus.REJECTED, sym.reason)

    target_volume = None
    sizing_detail = ""
    tsl = ttp = None

    if event.event_type in _OPEN_LIKE:
        master_vol = event.volume_after if event.event_type == diff.OPEN else event.volume_delta
        constraints = _broker_constraints(follower.follower_account, sym.follower_symbol)
        sr = compute_follower_volume(master_vol, _cfg(follower), constraints)
        if not sr.ok:
            return _record(event, follower, op, ReplicationStatus.REJECTED, sr.detail,
                           follower_symbol=sym.follower_symbol)
        target_volume = sr.volume
        c_note = "c:auth" if constraints.authoritative else "c:unverified"
        # COMPLETE desired SL+TP from the authoritative same-poll snapshot (fall back to the event's
        # own levels if the snapshot row is missing — an OPEN always carries both).
        snap = _master_position_from_event(event)
        m_sl = _f(snap.sl) if snap is not None else _f(event.new_sl)
        m_tp = _f(snap.tp) if snap is not None else _f(event.new_tp)
        m_entry = _f(snap.price_open) if snap is not None else None
        tr = translate_sltp(follower.sltp_translation, event.side, master_entry=m_entry,
                            master_sl=m_sl, master_tp=m_tp)
        tsl, ttp = tr.sl, tr.tp
        sizing_detail = f"{sr.detail};{c_note};sltp:{tr.reason}"
    elif event.event_type in _MODIFY_LIKE:
        # BLOCKER-3: a modify needs the COMPLETE desired SL+TP from the authoritative same-poll
        # snapshot, NOT the single changed field (an SL-only change carries new_tp=None and would
        # erase an intact TP at MODIFY time). No authoritative state ⇒ do NOT issue a modify.
        snap = _master_position_from_event(event)
        if snap is None:
            return _record(event, follower, op, ReplicationStatus.REJECTED, "master_state_unavailable",
                           follower_symbol=sym.follower_symbol)
        tr = translate_sltp(follower.sltp_translation, event.side, master_entry=_f(snap.price_open),
                            master_sl=_f(snap.sl), master_tp=_f(snap.tp))
        tsl, ttp = tr.sl, tr.tp
        sizing_detail = f"sltp:{tr.reason}"
    # CLOSE/PARTIAL_CLOSE intents reference the master event; follower-position mapping and the
    # actual close volume are resolved at execution time (follower ticket is unknown in PR B).

    return _record(event, follower, op, ReplicationStatus.SHADOW_RECORDED, "",
                   target_volume=target_volume, sizing_detail=sizing_detail[:64],
                   follower_symbol=sym.follower_symbol,
                   translated_sl=_dec(tsl), translated_tp=_dec(ttp))


def _f(v):
    return float(v) if v is not None else None


def _dec(v):
    from decimal import Decimal
    return Decimal(str(v)) if v is not None else None


def replicate_master_event(event) -> dict:
    """Record replication intents for ``event`` across all its master's configured followers."""
    created = 0
    for follower in event.master.followers.all():
        try:
            rep = _replicate_one(event, follower)
            if rep is not None:
                created += 1
        except Exception as e:  # noqa: BLE001 — one follower's failure must not sink the others
            logger.exception("copier replicate failed event=%s follower=%s: %s", event.pk, follower.pk, e)
    return {"event_id": event.pk, "intents": created}


def replicate_pending(*, limit: int = 500) -> dict:
    """Replicate recent MasterEvents that have no replication row yet (idempotent sweep)."""
    from trade_copier.models import MasterEvent
    events = (MasterEvent.objects.filter(replications__isnull=True)
              .order_by("master_id", "sequence")[:limit])
    total = 0
    n = 0
    for ev in events:
        n += 1
        total += replicate_master_event(ev)["intents"]
    return {"events": n, "intents": total}
