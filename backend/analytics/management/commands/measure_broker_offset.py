"""measure_broker_offset — READ ONLY evidence for each broker server's UTC offset.

Estimates offset = median(open_time - created_at) over recent trades. ``open_time`` is broker SERVER wall-time
(mislabeled UTC); ``created_at`` (Django auto_now_add, true server UTC) is when GuvFX first wrote the row, ~seconds
after the broker open, so the difference ~ the broker's current UTC offset. This is EVIDENCE for choosing an
authoritative ``BrokerServer.server_timezone`` (IANA); it never sets it (packet B3: do not guess/rewrite). A single
measurement gives the CURRENT-season offset only — confirm the IANA zone (which encodes DST) before setting it.
"""
import statistics
from collections import defaultdict

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Measure observed broker-server UTC offset per server (evidence for server_timezone). READ ONLY."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=3000, help="Recent trades to sample.")

    def handle(self, *args, **opts):
        from trading.models import Trade
        offs = defaultdict(list)
        rows = (Trade.objects.filter(open_time__isnull=False, created_at__isnull=False)
                .select_related("account__broker_server").order_by("-id")[:opts["limit"]])
        for t in rows:
            bs = getattr(getattr(getattr(t, "account", None), "broker_server", None), "server_name", None)
            if bs and t.open_time and t.created_at:
                offs[bs].append((t.open_time - t.created_at).total_seconds() / 3600.0)
        if not offs:
            self.stdout.write("no trades with open_time+created_at to measure")
            return
        for bs, vals in sorted(offs.items()):
            med = round(statistics.median(vals), 2)
            lo, hi = round(min(vals), 2), round(max(vals), 2)
            self.stdout.write(
                f"{bs}: n={len(vals)} median_offset_h={med} range=[{lo},{hi}] "
                f"(open_time - created_at ~ broker UTC offset; pick an IANA zone whose CURRENT offset == {med})")
