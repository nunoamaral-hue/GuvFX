"""trade_copier.open_result_binding — ADR-0096 event-driven post-fill binding of the copier follower ticket.

The ADR-0057 LIVE OPEN producer mints a ``PLACE_ORDER`` ``ExecutionJob`` and leaves the ``CopierReplication`` at
``EXECUTION_REQUESTED`` with ``follower_ticket`` NULL. The follower broker ticket only becomes known when the
hosted bridge FILLS the order and reports the outcome to the job-completion endpoint
(``ExecutionJob.result["ticket"]``). Until ADR-0096 the ONLY thing that bound ``CopierReplication.follower_ticket``
was the positions-read reconcile (``trade_copier.reconcile``), which a LIVE follower bridge refuses with
``account_not_demo`` unless the route-scoped read flag is armed — so on the real-time path a copied LIVE OPEN
could not be linked, and the governed MODIFY / FULL-CLOSE (which require a ``MATCHED``, non-null ``follower_ticket``)
could not fire.

This binds the follower ticket DIRECTLY from the OPEN order's fill RESULT, event-driven, at completion — with
NO ``/mt5/positions`` read. It is called fail-safe from the job-completion endpoint for a copier ``PLACE_ORDER``:

* SUCCESS with an integer result ticket  → bind ``follower_ticket`` + ``broker_result``,
  ``reconciliation_status=MATCHED``, ``status → EXECUTION_VERIFIED`` (mirrors the ADR-0087 mutation SUCCESS write);
* SUCCESS with NO / non-integer ticket    → UNKNOWN: bind NOTHING (never guesses a ticket); the slow reconcile
  remains the fallback and MODIFY/CLOSE stay fail-closed (contained);
* FAILED                                  → mark the replication ``EXECUTION_FAILED`` (no ticket, no lifecycle op).

Deterministic / idempotent / fail-closed:

* scoped to the EXACT replication the job belongs to (its ``copier_replication_id`` AND a check that the
  replication's ``execution_job`` is this job) — never a cross-tenant or sibling-route lookup;
* a replay with the SAME ticket is an idempotent no-op;
* a CONFLICTING ticket on an already-bound replication FAILS CLOSED — never overwrites — and raises a governed
  reconciliation alert;
* it creates NO order, contacts NO broker, consumes NO authorization allowance (the OPEN already consumed it);
* fully fail-safe: any error is swallowed + logged, so a binding fault can never break the job-completion response.

It is intentionally NOT gated by a new flag: a copier ``PLACE_ORDER`` exists only because the gated producer
created it (the full live arm stack was on), so the hook is inert in a DARK system (no copier orders) and MUST
run for a real copier order — including if the operator disarms mid-flight — so the placed position can always
be reliably MODIFY/CLOSE-managed and contained. Binding is a recording/containment act, never an order path.

Known limitation (netting follower, documented; fails SAFE). The governed MODIFY/FULL-CLOSE resolve the
follower by its MT5 POSITION ticket. The signal bridge's normal OPEN result reports the opening ORDER ticket in
``result["ticket"]``; for a fresh market open into a position-clean follower (the governed route's precondition
— exact pair, one order, one position, follower position-clean) the position ticket EQUALS the order ticket, so
the result-derived bind is correct. A NETTING follower already holding a same-symbol position is the one case
they can diverge (the order nets into the pre-existing position, whose ticket is older). ``_result_ticket``
prefers an explicit ``position`` / ``position_ticket`` field whenever a bridge surfaces one (and the lost-ACK
recovery result already carries the position ticket); absent that, a diverged bind resolves to NOTHING at
``positions_get`` (MT5 tickets are globally unique per server), so MODIFY/CLOSE fail CLOSED — never the wrong
position — and the ADR-0057 fallback (operator manual close) applies. The positions-read reconcile remains the
authority for that case. The governed #46→#48 route's position-clean precondition forecloses the divergence.
"""
from __future__ import annotations

import logging

from django.db import transaction

logger = logging.getLogger(__name__)


