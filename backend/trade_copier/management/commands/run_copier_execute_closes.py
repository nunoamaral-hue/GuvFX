"""run_copier_execute_closes — the FULL-CLOSE producer worker (control-plane completion, 2026-11-10).

Consume lifecycle FULL_CLOSE intents and produce the one enqueue-only CLOSE job per bound follower ticket
(gated, idempotent, single-flight). A DEMO follower takes the SHADOW/DEMO path; a LIVE (non-demo) follower
takes the governed live close (``create_live_full_close_job``) ONLY with the full live flag stack + a valid
authorization + the host admission — every gate re-read under the lock, fail-closed otherwise. No-op unless
COPIER_ENABLED AND COPIER_EXECUTE_ENABLED. NOT scheduled — run on demand by an operator.
"""
from django.core.management.base import BaseCommand

from trade_copier.execution_producer import produce_pending_closes


class Command(BaseCommand):
    help = "Produce enqueue-only FULL-CLOSE jobs from lifecycle close intents (gated; DEMO or governed LIVE)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **opts):
        self.stdout.write("copier-execute-closes: {}".format(produce_pending_closes(limit=opts["limit"])))
