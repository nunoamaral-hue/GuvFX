"""Recovery tooling — reclaim orphaned PLANNED SignalExecutionPlans (concurrency-gate leak).

A promotion-rejected (e.g. ``daily_drawdown_hit``) or otherwise never-promoted plan is left in
``PLANNED`` forever; ``count_active`` counts PLANNED-only, so each orphan permanently consumes one of
``PLAN_MAX_CONCURRENT_GROUPS`` concurrency slots and eventually rejects EVERY new signal
(``concurrent_limit_exceeded``). This command reclaims such orphans by transitioning them
``PLANNED → VOIDED`` (creates NO order/job).

CONTROLLED DATA MIGRATION posture:
  * DRY-RUN by default — lists EXACTLY which plans would be reclaimed and changes nothing.
  * ``--apply`` performs the compare-and-set transition (race-safe; idempotent; fully audited).
  * ``--account`` / ``--symbol`` / ``--source`` scope the operation.
  * ``--older-than-seconds`` MUST exceed ``SIGNAL_MAX_AGE_SECONDS`` (W1) — a plan younger than the
    staleness window may still be promotable, so the core function refuses a smaller value.

SAFETY: the only plan→order path re-reads ``status == PLANNED`` and re-checks signal age
≤ ``SIGNAL_MAX_AGE_SECONDS`` before creating any job, so a voided/old plan can never produce an order.

Usage::

    python manage.py reclaim_orphaned_planned_plans                          # dry-run, all accounts
    python manage.py reclaim_orphaned_planned_plans --account 1 --symbol XAUUSD
    python manage.py reclaim_orphaned_planned_plans --account 1 --symbol XAUUSD --apply
"""
import json

from django.core.management.base import BaseCommand, CommandError

from execution.execution_health import (
    ORPHANED_PLANNED_RECLAIM_SECONDS,
    reclaim_orphaned_planned_plans,
)


class Command(BaseCommand):
    help = (
        "Reclaim orphaned PLANNED signal-execution plans that permanently hold a concurrency slot. "
        "DRY-RUN by default; --apply to transition PLANNED->VOIDED (no order placed)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Perform the PLANNED->VOIDED transition. Default: dry-run (no change).")
        parser.add_argument("--account", type=int, default=None,
                            help="Restrict to this TradingAccount id.")
        parser.add_argument("--symbol", type=str, default=None,
                            help="Restrict to this symbol (case-insensitive).")
        parser.add_argument("--source", type=str, default=None,
                            help="Restrict to this signal source (e.g. ti_signals).")
        parser.add_argument("--older-than-seconds", type=int, default=ORPHANED_PLANNED_RECLAIM_SECONDS,
                            help=("Only reclaim plans created at least this many seconds ago "
                                  "(default: %(default)s). MUST exceed SIGNAL_MAX_AGE_SECONDS."))
        parser.add_argument("--limit", type=int, default=500,
                            help="Max plans to process this run (default: %(default)s).")
        parser.add_argument("--json", action="store_true",
                            help="Emit the full machine-readable report as JSON.")

    def handle(self, *args, **opts):
        try:
            report = reclaim_orphaned_planned_plans(
                older_than_seconds=opts["older_than_seconds"],
                account_id=opts["account"], symbol=opts["symbol"], source=opts["source"],
                limit=opts["limit"], apply=opts["apply"],
            )
        except ValueError as exc:  # W1 — invalid threshold (<= SIGNAL_MAX_AGE_SECONDS)
            raise CommandError(str(exc)) from exc

        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
            return

        mode = "APPLY (mutating)" if report["apply"] else "DRY-RUN (no change)"
        scope = "account=%s symbol=%s source=%s" % (
            opts["account"] or "ALL", opts["symbol"] or "ALL", opts["source"] or "ALL")
        self.stdout.write(
            f"reclaim_orphaned_planned_plans [{mode}] {scope} "
            f"older_than={report['older_than_seconds']}s")
        self.stdout.write(
            "  candidates: %d  age_buckets=%s" % (report["scanned"], report["age_buckets"]))
        # Deterministic, line-per-plan report — exactly what would change.
        for c in report["candidates"]:
            self.stdout.write(
                "  plan #%-6d acct=%s %-8s %-10s msg=%s created=%s age=%ds prior_reject=%s"
                % (c["plan_id"], c["account_id"], c["symbol"], c["source"], c["message_id"],
                   c["created_at"], c["age_seconds"], c["prior_reject_reason"]))
        if report["apply"]:
            self.stdout.write(self.style.SUCCESS(
                f"  RECLAIMED {report['reclaimed']} plan(s) PLANNED->VOIDED (concurrency slots freed; no order)."))
        else:
            self.stdout.write(
                f"  DRY-RUN: {report['scanned']} plan(s) WOULD be reclaimed. Re-run with --apply to execute.")
