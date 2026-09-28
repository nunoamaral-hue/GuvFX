"""rollback_catalogue_version — explicit, atomic rollback to a known-good RETIRED catalogue version.

Restores the previous authoritative catalogue WITHOUT rebuilding from any customer runtime: it re-activates a
RETIRED version (typically the current ACTIVE version's ``rollback_to`` predecessor) and retires the current
ACTIVE, in one transaction. Fail-closed: refuses unless the target is RETIRED, its manifest still verifies
(integrity), and every artefact is still human-approved for its exact SHA. On any failure the current ACTIVE
version remains authoritative.

Usage:  rollback_catalogue_version --to v1
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from broker_catalogue import service as S
from broker_catalogue.models import CatalogueVersion


class Command(BaseCommand):
    help = "Atomically re-activate a RETIRED catalogue version (rollback), fail-closed."

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="Label of the RETIRED version to re-activate.")

    def handle(self, *args, **opts):
        label = opts["to"]
        with transaction.atomic():
            try:
                target = CatalogueVersion.objects.select_for_update().get(label=label)
            except CatalogueVersion.DoesNotExist:
                raise CommandError(f"no such catalogue version: {label}")
            if target.status == CatalogueVersion.Status.ACTIVE:
                self.stdout.write(f"[catalogue] {label} already ACTIVE"); return
            if target.status != CatalogueVersion.Status.RETIRED:
                raise CommandError(f"REFUSED: {label} is {target.status}; rollback re-activates a RETIRED version only")
            arts = list(target.artefacts.select_for_update().order_by("broker_id"))
            # Integrity (requires the STRONG algo — a legacy/weaker version is refused, closing the downgrade path).
            if not S.verify_version_integrity(target, artefacts=arts):
                raise CommandError(f"REFUSED: {label} failed manifest integrity / non-strong algo — "
                                   f"will not re-activate a tampered or legacy version")
            unapproved = [a.broker_id for a in arts if not S.artefact_is_approved(a)]
            if unapproved:
                raise CommandError(f"REFUSED: {label} has unapproved artefact(s): {unapproved}")
            # Parity with activation: re-check unambiguous routing on the version being restored.
            dups = S.duplicate_server_names(target, artefacts=arts)
            if dups:
                raise CommandError(f"REFUSED: {label} has server name(s) claimed by multiple artefacts: {dups}")
            now = timezone.now()
            cur = CatalogueVersion.objects.select_for_update().filter(
                status=CatalogueVersion.Status.ACTIVE).first()
            if cur is not None:
                cur.status = CatalogueVersion.Status.RETIRED
                cur.retired_at = now
                cur.save(update_fields=["status", "retired_at"])
            target.status = CatalogueVersion.Status.ACTIVE
            target.activated_at = now
            target.rollback_to = cur
            target.save(update_fields=["status", "activated_at", "rollback_to"])
        self.stdout.write(f"[catalogue] ROLLED BACK to {label} (manifest {target.manifest_sha256[:12]})")