def _result_ticket(result):
    """The filled FOLLOWER ticket from an OPEN job result, or None when absent/ambiguous (NEVER guessed).

    Prefers an explicit position-ticket field when a bridge surfaces one (``position`` / ``position_ticket`` —
    the canonical identity the governed MODIFY / FULL-CLOSE resolve by via ``positions_get(ticket=...)``, and
    the identity the lost-ACK recovery and the positions-read reconcile already bind); only then falls back to
    ``ticket``, which the signal bridge sets to the opening ORDER ticket. For a fresh market open into a
    position-clean follower (the governed route's precondition) the position ticket EQUALS the order ticket, so
    the fallback is sound there; for a netting follower already holding a same-symbol position they can diverge
    (see the module docstring's netting limitation). A boolean or fractional value is garbage, never a ticket.
    """
    if not isinstance(result, dict):
        return None
    for key in ("position", "position_ticket", "ticket"):
        if key not in result:
            continue
        raw = result.get(key)
        if isinstance(raw, bool):  # a JSON bool must never coerce to ticket 1
            continue
        try:
            t = int(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(raw, float) and t != raw:  # a fractional ticket is garbage, not a truncation target
            continue
        if t > 0:
            return t
    return None


def bind_open_result(job) -> dict:
    """Bind (or fail-close) the follower ticket for a completed copier OPEN. Fail-safe; see module docstring."""
    try:
        from execution.models import ExecutionJob
        if getattr(job, "job_type", None) != ExecutionJob.JobType.PLACE_ORDER:
            return {"bound": False, "reason": "not_place_order"}
        payload = getattr(job, "payload", None) or {}
        rep_id = payload.get("copier_replication_id")
        if rep_id is None:
            return {"bound": False, "reason": "not_copier_order"}
        return _bind(job, int(rep_id))
    except Exception:  # noqa: BLE001 — a binding fault must NEVER break job completion.
        logger.exception("copier open-result binding contained (job=%s)", getattr(job, "id", None))
        return {"bound": False, "reason": "contained"}


def _bind(job, rep_id) -> dict:
    from execution.models import ExecutionJob
    from trade_copier.models import CopierReplication, ReplicationStatus
    from trade_copier.reconcile import MATCHED

    status = getattr(job, "status", None)
    ticket = _result_ticket(getattr(job, "result", None))
    with transaction.atomic():
        rep = (CopierReplication.objects.select_for_update().filter(pk=rep_id).first())
        if rep is None:
            return {"bound": False, "reason": "replication_absent"}
        # Anti cross-bind: only the replication that actually OWNS this job may be bound from its result.
        if rep.execution_job_id not in (None, job.id):
            return {"bound": False, "reason": "job_replication_mismatch"}

        if status == ExecutionJob.Status.FAILED:
            if rep.follower_ticket is None and rep.status == ReplicationStatus.EXECUTION_REQUESTED:
                rep.status = ReplicationStatus.EXECUTION_FAILED
                rep.broker_result = job.result if isinstance(job.result, dict) else {"status": "FAILED"}
                rep.save(update_fields=["status", "broker_result", "updated_at"])
            return {"bound": False, "reason": "open_failed"}

        if status != ExecutionJob.Status.SUCCESS:
            return {"bound": False, "reason": "not_terminal_success"}

        if ticket is None:
            # UNKNOWN — a SUCCESS with no usable ticket. NEVER guess; leave for the fallback reconcile; the
            # bound-ticket precondition keeps MODIFY/CLOSE fail-closed (contained).
            return {"bound": False, "reason": "unknown_no_ticket"}

        if rep.follower_ticket is not None:
            if int(rep.follower_ticket) == ticket:
                return {"bound": True, "idempotent": True, "ticket": ticket}
            # CONFLICT — a DIFFERENT ticket than already bound. Fail closed, alert, NEVER overwrite.
            # Log UNCONDITIONALLY: this is an integrity-class event and the governed alert layer is
            # default-OFF, so the alert alone could be silent. A ticket is an audit identifier, not a secret.
            logger.error("copier open-bind CONFLICT rep=%s existing=%s incoming=%s — not overwritten",
                         rep.id, int(rep.follower_ticket), ticket)
            _alert_conflict(rep, existing=int(rep.follower_ticket), incoming=ticket)
            return {"bound": False, "reason": "ticket_conflict",
                    "existing": int(rep.follower_ticket), "incoming": ticket}

        rep.follower_ticket = ticket
        rep.broker_result = job.result if isinstance(job.result, dict) else {"ticket": ticket}
        rep.reconciliation_status = MATCHED
        rep.status = ReplicationStatus.EXECUTION_VERIFIED
        rep.save(update_fields=["follower_ticket", "broker_result", "reconciliation_status",
                                "status", "updated_at"])
    return {"bound": True, "ticket": ticket, "replication_id": rep_id}


def _alert_conflict(rep, *, existing, incoming) -> None:
    """Raise a governed, de-duplicated operator alert for a conflicting fill ticket. Gated + fail-open; the ticket
    values are redacted by the alert layer; this NEVER resolves or overwrites anything."""
    try:
        from trade_copier import failure_alerts
        failure_alerts.raise_copier_alert(
            "open_bind_conflict", scope_key=f"rep:{rep.id}", ticket=incoming,
            reason="follower_ticket_conflict", revision=int(getattr(rep, "id", 0)))
    except Exception:  # noqa: BLE001 — alerting must never break binding.
        logger.warning("copier open-bind conflict alert routing failed rep=%s existing=%s",
                       getattr(rep, "id", None), existing)
