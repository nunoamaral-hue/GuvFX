"""teardown_copier_live_dark — the idempotent DARK teardown command for the governed live-demo
(GFX-COPIER-LIFECYCLE-ARMING-SAFETY-CORRECTION-148, ADR-0114).

Returns the backend control plane to DARK (disengage arms, revoke authorizations) and, via the signed host
executor (ADR-0117), restores host admission OFF (the final host-side execution gate), disables the standing
emitter task (``GuvFX_CopierEmitter_46``) and shreds its host-local ingest key — on EVERY state: success, partial
arming or abort. Each host step is dispatched only with ``--commit`` and reported as UNPROVEN (never silently
done) when the signed host executor is unavailable (DARK). Idempotent: safe to run repeatedly / on an already-DARK
system. No order is placed/modified/closed; no secret is read.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "ADR-0114: idempotent DARK teardown (disarm + revoke + signed host-admission OFF). Safe to re-run."

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
        emitter_disabler = None
        emitter_key_remover = None
        if o["commit"]:
            # ADR-0117: REAL signed-host teardown actuators (DARK-gated -> the host step is reported UNPROVEN if the
            # signed executor is unavailable, never silently 'done'). All three are risk-REDUCING + idempotent, so
            # they need no arm flags and are safe to run any time (mirrors restore_off): signed host-admission OFF
            # (the final host-side execution gate), disable GuvFX_CopierEmitter_46, and provisioner -Remove (shred
            # the host-local ingest key).
            from trade_copier.live_host_actuators import (
                make_admission_revoker, make_emitter_disabler, make_emitter_key_remover)
            admission_restore = make_admission_revoker(int(o["follower_account_id"]))
            emitter_disabler = make_emitter_disabler()
            emitter_key_remover = make_emitter_key_remover()

        res = teardown_live_dark(
            master_account_id=int(o["master_account_id"]), follower_account_id=int(o["follower_account_id"]),
            authorization_id=o["authorization_id"], admission_restore=admission_restore,
            emitter_disabler=emitter_disabler, emitter_key_remover=emitter_key_remover,
            revoke_authorization=not o["no_revoke"], revoked_by_email=o["by"], reason=o["reason"])
        self.stdout.write(json.dumps(res, indent=1, default=str))
        if res.get("backend_dark"):
            self.stdout.write(self.style.SUCCESS("Backend control plane is DARK."))
        else:
            self.stdout.write(self.style.WARNING("Backend NOT fully DARK — inspect steps/verification."))
        if not res.get("host_dark"):
            self.stdout.write(self.style.WARNING(
                "Host DARK UNPROVEN: re-run with --commit to dispatch the signed host actuators (admission OFF + "
                "emitter disable + ingest-key shred). If they stay unproven the signed host executor is "
                "unavailable (DARK) — perform the reviewed host steps manually: disable GuvFX_CopierEmitter_46 and "
                "run the emitter-key provisioner -Remove."))
