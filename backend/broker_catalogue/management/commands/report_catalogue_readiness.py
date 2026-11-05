"""report_catalogue_readiness — READ-ONLY per-broker catalogue-readiness report (Stream A / ADR-0078).

Prints each broker's eligibility + stable reason code for a FUTURE additive catalogue build. Performs ONLY
SELECTs: it never builds, activates, stages, approves, captures or writes anything. Safe to run anytime.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from ...readiness import evaluate_catalogue_readiness


class Command(BaseCommand):
    help = "READ-ONLY: report per-broker catalogue-readiness eligibility + reason codes. Writes nothing."

    def add_arguments(self, parser):
        parser.add_argument("--broker", action="append", default=None,
                            help="Broker id to evaluate (repeatable). Omit to evaluate every known broker.")
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    def handle(self, *args, **opts):
        rows = evaluate_catalogue_readiness(opts.get("broker"))
        if opts.get("json"):
            self.stdout.write(json.dumps([r.as_dict() for r in rows], indent=2, sort_keys=True))
            return
        if not rows:
            self.stdout.write("No brokers to evaluate.")
            return
        self.stdout.write("READ-ONLY catalogue readiness (no build/activation performed):")
        for r in rows:
            mark = "ELIGIBLE " if r.eligible else "ineligible"
            self.stdout.write(f"  [{mark}] {r.broker_id:<18} {r.reason_code:<24} {r.detail}")
