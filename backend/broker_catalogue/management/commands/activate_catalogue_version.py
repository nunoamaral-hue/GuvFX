"""activate_catalogue_version — promote a DRAFT catalogue version to ACTIVE (governed, fail-closed).

REFUSES to activate unless EVERY artefact in the version is human-APPROVED for its exact SHA (approvals app).
This is the promotion gate: it never self-approves and never activates a version containing an unapproved or
uncertified artefact. Activation retires the previous ACTIVE version (recording ``rollback_to`` so re-activating
the predecessor is the rollback), and stamps the aggregate manifest SHA. Atomic under a transaction.

Usage:  activate_catalogue_version --label v1
        activate_catalogue_version --label v1 --require-certified   # also require certification_result == PASS
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from broker_catalogue import service as S
from broker_catalogue.models import CatalogueVersion


class Command(BaseCommand):
    help = "Promote a DRAFT catalogue version to ACTIVE, fail-closed unless every artefact is approved."

    def add_arguments(self, parser):
        parser.add_argument("--label", required=True)
        parser.add_argument("--require-certified", action="store_true",
                            help="Also require certification_result == PASS for every artefact (behavioural cert).")

    def handle(self, *args, **opts):
        label = opts["label"]
        with transaction.atomic():
            try:
                version = CatalogueVersion.objects.select_for_update().get(label=label)
            except CatalogueVersion.DoesNotExist:
                raise CommandError(f"no such catalogue version: {label}")
            if version.status == CatalogueVersion.Status.ACTIVE:
                self.stdout.write(f"[catalogue] {label} already ACTIVE"); return
            arts = list(version.artefacts.all())
            if not arts:
                raise CommandError(f"version {label} has no artefacts")
            # Fail-closed promotion gate: every artefact must be human-approved for its exact SHA.
            unapproved = [a.broker_id for a in arts if not S.artefact_is_approved(a)]
            if unapproved:
                raise CommandError(f"REFUSED: unapproved artefact(s): {unapproved}. "
                                   f"Human approval (approvals app) required for each exact SHA before activation.")
            if opts["require_certified"]:
                uncert = [a.broker_id for a in arts if a.certification_result != "PASS"]
                if uncert:
                    raise CommandError(f"REFUSED: uncertified artefact(s): {uncert} (behavioural cert not PASS).")
            # Retire the current ACTIVE (rollback pointer) then activate this one atomically.
            prev = CatalogueVersion.objects.select_for_update().filter(
                status=CatalogueVersion.Status.ACTIVE).first()
            now = timezone.now()
            if prev is not None:
                prev.status = CatalogueVersion.Status.RETIRED
                prev.retired_at = now
                prev.save(update_fields=["status", "retired_at"])
                version.rollback_to = prev
            version.status = CatalogueVersion.Status.ACTIVE
            version.activated_at = now
            version.manifest_sha256 = S.compute_manifest_sha(version)
            version.save(update_fields=["status", "activated_at", "manifest_sha256", "rollback_to"])
        self.stdout.write(f"[catalogue] ACTIVATED {label} manifest_sha256={version.manifest_sha256} "
                          f"artefacts={[a.broker_id for a in arts]}")
