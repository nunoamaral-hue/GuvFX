"""TB-5 (Trusted Beta) — reconcile beta runtimes stuck at NOT_PROVISIONED and re-enqueue PROVISION.

Closes the order-of-operations gap: a PROVISION job is enqueued only at account-create time, so a
runtime left NOT_PROVISIONED because ``BETA_RUNTIMES_ENABLED`` was OFF at that moment is never
re-driven when the flag is later turned on. This command re-attempts ``reserve_beta_slot`` +
``enqueue_op`` for such runtimes. (A capacity/pool-full denial leaves the runtime BLOCKED, not
NOT_PROVISIONED, and is intentionally out of scope here — clearing a BLOCKED runtime is an operator
action.)

Safety:
  * DARK unless ``BETA_RUNTIMES_ENABLED`` (default OFF) — the master beta kill switch;
  * ENQUEUE-ONLY — it never advances a job (the provisioning worker does that), never touches the host;
  * IDEMPOTENT under concurrency — each runtime's check + reserve + enqueue runs inside one
    transaction that locks the runtime row (``select_for_update``) and re-checks for an active PROVISION
    job, so two overlapping passes serialise per runtime and the second skips (no double-enqueue);
  * only admitted-beta-tester, non-quarantined, cohort=BETA runtimes still at NOT_PROVISIONED;
  * fail-soft per runtime — a capacity denial or error on one runtime never blocks the others, and it
    can never touch Nuno's PRODUCTION runtimes (``reserve_beta_slot`` only ever gates cohort=BETA).
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from terminal_provisioning.models import AccountRuntime, ProvisioningJob, RuntimeState


class Command(BaseCommand):
    help = ("Re-enqueue PROVISION for beta runtimes stuck at NOT_PROVISIONED "
            "(dark unless BETA_RUNTIMES_ENABLED; enqueue-only; idempotent).")

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50,
                            help="max runtimes to reconcile in one pass")

    def handle(self, *args, **opts):
        from terminal_provisioning.beta_capacity import (
            CapacityError, beta_runtimes_enabled, get_or_create_beta_runtime, reserve_beta_slot)
        from terminal_provisioning.provisioner import enqueue_op

        if not beta_runtimes_enabled():
            self.stdout.write("reconcile_beta_provisioning: BETA_RUNTIMES_ENABLED off — nothing to do.")
            return

        active = [ProvisioningJob.Status.QUEUED, ProvisioningJob.Status.RUNNING]
        candidates = (
            AccountRuntime.objects
            .filter(cohort=AccountRuntime.Cohort.BETA, state=RuntimeState.NOT_PROVISIONED,
                    quarantined=False)
            .select_related("trading_account", "trading_account__user")
            .order_by("id")[:opts["limit"]])

        reenqueued = skipped = denied = errors = 0
        for rt in candidates:
            acct = getattr(rt, "trading_account", None)
            # ADR-0021: no per-user admission — reconcile any BETA runtime with an owner.
            if acct is None or getattr(acct, "user", None) is None:
                skipped += 1
                continue
            try:
                with transaction.atomic():
                    # M1: lock the runtime row so two overlapping passes serialise per runtime, then
                    # RE-CHECK state + the active-job set under the lock — the second pass now skips.
                    locked = AccountRuntime.objects.select_for_update().get(pk=rt.pk)
                    if locked.state != RuntimeState.NOT_PROVISIONED or locked.quarantined:
                        skipped += 1
                        continue
                    if ProvisioningJob.objects.filter(
                            runtime=locked, op=ProvisioningJob.Op.PROVISION, status__in=active).exists():
                        skipped += 1   # already has an active PROVISION job — never double-enqueue
                        continue
                    get_or_create_beta_runtime(acct)   # idempotent
                    try:
                        runtime = reserve_beta_slot(acct)  # QUEUED on grant; CapacityError otherwise
                    except CapacityError as exc:
                        denied += 1
                        self.stdout.write(f"deferred account_id={acct.id}: {exc.reason_code}")
                        continue
                    enqueue_op(runtime, ProvisioningJob.Op.PROVISION)
                    reenqueued += 1
            except Exception as exc:  # noqa: BLE001 — one runtime's failure must not block the rest
                errors += 1
                self.stderr.write(f"error account_id={getattr(acct, 'id', '?')}: {type(exc).__name__}")

        self.stdout.write(
            f"reconcile_beta_provisioning: reenqueued={reenqueued} skipped={skipped} "
            f"denied={denied} errors={errors}")
        if errors:
            # L3: a systemic fault must not exit 0 (a cron watching exit codes would read success).
            raise CommandError(f"{errors} runtime(s) errored during reconcile")
