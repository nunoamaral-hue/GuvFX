"""sanitise_broker_artefact — run the reusable content sanitiser over a candidate broker ``servers.dat`` and print
the machine verdict JSON. The verdict object goes into the approval metadata's ``sanitiser`` field; activation
requires ``metadata.sanitiser.passed == True`` (no operator free-text). Read-only; touches no DB.

The scan is a BOUNDED heuristic (see ``broker_catalogue.sanitiser``): it proves absence of the supplied identity
terms in common encodings and reports high-entropy regions; it does not prove absence of every possible secret.

Usage:  sanitise_broker_artefact --file /path/servers.dat --identity 830227146 --identity support@guvfx.com
"""
import json

from django.core.management.base import BaseCommand, CommandError

from broker_catalogue.sanitiser import scan_file


class Command(BaseCommand):
    help = "Scan a candidate broker servers.dat for identity terms + entropy; print the machine sanitiser verdict."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--identity", action="append", default=[],
                            help="Identity term to prove absent (repeatable): login, email, holder name, etc.")

    def handle(self, *args, **opts):
        try:
            verdict = scan_file(opts["file"], identity_terms=opts["identity"])
        except OSError as e:
            raise CommandError(f"cannot read {opts['file']}: {e.__class__.__name__}")
        self.stdout.write(json.dumps(verdict, sort_keys=True))
