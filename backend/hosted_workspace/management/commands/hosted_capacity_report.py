"""Authoritative physical-capacity diagnostic for the hosted execution nodes (operational visibility).

Distinguishes PHYSICAL node capacity (this report) from ENTITLEMENT (a per-USER owned-account cap, enforced
elsewhere) — the two are different accounting axes and must never be conflated. For each TerminalNode it reports:
  * max_accounts (physical ceiling),
  * occupant_count (the authoritative allocator count — distinct occupant accounts),
  * active_allocations (live accounts holding the slot),
  * reservations (hosted workspaces mid-provisioning: not yet READY),
  * unexpected_tombstoned_allocations (SUCCEEDED-decommissioned workspaces STILL holding execution_node — a
    resource leak; should be zero after the fix),
  * available slots.

READ-ONLY. No customer-facing surface; this is the operator oracle used to certify reusable capacity.
"""
from django.core.management.base import BaseCommand

from execution.models import HostCapacity, TerminalNode
from hosted_workspace.decommission import CLEANUP_SUCCEEDED
from hosted_workspace.flags import hosted_host_capacity_guard_enabled
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.provisioning import (host_capacity_ceiling, host_occupant_count,
                                           node_occupant_count)
from hosted_workspace.state_machine import WorkspaceLifecycleState as S


class Command(BaseCommand):
    help = "Physical capacity per hosted node: max / occupied / active / reservations / tombstoned-leaks / free."

    def handle(self, *args, **opts):
        self.stdout.write("=== hosted node physical-capacity report (NOT entitlement) ===")
        grand_leak = 0
        for n in TerminalNode.objects.all().order_by("id"):
            bound = HostedMt5Workspace.objects.filter(execution_node=n).select_related("trading_account")
            active = [w for w in bound if w.trading_account.disconnected_at is None]
            tomb_leak = [w for w in bound if w.trading_account.disconnected_at is not None
                         and w.cleanup_state == CLEANUP_SUCCEEDED]
            reservations = [w for w in active if w.canonical_state not in (S.EXECUTION_READY, S.CONNECTED)]
            occ = node_occupant_count(n)
            avail = max(0, (n.max_accounts or 0) - occ)
            grand_leak += len(tomb_leak)
            self.stdout.write(
                f"node id={n.id} host={n.hostname} status={getattr(n,'status',None)} max={n.max_accounts} "
                f"occupant_count={occ} active={len(active)} reservations={len(reservations)} "
                f"unexpected_tombstoned_allocations={len(tomb_leak)} available={avail}")
            if tomb_leak:
                self.stdout.write("    LEAK acct ids: " + ", ".join(str(w.trading_account_id) for w in tomb_leak))
        if grand_leak:
            self.stdout.write(self.style.WARNING(
                f"TOTAL unexpected tombstoned allocations (run reclaim_node_allocations --apply): {grand_leak}"))
        else:
            self.stdout.write(self.style.SUCCESS("No unexpected tombstoned allocations. Capacity is honest."))

        # ADR-0069 — PHYSICAL-HOST-WIDE rollup: logical nodes grouped by rdp_host (the physical box); the summed
        # occupancy is compared to the certified HostCapacity ceiling. The per-node report above can look healthy
        # while a shared box is oversubscribed — this section is the box-wide truth the allocator guard enforces.
        self.stdout.write("")
        guard = "ON" if hosted_host_capacity_guard_enabled() else "OFF"
        self.stdout.write(f"=== physical-host-wide rollup (HOSTED_HOST_CAPACITY_GUARD_ENABLED={guard}) ===")
        nodes_all = list(TerminalNode.objects.all())   # ALL statuses — a drained co-resident node still loads the box
        hosts = {}
        for n in nodes_all:
            hosts.setdefault((n.rdp_host or "").strip(), []).append(n)
        for hk, ns in sorted(hosts.items()):
            node_ids = ",".join(f"{n.id}:{n.status}" for n in ns)
            if not hk:
                self.stdout.write(f"host=(blank rdp_host — NOT deliverable) nodes=[{node_ids}] -> fails closed when guard ON")
                continue
            occ = host_occupant_count(hk, nodes_all)
            ceiling = host_capacity_ceiling(hk)
            row = HostCapacity.objects.filter(physical_host_key__iexact=hk).first()
            safe = row.certified_safe if row else None
            if ceiling is None:
                self.stdout.write(self.style.WARNING(
                    f"host={hk} nodes=[{node_ids}] occupancy={occ} ceiling=UNCERTIFIED safe=n/a "
                    f"-> guard fails closed here when ON (run set_host_capacity --apply)"))
            else:
                avail = max(0, ceiling - occ)
                verdict = "AT/OVER CEILING" if occ >= ceiling else f"ok ({avail} free)"
                self.stdout.write(
                    f"host={hk} nodes=[{node_ids}] occupancy={occ} ceiling={ceiling} safe={safe} "
                    f"available={avail} -> {verdict}")
