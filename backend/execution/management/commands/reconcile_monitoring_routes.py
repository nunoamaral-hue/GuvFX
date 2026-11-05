"""reconcile_monitoring_routes — default-OFF operator reconciliation for monitoring routes (ADR-0075).

Two-level darkness: a dormant no-op unless BOTH ``MONITORING_GATEWAY_ENABLED`` and
``MONITORING_ROUTE_RECONCILE_ENABLED`` are on. Even then it is READ-ONLY on bridges/ACLs/accounts — it
only refreshes each route's durable readiness/reason code and raises governed reliability alerts. It
adds NO production schedule; an operator (or a separately-reviewed scheduler) invokes it explicitly.
"""
import json

from django.core.management.base import BaseCommand

from execution import monitoring_reconcile


class Command(BaseCommand):
    help = "Reconcile monitoring-route readiness and raise governed alerts (default-OFF, read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--no-alerts", action="store_true",
                            help="Classify + update readiness only; do not open/resolve alerts.")

    def handle(self, *args, **options):
        from execution.monitoring_flags import (monitoring_gateway_enabled,
                                                 monitoring_route_reconcile_enabled)
        if not (monitoring_gateway_enabled() and monitoring_route_reconcile_enabled()):
            self.stdout.write("monitoring route reconcile DISABLED (flags off) — no-op")
            return
        summary = monitoring_reconcile.reconcile_routes(emit_alerts=not options.get("no_alerts"))
        self.stdout.write(json.dumps(summary, default=str, indent=2))
