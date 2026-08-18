"""Recovery tooling — reconcile STALE PRE-ACTIVATION order jobs before a node is activated.

When an execution node had no authorized order worker, real signals promoted to PROMOTED plans and
enqueued PLACE_ORDER jobs that then sat PENDING forever (0 fills). Those jobs MUST NOT suddenly
execute when node execution is later activated, and the PROMOTED plans permanently saturate the
per-account exposure budget (``account_exposure_exceeded`` on every later signal).

This command cancels those never-claimed PENDING jobs to FAILED (cancel-before-fill; can never race
a live claim) and then transitions their now-fully-terminal plans PROMOTED→CLOSED (reusing the
close-monitor), which releases the exposure + concurrency budget. It creates NO order, calls NO
bridge, and REFUSES Customer Zero (account 1) and account 18.

CONTROLLED DATA MIGRATION posture:
  * DRY-RUN by default — reports EXACTLY which jobs/plans would be reconciled and changes nothing.
  * ``--apply`` performs the compare-and-set cancels + close (race-safe; idempotent; audited).
  * ``--account-id`` is REQUIRED and account-scoped.
  * ``--older-than-seconds`` bounds staleness (default 1800).

Usage::

    python manage.py reconcile_stale_preactivation_orders --account-id 25            # dry-run
    python manage.py reconcile_stale_preactivation_orders --account-id 25 --apply    # execute
"""
import json

from django.core.management.base import BaseCommand, CommandError

from execution.stale_reconcile import (
    DEFAULT_OLDER_THAN_SECONDS,
    ProtectedAccountError,
    reconcile_stale_preactivation_orders,
)


class Command(BaseCommand):
    help = (
        "Reconcile stale pre-activation PENDING PLACE_ORDER jobs for an account (cancel-before-fill "
        "to FAILED + close the plans to release exposure). DRY-RUN by default; --apply to execute. "
        "Refuses Customer Zero and account 18. Places no order."
    )

    def add_arguments(self, parser):
        parser.add_argument("--account-id", type=int, required=True,
                            help="TradingAccount id to reconcile (required). Account-scoped.")
        parser.add_argument("--apply", action="store_true",
                            help="Perform the cancel + close. Default: dry-run (no change).")
        parser.add_argument("--older-than-seconds", type=int, default=DEFAULT_OLDER_THAN_SECONDS,
                            help="Only reconcile plans created at least this many seconds ago "
                                 "(default: %(default)s).")
        parser.add_argument("--limit", type=int, default=500,
                            help="Max plans to process this run (default: %(default)s).")
        parser.add_argument("--json", action="store_true",
                            help="Emit the full machine-readable report as JSON.")

    def handle(self, *args, **opts):
        try:
            report = reconcile_stale_preactivation_orders(
                account_id=opts["account_id"],
                older_than_seconds=opts["older_than_seconds"],
                limit=opts["limit"],
                apply=opts["apply"],
            )
        except ProtectedAccountError as exc:
            raise CommandError(str(exc)) from exc

        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
            return

        mode = "APPLY (mutating)" if report["apply"] else "DRY-RUN (no change)"
        self.stdout.write(
            f"reconcile_stale_preactivation_orders [{mode}] account={report['account_id']} "
            f"older_than={report['older_than_seconds']}s")
        self.stdout.write(
            "  scanned PROMOTED plans: %d  qualifying: %d  skipped: %d"
            % (report["scanned_promoted_plans"], len(report["candidates"]), len(report["skipped"])))
        for c in report["candidates"]:
            self.stdout.write(
                "  plan #%-6d %-8s %-10s legs=%d lot=%s age=%ds jobs=%s"
                % (c["plan_id"], c["symbol"], c["source"], c["leg_count"], c["total_lot"],
                   c["age_seconds"], c["job_ids"]))
        for s in report["skipped"]:
            self.stdout.write(self.style.WARNING("  SKIP plan #%s — %s" % (s["plan_id"], s["reason"])))
        if report["apply"]:
            self.stdout.write(self.style.SUCCESS(
                "  RECONCILED: %d job(s) PENDING->FAILED, %d plan(s) PROMOTED->CLOSED "
                "(exposure released; no order placed)."
                % (report["jobs_cancelled"], report["plans_closed"])))
        else:
            self.stdout.write(
                "  DRY-RUN: %d plan(s) WOULD be reconciled. Re-run with --apply to execute."
                % len(report["candidates"]))
