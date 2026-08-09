"""operational_health — print the unified Operational Readiness health rollup (READ-ONLY).

    python manage.py operational_health          # human-readable table
    python manage.py operational_health --json   # machine-readable rollup

Never mutates. A pure reporter: parse ``--json`` (``overall`` / ``fault_count``) to gate a script.
"""
import json

from django.core.management.base import BaseCommand

from core.operational_health import build_operational_health


class Command(BaseCommand):
    help = "Print the read-only unified Operational Readiness health rollup."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json",
                            help="Emit the rollup as JSON.")

    def handle(self, *args, **opts):
        rollup = build_operational_health()
        if opts["as_json"]:
            self.stdout.write(json.dumps(rollup, indent=2, sort_keys=True))
        else:
            self.stdout.write(f"OVERALL: {rollup['overall']}   (faults={rollup['fault_count']})")
            self.stdout.write(f"counts: {rollup['counts_by_state']}")
            self.stdout.write("")
            self.stdout.write(f"{'SUBSYSTEM':<20} {'STATE':<17} {'OBS':<4} DETAIL")
            for s in rollup["subsystems"]:
                obs = "yes" if s["observed"] else "no"
                self.stdout.write(f"{s['name']:<20} {s['state']:<17} {obs:<4} {s['detail']}")
            if rollup["awaiting_sponsor"]:
                self.stdout.write("")
                self.stdout.write(f"awaiting Sponsor/host gate: {', '.join(rollup['awaiting_sponsor'])}")
