"""provision_authorized_live_copier_bridge (ADR-0049) — the human-run LIVE bridge arming command.

DEFAULTS TO DRY-RUN. Re-evaluates every LIVE-bridge gate from the authoritative backend, renders the reviewed
bridge env (render_bridge_env — the sole source of MT5_ALLOW_LIVE), shows a redacted exact-change preview, and
ONLY with --commit dispatches the signed, account-scoped ACTIVATE_TENANT_BRIDGE_LIVE_AUTHORIZED to this one
tenant's host executor, verifies the signed attestation, and fails closed (OFF restore) on any mismatch.

It arms NOTHING by itself and creates NO order. A real --commit requires the process to carry the LIVE arm
stack (COPIER_ENABLED + COPIER_EXECUTE_ENABLED + COPIER_LIVE_EXECUTE_ENABLED + HOSTED_LIVE_EXECUTION_ENABLED) so
allow_live_for_account resolves True, plus a resolvable signed host executor. No secret is read or printed.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "ADR-0049: re-evaluate gates + (with --commit) deploy a LIVE-authorized per-tenant bridge env. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--account-id", required=True, type=int, help="The follower account (e.g. 48).")
        parser.add_argument("--authorization-id", required=True, type=int,
                            help="The exact CopierLiveAuthorization id this arming is bound to.")
        parser.add_argument("--commit", action="store_true", default=False,
                            help="Actually dispatch the signed host operation. Omit for a read-only dry-run preview.")

    def handle(self, *args, **o):
        from trade_copier.live_bridge_provision import provision_live
        res = provision_live(int(o["account_id"]), int(o["authorization_id"]), commit=bool(o["commit"]))
        self.stdout.write(json.dumps(res, indent=1, default=str))
        if not res.get("ok"):
            raise CommandError(f"provision refused: {res.get('reason')}")
        if res.get("armed"):
            self.stdout.write(self.style.SUCCESS("LIVE bridge ARMED + attested for the authorized pair."))
        else:
            self.stdout.write(self.style.WARNING("DRY-RUN only — nothing dispatched. Re-run with --commit to arm."))
