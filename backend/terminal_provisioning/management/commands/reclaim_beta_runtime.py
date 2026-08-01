"""Phase 1 — reclaim a BETA runtime's orphaned agent slot occupancy (ADR-0024). Operator-gated.

DRY-RUN BY DEFAULT. Drives the signed **STOP -> TOMBSTONE -> RELEASE** lifecycle through the deployed PR#252
``_step`` reconcile, advancing the slot's generation and returning it to Available. Never touches
``slots.sqlite`` or the host filesystem directly; never arms the provisioner; never advances a ProvisioningJob.

    # inspect only (no mutation, no agent write):
    python manage.py reclaim_beta_runtime --runtime-uuid 66972e0e-c803-49b5-8da2-a5df1c14e90d
    # inspect + a read-only signed agent occupancy probe:
    python manage.py reclaim_beta_runtime --runtime-uuid <uuid> --probe-agent
    # execute the reclaim (STOP->TOMBSTONE->RELEASE):
    python manage.py reclaim_beta_runtime --runtime-uuid <uuid> --expect-slot 2 --expect-generation 4 --apply
"""
from django.core.management.base import BaseCommand, CommandError

from terminal_provisioning import recovery
from terminal_provisioning.mgmt_client import ManagementChannelError, ManagementChannelTimeout
from terminal_provisioning.models import RuntimeState
from terminal_provisioning.provisioner import ProvisionStepError
from terminal_provisioning.runtime_state import record_transition


