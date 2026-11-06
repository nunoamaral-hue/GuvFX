"""ADR-0082 — governed, audited, fail-closed primitive to change a ``TerminalNode.max_accounts``.

The PER-NODE logical-capacity analogue of ``set_host_capacity`` (ADR-0069, which owns the PHYSICAL-HOST
certified ceiling ``HostCapacity``). This module is the only supported way to change a node's ``max_accounts``
other than a reviewed migration — replacing an unaudited Django-admin / ORM / raw-SQL field edit.

Design invariants (why each exists):

* **Occupancy-based host-safe guard (raises only, point-in-time).** A RAISE (``proposed_max > current
  max_accounts``) is refused unless ``proposed_max + other_co-resident_occupancy <= certified_safe`` for the
  node's physical host, using the SAME authoritative occupancy counting as the allocator
  (``hosted_workspace.provisioning.node_occupant_count`` / ``host_occupant_count``). That counting sums occupants
  across ALL co-resident logical nodes of ANY status (ACTIVE/DRAINING/OFFLINE/DISABLED), grouped by a
  trimmed/lower-cased ``rdp_host`` so a whitespace/case variant cannot escape the sum — this primitive never
  reproduces the ACTIVE-only under-count. A no-op or a LOWERING (still ``>= node occupancy``) can never increase
  occupancy potential, so it is always permitted, even when the host legitimately sits above ``certified_safe``
  (that is what makes the documented rollback/tighten path work). The guard is POINT-IN-TIME: it bounds the
  proposed ceiling against current co-resident occupancy; it does NOT bound the sum of co-resident ceilings, so
  durable occupancy remains governed by the ADR-0069 runtime guard (``max_total_occupants``, unchanged here) and
  operator capacity discipline.
* **Never widens anything else.** It only writes ``TerminalNode.max_accounts`` (+ an audit row). It never touches
  ``HostCapacity`` (the physical ceiling/safe), workspace assignments, tenant isolation, execution controls, or
  any order path. A Customer-Zero isolation invariant is captured before and re-checked after the write and the
  whole transaction rolls back if it ever differs (it structurally cannot, since isolation does not depend on
  ``max_accounts`` — a loud belt-and-suspenders).
* **Fail-closed + atomic + concurrency-safe.** ``apply`` runs in one transaction that ``select_for_update``-locks
  every co-resident node row (ordered by pk) and the ``HostCapacity`` row, RE-EVALUATES the full guard under the
  lock (so a concurrent capacity change or allocation that moved occupancy is seen and fails closed), writes the
  node and the audit row together, and rolls back entirely if the audit cannot be written.

No host or broker contact; no secret; prints/returns no credential or account login.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from django.db import transaction
from django.utils import timezone

from execution.models import HostCapacity, NodeCapacityAudit, TerminalNode

# ---- reason codes (stable, queryable) ---------------------------------------
R_OK = "OK"
R_NODE_NOT_FOUND = "NODE_NOT_FOUND"
R_NODE_AMBIGUOUS = "NODE_AMBIGUOUS"
R_NODE_INACTIVE = "NODE_INACTIVE"
R_HOST_AMBIGUOUS = "HOST_IDENTITY_AMBIGUOUS"            # blank rdp_host → cannot resolve the physical host
R_MAX_OUT_OF_RANGE = "MAX_OUT_OF_RANGE"
R_BELOW_OCCUPANCY = "MAX_BELOW_NODE_OCCUPANCY"
R_CERT_MISSING = "HOST_CAPACITY_CERTIFICATION_MISSING"
R_SAFE_MISSING = "CERTIFIED_SAFE_MISSING"
R_HOST_OVER_SAFE = "HOST_ALREADY_OVER_CERTIFIED_SAFE"
R_WOULD_EXCEED_SAFE = "WOULD_EXCEED_CERTIFIED_SAFE"
R_ISOLATION_VIOLATED = "ISOLATION_INVARIANT_VIOLATED"

MIN_MAX_ACCOUNTS = 1


class IsolationInvariantError(Exception):
    """Raised (and rolled back) if the Customer-Zero forbidden-node set changed across the write — a loud,
    fail-closed integrity stop. Structurally unreachable (``max_accounts`` does not affect isolation)."""


@dataclass
class NodeCapacityPlan:
    ok: bool
    reason: str
    applied: bool
    node_id: Optional[int]
    node_hostname: str
    physical_host_key: str
    old_max: Optional[int]
    proposed_max: Optional[int]
    node_occupancy: Optional[int]
    host_occupancy: Optional[int]
    other_node_occupancy: Optional[int]
    certified_safe: Optional[int]
    ceiling: Optional[int]
    remaining_safe_before: Optional[int]
    remaining_safe_after: Optional[int]
    audit_id: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _resolve_node(*, node_hostname: Optional[str], node_id):
    """Resolve exactly one node by immutable id or exact hostname. Returns (node, reason|None)."""
    if node_id is not None:
        try:
            return TerminalNode.objects.get(pk=node_id), None
        except TerminalNode.DoesNotExist:
            return None, R_NODE_NOT_FOUND
    hk = (node_hostname or "").strip()
    if not hk:
        return None, R_NODE_NOT_FOUND
    qs = list(TerminalNode.objects.filter(hostname__iexact=hk))
    if not qs:
        return None, R_NODE_NOT_FOUND
    if len(qs) > 1:                       # hostname is unique in schema; defensive fail-closed
        return None, R_NODE_AMBIGUOUS
    return qs[0], None


def _blank_plan(reason: str, *, node=None, proposed_max=None) -> NodeCapacityPlan:
    return NodeCapacityPlan(
        ok=False, reason=reason, applied=False,
        node_id=getattr(node, "pk", None),
        node_hostname=getattr(node, "hostname", "") or "",
        physical_host_key=(getattr(node, "rdp_host", "") or "").strip(),
        old_max=getattr(node, "max_accounts", None),
        proposed_max=proposed_max,
        node_occupancy=None, host_occupancy=None, other_node_occupancy=None,
        certified_safe=None, ceiling=None,
        remaining_safe_before=None, remaining_safe_after=None,
    )


def _coresident_nodes(hk: str, *, for_update: bool = False):
    """Co-resident logical nodes for physical host ``hk``, matched by a trimmed/lower-cased ``rdp_host`` (NOT the
    whitespace-sensitive ``rdp_host__iexact``), so a node whose stored ``rdp_host`` differs only by case or
    surrounding whitespace is never dropped from the host occupancy sum. ``for_update`` locks the rows: the
    ``TerminalNode`` table is tiny (a handful of hosts) and capacity changes are rare operator actions, so locking
    the whole table briefly is acceptable and guarantees the lock set is whitespace/case-complete."""
    hk_norm = (hk or "").strip().lower()
    if not hk_norm:
        return []
    qs = TerminalNode.objects.all().order_by("pk")
    if for_update:
        qs = qs.select_for_update()
    return [n for n in qs if (getattr(n, "rdp_host", "") or "").strip().lower() == hk_norm]


def evaluate_node_capacity_change(*, node_hostname: Optional[str] = None, node_id=None,
                                  proposed_max: int, node=None, nodes_locked=None) -> NodeCapacityPlan:
    """Pure, read-only evaluation of a proposed ``max_accounts`` change. Returns a :class:`NodeCapacityPlan`
    whose ``ok`` says whether an ``--apply`` would proceed and ``reason`` is a stable code. ``node`` and
    ``nodes_locked`` let :func:`apply_node_capacity_change` re-evaluate under its row locks."""
    from hosted_workspace.provisioning import host_occupant_count, node_occupant_count

    if node is None:
        node, why = _resolve_node(node_hostname=node_hostname, node_id=node_id)
        if node is None:
            return _blank_plan(why, proposed_max=proposed_max)

    # Node must be ACTIVE (reject inactive: draining/offline/disabled).
    if node.status != TerminalNode.Status.ACTIVE:
        return _blank_plan(R_NODE_INACTIVE, node=node, proposed_max=proposed_max)

    hk = (getattr(node, "rdp_host", "") or "").strip()
    if not hk:                            # blank rdp_host → physical-host identity ambiguous → fail closed
        return _blank_plan(R_HOST_AMBIGUOUS, node=node, proposed_max=proposed_max)

    if not isinstance(proposed_max, int) or proposed_max < MIN_MAX_ACCOUNTS:
        return _blank_plan(R_MAX_OUT_OF_RANGE, node=node, proposed_max=proposed_max)

    node_occ = node_occupant_count(node)

    row = HostCapacity.objects.filter(physical_host_key__iexact=hk).first()
    if row is None:                       # uncertified host → fail closed (ADR-0069 contract)
        p = _blank_plan(R_CERT_MISSING, node=node, proposed_max=proposed_max)
        p.node_occupancy = node_occ
        return p
    ceiling = row.max_total_occupants
    safe = row.certified_safe
    if safe is None:                      # cannot enforce certified_safe without a value → fail closed
        p = _blank_plan(R_SAFE_MISSING, node=node, proposed_max=proposed_max)
        p.node_occupancy = node_occ
        p.ceiling = ceiling
        return p

    # A node ceiling above the physical-host ABSOLUTE ceiling is nonsensical (documented range: [MIN, ceiling]).
    if proposed_max > ceiling:
        p = _blank_plan(R_MAX_OUT_OF_RANGE, node=node, proposed_max=proposed_max)
        p.node_occupancy = node_occ
        p.certified_safe = safe
        p.ceiling = ceiling
        return p

    # Never strand a live occupant: the new ceiling must not be below current node occupancy.
    if proposed_max < node_occ:
        p = _blank_plan(R_BELOW_OCCUPANCY, node=node, proposed_max=proposed_max)
        p.node_occupancy = node_occ
        p.certified_safe = safe
        p.ceiling = ceiling
        return p

    # Host-wide occupancy over ALL co-resident nodes (any status), grouped by trimmed/lower-cased rdp_host so a
    # whitespace/case variant cannot escape the sum — the authoritative, anti-undercount count. The target is
    # always a member of its own co-resident set, so other_occ = host_occ - node_occ is never negative.
    nodes = nodes_locked if nodes_locked is not None else _coresident_nodes(hk)
    host_occ = host_occupant_count(hk, nodes)
    other_occ = host_occ - node_occ
    remaining_before = safe - host_occ

    plan = NodeCapacityPlan(
        ok=False, reason=R_OK, applied=False,
        node_id=node.pk, node_hostname=node.hostname, physical_host_key=hk,
        old_max=node.max_accounts, proposed_max=proposed_max,
        node_occupancy=node_occ, host_occupancy=host_occ, other_node_occupancy=other_occ,
        certified_safe=safe, ceiling=ceiling,
        remaining_safe_before=remaining_before,
        # A ceiling change adds NO occupant, so remaining certified slots is unchanged by the change itself; it
        # merely permits the node to accept up to that remaining headroom at future allocation.
        remaining_safe_after=remaining_before,
    )

    # The certified-safe guards apply ONLY to a RAISE. A no-op or a lowering (already >= node occupancy via the
    # R_BELOW_OCCUPANCY floor) cannot increase occupancy potential, so it is permitted even when the host sits
    # above certified_safe — this is what lets the documented rollback/tighten path clamp capacity down.
    is_raise = proposed_max > node.max_accounts
    if is_raise and host_occ > safe:                  # host already over certified-safe → refuse the raise (rule 4)
        plan.reason = R_HOST_OVER_SAFE
        return plan
    if is_raise and proposed_max + other_occ > safe:  # the raise could permit occupancy past certified-safe (rule 5)
        plan.reason = R_WOULD_EXCEED_SAFE
        return plan

    plan.ok = True
    plan.reason = R_OK
    return plan


def apply_node_capacity_change(*, node_hostname: Optional[str] = None, node_id=None, proposed_max: int,
                               actor: str = "", reason: str, correlation_id: str = "") -> NodeCapacityPlan:
    """Apply a ``max_accounts`` change under row locks, with an atomic audit row. Re-evaluates the full guard
    UNDER the lock and fails closed (no write) on any violation. ``reason`` is mandatory. Returns the plan with
    ``applied`` True on success."""
    if not (reason or "").strip():
        raise ValueError("reason is mandatory for an applied node-capacity change")

    node0, why = _resolve_node(node_hostname=node_hostname, node_id=node_id)
    if node0 is None:
        return _blank_plan(why, proposed_max=proposed_max)
    hk = (getattr(node0, "rdp_host", "") or "").strip()
    if not hk:
        return _blank_plan(R_HOST_AMBIGUOUS, node=node0, proposed_max=proposed_max)

    from hosted_workspace.tenant_isolation import forbidden_execution_node_ids

    with transaction.atomic():
        # Lock EVERY co-resident node row (any status) in a deterministic order, then the HostCapacity row, so
        # two concurrent capacity changes — or a capacity change racing the allocator (which locks the same node
        # rows) — serialise and the guard is evaluated against committed state.
        locked_nodes = _coresident_nodes(hk, for_update=True)
        HostCapacity.objects.select_for_update().filter(physical_host_key__iexact=hk).first()
        locked_target = next((n for n in locked_nodes if n.pk == node0.pk), None)
        if locked_target is None:
            # rdp_host changed between resolve and lock → fail closed, no write.
            return _blank_plan(R_HOST_AMBIGUOUS, node=node0, proposed_max=proposed_max)

        isolation_before = frozenset(forbidden_execution_node_ids())

        plan = evaluate_node_capacity_change(
            proposed_max=proposed_max, node=locked_target, nodes_locked=locked_nodes,
        )
        if not plan.ok:
            return plan                   # transaction commits with no write; locks released

        plan.old_max = locked_target.max_accounts
        locked_target.max_accounts = proposed_max
        locked_target.save(update_fields=["max_accounts", "updated_at"])

        audit = NodeCapacityAudit.objects.create(
            node=locked_target, node_hostname=locked_target.hostname, physical_host_key=hk,
            previous_max_accounts=plan.old_max, new_max_accounts=proposed_max,
            node_occupancy_at_decision=plan.node_occupancy or 0,
            host_occupancy_at_decision=plan.host_occupancy or 0,
            certified_safe=plan.certified_safe, ceiling=plan.ceiling,
            actor=actor or "", reason=reason, correlation_id=correlation_id or "",
        )

        isolation_after = frozenset(forbidden_execution_node_ids())
        if isolation_after != isolation_before:
            raise IsolationInvariantError(R_ISOLATION_VIOLATED)   # rolls back node + audit

        plan.applied = True
        plan.audit_id = audit.pk
        return plan
