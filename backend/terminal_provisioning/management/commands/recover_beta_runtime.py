"""Phase 2 — recover a reclaimed BETA runtime into a retryable state + exactly one claimable PROVISION job
(ADR-0024). SEPARATE operator gate from Phase-1 reclaim. Pure backend — NO agent contact; provisioner stays DARK.

DRY-RUN BY DEFAULT. Moves the runtime ``REMOVED -> NOT_PROVISIONED`` (clearing any quarantine) and enqueues
EXACTLY ONE ``PROVISION`` job; the prior FAILED job is retained as history. The new job is inert until a
separate, later arming (Phase 3). Never arms the provisioner and never runs an agent op.

    python manage.py recover_beta_runtime --runtime-uuid 66972e0e-c803-49b5-8da2-a5df1c14e90d          # dry-run
    python manage.py recover_beta_runtime --runtime-uuid <uuid> --apply
"""
from django.core.management.base import BaseCommand, CommandError

from terminal_provisioning import recovery
from terminal_provisioning.models import ProvisioningJob


class Command(BaseCommand):
    help = ("Recover a reclaimed BETA runtime to a retryable state + exactly one PROVISION job "
            "(dry-run by default; --apply to execute; NO agent contact; provisioner stays DARK).")

    def add_arguments(self, parser):
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument("--runtime-uuid", help="AccountRuntime.runtime_uuid to recover")
        g.add_argument("--account-runtime-id", type=int, help="AccountRuntime pk to recover")
        parser.add_argument("--apply", action="store_true", help="execute (default is a dry-run)")
        parser.add_argument("--force-from-failed", action="store_true",
                            help="accept FAILED instead of requiring REMOVED (ORPHAN RISK if Phase-1 reclaim "
                                 "has not run — the still-assigned agent slot would be orphaned by a later retry; "
                                 "never accepts a live/HELD state)")
        parser.add_argument("--allow-armed", action="store_true",
                            help="proceed even if the provisioner is armed (by default recover refuses so the "
                                 "new job stays inert until a separate Phase-3 arming)")

    def _inventory(self, rt):
        jobs = list(ProvisioningJob.objects.filter(runtime=rt).order_by("id")
                    .values_list("id", "op", "status"))
        return jobs

    def handle(self, *args, **o):
        try:
            rt = recovery.resolve_runtime(runtime_uuid=o["runtime_uuid"],
                                          account_runtime_id=o["account_runtime_id"])
            recovery.assert_beta(rt)
            # Keep the pool DARK: an enqueued job under an armed worker would start a real retry WITHOUT the
            # separate Phase-3 arming.
            recovery.assert_dark_or_allow(allow_armed=o["allow_armed"])
        except recovery.ReclaimError as e:
            raise CommandError(f"refusing to recover: {e.reason_code}"
                               + (f" ({e.detail})" if e.detail else ""))

        self.stdout.write(f"runtime={rt.pk} uuid={rt.runtime_uuid} cohort={rt.cohort} "
                          f"state={rt.state} quarantined={rt.quarantined}")
        self.stdout.write(f"existing jobs (id,op,status) = {self._inventory(rt)}")
        self.stdout.write("plan: clear quarantine (if set) -> state NOT_PROVISIONED -> enqueue EXACTLY ONE "
                          "PROVISION job (Job #1 retained as history); provisioner stays DARK, job is inert.")
        if o["force_from_failed"]:
            self.stdout.write("WARNING: --force-from-failed accepts FAILED. If Phase-1 reclaim has NOT run, the "
                              "agent slot is still assigned and a later retry would ORPHAN it. Reclaim first.")

        if not o["apply"]:
            self.stdout.write("DRY-RUN: no mutation. Re-run with --apply to execute.")
            return

        try:
            job = recovery.recover_to_provisionable(rt, require_removed=not o["force_from_failed"])
        except recovery.ReclaimError as e:
            raise CommandError(f"recover failed: {e.reason_code}" + (f" ({e.detail})" if e.detail else ""))

        rt.refresh_from_db()
        self.stdout.write(f"RECOVERED: runtime state={rt.state}; created/active PROVISION job id={job.id} "
                          f"status={job.status}")
        self.stdout.write(f"jobs now (id,op,status) = {self._inventory(rt)}")
        from terminal_provisioning.beta_capacity import beta_runtimes_enabled
        dark = not beta_runtimes_enabled()
        self.stdout.write("EXACTLY_ONE_PROVISION_JOB; " + (
            "provisioner is DARK — the job is inert until a separate Phase-3 arming."
            if dark else
            "WARNING: the provisioner is ARMED (--allow-armed) — a worker may claim this job now; this is NOT "
            "the intended separate Phase-3 arming.") + " Phase 3 (arm + retry to RUNNING) is a separate "
            "Sponsor authorisation.")
