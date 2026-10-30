"""restore_copier_bridge_execution_off (ADR-0049) — the risk-reducing LIVE bridge disarm/restore command.

Renders the canonical execution-OFF bridge env (render_bridge_env(allow_live=False) — needs NO flags and NO
valid authorization) and, with --commit, dispatches the signed RESTORE_TENANT_BRIDGE_EXECUTION_OFF to this one
tenant's host executor, restarting ONLY that tenant's task and attesting MT5_ALLOW_LIVE is OFF. Idempotent and
safe to invoke repeatedly, including after the authorization is expired/consumed/revoked. DEFAULTS TO DRY-RUN.
Creates no order; only ever reduces risk.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "ADR-0049: restore a per-tenant bridge to execution-OFF (with --commit). Dry-run by default; risk-reducing."

    def add_arguments(self, parser):
        parser.add_argument("--account-id", required=True, type=int)
        parser.add_argument("--commit", action="store_true", default=False,
                            help="Actually dispatch the signed OFF-restore. Omit for a read-only dry-run preview.")

    def handle(self, *args, **o):
        from trade_copier.live_bridge_provision import restore_off
        res = restore_off(int(o["account_id"]), commit=bool(o["commit"]))
        self.stdout.write(json.dumps(res, indent=1, default=str))
        if not res.get("ok"):
            raise CommandError(f"restore refused: {res.get('reason')}")
        if res.get("dry_run"):
            self.stdout.write(self.style.WARNING("DRY-RUN only — nothing dispatched. Re-run with --commit to restore."))
        else:
            self.stdout.write(self.style.SUCCESS("Execution-OFF restored + attested for this tenant."))
