"""trade_copier.operator_view — the READ-ONLY operator lifecycle/failure view service (ADR-0083 Phase-3).

A truthful, redacted, data-quality-labelled summary of each ``(follower, master, master-ticket)`` lifecycle for
GuvFX operators. It READS the existing durable rows (``CopierPositionLifecycle`` + its transitions, the matched
OPEN ``CopierReplication``, the authoritative ``MasterEvent`` ledger, and the governing
``CopierLiveAuthorization``) and NOTHING ELSE: it creates no job, places/modifies/closes no order, arms nothing,
and takes no authorization action. It is DARK (gated on ``copier_operator_view_enabled``).

Truthfulness invariants (asserted by tests):
  * Missing/unreadable data is NEVER rendered as zero / closed / successful — it is labelled UNKNOWN/UNAVAILABLE
    and ``exposure_may_exist`` stays conservatively True.
  * ``follower_matches_master`` is True ONLY with confirmed reconcile evidence (MATCHED + a follower ticket) —
    never inferred from silence.
  * Secrets, raw broker logins, full tickets and account numbers are REDACTED. Reason codes are stable.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

# Data-quality labels (stable machine-readable codes).
DQ_VERIFIED = "VERIFIED"
DQ_PARTIAL = "PARTIAL"
DQ_STALE = "STALE"
DQ_UNKNOWN = "UNKNOWN"
DQ_UNAVAILABLE = "UNAVAILABLE"

# A non-terminal lifecycle not observed within this window is STALE (bridge/observer evidence has aged).
_STALE_AFTER_SECONDS = 15 * 60
_MAX_PAGE = 200

# Sentinel so a prefetched value of None ("no such row") is distinguishable from "not provided (query it)".
_UNSET = object()


def _redact(value, keep: int = 4) -> str | None:
    """Redact a broker login / ticket / account number to a masked tail (e.g. '…6411'). None/empty stay None.
    A short value (length <= keep) is NEVER shown whole — it collapses to a constant mask, so the contract 'no
    full identifier appears anywhere' holds even for short ids."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) <= keep:
        return "…"            # never echo a short identifier in full
    return "…" + s[-keep:]


def _exposure_may_exist(state: str, *, reconciled_matched: bool, attempted: bool) -> bool:
    """A follower position MAY still exist unless we have confirmed it does NOT. Decided from EVIDENCE, never
    from the state label alone: a confirmed reconcile match or an armed (unconfirmed-fill) attempt means a
    position may exist even under a 'failed' terminal (a link confirmed after the entry deadline reaches
    ENTRY_FAILED_FINAL while the follower position is genuinely open). Only a confirmed full CLOSE, or a terminal
    with NO attempt and NO match, means no exposure."""
    from trade_copier.lifecycle import LifecycleState
    if state == LifecycleState.CLOSED.value:
        return False                              # confirmed full close — the position is gone
    if reconciled_matched or attempted:
        return True                               # evidence of a (possibly) open follower position
    if state in (LifecycleState.ENTRY_FAILED_FINAL.value, LifecycleState.OBSERVED_OPEN.value):
        return False                              # no attempt, no match -> never entered
    # ENTRY_PENDING / LINKED / MODIFY_PENDING / CLOSE_PENDING / CONTAINED_FAILURE ⇒ assume exposure may exist.
    return True


def _data_quality(row, *, reconciled_matched: bool, attempted: bool, now) -> str:
    from trade_copier.lifecycle import LifecycleState
    state = row.state
    if state == LifecycleState.CONTAINED_FAILURE.value:
        return DQ_UNKNOWN            # divergence/ambiguity — operator must establish ground truth
    if state == LifecycleState.CLOSED.value and row.closed_at is not None:
        return DQ_VERIFIED
    if state == LifecycleState.ENTRY_FAILED_FINAL.value:
        # VERIFIED (a truthful "no position") ONLY when nothing indicates a follower position. If the entry
        # actually reconciled MATCHED (linked after the deadline) or an unconfirmed attempt exists, the terminal
        # CONTRADICTS the evidence — surface UNKNOWN for investigation, never "verified no exposure".
        return DQ_UNKNOWN if (reconciled_matched or attempted) else DQ_VERIFIED
    last_seen = row.last_transition_at or row.open_observed_at
    if not row.is_terminal and last_seen is not None and (now - last_seen).total_seconds() > _STALE_AFTER_SECONDS:
        return DQ_STALE
    if state in (LifecycleState.LINKED.value, LifecycleState.MODIFY_PENDING.value,
                 LifecycleState.CLOSE_PENDING.value):
        return DQ_VERIFIED if reconciled_matched else DQ_PARTIAL
    if state in (LifecycleState.OBSERVED_OPEN.value, LifecycleState.ENTRY_PENDING.value):
        return DQ_PARTIAL
    return DQ_UNKNOWN


