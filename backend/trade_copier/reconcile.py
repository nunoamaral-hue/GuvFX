"""trade_copier.reconcile — follower-ticket reconciliation (ADR-0052, Workstream B increment 1).

Establishes the **verified copier-owned follower-ticket association**: which follower broker ticket a
copied OPEN produced. This is the prerequisite for any follower-position-targeting lifecycle op
(CLOSE/MODIFY), which must NEVER act on a position merely because its symbol resembles the master's.

STRICTLY read-only. For an **armed DEMO OPEN** replication (``status=EXECUTION_REQUESTED`` — a real DEMO
order was requested) whose ``follower_ticket`` is not yet set, this reads the follower account's live
positions through the follower's OWN per-tenant bridge under the snapshot identity firewall (reusing the
observer's audited ``fetch_master_positions`` — a generic identity-verified account-position read) and
matches the position whose ``comment`` equals the producer's deterministic provenance tag
``CPY<replication_id>``:

* exactly one match  ⇒ ``follower_ticket`` set, ``reconciliation_status=MATCHED`` (written once);
* no match (yet)     ⇒ ``UNMATCHED`` — benign, the order may not have filled; ticket left NULL;
* more than one      ⇒ ``AMBIGUOUS`` — fail closed, never pick one; ticket left NULL;
* identity refusal / read error ⇒ NO write at all (never assume what was not observed).

A confirmed ``MATCHED`` ticket is never silently overwritten: once set it is left intact, and a later
disappearance of that ticket from the follower is recorded as ``DIVERGENT_MISSING`` (the ticket value is
kept — append-only in effect — so a subsequent CLOSE still fails closed on a divergent association).

Gated by ``copier_reconcile_enabled()`` (DARK default OFF, ANDed with the master gate). SHADOW
replications have no real order and are never reconciled. Nothing here places, closes or modifies an order.
"""
from __future__ import annotations

import logging

from django.db import transaction

from trade_copier.flags import copier_reconcile_enabled
from trade_copier.models import CopierFollower, CopierReplication, ReplicationStatus
from trade_copier.observer import fetch_master_positions as _fetch_account_positions

logger = logging.getLogger(__name__)

# reconciliation_status values (plain strings; the column is a free CharField — no DB migration needed).
MATCHED = "MATCHED"
UNMATCHED = "UNMATCHED"
AMBIGUOUS = "AMBIGUOUS"
DIVERGENT_MISSING = "DIVERGENT_MISSING"
# Partial-close post-send reconciliation states (ADR-0055). A partial close is NEVER declared terminal from
# the bridge return alone — the authoritative remaining volume is broker state, re-read here.
# NB: values kept <= 24 chars to fit CopierReplication.reconciliation_status (CharField max_length=24).
PARTIAL_RECONCILED = "PCLOSE_RECONCILED"
PARTIAL_DIVERGENT_VOLUME = "PCLOSE_DIVERGENT_VOL"
PARTIAL_DIVERGENT_TICKET_GONE = "PCLOSE_TICKET_GONE"
PARTIAL_RECONCILE_UNAVAILABLE = "PCLOSE_RECON_UNAVAIL"


def _provenance_comment(replication_id: int) -> str:
    """The exact tag the OPEN producer stamps on the follower order (execution_producer._payload)."""
    return f"CPY{replication_id}"


def _unreconciled_open_qs(follower: CopierFollower):
    """Armed-DEMO OPEN intents on this follower that reached the broker and still need a ticket.

    Only ``EXECUTION_REQUESTED`` (a real DEMO ``PLACE_ORDER`` was produced) can have a follower position;
    a SHADOW intent reaches no broker and is never reconciled."""
    return (CopierReplication.objects
            .filter(follower=follower, requested_op="OPEN",
                    status=ReplicationStatus.EXECUTION_REQUESTED,
                    follower_ticket__isnull=True)
            .order_by("id"))


def reconcile_follower(follower: CopierFollower, *, fetch=_fetch_account_positions) -> dict:
    """Reconcile all unreconciled armed-DEMO OPEN intents for ONE follower from a SINGLE position read.

    Read-only + identity-firewalled; fail-closed (a refused/failed read writes nothing). One bridge call
    per follower (not per replication). Each matched ticket is written under a row lock so two passes
    cannot double-assign."""
    pending = list(_unreconciled_open_qs(follower))
    if not pending:
        return {"follower_id": follower.pk, "ok": True, "reason": "nothing_to_reconcile",
                "matched": 0, "ambiguous": 0, "unmatched": 0}

    result = fetch(follower.follower_account)
    if not result.ok:
        # Never assume: a refused identity firewall / read error leaves every intent unreconciled.
        logger.warning("copier reconcile read refused for follower %s: %s", follower.pk, result.reason)
        return {"follower_id": follower.pk, "ok": False, "reason": result.reason,
                "matched": 0, "ambiguous": 0, "unmatched": 0}

    # comment -> [tickets]; a comment appearing on >1 live position is ambiguous.
    by_comment: dict[str, list[int]] = {}
    for p in result.positions:
        by_comment.setdefault((p.comment or ""), []).append(int(p.ticket))

    matched = ambiguous = unmatched = 0
    for rep in pending:
        tickets = by_comment.get(_provenance_comment(rep.id), [])
        with transaction.atomic():
            locked = (CopierReplication.objects.select_for_update().get(pk=rep.pk))
            if locked.follower_ticket is not None:   # another pass won the race — leave it
                continue
            if len(tickets) == 1:
                locked.follower_ticket = tickets[0]
                locked.reconciliation_status = MATCHED
                matched += 1
            elif len(tickets) == 0:
                locked.reconciliation_status = UNMATCHED
                unmatched += 1
            else:
                locked.reconciliation_status = AMBIGUOUS   # fail closed: never pick one
                ambiguous += 1
            locked.save(update_fields=["follower_ticket", "reconciliation_status", "updated_at"])

    return {"follower_id": follower.pk, "ok": True, "reason": "ok",
            "matched": matched, "ambiguous": ambiguous, "unmatched": unmatched,
            "considered": len(pending)}


