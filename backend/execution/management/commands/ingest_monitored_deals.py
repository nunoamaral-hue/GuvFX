"""ingest_monitored_deals — enqueue read-only deal-history syncs for MONITORED accounts.

DARK unless ``MONITORED_DEALS_INGEST_ENABLED`` (or --force). Enqueues one ``SYNC_POSITIONS``
ExecutionJob per connected account that owns a READY per-tenant read endpoint and lacks an
in-flight sync, so the dashboard's realized metrics (Net P&L, Win Rate, Profit Factor, Max
Drawdown, Expectancy, closed-trade count, daily realized P&L) are ingested for MONITORING-only
accounts — not just accounts holding open execution plans. The enqueued job is read-only (the
worker reads ``/mt5/snapshots/deals`` and upserts trading.Trade rows, identity-firewalled and
idempotent); it NEVER places, modifies or closes an order.

Intended cadence: a ~15-minute cron (see deploy/monitored-deals, mirroring deploy/equity-snapshots).
See execution.monitored_ingest for the per-account guarantees (idempotent dedup, fail-closed on
missing identity, quiet re-probe backoff on repeated failures).
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand

_TRUTHY = ("1", "true", "yes", "on")


def _flag(name: str, default: str = "") -> bool:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


class Command(BaseCommand):
    help = ("Enqueue read-only SYNC_POSITIONS deal-history syncs for monitored accounts. "
            "DARK unless MONITORED_DEALS_INGEST_ENABLED (or --force).")

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Run even if MONITORED_DEALS_INGEST_ENABLED is off (controlled validation).")
        parser.add_argument("--account-id", type=int, default=None,
                            help="Limit to one TradingAccount id (bypasses the monitored-set query).")
        parser.add_argument("--cooldown-minutes", type=int, default=None,
                            help="Override the failed-probe re-enqueue backoff (minutes; 0 disables).")

    def handle(self, *args, **opts):
        if not (_flag("MONITORED_DEALS_INGEST_ENABLED") or opts["force"]):
            self.stdout.write("monitored deal ingest DARK (MONITORED_DEALS_INGEST_ENABLED off); no-op")
            return

        from execution.monitored_ingest import enqueue_monitored_deal_syncs, DEFAULT_FAILURE_COOLDOWN_MIN

        account_ids = [opts["account_id"]] if opts["account_id"] else None
        cooldown = opts["cooldown_minutes"] if opts["cooldown_minutes"] is not None else DEFAULT_FAILURE_COOLDOWN_MIN

        summary = enqueue_monitored_deal_syncs(account_ids=account_ids, failure_cooldown_minutes=cooldown)
        self.stdout.write(
            "monitored deal ingest: considered={considered} queued={queued} "
            "skipped_in_flight={skipped_in_flight} skipped_cooldown={skipped_cooldown} "
            "skipped_no_identity={skipped_no_identity} missing={missing}".format(**summary))
