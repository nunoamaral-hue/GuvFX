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
        return BrokerConstraints(
            min_volume=float(_get("volume_min", "min_volume") or 0.01),
            lot_step=float(_get("volume_step", "lot_step") or 0.01),
            max_volume=(float(_get("volume_max", "max_volume")) if _get("volume_max", "max_volume") else None),
        )
    except Exception:  # noqa: BLE001 — metadata is best-effort; conservative defaults otherwise
        return BrokerConstraints()


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


def _replicate_one(event, follower) -> CopierReplication | None:
    if CopierReplication.objects.filter(master_event=event, follower=follower).exists():
        return None  # idempotent: already replicated this event to this follower
    op = _OP_FOR_EVENT.get(event.event_type, "MODIFY_SLTP")

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
        sr = compute_follower_volume(master_vol, _cfg(follower),
                                     _broker_constraints(follower.follower_account, sym.follower_symbol))
        if not sr.ok:
            return _record(event, follower, op, ReplicationStatus.REJECTED, sr.detail,
                           follower_symbol=sym.follower_symbol)
        target_volume = sr.volume
        sizing_detail = sr.detail
        tr = translate_sltp(follower.sltp_translation, event.side, master_entry=None,
                            master_sl=_f(event.new_sl), master_tp=_f(event.new_tp))
        tsl, ttp = tr.sl, tr.tp
        if not tr.ok:
            sizing_detail = (sizing_detail + f";sltp:{tr.reason}")[:64]
    elif event.event_type in _MODIFY_LIKE:
        tr = translate_sltp(follower.sltp_translation, event.side, master_entry=None,
                            master_sl=_f(event.new_sl), master_tp=_f(event.new_tp))
        tsl, ttp = tr.sl, tr.tp
        sizing_detail = (f"sltp:{tr.reason}")[:64]
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