class Command(BaseCommand):
    help = ("Reclaim a BETA runtime's orphaned agent slot via signed STOP->TOMBSTONE->RELEASE "
            "(dry-run by default; --apply to execute; never arms the provisioner).")

    def add_arguments(self, parser):
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument("--runtime-uuid", help="AccountRuntime.runtime_uuid to reclaim")
        g.add_argument("--account-runtime-id", type=int, help="AccountRuntime pk to reclaim")
        parser.add_argument("--apply", action="store_true", help="execute (default is a dry-run)")
        parser.add_argument("--job-id", type=int, default=None,
                            help="stable idempotency anchor (default: the runtime's PROVISION job id)")
        parser.add_argument("--expect-slot", type=int, default=None, help="fail closed unless the agent slot matches")
        parser.add_argument("--expect-generation", type=int, default=None,
                            help="fail closed unless the agent generation matches")
        parser.add_argument("--allow-running", action="store_true", help="proceed even if a slot process is observed")
        parser.add_argument("--allow-armed", action="store_true", help="proceed even if the provisioner is armed")
        parser.add_argument("--allow-state", action="store_true",
                            help="reclaim from a state outside the default reclaimable set")
        parser.add_argument("--probe-agent", action="store_true",
                            help="dry-run only: also do a read-only signed VERIFY occupancy probe")

    def _out(self, msg):
        self.stdout.write(msg)

    def handle(self, *args, **o):
        try:
            rt = recovery.resolve_runtime(runtime_uuid=o["runtime_uuid"],
                                          account_runtime_id=o["account_runtime_id"])
            recovery.assert_beta(rt)
            if rt.state not in recovery.RECLAIMABLE_STATES and not o["allow_state"]:
                raise recovery.ReclaimError("state_not_reclaimable",
                                            detail=f"state={rt.state}; pass --allow-state to widen")
            recovery.assert_no_active_job(rt)
            recovery.assert_dark_or_allow(allow_armed=o["allow_armed"])
            job_id = o["job_id"] or recovery.stable_reclaim_job_id(rt)
        except recovery.ReclaimError as e:
            raise CommandError(f"refusing to reclaim: {e.reason_code}"
                               + (f" ({e.detail})" if e.detail else ""))

        self._out(f"runtime={rt.pk} uuid={rt.runtime_uuid} cohort={rt.cohort} state={rt.state}")
        self._out(f"idempotency anchor job_id={job_id} (stable; reused across every STOP/TOMBSTONE/RELEASE and re-run)")
        self._out(f"expect slot={o['expect_slot']} generation={o['expect_generation']}")
        self._out("plan: STOP -> TOMBSTONE -> RELEASE (advance generation, return slot to Available)")
        self._out("NOTE: the backend cannot read the agent's stage_evidence / slot-quarantine; RELEASE's "
                  "host-side evidence gates surface only as a fail-closed error at --apply time.")

        if not o["apply"]:
            if o["probe_agent"]:
                self._probe_only(rt, job_id, o)
            self._out("DRY-RUN: no mutation, no signed mutating request. Re-run with --apply to execute.")
            return

        # ── APPLY ──
        try:
            client = recovery.make_reclaim_client(job_id=job_id, correlation_id=f"reclaim-rt-{rt.pk}")
        except (recovery.ReclaimError, ManagementChannelError, ManagementChannelTimeout) as e:
            raise CommandError(f"negotiation failed: {getattr(e, 'reason_code', 'agent_unreachable')}")

        # read-only occupancy probe first: already-released -> idempotent success; else assert it matches
        try:
            probe = client.probe_occupancy(rt)
        except ManagementChannelError as e:
            if getattr(e, "reason_code", "") == "runtime_not_assigned":
                self._out("agent reports runtime_not_assigned -> slot already released (idempotent)")
                if rt.state != RuntimeState.REMOVED:
                    record_transition(rt, RuntimeState.REMOVED, event_type="RECLAIM",
                                      reason_code="already_released")
                self._out("SLOT_ALREADY_RELEASED; backend runtime -> REMOVED. Proceed to recover_beta_runtime.")
                return
            raise CommandError(f"occupancy probe failed: {getattr(e, 'reason_code', 'probe_failed')}")
        except ManagementChannelTimeout:
            raise CommandError("occupancy probe timed out (ambiguous) — retry when the agent is reachable")

        self._out(f"agent occupancy: slot={probe.get('slot')} generation={probe.get('generation')} "
                  f"running={probe.get('running')} uuid={probe.get('runtime_uuid')}")
        try:
            recovery.assert_probe_matches(rt, probe, expect_slot=o["expect_slot"],
                                          expect_generation=o["expect_generation"],
                                          allow_running=o["allow_running"])
        except recovery.ReclaimError as e:
            raise CommandError(f"refusing to reclaim: {e.reason_code}")

        # drive STOP -> TOMBSTONE -> RELEASE; fail CLOSED (quarantine, never REMOVED) on any unresolved error
        try:
            result = recovery.drive_reclaim_sequence(rt, client)
        except (ProvisionStepError, ManagementChannelError, ManagementChannelTimeout) as e:
            code = getattr(e, "reason_code", "reclaim_failed")
            recovery.quarantine_on_reclaim_failure(rt, code)
            raise CommandError(f"reclaim failed at an agent step: {code} — runtime quarantined, NOT removed. "
                               f"Inspect and retry (idempotent under the same job_id).")

        recovery.mark_reclaimed(rt)
        self._out(f"RELEASE ok: released={result.get('released')} available={result.get('available')} "
                  f"slot={result.get('slot')} generation={result.get('generation')}")
        self._out("SLOT_RECLAIMED; backend runtime -> REMOVED; agent slot advanced + Available. "
                  "Backend recovery is the SEPARATE Phase 2 (recover_beta_runtime).")

    def _probe_only(self, rt, job_id, o):
        """Dry-run opt-in: a single read-only signed VERIFY occupancy probe (no mutation)."""
        try:
            client = recovery.make_reclaim_client(job_id=job_id, correlation_id=f"reclaim-probe-{rt.pk}")
            probe = client.probe_occupancy(rt)
            self._out(f"[probe] agent occupancy: slot={probe.get('slot')} generation={probe.get('generation')} "
                      f"running={probe.get('running')} uuid={probe.get('runtime_uuid')}")
        except ManagementChannelError as e:
            code = getattr(e, "reason_code", "probe_failed")
            self._out(f"[probe] agent reports: {code}"
                      + (" -> slot already released" if code == "runtime_not_assigned" else ""))
        except ManagementChannelTimeout:
            self._out("[probe] timed out (ambiguous) — the agent may be busy/unreachable")
