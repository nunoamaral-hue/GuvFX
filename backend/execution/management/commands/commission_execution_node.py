"""Commission a hosted execution NODE's order path (ADR-0048) — deterministic, idempotent, no order.

Makes a TerminalNode fit for automated hosted service by registering/authorizing its DEDICATED
node-aware order worker and verifying ``node_execution_operational``. Identical for Node 2, 3, 4, …
(no account-specific code). A commissioned node authorises NO customer and places NO order.

The worker secret is taken from ``$GUVFX_NODE_WORKER_SECRET`` (never a CLI arg, never logged).

Usage::

    GUVFX_NODE_WORKER_SECRET=... python manage.py commission_execution_node \
        --node-hostname guvfx-beta-node-1 --worker-id mt5-node2-order-1            # dry-run
    GUVFX_NODE_WORKER_SECRET=... python manage.py commission_execution_node \
        --node-hostname guvfx-beta-node-1 --worker-id mt5-node2-order-1 --apply    # register + authorize
"""
import json

from django.core.management.base import BaseCommand

from execution.node_commission import (
    DEFAULT_STALE_OLDER_THAN_SECONDS,
    commission_execution_node,
)


class Command(BaseCommand):
    help = ("Commission a hosted execution node's order path (dedicated worker + node grant + verify). "
            "DRY-RUN by default; --apply to register/authorize. Refuses Customer Zero nodes; refuses while "
            "stale PENDING orders exist; places no order; arms no customer.")

    def add_arguments(self, parser):
        parser.add_argument("--node-hostname", required=True)
        parser.add_argument("--worker-id", required=True,
                            help="Dedicated node worker identity (must differ from legacy/other-node workers).")
        parser.add_argument("--apply", action="store_true",
                            help="Register/authorize the worker. Default: dry-run (no change).")
        parser.add_argument("--require-liveness", action="store_true",
                            help="Require the worker to have been seen recently (runtime check).")
        parser.add_argument("--bridge-url", default="",
                            help="Optional: persist the node's order bridge URL (never overwrites a different "
                                 "existing URL). No host contact.")
        parser.add_argument("--older-than-seconds", type=int, default=DEFAULT_STALE_OLDER_THAN_SECONDS,
                            help="Stale-order window for the reconcile-first guard (default %(default)s).")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **opts):
        report = commission_execution_node(
            node_hostname=opts["node_hostname"], worker_id=opts["worker_id"], apply=opts["apply"],
            require_liveness=opts["require_liveness"], bridge_url=opts["bridge_url"],
            stale_older_than_seconds=opts["older_than_seconds"],
        )
        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
            return
        mode = "APPLY" if report["apply"] else "DRY-RUN"
        self.stdout.write(f"commission_execution_node [{mode}] node={report['node_hostname']} "
                          f"worker={report['worker_id']}")
        self.stdout.write(f"  applied={report['applied']}  operational={report['operational']}  "
                          f"reason={report['reason']}")
        if report["checks"]:
            self.stdout.write(f"  checks={report['checks']}")
        if not report["operational"]:
            self.stdout.write(self.style.WARNING(
                "  node NOT execution-operational — resolve the reason above before it accepts automated "
                "hosted customers (a commissioned node authorises no customer and places no order)."))
        else:
            self.stdout.write(self.style.SUCCESS("  node execution-operational."))
