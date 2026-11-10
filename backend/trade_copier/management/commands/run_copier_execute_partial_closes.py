"""run_copier_execute_partial_closes — the PARTIAL-CLOSE producer worker (control-plane completion, 2026-11-10).

Consume lifecycle PARTIAL_CLOSE (reduce-only) intents and produce the one enqueue-only PARTIAL_CLOSE_TRADE job
per bound follower ticket (gated, idempotent, single-flight, reduce-only). A DEMO follower takes the
SHADOW/DEMO path; a LIVE follower takes the governed live partial close (``create_live_partial_close_job``)
ONLY with the full live flag stack + COPIER_LIVE_PARTIAL_CLOSE_ENABLED + the matching authority + the host
admission, re-read under the lock, fail-closed otherwise. No-op unless COPIER_ENABLED AND COPIER_EXECUTE_ENABLED.
NOT scheduled. (Not used by the #46->#48 OPEN->MODIFY->CLOSE demo, which grants no partial-close authority.)
"""
from django.core.management.base import BaseCommand

from trade_copier.execution_producer import produce_pending_partial_closes


class Command(BaseCommand):
    help = "Produce enqueue-only PARTIAL-CLOSE jobs from lifecycle partial intents (gated; DEMO or governed LIVE)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **opts):
        self.stdout.write("copier-execute-partial-closes: {}".format(
            produce_pending_partial_closes(limit=opts["limit"])))
