"""Reconcile stale physical-capacity allocations held by FULLY-DECOMMISSIONED hosted workspaces.

A resource-leak defect left tombstoned accounts whose Stage-2 cleanup SUCCEEDED still bound to their
``execution_node`` (and, in one case, holding a non-RETIRED endpoint), so they kept consuming node capacity and
blocked new provisioning. The permanent fix releases these on decommission success; this command is the one-time
(idempotent, re-runnable) reconcile for pre-existing leaks, and an operational safety net.

READ-ONLY by default (reports the leaks). ``--apply`` performs the reclamation via the SAME
``reclaim_decommissioned_allocation`` used by the success path — which acts ONLY while
``cleanup_state == SUCCEEDED`` (so an in-progress/failed cleanup is never prematurely freed). Never touches a
workspace that is not a proven-decommissioned tombstone.
"""
from django.core.management.base import BaseCommand

from hosted_workspace.decommission import CLEANUP_SUCCEEDED, reclaim_decommissioned_allocation
from hosted_workspace.models import HostedMt5Workspace


class Command(BaseCommand):
    help = "Release node slots + retire endpoints still held by SUCCEEDED-decommissioned workspaces (leak fix)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Perform the reclamation. Omit for a read-only report (default).")

    def handle(self, *args, **opts):
        apply = bool(opts.get("apply"))
        # Candidates: a proven-decommissioned workspace (cleanup SUCCEEDED) that still holds a node slot OR a
        # non-retired endpoint. Eligibility (tombstoned + SUCCEEDED + exec disabled) is re-asserted in the
        # reclaim function's WHERE-gate, so a race can never free a live slot.
        from django.db.models import Q
        try:
            from execution.models import HostedExecutionEndpoint
            leaky_ep_ws = set(HostedExecutionEndpoint.objects
                              .filter(workspace__cleanup_state=CLEANUP_SUCCEEDED)
                              .exclude(state=HostedExecutionEndpoint.State.RETIRED)
                              .values_list("workspace_id", flat=True))
        except Exception:
            leaky_ep_ws = set()
        qs = (HostedMt5Workspace.objects.filter(cleanup_state=CLEANUP_SUCCEEDED)
              .filter(Q(execution_node__isnull=False) | Q(workspace_node__isnull=False) | Q(pk__in=leaky_ep_ws))
              .select_related("trading_account", "execution_node").order_by("trading_account_id"))
        total = qs.count()
        self.stdout.write(f"candidates (SUCCEEDED-decommissioned still holding a slot/endpoint): {total}  apply={apply}")
        node_released = ep_retired = 0
        for ws in qs:
            acct = ws.trading_account
            before = {"exec_node": ws.execution_node_id, "ws_node": ws.workspace_node_id,
                      "endpoint_leak": ws.pk in leaky_ep_ws}
            if apply:
                rec = reclaim_decommissioned_allocation(ws.pk)
                node_released += rec["node_released"]; ep_retired += rec["endpoint_retired"]
                self.stdout.write(f"  acct={acct.id} ws={ws.pk} {before} -> reclaimed {rec}")
            else:
                self.stdout.write(f"  acct={acct.id} ws={ws.pk} {before} (dry-run; pass --apply to reclaim)")
        if apply:
            self.stdout.write(self.style.SUCCESS(
                f"reclaimed: node_slots_released={node_released} endpoints_retired={ep_retired}"))
        else:
            self.stdout.write("DRY-RUN complete (no changes). Re-run with --apply to reclaim.")
