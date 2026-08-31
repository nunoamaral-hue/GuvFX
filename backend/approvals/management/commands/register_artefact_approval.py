"""Register a PENDING artefact approval bound to an exact SHA-256. Creates the human-review request; it does
NOT approve anything. Example:
  register_artefact_approval --kind broker_servers_dat --ref pepperstone/v1 --sha256 <64hex> --metadata '{...}'
"""
import json

from django.core.management.base import BaseCommand, CommandError

from approvals.services import register_pending


class Command(BaseCommand):
    help = "Register a PENDING artefact approval (human-gated; does not approve)."

    def add_arguments(self, parser):
        parser.add_argument("--kind", required=True)
        parser.add_argument("--ref", required=True)
        parser.add_argument("--sha256", required=True)
        parser.add_argument("--metadata", default="{}", help="JSON object of non-secret provenance")

    def handle(self, *args, **opts):
        try:
            meta = json.loads(opts["metadata"] or "{}")
            if not isinstance(meta, dict):
                raise ValueError("metadata must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            raise CommandError(f"bad --metadata: {exc}")
        try:
            row = register_pending(artefact_kind=opts["kind"], artefact_ref=opts["ref"],
                                   sha256=opts["sha256"], metadata=meta)
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stdout.write(
            f"[artefact-approval] id={row.pk} status={row.status} kind={row.artefact_kind} "
            f"ref={row.artefact_ref} sha256_suffix=...{row.sha256[-12:]}")
