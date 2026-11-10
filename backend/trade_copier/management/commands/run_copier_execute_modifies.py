"""run_copier_execute_modifies — the SL/TP MODIFY producer worker (control-plane completion, 2026-11-10).

Consume lifecycle MODIFY (protection-change) intents and produce the one enqueue-only MODIFY_POSITION job per
bound follower ticket (gated, idempotent, single-flight). A DEMO follower takes the SHADOW/DEMO path; a LIVE
follower takes the governed live modify (``create_live_modify_job``) ONLY with the full live flag stack +
COPIER_LIVE_MODIFY_ENABLED + the matching authorization authority + the host admission — all re-read under
the lock, fail-closed otherwise. No-op unless COPIER_ENABLED AND COPIER_EXECUTE_ENABLED. NOT scheduled.
"""
from django.core.management.base import BaseCommand

from trade_copier.execution_producer import produce_pending_modifies


class Command(BaseCommand):
    help = "Produce enqueue-only MODIFY (SL/TP) jobs from lifecycle modify intents (gated; DEMO or governed LIVE)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **opts):
        self.stdout.write("copier-execute-modifies: {}".format(produce_pending_modifies(limit=opts["limit"])))
