"""
AUTO-SHADOW-CLOSE-MONITOR — run the close-monitor once over closed, unprocessed trades.

Classifies closed trades into internal ``TradeOutcomeRecord`` rows (idempotent). Creates NO
order, NO Telegram notification, NO WIMS contract — internal records only. Safe to run
repeatedly; already-recorded trades are skipped.

Usage::

    python manage.py run_close_monitor              # default batch
    python manage.py run_close_monitor --limit 100
"""
from django.core.management.base import BaseCommand

from execution.close_monitor import DEFAULT_LIMIT, process_closed_trades


class Command(BaseCommand):
    help = "Classify closed, not-yet-processed trades into internal TradeOutcomeRecords."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    def handle(self, *args, **opts):
        counts = process_closed_trades(limit=opts["limit"])
        self.stdout.write(
            "close-monitor: processed={processed} win={win} loss={loss} "
            "breakeven={breakeven} skipped={skipped} (internal records only; no order/"
            "notification/WIMS)".format(**counts)
        )
