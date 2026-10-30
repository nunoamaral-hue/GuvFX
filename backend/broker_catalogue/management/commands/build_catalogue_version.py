"""build_catalogue_version — assemble a DRAFT catalogue version + artefacts from registered broker approvals.

DB-ONLY + idempotent + DETERMINISTic: creates (or updates) a DRAFT ``CatalogueVersion`` and one
``CatalogueArtefact`` per broker from the ``broker_servers_dat`` approvals whose ref matches ``<broker_id>/<label>``
(e.g. pepperstone/v1). For each broker it binds the SINGLE APPROVED row (never a stray PENDING/REJECTED — no
last-write-wins), pulling non-secret provenance (broker, servers, size, build) and the machine sanitiser verdict
from the approval metadata, and binding the artefact to the approval's exact SHA + size + servers. It NEVER
activates the version (activation is the human-gated ``activate_catalogue_version``, which additionally enforces the
sanitiser gate, server-name uniqueness and — with ``--attest-host`` — host byte attestation) and NEVER copies host
files. Refuses to rebuild a non-DRAFT (immutable) version. Deterministic: brokers processed in sorted order; the
resulting manifest is order-independent. Safe to run repeatedly.

Usage:  build_catalogue_version --label v1
"""
from django.core.management.base import BaseCommand, CommandError

from approvals.models import ArtefactApproval
from broker_catalogue import service as S
from broker_catalogue.models import CatalogueArtefact, CatalogueVersion


class Command(BaseCommand):
    help = "Assemble a DRAFT catalogue version + artefacts from broker_servers_dat approvals (DB-only; no activate)."

    def add_arguments(self, parser):
        parser.add_argument("--label", required=True)
        # Option B — explicit, fail-closed carry-forward-active mode. Carries the single ACTIVE version's
        # already-approved artefacts into the new DRAFT and adds ONLY the explicitly-named, separately-APPROVED
        # refs. Never auto-approves; never implicitly replaces an active broker (see broker_catalogue.carry_forward).
        parser.add_argument("--carry-forward-active", action="store_true",
                            help="Carry the ACTIVE version's approved artefacts into the new DRAFT, plus the "
                                 "explicitly listed --include-approved-ref additions (all must be APPROVED).")
        parser.add_argument("--include-approved-ref", action="append", default=[], dest="include_refs",
                            metavar="<broker>/<label>",
                            help="An explicit extra APPROVED ref to add in carry-forward mode (repeatable). "
                                 "Each broker must NOT already be in the ACTIVE set (no implicit replacement).")

    def handle(self, *args, **opts):
        label = opts["label"]
        if opts.get("carry_forward_active"):
            from broker_catalogue.carry_forward import build_carry_forward_draft, CarryForwardError, inventory_rows
            try:
                version = build_carry_forward_draft(target_label=label, include_refs=opts.get("include_refs") or [])
            except CarryForwardError as e:
                raise CommandError(str(e))
            rows = inventory_rows(version)
            self.stdout.write(f"[catalogue] carry-forward DRAFT {label} (status={version.status}) "
                              f"artefacts={len(rows)}:")
            for r in rows:
                self.stdout.write(f"  - {r['broker_id']} ref={r['artefact_ref']} sha={r['sha256'][:12]} "
                                  f"size={r['size_bytes']} servers={r['servers']} relpath={r['host_relpath']} "
                                  f"approved={r['approved']}")
            self.stdout.write("[catalogue] DRAFT only — NOT active; stage bytes + run activate_catalogue_version "
                              "--attest-host after host staging.")
            return
        if opts.get("include_refs"):
            raise CommandError("--include-approved-ref requires --carry-forward-active")
        version, _ = CatalogueVersion.objects.get_or_create(
            label=label, defaults={"status": CatalogueVersion.Status.DRAFT})
        if version.status != CatalogueVersion.Status.DRAFT:
            raise CommandError(f"version {label} is {version.status}; refuse to rebuild a non-DRAFT version")

        rows = ArtefactApproval.objects.filter(
            artefact_kind="broker_servers_dat", artefact_ref__endswith=f"/{label}")
        if not rows:
            raise CommandError(f"no broker_servers_dat approvals with ref '<broker>/{label}'")

        # Group by broker_id; select the SINGLE APPROVED row per broker (deterministic — never last-write-wins).
        by_broker: dict = {}
        for appr in rows:
            broker_id = appr.artefact_ref.split("/", 1)[0]
            by_broker.setdefault(broker_id, []).append(appr)

        built = []
        seen_servers: dict = {}
        for broker_id in sorted(by_broker):                       # explicit order -> deterministic layout
            if not broker_id or not broker_id.replace("_", "").isalnum() or len(broker_id) > 32:
                raise CommandError(f"REFUSED: malformed broker_id slug: {broker_id!r}")
            approved = [a for a in by_broker[broker_id] if a.status == ArtefactApproval.Status.APPROVED]
            if len(approved) > 1:
                raise CommandError(f"REFUSED: {broker_id} has {len(approved)} APPROVED approvals for '{broker_id}/"
                                   f"{label}'; expected exactly one exact-SHA approval")
            appr = approved[0] if approved else None
            src = appr or sorted(by_broker[broker_id], key=lambda a: a.id)[0]   # bind identity even while pending
            meta = src.metadata or {}
            servers = [str(s).strip() for s in (meta.get("servers_intended") or meta.get("servers") or []) if str(s).strip()]
            # Each server must actually belong to this broker (no cross-broker/foreign server -> mis-routed preseed).
            own = S.server_ownership_problems(broker_id, servers)
            if own:
                raise CommandError(f"REFUSED: {broker_id} server ownership: {own}")
            # Server-name uniqueness across the whole version (case-insensitive) — no two brokers may claim a server.
            for s in servers:
                low = s.lower()
                if low in seen_servers and seen_servers[low] != broker_id:
                    raise CommandError(f"REFUSED: server '{s}' claimed by both {seen_servers[low]} and {broker_id}")
                seen_servers[low] = broker_id
            san = meta.get("sanitiser") or {}
            CatalogueArtefact.objects.update_or_create(
                version=version, broker_id=broker_id,
                defaults=dict(
                    display_name=meta.get("broker", broker_id),
                    servers=servers,
                    artefact_kind="broker_servers_dat", artefact_ref=src.artefact_ref,
                    sha256=src.sha256.lower(), size_bytes=int(meta.get("size_bytes") or 0),
                    source_mt5_build=str(meta.get("source_mt5_build") or ""),
                    host_relpath=f"versions/{label}/{broker_id}/servers.dat",
                    capture_provenance={k: meta.get(k) for k in ("source", "candidate_id", "sanitisation_basis")}
                    | {"sanitiser_version": san.get("version", ""),
                       "sanitiser_evidence_sha256": san.get("evidence_sha256", "")},
                    sanitisation_result=("PASS" if san.get("passed") is True else
                                         (meta.get("sanitisation") or "")),   # machine verdict wins; legacy fallback
                    certification_result=("PASS" if meta.get("behavioural_cert") == "PASS" else "PENDING"),
                    approval=appr if (appr and appr.status == ArtefactApproval.Status.APPROVED) else None,
                ))
            built.append(f"{broker_id}({src.status})")

        dups = S.duplicate_server_names(version)
        if dups:
            raise CommandError(f"REFUSED: duplicate server names across the version: {dups}")
        self.stdout.write(f"[catalogue] DRAFT {label} artefacts={built} "
                          f"(NOT active; run activate_catalogue_version after human approval + host staging)")
