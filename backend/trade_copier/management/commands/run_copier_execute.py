"""run_copier_execute — the OPEN producer. Consume SHADOW_RECORDED OPEN replication intents and produce the
one enqueue-only ExecutionJob for each (gated, idempotent, single-flight under ``select_for_update``).

No-op unless COPIER_ENABLED AND COPIER_EXECUTE_ENABLED are on (re-checked under the lock).

DEMO follower → SHADOW path: PLACE_ORDER_SHADOW (suppressed) unless COPIER_EXECUTE_DEMO_ARMED, then a DEMO
PLACE_ORDER. LIVE (non-demo) follower → the GOVERNED LIVE OPEN path:
``execution_producer.produce_one`` routes a LIVE follower through ``live_execution.admit_open_live`` →
``create_live_open_job`` (a truthful LIVE ``PLACE_ORDER``), consuming exactly one authorization allowance
unit under the SAME lock so a retry can never place a second order. The LIVE path is itself DARK and
fail-closed: it fires only with COPIER_LIVE_EXECUTE_ENABLED + HOSTED_LIVE_EXECUTION_ENABLED + a valid,
unexpired per-pair ``CopierLiveAuthorization`` + the host RED admission; every gate is re-read inside the
lock. When any gate is OFF the LIVE account is refused fail-closed (``account_not_demo``-equivalent) and no
order is produced. (Corrected 2026-11-10: the prior "DEMO-only / Never LIVE" header predated the ADR-0057
live OPEN producer and was factually wrong — ``produce_one`` DOES reach ``create_live_open_job`` for a fully
authorized LIVE follower.)

Usage::

    python manage.py run_copier_execute
    python manage.py run_copier_execute --limit 50
"""
from django.core.management.base import BaseCommand

from trade_copier.execution_producer import produce_pending


class Command(BaseCommand):
    help = "Produce the enqueue-only OPEN ExecutionJob per intent (gated; DEMO shadow/demo or GOVERNED LIVE)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **opts):
        summary = produce_pending(limit=opts["limit"])
        self.stdout.write("copier-execute (OPEN producer): {}".format(summary))
