"""hosted_workspace_preflight — the authoritative READ-ONLY Hosted Workspace pre-flight.

    python manage.py hosted_workspace_preflight          # human-readable checklist
    python manage.py hosted_workspace_preflight --json    # machine-readable verdict + checks

Verifies (read-only) everything a repository can prove before enabling Hosted Workspace, and is honest
about the external Sponsor/host gates it cannot satisfy. MUTATES NOTHING; parse ``--json`` (``verdict``)
to gate a script.
"""
import json

from django.core.management.base import BaseCommand

from core.preflight import run_preflight


class Command(BaseCommand):
    help = "Read-only Hosted Workspace pre-flight (verdict + per-check status)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json",
                            help="Emit the pre-flight result as JSON.")

    def handle(self, *args, **opts):
        result = run_preflight()
        if opts["as_json"]:
            self.stdout.write(json.dumps(result, indent=2, sort_keys=True))
            return
        self.stdout.write(f"VERDICT: {result['verdict']}   (counts={result['counts']})")
        self.stdout.write("")
        for c in result["checks"]:
            self.stdout.write(f"[{c['status']:<7}] {c['category']:<9} {c['id']:<24} {c['title']}")
            if c["detail"]:
                self.stdout.write(f"           -> {c['detail']}")
        if result["blocking"]:
            self.stdout.write("")
            self.stdout.write("Blocking / external gates:")
            for b in result["blocking"]:
                self.stdout.write(f"  - [{b['status']}] {b['id']}: {b['detail']}")
