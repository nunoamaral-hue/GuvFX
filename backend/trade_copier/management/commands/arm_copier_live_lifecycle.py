"""arm_copier_live_lifecycle — the ONE governed, atomic OPEN+MODIFY+FULL_CLOSE live-demo arming command
(GFX-COPIER-LIFECYCLE-ARMING-SAFETY-CORRECTION-148, ADR-0114). DEFAULTS TO DRY-RUN.

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

        admission_provisioner = None
        emitter_enabler = None
        if o["commit"]:
            # Real arm: provision host admission via the signed executor (fail-closed if unavailable). The standing
            # emitter enable + key are reviewed HOST steps; no backend signed-executor op enables a scheduled task,
            # so emitter_enabler stays None and --commit fails closed (teardown) rather than a partial arm.
            def admission_provisioner(authz):  # noqa: E301
                from trade_copier.live_bridge_provision import provision_live
                return provision_live(int(o["follower_account_id"]), int(authz.id), commit=True)

        res = arm_live_lifecycle(
            master_account_id=int(o["master_account_id"]), follower_account_id=int(o["follower_account_id"]),
            source_symbol=o["source_symbol"], follower_symbol=o["follower_symbol"],
            authorization_id=int(o["authorization_id"]), follower_flat_confirmed=bool(o["follower_flat_confirmed"]),
            emitter_key_probe=emitter_key_probe, host_admission_probe=host_admission_probe,
            emitter_task_probe=emitter_task_probe, admission_provisioner=admission_provisioner,
            emitter_enabler=emitter_enabler, commit=bool(o["commit"]), by_email=o["by"], reason=o["reason"])

        self.stdout.write(json.dumps(res, indent=1, default=str))
        if not res.get("ok"):
            raise CommandError(f"arm refused: {res.get('reason')} blockers={res.get('blockers')}")
        if res.get("armed"):
            self.stdout.write(self.style.SUCCESS("Lifecycle ARMED atomically (OPEN+MODIFY+FULL_CLOSE)."))
        else:
            self.stdout.write(self.style.WARNING("DRY-RUN only — contract satisfied. Re-run with --commit to arm."))
