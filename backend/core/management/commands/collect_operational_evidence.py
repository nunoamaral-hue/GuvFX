"""collect_operational_evidence — emit a schema-conformant Operational Readiness evidence manifest.

    python manage.py collect_operational_evidence --packet-id OPS-READINESS --handoff-id <id>
    python manage.py collect_operational_evidence ... --out evidence/manifests/ops-readiness.json

READ-ONLY: runs the health rollup + pre-flight + rollback plan and records them as machine-readable
evidence (``evidence/schema/evidence-manifest.schema.json``). Git/time facts are resolved here so the
pure builder stays deterministic. Writes to ``--out`` if given, else stdout. MUTATES NO SYSTEM STATE.
"""
import json
import subprocess
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from core.operational_evidence import build_operational_evidence


def _git(*args) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=10,
                              check=False).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


class Command(BaseCommand):
    help = "Collect a read-only Operational Readiness evidence manifest (schema-conformant)."

    def add_arguments(self, parser):
        parser.add_argument("--packet-id", default="OPS-READINESS")
        parser.add_argument("--handoff-id", default="")
        parser.add_argument("--reviewer", default=None)
        parser.add_argument("--out", default="", help="Write manifest to this path (else stdout).")

    def handle(self, *args, **opts):
        created = datetime.now(timezone.utc).isoformat()
        handoff = opts["handoff_id"] or f"ops-readiness-{_git('rev-parse', '--short', 'HEAD')}"
        manifest = build_operational_evidence(
            packet_id=opts["packet_id"],
            handoff_id=handoff,
            created_at_utc=created,
            branch=_git("rev-parse", "--abbrev-ref", "HEAD"),
            base_commit=_git("merge-base", "origin/main", "HEAD"),
            head_commit=_git("rev-parse", "HEAD"),
            reviewer=opts["reviewer"],
        )
        blob = json.dumps(manifest, indent=2, sort_keys=True)
        out = opts["out"]
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(blob + "\n")
            self.stdout.write(f"wrote {out} (status={manifest['status']})")
        else:
            self.stdout.write(blob)
