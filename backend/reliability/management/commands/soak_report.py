"""WS-G — soak_report: capture a durable soak-test evidence snapshot + print a grep-friendly
summary. Cron-driven (VPS-side, not Claude-dependent). Read-only aggregation; persists one
SoakSnapshot row per run (unless --no-persist).

    python manage.py soak_report                 # 24h window, persist + print
    python manage.py soak_report --window 1      # 1h window
    python manage.py soak_report --no-persist    # print only (baseline dry-run)
"""
from django.core.management.base import BaseCommand

from reliability.services.soak_report import build_soak_snapshot


class Command(BaseCommand):
    help = "Capture a durable soak-test evidence snapshot (by source) and print a summary."

    def add_arguments(self, parser):
        parser.add_argument("--window", type=int, default=24, help="Window in hours (default 24).")
        parser.add_argument("--no-persist", action="store_true", help="Print only; do not save a row.")

    def handle(self, *args, **opts):
        snap = build_soak_snapshot(window_hours=opts["window"], persist=not opts["no_persist"])
        a = snap["alerts"]
        self.stdout.write(
            f"soak-report: window={snap['window_hours']}h "
            f"alerts[opened={a['opened']} critical={a['critical']} open_now={a['open_now']}]")
        for s in snap["by_source"]:
            pc = s.get("provider_commands") or {}
            self.stdout.write(
                f"  {s['source_label']:<12} signals={s['signals_received']} rejected={s['rejected']} "
                f"promoted={s['plans_promoted']} filled={s['orders_filled']} closed={s['trades_closed']} "
                f"W/L/BE={s['wins']}/{s['losses']}/{s['breakevens']} pnl={s['realised_pnl']} "
                f"be_mods={s['breakeven_modifications']}(ok={s['breakeven_verified']}) "
                f"closes={s['provider_close_jobs']} cmds={sum(pc.values()) if pc else 0} "
                f"cards={s['cards_delivered']}")
        persisted = "" if opts["no_persist"] else " (persisted)"
        self.stdout.write(f"soak-report: done{persisted}")
