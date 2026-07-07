"""
GFX-PKT-BROKER-SYMBOL-DEPLOY-AND-SYNC — broker instrument cache staleness visibility.

Read-only operator view of the ``execution.BrokerInstrument`` cache: when each account was
last synced, how many symbols are enabled, and a clear WARNING when the cache is older than a
threshold (``BROKER_SYMBOLS_STALE_HOURS``, default 48h). Places NO order, does NO network call,
mutates NOTHING — it only reports what ``sync_broker_instruments`` last wrote.

Usage::

    python manage.py broker_instrument_status                 # all accounts with a cache
    python manage.py broker_instrument_status --account 1
    python manage.py broker_instrument_status --account 1 --stale-hours 24
"""
import os

from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Q
from django.utils import timezone

from execution.models import BrokerInstrument
from trading.models import TradingAccount

DEFAULT_STALE_HOURS = int(os.getenv("BROKER_SYMBOLS_STALE_HOURS", "48") or 48)


def broker_instrument_staleness(account, stale_hours: int = DEFAULT_STALE_HOURS) -> dict:
    """Return a staleness summary for one account. Pure DB read — no network, no order.

    ``last_synced`` is the most recent ``synced_at`` across the account's rows (None if never
    synced). ``stale`` is True when the cache is absent or older than ``stale_hours``.
    """
    agg = BrokerInstrument.objects.filter(account=account).aggregate(
        total=Count("id"),
        enabled=Count("id", filter=Q(enabled=True)),
        last_synced=Max("synced_at"),
    )
    last = agg["last_synced"]
    age_hours = None
    if last is not None:
        age_hours = (timezone.now() - last).total_seconds() / 3600.0
    stale = last is None or (age_hours is not None and age_hours > stale_hours)
    return {
        "account_id": getattr(account, "id", None),
        "total": agg["total"] or 0,
        "enabled": agg["enabled"] or 0,
        "disabled": (agg["total"] or 0) - (agg["enabled"] or 0),
        "last_synced": last,
        "age_hours": age_hours,
        "stale": stale,
        "stale_hours_threshold": stale_hours,
    }


class Command(BaseCommand):
    help = "Report BrokerInstrument cache freshness per account (read-only; warns if stale)."

    def add_arguments(self, parser):
        parser.add_argument("--account", type=int, default=None,
                            help="Account id to report (default: all accounts that have a cache).")
        parser.add_argument("--stale-hours", type=int, default=DEFAULT_STALE_HOURS,
                            help=f"Staleness threshold in hours (default {DEFAULT_STALE_HOURS}).")

    def handle(self, *args, **opts):
        stale_hours = opts["stale_hours"]
        if opts["account"] is not None:
            accounts = list(TradingAccount.objects.filter(pk=opts["account"]))
            if not accounts:
                self.stderr.write(f"broker-symbols: account {opts['account']} not found")
                return
        else:
            ids = (BrokerInstrument.objects.values_list("account_id", flat=True).distinct())
            accounts = list(TradingAccount.objects.filter(pk__in=list(ids)).order_by("id"))
            if not accounts:
                self.stdout.write("broker-symbols: no account has a synced BrokerInstrument cache yet "
                                  "(run: manage.py sync_broker_instruments --account <id>)")
                return

        any_stale = False
        for acct in accounts:
            s = broker_instrument_staleness(acct, stale_hours)
            if s["last_synced"] is None:
                any_stale = True
                self.stdout.write(
                    f"broker-symbols: acct#{s['account_id']} NEVER SYNCED — cache empty; "
                    f"registry falls back to the baseline allowlist. Run sync_broker_instruments."
                )
                continue
            age = s["age_hours"]
            when = s["last_synced"].isoformat()
            line = (f"broker-symbols: acct#{s['account_id']} last_synced={when} "
                    f"(age={age:.1f}h) enabled={s['enabled']}/{s['total']} disabled={s['disabled']}")
            if s["stale"]:
                any_stale = True
                self.stdout.write(
                    f"{line}  ** STALE ** (> {stale_hours}h) — re-run sync_broker_instruments; a "
                    f"delisted/renamed symbol may still resolve until refreshed."
                )
            else:
                self.stdout.write(f"{line}  OK (fresh, <= {stale_hours}h).")
        if any_stale:
            self.stdout.write("broker-symbols: at least one cache is STALE or missing — see above.")
