"""
GFX-PKT-E3-NODE-ASSIGNMENT-ENFORCEMENT — read-only terminal-node audit.

Reports, for every TradingAccount, whether it would pass terminal-node
enforcement (`RISK_REQUIRE_TERMINAL_NODE`): an assigned node in operator-declared
ACTIVE status. Pre-E3 checklist item: every account that can carry real orders
must PASS before the enforcement flag is enabled in production.

Read-only: no writes, no MT5/network calls, no secrets printed.

Usage::

    python manage.py audit_node_assignments            # report only (exit 0)
    python manage.py audit_node_assignments --strict    # exit 1 if any account fails
"""

import os

from django.core.management.base import BaseCommand

from trading.models import TradingAccount


class Command(BaseCommand):
    help = (
        "Read-only audit: which accounts would pass terminal-node enforcement "
        "(RISK_REQUIRE_TERMINAL_NODE). --strict exits 1 if any would fail."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict", action="store_true",
            help="Exit non-zero if any account would fail enforcement.",
        )

    def handle(self, *args, **options):
        failures = 0
        accounts = TradingAccount.objects.select_related("terminal_node").order_by("id")
        for acct in accounts:
            node = acct.terminal_node
            node_label = f"{node.hostname} ({node.status})" if node else "(none)"
            # Evaluate the enforcement verdict as if the flag were ON, regardless
            # of the current env, so the audit is meaningful pre-enablement.
            if node is None:
                verdict = "FAIL account_node_unassigned"
            elif node.status != node.Status.ACTIVE:
                verdict = "FAIL node_not_active"
            else:
                verdict = "PASS"
            if verdict != "PASS":
                failures += 1
            kind = "demo" if acct.is_demo else "LIVE"
            self.stdout.write(
                f"account={acct.id} [{kind}] name={acct.name!r} node={node_label} -> {verdict}"
            )

        total = accounts.count()
        self.stdout.write(f"audited={total} pass={total - failures} fail={failures}")
        flag = os.getenv("RISK_REQUIRE_TERMINAL_NODE", "") or "(unset — enforcement OFF)"
        self.stdout.write(f"RISK_REQUIRE_TERMINAL_NODE={flag}")
        if options["strict"] and failures:
            raise SystemExit(1)
