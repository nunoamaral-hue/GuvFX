"""trade_copier.lifecycle_dispatch — DARK lifecycle→producer-boundary SHADOW dispatch (ADR-0074 Phase 7).

The missing connective tissue between the per-master-ticket lifecycle (``trade_copier.lifecycle``) and the
execution producers (``trade_copier.execution_producer``) — wired WITHOUT activating execution.

For each actionable lifecycle decision the pass:
  1. locates the corresponding ``CopierReplication`` intent,
  2. RE-CHECKS the producer-boundary authority by calling the EXISTING ``admit_*`` functions (which create no
     rows and perform no broker write — the audited producer admission logic), and
  3. records a SYNTHETIC, append-only ``CopierLifecycleDispatch`` row with the op, the producer's deterministic
     idempotency key, the admission verdict, and ``would_execute`` (what WOULD happen once armed).

It NEVER calls ``produce_*`` (the OPEN producer mints a ``PLACE_ORDER_SHADOW`` ExecutionJob even unarmed), so it
creates **no ExecutionJob** and contacts **no broker**. The one admission that requires a live follower read —
``admit_partial_close`` — is deliberately DEFERRED (recorded, never executed) so the DARK pass performs no
broker contact. Gated by ``copier_lifecycle_dispatch_enabled`` (default OFF) and independent of the execute/arm
gates, which remain the sole authority for any order.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# lifecycle state → (op, requested_op candidates, idempotency-key prefix)
_OPEN = ("OPEN", ("OPEN",), "copier")
_MODIFY = ("MODIFY", ("MODIFY_SLTP",), "copier-modify")
_CLOSE = ("CLOSE", ("CLOSE",), "copier-close")
_PARTIAL = ("PARTIAL_CLOSE", ("PARTIAL_CLOSE",), "copier-pclose")


def _dispatch_for(lc):
    """Decide which producer op (if any) an actionable lifecycle row maps to, and the candidate requested_ops
    to search for its intent. Returns a list of (op, requested_ops, key_prefix) — CLOSE_PENDING searches both a
    full-close and a partial-close intent (most-recent wins)."""
    from trade_copier.lifecycle import LifecycleState
    state = lc.state
    if state == LifecycleState.OBSERVED_OPEN.value:
        return [_OPEN]
    if state == LifecycleState.MODIFY_PENDING.value:
        return [_MODIFY]
    if state == LifecycleState.CLOSE_PENDING.value:
        return [_CLOSE, _PARTIAL]
    return []


def _latest_replication(lc, requested_ops):
    from trade_copier.models import CopierReplication
    return (CopierReplication.objects
            .filter(follower=lc.follower, requested_op__in=list(requested_ops),
                    master_event__master=lc.master,
                    master_event__position_ticket=lc.master_position_ticket)
            .select_related("master_event").order_by("-id").first())


def _admit(op, rep):
    """Call the EXISTING producer-boundary admission (read-only, creates nothing). Returns (ok, reason).

    PARTIAL_CLOSE admission requires a live follower read (``read_follower_position``) — forbidden in the DARK
    pass — so it is DEFERRED here rather than executed."""
    from trade_copier import execution_producer as ep
    if op == "OPEN":
        ok, reason = ep.admit_open(rep)
        return bool(ok), str(reason)
    if op == "MODIFY":
        ok, _ticket, reason = ep.admit_modify(rep)
        return bool(ok), str(reason)
    if op == "CLOSE":
        ok, _ticket, reason = ep.admit_close(rep)
        return bool(ok), str(reason)
    if op == "PARTIAL_CLOSE":
        return False, "partial_admission_requires_live_read_deferred_dark"
    return False, "unknown_op"


def dispatch_one(lc) -> dict:
    """Record the SHADOW dispatch decision(s) for one actionable lifecycle row. Idempotent via the unique
    ``(lifecycle, idempotency_key)``. Creates NO ExecutionJob. Returns a summary dict."""
    from trade_copier.models import CopierLifecycleDispatch
    recorded = 0
    for (op, requested_ops, key_prefix) in _dispatch_for(lc):
        rep = _latest_replication(lc, requested_ops)
        if rep is None:
            continue   # no intent recorded for this op yet (replication pass has not produced it)
        idem = f"{key_prefix}:{rep.pk}"[:48]
        ok, reason = _admit(op, rep)
        would_execute = bool(ok) and op != "PARTIAL_CLOSE"
        _row, created = CopierLifecycleDispatch.objects.get_or_create(
            lifecycle=lc, idempotency_key=idem,
            defaults={
                "replication": rep, "op": op, "admit_ok": bool(ok),
                "admit_reason": reason[:64], "would_execute": would_execute,
                "detail": {"state": lc.state, "requested_op": rep.requested_op, "ticket": lc.master_position_ticket},
            })
        if created:
            recorded += 1
    return {"lifecycle_id": lc.id, "recorded": recorded}


def run_lifecycle_dispatch_pass(*, limit: int = 500) -> dict:
    """One DARK, read-only lifecycle→producer SHADOW-dispatch pass. Gated + fail-closed.

    Inert unless ``COPIER_ENABLED`` AND ``COPIER_LIFECYCLE_DISPATCH_ENABLED`` are on. Reuses the producer
    admission logic read-only and records synthetic ``CopierLifecycleDispatch`` rows; creates NO ExecutionJob
    and contacts NO broker. Returns a secret-free summary."""
    from trade_copier.flags import copier_lifecycle_dispatch_enabled
    if not copier_lifecycle_dispatch_enabled():
        return {"enabled": False, "lifecycles": 0, "recorded": 0}

    from trade_copier.lifecycle import LINKED_STATES, LifecycleState
    from trade_copier.models import CopierPositionLifecycle
    actionable = [LifecycleState.OBSERVED_OPEN.value, LifecycleState.MODIFY_PENDING.value,
                  LifecycleState.CLOSE_PENDING.value]
    rows = (CopierPositionLifecycle.objects.filter(is_terminal=False, state__in=actionable)
            .select_related("follower", "master").order_by("id")[:limit])
    _ = LINKED_STATES  # (documented: LINKED has no pending op, so it is intentionally not dispatched)
    lifecycles = 0
    recorded = 0
    for lc in rows:
        lifecycles += 1
        try:
            out = dispatch_one(lc)
            recorded += out.get("recorded", 0)
        except Exception as e:  # noqa: BLE001 — one lifecycle's error must not sink the pass
            logger.exception("copier lifecycle dispatch failed lifecycle=%s: %s", lc.id, e)
    return {"enabled": True, "lifecycles": lifecycles, "recorded": recorded}
