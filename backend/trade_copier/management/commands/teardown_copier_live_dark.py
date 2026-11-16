"""teardown_copier_live_dark — the idempotent DARK teardown command for the governed live-demo
(GFX-COPIER-LIFECYCLE-ARMING-SAFETY-CORRECTION-148, ADR-0113).

Returns the backend control plane to DARK (disengage arms, revoke authorizations) and, via the signed host
executor, restores host admission OFF — the final host-side execution gate — on EVERY state: success, partial
arming or abort. Disabling the standing emitter task and shredding its ephemeral key are reviewed HOST steps
(run the provisioner ``-Remove`` + disable the task) that this command reports as UNPROVEN unless a signed-host
actuator performs them, never as silently done. Idempotent: safe to run repeatedly / on an already-DARK system.
No order is placed/modified/closed; no secret is read.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "ADR-0113: idempotent DARK teardown (disarm + revoke + signed host-admission OFF). Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument("--master-account-id", required=True, type=int)
        parser.add_argument("--follower-account-id", required=True, type=int)
        parser.add_argument("--authorization-id", type=int, default=None)
        parser.add_argument("--by", default="", help="Operator email recorded on the revoke (audit).")
        parser.add_argument("--reason", default="teardown")
        parser.add_argument("--no-revoke", action="store_true", default=False,
                            help="Preserve the authorization (arm-rollback semantics); default revokes it.")
        parser.add_argument("--commit", action="store_true", default=False,
                            help="Dispatch the signed host-admission RESTORE OFF. Omit for backend-only teardown.")

    def handle(self, *args, **o):
        from trade_copier.lifecycle_teardown import teardown_live_dark

        admission_restore = None
        if o["commit"]:
            def admission_restore():  # noqa: E301 — signed host-admission OFF (idempotent, needs no flags/authz)
                from trade_copier.live_bridge_provision import restore_off
                return restore_off(int(o["follower_account_id"]), commit=True)

        res = teardown_live_dark(
            master_account_id=int(o["master_account_id"]), follower_account_id=int(o["follower_account_id"]),
            authorization_id=o["authorization_id"], admission_restore=admission_restore,
            emitter_disabler=None, emitter_key_remover=None,
            revoke_authorization=not o["no_revoke"], revoked_by_email=o["by"], reason=o["reason"])
        self.stdout.write(json.dumps(res, indent=1, default=str))
        if res.get("backend_dark"):
            self.stdout.write(self.style.SUCCESS("Backend control plane is DARK."))
        else:
            self.stdout.write(self.style.WARNING("Backend NOT fully DARK — inspect steps/verification."))
        if not res.get("host_dark"):
            self.stdout.write(self.style.WARNING(
                "Host DARK UNPROVEN: disable GuvFX_CopierEmitter_46 and run the emitter-key provisioner -Remove, "
                "and re-run with --commit for the signed host-admission OFF."))
