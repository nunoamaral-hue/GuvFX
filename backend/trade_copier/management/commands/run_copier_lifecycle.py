"""run_copier_lifecycle — project the per-master-ticket lifecycle state machine (ADR-0071).

DARK and READ-ONLY. No-op unless COPIER_ENABLED AND COPIER_LIFECYCLE_ENABLED are on. Folds the authoritative
MasterEvent ledger + reconcile-derived link facts through the pure lifecycle engine and materialises
``CopierPositionLifecycle`` rows + an append-only ``CopierLifecycleTransition`` audit. Creates NO ExecutionJob
and contacts NO broker — it records decisions and lifecycle state only; the execute/arm gates remain the sole
authority for any order.

Usage::

    python manage.py run_copier_lifecycle
    python manage.py run_copier_lifecycle --limit 200 \
        --entry-admissibility-seconds 120 --max-modification-age-seconds 120 --max-event-age-seconds 300
"""
from django.core.management.base import BaseCommand

from trade_copier.lifecycle import LifecyclePolicy, run_lifecycle_pass


class Command(BaseCommand):
    help = "Project the copier per-master-ticket lifecycle (gated, READ-ONLY; no order placed/closed/modified)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--entry-admissibility-seconds", type=float, default=120.0)
        parser.add_argument("--max-modification-age-seconds", type=float, default=120.0)
        parser.add_argument("--max-event-age-seconds", type=float, default=300.0)

    def handle(self, *args, **opts):
        policy = LifecyclePolicy(
            entry_admissibility_seconds=opts["entry_admissibility_seconds"],
            max_modification_age_seconds=opts["max_modification_age_seconds"],
            max_event_age_seconds=opts["max_event_age_seconds"],
        )
        summary = run_lifecycle_pass(policy=policy, limit=opts["limit"])
        self.stdout.write(
            "copier-lifecycle: {} (READ-ONLY; no order placed/closed/modified)".format(summary))
