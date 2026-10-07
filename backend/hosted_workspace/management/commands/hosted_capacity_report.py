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

from execution.models import TerminalNode
from hosted_workspace.decommission import CLEANUP_SUCCEEDED
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.provisioning import node_occupant_count
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