def build_operator_summary(row, *, now=None, open_repl=_UNSET, last_event=_UNSET, authz=_UNSET) -> dict:
    """Build the redacted, truthful operator summary for ONE lifecycle row. Reads related rows READ-ONLY.

    ``open_repl`` / ``last_event`` / ``authz`` may be passed in (the batched list path prefetches them for the
    whole page to bound query cost); when left unset each is queried for this row (standalone use)."""
    from trade_copier.lifecycle import LifecycleState
    from trade_copier.models import CopierReplication, MasterEvent, CopierLiveAuthorization
    from trade_copier.reconcile import MATCHED
    now = now or timezone.now()

    master = row.master
    follower = row.follower
    macct = getattr(master, "trading_account", None)
    facct = getattr(follower, "follower_account", None)

    # The matched OPEN replication for this ticket (confirmed follower ticket + requested/confirmed volume).
    if open_repl is _UNSET:
        open_repl = (CopierReplication.objects
                     .filter(follower=follower, requested_op="OPEN", master_event__master_id=master.id,
                             master_event__position_ticket=row.master_position_ticket)
                     .order_by("-id").first())
    reconciled_matched = bool(open_repl is not None and open_repl.reconciliation_status == MATCHED
                              and open_repl.follower_ticket is not None)

    # Latest authoritative master event for this ticket (source + revision + last master state).
    if last_event is _UNSET:
        last_event = (MasterEvent.objects
                      .filter(master=master, position_ticket=row.master_position_ticket)
                      .order_by("-sequence").first())

    # Governing LIVE authorization for the pair (expiry/status only — NEVER any secret). None when absent.
    if authz is _UNSET:
        authz = None
        if macct is not None and facct is not None:
            authz = (CopierLiveAuthorization.objects
                     .filter(master_account_id=macct.id, follower_account_id=facct.id, revoked_at__isnull=True)
                     .order_by("-created_at").first())

    attempted = bool(getattr(row, "attempted", False))
    state = row.state
    exposure = _exposure_may_exist(state, reconciled_matched=reconciled_matched, attempted=attempted)
    dq = _data_quality(row, reconciled_matched=reconciled_matched, attempted=attempted, now=now)
    manual = (state == LifecycleState.CONTAINED_FAILURE.value) or (exposure and dq == DQ_UNKNOWN)

    requested_volume = (str(open_repl.target_volume) if (open_repl is not None
                        and open_repl.target_volume is not None) else None)
    # Reconcile MATCHED confirms the follower position EXISTS (a one-to-one provenance-comment match), NOT its
    # fill VOLUME — a partial fill / broker min-volume rounding still reconciles MATCHED. So the requested lot is
    # reported as requested only; ``confirmed_volume`` stays null (never fabricated from the request) until a
    # fill-volume reconcile exists, and ``position_existence_confirmed`` carries the existence evidence.
    confirmed_volume = None
    position_existence_confirmed = reconciled_matched

    return {
        "lifecycle_id": row.id,
        "owner_user_id": getattr(macct, "user_id", None),
        # Redacted identities — internal account ids are shown; broker logins/tickets are masked.
        "master": {
            "account_id": getattr(macct, "id", None),
            "login_redacted": _redact(getattr(macct, "account_number", None)),
            "server": str(getattr(getattr(macct, "broker_server", None), "server_name", "") or "") or None,
        },
        "follower": {
            "account_id": getattr(facct, "id", None),
            "login_redacted": _redact(getattr(facct, "account_number", None)),
            "server": str(getattr(getattr(facct, "broker_server", None), "server_name", "") or "") or None,
            "ticket_redacted": _redact(row.follower_ticket),
        },
        "master_position_ticket_redacted": _redact(row.master_position_ticket),
        "symbol": getattr(last_event, "symbol", None) or getattr(open_repl, "follower_symbol", None),
        "state": state,
        "pending_op": row.pending_op,
        "is_terminal": bool(row.is_terminal),
        # Last confirmed master/follower state.
        "last_master_event": ({"type": last_event.event_type, "sequence": int(last_event.sequence),
                               "source": getattr(last_event, "source", None),
                               "observed_at": (last_event.observed_at.isoformat()
                                               if last_event.observed_at else None)}
                              if last_event is not None else None),
        "follower_link": {
            "reconciliation": (open_repl.reconciliation_status if open_repl is not None else None) or "UNKNOWN",
            "has_confirmed_ticket": reconciled_matched,
        },
        "requested_volume": requested_volume,
        "confirmed_volume": confirmed_volume,              # null until a fill-volume reconcile exists (never 0)
        "position_existence_confirmed": position_existence_confirmed,
        # Truthful posture — never inferred from silence.
        "exposure_may_exist": exposure,
        "manual_investigation_required": bool(manual),
        "follower_matches_master": reconciled_matched,   # True ONLY with confirmed reconcile evidence
        "failure_reason": (row.failure_reason or "") or None,
        # Timestamps.
        "first_observed_at": row.open_observed_at.isoformat() if row.open_observed_at else None,
        "last_observed_at": row.last_transition_at.isoformat() if row.last_transition_at else None,
        "last_confirmed_at": ((row.closed_at or row.linked_at).isoformat()
                              if (row.closed_at or row.linked_at) else None),
        # Authorization expiry/status — NO secret material.
        "authorization": ({"status": authz.status,
                           "expires_at": authz.expires_at.isoformat() if authz.expires_at else None,
                           "expired": bool(authz.expires_at and authz.expires_at <= now),
                           "correlation_id": authz.correlation_id or None}
                          if authz is not None else None),
        "data_quality": dq,
    }


