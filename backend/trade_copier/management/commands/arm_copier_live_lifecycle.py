"""arm_copier_live_lifecycle — the ONE governed, atomic OPEN+MODIFY+FULL_CLOSE live-demo arming command
(GFX-COPIER-LIFECYCLE-ARMING-SAFETY-CORRECTION-148, ADR-0114; REAL signed-host actuators wired in ADR-0117).
DEFAULTS TO DRY-RUN.

Validates the COMPLETE lifecycle-arming contract (route/symbol/caps, MODIFY+FULL_CLOSE authority AND execution
flags, partial-close excluded, #46 clean at the watermark, #48 flatness via the human-confirm gate, host admission
OFF, emitter task disabled, emitter signing-key readiness proven value-blind) and only with ``--commit`` performs
the atomic provision-admission -> enable-emitter -> create-arm sequence, with FULL rollback to DARK on any failure.

Host-side facts (emitter key readiness, host admission posture, emitter task state) are supplied as OPERATOR
ATTESTATIONS from the reviewed host ``-Verify`` checks — this command performs NO Windows-host contact itself. A
real ``--commit`` additionally requires signed-host actuators (provision + emitter-enable); where the backend
cannot dispatch them it FAILS CLOSED and rolls back, never a partial arm. No secret is read or printed.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = ("ADR-0114: atomic OPEN+MODIFY+FULL_CLOSE live-demo arming with full DARK rollback. Dry-run by default; "
            "host facts are operator attestations; --commit needs signed-host actuators and fails closed without.")

    def add_arguments(self, parser):
        parser.add_argument("--master-account-id", required=True, type=int)
        parser.add_argument("--follower-account-id", required=True, type=int)
        parser.add_argument("--source-symbol", required=True)
        parser.add_argument("--follower-symbol", required=True)
        parser.add_argument("--authorization-id", required=True, type=int)
        parser.add_argument("--by", required=True, help="STAFF operator/approver email (recorded for audit).")
        parser.add_argument("--reason", required=True)
        # Operator attestations from the reviewed host -Verify checks (this command does NOT contact the host).
        parser.add_argument("--follower-flat-confirmed", action="store_true", default=False,
                            help="Operator visually confirmed #48 is flat (backend cannot read live #48 positions).")
        parser.add_argument("--emitter-key-ready", action="store_true", default=False,
                            help="Operator attests the reviewed host key -Verify passed (value-blind).")
        parser.add_argument("--host-admission-off", action="store_true", default=False,
                            help="Operator attests #48 bridge admission is currently OFF (host -Verify).")
        parser.add_argument("--emitter-task-disabled", action="store_true", default=False,
                            help="Operator attests GuvFX_CopierEmitter_46 is currently DISABLED (host -Verify).")
        parser.add_argument("--commit", action="store_true", default=False,
                            help="Perform the atomic arm. Omit for a read-only contract dry-run.")

    def handle(self, *args, **o):
        from trade_copier.lifecycle_arming import arm_live_lifecycle

        key_ready = bool(o["emitter_key_ready"])
        admission_off = bool(o["host_admission_off"])
        task_disabled = bool(o["emitter_task_disabled"])

        def emitter_key_probe():
            return {"ready": key_ready, "identity_ok": key_ready}

        def host_admission_probe():
            return {"off": admission_off}

        def emitter_task_probe():
            return {"disabled": task_disabled}

        master_id = int(o["master_account_id"])
        follower_id = int(o["follower_account_id"])
        admission_provisioner = None
        emitter_enabler = None
        rollback_teardown = None
        if o["commit"]:
            # ADR-0117: REAL signed-host actuators (DARK-gated). admission_provisioner provisions follower #48
            # MT5_ALLOW_LIVE; emitter_enabler enables GuvFX_CopierEmitter_46. When the signed host executor is
            # unavailable (the repository-only / DARK phase) these fail closed and the arm rolls the WHOLE stack
            # back to DARK — never a partial arm, and never the old emitter_enabler=None dead end.
            from trade_copier.live_host_actuators import (
                make_admission_provisioner, make_admission_revoker, make_emitter_disabler,
                make_emitter_enabler, make_emitter_key_remover)
            admission_provisioner = make_admission_provisioner(follower_id)
            emitter_enabler = make_emitter_enabler()

            # Full reverse-order rollback to DARK on ANY post-validation failure: disable the emitter + shred its
            # key + restore host admission OFF with the real signed actuators (the pure arm's default teardown
            # leaves the emitter disabler/key-remover unset). The arm-internal rollback PRESERVES the still-valid
            # authorization (revoke_authorization=False) so a transient host failure never discards a valid grant.
            def rollback_teardown():  # noqa: E301
                from django.utils import timezone as _tz
                from trade_copier.lifecycle_teardown import teardown_live_dark
                return teardown_live_dark(
                    master_account_id=master_id, follower_account_id=follower_id,
                    authorization_id=int(o["authorization_id"]),
                    admission_restore=make_admission_revoker(follower_id),
                    emitter_disabler=make_emitter_disabler(),
                    emitter_key_remover=make_emitter_key_remover(),
                    revoke_authorization=False, now=_tz.now())

        res = arm_live_lifecycle(
            master_account_id=master_id, follower_account_id=follower_id,
            source_symbol=o["source_symbol"], follower_symbol=o["follower_symbol"],
            authorization_id=int(o["authorization_id"]), follower_flat_confirmed=bool(o["follower_flat_confirmed"]),
            emitter_key_probe=emitter_key_probe, host_admission_probe=host_admission_probe,
            emitter_task_probe=emitter_task_probe, admission_provisioner=admission_provisioner,
            emitter_enabler=emitter_enabler, teardown=rollback_teardown,
            commit=bool(o["commit"]), by_email=o["by"], reason=o["reason"])

        self.stdout.write(json.dumps(res, indent=1, default=str))
        if not res.get("ok"):
            raise CommandError(f"arm refused: {res.get('reason')} blockers={res.get('blockers')}")
        if res.get("armed"):
            self.stdout.write(self.style.SUCCESS("Lifecycle ARMED atomically (OPEN+MODIFY+FULL_CLOSE)."))
        else:
            self.stdout.write(self.style.WARNING("DRY-RUN only — contract satisfied. Re-run with --commit to arm."))
