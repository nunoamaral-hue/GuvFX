"""run_copier_mutation_dispatch — the ADR-0086 lifecycle-mutation DISPATCHER worker (control-plane completion,
2026-11-10).

Consume the governed MODIFY_POSITION / CLOSE_TRADE ExecutionJobs created by the live producers and dispatch
each through the ADR-0087 per-tenant transport to the bound follower bridge: single-flight claim, send-time
gate re-check (per-op arm + execute-enabled re-read), migration-free follower watermark, ONE shot — a
timeout/indeterminate result is UNKNOWN_OUTCOME (need-reconcile), never a success and never a retried order.

DARK: ``run_mutation_dispatch_pass`` is a no-op unless ``copier_mutation_dispatch_enabled`` (ANDed with the
LIVE-execute arm stack), and the REAL transport is inert unless ``copier_mutation_transport_enabled``. With
both OFF (default) it contacts no broker. NOT scheduled — run on demand by an operator.
"""
from django.core.management.base import BaseCommand

from trade_copier.mutation_dispatch import run_mutation_dispatch_pass
from trade_copier.mutation_transport import mutation_transport


class Command(BaseCommand):
    help = "Dispatch governed copier MODIFY/CLOSE jobs via the per-tenant transport (gated; inert by default)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **opts):
        summary = run_mutation_dispatch_pass(transport=mutation_transport(), limit=opts["limit"])
        self.stdout.write("copier-mutation-dispatch: {} (gated; transport inert unless armed)".format(summary))
