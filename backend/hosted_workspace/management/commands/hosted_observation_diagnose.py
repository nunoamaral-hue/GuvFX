"""Operator diagnosis: WHY a Hosted Workspace has not reached CONNECTED (DARK, read-only).

For each hosted workspace (or one ``--account``) prints the stable corroboration reason code, a secret-free note,
and the durable evidence the diagnosis is derived from (observation version, decision age, delivery state, the
``proj_*`` projection booleans). Also prints the fail-open fleet health metrics.

STRICTLY READ-ONLY. It calls only the pure ``corroboration_diagnosis`` helpers — no host contact, no observer
trigger, no workspace mutation, no flag change. It is an operator-local oracle (staff by nature of shell access),
NOT a customer surface, and emits NO credential, full login, Windows username, or runtime/terminal path.
"""
from django.core.management.base import BaseCommand

from hosted_workspace.corroboration_diagnosis import (
    diagnose_workspace,
    observation_health_metrics,
)
from hosted_workspace.models import HostedMt5Workspace


class Command(BaseCommand):
    help = "Diagnose WHY hosted workspaces have not reached CONNECTED (read-only; redacted reason codes)."

    def add_arguments(self, parser):
        parser.add_argument("--account", type=int, default=None,
                            help="Diagnose only this TradingAccount id (default: the whole fleet).")

    def handle(self, *args, **opts):
        qs = HostedMt5Workspace.objects.select_related("trading_account__broker_server").order_by("id")
        account_id = opts.get("account")
        if account_id is not None:
            qs = qs.filter(trading_account_id=account_id)

        self.stdout.write("=== hosted workspace connection diagnosis (read-only; secret-free) ===")
        n = 0
        for ws in qs:
            d = diagnose_workspace(ws)
            n += 1
            self.stdout.write(
                f"acct={d['account_id']} owner={d['owner_id']} reason={d['reason']} "
                f"canonical={d['canonical_state']}/{d['canonical_reason']} delivery={d['delivery_state']} "
                f"observed={d['observed']} obs_version={d['observation_version']} "
                f"decision_age_s={d['last_decision_age_seconds']} "
                f"proj(conn={d['proj_connected']},match={d['proj_account_match']},"
                f"proc={d['proj_process_running']},ipc={d['proj_ipc_available']}) fault={d['is_fault']}")
            if d["note"]:
                self.stdout.write(f"    → {d['note']}")
        if n == 0:
            self.stdout.write("(no matching hosted workspaces)")

        if account_id is None:
            m = observation_health_metrics()
            self.stdout.write("--- fleet health metrics ---")
            self.stdout.write(
                f"total={m.get('workspace_total')} never_observed={m.get('never_observed')} "
                f"stuck_authorized={m.get('stuck_authorized')} stuck_none={m.get('stuck_none')} "
                f"faults={m.get('fault_count')} health={m.get('health')}")
            self.stdout.write(f"reason_code_counts={m.get('reason_code_counts')}")
            self.stdout.write(f"observation_age={m.get('observation_age')}")
            self.stdout.write(f"snapshot_age={m.get('snapshot_age')}")