def reconcile_one(replication: CopierReplication, *, fetch=_fetch_account_positions) -> dict:
    """Reconcile a single replication (targeted / test helper). Delegates the read to the follower batch."""
    if replication.requested_op != "OPEN":
        return {"replication_id": replication.id, "ok": False, "reason": "not_open"}
    if replication.status != ReplicationStatus.EXECUTION_REQUESTED:
        return {"replication_id": replication.id, "ok": False, "reason": "not_executed_demo"}
    if replication.follower_ticket is not None:
        return {"replication_id": replication.id, "ok": True, "reason": "already_reconciled",
                "follower_ticket": replication.follower_ticket}
    summary = reconcile_follower(replication.follower, fetch=fetch)
    replication.refresh_from_db(fields=["follower_ticket", "reconciliation_status"])
    return {"replication_id": replication.id, "ok": summary["ok"], "reason": summary["reason"],
            "follower_ticket": replication.follower_ticket,
            "reconciliation_status": replication.reconciliation_status}


def read_follower_position(follower: CopierFollower, ticket: int, *, fetch=_fetch_account_positions):
    """Read ONE follower position (by broker ticket) through the identity firewall. READ-ONLY.

    Returns ``(ok, position_or_None, reason)`` where ``position`` is the live ``ObservedPosition`` (carrying
    the authoritative CURRENT volume + symbol) for ``ticket``, or None when the read was refused/failed or
    the ticket is no longer open. Used by the partial-close producer (authoritative current volume for the
    proportional calc) and by remaining-volume reconciliation after a partial close. Never mutates."""
    result = fetch(follower.follower_account)
    if not result.ok:
        return False, None, result.reason
    for p in result.positions:
        if int(p.ticket) == int(ticket):
            return True, p, "ok"
    return True, None, "ticket_not_open"


def reconcile_partial_close(replication: CopierReplication, intended_remaining, *, step: float,
                            fetch=_fetch_account_positions) -> dict:
    """Post-send reconciliation for a produced partial close (ADR-0055). READ-ONLY; NO retry.

    Re-reads the follower position (authoritative) for the replication's ``follower_ticket`` and compares the
    actual remaining volume against the intended remaining, with broker-step tolerance. Writes a terminal
    reconciliation state on the row and returns it. Crucially, a ticket that is GONE after a PARTIAL close is
    recorded as a DIVERGENCE (operator exception) — it is NEVER silently treated as an approved full close.
    A refused read is non-terminal (``PARTIAL_RECONCILE_UNAVAILABLE``) — re-read later, never retry the send."""
    ticket = replication.follower_ticket
    if ticket is None:
        return {"ok": False, "reason": "no_follower_ticket"}
    ok_read, pos, read_reason = read_follower_position(replication.follower, int(ticket), fetch=fetch)
    if not ok_read:
        _set_recon(replication, PARTIAL_RECONCILE_UNAVAILABLE)
        return {"ok": False, "status": PARTIAL_RECONCILE_UNAVAILABLE, "reason": read_reason}
    if pos is None:
        # Ticket GONE after a PARTIAL close — a divergence requiring operator review, NOT a silent full close.
        _set_recon(replication, PARTIAL_DIVERGENT_TICKET_GONE)
        return {"ok": True, "status": PARTIAL_DIVERGENT_TICKET_GONE, "actual_remaining": None}
    actual = float(pos.volume)
    tol = max(float(step) * 0.5, 1e-9)
    if abs(actual - float(intended_remaining)) <= tol:
        _set_recon(replication, PARTIAL_RECONCILED)
        return {"ok": True, "status": PARTIAL_RECONCILED, "actual_remaining": actual}
    _set_recon(replication, PARTIAL_DIVERGENT_VOLUME)       # record divergence; caller raises an exception, no retry
    return {"ok": True, "status": PARTIAL_DIVERGENT_VOLUME, "actual_remaining": actual,
            "intended_remaining": float(intended_remaining)}


def _set_recon(replication: CopierReplication, status: str) -> None:
    replication.reconciliation_status = status
    replication.save(update_fields=["reconciliation_status", "updated_at"])


def run_reconcile_pass(*, limit: int = 200, fetch=_fetch_account_positions) -> dict:
    """One reconciliation pass over enabled followers with unreconciled armed-DEMO OPENs. Gated + fail-closed."""
    if not copier_reconcile_enabled():
        return {"enabled": False, "followers": 0, "matched": 0}
    follower_ids = (CopierReplication.objects
                    .filter(requested_op="OPEN", status=ReplicationStatus.EXECUTION_REQUESTED,
                            follower_ticket__isnull=True, follower__enabled=True)
                    .values_list("follower_id", flat=True).distinct()[:limit])
    followers = (CopierFollower.objects
                 .filter(pk__in=list(follower_ids))
                 .select_related("follower_account", "follower_account__broker_server"))
    matched = 0
    considered = 0
    for follower in followers:
        considered += 1
        try:
            matched += reconcile_follower(follower, fetch=fetch).get("matched", 0)
        except Exception as e:  # noqa: BLE001 — one follower's error must not sink the pass
            logger.exception("copier reconcile_follower failed for follower %s: %s", follower.pk, e)
    return {"enabled": True, "followers": considered, "matched": matched}
