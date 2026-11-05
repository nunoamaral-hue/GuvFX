"""run_copier_lifecycle_dispatch — DARK lifecycle→producer SHADOW dispatch pass (ADR-0074 Phase 7).

No-op unless COPIER_ENABLED AND COPIER_LIFECYCLE_DISPATCH_ENABLED are on. Re-checks the EXISTING producer
admission (``execution_producer.admit_*``, read-only) for each actionable lifecycle decision and records a
SYNTHETIC ``CopierLifecycleDispatch`` row with the producer's deterministic idempotency key and admission
verdict. Creates NO ExecutionJob and contacts NO broker; the execute/arm gates remain the sole authority for
any order. NOT scheduled — run on demand by an operator only.

Usage::

    python manage.py run_copier_lifecycle_dispatch
    python manage.py run_copier_lifecycle_dispatch --limit 200
"""
from django.core.management.base import BaseCommand

from trade_copier.lifecycle_dispatch import run_lifecycle_dispatch_pass


class Command(BaseCommand):
    help = "Record copier lifecycle→producer SHADOW dispatch decisions (gated; no ExecutionJob, no broker contact)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **opts):
        summary = run_lifecycle_dispatch_pass(limit=opts["limit"])
        self.stdout.write(
            "copier-lifecycle-dispatch: {} (SHADOW only; no ExecutionJob, no broker contact)".format(summary))
