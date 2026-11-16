"""reconcile_monitoring_readonly_positions (GFX-137, ADR-0111) — the dedicated, monitoring-ONLY read-only-positions
bridge reconciler command.

For EXACT, owner-authorized, identity-pinned, allow-listed LIVE persistent-workspace account id(s) it renders the
execution-OFF bridge env WITH ``MT5_LIVE_READONLY_POSITIONS_ENABLED=1`` and (only with ``--commit``) applies it via
the signed ``RESTORE_TENANT_BRIDGE_EXECUTION_OFF`` transport — byte-exact rollback, post-apply read-back, per-account
truthful reporting. DEFAULTS TO DRY-RUN. Never renders/sets ``MT5_ALLOW_LIVE``, never arms execution, never touches
Copy-Trading. ``--teardown`` removes ONLY the read-only flag and returns the bridge to its captured baseline.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = ("GFX-137: reconcile per-tenant read-only live-positions monitoring for explicit allow-listed accounts "
            "(dry-run by default). Read-only; never arms execution or Copy-Trading.")

    def add_arguments(self, parser):
        parser.add_argument("--account-ids", required=True, nargs="+", type=int,
                            help="EXACT trading-account ids to reconcile (never a sweep).")
        parser.add_argument("--expected-owner", required=True, type=int,
                            help="Owner (user) id pin — every account must belong to this owner or it is refused.")
        parser.add_argument("--expected-login", default=None,
                            help="Optional identity pin: each account's broker login must equal this.")
        parser.add_argument("--expected-workspace-uuid", default=None,
                            help="Optional identity pin: the endpoint workspace_uuid must equal this.")
        parser.add_argument("--commit", action="store_true", default=False,
                            help="Actually dispatch the signed read-only env. Omit for a read-only dry-run preview.")
        parser.add_argument("--teardown", action="store_true", default=False,
                            help="Remove ONLY the read-only flag, restoring the captured baseline env.")
        parser.add_argument("--max-concurrency", type=int, default=1,
                            help="Bounded parallelism across accounts (default 1 = sequential, fail-closed).")

    def handle(self, *args, **o):
        from hosted_workspace.monitoring_reconcile import reconcile, _RealMonitoringOps

        commit = bool(o["commit"])
        # The real signed host transport is constructed ONLY for a --commit run; a dry-run contacts no host at all.
        ops = _RealMonitoringOps() if commit else None

        report = reconcile(
            o["account_ids"], commit=commit,
            expected_owner_id=int(o["expected_owner"]),
            expected_login=o.get("expected_login"),
            expected_workspace_uuid=o.get("expected_workspace_uuid"),
            ops=ops, teardown=bool(o["teardown"]), max_concurrency=int(o["max_concurrency"]),
        )
        self.stdout.write(json.dumps(report, indent=1, default=str))

        summary = report["summary"]
        if summary.get("dry_run"):
            self.stdout.write(self.style.WARNING(
                "DRY-RUN only — nothing dispatched. Re-run with --commit to apply (host-gated; Sponsor-approved)."))
            return
        if summary.get("refused", 0) > 0:
            raise CommandError(
                f"{summary['refused']}/{summary['total']} account(s) refused or failed — see per-account reasons.")
        self.stdout.write(self.style.SUCCESS(
            f"Read-only monitoring reconciled for {summary['applied']}/{summary['total']} account(s)."))
