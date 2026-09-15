"""build_catalogue_version — assemble a DRAFT catalogue version + artefacts from registered broker approvals.

DB-ONLY + idempotent: creates (or updates) a DRAFT ``CatalogueVersion`` and one ``CatalogueArtefact`` per
``broker_servers_dat`` approval whose ref matches ``<broker_id>/<label>`` (e.g. pepperstone/v1, is6/v1),
pulling non-secret provenance (broker, servers, size, build) from the approval metadata and binding each
artefact to the approval's exact SHA. It NEVER activates the version (activation is the human-gated promotion,
``activate_catalogue_version``, which fail-closes unless every artefact is APPROVED) and NEVER copies host
files — staging the immutable bytes to ``catalogue/versions/<label>/<broker_id>/servers.dat`` on the host is a
separate promotion step. Safe to run repeatedly.

Usage:  build_catalogue_version --label v1
"""
from django.core.management.base import BaseCommand, CommandError

from approvals.models import ArtefactApproval
from broker_catalogue.models import CatalogueArtefact, CatalogueVersion


class Command(BaseCommand):
    help = "Assemble a DRAFT catalogue version + artefacts from broker_servers_dat approvals (DB-only; no activate)."

    def add_arguments(self, parser):
        parser.add_argument("--label", required=True)

    def handle(self, *args, **opts):
        label = opts["label"]
        version, _ = CatalogueVersion.objects.get_or_create(
            label=label, defaults={"status": CatalogueVersion.Status.DRAFT})
        if version.status != CatalogueVersion.Status.DRAFT:
            raise CommandError(f"version {label} is {version.status}; refuse to rebuild a non-DRAFT version")
        approvals = ArtefactApproval.objects.filter(
            artefact_kind="broker_servers_dat", artefact_ref__endswith=f"/{label}")
        if not approvals:
            raise CommandError(f"no broker_servers_dat approvals with ref '<broker>/{label}'")
        built = []
        for appr in approvals:
            broker_id = appr.artefact_ref.split("/", 1)[0]
            meta = appr.metadata or {}
            CatalogueArtefact.objects.update_or_create(
                version=version, broker_id=broker_id,
                defaults=dict(
                    display_name=meta.get("broker", broker_id),
                    servers=meta.get("servers_intended") or meta.get("servers") or [],
                    artefact_kind="broker_servers_dat", artefact_ref=appr.artefact_ref,
                    sha256=appr.sha256.lower(), size_bytes=int(meta.get("size_bytes") or 0),
                    source_mt5_build=str(meta.get("source_mt5_build") or ""),
                    host_relpath=f"versions/{label}/{broker_id}/servers.dat",
                    capture_provenance={k: meta.get(k) for k in ("source", "candidate_id", "sanitisation_basis")},
                    sanitisation_result=meta.get("sanitisation", ""),
                    certification_result=("PASS" if meta.get("behavioural_cert") == "PASS" else "PENDING"),
                    approval=appr if appr.status == ArtefactApproval.Status.APPROVED else None,
                ))
            built.append(f"{broker_id}({appr.status})")
        self.stdout.write(f"[catalogue] DRAFT {label} artefacts={built} "
                          f"(NOT active; run activate_catalogue_version after human approval + host staging)")
