"""register_capture_candidate — governed NEEDS_CAPTURE candidate registration (Stream A / ADR-0078).

Scaffolding ONLY. Records a governance placeholder that a broker's authoritative ``servers.dat`` is NOT yet
captured (e.g. Dominion Markets). It NEVER creates an artefact, approval, SHA, catalogue version, or any host/
production state. DARK by default: without ``--commit`` AND ``BROKER_CATALOGUE_CAPTURE_WORKFLOW_ENABLED`` armed,
it is a dry-run that reports intent and writes nothing. The STOP boundary: it cannot advance a candidate to
CAPTURED — that is a separate, authorised capture step.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from ...capture import register_needs_capture


class Command(BaseCommand):
    help = ("Register a governed NEEDS_CAPTURE candidate (scaffolding only; never creates an artefact). "
            "Dry-run unless --commit AND BROKER_CATALOGUE_CAPTURE_WORKFLOW_ENABLED are both set.")

    def add_arguments(self, parser):
        parser.add_argument("--broker-id", required=True, help="Stable broker slug, e.g. dominion_markets")
        parser.add_argument("--display-name", required=True, help="Display name, e.g. 'Dominion Markets'")
        parser.add_argument("--server-hint", default="",
                            help="Broker-published non-PII server identity hint, e.g. DominionMarkets-Live")
        parser.add_argument("--reason", default="", help="Non-secret governance note")
        parser.add_argument("--commit", action="store_true",
                            help="Actually write the NEEDS_CAPTURE placeholder (requires the DARK flag armed).")

    def handle(self, *args, **opts):
        try:
            result = register_needs_capture(
                broker_id=opts["broker_id"], display_name=opts["display_name"],
                server_identity_hint=opts["server_hint"], reason=opts["reason"], commit=opts["commit"])
        except ValueError as exc:
            raise CommandError(str(exc))
        if result["written"]:
            verb = "CREATED" if result["created"] else "REFRESHED"
            self.stdout.write(f"{verb} NEEDS_CAPTURE candidate for {result['broker_id']} "
                              f"(status={result['capture_status']}). No artefact/approval/SHA created.")
        elif result["commit_requested"] and not result["flag_armed"]:
            self.stdout.write("DRY-RUN (flag OFF): --commit was requested but "
                              "BROKER_CATALOGUE_CAPTURE_WORKFLOW_ENABLED is not armed; nothing written.")
        else:
            self.stdout.write("DRY-RUN: would register a NEEDS_CAPTURE candidate (pass --commit with the flag "
                              "armed to write). Nothing written.")
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True, default=str))