def list_operator_lifecycles(*, account_id=None, state=None, attention_only=False,
                             limit: int = 50, offset: int = 0, now=None) -> dict:
    """Paginated, bounded, READ-ONLY operator listing. Gated: inert (``enabled:false``, no rows) unless
    ``COPIER_ENABLED`` AND ``COPIER_OPERATOR_VIEW_ENABLED``. Never mutates; never contacts a broker."""
    from trade_copier.flags import copier_operator_view_enabled
    if not copier_operator_view_enabled():
        return {"enabled": False, "items": [], "total": 0, "limit": 0, "offset": 0}

    from trade_copier.models import CopierPositionLifecycle
    now = now or timezone.now()
    limit = max(1, min(int(limit or 50), _MAX_PAGE))
    offset = max(0, int(offset or 0))

    qs = (CopierPositionLifecycle.objects
          .select_related("master", "master__trading_account", "master__trading_account__broker_server",
                          "follower", "follower__follower_account", "follower__follower_account__broker_server")
          .order_by("-updated_at"))
    if account_id is not None:
        # Tenant/account scoping: either side of the pair.
        from django.db.models import Q
        qs = qs.filter(Q(master__trading_account_id=int(account_id))
                       | Q(follower__follower_account_id=int(account_id)))
    if state:
        qs = qs.filter(state=str(state))
    if attention_only:
        from trade_copier.lifecycle import LifecycleState
        qs = qs.filter(state=LifecycleState.CONTAINED_FAILURE.value)

    total = qs.count()
    rows = list(qs[offset:offset + limit])

    # Bulk-prefetch the per-row related rows for the WHOLE page (bounds query cost to a small constant per page,
    # not O(rows): one query each for OPEN replications, master events and authorizations).
    from trade_copier.models import CopierReplication, MasterEvent, CopierLiveAuthorization
    m_ids = {r.master_id for r in rows}
    f_ids = {r.follower_id for r in rows}
    tickets = {r.master_position_ticket for r in rows}
    macct_ids = {r.master.trading_account_id for r in rows if getattr(r.master, "trading_account_id", None)}
    facct_ids = {r.follower.follower_account_id for r in rows if getattr(r.follower, "follower_account_id", None)}

    opens: dict = {}
    if rows:
        for rep in (CopierReplication.objects
                    .filter(requested_op="OPEN", follower_id__in=f_ids,
                            master_event__master_id__in=m_ids, master_event__position_ticket__in=tickets)
                    .select_related("master_event").order_by("id")):
            # highest id wins (matches .order_by("-id").first())
            opens[(rep.follower_id, rep.master_event.master_id, rep.master_event.position_ticket)] = rep
    events: dict = {}
    if rows:
        for ev in (MasterEvent.objects.filter(master_id__in=m_ids, position_ticket__in=tickets)
                   .order_by("sequence")):
            events[(ev.master_id, ev.position_ticket)] = ev   # highest sequence wins
    authzs: dict = {}
    if macct_ids and facct_ids:
        for az in (CopierLiveAuthorization.objects
                   .filter(master_account_id__in=macct_ids, follower_account_id__in=facct_ids,
                           revoked_at__isnull=True).order_by("created_at")):
            authzs[(az.master_account_id, az.follower_account_id)] = az   # highest created_at wins

    items = []
    for r in rows:
        macct_id = getattr(r.master, "trading_account_id", None)
        facct_id = getattr(r.follower, "follower_account_id", None)
        items.append(build_operator_summary(
            r, now=now,
            open_repl=opens.get((r.follower_id, r.master_id, r.master_position_ticket)),
            last_event=events.get((r.master_id, r.master_position_ticket)),
            authz=authzs.get((macct_id, facct_id))))
    return {"enabled": True, "items": items, "total": total, "limit": limit, "offset": offset,
            "has_more": (offset + len(items)) < total}
